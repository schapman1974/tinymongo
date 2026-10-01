"""JSON batches retain private resident rows without copying their payloads."""

from datetime import datetime
import importlib

from bson import ObjectId
import pytest

from tinymongo import TinyMongoClient, storage_backends as sb
from tinymongo.errors import BulkWriteError


def test_batch_does_not_copy_or_compare_resident_payloads(tmp_path, monkeypatch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": i, "payload": [i] * 100} for i in range(100)])
        original = sb.copy.deepcopy
        visits = []

        def counted(value, *args, **kwargs):
            if isinstance(value, dict) and any(
                isinstance(row, dict) and "payload" in row for row in value.values()
            ):
                visits.append(value)
            return original(value, *args, **kwargs)

        monkeypatch.setattr(sb.copy, "deepcopy", counted)
        monkeypatch.setattr(
            sb, "storage_values_equal", lambda *args: pytest.fail("resident compared")
        )
        col.insert_many([{"_id": 100, "nested": [1]}])
        assert not visits
        assert col.count_documents({}) == 101


def test_bson_caller_isolation_and_reopen(tmp_path):
    oid = ObjectId()
    doc = {"_id": oid, "nested": [datetime(2026, 1, 2), {"a": [1]}]}
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": "resident", "payload": [1]}])
        col.insert_many([doc])
        doc["nested"][1]["a"].append(2)
        oid.__setstate__(ObjectId().binary)
        assert list(col.find({}))[1]["nested"][1] == {"a": [1]}
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        rows = list(client.app.items.find({}))
        assert len(rows) == 2
        assert rows[0]["payload"] == [1]
        assert rows[1]["_id"] != oid
        assert rows[1]["nested"][0] == datetime(2026, 1, 2)


@pytest.mark.parametrize("ordered", [True, False])
def test_duplicate_batch_keeps_partial_success(tmp_path, ordered):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": 1}])
        with pytest.raises(BulkWriteError) as caught:
            col.insert_many([{"_id": 2}, {"_id": 1.0}, {"_id": 3}], ordered=ordered)
        assert caught.value.details["nInserted"] == (1 if ordered else 2)
        assert col.count_documents({}) == (2 if ordered else 3)


@pytest.mark.parametrize(
    "owner,method",
    [
        ("table", "all"),
        ("table", "insert_multiple"),
        ("table", "_read"),
        ("table", "_write"),
        ("table", "_get_next_id"),
        ("table", "clear_cache"),
        ("proxy", "read"),
        ("proxy", "write"),
        ("storage", "read"),
        ("storage", "read_table"),
        ("storage", "write_table"),
        ("storage", "_merge_data"),
        ("storage", "_serialize_table"),
        ("storage", "_write_cached_tables"),
        ("storage", "_load_cached"),
        ("storage", "_file_signature"),
    ],
)
def test_native_hook_guard(tmp_path, monkeypatch, owner, method):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": "old"}])
        table = col.table
        target = {
            "table": table,
            "proxy": table._storage,
            "storage": table._storage._storage,
        }[owner]
        original = getattr(target, method)
        monkeypatch.setattr(target, method, lambda *a, **k: original(*a, **k))
        assert not table._native_json_append()
        assert not isinstance(
            table._read_insert_snapshot(ids_only=True), sb._JSONInsertSnapshot
        )
        col.insert_many([{"_id": "new"}])
        assert col.count_documents({}) == 2


@pytest.mark.parametrize("event", ["insert", "delete", "index"])
def test_clone_reentrancy_replans(tmp_path, monkeypatch, event):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": "old", "email": "old"}])
        original = sb.clone_document
        invoked = []

        def reenter(value):
            if not invoked and "items" in value:
                invoked.append(True)
                if event == "insert":
                    col.insert_one({"_id": "new", "email": "new"})
                elif event == "delete":
                    col.delete_one({"_id": "old"})
                else:
                    col.create_index("email", unique=True)
            return original(value)

        monkeypatch.setattr(sb, "clone_document", reenter)
        if event in ("insert", "index"):
            with pytest.raises(BulkWriteError):
                col.insert_many([{"_id": "new", "email": "old"}])
        else:
            col.insert_many([{"_id": "new", "email": "old"}])
            assert col.find_one({"_id": "old"}) is None
        assert invoked
        assert col.count_documents({}) == (2 if event == "insert" else 1)


@pytest.mark.parametrize("failure", ["clone", "serialize", "fsync", "replace"])
def test_failure_keeps_cached_rows_and_retry(tmp_path, monkeypatch, failure):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": "old", "payload": [1]}])
        storage = col.table._storage._storage
        cached = storage._cached_data
        text = storage._serialized_documents
        last_id = col.table._last_id

        def fail(*args, **kwargs):
            raise OSError("injected failure")

        with monkeypatch.context() as patch:
            if failure == "clone":
                patch.setattr(sb, "clone_document", fail)
            elif failure == "serialize":
                patch.setattr(sb, "json_dumps", fail)
            else:
                patch.setattr(sb.os, failure, fail)
            with pytest.raises(OSError, match="injected"):
                col.insert_many([{"_id": "new"}])
        assert storage._cached_data is cached
        assert storage._serialized_documents is text
        assert col.table._last_id == last_id
        col.insert_many([{"_id": "new"}])
        assert col.count_documents({}) == 2
        assert col.find_one({"_id": "old"})["payload"] == [1]


def test_unique_secondary_index_rejects_duplicate(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": "old", "email": "same"}])
        col.create_index("email", unique=True)
        with pytest.raises(BulkWriteError):
            col.insert_many([{"_id": "new", "email": "same"}])
        assert col.count_documents({}) == 1


def test_revision_change_after_planner_retries(tmp_path, monkeypatch):
    core = importlib.import_module("tinymongo.tinymongo")
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": "old"}])
        original = core._plan_insert_many
        called = []

        def plan(*args, **kwargs):
            result = original(*args, **kwargs)
            if not called:
                called.append(True)
                col.insert_one({"_id": "new"})
            return result

        monkeypatch.setattr(core, "_plan_insert_many", plan)
        with pytest.raises(BulkWriteError):
            col.insert_many([{"_id": "new"}])
        assert col.count_documents({}) == 2


@pytest.mark.parametrize(
    "rows",
    [
        [],
        {"1": []},
        {"1": {"payload": 1}},
        {"1": {"_id": 1}, "01": {"_id": 2}},
        {"1": {"_id": 1}, "2": {"_id": 1.0}},
        {"1": {"_id": object()}},
        {"2": {"_id": "different-boundary"}},
    ],
)
def test_legacy_snapshot_falls_back(tmp_path, rows):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": "old"}])
        storage = col.table._storage._storage
        storage._cached_data["items"] = rows
        assert col.table._read_json_insert_snapshot() is None


def test_external_client_invalidates_snapshot(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": "old"}])
        snapshot = col.table._read_insert_snapshot(ids_only=True)
        with TinyMongoClient(str(tmp_path), backend="json") as other:
            other.app.items.insert_many([{"_id": "external"}])
        with pytest.raises(sb._RetryMemoryInsert):
            col.table._append_json_from_snapshot([{"_id": "new"}], snapshot)
        col.insert_many([{"_id": "new"}])
        assert col.count_documents({}) == 3


@pytest.mark.parametrize("change", ["mode", "boundary", "hook"])
def test_changed_append_capability_keeps_complete_rows(tmp_path, monkeypatch, change):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": "old", "payload": [1]}])
        table = col.table
        snapshot = table._read_insert_snapshot(ids_only=True)
        if change == "mode":
            table._storage._storage.merge_writes = False
        elif change == "boundary":
            table._last_id += 1
        else:
            original = table._write
            monkeypatch.setattr(table, "_write", lambda *a: original(*a))
        table._insert_multiple_from_snapshot([{"_id": "new"}], snapshot)
        assert col.find_one({"_id": "old"})["payload"] == [1]
        assert col.count_documents({}) == 2


@pytest.mark.parametrize("normalized_id", ["old", object()])
def test_normalized_identity_conflict_uses_full_write(
    tmp_path, monkeypatch, normalized_id
):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": "old", "payload": [1]}])
        original = sb.clone_document
        calls = []

        def normalize(value):
            result = original(value)
            if "items" in result and not calls:
                calls.append(True)
                for row in result["items"].values():
                    row["_id"] = normalized_id
            return result

        monkeypatch.setattr(sb, "clone_document", normalize)
        col.insert_many([{"_id": "new"}])
        assert col.find_one({"_id": "old"})["payload"] == [1]
        assert col.find_one({"_id": "new"}) is not None


def test_id_snapshot_callback_forces_fresh_planning(tmp_path, monkeypatch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": "old"}])
        # Exercise the conservative full-snapshot path and its copy callback.
        monkeypatch.setattr(sb.MemoryTable, "_read_json_candidates", lambda *a: None)
        original = sb._copy_insert_id
        calls = []

        def reenter(value):
            if not calls:
                calls.append(True)
                col.insert_one({"_id": "new"})
            return original(value)

        monkeypatch.setattr(sb, "_copy_insert_id", reenter)
        with pytest.raises(BulkWriteError):
            col.insert_many([{"_id": "new"}])
        assert col.count_documents({}) == 2
