import pytest
import sys
from types import SimpleNamespace

import tinymongo as tm
from tinymongo.errors import DuplicateKeyError
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
    _join_uri,
    matches_filter,
)


class FakeRemoteStore:
    def __init__(self):
        self.tables = {}
        self.metadata = set()


class FakeRemoteCursor:
    def __init__(self, store):
        self.store = store
        self.rows = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        upper = normalized.upper()
        if upper.startswith("CREATE TABLE"):
            table = self._table_from_create(normalized)
            if "TINYMONGO_COLLECTIONS" not in table:
                self.store.tables.setdefault(table, {})
        elif upper.startswith("INSERT") and "TINYMONGO_COLLECTIONS" in upper:
            self.store.metadata.add(tuple(params))
        elif upper.startswith("SELECT DISTINCT"):
            self.rows = sorted({(database,) for database, _ in self.store.metadata})
        elif upper.startswith("SELECT COLLECTION_NAME"):
            database = params[0]
            self.rows = sorted(
                (collection,)
                for metadata_database, collection in self.store.metadata
                if metadata_database == database
            )
        elif upper.startswith("DROP TABLE"):
            self.store.tables.pop(self._table_after(normalized, "TABLE IF EXISTS"), None)
        elif upper.startswith("DELETE FROM") and "TINYMONGO_COLLECTIONS" in upper:
            self.store.metadata.discard(tuple(params))
        elif upper.startswith("DELETE FROM"):
            self.store.tables.setdefault(self._table_after(normalized, "FROM"), {}).pop(
                params[0], None
            )
        elif upper.startswith("SELECT DATA") and "WHERE _ID" in upper:
            table = self._table_after(normalized, "FROM")
            data = self.store.tables.get(table, {}).get(params[0])
            self.rows = [(data,)] if data is not None else []
        elif upper.startswith("SELECT DATA"):
            table = self._table_after(normalized, "FROM")
            self.rows = [(data,) for data in self.store.tables.get(table, {}).values()]
        elif upper.startswith("UPDATE"):
            table = self._table_after(normalized, "UPDATE")
            self.store.tables.setdefault(table, {})[params[1]] = params[0]
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
        for doc_id, data in rows:
            if doc_id in target and "CONFLICT" not in sql.upper() and "REPLACE" not in sql.upper():
                raise RuntimeError("duplicate key")
            target[doc_id] = data

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self):
        pass

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


def test_sql_compiler_branches():
    sqlite = SQLCompiler("sqlite")
    duckdb = SQLCompiler("duckdb")

    assert sqlite.compile({}) == ("", [])
    assert "$.name" in sqlite.compile({"name": "Ada"})[1]
    assert duckdb.compile({"active": True})[1][-1] == "true"
    assert "_id IN" in duckdb.compile({"_id": {"$in": [1, 2]}})[0]
    assert "_id !=" in duckdb.compile({"_id": {"$ne": 1}})[0]
    assert " OR " in duckdb.compile({"$or": [{"name": "Ada"}, {"name": "Grace"}]})[0]
    assert "NOT" in duckdb.compile({"$nor": [{"name": "Ada"}, {"name": "Grace"}]})[0]
    assert " AND " in duckdb.compile({"$and": [{"age": {"$gt": 1}}, {"age": {"$lt": 9}}]})[0]
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
    assert backend.create_index("anything", "field") == "field"
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
    assert _join_uri("s3://bucket/prefix/", "/db.parquet") == "s3://bucket/prefix/db.parquet"

    monkeypatch.setenv("TINYMONGO_S3_REGION", "us-west-004")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("TINYMONGO_S3_ENDPOINT", "s3.us-west-004.backblazeb2.com")
    monkeypatch.setenv("TINYMONGO_S3_USE_SSL", "false")
    monkeypatch.setenv("TINYMONGO_DUCKDB_SETUP_SQL", "SET custom_setting='x'; LOAD httpfs;")
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

    backend.insert_many("users", [{"_id": 1, "name": "Grace"}], bypass_document_validation=True)
    assert backend.find_one("users", {"_id": 1})["name"] == "Grace"
    assert backend.create_index("users", "name") == "name"
    assert backend.delete_many("users", {"_id": "missing"}) == []
    assert backend.drop_collection("users") is True
    assert backend.drop_collection("users") is False


def test_duckdb_backend_threads_duplicate_bypass_drop(tmp_path):
    pytest.importorskip("duckdb")
    backend = DuckDBTableBackend(str(tmp_path / "db.duckdb"), threads=2)
    backend.insert_many("users", [{"_id": 1, "name": "Ada"}])

    with pytest.raises(DuplicateKeyError):
        backend.insert_many("users", [{"_id": 1, "name": "Grace"}])

    backend.insert_many("users", [{"_id": 1, "name": "Grace"}], bypass_document_validation=True)
    assert backend.find_one("users", {"_id": 1})["name"] == "Grace"
    assert backend.delete_many("users", {"_id": "missing"}) == []
    assert backend.drop_collection("users") is True
    assert backend.drop_collection("users") is False


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
    backend.insert_many("users", [{"_id": 1, "name": "Grace"}], bypass_document_validation=True)
    assert backend.find_one("users", {"_id": 1})["name"] == "Grace"
    assert backend.drop_collection("users") is True
    assert backend.drop_collection("users") is False


def test_parquet_object_store_paths_and_fake_listing(monkeypatch):
    pytest.importorskip("duckdb")
    backend = ParquetDuckDBBackend(
        "s3://bucket/prefix/app.parquet",
        duckdb_config={"s3_region": "auto"},
    )

    assert backend._is_object_store is True
    assert backend._collection_path("users") == "s3://bucket/prefix/app.parquet/users.parquet"

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
    monkeypatch.setattr(backend, "_write_rows", lambda collection, rows: written.append((collection, rows)))

    assert backend.drop_collection("users") is True
    assert written == [("users", [])]


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

    assert collection.any_attribute is collection
    assert collection.create_index("email") == "email"
    assert collection.drop_index("email") is None
    assert collection.list_indexes() == [{"name": "_id_", "key": [("_id", 1)]}]
    assert collection.drop() is True
    assert collection.drop() is False

    with pytest.raises(ValueError):
        collection.insert_one("bad")
    with pytest.raises(ValueError):
        collection.insert_many("bad")

    one = collection.insert_one({"email": "one@example.com"})
    many = collection.insert_many([{"email": "two@example.com"}])

    assert one.inserted_id
    assert many.inserted_ids[0]
    assert collection.update_many({}, {"$set": {"active": True}}).modified_count == 2
    assert collection.replace_one({"email": "missing"}, {"email": "none"}).matched_count == 0
    assert collection.replace_one({"email": "one@example.com"}, {"email": "one@example.com"}).matched_count == 1
    assert collection.find_one_and_update({"email": "two@example.com"}, {"$set": {"active": False}})["active"] is True
    assert collection.find_one_and_update({"email": "missing"}, {"$set": {"active": False}}) is None
    assert collection.delete_one({"email": "one@example.com"}).deleted_count == 1


def test_remote_sql_backend_requires_dsn():
    with pytest.raises(ValueError):
        RemoteSQLTableBackend("", database="app")


def test_optional_driver_error_messages(monkeypatch):
    def missing_import(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("tinymongo.table_backends.importlib.import_module", missing_import)

    with pytest.raises(ImportError, match='pip install "tinymongo\\[postgres\\]"') as postgres:
        PostgresTableBackend("", database="app", dsn="postgresql://db")
    assert "optional Python driver 'psycopg'" in str(postgres.value)

    with pytest.raises(ImportError, match='pip install "tinymongo\\[mysql\\]"') as mysql:
        MySQLTableBackend("", database="app", dsn="mysql://db")
    assert "optional Python driver 'pymysql'" in str(mysql.value)

    with pytest.raises(ImportError, match="pip install duckdb") as duckdb:
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
    assert backend._commit(BadConn()) is None
    assert backend._close_cursor(BadCursor()) is None
    assert backend._data_placeholder() == "%s"

    monkeypatch.setattr(backend, "create_collection", lambda collection: None)
    assert backend.delete_ids("users", []) is None
    assert backend.create_index("users", "email") == "email"


def test_postgres_table_backend_with_fake_driver(monkeypatch):
    store = FakeRemoteStore()
    fake_psycopg = SimpleNamespace(connect=lambda dsn: FakeRemoteConnection(store))
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    backend = PostgresTableBackend("", database="app", dsn="postgresql://db")

    backend.insert_many("users", [{"_id": 1, "name": "Ada", "age": 36}])
    with pytest.raises(DuplicateKeyError):
        backend.insert_many("users", [{"_id": 1, "name": "Grace"}])
    backend.insert_many(
        "users",
        [{"_id": 1, "name": "Grace", "age": 40}],
        bypass_document_validation=True,
    )

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
    backend.insert_many(
        "users",
        [{"_id": "a", "name": "Grace"}],
        bypass_document_validation=True,
    )

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
    collection.insert_many([
        {"_id": 1, "status": "draft", "score": 2},
        {"_id": 2, "status": "published", "score": 5},
        {"_id": 3, "status": "archived", "score": 9},
    ])

    matches = collection.find({"$nor": [{"status": "draft"}, {"score": {"$gt": 8}}]})

    assert matches.count() == 1
    assert matches[0]["_id"] == 2
