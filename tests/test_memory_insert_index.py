"""Native batch planning/append must avoid repeating resident identity work."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from tinymongo import TinyMongoClient, storage_backends as sb
from tinymongo.errors import BulkWriteError


def client():
    return TinyMongoClient("memory://" + uuid4().hex, backend="memory")


def test_fixed_batches_do_not_scan_resident_identities(monkeypatch):
    core = importlib.import_module("tinymongo.tinymongo")
    with client() as conn:
        collection = conn.app.items
        collection.insert_many([{"_id": i} for i in range(1000)])
        calls = []
        for module in (sb, core):
            original = module.bson_value_identity_key

            def counted(value, original=original):
                calls.append(value)
                return original(value)

            monkeypatch.setattr(module, "bson_value_identity_key", counted)
        for start in (1000, 1010, 1020):
            calls.clear()
            collection.insert_many([{"_id": i} for i in range(start, start + 10)])
            assert len(calls) <= 60
        assert collection.count_documents({}) == 1030


@pytest.mark.parametrize("ordered", [True, False])
def test_candidates_preserve_duplicates_and_partial_success(ordered):
    with client() as conn:
        collection = conn.app.items
        collection.insert_many([{"_id": {"n": [1]}, "payload": ["old"]}])
        with pytest.raises(BulkWriteError) as caught:
            collection.insert_many(
                [
                    {"_id": "first"},
                    {"_id": {"n": [1.0]}},
                    {"_id": True},
                    {"_id": "first"},
                    {"_id": 1},
                ],
                ordered=ordered,
            )
        assert caught.value.details["nInserted"] == (1 if ordered else 3)
        assert collection.find_one({"_id": {"n": [1]}})["payload"] == ["old"]
        assert collection.count_documents({}) == (2 if ordered else 4)


@pytest.mark.parametrize(
    "mutation",
    ["write", "write_table", "purge", "update", "delete", "drop", "neighbor"],
)
def test_index_invalidates_after_general_mutation(mutation):
    with client() as conn:
        collection = conn.app.items
        collection.insert_many([{"_id": "old", "payload": [1]}])
        storage = collection.parent.tinydb._storage
        before = storage._insert_identity_index("items")
        if mutation in ("write", "write_table"):
            data = {"1": {"_id": "old", "payload": [2]}}
            if mutation == "write":
                storage.write({"items": data})
            else:
                storage.write_table("items", data)
        elif mutation == "purge":
            storage.purge_table("items")
        elif mutation == "update":
            collection.update_one({"_id": "old"}, {"$set": {"payload": [2]}})
        elif mutation == "delete":
            collection.delete_one({"_id": "old"})
        elif mutation == "drop":
            collection.drop()
        else:
            conn.app.neighbor.insert_many([{"_id": "neighbor"}])
        collection.insert_many([{"_id": "new"}])
        assert storage._insert_identity_index("items") is not before
        assert collection.find_one({"_id": "new"}) == {"_id": "new"}
        if mutation in ("write", "write_table", "update"):
            assert collection.find_one({"_id": "old"})["payload"] == [2]
        elif mutation == "neighbor":
            assert collection.find_one({"_id": "old"})["payload"] == [1]
        else:
            assert collection.count_documents({}) == 1


def test_shared_clients_serialize_duplicate_batch_writes():
    address = "memory://" + uuid4().hex
    with (
        TinyMongoClient(address, backend="memory") as a,
        TinyMongoClient(address, backend="memory") as b,
    ):
        a.app.items.insert_many([{"_id": "seed"}])

        def insert(conn):
            try:
                conn.app.items.insert_many([{"_id": {"nested": [1]}}])
                return True
            except BulkWriteError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(insert, (a, b))) == [False, True]
        assert a.app.items.count_documents({}) == b.app.items.count_documents({}) == 2


def test_candidates_and_new_payloads_are_detached():
    with client() as conn:
        collection = conn.app.items
        original = {"_id": {"nested": [1]}, "payload": [2]}
        collection.insert_many([original])
        snapshot = collection.table._read_insert_snapshot(True, [original])
        assert isinstance(snapshot, sb._InsertCandidates)
        snapshot[1]["_id"]["nested"].append(3)
        original["payload"].append(4)
        original["_id"]["nested"].append(5)
        assert collection.find_one({"_id": {"nested": [1]}})["payload"] == [2]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        {"01": {"_id": "a"}},
        {"1": {}},
        {"1": [("x", 1)]},
        {"1": {"_id": 1}, "2": {"_id": 1.0}},
    ],
)
def test_index_rejects_legacy_shapes(rows):
    storage = sb.MemoryStorage(uuid4().hex)
    storage._entry["data"] = {"items": rows}
    assert storage._insert_identity_index("items") is None


def test_non_enumerable_ids_keep_full_snapshot(monkeypatch):
    with client() as conn:
        collection = conn.app.items
        collection.insert_many([{"_id": "old"}])
        storage = collection.parent.tinydb._storage
        storage._entry.pop("insert_indexes")
        monkeypatch.setattr(sb, "bson_value_identity_key", lambda value: None)
        assert storage._insert_identity_index("items") is None
        assert not isinstance(
            collection.table._read_insert_snapshot(True, [{"_id": "new"}]),
            sb._InsertCandidates,
        )


@pytest.mark.parametrize("change", ["hook", "revision", "last_id", "replacement"])
def test_candidate_fallback_reads_complete_rows(monkeypatch, change):
    with client() as conn:
        collection = conn.app.items
        collection.insert_many([{"_id": "old", "payload": [1]}])
        table = collection.table
        storage = table._storage._storage
        snapshot = table._read_insert_snapshot(True, [{"_id": "new"}])
        if change == "hook":
            write = table._write
            monkeypatch.setattr(table, "_write", lambda rows: write(rows))
        elif change == "revision":
            storage.write_table("items", {"2": {"_id": "neighbor"}})
        elif change == "last_id":
            table._last_id += 1
        else:
            storage.merge_writes = False
        table._insert_multiple_from_snapshot([{"_id": "new"}], snapshot)
        assert collection.find_one({"_id": "old"})["payload"] == [1]
        assert collection.find_one({"_id": "new"}) == {"_id": "new"}


@pytest.mark.parametrize(
    "change", ["revision", "hook", "duplicate", "unsupported", "batch_duplicate"]
)
def test_codec_reentry_revalidates_before_append(monkeypatch, change):
    with client() as conn:
        collection = conn.app.items
        collection.insert_many([{"_id": "old", "payload": [1]}])
        table = collection.table
        storage = table._storage._storage
        docs = [{"_id": "new"}, {"_id": "second"}]
        snapshot = table._read_insert_snapshot(True, docs)
        clone = sb.clone_document
        calls = []

        def reenter(value):
            result = clone(value)
            if not calls:
                calls.append(True)
                if change == "revision":
                    storage.write_table("items", {"2": {"_id": "neighbor"}})
                elif change == "hook":
                    write = table._write
                    monkeypatch.setattr(table, "_write", lambda rows: write(rows))
                elif change == "duplicate":
                    result["items"]["2"]["_id"] = "old"
                elif change == "batch_duplicate":
                    result["items"]["3"]["_id"] = "new"
                else:
                    monkeypatch.setattr(
                        sb, "bson_value_identity_key", lambda value: None
                    )
            return result

        monkeypatch.setattr(sb, "clone_document", reenter)
        table._insert_multiple_from_snapshot(docs, snapshot)
        assert collection.find_one({"_id": "old"})["payload"] == [1]
        assert collection.find_one({"_id": "new"}) == {"_id": "new"}
        assert collection.find_one({"_id": "second"}) == {"_id": "second"}


@pytest.mark.parametrize("hook", ["merge", "next_id", "clear_cache"])
def test_additional_native_hooks_disable_candidate_append(monkeypatch, hook):
    with client() as conn:
        collection = conn.app.items
        collection.insert_many([{"_id": "old", "payload": [1]}])
        table = collection.table
        storage = table._storage._storage
        owner, name = {
            "merge": (storage, "_merge_data"),
            "next_id": (table, "_get_next_id"),
            "clear_cache": (table, "clear_cache"),
        }[hook]
        original = getattr(owner, name)
        calls = []

        def wrapped(*args):
            calls.append(True)
            return original(*args)

        monkeypatch.setattr(owner, name, wrapped)
        snapshot = table._read_insert_snapshot(True, [{"_id": "new"}])
        assert not isinstance(snapshot, sb._InsertCandidates)
        assert snapshot[1]["payload"] == [1]
        table._insert_multiple_from_snapshot([{"_id": "new"}], snapshot)
        assert calls
        assert table.all() == [{"_id": "old", "payload": [1]}, {"_id": "new"}]


def test_index_preserves_bson_subtypes_and_reopen():
    bson = pytest.importorskip("bson")
    address = "memory://" + uuid4().hex
    oid = bson.ObjectId()
    values = [oid, bson.Binary(b"x", 0), bson.Binary(b"x", 128), True, 1]
    with TinyMongoClient(address, backend="memory") as conn:
        for value in values:
            conn.app.items.insert_many([{"_id": value, "payload": [1]}])
        oid.__setstate__(bson.ObjectId().binary)
    with TinyMongoClient(address, backend="memory") as conn:
        for value in values[1:]:
            with pytest.raises(BulkWriteError):
                conn.app.items.insert_many([{"_id": value}])
        assert conn.app.items.count_documents({}) == 5
        conn.app.items.insert_many([{"_id": oid}])
        assert conn.app.items.count_documents({}) == 6
