import pytest
import tinymongo as tm


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


def test_parquet_backend(tmp_path):
    pytest.importorskip("pyarrow")
    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="parquet")
    db = client.testdb
    coll = db.testcollection
    coll.insert_one({"_id": 1, "value": "parquet"})

    assert coll.count() == 1
    assert (tmp_path / "db" / "testdb.parquet").exists()


def test_duckdb_backend(tmp_path):
    pytest.importorskip("duckdb")
    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="duckdb")
    db = client.testdb
    coll = db.testcollection
    coll.insert_one({"_id": 1, "value": "duckdb"})

    assert coll.count() == 1
    assert (tmp_path / "db" / "testdb.duckdb").exists()


def test_duckdb_backend_multiple_writes(tmp_path):
    pytest.importorskip("duckdb")
    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="duckdb")
    coll = client.testdb.testcollection

    coll.insert_one({"_id": 1, "value": "first"})
    coll.insert_one({"_id": 2, "value": "second"})

    assert coll.count() == 2


def test_internal_client_attrs_are_preserved(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path / "db"), backend="tinydb")

    assert client._backend == "tinydb"
    assert client._storage is not None

    db = client.testdb
    assert db._foldername == str(tmp_path / "db")
    assert not (tmp_path / "db" / "_storage.json").exists()
