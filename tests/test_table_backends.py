import os
import sqlite3

import duckdb
import pytest
import sys
from types import SimpleNamespace

import tinymongo as tm
from tinymongo.errors import (
    DuplicateKeyError,
    OperationFailure,
    StorageCorruptionError,
    TinyMongoNotSupportedError,
)
from tinymongo.indexes import parse_index_spec
from tinymongo.table_backends import (
    DuckDBTableBackend,
    MySQLTableBackend,
    ParquetDuckDBBackend,
    PostgresTableBackend,
    RemoteSQLTableBackend,
    SQLCompiler,
    SQLiteTableBackend,
    TableBackend,
    _duckdb_object_store_settings,
    _duckdb_secret_sql_from_env,
    _duckdb_setup_sql_from_env,
    _import_optional_driver,
    _is_object_store_uri,
    _json_dumps,
    _json_loads,
    _join_uri,
    _reject_remote_unique_arrays,
    matches_filter,
    requires_python_filter,
)


def test_json_loader_accepts_remote_driver_decoded_documents():
    document = {"_id": 1, "nested": {"value": True}}

    assert _json_loads(document) is document


def test_successful_filter_operator_branches():
    doc = {"value": 5, "tags": ["a", "b"], "name": "Ada"}
    filters = [
        {"missing": {"$exists": False}},
        {"value": {"$gt": 4}},
        {"value": {"$gte": 5}},
        {"value": {"$lt": 6}},
        {"value": {"$lte": 5}},
        {"value": {"$ne": 6}},
        {"value": {"$in": [4, 5]}},
        {"value": {"$nin": [6, 7]}},
        {"tags": {"$all": ["a", "b"]}},
        {"name": {"$regex": "^A"}},
        {"value": {"$not": {"$eq": 6}}},
        {"value": {"$eq": 5}},
    ]
    assert all(matches_filter(doc, filter_doc) for filter_doc in filters)

    sql, params = SQLCompiler("sqlite").compile({"missing": {"$exists": True}})
    assert "NOT (" not in sql
    assert "$" in sql and "missing" in sql
    assert params == []


def test_backend_noop_update_and_object_store_initialization(tmp_path):
    backend = SQLiteTableBackend(str(tmp_path / "db.sqlite"))
    backend.insert_many("items", [{"_id": 1, "active": True}])
    assert backend.update_many("items", {"_id": 1}, {"$set": {"active": True}}) == []

    object_backend = TableBackend("s3://bucket/db")
    assert object_backend.path == "s3://bucket/db"
    with object_backend._write_lock():
        assert object_backend.path == "s3://bucket/db"


def test_legacy_migration_empty_collection_branches(tmp_path):
    sqlite_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(sqlite_path))
    conn.execute("CREATE TABLE tinydb(id INTEGER PRIMARY KEY, data TEXT)")
    conn.execute("INSERT INTO tinydb VALUES(1, ?)", ('{"empty": {}}',))
    conn.commit()
    conn.close()
    assert SQLiteTableBackend(str(sqlite_path)).list_collections() == ["empty"]

    duck_path = tmp_path / "legacy.duckdb"
    conn = duckdb.connect(str(duck_path))
    conn.execute("CREATE TABLE tinydb(id INTEGER PRIMARY KEY, data VARCHAR)")
    conn.execute("INSERT INTO tinydb VALUES(1, ?)", ('{"empty": {}}',))
    conn.close()
    assert DuckDBTableBackend(str(duck_path)).list_collections() == ["empty"]


def test_parquet_temporary_file_cleanup_branch(tmp_path, monkeypatch):
    backend = ParquetDuckDBBackend(str(tmp_path / "parquet"))
    monkeypatch.setattr(os, "replace", lambda source, target: None)

    backend._write_rows("items", [("1", '{"_id": 1}')])

    assert not list((tmp_path / "parquet").glob("tmp_items_*.parquet"))


class FakeRemoteStore:
    def __init__(self):
        self.tables = {}
        self.columns = {}
        self.metadata = set()
        self.indexes = {}
        self.index_ddl = []
        self.schema_ddl = []


class FakeRemoteCursor:
    def __init__(self, store):
        self.store = store
        self.rows = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        upper = normalized.upper()
        if upper.startswith("CREATE TABLE"):
            table = self._table_from_create(normalized)
            if "TINYMONGO_" not in table.upper():
                if table not in self.store.tables:
                    self.store.tables[table] = {}
                    self.store.columns[table] = {"_id", "data"}
                    if "DATA_ORDERED" in upper:
                        self.store.columns[table].add("data_ordered")
        elif upper.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX")):
            self.store.index_ddl.append(normalized)
        elif upper.startswith("ALTER TABLE") and "DATA_ORDERED" in upper:
            table = self._table_after(normalized, "TABLE")
            self.store.columns.setdefault(table, {"_id", "data"}).add("data_ordered")
            self.store.schema_ddl.append(normalized)
        elif upper.startswith(("ALTER TABLE", "DROP INDEX")):
            self.store.index_ddl.append(normalized)
        elif upper.startswith("INSERT") and "TINYMONGO_INDEXES" in upper:
            database, collection, name, field, unique = params
            self.store.indexes[(database, collection, name)] = (field, bool(unique))
        elif upper.startswith("INSERT") and "TINYMONGO_COLLECTIONS" in upper:
            self.store.metadata.add(tuple(params))
        elif upper.startswith("SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS"):
            table = params[0]
            self.rows = (
                [(1,)] if "data_ordered" in self.store.columns.get(table, set()) else []
            )
        elif upper.startswith("SELECT DISTINCT"):
            self.rows = sorted({(database,) for database, _ in self.store.metadata})
        elif upper.startswith("SELECT INDEX_NAME"):
            database, collection = params
            self.rows = sorted(
                (name, field, unique)
                for (index_database, index_collection, name), (
                    field,
                    unique,
                ) in self.store.indexes.items()
                if index_database == database and index_collection == collection
            )
        elif upper.startswith("SELECT COLLECTION_NAME"):
            database = params[0]
            self.rows = sorted(
                (collection,)
                for metadata_database, collection in self.store.metadata
                if metadata_database == database
            )
        elif upper.startswith("DROP TABLE"):
            table = self._table_after(normalized, "TABLE IF EXISTS")
            self.store.tables.pop(table, None)
            self.store.columns.pop(table, None)
        elif upper.startswith("DELETE FROM") and "TINYMONGO_COLLECTIONS" in upper:
            self.store.metadata.discard(tuple(params))
        elif upper.startswith("DELETE FROM") and "TINYMONGO_INDEXES" in upper:
            if len(params) == 2:
                database, collection = params
                names = [
                    key
                    for key in self.store.indexes
                    if key[:2] == (database, collection)
                ]
                for key in names:
                    self.store.indexes.pop(key, None)
            else:
                database, collection, name = params
                self.store.indexes.pop((database, collection, name), None)
        elif upper.startswith("DELETE FROM"):
            self.store.tables.setdefault(self._table_after(normalized, "FROM"), {}).pop(
                params[0], None
            )
        elif upper.startswith("SELECT _ID, DATA_ORDERED, DATA"):
            table = self._table_after(normalized, "FROM")
            self.rows = [
                (
                    doc_id,
                    self._payload_columns(value)[1],
                    self._payload_columns(value)[0],
                )
                for doc_id, value in self.store.tables.get(table, {}).items()
            ]
        elif upper.startswith("SELECT DATA_ORDERED, DATA") and "WHERE _ID" in upper:
            table = self._table_after(normalized, "FROM")
            value = self.store.tables.get(table, {}).get(params[0])
            if value is None:
                self.rows = []
            else:
                data, ordered_data = self._payload_columns(value)
                self.rows = [(ordered_data, data)]
        elif upper.startswith("SELECT DATA_ORDERED, DATA"):
            table = self._table_after(normalized, "FROM")
            self.rows = [
                (self._payload_columns(value)[1], self._payload_columns(value)[0])
                for value in self.store.tables.get(table, {}).values()
            ]
        elif upper.startswith("SELECT DATA") and "WHERE _ID" in upper:
            table = self._table_after(normalized, "FROM")
            value = self.store.tables.get(table, {}).get(params[0])
            data = self._payload_columns(value)[0] if value is not None else None
            self.rows = [(data,)] if data is not None else []
        elif upper.startswith("SELECT DATA"):
            table = self._table_after(normalized, "FROM")
            self.rows = [
                (self._payload_columns(value)[0],)
                for value in self.store.tables.get(table, {}).values()
            ]
        elif upper.startswith("UPDATE"):
            table = self._table_after(normalized, "UPDATE")
            if len(params) == 3:
                data, ordered_data, doc_id = params
                self.store.tables.setdefault(table, {})[doc_id] = (
                    data,
                    ordered_data,
                )
            else:
                data, doc_id = params
                self.store.tables.setdefault(table, {})[doc_id] = data
        return self

    def executemany(self, sql, rows):
        if sql.upper().startswith("DELETE FROM"):
            table = self._table_after(" ".join(sql.split()), "FROM")
            target = self.store.tables.setdefault(table, {})
            for (doc_id,) in rows:
                target.pop(doc_id, None)
            return

        table = self._table_after(" ".join(sql.split()), "INTO")
        target = self.store.tables.setdefault(table, {})
        for row in rows:
            doc_id, data = row[:2]
            if (
                doc_id in target
                and "CONFLICT" not in sql.upper()
                and "REPLACE" not in sql.upper()
            ):
                raise RuntimeError("duplicate key")
            target[doc_id] = (data, row[2]) if len(row) == 3 else data

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self):
        pass

    def _payload_columns(self, value):
        if isinstance(value, tuple) and len(value) == 2:
            return value
        return value, None

    def _table_from_create(self, sql):
        return self._clean(sql.split("IF NOT EXISTS", 1)[1].strip().split()[0])

    def _table_after(self, sql, marker):
        return self._clean(sql.split(marker, 1)[1].strip().split()[0])

    def _clean(self, value):
        return value.strip().strip('"').strip("`")


class FakeRemoteConnection:
    def __init__(self, store):
        self.store = store
        self.commits = 0

    def cursor(self):
        return FakeRemoteCursor(self.store)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def test_matches_filter_operator_edges():
    doc = {"name": "Ada", "age": 36, "tags": ["math", "code"], "nested": {"x": 2}}

    assert matches_filter(doc, {})
    assert not matches_filter(doc, "bad")
    assert matches_filter(doc, {"$and": [{"age": {"$gt": 30}}, {"name": "Ada"}]})
    assert matches_filter(doc, {"$or": [{"name": "Grace"}, {"age": {"$lte": 36}}]})
    assert matches_filter(doc, {"$nor": [{"name": "Grace"}, {"age": {"$gt": 40}}]})
    assert not matches_filter(doc, {"$nor": [{"name": "Ada"}]})
    assert not matches_filter(doc, {"age": {"$gt": 40}})
    assert not matches_filter(doc, {"age": {"$gte": 40}})
    assert not matches_filter(doc, {"age": {"$lt": 30}})
    assert not matches_filter(doc, {"age": {"$lte": 30}})
    assert not matches_filter(doc, {"name": {"$ne": "Ada"}})
    assert matches_filter(doc, {"name": {"$nin": ["Grace"]}})
    assert not matches_filter(doc, {"name": {"$nin": ["Ada"]}})
    assert matches_filter(doc, {"name": {"$regex": "^A"}})
    assert not matches_filter(doc, {"name": {"$regex": "^Z"}})
    assert matches_filter(doc, {"name": {"$not": "Grace"}})
    assert not matches_filter(doc, {"name": {"$not": "Ada"}})
    assert matches_filter(doc, {"nested.x": 2})
    assert not matches_filter(doc, {"tags": "missing"})
    assert not matches_filter(doc, {"missing": {"$in": ["Ada"]}})
    assert not matches_filter(doc, {"missing": {"$exists": True}})
    assert not matches_filter(doc, {"missing": {"$eq": 1}})
    assert not matches_filter(doc, {"tags": {"$all": ["missing"]}})
    assert not matches_filter(doc, {"name": {"$unknown": "Ada"}})


def test_array_equality_supports_exact_arrays_and_scalar_membership():
    document = {"items": [1, 2], "nested": [["a", "b"], ["c"]]}

    assert matches_filter(document, {"items": [1, 2]})
    assert matches_filter(document, {"items": 2})
    assert matches_filter(document, {"nested": ["a", "b"]})
    assert not matches_filter(document, {"items": [2, 1]})


def test_python_filter_error_and_option_edges():
    doc = {"value": 5, "name": "Ada"}

    assert not matches_filter(doc, {"value": {"$gt": "not-a-number"}})
    assert not matches_filter(doc, {"name": {"$regex": "["}})
    assert not matches_filter(doc, {"name": {"$options": "i"}})
    assert requires_python_filter({"$where": True})
    assert not requires_python_filter({"$and": [{"_id": 1}]})


@pytest.mark.parametrize("backend_class", [SQLiteTableBackend, DuckDBTableBackend])
def test_local_table_find_falls_back_when_sql_compilation_fails(
    tmp_path, monkeypatch, backend_class
):
    suffix = "sqlite" if backend_class is SQLiteTableBackend else "duckdb"
    backend = backend_class(str(tmp_path / ("fallback." + suffix)))
    backend.insert_many(
        "items",
        [
            {"_id": 1, "name": "Ada"},
            {"_id": 2, "name": "Grace"},
        ],
    )

    def fail_compile(_filter_doc):
        raise ValueError("forced compiler failure")

    monkeypatch.setattr(backend.compiler, "compile", fail_compile)

    assert backend.find("items", {"_id": 2}) == [{"_id": 2, "name": "Grace"}]


def test_sql_compiler_branches():
    sqlite = SQLCompiler("sqlite")
    duckdb = SQLCompiler("duckdb")

    assert sqlite.compile({}) == ("", [])
    assert "json_extract(data, '$.\"name\"')" in sqlite.compile({"name": "Ada"})[0]
    assert duckdb.compile({"active": True})[1][-1] == "true"
    assert "_id IN" in duckdb.compile({"_id": {"$in": [1, 2]}})[0]
    assert "_id !=" in duckdb.compile({"_id": {"$ne": 1}})[0]
    assert " OR " in duckdb.compile({"$or": [{"name": "Ada"}, {"name": "Grace"}]})[0]
    assert "NOT" in duckdb.compile({"$nor": [{"name": "Ada"}, {"name": "Grace"}]})[0]
    assert (
        " AND "
        in duckdb.compile({"$and": [{"age": {"$gt": 1}}, {"age": {"$lt": 9}}]})[0]
    )
    assert "NOT" in duckdb.compile({"missing": {"$exists": False}})[0]
    assert "IN" in duckdb.compile({"name": {"$in": ["Ada", "Grace"]}})[0]
    assert "!=" in duckdb.compile({"name": {"$ne": "Ada"}})[0]
    assert "=" in duckdb.compile({"name": {"$eq": "Ada"}})[0]

    with pytest.raises(ValueError):
        duckdb.compile({"_id": {"$gt": 1}})
    with pytest.raises(ValueError):
        duckdb.compile({"name": {"$regex": "A"}})


def test_table_backend_abstract_methods(tmp_path):
    backend = TableBackend(str(tmp_path / "db"))

    assert backend.close() is None
    backend.find = lambda collection, filter_doc=None: [{"_id": 1}]
    assert backend.all_docs("anything") == [{"_id": 1}]
    assert backend.create_index("anything", "field") == "field_1"
    assert backend.create_index("anything", "field") == "field_1"
    assert (
        backend.create_index("anything", parse_index_spec("email", unique=True))
        == "email_1"
    )
    assert backend.drop_index("anything", "email") is None
    with pytest.raises(OperationFailure, match="Index not found"):
        backend.drop_index("anything", "missing")
    assert backend.drop_index("anything", "field") is None
    assert backend.list_indexes("anything") == [{"name": "_id_", "key": [("_id", 1)]}]
    with pytest.raises(NotImplementedError):
        backend.list_collections()
    with pytest.raises(NotImplementedError):
        backend.create_collection("items")
    with pytest.raises(NotImplementedError):
        backend.drop_collection("items")
    with pytest.raises(NotImplementedError):
        backend.insert_many("items", [])
    with pytest.raises(NotImplementedError):
        backend.replace_one("items", 1, {})
    with pytest.raises(NotImplementedError):
        backend.delete_ids("items", [1])


def test_object_store_uri_helpers_and_env_config(monkeypatch):
    assert _is_object_store_uri("s3://bucket/path")
    assert _is_object_store_uri("gs://bucket/path")
    assert _is_object_store_uri("az://container/path")
    assert not _is_object_store_uri("/tmp/path")
    assert (
        _join_uri("s3://bucket/prefix/", "/db.parquet")
        == "s3://bucket/prefix/db.parquet"
    )

    monkeypatch.setenv("TINYMONGO_S3_REGION", "us-west-004")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("TINYMONGO_S3_ENDPOINT", "s3.us-west-004.backblazeb2.com")
    monkeypatch.setenv("TINYMONGO_S3_USE_SSL", "false")
    monkeypatch.setenv(
        "TINYMONGO_DUCKDB_SETUP_SQL", "SET custom_setting='x'; LOAD httpfs;"
    )
    monkeypatch.setenv("GOOGLE_HMAC_KEY_ID", "gcs-key")
    monkeypatch.setenv("GOOGLE_HMAC_SECRET", "gcs-secret")
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")

    settings = _duckdb_object_store_settings()

    assert settings["s3_region"] == "us-west-004"
    assert settings["s3_access_key_id"] == "key"
    assert settings["s3_secret_access_key"] == "secret"
    assert settings["s3_endpoint"] == "s3.us-west-004.backblazeb2.com"
    assert settings["s3_use_ssl"] is False
    assert "gcs-key" in _duckdb_secret_sql_from_env()[0]
    assert "UseDevelopmentStorage=true" in _duckdb_secret_sql_from_env()[1]
    assert _duckdb_setup_sql_from_env() == ["SET custom_setting='x'", "LOAD httpfs"]


def test_sqlite_backend_duplicate_bypass_drop_and_indexes(tmp_path):
    backend = SQLiteTableBackend(str(tmp_path / "db.sqlite"))
    backend.insert_many("users", [{"_id": 1, "name": "Ada"}])

    with pytest.raises(DuplicateKeyError):
        backend.insert_many("users", [{"_id": 1, "name": "Grace"}])

    with pytest.raises(DuplicateKeyError):
        backend.insert_many(
            "users", [{"_id": 1, "name": "Grace"}], bypass_document_validation=True
        )
    assert backend.find_one("users", {"_id": 1})["name"] == "Ada"
    assert backend.create_index("users", "name") == "name_1"
    assert backend.delete_many("users", {"_id": "missing"}) == []
    assert backend.drop_collection("users") is True
    assert backend.drop_collection("users") is False


def test_sqlite_unique_index_uses_durable_native_ddl(tmp_path):
    path = str(tmp_path / "db.sqlite")
    backend = SQLiteTableBackend(path)
    spec = parse_index_spec("value", unique=True, name="typed_value")

    assert backend.create_index("values", spec) == "typed_value"
    physical_name = backend._physical_index_name("values", spec)
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (physical_name,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert "CREATE UNIQUE INDEX" in row[0]
    assert "tinymongo_unique_token" in row[0]
    backend.insert_many(
        "values",
        [
            {"_id": 1, "value": 1.0},
            {"_id": 2, "value": 1.0000000000000002},
        ],
    )
    where, params = backend.compiler.compile({"value": 1.0})
    conn = sqlite3.connect(path)
    try:
        plan = conn.execute(
            'EXPLAIN QUERY PLAN SELECT data FROM "values"' + where, params
        ).fetchall()
    finally:
        conn.close()
    assert any(physical_name + "_lookup" in item[-1] for item in plan)
    assert SQLiteTableBackend(path).list_indexes("values")[-1]["name"] == (
        "typed_value"
    )

    backend.drop_index("values", "typed_value")
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                (physical_name,),
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                (physical_name + "_lookup",),
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_sqlite_unique_index_keeps_distinct_arbitrary_precision_integers(tmp_path):
    backend = SQLiteTableBackend(str(tmp_path / "large-integers.sqlite"))
    backend.insert_many(
        "values",
        [
            {"_id": 1, "value": 2**63},
            {"_id": 2, "value": 2**63 + 1},
        ],
    )

    assert (
        backend.create_index("values", parse_index_spec("value", unique=True))
        == "value_1"
    )
    with pytest.raises(DuplicateKeyError):
        backend.insert_many("values", [{"_id": 3, "value": 2**63 + 1}])


def test_sqlite_public_index_lookup_unions_scalar_and_array_matches(
    tmp_path, monkeypatch
):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.people
    collection.insert_many(
        [
            {"_id": 1, "email": "ada@example.com"},
            {"_id": 2, "email": ["ada@example.com", "other@example.com"]},
            {"_id": 3, "email": "grace@example.com"},
        ]
    )
    collection.create_index("email", name="email_lookup")

    backend = collection.parent.engine
    traced_sql = []
    original_connect = backend._connect

    def traced_connect():
        conn = original_connect()
        conn.set_trace_callback(traced_sql.append)
        return conn

    monkeypatch.setattr(backend, "_connect", traced_connect)

    assert [doc["_id"] for doc in collection.find({"email": "ada@example.com"})] == [
        1,
        2,
    ]
    assert any("json_type" in sql and "IN ('text'" in sql for sql in traced_sql)
    assert any("json_type" in sql and "= 'array'" in sql for sql in traced_sql)

    spec = parse_index_spec("email", name="email_lookup")
    where, params = backend.compiler.compile({"email": "ada@example.com"})
    path = "'$.\"email\"'"
    sql = (
        'EXPLAIN QUERY PLAN SELECT data FROM "people"'
        + where
        + " AND json_type(data, {0}) IN "
        "('text', 'integer', 'real', 'true', 'false')".format(path)
    )
    conn = original_connect()
    try:
        plan = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    assert any(backend._physical_index_name("people", spec) in row[-1] for row in plan)
    client.close()


def test_sqlite_maps_native_replace_and_index_integrity_errors(tmp_path, monkeypatch):
    backend = SQLiteTableBackend(str(tmp_path / "db.sqlite"))

    class FailingConnection:
        def execute(self, *args, **kwargs):
            raise sqlite3.IntegrityError("native unique conflict")

        def close(self):
            pass

    monkeypatch.setattr(backend, "create_collection", lambda collection: None)
    monkeypatch.setattr(backend, "find", lambda collection, filter_doc: [])
    monkeypatch.setattr(backend, "validate_unique_post_image", lambda *args: None)
    monkeypatch.setattr(backend, "get_index_specs", lambda collection: [])
    monkeypatch.setattr(backend, "_connect", FailingConnection)

    with pytest.raises(DuplicateKeyError, match="native unique conflict"):
        backend.replace_one("users", 1, {"_id": 1})
    with pytest.raises(DuplicateKeyError, match="native unique conflict"):
        backend.create_index("users", parse_index_spec("email"))


def test_duckdb_backend_threads_duplicate_bypass_drop(tmp_path):
    pytest.importorskip("duckdb")
    backend = DuckDBTableBackend(str(tmp_path / "db.duckdb"), threads=2)
    backend.insert_many("users", [{"_id": 1, "name": "Ada"}])

    with pytest.raises(DuplicateKeyError):
        backend.insert_many("users", [{"_id": 1, "name": "Grace"}])

    with pytest.raises(DuplicateKeyError):
        backend.insert_many(
            "users", [{"_id": 1, "name": "Grace"}], bypass_document_validation=True
        )
    assert backend.find_one("users", {"_id": 1})["name"] == "Ada"
    assert backend.delete_many("users", {"_id": "missing"}) == []
    assert backend.drop_collection("users") is True
    assert backend.drop_collection("users") is False


def test_duckdb_does_not_mask_non_constraint_insert_errors(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    backend = DuckDBTableBackend(str(tmp_path / "db.duckdb"))

    class FailingConnection:
        def executemany(self, *args, **kwargs):
            raise RuntimeError("duckdb execution failed")

        def close(self):
            pass

    monkeypatch.setattr(backend, "create_collection", lambda collection: None)
    monkeypatch.setattr(backend, "find", lambda collection, filter_doc: [])
    monkeypatch.setattr(backend, "validate_unique_post_image", lambda *args: None)
    monkeypatch.setattr(backend, "_connect", FailingConnection)

    with pytest.raises(RuntimeError, match="duckdb execution failed"):
        backend.insert_many("users", [{"_id": 1}])


def test_parquet_backend_empty_and_duplicate_paths(tmp_path):
    pytest.importorskip("duckdb")
    backend = ParquetDuckDBBackend(str(tmp_path / "db.parquet"))

    backend.directory = str(tmp_path / "missing.parquet")
    assert backend.list_collections() == []
    backend.directory = str(tmp_path / "db.parquet")
    backend.create_collection("users")
    assert backend.find("users", {}) == []
    assert backend._read_all_rows("users") == []
    backend.insert_many("users", [{"_id": 1, "name": "Ada"}])
    assert backend.list_collections() == ["users"]
    with pytest.raises(DuplicateKeyError):
        backend.insert_many("users", [{"_id": 1, "name": "Grace"}])
    with pytest.raises(DuplicateKeyError):
        backend.insert_many(
            "users", [{"_id": 1, "name": "Grace"}], bypass_document_validation=True
        )
    assert backend.find_one("users", {"_id": 1})["name"] == "Ada"
    assert backend.drop_collection("users") is True
    assert backend.drop_collection("users") is False


def test_parquet_empty_index_collection_and_corrupt_catalog_fail_closed(tmp_path):
    pytest.importorskip("duckdb")
    backend = ParquetDuckDBBackend(str(tmp_path / "parquet"))

    backend.create_index("empty", parse_index_spec("email", unique=True))
    assert "empty" in backend.list_collections()
    assert backend.drop_collection("empty") is True
    assert backend.get_index_specs("empty") == []

    backend.create_index("metadata_only", parse_index_spec("email"))
    os.remove(backend._collection_path("metadata_only"))
    assert backend.drop_collection("metadata_only") is True
    assert backend.get_index_specs("metadata_only") == []

    backend.create_index("users", parse_index_spec("email", unique=True))
    with open(backend._collection_path("__tinymongo_indexes"), "wb") as handle:
        handle.write(b"not a parquet file")

    with pytest.raises(StorageCorruptionError, match="Cannot read Parquet"):
        backend.get_index_specs("users")
    with pytest.raises(StorageCorruptionError, match="Cannot read Parquet"):
        backend.insert_many("users", [{"_id": 1, "email": "same@example.com"}])


def test_parquet_object_store_paths_and_fake_listing(monkeypatch):
    pytest.importorskip("duckdb")
    backend = ParquetDuckDBBackend(
        "s3://bucket/prefix/app.parquet",
        duckdb_config={"s3_region": "auto"},
    )

    assert backend._is_object_store is True
    assert (
        backend._collection_path("users")
        == "s3://bucket/prefix/app.parquet/users.parquet"
    )

    class FakeConn:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append((sql, params))
            if "glob" in sql:
                return self
            raise RuntimeError("not expected")

        def fetchall(self):
            return [
                ("s3://bucket/prefix/app.parquet/users.parquet",),
                ("s3://bucket/prefix/app.parquet/events.parquet",),
            ]

        def close(self):
            pass

    fake = FakeConn()
    monkeypatch.setattr(backend, "_connect", lambda: fake)

    assert backend.list_collections() == ["events", "users"]

    monkeypatch.setattr(backend, "_connect", lambda: RaisingGlobConn())
    assert backend.list_collections() == []


class RaisingGlobConn:
    def execute(self, *args, **kwargs):
        raise RuntimeError("glob failed")

    def close(self):
        pass


def test_parquet_object_store_missing_and_drop_paths(monkeypatch):
    pytest.importorskip("duckdb")
    backend = ParquetDuckDBBackend("s3://bucket/prefix/app.parquet")

    class RaisingConn:
        def execute(self, *args, **kwargs):
            raise RuntimeError("missing")

        def close(self):
            pass

    monkeypatch.setattr(backend, "_connect", lambda: RaisingConn())
    assert backend._read_all_rows("missing") == []
    assert backend.find("missing", {}) == []
    assert backend.drop_collection("missing") is False

    written = []
    monkeypatch.setattr(backend, "list_collections", lambda: ["users"])
    monkeypatch.setattr(
        backend,
        "_write_rows",
        lambda collection, rows: written.append((collection, rows)),
    )

    assert backend.drop_collection("users") is True
    assert written == [("users", []), ("__tinymongo_indexes", [])]


def test_duckdb_object_store_connection_configuration(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    backend = DuckDBTableBackend(
        str(tmp_path / "db.duckdb"),
        duckdb_config={"s3_region": "us-east-1", "s3_use_ssl": True},
    )
    monkeypatch.setenv("TINYMONGO_DUCKDB_SETUP_SQL", "LOAD httpfs; SET custom='ok'")

    class FakeConn:
        def __init__(self):
            self.statements = []

        def execute(self, sql):
            self.statements.append(sql)

    fake = FakeConn()

    backend._configure_duckdb_connection(fake)

    assert "SET s3_region='us-east-1'" in fake.statements
    assert "SET s3_use_ssl=true" in fake.statements
    assert "LOAD httpfs" in fake.statements
    assert "SET custom='ok'" in fake.statements


def test_duckdb_object_store_configuration_ignores_setup_errors(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    backend = DuckDBTableBackend(
        str(tmp_path / "db.duckdb"),
        duckdb_config={"s3_region": "us-east-1"},
    )
    monkeypatch.setenv("GOOGLE_HMAC_KEY_ID", "gcs-key")
    monkeypatch.setenv("GOOGLE_HMAC_SECRET", "gcs-secret")

    class BadConn:
        def execute(self, sql):
            raise RuntimeError(sql)

    backend._configure_duckdb_connection(BadConn())


def test_parquet_object_store_extension_loading():
    pytest.importorskip("duckdb")

    class FakeConn:
        def __init__(self):
            self.statements = []

        def execute(self, sql):
            self.statements.append(sql)
            if sql.startswith("INSTALL"):
                raise RuntimeError("offline")

    s3_backend = ParquetDuckDBBackend("s3://bucket/app.parquet")
    s3_fake = FakeConn()
    s3_backend._load_object_store_extensions(s3_fake)
    assert "LOAD httpfs" in s3_fake.statements

    azure_backend = ParquetDuckDBBackend("az://container/app.parquet")
    azure_fake = FakeConn()
    azure_backend._load_object_store_extensions(azure_fake)
    assert "LOAD azure" in azure_fake.statements


def test_parquet_object_store_connect_loads_extensions(monkeypatch):
    pytest.importorskip("duckdb")
    backend = ParquetDuckDBBackend("s3://bucket/app.parquet")
    loaded = []
    monkeypatch.setattr(
        backend, "_load_object_store_extensions", lambda conn: loaded.append(True)
    )

    conn = backend._connect()
    conn.close()

    assert loaded == [True]


def test_tinymongo_table_backend_api_branches(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="sqlite")
    db = client.app
    db._refresh_table()
    collection = db.users

    assert collection.any_attribute is not collection
    assert collection.any_attribute.name == "users.any_attribute"
    assert collection.create_index("email") == "email_1"
    assert collection.drop_index("email") is None
    assert collection.list_indexes() == [{"name": "_id_", "key": [("_id", 1)]}]
    assert collection.drop() is True
    assert collection.drop() is False

    with pytest.raises(ValueError):
        collection.insert_one("bad")
    with pytest.raises(TypeError):
        collection.insert_many("bad")

    one = collection.insert_one({"email": "one@example.com"})
    many = collection.insert_many([{"email": "two@example.com"}])

    assert one.inserted_id
    assert many.inserted_ids[0]
    assert collection.update_many({}, {"$set": {"active": True}}).modified_count == 2
    assert (
        collection.replace_one({"email": "missing"}, {"email": "none"}).matched_count
        == 0
    )
    assert (
        collection.replace_one(
            {"email": "one@example.com"}, {"email": "one@example.com"}
        ).matched_count
        == 1
    )
    assert (
        collection.find_one_and_update(
            {"email": "two@example.com"}, {"$set": {"active": False}}
        )["active"]
        is True
    )
    assert (
        collection.find_one_and_update(
            {"email": "missing"}, {"$set": {"active": False}}
        )
        is None
    )
    assert collection.delete_one({"email": "one@example.com"}).deleted_count == 1


def test_remote_sql_backend_requires_dsn():
    with pytest.raises(ValueError):
        RemoteSQLTableBackend("", database="app")


def test_optional_driver_error_messages(monkeypatch):
    def missing_import(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(
        "tinymongo.table_backends.importlib.import_module", missing_import
    )

    with pytest.raises(
        ImportError, match='pip install "tinymongo\\[postgres\\]"'
    ) as postgres:
        PostgresTableBackend("", database="app", dsn="postgresql://db")
    assert "optional Python driver 'psycopg'" in str(postgres.value)

    with pytest.raises(
        ImportError, match='pip install "tinymongo\\[mysql\\]"'
    ) as mysql:
        MySQLTableBackend("", database="app", dsn="mysql://db")
    assert "optional Python driver 'pymysql'" in str(mysql.value)

    with pytest.raises(ImportError, match=r"tinymongo\[duckdb\]") as duckdb:
        DuckDBTableBackend("db.duckdb")
    assert "optional Python driver 'duckdb'" in str(duckdb.value)

    with pytest.raises(ImportError, match="pip install example"):
        _import_optional_driver("missing_driver", "example", "pip install example")


def test_remote_sql_backend_defensive_edges(monkeypatch):
    backend = RemoteSQLTableBackend("", database="app", dsn="remote")

    class BadCursor:
        def execute(self, *args, **kwargs):
            raise RuntimeError("execute")

        def executemany(self, *args, **kwargs):
            raise RuntimeError("executemany")

        def close(self):
            raise RuntimeError("close")

    class BadConn:
        def cursor(self):
            return BadCursor()

        def commit(self):
            raise RuntimeError("commit")

    with pytest.raises(RuntimeError, match="execute"):
        backend._execute(BadConn(), "SELECT 1")
    with pytest.raises(RuntimeError, match="executemany"):
        backend._executemany(BadConn(), "SELECT 1", [])
    with pytest.raises(RuntimeError, match="commit"):
        backend._commit(BadConn())
    assert backend._close_cursor(BadCursor()) is None
    assert backend._data_placeholder() == "%s"
    with backend._write_lock():
        pass

    postgres_duplicate = RuntimeError("conflict")
    postgres_duplicate.sqlstate = "23505"
    assert backend._is_duplicate_error(postgres_duplicate)
    assert backend._is_duplicate_error(RuntimeError(1062, "duplicate entry"))
    assert backend._is_duplicate_error(RuntimeError("unique constraint failed"))
    assert not backend._is_duplicate_error(RuntimeError("syntax error"))

    monkeypatch.setattr(backend, "create_collection", lambda collection: None)
    assert backend.delete_ids("users", []) is None
    with pytest.raises(NotImplementedError):
        backend._create_native_index(None, "users", parse_index_spec("email"))
    with pytest.raises(NotImplementedError):
        backend._drop_native_index(None, "users", parse_index_spec("email"))


def _postgres_backend(monkeypatch, store):
    fake_psycopg = SimpleNamespace(connect=lambda dsn: FakeRemoteConnection(store))
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    return PostgresTableBackend("", database="app", dsn="postgresql://db")


def _mysql_backend(monkeypatch, store):
    monkeypatch.setitem(
        sys.modules,
        "pymysql",
        SimpleNamespace(connect=lambda **kwargs: FakeRemoteConnection(store)),
    )
    return MySQLTableBackend("", database="app", dsn="mysql://localhost/db")


def test_postgres_indexes_are_native_durable_unique_and_droppable(monkeypatch):
    store = FakeRemoteStore()
    backend = _postgres_backend(monkeypatch, store)
    backend.insert_many(
        "users",
        [
            {"_id": 1, "email": "one@example.com"},
            {"_id": 2, "email": "two@example.com"},
        ],
    )

    spec = parse_index_spec("email", unique=True)
    assert backend.create_index("users", spec) == "email_1"
    ddl_count = len(store.index_ddl)
    assert backend.create_index("users", spec) == "email_1"
    assert len(store.index_ddl) == ddl_count
    assert backend.list_indexes("users") == [
        {"name": "_id_", "key": [("_id", 1)]},
        {"name": "email_1", "key": [("email", 1)], "unique": True},
    ]
    assert _postgres_backend(monkeypatch, store).list_indexes("users") == (
        backend.list_indexes("users")
    )
    assert any(
        "CREATE UNIQUE INDEX" in sql
        and "COALESCE(jsonb_extract_path(data, 'email'), 'null'::jsonb)" in sql
        and "jsonb_typeof(data)" not in sql
        for sql in store.index_ddl
    )

    with pytest.raises(DuplicateKeyError):
        backend.insert_many("users", [{"_id": 3, "email": "one@example.com"}])
    with pytest.raises(DuplicateKeyError):
        backend.replace_one("users", 2, {"_id": 2, "email": "one@example.com"})

    backend.drop_index("users", "email_1")
    assert backend.list_indexes("users") == [{"name": "_id_", "key": [("_id", 1)]}]
    assert any(sql.startswith("DROP INDEX IF EXISTS") for sql in store.index_ddl)
    with pytest.raises(OperationFailure, match="Index not found"):
        backend.drop_index("users", "email")


def test_remote_unique_creation_preflights_and_drop_cleans_catalog(monkeypatch):
    store = FakeRemoteStore()
    backend = _postgres_backend(monkeypatch, store)
    backend.insert_many(
        "users",
        [
            {"_id": 1, "email": "same@example.com"},
            {"_id": 2, "email": "same@example.com"},
        ],
    )

    with pytest.raises(DuplicateKeyError):
        backend.create_index("users", parse_index_spec("email", unique=True))
    assert backend.list_indexes("users") == [{"name": "_id_", "key": [("_id", 1)]}]
    assert not store.index_ddl

    backend.create_index("users", parse_index_spec("email"))
    with pytest.raises(OperationFailure, match="different options"):
        backend.create_index("users", parse_index_spec("email", unique=True))
    assert backend.drop_collection("users") is True
    assert not store.indexes


def test_remote_native_errors_are_mapped_without_hiding_other_failures(monkeypatch):
    store = FakeRemoteStore()
    backend = _postgres_backend(monkeypatch, store)
    backend.insert_many("users", [{"_id": 1, "email": "one@example.com"}])
    assert backend.find_one("users", {"_id": {"$eq": 1}})["email"] == (
        "one@example.com"
    )
    assert (
        backend.replace_one(
            "users",
            "missing",
            {"_id": "missing", "email": "missing@example.com"},
        )
        is None
    )

    table = backend._table_name("users")
    store.tables[table]["legacy-collision"] = _json_dumps(
        {"_id": 2, "email": "legacy@example.com"}
    )
    assert backend.find_one("users", {"_id": "legacy-collision"}) is None

    original_execute = backend._execute

    def fail_update(message):
        def execute(conn, sql, params=None):
            if sql.lstrip().upper().startswith("UPDATE"):
                raise RuntimeError(message)
            return original_execute(conn, sql, params)

        return execute

    monkeypatch.setattr(backend, "_execute", fail_update("duplicate key"))
    with pytest.raises(DuplicateKeyError):
        backend.replace_one("users", 1, {"_id": 1, "email": "changed@example.com"})

    monkeypatch.setattr(backend, "_execute", fail_update("syntax error"))
    with pytest.raises(RuntimeError, match="syntax error"):
        backend.replace_one("users", 1, {"_id": 1, "email": "changed@example.com"})

    def fail_native(conn, collection, spec):
        raise RuntimeError("duplicate key")

    monkeypatch.setattr(backend, "_execute", original_execute)
    monkeypatch.setattr(backend, "_create_native_index", fail_native)
    spec = parse_index_spec("email", unique=True)
    with pytest.raises(DuplicateKeyError):
        backend.create_index("users", spec)

    def fail_native_syntax(conn, collection, spec):
        raise RuntimeError("syntax error")

    monkeypatch.setattr(backend, "_create_native_index", fail_native_syntax)
    with pytest.raises(RuntimeError, match="syntax error"):
        backend.create_index("users", spec)

    def fail_insert(conn, collection, rows, bypass_document_validation):
        raise RuntimeError("insert syntax error")

    def fail_duplicate_insert(conn, collection, rows, bypass_document_validation):
        raise RuntimeError("duplicate key")

    monkeypatch.setattr(backend, "_insert_rows", fail_duplicate_insert)
    with pytest.raises(DuplicateKeyError):
        backend.insert_many("users", [{"_id": 3, "email": "three@example.com"}])

    monkeypatch.setattr(backend, "_insert_rows", fail_insert)
    with pytest.raises(RuntimeError, match="insert syntax error"):
        backend.insert_many("users", [{"_id": 4, "email": "four@example.com"}])


def test_mysql_uses_type_aware_generated_column_and_rejects_multikey_unique(
    monkeypatch,
):
    store = FakeRemoteStore()
    backend = _mysql_backend(monkeypatch, store)
    backend.insert_many(
        "items",
        [
            {"_id": 1, "value": True, "tags": ["a"]},
            {"_id": 2, "value": 1, "tags": ["b"]},
        ],
    )

    backend.create_index("items", parse_index_spec("value", unique=True))
    with pytest.raises(TinyMongoNotSupportedError, match="does not support array"):
        backend.create_index(
            "items", parse_index_spec("tags", unique=True, name="unique_tags")
        )

    assert _mysql_backend(monkeypatch, store).list_indexes("items") == [
        {"name": "_id_", "key": [("_id", 1)]},
        {"name": "value_1", "key": [("value", 1)], "unique": True},
    ]
    assert any(
        "GENERATED ALWAYS AS" in sql
        and "JSON_TYPE(JSON_EXTRACT(data, '$.\"value\"'))" in sql
        and "JSON_TYPE(data)" not in sql
        and "CONCAT('bool:'" in sql
        and "AS DECIMAL(65, 30)" in sql
        and "SHA2(CASE" in sql
        and "CHAR(64) CHARACTER SET ascii COLLATE ascii_bin" in sql
        for sql in store.index_ddl
    )
    assert sum("CREATE UNIQUE INDEX" in sql for sql in store.index_ddl) == 1

    with pytest.raises(TinyMongoNotSupportedError, match="does not support array"):
        backend.insert_many("items", [{"_id": 3, "value": [3], "tags": ["a"]}])

    backend.drop_index("items", "value_1")
    assert any(sql.startswith("DROP INDEX") for sql in store.index_ddl)
    assert any("DROP COLUMN" in sql for sql in store.index_ddl)

    backend.insert_many("numbers", [{"_id": 1, "value": 1}, {"_id": 2, "value": 1.0}])
    with pytest.raises(DuplicateKeyError):
        backend.create_index("numbers", parse_index_spec("value", unique=True))


def test_remote_array_guard_ignores_nonunique_specs():
    _reject_remote_unique_arrays(
        [{"_id": 1, "tags": ["a", "b"]}],
        [parse_index_spec("tags")],
    )


@pytest.mark.parametrize("backend_name", ["postgres", "mysql"])
def test_remote_unique_array_writes_fail_closed_across_clients(
    monkeypatch, backend_name
):
    store = FakeRemoteStore()
    factory = _postgres_backend if backend_name == "postgres" else _mysql_backend
    first = factory(monkeypatch, store)
    second = factory(monkeypatch, store)
    first.create_index("items", parse_index_spec("tags", unique=True))
    first.insert_many("items", [{"_id": 1, "tags": "a"}])

    with pytest.raises(TinyMongoNotSupportedError, match="multikey uniqueness"):
        second.insert_many("items", [{"_id": 2, "tags": ["a", "b"]}])
    with pytest.raises(TinyMongoNotSupportedError, match="multikey uniqueness"):
        second.replace_one("items", 1, {"_id": 1, "tags": ["a"]})

    assert second.find_one("items", {"_id": 1}) == {"_id": 1, "tags": "a"}
    assert second.find_one("items", {"_id": 2}) is None


def test_postgres_table_backend_with_fake_driver(monkeypatch):
    store = FakeRemoteStore()
    fake_psycopg = SimpleNamespace(connect=lambda dsn: FakeRemoteConnection(store))
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    backend = PostgresTableBackend("", database="app", dsn="postgresql://db")

    backend.insert_many("users", [{"_id": 1, "name": "Ada", "age": 36}])
    with pytest.raises(DuplicateKeyError):
        backend.insert_many("users", [{"_id": 1, "name": "Grace"}])
    with pytest.raises(DuplicateKeyError):
        backend.insert_many(
            "users",
            [{"_id": 1, "name": "Grace", "age": 40}],
            bypass_document_validation=True,
        )
    backend.replace_one("users", 1, {"_id": 1, "name": "Grace", "age": 40})

    assert backend.list_databases() == ["app"]
    assert backend.list_collections() == ["users"]
    assert backend.find_one("users", {"_id": 1})["name"] == "Grace"
    assert backend.find("users", {"age": {"$gte": 40}})[0]["name"] == "Grace"

    backend.update_many("users", {"_id": 1}, {"$inc": {"age": 1}})
    assert backend.find_one("users", {"_id": 1})["age"] == 41

    assert backend.delete_many("users", {"name": "Grace"}) == [1]
    assert backend.find("users", {}) == []
    assert backend.drop_collection("users") is True
    assert backend.drop_collection("users") is False
    assert backend._data_placeholder() == "%s::jsonb"


@pytest.mark.parametrize("backend_factory", [_postgres_backend, _mysql_backend])
def test_remote_dual_payload_preserves_order_and_nonfinite_values(
    monkeypatch,
    backend_factory,
):
    store = FakeRemoteStore()
    backend = backend_factory(monkeypatch, store)
    ordered_id = {"longer": 1, "a": 2}
    reordered_id = {"a": 2, "longer": 1}

    backend.insert_many(
        "values",
        [
            {
                "_id": ordered_id,
                "value": float("nan"),
            },
            {
                "_id": reordered_id,
                "value": float("inf"),
            },
        ],
    )

    raw_values = list(store.tables[backend._table_name("values")].values())
    assert all(isinstance(value, tuple) for value in raw_values)
    assert all(data == ordered_data for data, ordered_data in raw_values)
    assert all(data.startswith("{") for data, _ordered_data in raw_values)
    assert all("NaN" not in data and "Infinity" not in data for data, _ in raw_values)
    assert backend.find_one("values", {"_id": ordered_id})["_id"] == ordered_id
    assert backend.find_one("values", {"_id": reordered_id})["_id"] == reordered_id
    assert backend.find_one("values", {"_id": ordered_id})["value"] != (
        backend.find_one("values", {"_id": ordered_id})["value"]
    )
    assert backend.find_one("values", {"_id": reordered_id})["value"] == float("inf")


@pytest.mark.parametrize("backend_factory", [_postgres_backend, _mysql_backend])
def test_remote_dual_payload_upgrades_and_recovers_legacy_rows(
    monkeypatch,
    backend_factory,
):
    store = FakeRemoteStore()
    backend = backend_factory(monkeypatch, store)
    table = backend._table_name("legacy")
    ordered_id = {"longer": 1, "a": 2}
    database_reordered_id = {"a": 2, "longer": 1}
    legacy_row_id = str(ordered_id)
    store.tables[table] = {
        legacy_row_id: _json_dumps({"_id": database_reordered_id, "label": "legacy"})
    }
    store.columns[table] = {"_id", "data"}

    found = backend.find_one("legacy", {"_id": ordered_id})

    assert found == {"_id": ordered_id, "label": "legacy"}
    assert backend.find_one("legacy", {"label": "legacy"}) == found
    assert "data_ordered" in store.columns[table]
    assert sum("DATA_ORDERED" in sql.upper() for sql in store.schema_ddl) == 1

    backend.replace_one(
        "legacy",
        ordered_id,
        {"_id": ordered_id, "label": "updated"},
    )

    assert isinstance(store.tables[table][legacy_row_id], tuple)
    assert backend.find_one("legacy", {"_id": ordered_id})["label"] == "updated"
    assert backend.find_one("legacy", {"_id": database_reordered_id}) is None


def test_mysql_table_backend_with_fake_driver(monkeypatch):
    store = FakeRemoteStore()
    seen_kwargs = []

    def connect(**kwargs):
        seen_kwargs.append(kwargs)
        return FakeRemoteConnection(store)

    monkeypatch.setitem(sys.modules, "pymysql", SimpleNamespace(connect=connect))

    backend = MySQLTableBackend(
        "",
        database="app",
        dsn="mysql://user:pass@localhost:3307/tinymongo?charset=utf8mb4",
    )
    backend.insert_many("users", [{"_id": "a", "name": "Ada"}])
    with pytest.raises(DuplicateKeyError):
        backend.insert_many(
            "users",
            [{"_id": "a", "name": "Grace"}],
            bypass_document_validation=True,
        )
    backend.replace_one("users", "a", {"_id": "a", "name": "Grace"})

    assert seen_kwargs[0]["host"] == "localhost"
    assert seen_kwargs[0]["port"] == 3307
    assert seen_kwargs[0]["database"] == "tinymongo"
    assert backend.find_one("users", {"_id": "a"})["name"] == "Grace"
    assert backend._quote("a`b") == "`a``b`"

    host_backend = MySQLTableBackend("", database="app", dsn="localhost")
    host_backend._connect().close()
    assert seen_kwargs[-1] == {"host": "localhost"}


def test_remote_sql_client_paths_and_env_dsn(monkeypatch, tmp_path):
    store = FakeRemoteStore()
    fake_psycopg = SimpleNamespace(connect=lambda dsn: FakeRemoteConnection(store))
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setenv("TINYMONGO_POSTGRES_DSN", "postgresql://db")

    client = tm.TinyMongoClient(str(tmp_path / "unused"), backend="postgres")
    client.app.users.insert_one({"_id": "one", "name": "Ada"})

    assert client.app._path == "app"
    assert client.server_info()["dsnConfigured"] is True
    assert client.list_database_names() == ["app"]
    assert client.app.collection_names() == ["users"]


@pytest.mark.parametrize("backend", ["sqlite", "duckdb", "parquet"])
def test_table_backends_support_nor_operator(tmp_path, backend):
    pytest.importorskip("duckdb") if backend in {"duckdb", "parquet"} else None
    client = tm.TinyMongoClient(str(tmp_path / backend), backend=backend)
    collection = client.app.items
    collection.insert_many(
        [
            {"_id": 1, "status": "draft", "score": 2},
            {"_id": 2, "status": "published", "score": 5},
            {"_id": 3, "status": "archived", "score": 9},
        ]
    )

    matches = collection.find({"$nor": [{"status": "draft"}, {"score": {"$gt": 8}}]})

    assert matches.count() == 1
    assert matches[0]["_id"] == 2
