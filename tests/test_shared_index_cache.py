"""Short-lived JSON/memory collection handles reuse revision-bound indexes."""

from uuid import uuid4

import pytest

from tinymongo import TinyMongoClient
from tinymongo.storage_backends import AtomicJSONStorage, clear_memory_namespace


@pytest.fixture
def shared_memory_address():
    address = "memory://shared-index-" + uuid4().hex
    yield address
    clear_memory_namespace(address)


@pytest.mark.parametrize("backend", ["json", "memory"])
def test_fresh_handles_share_index_without_reading_resident_table(
    tmp_path, monkeypatch, backend
):
    with TinyMongoClient(str(tmp_path), backend=backend) as client:
        items = client.app.items
        items.insert_many([{"_id": i, "key": i, "nested": [i]} for i in range(40)])
        items.create_index("key")
        assert items.find_one({"key": 3})["_id"] == 3

        def no_scan():
            pytest.fail("fresh handle scanned an unchanged indexed table")

        monkeypatch.setattr(items.table, "all", no_scan)
        found = client.app.items.find_one({"key": 3})
        found["nested"].append("caller change")
        assert client.app.items.find_one({"key": 3})["nested"] == [3]
        assert client.app.items.find_one({"key": 4})["_id"] == 4
        assert client.app.items.find_one({"key": 99}) is None


@pytest.mark.parametrize("backend", ["json", "memory"])
def test_shared_indexes_refresh_after_writes_from_either_client(
    tmp_path, backend, shared_memory_address
):
    address = shared_memory_address if backend == "memory" else str(tmp_path)
    with (
        TinyMongoClient(address, backend=backend) as first,
        TinyMongoClient(address, backend=backend) as second,
    ):
        items = first.app.items
        items.insert_one({"_id": 1, "key": "before"})
        items.create_index("key")
        assert items.find_one({"key": "before"})["_id"] == 1
        assert first.app.items.find_one({"key": "before"})["_id"] == 1
        second.app.items.update_one({"_id": 1}, {"$set": {"key": "after"}})
        assert items.find_one({"key": "before"}) is None
        assert first.app.items.find_one({"key": "after"})["_id"] == 1
        first.app.items.insert_one({"_id": 2, "key": "after"})
        assert len(list(items.find({"key": "after"}))) == 2
        second.app.items.delete_one({"_id": 1})
        assert [d["_id"] for d in first.app.items.find({"key": "after"})] == [2]
        second.app.items.drop_index("key")
        assert items.find_one({"key": "after"})["_id"] == 2
        second.app.items.drop()
        second.app.items.insert_one({"_id": 3, "key": "new"})
        second.app.items.create_index("key")
        assert items.find_one({"key": "after"}) is None
        assert first.app.items.find_one({"key": "new"})["_id"] == 3


def test_storage_without_revision_keeps_handle_local_cache(tmp_path, monkeypatch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        items = client.app.items
        items.insert_one({"_id": 1, "key": "one"})
        items.create_index("key")
        monkeypatch.setattr(AtomicJSONStorage, "revision", property(lambda self: None))
        assert items.find_one({"key": "one"})["_id"] == 1
        assert not hasattr(items.table, "_tinymongo_index_cache")
        cached = items._index_cache["key"]
        assert items.find_one({"key": "one"})["_id"] == 1
        assert items._index_cache["key"] is cached
