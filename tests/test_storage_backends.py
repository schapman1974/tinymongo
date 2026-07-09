import pytest
import tinymongo as tm
from tinymongo.storage_backends import DuckDBStorage, SQLiteStorage


def test_tinydb_backend_default(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="tinydb")
    db = client.testdb
    coll = db.testcollection
    coll.insert_one({"_id": 1, "value": "tinydb"})

    assert coll.count() == 1
    assert (tmp_path / "db" / "testdb.json").exists()


def test_sqlite_backend(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="sqlite")
    db = client.testdb
    coll = db.testcollection
    coll.insert_one({"_id": 1, "value": "sqlite"})

    assert coll.count() == 1
    assert (tmp_path / "db" / "testdb.sqlite").exists()


def test_sqlite_backend_uses_collection_tables(tmp_path):
    import sqlite3

    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="sqlite")
    client.testdb.users.insert_one({"_id": 1, "name": "Ada", "age": 36})
    client.testdb.events.insert_one({"_id": "e1", "kind": "login"})

    conn = sqlite3.connect(str(tmp_path / "db" / "testdb.sqlite"))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        rows = conn.execute("SELECT _id, data FROM users").fetchall()
    finally:
        conn.close()

    assert {"users", "events"}.issubset(tables)
    assert "tinydb" not in tables
    assert rows[0][0] == "1"
    assert '"name":"Ada"' in rows[0][1]


def test_parquet_backend(tmp_path):
    pytest.importorskip("duckdb")
    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="parquet")
    db = client.testdb
    coll = db.testcollection
    coll.insert_one({"_id": 1, "value": "parquet"})

    assert coll.count() == 1
    assert (tmp_path / "db" / "testdb.parquet").exists()
    assert (tmp_path / "db" / "testdb.parquet" / "testcollection.parquet").exists()


def test_duckdb_backend(tmp_path):
    pytest.importorskip("duckdb")
    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="duckdb")
    db = client.testdb
    coll = db.testcollection
    coll.insert_one({"_id": 1, "value": "duckdb"})

    assert coll.count() == 1
    assert (tmp_path / "db" / "testdb.duckdb").exists()


def test_duckdb_backend_uses_collection_tables(tmp_path):
    duckdb = pytest.importorskip("duckdb")

    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="duckdb")
    client.testdb.users.insert_one({"_id": 1, "name": "Ada", "age": 36})
    client.testdb.events.insert_one({"_id": "e1", "kind": "login"})

    conn = duckdb.connect(str(tmp_path / "db" / "testdb.duckdb"))
    try:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        rows = conn.execute('SELECT _id, data FROM "users"').fetchall()
    finally:
        conn.close()

    assert {"users", "events"}.issubset(tables)
    assert "tinydb" not in tables
    assert rows[0][0] == "1"
    assert '"name":"Ada"' in rows[0][1]


def test_sqlite_backend_migrates_legacy_blob_file(tmp_path):
    db_file = tmp_path / "db" / "legacy.sqlite"
    SQLiteStorage(str(db_file)).write(
        {"users": {"1": {"_id": 1, "name": "Ada"}, "2": {"_id": 2, "name": "Grace"}}}
    )

    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="sqlite")

    assert client.legacy.users.count_documents({}) == 2
    assert client.legacy.users.find_one({"name": "Ada"})["_id"] == 1
    assert "tinydb" not in client.legacy.collection_names()


def test_duckdb_backend_migrates_legacy_blob_file(tmp_path):
    pytest.importorskip("duckdb")
    db_file = tmp_path / "db" / "legacy.duckdb"
    DuckDBStorage(str(db_file)).write(
        {"users": {"1": {"_id": 1, "name": "Ada"}, "2": {"_id": 2, "name": "Grace"}}}
    )

    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="duckdb")

    assert client.legacy.users.count_documents({}) == 2
    assert client.legacy.users.find_one({"name": "Grace"})["_id"] == 2
    assert "tinydb" not in client.legacy.collection_names()


def test_duckdb_backend_multiple_writes(tmp_path):
    pytest.importorskip("duckdb")
    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="duckdb")
    coll = client.testdb.testcollection

    coll.insert_one({"_id": 1, "value": "first"})
    coll.insert_one({"_id": 2, "value": "second"})

    assert coll.count() == 2


@pytest.mark.parametrize("backend", ["sqlite", "duckdb", "parquet"])
def test_table_backends_support_common_queries(tmp_path, backend):
    if backend in ("duckdb", "parquet"):
        pytest.importorskip("duckdb")

    client = tm.TinyMongoClient(str(tmp_path / backend), backend=backend)
    coll = client.app.users
    coll.insert_many(
        [
            {"_id": 1, "name": "Ada", "age": 36, "active": True, "tags": ["math"]},
            {"_id": 2, "name": "Grace", "age": 40, "active": False, "tags": ["code"]},
            {"_id": 3, "name": "Katherine", "age": 34, "tags": ["math", "space"]},
        ]
    )

    assert [doc["_id"] for doc in coll.find({"age": {"$gte": 36}}).sort("_id", 1)] == [1, 2]
    assert coll.find({"name": {"$in": ["Ada", "Grace"]}}).count() == 2
    assert coll.find({"active": {"$exists": False}}).count() == 1
    assert coll.find({"tags": {"$all": ["math", "space"]}}).count() == 1

    coll.update_one({"name": "Ada"}, {"$inc": {"age": 1}})
    assert coll.find_one({"_id": 1})["age"] == 37

    assert coll.delete_many({"age": {"$lt": 37}}).deleted_count == 1
    assert coll.count_documents({}) == 2


def test_internal_client_attrs_are_preserved(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="tinydb")

    assert client._backend == "tinydb"
    assert client._storage is not None

    db = client.testdb
    assert db._foldername == str(tmp_path / "db")
    assert not (tmp_path / "db" / "_storage.json").exists()
