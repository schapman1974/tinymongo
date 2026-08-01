"""Focused coverage for compatibility paths that are awkward to reach normally."""

import pytest

import tinymongo


class _StubDatabase:
    def __init__(self, collections=()):
        self.collections = list(collections)
        self.dropped = []
        self.closed = False

    def list_collection_names(self):
        return list(self.collections)

    def drop_collection(self, name):
        self.dropped.append(name)

    def close(self):
        self.closed = True


def test_directory_database_metadata_and_drop_cleanup(tmp_path):
    root = tmp_path / "parquet"
    database_path = root / "analytics.parquet"
    nested = database_path / "partitions"
    nested.mkdir(parents=True)
    (database_path / "root.bin").write_bytes(b"123")
    (nested / "part.bin").write_bytes(b"4567")

    client = tinymongo.TinyMongoClient(str(root), backend="parquet")
    database = _StubDatabase(["events"])
    client._databases["analytics"] = database

    assert client.list_databases().to_list() == [
        {"name": "analytics", "sizeOnDisk": 7, "empty": False}
    ]
    assert client.drop_database("analytics") is None
    assert database.dropped == ["events"]
    assert database.closed is True
    assert not database_path.exists()


def test_drop_database_unopened_and_cleanup_bypass_paths(tmp_path, monkeypatch):
    client = tinymongo.TinyMongoClient(str(tmp_path / "missing-path"))
    unopened = _StubDatabase()
    monkeypatch.setattr(client, "list_database_names", lambda: ["ghost"])
    monkeypatch.setattr(client, "_get_db", lambda name: unopened)

    assert client.drop_database("ghost") is None
    assert unopened.closed is True

    retained_root = tmp_path / "retained"
    retained_root.mkdir()
    retained_file = retained_root / "remote.json"
    retained_file.write_text("{}", encoding="utf-8")
    retained = tinymongo.TinyMongoClient(str(retained_root))
    retained._databases["remote"] = _StubDatabase()
    retained._storage_uri = "s3://example/prefix"

    assert retained.drop_database("remote") is None
    assert retained_file.exists()


def test_create_indexes_skip_and_duplicate_field_drop_branches():
    client = tinymongo.TinyMongoClient(backend="memory")
    collection = client.app.items

    with pytest.raises(TypeError, match="one iterable"):
        collection.create_indexes([], object())

    with pytest.warns(UserWarning, match="text indexing is ignored"):
        assert collection.create_indexes(
            [{"key": {"body": "text"}, "name": "body_text"}]
        ) == ["body_text"]
    assert collection.list_indexes() == [{"name": "_id_", "key": [("_id", 1)]}]

    collection.create_index("email", name="email_a")
    collection.create_index("email", name="email_b", unique=True)
    collection.drop_index("email_a")
    assert "email" in collection._indexes
    assert {item["name"] for item in collection.list_indexes()} == {
        "_id_",
        "email_b",
    }
    client.close()


def test_parse_condition_operator_construction_edges():
    collection = tinymongo.TinyMongoClient(backend="memory").app.items

    cases = [
        ({"$ne": 1}, "value", None),
        ({"$gt": 0, "$ne": 1}, "value", None),
        ({"$ne": 1}, "$not", "value"),
        ({"$not": 1}, "value", None),
        ({"$gt": 0, "$not": 1}, "value", None),
        ({"$not": 1}, "$not", "value"),
        ({"$not": {"$gt": 1}}, "value", None),
        ({"$regex": r"a\\\\b\\c"}, "value", None),
        ({"$gt": "a", "$regex": "b"}, "value", None),
        ({"$nin": [1, 2]}, "value", None),
        ({"$nin": 1}, "value", None),
        ({"$gt": 0, "$nin": [1, 2]}, "value", None),
        ({"$and": [{"a": 1}, {"b": 2}]}, None, None),
        ({"$or": [{"a": 1}, {"b": 2}]}, None, None),
        ({"$nor": [{"a": 1}, {"b": 2}]}, None, None),
        ({"$in": [1, 2]}, "value", None),
        ({"$all": [1, 2]}, "value", None),
        ({"$gt": 0, "$not": [1]}, "value", None),
        ({"$unknown": []}, "value", None),
        ({"value": [1, 2]}, None, None),
    ]

    for query, prev_key, last_prev_key in cases:
        list(collection.parse_condition(query, prev_key, last_prev_key))


def test_cursor_clone_and_closed_legacy_methods():
    manual = tinymongo.TinyMongoCursor([{"_id": 1}])
    clone = manual.clone()
    assert clone.to_list() == [{"_id": 1}]

    collection = tinymongo.TinyMongoClient(backend="memory").app.items
    collection.insert_one({"_id": 1})
    assert collection.find({}).clone().to_list() == [{"_id": 1}]

    cursor = tinymongo.TinyMongoCursor([{"_id": 1}])
    cursor.close()
    assert cursor.hasNext() is False
    with pytest.raises(StopIteration):
        cursor.next()
