from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from tinydb.database import StorageProxy, Table

from tinymongo import TinyMongoClient, storage_backends as sb
from tinymongo.errors import BulkWriteError, InvalidDocument


@pytest.fixture(params=["memory", "json"])
def clients(request, tmp_path):
    uri = "memory://" + uuid4().hex if request.param == "memory" else str(tmp_path)
    first = TinyMongoClient(uri, backend=request.param)
    peer = TinyMongoClient(uri, backend=request.param)
    yield first, peer
    first.close()
    peer.close()


def test_batch_reads_target_once_and_detaches_values(clients, monkeypatch):
    first, peer = clients
    collection = first.app.items
    collection.insert_one({"_id": "old", "nested": [1]})
    reads = []
    original = sb.MemoryStorageProxy.read

    def tracked(self):
        if self._table_name == "items":
            reads.append(self._table_name)
        return original(self)

    monkeypatch.setattr(sb.MemoryStorageProxy, "read", tracked)
    document = {
        "_id": "new",
        "nested": [2],
        "when": datetime(2020, 1, 1, tzinfo=timezone.utc),
    }
    collection.insert_many([document, {"_id": "next"}])
    assert reads == ["items"]
    document["nested"].append(3)
    result = peer.app.items.find_one({"_id": "new"})
    assert result["nested"] == [2]
    assert result["when"] == datetime(2020, 1, 1)
    result["nested"].append(4)
    assert collection.find_one({"_id": "new"})["nested"] == [2]
    assert collection.find_one({"_id": "old"})["nested"] == [1]


@pytest.mark.parametrize("ordered", [True, False])
def test_batch_partial_failures_and_stale_clients(clients, ordered):
    first, peer = clients
    collection = first.app.items
    collection.create_index("key", unique=True)
    peer.app.items.insert_many([{"_id": 0, "key": "taken"}])
    docs = [
        {"_id": 1, "key": "one"},
        {"_id": 2, "key": "taken"},
        {"_id": 3, "key": "three"},
    ]
    with pytest.raises(BulkWriteError) as caught:
        collection.insert_many(docs, ordered=ordered)
    assert caught.value.details["nInserted"] == (1 if ordered else 2)
    assert caught.value.details["writeErrors"][0]["index"] == 1
    assert {d["_id"] for d in peer.app.items.find({})} == (
        {0, 1} if ordered else {0, 1, 3}
    )
    with pytest.raises(BulkWriteError) as caught:
        peer.app.items.insert_many([{"_id": 1, "key": "different"}])
    assert caught.value.details["nInserted"] == 0


def test_failed_preflight_or_write_does_not_publish_snapshot(clients, monkeypatch):
    first, peer = clients
    collection = first.app.items
    collection.insert_many([{"_id": 0}])
    with pytest.raises(InvalidDocument):
        collection.insert_many([{"_id": 1}, {"_id": 2, "bad": object()}])
    assert peer.app.items.count_documents({}) == 1
    original = sb.MemoryStorageProxy.write

    def fail(self, data):
        if self._table_name == "items":
            raise OSError("injected write failure")
        return original(self, data)

    with monkeypatch.context() as patch:
        patch.setattr(sb.MemoryStorageProxy, "write", fail)
        with pytest.raises(OSError, match="injected"):
            collection.insert_many([{"_id": 3}])
    assert peer.app.items.count_documents({}) == 1
    collection.insert_many([{"_id": 4}])
    assert {d["_id"] for d in peer.app.items.find({})} == {0, 4}


def test_concurrent_batches_preserve_unique_ownership(clients):
    first, peer = clients
    first.app.items.create_index("key", unique=True)

    def write(args):
        client, index = args
        try:
            client.app.items.insert_many(
                [{"_id": index, "key": "same"}, {"_id": index + 100, "key": index}],
                ordered=False,
            )
        except BulkWriteError:
            pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, [(first, 1), (peer, 2)]))
    docs = list(first.app.items.find({}))
    assert len(docs) == 3
    assert sum(d["key"] == "same" for d in docs) == 1
    assert {d["_id"] for d in docs} >= {101, 102}


def test_snapshot_preserves_internal_ids_and_clears_query_cache(clients):
    first, _ = clients
    collection = first.app.items
    collection.insert_many([{"_id": "a"}, {"_id": "b"}])
    collection.delete_one({"_id": "a"})
    collection.find_one({"_id": "c"})
    collection.insert_many([{"_id": "c"}, {"_id": "d"}])
    assert collection.find_one({"_id": "c"}) == {"_id": "c"}
    assert sorted(collection.table._read()) == [2, 3, 4]


@pytest.mark.parametrize("custom", ["storage", "proxy", "table"])
def test_custom_paths_keep_original_batch_hooks(tmp_path, monkeypatch, custom):
    class CustomStorage(sb.MemoryStorage):
        pass

    class CustomProxy(StorageProxy):
        pass

    class CustomTable(sb.MemoryTable):
        pass

    kwargs = {"storage": CustomStorage if custom == "storage" else sb.MemoryStorage}
    if custom == "proxy":
        kwargs["storage_proxy_class"] = CustomProxy
    if custom == "table":
        kwargs["table_class"] = CustomTable
    db = sb.MemoryTinyDB(uuid4().hex, **kwargs)
    table = db.table("items")
    calls = []
    original = Table.insert_multiple

    def tracked(self, documents):
        calls.append(True)
        return original(self, documents)

    monkeypatch.setattr(Table, "insert_multiple", tracked)
    client = TinyMongoClient(backend="memory")
    collection = client.app.items
    monkeypatch.setattr(collection, "_refresh_table", lambda: None)
    collection.table = table
    collection.insert_many([{"_id": "custom"}])
    assert calls == [True]
    assert table.all()[0]["_id"] == "custom"
    client.close()
