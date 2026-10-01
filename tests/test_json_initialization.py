"""Warm JSON table initialization reuses only current, native insert indexes."""

import pytest

from tinymongo import TinyMongoClient, storage_backends as sb
from tinymongo.errors import StorageCorruptionError


@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_warm_json_initialization_does_not_scan_resident_ids(
    tmp_path, monkeypatch, unique, batch
):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        if unique:
            col.create_index("n", unique=True)
        col.insert_many([{"_id": i, "n": i} for i in range(100)])
        col.insert_one({"_id": 100, "n": 100})
        original = sb._table_ids
        scanned = []

        def counted(rows):
            scanned.append(len(rows))
            return original(rows)

        monkeypatch.setattr(sb, "_table_ids", counted)
        for i in range(101, 104):
            row = {"_id": i, "n": i}
            if batch:
                col.insert_many([row])
            else:
                col.insert_one(row)
        # Index metadata can remain cold; the resident target must not be scanned.
        assert max(scanned, default=0) <= 1
        assert col.count_documents({}) == 104
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        assert client.app.items.count_documents({}) == 104


def warm(client):
    col = client.app.items
    col.insert_many([{"_id": i} for i in range(3)])
    col.insert_one({"_id": 3})
    storage = col.parent.tinydb._storage
    assert "items" in storage._insert_indexes
    return storage


def initialize(storage):
    return sb.MemoryTable(sb.MemoryStorageProxy(storage, "items"), "items")


@pytest.mark.parametrize(
    "hook", ["init", "ids", "snapshot", "merge", "read", "stale", "absent"]
)
def test_custom_and_stale_initialization_falls_back(tmp_path, monkeypatch, hook):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        storage = warm(client)
        original = sb._table_ids
        scanned = []
        monkeypatch.setattr(
            sb, "_table_ids", lambda rows: scanned.append(len(rows)) or original(rows)
        )
        expected = 4
        if hook == "init":

            def custom(self, ids):
                self._last_id = max(ids) + 10

            monkeypatch.setattr(sb.MemoryTable, "_init_last_id", custom)
            expected = 14
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
        elif hook == "merge":
            storage.merge_writes = False
        elif hook == "read":
            original_read = storage.read
            monkeypatch.setattr(storage, "read", lambda: original_read())
        elif hook == "stale":
            storage._insert_indexes["items"][0] = ("stale",)
        else:
            storage._insert_indexes.clear()
        assert initialize(storage)._last_id == expected
        if hook not in ("ids", "snapshot"):
            assert scanned == [4]


@pytest.mark.parametrize(
    "mutation", ["replace", "delete", "drop", "neighbor", "unlink", "corrupt"]
)
def test_initialization_observes_external_file_changes(tmp_path, mutation):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        storage = warm(client)
        other = sb.AtomicJSONStorage(storage.path)
        other.merge_writes = False
        expected = 4
        if mutation == "replace":
            other.write_table("items", {"71": {"_id": "replacement"}})
            expected = 71
        elif mutation == "delete":
            other.write_table("items", {})
            expected = 0
        elif mutation == "drop":
            other.purge_table("items")
            expected = 0
        elif mutation == "neighbor":
            other.write_table("neighbor", {"99": {"_id": "neighbor"}})
        elif mutation == "unlink":
            (tmp_path / "app.json").unlink()
            expected = 0
        else:
            (tmp_path / "app.json").write_text("{broken")
            with pytest.raises(StorageCorruptionError):
                initialize(storage)
            return
        table = initialize(storage)
        assert table._last_id == expected
        assert table.insert({"_id": "new"}) == expected + 1
        assert storage.read_table("items")[str(expected + 1)]["_id"] == "new"


@pytest.mark.parametrize("change", ["hook", "write"])
def test_lock_callback_rechecks_hooks_and_revision(tmp_path, monkeypatch, change):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        storage = warm(client)
        original = storage._acquire_lock
        invoked = []

        def reenter():
            locks = original()
            if not invoked:
                invoked.append(True)
                if change == "hook":
                    monkeypatch.setattr(storage, "read_table_ids", lambda name: [63])
                else:
                    storage.write_table("items", {"72": {"_id": "nested"}})
            return locks

        monkeypatch.setattr(storage, "_acquire_lock", reenter)
        assert initialize(storage)._last_id == (63 if change == "hook" else 5)
        assert invoked
