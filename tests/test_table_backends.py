import pytest

import tinymongo as tm
from tinymongo.errors import DuplicateKeyError
from tinymongo.table_backends import (
    DuckDBTableBackend,
    ParquetDuckDBBackend,
    SQLCompiler,
    SQLiteTableBackend,
    TableBackend,
    matches_filter,
)


def test_matches_filter_operator_edges():
    doc = {"name": "Ada", "age": 36, "tags": ["math", "code"], "nested": {"x": 2}}

    assert matches_filter(doc, {})
    assert not matches_filter(doc, "bad")
    assert matches_filter(doc, {"$and": [{"age": {"$gt": 30}}, {"name": "Ada"}]})
    assert matches_filter(doc, {"$or": [{"name": "Grace"}, {"age": {"$lte": 36}}]})
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
