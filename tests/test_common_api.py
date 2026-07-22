import os

import pytest

import tinymongo
from tinymongo.errors import InvalidOperation


def test_client_lists_and_drops_file_database(tmp_path):
    client = tinymongo.TinyMongoClient(str(tmp_path), backend="tinydb")
    database = client.get_database("app")
    assert database.name == "app"
    assert database.get_collection("items").name == "items"
    assert database.items.database is database
    assert database.items.full_name == "app.items"
    database.items.insert_one({"_id": 1})

    metadata = client.list_databases().to_list()

    assert metadata[0]["name"] == "app"
    assert metadata[0]["sizeOnDisk"] > 0
    assert metadata[0]["empty"] is False
    assert client.database_names() == ["app"]

    assert client.drop_database(database) is None
    assert client.list_database_names() == []
    assert not os.path.exists(tmp_path / "app.json")
    assert client.drop_database("missing") is None
    with pytest.raises(TypeError):
        client.drop_database(object())
    client.close()


def test_client_lists_and_drops_named_memory_database():
    client = tinymongo.TinyMongoClient("memory://common-api", backend="memory")
    client.app.items.insert_one({"_id": 1})

    assert client.list_databases().to_list() == [
        {"name": "app", "sizeOnDisk": 0, "empty": False}
    ]

    client.drop_database("app")
    assert client.list_database_names() == []
    client.close()


def test_client_drops_local_parquet_database_directory(tmp_path):
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    client = tinymongo.TinyMongoClient(str(tmp_path), backend="parquet")
    client.analytics.events.insert_one({"_id": 1})
    database_path = tmp_path / "analytics.parquet"
    assert database_path.is_dir()

    client.drop_database("analytics")

    assert not database_path.exists()
    assert client.list_database_names() == []
    client.close()


@pytest.mark.parametrize("backend", ["memory", "tinydb", "sqlite"])
def test_find_one_and_delete_honors_sort_and_projection(tmp_path, backend):
    folder = (
        "memory://find-one-delete-{0}".format(backend)
        if backend == "memory"
        else str(tmp_path / backend)
    )
    client = tinymongo.TinyMongoClient(folder, backend=backend)
    collection = client.app.jobs
    collection.insert_many(
        [
            {"_id": 1, "status": "pending", "priority": 1, "secret": "a"},
            {"_id": 2, "status": "pending", "priority": 2, "secret": "b"},
        ]
    )

    removed = collection.find_one_and_delete(
        {"status": "pending"},
        sort=[("priority", -1)],
        projection={"secret": 0},
    )

    assert removed == {"_id": 2, "status": "pending", "priority": 2}
    assert collection.find_one({"_id": 2}) is None
    assert collection.find_one_and_delete({"status": "missing"}) is None
    client.close()


def test_distinct_index_information_and_cursor_lifecycle():
    assert issubclass(tinymongo.TinyMongoUnsupportedWarning, UserWarning)
    client = tinymongo.TinyMongoClient(backend="memory")
    collection = client.app.items
    collection.insert_many(
        [
            {"_id": 1, "kind": "a", "profile": {"team": "one"}, "tags": ["x", "y"]},
            {"_id": 2, "kind": "b", "profile": {"team": "one"}, "tags": ["y"]},
            {"_id": 3, "kind": "a", "profile": {"team": "two"}},
        ]
    )
    collection.create_index("kind", name="kind_lookup")

    assert collection.distinct("profile.team", {"kind": "a"}) == ["one", "two"]
    assert collection.distinct("tags") == ["x", "y"]
    assert collection.index_information() == {
        "_id_": {"key": [("_id", 1)]},
        "kind_lookup": {"key": [("kind", 1)]},
    }

    cursor = collection.find({}).sort("_id")
    collection.insert_one(
        {"_id": 4, "kind": "c", "profile": {"team": "three"}, "tags": ["z"]}
    )
    clone = cursor.clone().skip(1).limit(-1)
    assert clone.to_list() == [
        {
            "_id": 2,
            "kind": "b",
            "profile": {"team": "one"},
            "tags": ["y"],
        }
    ]
    assert [item["_id"] for item in cursor.clone().to_list()] == [1, 2, 3, 4]
    assert cursor.to_list(1)[0]["_id"] == 1
    assert cursor.rewind().to_list(1)[0]["_id"] == 1
    assert cursor.alive is True
    cursor.close()
    assert cursor.alive is False
    assert cursor.to_list() == []
    assert list(cursor) == []
    with pytest.raises(InvalidOperation):
        cursor.rewind()
    with pytest.raises(TypeError):
        clone.skip(True)
    with pytest.raises(ValueError):
        clone.skip(-1)
    with pytest.raises(TypeError):
        clone.limit(1.5)
    with pytest.raises(TypeError):
        clone.to_list(True)
    with pytest.raises(ValueError):
        clone.to_list(-1)
    client.close()


def test_iterating_cursor_preserves_consumed_position():
    cursor = tinymongo.TinyMongoCursor([{"_id": 1}, {"_id": 2}, {"_id": 3}])

    assert next(cursor) == {"_id": 1}
    assert list(cursor) == [{"_id": 2}, {"_id": 3}]
    assert list(cursor) == []
    with pytest.raises(StopIteration):
        cursor.next()


@pytest.mark.parametrize("backend", ["memory", "tinydb", "sqlite"])
def test_find_and_modify_honors_before_after_upsert_sort_and_projection(
    tmp_path, backend
):
    location = (
        "memory://find-and-modify-semantics"
        if backend == "memory"
        else str(tmp_path / backend)
    )
    client = tinymongo.TinyMongoClient(location, backend=backend)
    collection = client.app.items
    collection.insert_many(
        [
            {"_id": 1, "status": "pending", "rank": 1, "secret": "one"},
            {"_id": 2, "status": "pending", "rank": 2, "secret": "two"},
        ]
    )

    after = collection.find_one_and_update(
        {"status": "pending"},
        {"$set": {"status": "complete"}},
        sort=[("rank", -1)],
        projection={"status": 1},
        return_document=True,
    )
    assert after == {"_id": 2, "status": "complete"}

    before = collection.find_one_and_replace(
        {"_id": 1}, {"status": "replaced", "rank": 3}
    )
    assert before["status"] == "pending"

    assert (
        collection.find_one_and_update(
            {"_id": 3}, {"$set": {"status": "inserted"}}, upsert=True
        )
        is None
    )
    assert collection.find_one({"_id": 3})["status"] == "inserted"
    assert collection.find_one_and_replace(
        {"_id": 4},
        {"_id": 4, "status": "inserted replacement"},
        upsert=True,
        return_document=True,
    ) == {"_id": 4, "status": "inserted replacement"}

    with pytest.raises(ValueError, match="return_document"):
        collection.find_one_and_update({}, {"$set": {"seen": True}}, return_document=1)
    with pytest.raises(ValueError, match="return_document"):
        collection.find_one_and_replace({}, {}, return_document="after")
    client.close()


def test_file_crud_read_modify_write_paths_take_an_outer_collection_lock(tmp_path):
    client = tinymongo.TinyMongoClient(str(tmp_path / "locked"), backend="tinydb")
    collection = client.app.items
    collection.insert_many([{"_id": 1, "value": 1}, {"_id": 2, "value": 2}])
    acquisitions = []
    acquire = collection._acquire_collection_lock

    def tracked_acquire():
        acquisitions.append(True)
        return acquire()

    collection._acquire_collection_lock = tracked_acquire

    collection.find_one_and_update({"_id": 1}, {"$set": {"value": 3}})
    assert len(acquisitions) == 3
    acquisitions.clear()

    collection.find_one_and_replace({"_id": 1}, {"value": 4})
    assert len(acquisitions) == 3
    acquisitions.clear()

    collection.delete_one({"_id": 1})
    assert len(acquisitions) == 2
    acquisitions.clear()

    collection.delete_many({})
    assert len(acquisitions) == 2
    client.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_update_many_supports_embedded_document_ids(tmp_path, backend):
    location = (
        "memory://embedded-document-id"
        if backend == "memory"
        else str(tmp_path / backend)
    )
    client = tinymongo.TinyMongoClient(location, backend=backend)
    collection = client.app.items
    document_id = {"tenant": 1, "item": 2}
    collection.insert_one({"_id": document_id, "value": 1})

    result = collection.update_many({}, {"$set": {"value": 2}})

    assert result.matched_count == 1
    assert result.modified_count == 1
    assert collection.find_one({"_id": document_id})["value"] == 2
    client.close()
