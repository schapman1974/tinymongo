"""Batch unique validation must not copy unrelated resident payloads."""

import copy
import importlib

import pytest

from tinymongo.errors import BulkWriteError

from bson import ObjectId

from tinymongo import TinyMongoClient, storage_backends as sb

pytestmark = pytest.mark.parametrize("backend", ["memory", "json"])


def test_unique_batch_does_not_copy_resident_payload(tmp_path, backend, monkeypatch):
    address = "memory://" + tmp_path.name if backend == "memory" else str(tmp_path)
    with TinyMongoClient(address, backend=backend) as client:
        col = client.app.items
        documents = [
            {"_id": ObjectId(), "email": str(i), "payload": list(range(100))}
            for i in range(20)
        ]
        col.insert_many(documents)
        col.create_index("email", unique=True)
        original = sb.copy.deepcopy
        copied = []

        def observe(value, *args, **kwargs):
            if isinstance(value, dict) and (
                "payload" in value
                or any(
                    isinstance(row, dict) and "payload" in row for row in value.values()
                )
            ):
                copied.append(True)
            return original(value, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(sb.copy, "deepcopy", observe)
            col.insert_many([{"email": "new-1"}, {"email": "new-2"}])
        assert copied == []
    with TinyMongoClient(address, backend=backend) as client:
        assert client.app.items.count_documents({}) == 22
        assert client.app.items.find_one({"_id": documents[0]["_id"]})[
            "payload"
        ] == list(range(100))


@pytest.mark.parametrize("ordered", [True, False])
@pytest.mark.parametrize(
    "field,options,old,new",
    [
        ("email", {}, None, None),
        ("email", {"sparse": True}, None, None),
        ("email", {}, 1, 1.0),
        ("profile.email", {}, ["a", "b"], "b"),
        ([("group", 1), ("email", 1)], {}, ["a", "b"], "b"),
    ],
)
def test_batch_projection_preserves_duplicate_order(
    tmp_path, backend, ordered, field, options, old, new
):
    def row(key, value):
        result = {"_id": key, "group": "g"}
        result["profile" if field == "profile.email" else "email"] = (
            {"email": value} if field == "profile.email" else value
        )
        return result

    with TinyMongoClient(
        ("memory://" + tmp_path.name if backend == "memory" else str(tmp_path)),
        backend=backend,
    ) as client:
        col = client.app.items
        col.create_index(field, unique=True, **options)
        col.insert_one(dict(row("old", old), payload=[1]))
        with pytest.raises(BulkWriteError) as error:
            col.insert_many(
                [
                    row("accepted", "fresh"),
                    row("conflict", new),
                    row("accepted", "another"),
                    row("last", "last"),
                ],
                ordered=ordered,
            )
        assert [e["index"] for e in error.value.details["writeErrors"]] == (
            [1] if ordered else [1, 2]
        )
        assert error.value.details["nInserted"] == (1 if ordered else 2)
        assert col.find_one({"_id": "old"})["payload"] == [1]


@pytest.mark.parametrize("stage", ["copy", "plan"])
@pytest.mark.parametrize("mutation", ["insert", "catalog"])
def test_batch_retries_reentrant_changes(
    tmp_path, backend, monkeypatch, stage, mutation
):
    module = importlib.import_module("tinymongo.tinymongo")
    address = "memory://" + tmp_path.name if backend == "memory" else str(tmp_path)
    with (
        TinyMongoClient(address, backend=backend) as client,
        TinyMongoClient(address, backend=backend) as peer,
    ):
        col = client.app.items
        col.insert_one(
            {"_id": "old", "email": "old", "other": "shared", "payload": [1]}
        )
        col.create_index("email", unique=True)
        fired = []

        def mutate():
            fired.append(True)
            if mutation == "insert":
                peer.app.items.insert_one({"_id": "peer", "email": "new"})
            else:
                peer.app.items.create_index("other", unique=True)

        if stage == "copy":
            original = sb.copy.deepcopy

            def copied(value, *args, **kwargs):
                if (
                    not fired
                    and isinstance(value, dict)
                    and value.get("email") == "old"
                ):
                    mutate()
                return original(value, *args, **kwargs)

            monkeypatch.setattr(sb.copy, "deepcopy", copied)
        else:
            original = module._plan_insert_many

            def plan(*args, **kwargs):
                result = original(*args, **kwargs)
                if not fired:
                    mutate()
                return result

            monkeypatch.setattr(module, "_plan_insert_many", plan)
        with pytest.raises(BulkWriteError):
            col.insert_many([{"_id": "new", "email": "new", "other": "shared"}])
        assert fired == [True]
        assert col.find_one({"_id": "new"}) is None
        assert col.find_one({"_id": "old"})["payload"] == [1]


@pytest.mark.parametrize("change", ["replacement", "all", "insert_multiple", "_write"])
def test_batch_projection_hook_fallback(tmp_path, backend, monkeypatch, change):
    with TinyMongoClient(
        ("memory://" + tmp_path.name if backend == "memory" else str(tmp_path)),
        backend=backend,
    ) as client:
        col = client.app.items
        col.insert_one({"_id": "old", "email": "a", "payload": [1]})
        table = col.table
        calls = []
        if change == "replacement":
            table._storage._storage.merge_writes = False
        else:
            original = getattr(table, change)

            def wrapped(*args):
                calls.append(True)
                return original(*args)

            monkeypatch.setattr(table, change, wrapped)
        assert table._read_insert_snapshot(fields={"_id", "email"}) is None
        residents = table.all()
        assert residents[0]["payload"] == [1]
        table.insert_multiple([{"_id": "new", "email": "b"}])
        if change != "replacement":
            assert calls
        assert col.find_one({"_id": "old"})["payload"] == [1]


@pytest.mark.parametrize("change", ["replacement", "insert_multiple", "_write"])
def test_batch_hook_change_after_projection(tmp_path, backend, monkeypatch, change):
    with TinyMongoClient(
        ("memory://" + tmp_path.name if backend == "memory" else str(tmp_path)),
        backend=backend,
    ) as client:
        col = client.app.items
        col.insert_one({"_id": "old", "email": "a", "payload": [1]})
        table = col.table
        snapshot = table._read_insert_snapshot(fields={"_id", "email"})
        assert isinstance(snapshot, sb._InsertIDSnapshot)
        calls = []
        if change == "replacement":
            table._storage._storage.merge_writes = False
        else:
            original = getattr(table, change)

            def wrapped(*args):
                calls.append(True)
                return original(*args)

            monkeypatch.setattr(table, change, wrapped)
        table._insert_multiple_from_snapshot([{"_id": "new", "email": "b"}], snapshot)
        if change != "replacement":
            assert calls
        assert col.find_one({"_id": "old"})["payload"] == [1]


@pytest.mark.parametrize(
    "malformation", ["missing", "subclass", "opaque", "duplicate", "internal_collision"]
)
def test_batch_legacy_fallback(tmp_path, backend, malformation):
    with TinyMongoClient(
        ("memory://" + tmp_path.name if backend == "memory" else str(tmp_path)),
        backend=backend,
    ) as client:
        col = client.app.items
        col.insert_one({"_id": "old", "payload": [1]})
        storage = col.table._storage._storage
        rows = (
            storage._entry["data"] if backend == "memory" else storage._cached_data
        )["items"]
        if backend == "json":
            # Native JSON writes and file reloads replace the private snapshot.
            # Inject legacy data the same way, rather than mutating a snapshot
            # whose shape the warm insert index has already validated.
            rows = copy.deepcopy(rows)
            storage._cached_data["items"] = rows
        if malformation == "missing":
            rows["1"].pop("_id")
        elif malformation == "subclass":

            class Row(dict):
                pass

            rows["1"] = Row(rows["1"])
        elif malformation == "opaque":
            rows["1"]["_id"] = object()
        else:
            rows["01" if malformation == "internal_collision" else "2"] = dict(
                rows["1"]
            )
        snapshot = col.table._read_insert_snapshot(fields={"_id"})
        assert not isinstance(snapshot, sb._InsertIDSnapshot)
        assert snapshot[1]["payload"] == [1]


def test_batch_projection_detaches_roots_and_keeps_partial_predicates(
    tmp_path, backend
):
    with TinyMongoClient(
        ("memory://" + tmp_path.name if backend == "memory" else str(tmp_path)),
        backend=backend,
    ) as client:
        col = client.app.items
        col.insert_one(
            {
                "_id": {"nested": [1]},
                "profile": {"email": ["a"]},
                "active": True,
                "payload": [1],
            }
        )
        snapshot = col.table._read_insert_snapshot(fields={"_id", "profile", "missing"})
        snapshot[1]["_id"]["nested"].append(2)
        snapshot[1]["profile"]["email"].append("b")
        assert col.find_one({})["profile"] == {"email": ["a"]}
        col.create_index(
            "profile.email", unique=True, partialFilterExpression={"active": True}
        )
        col.insert_many([{"profile": {"email": "a"}, "active": False}])
        with pytest.raises(BulkWriteError):
            col.insert_many([{"profile": {"email": "a"}, "active": True}])


def test_batch_includes_compound_roots_and_sparse_types(tmp_path, backend, monkeypatch):
    with TinyMongoClient(
        ("memory://" + tmp_path.name if backend == "memory" else str(tmp_path)),
        backend=backend,
    ) as client:
        col = client.app.items
        col.create_index("email", unique=True, sparse=True)
        col.create_index([("left", 1), ("right", 1)])
        col.insert_many(
            [{"_id": "old", "email": True, "left": [1], "right": 2, "payload": [1]}]
        )
        original = sb.MemoryTable._read_insert_snapshot
        seen = []

        def read(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            seen.append(result)
            return result

        monkeypatch.setattr(sb.MemoryTable, "_read_insert_snapshot", read)
        col.insert_many([{"email": 1}, {}, {}])
        assert seen[0][1] == {"_id": "old", "email": True, "left": [1], "right": 2}
        assert col.count_documents({}) == 4
        with pytest.raises(BulkWriteError):
            col.insert_many([{"email": 1.0}])


def test_collection_batch_preserves_custom_all_hook(tmp_path, backend, monkeypatch):
    with TinyMongoClient(
        ("memory://" + tmp_path.name if backend == "memory" else str(tmp_path)),
        backend=backend,
    ) as client:
        col = client.app.items
        col.insert_one({"_id": "old", "email": "a", "payload": [1]})
        col.create_index("email", unique=True)
        original = sb.MemoryTable.all
        seen = []

        def all_rows(self):
            rows = original(self)
            if self._name == "items":
                seen.append(rows[0]["payload"])
            return rows

        monkeypatch.setattr(sb.MemoryTable, "all", all_rows)
        col.insert_many([{"_id": "new", "email": "b"}])
        assert seen == [[1]]
