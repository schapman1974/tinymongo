"""Native unique batches reuse revision-bound conflict candidates."""

import importlib
from uuid import uuid4

import pytest

from tinymongo import TinyMongoClient, indexes, storage_backends as sb
from tinymongo.errors import BulkWriteError, DuplicateKeyError


def test_warm_unique_batches_skip_unrelated_residents(monkeypatch):
    address = "memory://" + uuid4().hex
    with TinyMongoClient(address, backend="memory") as client:
        col = client.app.items
        col.insert_many(
            [{"_id": i, "email": str(i), "payload": [i]} for i in range(1000)]
        )
        col.create_index("email", unique=True)
        col.insert_one({"_id": 1000, "email": "1000"})
        visited = []
        original = indexes.index_tokens

        def tokens(row, field):
            if row["_id"] < 1000:
                visited.append(row["_id"])
            return original(row, field)

        with monkeypatch.context() as patch:
            patch.setattr(indexes, "index_tokens", tokens)
            patch.setattr(
                importlib.import_module("tinymongo.tinymongo"), "index_tokens", tokens
            )
            for start in range(1001, 1031, 10):
                col.insert_many(
                    [{"_id": i, "email": str(i)} for i in range(start, start + 10)]
                )
        assert visited == []
        for key, email in [(2000, "0"), (2001, "1030"), (0, "fresh")]:
            with pytest.raises(BulkWriteError):
                col.insert_many([{"_id": key, "email": email}])
    with TinyMongoClient(address, backend="memory") as client:
        assert client.app.items.count_documents({}) == 1031
        assert client.app.items.find_one({"_id": 0})["payload"] == [0]


@pytest.mark.parametrize("conflict", ["resident", "batch"])
def test_normalized_batch_conflicts_do_not_poison_cache(monkeypatch, conflict):
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        col = client.app.items
        col.create_index("email", unique=True)
        col.insert_one({"_id": "old", "email": "old"})
        storage = col.table._storage._storage
        revision = storage.revision
        last_id = col.table._last_id
        original = sb.clone_document

        def clone(value):
            result = original(value)
            for row in result.get("items", {}).values():
                row["email"] = "old" if conflict == "resident" else "shared"
            return result

        docs = [{"_id": "a", "email": "a"}, {"_id": "b", "email": "b"}]
        with monkeypatch.context() as patch:
            patch.setattr(sb, "clone_document", clone)
            with pytest.raises(DuplicateKeyError):
                col.insert_many(docs)
        assert storage.revision == revision
        assert col.table._last_id == last_id
        assert col.count_documents({}) == 1
        col.insert_many(docs)
        with pytest.raises(BulkWriteError):
            col.insert_many([{"_id": "again", "email": "b"}])


@pytest.mark.parametrize("ordered", [True, False])
def test_candidates_include_id_and_unique_conflicts_in_operation_order(ordered):
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        col = client.app.items
        col.create_index("email", unique=True, sparse=True)
        col.insert_many(
            [{"_id": "old", "email": ["a", "b"]}, {"_id": "id-owner", "email": "c"}]
        )
        docs = [
            {"_id": "new", "email": True},
            {"_id": "conflict", "email": "b"},
            {"_id": "id-owner", "email": "unused"},
            {"_id": "intra", "email": True},
            {"_id": "number", "email": 1},
            {"_id": "missing"},
            {"_id": "missing-two"},
        ]
        with pytest.raises(BulkWriteError) as error:
            col.insert_many(docs, ordered=ordered)
        details = error.value.details
        assert [e["index"] for e in details["writeErrors"]] == (
            [1] if ordered else [1, 2, 3]
        )
        assert [e["op"] for e in details["writeErrors"]] == (
            [docs[1]] if ordered else docs[1:4]
        )
        assert details["nInserted"] == (1 if ordered else 4)
        assert col.find_one({"_id": "old"})["email"] == ["a", "b"]


def test_zero_timestamp_batch_preserves_planner_conflict_semantics(monkeypatch):
    from bson import Timestamp

    module = importlib.import_module("tinymongo.tinymongo")
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        col = client.app.items
        col.create_index("stamp", unique=True)
        col.insert_one({"_id": "old", "stamp": Timestamp(123, 1)})
        monkeypatch.setattr(module, "_next_server_timestamp", lambda: Timestamp(123, 1))
        doc = {"_id": "new", "stamp": Timestamp(0, 0)}
        with pytest.raises(BulkWriteError) as error:
            col.insert_many([doc])
        assert error.value.details["writeErrors"][0]["index"] == 0
        assert doc["stamp"] == Timestamp(0, 0)
        assert col.count_documents({}) == 1


@pytest.mark.parametrize("stage", ["clone", "validate"])
@pytest.mark.parametrize("change", ["index", "hook"])
def test_batch_append_callback_changes_are_rechecked(monkeypatch, stage, change):
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        col = client.app.items
        col.create_index("email", unique=True)
        col.insert_one({"_id": "old", "email": "old", "other": "same"})
        fired = []
        hooks = []
        write = sb.MemoryTable._write

        def wrapped(self, data):
            hooks.append(True)
            return write(self, data)

        def mutate():
            fired.append(True)
            if change == "index":
                col.create_index("other", unique=True)
            else:
                monkeypatch.setattr(sb.MemoryTable, "_write", wrapped)

        if stage == "clone":
            original = sb.clone_document

            def clone(value):
                result = original(value)
                if not fired and "items" in result:
                    mutate()
                return result

            monkeypatch.setattr(sb, "clone_document", clone)
        else:
            original = indexes.validate_unique_documents

            def validate(rows, specs):
                rows = list(rows)
                result = original(rows, specs)
                if not fired and any(row["_id"] == "new" for row in rows):
                    mutate()
                return result

            monkeypatch.setattr(indexes, "validate_unique_documents", validate)
        docs = [{"_id": "new", "email": "new", "other": "same"}]
        if change == "index":
            with pytest.raises(BulkWriteError):
                col.insert_many(docs)
            assert col.find_one({"_id": "new"}) is None
        else:
            col.insert_many(docs)
            assert hooks
        assert fired == [True]


def test_concurrent_unique_batches_share_atomic_owners():
    from concurrent.futures import ThreadPoolExecutor

    address = "memory://" + uuid4().hex
    with (
        TinyMongoClient(address, backend="memory") as a,
        TinyMongoClient(address, backend="memory") as b,
    ):
        a.app.items.create_index("email", unique=True)
        a.app.items.insert_one({"_id": "warm", "email": "warm"})

        def insert(args):
            client, prefix = args
            try:
                client.app.items.insert_many(
                    [{"_id": prefix + str(i), "email": i} for i in range(10)],
                    ordered=False,
                )
                return 10
            except BulkWriteError as error:
                return error.details["nInserted"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(insert, [(a, "a"), (b, "b")])) == 10
        assert a.app.items.count_documents({}) == 11
        assert b.app.items.count_documents({}) == 11
