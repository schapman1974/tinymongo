"""Single writes reuse native memory candidates without changing their contract."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from tinymongo import TinyMongoClient, storage_backends as sb
from tinymongo.errors import DuplicateKeyError


def client(address=None):
    return TinyMongoClient(address or "memory://" + uuid4().hex, backend="memory")


def test_single_insert_does_not_read_resident_payloads(monkeypatch):
    with client() as conn:
        col = conn.app.items
        col.insert_many([{"_id": i, "nested": [i]} for i in range(1000)])
        visited = []

        # Observe deepcopy directly, preserving the native read hook guard.
        original_copy = sb.copy.deepcopy

        def copied(value, *args, **kwargs):
            if isinstance(value, dict) and "1" in value:
                visited.extend(value)
            return original_copy(value, *args, **kwargs)

        monkeypatch.setattr(sb.copy, "deepcopy", copied)
        for value in range(1000, 1003):
            result = col.insert_one({"_id": value, "nested": [value]})
            assert result.inserted_id == value
            assert result.eid == value + 1
        assert visited == []
        assert col.count_documents({}) == 1003


@pytest.mark.parametrize(
    "existing,new", [(1, 1.0), ({"n": [1]}, {"n": [1.0]}), (None, None)]
)
def test_single_candidates_reject_exact_identity(existing, new):
    with client() as conn:
        col = conn.app.items
        col.insert_one({"_id": existing, "payload": [1]})
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": new, "payload": [2]})
        assert col.count_documents({}) == 1
        assert col.find_one({})["payload"] == [1]


def test_single_preserves_boolean_identity_and_caller_isolation():
    address = "memory://" + uuid4().hex
    with client(address) as conn:
        col = conn.app.items
        col.insert_one({"_id": 1})
        doc = {"_id": True, "payload": [2]}
        result = col.insert_one(doc)
        assert result.inserted_id is True
        doc["payload"].append(3)
    with client(address) as conn:
        assert conn.app.items.find_one({"_id": True})["payload"] == [2]
        assert conn.app.items.count_documents({}) == 2


def test_single_secondary_unique_constraint_uses_full_table():
    with client() as conn:
        col = conn.app.items
        col.create_index("email", unique=True)
        col.insert_one({"_id": "first", "email": "same"})
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": "second", "email": "same"})
        assert col.count_documents({}) == 1


@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.parametrize("hook", ["all", "insert", "_read", "_write"])
def test_single_native_table_hooks_are_preserved(monkeypatch, hook, unique):
    with client() as conn:
        col = conn.app.items
        col.insert_one({"_id": "old", "payload": [1]})
        if unique:
            col.create_index("email", unique=True, sparse=True)
        original = getattr(sb.MemoryTable, hook)
        calls = []

        def wrapped(self, *args, **kwargs):
            calls.append(self._name)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(sb.MemoryTable, hook, wrapped)
        col.insert_one({"_id": "new"})
        assert "items" in calls
        assert col.find_one({"_id": "old"})["payload"] == [1]


def test_single_clients_serialize_duplicate_writes():
    address = "memory://" + uuid4().hex
    with client(address) as a, client(address) as b:
        a.app.items.insert_many([{"_id": "seed"}])

        def insert(conn):
            try:
                conn.app.items.insert_one({"_id": {"n": [1]}})
                return True
            except DuplicateKeyError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(insert, (a, b))) == [False, True]
        assert a.app.items.count_documents({}) == b.app.items.count_documents({}) == 2


@pytest.mark.parametrize(
    "change", ["insert_hook", "write_hook", "revision", "replacement"]
)
def test_single_append_rechecks_native_capability(monkeypatch, change):
    with client() as conn:
        col = conn.app.items
        col.insert_one({"_id": "old", "payload": [1]})
        table = col.table
        storage = table._storage._storage
        snapshot = table._read_single_insert_snapshot({"_id": "new"})
        assert isinstance(snapshot, sb._InsertCandidates)
        calls = []
        if change in ("insert_hook", "write_hook"):
            name = "insert" if change == "insert_hook" else "_write"
            original = getattr(table, name)

            def wrapped(*args):
                calls.append(True)
                return original(*args)

            monkeypatch.setattr(table, name, wrapped)
        elif change == "revision":
            storage.write_table("items", {"2": {"_id": "neighbor"}})
            # Match refreshed allocation, while retaining the stale snapshot.
            table._last_id = 2
        else:
            storage.merge_writes = False
        eid = table._insert_one_from_snapshot({"_id": "new"}, snapshot)
        assert eid == (3 if change == "revision" else 2)
        if change in ("insert_hook", "write_hook"):
            assert calls
        assert col.find_one({"_id": "old"})["payload"] == [1]
        assert col.find_one({"_id": "new"}) == {"_id": "new"}
        if change == "revision":
            assert col.find_one({"_id": "neighbor"}) == {"_id": "neighbor"}


def test_single_legacy_identity_uses_complete_snapshot(monkeypatch):
    with client() as conn:
        col = conn.app.items
        col.insert_one({"_id": "old", "payload": [1]})
        storage = col.parent.tinydb._storage
        storage._entry.pop("insert_indexes", None)
        monkeypatch.setattr(sb, "bson_value_identity_key", lambda value: None)
        assert col.table._read_single_insert_snapshot({"_id": "new"}) is None
        col.insert_one({"_id": "new"})
        assert col.find_one({"_id": "old"})["payload"] == [1]
        assert col.count_documents({}) == 2


def test_single_json_keeps_durable_path(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="json") as conn:
        col = conn.app.items
        col.insert_one({"_id": "old"})
        assert col.table._read_single_insert_snapshot({"_id": "new"}) is None
        col.insert_one({"_id": "new"})
    with TinyMongoClient(str(tmp_path), backend="json") as conn:
        assert conn.app.items.count_documents({}) == 2


def test_unique_single_insert_avoids_full_resident_copy(monkeypatch):
    with client() as conn:
        col = conn.app.items
        col.insert_many(
            [{"_id": i, "email": str(i), "payload": [i]} for i in range(1000)]
        )
        col.create_index("email", unique=True)
        visited = []
        original_copy = sb.copy.deepcopy

        def copied(value, *args, **kwargs):
            if isinstance(value, dict) and "1" in value and "email" in value["1"]:
                visited.append(len(value))
            return original_copy(value, *args, **kwargs)

        monkeypatch.setattr(sb.copy, "deepcopy", copied)
        result = col.insert_one({"_id": 1000, "email": "1000", "payload": [1000]})
        assert result.inserted_id == 1000
        assert result.eid == 1001
        assert visited == []
        assert col.find_one({"_id": 0})["payload"] == [0]


@pytest.mark.parametrize("change", ["insert_hook", "write_hook", "replacement"])
def test_unique_single_snapshot_rechecks_hooks(monkeypatch, change):
    with client() as conn:
        col = conn.app.items
        col.insert_one({"_id": "old", "payload": [1]})
        col.create_index("email", unique=True, sparse=True)
        table = col.table
        snapshot = table._read_single_insert_snapshot({"_id": "new"}, ids_only=False)
        assert snapshot[1]["payload"] == [1]
        calls = []
        if change == "replacement":
            table._storage._storage.merge_writes = False
        else:
            hook = "insert" if change == "insert_hook" else "_write"
            original = getattr(table, hook)

            def wrapped(*args):
                calls.append(True)
                return original(*args)

            monkeypatch.setattr(table, hook, wrapped)
        assert table._insert_one_from_snapshot({"_id": "new"}, snapshot) == 2
        if change != "replacement":
            assert calls
        assert col.find_one({"_id": "old"})["payload"] == [1]
        assert col.count_documents({}) == 2


def test_unique_single_insert_preserves_partial_compound_and_isolation():
    address = "memory://" + uuid4().hex
    with client(address) as conn:
        col = conn.app.items
        col.create_index(
            [("group", 1), ("email", 1)],
            unique=True,
            partialFilterExpression={"active": True},
        )
        doc = {
            "_id": 1,
            "group": "a",
            "email": ["x", "y"],
            "active": True,
            "payload": [1],
        }
        col.insert_one(doc)
        doc["payload"].append(2)
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": 2, "group": "a", "email": "y", "active": True})
        col.insert_one({"_id": 3, "group": "a", "email": "y", "active": False})
    with client(address) as conn:
        assert conn.app.items.count_documents({}) == 2
        assert conn.app.items.find_one({"_id": 1})["payload"] == [1]


def test_unique_single_clients_serialize_conflicts():
    address = "memory://" + uuid4().hex
    with client(address) as a, client(address) as b:
        a.app.items.create_index("email", unique=True)

        def insert(pair):
            conn, ident = pair
            try:
                conn.app.items.insert_one({"_id": ident, "email": "shared"})
                return True
            except DuplicateKeyError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(insert, [(a, 1), (b, 2)])) == [False, True]
        assert a.app.items.count_documents({}) == b.app.items.count_documents({}) == 1
