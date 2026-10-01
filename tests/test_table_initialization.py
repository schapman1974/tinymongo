from uuid import uuid4

import pytest
from tinydb.database import StorageProxy, Table

from tinymongo import storage_backends as sb


@pytest.fixture(params=[sb.MemoryStorage, sb.AtomicJSONStorage], ids=["memory", "json"])
def storage(request, tmp_path):
    path = (
        str(tmp_path / "db.json")
        if request.param is sb.AtomicJSONStorage
        else str(uuid4())
    )
    return request.param(path)


def test_open_populated_table_does_not_copy_documents(storage, monkeypatch):
    storage.write(
        {"items": {"2": {"nested": [{"value": 1}]}, "19": {"body": "x" * 10000}}}
    )
    # Warm the JSON decoder so this measures table initialization only.
    storage.table_names()
    original = sb.copy.deepcopy
    copied = []

    def tracked(value, *args, **kwargs):
        copied.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(sb.copy, "deepcopy", tracked)
    db = sb.MemoryTinyDB(storage=lambda: storage)
    table = db.table("items", cache_size=3)
    assert copied == []
    assert table.insert({"fresh": True}) == 20
    result = table.get(doc_id=2)
    result["nested"][0]["value"] = 99
    assert table.get(doc_id=2)["nested"][0]["value"] == 1
    assert db.tables() == {"_default", "items"}


def test_empty_table_initialization_creates_storage_and_allocates_id(storage):
    db = sb.MemoryTinyDB(storage=lambda: storage)
    table = db.table("empty")
    assert "empty" in storage.table_names()
    assert table.insert({"ok": True}) == 1


@pytest.mark.parametrize(
    "rows",
    [
        {"-4": {"v": 1}, "003": {"v": 2}, "3": {"v": 3}},
        {"4": [["v", 1]]},
        {"bad": {"v": 1}},
        {"3": 42},
        [],
    ],
)
def test_legacy_initialization_matches_tinydb(storage, rows):
    storage.merge_writes = False
    storage.write({"items": rows})
    proxy = sb.MemoryStorageProxy(storage, "items")
    try:
        expected = Table(proxy, "items")._last_id
    except Exception as error:
        with pytest.raises(type(error)):
            sb.MemoryTinyDB(storage=lambda: storage).table("items")
    else:
        actual = sb.MemoryTinyDB(storage=lambda: storage).table("items")
        assert actual._last_id == expected
        assert actual.insert({"new": True}) == expected + 1


def test_custom_storage_and_proxy_keep_read_transformations(tmp_path):
    class CustomStorage(sb.MemoryStorage):
        def read_table(self, name):
            return {"42": {"source": "custom"}}

    db = sb.MemoryTinyDB(str(uuid4()), storage=CustomStorage)
    assert db.table("items")._last_id == 42

    class CustomProxy(StorageProxy):
        def read(self):
            return {67: {"source": "proxy"}}

    db = sb.MemoryTinyDB(
        str(uuid4()), storage=sb.MemoryStorage, storage_proxy_class=CustomProxy
    )
    assert db.table("items")._last_id == 67


def test_reopen_observes_external_ids(storage):
    db = sb.MemoryTinyDB(storage=lambda: storage)
    db.table("items").insert({"value": 1})
    storage.merge_writes = False
    storage.write_table("items", {"97": {"value": 2}})
    db._table_cache.clear()
    assert db.table("items").insert({"value": 3}) == 98


def test_custom_table_keeps_read_transformations():
    class CustomTable(sb.MemoryTable):
        def _read(self):
            return {83: {"custom": True}}

    db = sb.MemoryTinyDB(
        str(uuid4()), storage=sb.MemoryStorage, table_class=CustomTable
    )
    assert db.table("items")._last_id == 83


def test_warm_memory_batches_do_not_enumerate_resident_ids(monkeypatch):
    from tinymongo import TinyMongoClient

    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        collection = client.app.items
        collection.insert_many([{"_id": i} for i in range(1000)])
        original = sb._table_ids
        scanned = []

        def counted(rows):
            scanned.append(len(rows))
            return original(rows)

        monkeypatch.setattr(sb, "_table_ids", counted)
        for start in (1000, 1010, 1020):
            collection.insert_many([{"_id": i} for i in range(start, start + 10)])
        assert sum(scanned) == 0
        assert collection.count_documents({}) == 1030


@pytest.mark.parametrize("last_id", [0, 19])
def test_warm_initialization_reuses_last_id_without_building_index(
    last_id, monkeypatch
):
    storage = sb.MemoryStorage(uuid4().hex)
    storage.write({"items": {str(last_id): {"_id": "old"}} if last_id else {}})
    with storage.collection_lock:
        storage._insert_identity_index("items")
    monkeypatch.setattr(
        storage, "_insert_identity_index", lambda name: pytest.fail("rebuilt index")
    )
    table = sb.MemoryTable(sb.MemoryStorageProxy(storage, "items"), "items")
    assert table._last_id == last_id
    assert table.insert({"_id": "new"}) == last_id + 1
    assert storage.read_table_ids("items") == (
        [last_id, last_id + 1] if last_id else [1]
    )


@pytest.mark.parametrize("hook", ["init", "ids", "snapshot", "merge", "stale"])
def test_warm_initialization_keeps_hooks_and_stale_fallback(hook, monkeypatch):
    storage = sb.MemoryStorage(uuid4().hex)
    storage.write({"items": {"19": {"_id": "old"}}})
    with storage.collection_lock:
        index = storage._insert_identity_index("items")
    if hook == "init":

        def initialize(self, ids):
            self._last_id = max(ids) + 10

        monkeypatch.setattr(sb.MemoryTable, "_init_last_id", initialize)
        expected = 29
    elif hook == "ids":
        monkeypatch.setattr(storage, "read_table_ids", lambda name: [42])
        expected = 42
    elif hook == "snapshot":
        monkeypatch.setattr(
            storage,
            "_read_table",
            lambda name, snapshot: snapshot({"57": {"_id": "custom"}}),
        )
        expected = 57
    else:
        if hook == "merge":
            storage.merge_writes = False
        else:
            index[0] -= 1
        observed = []
        original = sb._table_ids
        monkeypatch.setattr(
            sb, "_table_ids", lambda rows: observed.append(len(rows)) or original(rows)
        )
        expected = 19
    table = sb.MemoryTable(sb.MemoryStorageProxy(storage, "items"), "items")
    assert table._last_id == expected
    if hook in ("merge", "stale"):
        assert observed == [1]


@pytest.mark.parametrize("mutation", ["replace", "delete", "drop", "neighbor"])
def test_warm_initialization_observes_second_storage_mutations(mutation):
    address = uuid4().hex
    storage = sb.MemoryStorage(address)
    other = sb.MemoryStorage(address)
    storage.write({"items": {"19": {"_id": "old"}}})
    with storage.collection_lock:
        storage._insert_identity_index("items")
    other.merge_writes = False
    if mutation == "replace":
        other.write_table("items", {"71": {"_id": "replacement"}})
        expected = 71
    elif mutation == "delete":
        other.write_table("items", {})
        expected = 0
    elif mutation == "drop":
        other.purge_table("items")
        expected = 0
    else:
        other.write_table("neighbor", {"99": {"_id": "neighbor"}})
        expected = 19
    table = sb.MemoryTable(sb.MemoryStorageProxy(storage, "items"), "items")
    assert table.insert({"_id": "new"}) == expected + 1
