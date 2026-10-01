"""Unique validation must not copy unrelated resident payloads."""

from uuid import uuid4

import pytest

from tinymongo import TinyMongoClient, storage_backends as sb
from tinymongo.errors import DuplicateKeyError


def test_unique_insert_does_not_copy_unindexed_payload(monkeypatch):
    address = "memory://" + uuid4().hex
    with TinyMongoClient(address, backend="memory") as client:
        col = client.app.items
        col.insert_many(
            [
                {"_id": i, "email": str(i), "payload": {"large": list(range(100))}}
                for i in range(20)
            ]
        )
        col.create_index("email", unique=True)
        copied_payloads = []
        original = sb.copy.deepcopy

        def observe(value, *args, **kwargs):
            if isinstance(value, dict):
                if "payload" in value or "large" in value:
                    copied_payloads.append(value)
                if any(
                    isinstance(row, dict) and "payload" in row for row in value.values()
                ):
                    copied_payloads.append(value)
            return original(value, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(sb.copy, "deepcopy", observe)
            col.insert_one({"_id": 20, "email": "20"})
        assert copied_payloads == []
    with TinyMongoClient(address, backend="memory") as client:
        assert client.app.items.count_documents({}) == 21
        assert client.app.items.find_one({"_id": 0})["payload"] == {
            "large": list(range(100))
        }


@pytest.mark.parametrize(
    "key,options,first,duplicate",
    [
        ("email", {}, {"email": None}, {}),
        ("email", {"sparse": True}, {"email": None}, {"email": None}),
        (
            "profile.email",
            {},
            {"profile": {"email": ["a", "b"]}},
            {"profile": {"email": "b"}},
        ),
        (
            [("group", 1), ("email", 1)],
            {},
            {"group": "g", "email": ["a", "b"]},
            {"group": "g", "email": "b"},
        ),
        ("email", {}, {"email": 1}, {"email": 1.0}),
    ],
)
def test_projected_validation_preserves_unique_semantics(
    key, options, first, duplicate
):
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        col = client.app.items
        col.create_index(key, unique=True, **options)
        col.insert_one(dict(first, _id="old", payload=[1]))
        with pytest.raises(DuplicateKeyError):
            col.insert_one(dict(duplicate, _id="new"))
        assert col.count_documents({}) == 1
        assert col.find_one({"_id": "old"})["payload"] == [1]


def test_projection_detaches_index_roots_and_preserves_missing():
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        col = client.app.items
        col.insert_one(
            {"_id": {"nested": [1]}, "profile": {"email": ["a"]}, "payload": [1]}
        )
        snapshot = col.table._read_single_insert_snapshot(
            {"_id": "new"}, ids_only=False, fields={"_id", "profile", "missing"}
        )
        assert isinstance(snapshot, sb._InsertIDSnapshot)
        assert set(snapshot[1]) == {"_id", "profile"}
        snapshot[1]["profile"]["email"].append("b")
        snapshot[1]["_id"]["nested"].append(2)
        assert col.find_one({}) == {
            "_id": {"nested": [1]},
            "profile": {"email": ["a"]},
            "payload": [1],
        }


@pytest.mark.parametrize("change", ["replacement", "write_hook", "insert_hook"])
def test_projected_rows_never_replace_full_rows(monkeypatch, change):
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        col = client.app.items
        col.insert_one({"_id": "old", "email": "a", "payload": [1]})
        table = col.table
        snapshot = table._read_single_insert_snapshot(
            {"_id": "new"}, ids_only=False, fields={"_id", "email"}
        )
        calls = []
        if change == "replacement":
            table._storage._storage.merge_writes = False
        else:
            hook = "_write" if change == "write_hook" else "insert"
            original = getattr(table, hook)

            def wrapped(*args):
                calls.append(True)
                return original(*args)

            monkeypatch.setattr(table, hook, wrapped)
        table._insert_one_from_snapshot({"_id": "new", "email": "b"}, snapshot)
        if change != "replacement":
            assert calls
        assert col.find_one({"_id": "old"})["payload"] == [1]
        assert col.count_documents({}) == 2


def test_custom_validator_receives_complete_residents(monkeypatch):
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        col = client.app.items
        col.insert_one({"_id": "old", "email": "a", "payload": [1]})
        col.create_index("email", unique=True)
        original = col._validate_unique_post_image
        seen = []

        def validate(documents, extra_specs=()):
            seen.append(documents[0]["payload"])
            original(documents, extra_specs)

        monkeypatch.setattr(col, "_validate_unique_post_image", validate)
        col.insert_one({"_id": "new", "email": "b"})
        assert seen == [[1]]


def test_nonunique_compound_roots_are_included(monkeypatch):
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        col = client.app.items
        col.create_index("email", unique=True)
        col.create_index([("left", 1), ("right", 1)])
        col.insert_one(
            {"_id": "old", "email": "a", "left": [1], "right": 2, "payload": [1]}
        )
        original = sb.MemoryTable._read_single_insert_snapshot
        seen = []

        def read(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            seen.append(result)
            return result

        monkeypatch.setattr(sb.MemoryTable, "_read_single_insert_snapshot", read)
        col.insert_one({"_id": "new", "email": "b", "left": [3], "right": 4})
        assert seen[0][1] == {"_id": "old", "email": "a", "left": [1], "right": 2}


@pytest.mark.parametrize(
    "malformation", ["missing_id", "nonenumerable", "row_subclass"]
)
def test_legacy_rows_keep_full_snapshot(malformation):
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        col = client.app.items
        col.insert_one({"_id": "old", "payload": [1]})
        rows = col.table._storage._storage._entry["data"]["items"]
        if malformation == "missing_id":
            rows["1"].pop("_id")
        elif malformation == "nonenumerable":
            rows["1"]["_id"] = object()
        else:

            class Row(dict):
                pass

            rows["1"] = Row(rows["1"])
        snapshot = col.table._read_single_insert_snapshot(
            {"_id": "new"}, ids_only=False, fields={"_id", "email"}
        )
        assert not isinstance(snapshot, sb._InsertIDSnapshot)
        assert snapshot[1]["payload"] == [1]


@pytest.mark.parametrize("stage", ["copy", "validation"])
@pytest.mark.parametrize("mutation", ["insert", "catalog"])
def test_reentrant_changes_retry_with_current_rows_and_catalog(
    monkeypatch, stage, mutation
):
    import importlib

    module = importlib.import_module("tinymongo.tinymongo")
    address = "memory://" + uuid4().hex
    with (
        TinyMongoClient(address, backend="memory") as client,
        TinyMongoClient(address, backend="memory") as peer,
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
                if not fired and isinstance(value, dict) and value.get("_id") == "old":
                    mutate()
                return original(value, *args, **kwargs)

            monkeypatch.setattr(sb.copy, "deepcopy", copied)
        else:
            original = module.validate_unique_documents

            def validate(documents, specs):
                if not fired and any(row.get("_id") == "new" for row in documents):
                    mutate()
                return original(documents, specs)

            monkeypatch.setattr(module, "validate_unique_documents", validate)
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": "new", "email": "new", "other": "shared"})
        assert fired == [True]
        assert col.find_one({"_id": "new"}) is None
        assert col.find_one({"_id": "old"})["payload"] == [1]


def test_partial_index_keeps_predicate_fields(monkeypatch):
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        col = client.app.items
        col.create_index("email", unique=True, partialFilterExpression={"active": True})
        col.insert_one({"_id": "old", "email": "a", "active": True, "payload": [1]})
        original = sb.MemoryTable._read_single_insert_snapshot
        seen = []

        def read(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            seen.append(result)
            return result

        monkeypatch.setattr(sb.MemoryTable, "_read_single_insert_snapshot", read)
        col.insert_one({"_id": "new", "email": "a", "active": False})
        assert seen[0][1]["payload"] == [1]
        assert seen[0][1]["active"] is True
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": "conflict", "email": "a", "active": True})


def test_sparse_missing_and_boolean_numeric_distinction():
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        col = client.app.items
        col.create_index("email", unique=True, sparse=True)
        for document in [
            {"_id": 1},
            {"_id": 2},
            {"_id": 3, "email": True},
            {"_id": 4, "email": 1},
        ]:
            col.insert_one(document)
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": 5, "email": 1.0})
        assert col.count_documents({}) == 4
