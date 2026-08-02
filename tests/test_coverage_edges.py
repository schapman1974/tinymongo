import json
import runpy
import os
import sqlite3
import sys
import threading
import warnings

import pytest

import tinymongo as tm
from tinymongo.errors import InvalidOperation, StorageCorruptionError
from tinymongo import cli
from tinymongo import tinymongo as core
from tinymongo import parquet_storage as ps
from tinymongo import storage_backends as sb
from tinymongo.errors import (
    BulkWriteError,
    DuplicateKeyError,
    OperationFailure,
    TinyMongoNotSupportedError,
    WriteError,
)
from tinymongo.results import (
    DeleteResult,
    InsertManyResult,
    InsertOneResult,
    UpdateResult,
)


def test_result_properties_and_error_classes():
    one = InsertOneResult(eid=10, inserted_id="abc")
    many = InsertManyResult(eids=[1, 2], inserted_ids=["a", "b"])
    updated = UpdateResult(raw_result="not-a-list")
    deleted = DeleteResult(raw_result=3)

    assert one.eid == 10
    assert one.inserted_id == "abc"
    assert many.eids == [1, 2]
    assert many.inserted_ids == ["a", "b"]
    assert updated.raw_result == "not-a-list"
    assert updated.matched_count == 0
    assert updated.modified_count == 0
    assert updated.upserted_id is None
    assert deleted.raw_result == 3
    assert deleted.deleted_count == 3
    duplicate = DuplicateKeyError("duplicate")
    assert str(duplicate)
    assert duplicate.code == 11000
    detailed_duplicate = DuplicateKeyError(
        "duplicate",
        details={"keyPattern": {"email": 1}},
    )
    assert detailed_duplicate.code == 11000
    assert detailed_duplicate.details == {"keyPattern": {"email": 1}}
    failure = OperationFailure("failed", code=42, details={"ok": False})
    assert str(failure)
    assert failure.code == 42
    assert failure.details == {"ok": False}

    list_result = UpdateResult(raw_result=[1])
    assert list_result.matched_count == 1
    assert list_result.modified_count == 1


def test_cli_json_helpers_and_errors(tmp_path, capsys):
    payload_file = tmp_path / "payload.json"

    cli._dump_json({"b": 1}, str(payload_file))
    assert json.loads(payload_file.read_text(encoding="utf-8")) == {"b": 1}
    assert cli._load_json(str(payload_file)) == {"b": 1}

    cli._dump_json({"a": 1}, "-")
    assert json.loads(capsys.readouterr().out) == {"a": 1}

    with pytest.raises(SystemExit):
        cli.main(["import", str(tmp_path), "db", "coll", str(payload_file)])

    bad_docs = tmp_path / "bad-docs.json"
    bad_docs.write_text('[{"_id": 1}, 2]', encoding="utf-8")
    with pytest.raises(SystemExit, match="JSON objects"):
        cli.main(["import", str(tmp_path), "db", "coll", str(bad_docs)])

    assert cli._db_names(str(tmp_path / "missing"), "tinydb") == []
    (tmp_path / ".hidden.json").write_text("{}", encoding="utf-8")
    (tmp_path / "wrong.sqlite").write_text("", encoding="utf-8")
    assert ".hidden" not in cli._db_names(str(tmp_path), "tinydb")

    empty_docs = tmp_path / "empty-docs.json"
    empty_docs.write_text("[]", encoding="utf-8")
    one_doc = tmp_path / "one-doc.json"
    one_doc.write_text('[{"_id": 1}]', encoding="utf-8")
    assert (
        cli.main(["import", str(tmp_path / "imports"), "db", "items", str(empty_docs)])
        == 0
    )
    assert (
        cli.main(
            [
                "import",
                str(tmp_path / "imports"),
                "db",
                "items",
                str(one_doc),
                "--mode",
                "append",
            ]
        )
        == 0
    )


def test_cli_main_module_entrypoint(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["tinymongo", "--help"])
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="'tinymongo.cli' found in sys.modules"
        )
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("tinymongo.cli", run_name="__main__")
    assert exc.value.code == 0


def test_storage_backend_helpers_and_sqlite_edge_paths(tmp_path, monkeypatch):
    db_file = tmp_path / "db.sqlite"
    storage = sb.SQLiteStorage(str(db_file))

    assert storage.read() == {}
    storage.write({"_default": {"1": {"_id": 1}}})
    assert storage.read() == {"_default": {"1": {"_id": 1}}}

    sqlite3.connect(str(db_file)).execute("DELETE FROM tinydb").connection.commit()
    assert storage.read() == {}

    db_file.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(StorageCorruptionError):
        storage.read()

    assert sb.get_storage_class(None) is sb.get_storage_class("tinydb")
    assert sb.get_storage_class(sb.SQLiteStorage) is sb.SQLiteStorage
    assert sb.storage_extension("json") == ".json"
    assert sb.storage_extension("parquetv2") == ".parquet"
    assert sb.storage_extension("duckdb") == ".duckdb"
    assert sb.storage_extension("postgres") == ""
    assert sb.storage_extension("mariadb") == ""
    assert sb.is_object_storage_uri("s3://bucket/path") is True
    assert sb.is_object_storage_uri("/tmp/path") is False
    assert (
        sb.join_storage_uri("s3://bucket/prefix/", "/db.parquet")
        == "s3://bucket/prefix/db.parquet"
    )
    assert sb.join_storage_uri(str(tmp_path), "db.parquet") == os.path.join(
        str(tmp_path), "db.parquet"
    )
    assert sb.is_table_backend("sqlite") is True
    assert sb.is_table_backend("postgres") is True
    assert sb.is_remote_sql_backend("postgresql") is True
    assert sb.is_remote_sql_backend("mysql") is True
    assert sb.get_storage_class("postgres") is sb.AtomicJSONStorage
    assert sb.is_table_backend("tinydb") is False
    assert sb.get_table_backend("sqlite") is not None
    assert sb.get_table_backend("postgres") is not None
    assert sb.get_table_backend("mariadb") is not None
    with pytest.raises(ValueError):
        sb.get_storage_class("missing")
    with pytest.raises(ValueError):
        sb.storage_extension("missing")
    with pytest.raises(ValueError):
        sb.get_table_backend("tinydb")

    monkeypatch.setattr(sb, "duckdb", None)
    with pytest.raises(ImportError, match="tinymongo.*duckdb"):
        sb.DuckDBStorage(str(tmp_path / "db.duckdb"))

    with monkeypatch.context() as ctx:
        ctx.setattr(
            sb.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
        )
        sb._fsync_dir(str(tmp_path))

    cleanup_file = tmp_path / "cleanup.sqlite"
    cleanup_storage = sb.SQLiteStorage(str(cleanup_file))
    monkeypatch.setattr(
        sb.os, "replace", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        sb.os, "remove", lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(RuntimeError):
        cleanup_storage.write({"x": {}})


def test_atomic_json_corruption_is_not_silently_ignored(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("not json", encoding="utf-8")
    storage = sb.AtomicJSONStorage(str(path))

    with pytest.raises(StorageCorruptionError):
        storage.read()
    with pytest.raises(StorageCorruptionError):
        storage.write({"items": {}})


def test_duckdb_storage_with_fake_driver(tmp_path, monkeypatch):
    class FakeCursor:
        def __init__(self, path):
            self.path = path

        def execute(self, sql, args=None):
            if "INSERT" in sql:
                with open(self.path, "w", encoding="utf-8") as handle:
                    handle.write(args[0])
            return self

        def fetchone(self):
            if not os.path.exists(self.path):
                return None
            with open(self.path, "r", encoding="utf-8") as handle:
                value = handle.read()
            return None if value is None else (value,)

        def close(self):
            pass

    class FakeDuckDB:
        def connect(self, path):
            return FakeCursor(path)

    monkeypatch.setattr(sb, "duckdb", FakeDuckDB())
    storage = sb.DuckDBStorage(str(tmp_path / "db.duckdb"))
    assert storage.read() == {}
    storage.write({"_default": {"1": {"_id": 1}}})
    assert storage.read() == {"_default": {"1": {"_id": 1}}}

    empty_file = tmp_path / "empty.duckdb"
    empty_file.touch()
    with pytest.raises(StorageCorruptionError):
        sb.DuckDBStorage(str(empty_file)).read()

    with open(empty_file, "w", encoding="utf-8") as handle:
        handle.write("not json")
    with pytest.raises(StorageCorruptionError):
        sb.DuckDBStorage(str(empty_file)).read()

    class NoneCursor(FakeCursor):
        def fetchone(self):
            return None

    class NoneDuckDB:
        def connect(self, path):
            return NoneCursor(path)

    monkeypatch.setattr(sb, "duckdb", NoneDuckDB())
    assert sb.DuckDBStorage(str(empty_file)).read() == {}

    with monkeypatch.context() as ctx:
        ctx.setattr(sb.os, "replace", lambda *args, **kwargs: None)
        sb.DuckDBStorage(str(tmp_path / "cleanup.duckdb")).write({"x": {}})


def test_parquet_storage_read_write_and_merge(tmp_path):
    path = tmp_path / "db.parquet"
    storage = ps.ParquetStorage(str(path))

    assert storage.read() == {}
    storage.write({"users": {"1": {"_id": "a", "name": "Ada"}}})
    storage.write({"users": {"1": {"_id": "a", "name": "Ada Lovelace"}}})
    storage.write({"users": {"2": {"_id": "b", "name": "Grace"}}})

    assert storage.read()["users"] == {
        "1": {"_id": "a", "name": "Ada Lovelace"},
        "2": {"_id": "b", "name": "Grace"},
    }

    path.write_text("not parquet", encoding="utf-8")
    with pytest.raises(StorageCorruptionError):
        storage.read()


def test_parquet_storage_missing_pyarrow_message(tmp_path, monkeypatch):
    path = tmp_path / "db.parquet"
    storage = ps.ParquetStorage(str(path))

    monkeypatch.setattr(ps, "pq", None)
    monkeypatch.setattr(ps, "pa", None)
    assert storage.read() == {}

    with pytest.raises(ImportError, match="tinymongo.*parquet"):
        storage.write({"users": {"1": {"_id": "a", "name": "Ada"}}})

    path.write_text("not parquet", encoding="utf-8")
    with pytest.raises(ImportError, match="tinymongo.*parquet"):
        storage.read()


def test_parquet_storage_fake_arrow_edge_paths(tmp_path, monkeypatch):
    path = tmp_path / "db.parquet"
    storage = ps.ParquetStorage(str(path))
    path.write_text("exists", encoding="utf-8")

    class FakeColumn:
        def __init__(self, values):
            self.values = values

        def to_pylist(self):
            return self.values

    class FakeTable:
        def __init__(self, names, values=None):
            self.column_names = names
            self.values = values or []

        def column(self, name):
            return FakeColumn(self.values)

    class FakePQ:
        def __init__(self, table):
            self.table = table

        def read_table(self, path):
            return self.table

        def write_table(self, table, tmp, version=None):
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write("parquet")

    monkeypatch.setattr(ps, "pq", FakePQ(FakeTable(["other"])))
    assert storage.read() == {}
    monkeypatch.setattr(ps, "pq", FakePQ(FakeTable(["data"], [])))
    assert storage.read() == {}
    monkeypatch.setattr(ps, "pq", FakePQ(FakeTable(["data"], ["not json"])))
    with pytest.raises(StorageCorruptionError):
        storage.read()

    monkeypatch.setattr(
        ps, "pq", FakePQ(FakeTable(["data"], ['{"old":{"1":{"_id":"a"}}}']))
    )

    class FakePA:
        def string(self):
            return "string"

        def array(self, values, type=None):
            return values

        def table(self, data):
            return data

    monkeypatch.setattr(ps, "pa", FakePA())
    storage.write({"new": {"1": {"_id": "b"}}})

    with monkeypatch.context() as ctx:
        ctx.setattr(ps.os, "replace", lambda *args, **kwargs: None)
        storage.write({"new": {"2": {"_id": "c"}}})

    monkeypatch.setattr(ps, "pq", FakePQ(FakeTable(["data"], ["not json"])))
    with pytest.raises(StorageCorruptionError):
        storage.write({"new": {"3": {"_id": "d"}}})


def test_client_database_contexts_and_idempotent_close(tmp_path):
    with tm.TinyMongoClient(str(tmp_path / "client")) as client:
        database = client.app
        with database as entered:
            assert entered is database
            entered.items.insert_one({"_id": 1})

    client.close()
    assert client.__enter__() is client
    client.__exit__(None, None, None)


def test_engine_context_close_and_missing_update(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "sqlite"), backend="sqlite")
    database = client.app
    assert database.__enter__() is database
    collection = database.items

    result = collection.update_one({"_id": "missing"}, {"$set": {"x": 1}})
    assert result.matched_count == 0
    database.__exit__(None, None, None)


def test_local_replace_noop_and_lock_acquire_failure(tmp_path, monkeypatch):
    client = tm.TinyMongoClient(str(tmp_path / "db"))
    collection = client.app.items
    collection.insert_one({"_id": 1, "x": 1})
    result = collection.replace_one({"_id": 1}, {"x": 1})
    assert result.matched_count == 1
    assert result.modified_count == 0

    class BrokenLock:
        def acquire(self):
            raise RuntimeError("lock failed")

    class BrokenPortalocker:
        @staticmethod
        def Lock(*args, **kwargs):
            return BrokenLock()

    monkeypatch.setattr(ps, "portalocker", BrokenPortalocker)
    with pytest.raises(RuntimeError, match="lock failed"):
        collection._acquire_collection_lock()


def test_remaining_query_update_and_cursor_branches(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "db"))
    collection = client.app.items
    collection.insert_many(
        [
            {"_id": 1, "value": 1, "active": True},
            {"_id": 2, "value": 5, "active": False},
        ]
    )

    assert [doc["_id"] for doc in collection.find({"value": {"$not": {"$gt": 3}}})] == [
        1
    ]
    result = collection.update_many({}, {"$set": {"active": True}})
    assert result.matched_count == 2
    assert result.modified_count == 1

    sqlite = tm.TinyMongoClient(str(tmp_path / "sqlite"), backend="sqlite").app.items
    sqlite.insert_one({"_id": 1, "value": 1})
    result = sqlite.replace_one({"_id": 1}, {"value": 1})
    assert result.matched_count == 1
    assert result.modified_count == 0

    cursor = tm.TinyMongoCursor([{"x": 1}, {"x": 2}])
    cursor.paginate(0, 10)
    assert cursor.count() == 2

    marker = object()
    assert cursor._order(marker) == (0, None)

    rows = tm.TinyMongoCursor(
        [
            {"nested": [{"other": 1}]},
            {"nested": [{"other": 1}, {"also": 2}]},
        ]
    )
    rows.sort("nested.value", 1)
    rows.sort("nested.value", -1)


def test_upsert_helpers_database_compatibility_and_find_and_modify(tmp_path):
    assert core._apply_update_document({"_id": 1}, {"value": 2}) == {
        "_id": 1,
        "value": 2,
    }
    assert core._apply_update_document({"_id": 1}, {"_id": 2}) == {"_id": 2}
    generated = core._document_for_upsert(
        {"$or": [{"name": "ignored"}], "score": {"$gt": 1}},
        {"$set": {"active": True}},
    )
    assert generated["active"] is True
    assert "score" not in generated

    client = tm.TinyMongoClient(str(tmp_path / "db"))
    database = client.app
    assert database.get_collection("items").tablename == "items"
    collection = database.items
    collection.insert_one({"_id": 1})
    assert database.drop_collection(collection) is True

    after = collection.find_one_and_update(
        {"_id": 2},
        {"$set": {"value": 1}},
        upsert=True,
        return_document=True,
    )
    assert after == {"_id": 2, "value": 1}
    replaced = collection.find_one_and_replace(
        {"_id": 3}, {"_id": 3, "value": 2}, upsert=True
    )
    assert replaced is None
    assert collection.find_one({"_id": 3}) == {"_id": 3, "value": 2}
    assert collection.find_one_and_replace({"_id": "missing"}, {"value": 3}) is None
    assert collection.find_one_and_replace(
        {"_id": 3}, {"value": 4}, return_document=True
    ) == {"_id": 3, "value": 4}
    assert collection.find_one_and_replace({"_id": 3}, {"value": 5}) == {
        "_id": 3,
        "value": 4,
    }

    sqlite = tm.TinyMongoClient(str(tmp_path / "sqlite"), backend="sqlite").app.items
    result = sqlite.update_many(
        {"email": "ada@example.com"}, {"$set": {"active": True}}, upsert=True
    )
    assert result.upserted_id is not None
    assert (
        sqlite.find_one_and_update(
            {"email": "grace@example.com"},
            {"$set": {"active": True}},
            upsert=True,
        )
        is None
    )
    assert sqlite.find_one({"email": "grace@example.com"})["active"] is True


def test_parquet_lock_helpers_and_fsync_failures(monkeypatch):
    lock = threading.RLock()
    assert ps._acquire_rlock(lock) is True
    assert ps._acquire_rlock(lock) is False
    lock.release()
    lock.release()

    monkeypatch.setattr(
        ps.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
    )
    ps._fsync_dir("/does/not/matter")

    class BadLock:
        def _is_owned(self):
            raise RuntimeError()

        def acquire(self, blocking=True):
            return True

    assert ps._acquire_rlock(BadLock()) is True

    class BlockingLock:
        def __init__(self):
            self.calls = 0

        def _is_owned(self):
            return False

        def acquire(self, blocking=True):
            self.calls += 1
            return False if self.calls == 1 else True

    assert ps._acquire_rlock(BlockingLock()) is True


def test_cursor_and_gridfs_edges():
    cursor = tm.TinyMongoCursor(
        [
            {"name": "Ada", "scores": [{"value": 2}]},
            {"name": "Grace", "scores": [{"value": 1}]},
            {"name": "Empty", "scores": []},
            {"name": "Missing"},
        ]
    )

    assert cursor.sort("scores.value", -1)[0]["name"] == "Ada"
    assert tm.TinyMongoCursor([{"name": "Ada"}])["name"] == "Ada"
    assert cursor.limit(2).count(with_limit_and_skip=True) == 2
    assert cursor.has_next() is True
    assert cursor.next()["name"] == "Ada"
    assert cursor.hasNext() is True
    assert list(iter(cursor))

    with pytest.raises(TypeError):
        tm.TinyMongoCursor([]).sort([("name", 2)])
    with pytest.raises(TypeError):
        tm.TinyMongoCursor([]).sort(["name"])
    with pytest.raises(ValueError):
        tm.TinyMongoCursor([]).sort([("name", 1, 2)])
    with pytest.raises(TypeError):
        tm.TinyMongoCursor([]).sort([(1, 1)])
    with pytest.raises(ValueError):
        tm.TinyMongoCursor([]).sort([("name", 1)], 1)
    with pytest.raises(TypeError):
        tm.TinyMongoCursor([]).sort("name", 0)
    with pytest.raises(ValueError):
        tm.TinyMongoCursor([]).sort(42)

    gridfs = tm.TinyGridFS()
    assert gridfs.grid_fs("db").database == "db"
    assert gridfs.GridFS("other").database == "other"
    assert len(tm.generate_id()) > 0


def test_collection_error_and_compatibility_edges(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "db"))
    client.close()
    with pytest.raises(InvalidOperation):
        client.db
    client = tm.TinyMongoClient(str(tmp_path / "db"))
    collection = client.db.collection
    assert repr(collection) == "collection"
    assert collection.anything.tablename == "collection.anything"

    with pytest.raises(ValueError):
        collection.insert_one(["not", "a", "dict"])
    with pytest.raises(TypeError):
        collection.insert_many({"not": "a list"})
    with pytest.raises(AttributeError):
        getattr(client, "_private")

    collection.insert_one({"_id": 0, "name": "zero"})
    collection.insert_one({"_id": "", "name": "empty"})
    generated = collection.insert_one({"name": "generated"})
    assert generated.inserted_id
    with pytest.raises(DuplicateKeyError):
        collection.insert_one({"_id": 0}, bypass_document_validation=True)
    with pytest.raises(DuplicateKeyError):
        collection.insert_one({"_id": 0})
    with pytest.raises(BulkWriteError):
        collection.insert_many([{"_id": "dupe"}, {"_id": "dupe"}])
    assert collection.insert_many([{"name": "generated-many"}]).inserted_ids[0]

    assert collection.insert({"_id": "single"}).inserted_id == "single"
    assert collection.insert([{"_id": "many"}]).inserted_ids == ["many"]
    assert (
        collection.update({"_id": "many"}, {"$set": {"updated": True}}).modified_count
        == 1
    )
    assert (
        collection.update({"_id": "many"}, [{"$set": {"updated": False}}])[
            0
        ].modified_count
        == 1
    )
    assert collection.remove({"_id": "many"}, multi=False).deleted_count == 1
    assert collection.remove({"name": "missing"}, multi=True).deleted_count == 0


def test_direct_helper_and_lock_edges(tmp_path, monkeypatch):
    doc = {"a": {"b": 1}, "items": []}
    core._unset_nested(doc, "a.missing.value")
    assert doc == {"a": {"b": 1}, "items": []}
    assert (
        core._apply_update_document({"_id": 1}, {"$pull": {"items": "x"}})["items"]
        == []
    )
    assert core._apply_update_document({"_id": 1}, {"$addToSet": {"items": "x"}})[
        "items"
    ] == ["x"]
    with pytest.raises(ValueError):
        core._apply_update_document({"_id": 1, "items": "x"}, {"$pull": {"items": "x"}})
    with pytest.raises(WriteError) as caught:
        core._apply_update_document(
            {"_id": 1, "items": "x"}, {"$addToSet": {"items": "x"}}
        )
    assert caught.value.code == 2

    with monkeypatch.context() as ctx:
        ctx.setattr(
            core.os,
            "makedirs",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("nope")),
        )
        tm.TinyMongoClient(str(tmp_path / "cannot-create"))

    client = tm.TinyMongoClient(str(tmp_path / "db"))
    c = client.db.collection
    c.insert_many([{"_id": 1, "lookup": {"nested": ["x"]}}, {"_id": 2}])
    assert c._get_index("missing") is None
    unbuilt = core.TinyMongoCollection("unbuilt", client.db)
    unbuilt.create_index("field")
    assert unbuilt._get_index("field") == {}
    c.create_index("absent")
    assert c._get_index("absent") == {}
    assert c._get_index("absent") == {}
    c.create_index("lookup")
    assert c._get_index("lookup") == {}

    monkeypatch.setattr(
        ps, "_acquire_rlock", lambda lock: (_ for _ in ()).throw(RuntimeError())
    )
    with pytest.raises(RuntimeError):
        c._acquire_collection_lock()
    c._release_collection_lock(object(), object())


def test_query_operator_branches_without_mongo(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "db"))
    c = client.db.collection
    c.insert_many(
        [
            {
                "_id": 1,
                "count": 1,
                "name": "alpha",
                "tags": ["a"],
                "meta": {"active": True},
            },
            {
                "_id": 2,
                "count": 5,
                "name": "beta",
                "tags": ["b"],
                "meta": {"active": False},
            },
            {"_id": 3, "count": 10, "name": "gamma", "tags": ["a", "c"]},
        ]
    )

    assert c.find({"count": {"$gt": 1, "$lt": 10}}).count() == 1
    assert c.find({"count": {"$gte": 5, "$lte": 10}}).count() == 2
    assert c.find({"name": {"$ne": "alpha"}}).count() == 2
    assert c.find({"name": {"$regex": "^a"}}).count() == 1
    assert c.find({"name": {"$not": {"$eq": "alpha"}}}).count() == 2
    assert c.find({"count": {"$not": {"$gt": 5}}}).count() == 2
    assert c.find({"tags": {"$in": ["c", "missing"]}}).count() == 1
    assert (
        c.find({"$and": [{"tags": {"$in": ["a"]}}, {"count": {"$lt": 5}}]}).count() == 1
    )
    assert c.find({"$or": [{"name": "alpha"}, {"name": "gamma"}]}).count() == 2
    assert c.find({"missing": {"$exists": False}}).count() == 3
    assert c.find({"tags": [["a"]]}).count() == 0
    with pytest.raises(TinyMongoNotSupportedError, match=r"\$unknown"):
        c.find({"tags": {"$unknown": ["a"]}})


def test_unknown_query_operator_fails_before_collection_storage_is_created(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="sqlite")
    collection = client.app.items

    with pytest.raises(TinyMongoNotSupportedError, match=r"\$exsits"):
        collection.find({"value": {"$exsits": True}})

    assert client.app.list_collection_names() == []
    client.close()


@pytest.mark.parametrize("error", [AttributeError("bad query"), TypeError("bad query")])
def test_tinydb_native_search_errors_remain_empty_results(tmp_path, monkeypatch, error):
    client = tm.TinyMongoClient(str(tmp_path / "db"))
    collection = client.app.items
    collection.insert_one({"_id": 1, "name": "Ada"})
    monkeypatch.setattr(
        collection.table,
        "search",
        lambda _condition: (_ for _ in ()).throw(error),
    )

    assert list(collection.find({"name": {"$gt": "A"}})) == []
    client.close()


def test_collection_write_and_no_match_edges(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "db"))
    db = client.db
    c = db.collection

    assert c.count_documents() == 0
    assert c.update_one({"_id": "missing"}, {"$set": {"x": 1}}).matched_count == 0
    assert c.replace_one({"_id": "missing"}, {"x": 1}).matched_count == 0
    assert c.find_one_and_update({"_id": "missing"}, {"$set": {"x": 1}}) is None

    c.insert_one({"_id": "a", "value": 1})
    assert c.find_one_and_update({"_id": "a"}, {"$set": {"value": 2}})["value"] == 1
    assert c.find_one({"_id": "a"})["value"] == 2

    with pytest.raises(ValueError, match="update only works with \\$ operators"):
        c.update_one({"_id": "a"}, {"value": 3})
    c.replace_one({"_id": "a"}, {"value": 3})
    assert c.find_one({"_id": "a"}) == {"_id": "a", "value": 3}

    assert c.drop() is True
    assert c.drop() is False
    c.build_table()
    c.insert_one({"_id": "drop-table-branch"})
    db.tinydb.drop_table = lambda name: None
    db._refresh_table = lambda: None
    assert c.drop() is True
    c.insert_many([{"_id": 1}, {"_id": 2}], bypass_document_validation=True)
    assert c.delete_many({}).deleted_count == 3
    assert "_default" not in db.collection_names()


def test_update_operator_error_edges(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "db"))
    c = client.db.collection
    c.insert_one({"_id": 1, "count": "one"})

    with pytest.raises(TinyMongoNotSupportedError, match="Unsupported update operator"):
        c.update_one({"_id": 1}, {"$unknown": {"x": 1}})
    with pytest.raises(ValueError, match="update only works with \\$ operators"):
        c.update_one({"_id": 1}, {})
    with pytest.raises(ValueError, match="update only works with \\$ operators"):
        c.update_one({"_id": 1}, {"$set": {"x": 1}, "plain": 2})
    with pytest.raises(ValueError, match="requires a dict"):
        c.update_one({"_id": 1}, {"$set": "not-a-dict"})
    with pytest.raises(WriteError, match="\\$inc requires numeric values") as one:
        c.update_one({"_id": 1}, {"$inc": {"count": 1}})
    assert one.value.code == 14
    with pytest.raises(WriteError, match="\\$inc requires numeric values") as many:
        c.update_many({"_id": 1}, {"$inc": {"count": 1}})
    assert many.value.code == 14
    with pytest.raises(TypeError):
        c.replace_one({"_id": 1}, object())

    fresh = core.TinyMongoCollection("fresh", client.db)
    assert fresh.update_one({"_id": "missing"}, {"$set": {"x": 1}}).matched_count == 0
    assert (
        core.TinyMongoCollection("fresh_many", client.db)
        .update_many({"_id": "missing"}, {"$set": {"x": 1}})
        .matched_count
        == 0
    )
    assert (
        core.TinyMongoCollection("fresh_replace", client.db)
        .replace_one({"_id": "missing"}, {"x": 1})
        .matched_count
        == 0
    )
    assert (
        core.TinyMongoCollection("fresh_find_update", client.db).find_one_and_update(
            {"_id": "missing"}, {"$set": {"x": 1}}
        )
        is None
    )
    assert (
        core.TinyMongoCollection("fresh_find_one", client.db).find_one(
            {"_id": "missing"}
        )
        is None
    )


def test_database_refresh_ignores_close_errors(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "db"))
    db = client.db

    class BadTinyDB:
        def close(self):
            raise RuntimeError()

    db.tinydb = BadTinyDB()
    db._refresh_table()
    assert "_default" not in db.collection_names()


def test_parquet_storage_uri_client_paths_and_listing(tmp_path, monkeypatch):
    class FakeEngine:
        instances = []

        def __init__(
            self,
            path,
            threads=None,
            duckdb_config=None,
            database=None,
            dsn=None,
        ):
            self.path = path
            self.threads = threads
            self.duckdb_config = duckdb_config
            FakeEngine.instances.append(self)

        def list_collections(self):
            return ["app", "audit"]

    monkeypatch.setattr(core, "get_table_backend", lambda backend: FakeEngine)

    client = tm.TinyMongoClient(
        str(tmp_path / "db"),
        backend="parquet",
        storage_uri="s3://bucket/root",
        threads=4,
        duckdb_config={"s3_region": "auto"},
    )
    db = client.app

    assert db._path == "s3://bucket/root/app.parquet"
    assert client.server_info()["storageUri"] == "s3://bucket/root"
    assert client.list_database_names() == ["app", "audit"]
    assert FakeEngine.instances[-1].threads == 4
    assert FakeEngine.instances[-1].duckdb_config == {"s3_region": "auto"}


def test_parquet_storage_uri_env_var(tmp_path, monkeypatch):
    class FakeEngine:
        def __init__(
            self,
            path,
            threads=None,
            duckdb_config=None,
            database=None,
            dsn=None,
        ):
            self.path = path

        def list_collections(self):
            return []

    monkeypatch.setenv("TINYMONGO_STORAGE_URI", "gs://bucket/root")
    monkeypatch.setattr(core, "get_table_backend", lambda backend: FakeEngine)

    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="parquet")

    assert client.app._path == "gs://bucket/root/app.parquet"


def test_nonlocal_clients_skip_placeholder_folders_and_validate_eagerly(
    tmp_path,
    monkeypatch,
):
    remote_path = tmp_path / "unused-remote"
    tm.TinyMongoClient(
        str(remote_path),
        backend="postgres",
        dsn="postgresql://configured",
    )
    assert not remote_path.exists()

    object_path = tmp_path / "unused-object"
    tm.TinyMongoClient(
        str(object_path),
        backend="parquet",
        storage_uri="s3://bucket/root",
    )
    assert not object_path.exists()

    with pytest.raises(ValueError) as error:
        tm.TinyMongoClient(str(tmp_path / "invalid"), backend="not-a-backend")
    assert "json" in str(error.value)
    assert "postgresql" in str(error.value)
    assert "mysql" in str(error.value)

    monkeypatch.setenv("TINYMONGO_STORAGE_URI", "s3://unrelated/root")
    local_path = tmp_path / "local-json"
    local = tm.TinyMongoClient(str(local_path))
    local.app.items.insert_one({"_id": 1})
    assert local.server_info()["storageUri"] is None
    assert local._dsn_from_env("tinydb") is None
    assert local.drop_database("app") is None
    assert local.list_database_names() == []
    assert not (local_path / "app.json").exists()

    local_parquet_path = tmp_path / "local-parquet"
    local_parquet = tm.TinyMongoClient(
        str(local_parquet_path),
        backend="parquet",
        storage_uri="",
    )
    assert local_parquet._storage_uri is None
    assert local_parquet_path.is_dir()

    monkeypatch.setenv("TINYMONGO_POSTGRES_DSN", "postgresql://environment")
    remote_without_dsn = tm.TinyMongoClient(
        str(tmp_path / "remote-without-dsn"),
        backend="postgres",
        dsn="",
    )
    assert remote_without_dsn.server_info()["dsnConfigured"] is False


def test_closed_client_rejects_metadata_and_listing_operations():
    client = tm.TinyMongoClient(backend="memory")
    client.close()

    operations = (
        client.server_info,
        client.capabilities,
        lambda: client.supports("projections"),
        client.list_database_names,
        client.list_databases,
        client.database_names,
        lambda: client.drop_database("missing"),
    )
    for operation in operations:
        with pytest.raises(InvalidOperation, match="closed TinyMongoClient"):
            operation()


def test_mysql_dsn_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("TINYMONGO_MYSQL_DSN", "mysql://user:pass@localhost/db")

    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="mariadb")

    assert client._dsn == "mysql://user:pass@localhost/db"


def test_cursor_sort_order_branches():
    cursor = tm.TinyMongoCursor(
        [
            {"_id": 1, "value": {"b": 2, "a": 1}},
            {"_id": 2, "value": ["z", "a"]},
            {"_id": 3, "value": []},
            {"_id": 4, "value": object()},
        ]
    )

    with pytest.warns(tm.TinyMongoUnsupportedWarning):
        assert cursor.sort("value", 1).count() == 4
    assert cursor.sort("value").count() == 4
    assert cursor._order(True) == (7, True)
    assert cursor._order([1, "a"], None)[0] == 4
    assert cursor.sort([("value", 1), ("_id", -1)]).count() == 4
    assert (
        tm.TinyMongoCursor([{"x": 1}, {"x": 2}, {"x": 3}], skip=1, limit=2).count() == 2
    )
    assert tm.TinyMongoCursor([]).hasNext() is False

    ascending_list = tm.TinyMongoCursor(
        [
            {"items": [{"score": 1}]},
            {"items": [{"score": 2}]},
        ]
    )
    assert ascending_list.sort("items.score", 1)[0]["items"][0]["score"] == 1


def test_backend_duckdb_selection_with_fake_driver(monkeypatch):
    class FakeDuckDB:
        def connect(self, path):
            raise RuntimeError(path)

    monkeypatch.setattr(sb, "duckdb", FakeDuckDB())
    assert sb.get_storage_class("duckdb") is sb.DuckDBStorage
