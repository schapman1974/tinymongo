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
