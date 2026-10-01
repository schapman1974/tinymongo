"""Single JSON inserts retain resident payloads behind native-hook guards."""

from datetime import datetime

from bson import ObjectId
import pytest

from tinymongo import TinyMongoClient, storage_backends as sb
from tinymongo.errors import DuplicateKeyError


def test_single_does_not_copy_or_normalize_residents(tmp_path, monkeypatch):
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
        result = col.insert_one({"_id": 100, "nested": [1]})
        assert result.inserted_id == 100
        assert result.eid == 101
        assert not visits
        assert col.count_documents({}) == 101


def test_bson_caller_isolation_and_reopen(tmp_path):
    oid = ObjectId()
    doc = {"_id": oid, "nested": [datetime(2026, 1, 2), {"a": [1]}]}
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "resident", "payload": [1]})
        col.insert_one(doc)
        doc["nested"][1]["a"].append(2)
        oid.__setstate__(ObjectId().binary)
        assert list(col.find({}))[1]["nested"][1] == {"a": [1]}
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        rows = list(client.app.items.find({}))
        assert len(rows) == 2
        assert rows[0]["payload"] == [1]
        assert rows[1]["_id"] != oid
        assert rows[1]["nested"][0] == datetime(2026, 1, 2)


@pytest.mark.parametrize(
    "old,new", [(1, 1.0), ({"a": [1]}, {"a": [1.0]}), (None, None)]
)
def test_duplicate_identity_keeps_existing_row(tmp_path, old, new):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": old, "payload": [1]})
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": new})
        assert list(col.find({})) == [{"_id": old, "payload": [1]}]


def test_boolean_and_number_are_distinct(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": 1})
        col.insert_one({"_id": True})
        assert col.count_documents({}) == 2


@pytest.mark.parametrize("event", ["insert", "delete", "index"])
def test_clone_reentrancy_replans(tmp_path, monkeypatch, event):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old", "email": "old"})
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
            with pytest.raises(DuplicateKeyError):
                col.insert_one({"_id": "new", "email": "old"})
        else:
            col.insert_one({"_id": "new", "email": "old"})
            assert col.find_one({"_id": "old"}) is None
        assert invoked
        assert col.count_documents({}) == (2 if event == "insert" else 1)


@pytest.mark.parametrize("failure", ["clone", "serialize", "fsync", "replace"])
def test_failure_keeps_cache_and_allocation(tmp_path, monkeypatch, failure):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old", "payload": [1]})
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
                col.insert_one({"_id": "new"})
        assert storage._cached_data is cached
        assert storage._serialized_documents is text
        assert col.table._last_id == last_id
        assert col.insert_one({"_id": "new"}).eid == last_id + 1
        assert col.find_one({"_id": "old"})["payload"] == [1]


@pytest.mark.parametrize("hook", ["all", "insert", "_read", "_write"])
def test_custom_table_hooks_are_called(tmp_path, monkeypatch, hook):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old", "payload": [1]})
        original = getattr(sb.MemoryTable, hook)
        calls = []

        def wrapped(self, *args, **kwargs):
            calls.append(self._name)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(sb.MemoryTable, hook, wrapped)
        col.insert_one({"_id": "new"})
        assert "items" in calls
        assert col.find_one({"_id": "old"})["payload"] == [1]


def test_custom_validator_receives_complete_rows(tmp_path, monkeypatch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old", "payload": [1]})
        original = col._validate_unique_post_image
        seen = []

        def validate(rows):
            seen.extend(rows)
            return original(rows)

        monkeypatch.setattr(col, "_validate_unique_post_image", validate)
        col.insert_one({"_id": "new"})
        assert seen[0]["payload"] == [1]


def test_secondary_unique_index_is_enforced(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.create_index("email", unique=True)
        col.insert_one({"_id": "old", "email": "same"})
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": "new", "email": "same"})
        assert col.count_documents({}) == 1


@pytest.mark.parametrize("change", ["insert_hook", "write_hook", "boundary", "mode"])
def test_changed_capability_preserves_residents(tmp_path, monkeypatch, change):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old", "payload": [1]})
        table = col.table
        snapshot = table._read_single_insert_snapshot({"_id": "new"}, fields={"_id"})
        assert isinstance(snapshot, sb._JSONInsertSnapshot)
        if change in ("insert_hook", "write_hook"):
            method = "insert" if change == "insert_hook" else "_write"
            original = getattr(table, method)
            monkeypatch.setattr(table, method, lambda *a: original(*a))
        elif change == "boundary":
            table._last_id += 1
        else:
            table._storage._storage.merge_writes = False
        table._insert_one_from_snapshot({"_id": "new"}, snapshot)
        assert col.count_documents({}) == 2
        assert col.find_one({"_id": "old"})["payload"] == [1]


def test_external_writer_invalidates_snapshot(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old"})
        snapshot = col.table._read_single_insert_snapshot(
            {"_id": "new"}, fields={"_id"}
        )
        with TinyMongoClient(str(tmp_path), backend="json") as other:
            other.app.items.insert_one({"_id": "external"})
        with pytest.raises(sb._RetryMemoryInsert):
            col.table._insert_one_from_snapshot({"_id": "new"}, snapshot)
        col.insert_one({"_id": "new"})
        assert col.count_documents({}) == 3


def test_id_snapshot_reentrancy_revalidates(tmp_path, monkeypatch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old"})
        # Exercise the conservative full-snapshot path and its copy callback.
        monkeypatch.setattr(sb.MemoryTable, "_read_json_candidates", lambda *a: None)
        original = sb._copy_insert_id
        called = []

        def reenter(value):
            if not called:
                called.append(True)
                col.insert_one({"_id": "new"})
            return original(value)

        monkeypatch.setattr(sb, "_copy_insert_id", reenter)
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": "new"})
        assert col.count_documents({}) == 2


def test_validator_reentrancy_revalidates(tmp_path, monkeypatch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old"})
        original = sb.bson_value_identity_key
        called = []

        def reenter(value):
            if not called:
                called.append(True)
                col.insert_one({"_id": "new"})
            return original(value)

        monkeypatch.setattr(sb, "bson_value_identity_key", reenter)
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": "new"})
        assert col.count_documents({}) == 2
