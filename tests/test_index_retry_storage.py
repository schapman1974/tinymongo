"""Index declaration retries avoid copying unchanged resident collections."""

from uuid import uuid4

import pytest

from tinymongo import TinyMongoClient
from tinymongo.errors import DuplicateKeyError, OperationFailure
from tinymongo.storage_backends import (
    AtomicJSONStorage,
    MemoryStorage,
    clear_memory_namespace,
)


@pytest.fixture(params=["json", "memory"])
def target(request, tmp_path):
    backend = request.param
    address = str(tmp_path) if backend == "json" else "memory://" + uuid4().hex
    with TinyMongoClient(address, backend=backend) as client:
        yield client, address, backend
    if backend == "memory":
        clear_memory_namespace(address)


def test_unchanged_retries_do_not_copy_resident_collection(target, monkeypatch):
    client, _, backend = target
    col = client.app.docs
    col.insert_many([{"_id": i, "key": i, "nested": [i]} for i in range(40)])
    col.create_index("key", unique=True)
    # Synchronize the handle with the revision produced by index persistence.
    assert col.find_one({"key": 1})["_id"] == 1
    storage = AtomicJSONStorage if backend == "json" else MemoryStorage
    original = storage.read_table

    def read(self, name):
        assert name != "docs", "index retry copied unchanged resident documents"
        return original(self, name)

    monkeypatch.setattr(storage, "read_table", read)
    for _ in range(3):
        assert col.create_index("key", unique=True) == "key_1"
        assert client.app.docs.create_index("key", unique=True) == "key_1"
    with pytest.raises(OperationFailure):
        col.create_index("key", unique=False)


def test_retry_refreshes_after_peer_changes_catalog_and_documents(target):
    client, address, backend = target
    col = client.app.docs
    col.insert_one({"_id": 1, "key": 1})
    col.create_index("key", unique=True)
    assert col.find_one({"key": 1})["_id"] == 1
    with TinyMongoClient(address, backend=backend) as peer:
        peer.app.docs.drop_index("key_1")
        peer.app.docs.insert_one({"_id": 2, "key": 1})
        with pytest.raises(DuplicateKeyError):
            col.create_index("key", unique=True)
        peer.app.docs.create_index("key")
        with pytest.raises(OperationFailure):
            col.create_index("key", unique=True)
        assert col.create_index("key") == "key_1"


def test_retry_without_revision_still_refreshes(target, monkeypatch):
    client, _, backend = target
    col = client.app.docs
    col.insert_one({"_id": 1, "key": 1})
    col.create_index("key")
    assert col.find_one({"key": 1})["_id"] == 1
    storage = AtomicJSONStorage if backend == "json" else MemoryStorage
    monkeypatch.setattr(storage, "revision", property(lambda self: None))
    calls = []
    original = col._refresh_table

    def refresh():
        calls.append(True)
        original()

    monkeypatch.setattr(col, "_refresh_table", refresh)
    assert col.create_index("key") == "key_1"
    assert calls == [True]


def test_new_index_after_synchronized_read_is_persisted(target):
    client, address, backend = target
    col = client.app.docs
    col.insert_many([{"_id": 1, "key": 1}, {"_id": 2, "key": 2}])
    assert col.find_one({"_id": 1})["key"] == 1
    assert col.create_index("key", unique=True) == "key_1"
    with TinyMongoClient(address, backend=backend) as peer:
        with pytest.raises(DuplicateKeyError):
            peer.app.docs.insert_one({"_id": 3, "key": 1})
