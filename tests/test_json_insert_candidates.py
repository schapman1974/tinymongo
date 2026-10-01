"""Warm JSON inserts should inspect conflicts rather than every resident row."""

import importlib

import pytest

from tinymongo import TinyMongoClient, indexes, storage_backends as sb
from tinymongo.errors import BulkWriteError, DuplicateKeyError


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("unique", [False, True])
def test_warm_json_inserts_skip_unrelated_residents(
    tmp_path, monkeypatch, batch, unique
):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many(
            [{"_id": i, "email": str(i), "payload": [i]} for i in range(100)]
        )
        if unique:
            col.create_index("email", unique=True)
        # Allow the first insert to construct revision-bound owner maps.
        col.insert_one({"_id": 100, "email": "100"})
        visits = {"identity": 0, "unique": 0}
        identity_key = sb.bson_value_identity_key
        index_tokens = indexes.index_tokens

        def identity(value):
            if type(value) is int and 0 <= value < 100:
                visits["identity"] += 1
            return identity_key(value)

        def tokens(row, field):
            if type(row.get("_id")) is int and 0 <= row["_id"] < 100:
                visits["unique"] += 1
            return index_tokens(row, field)

        with monkeypatch.context() as patch:
            patch.setattr(sb, "bson_value_identity_key", identity)
            patch.setattr(indexes, "index_tokens", tokens)
            patch.setattr(
                importlib.import_module("tinymongo.tinymongo"), "index_tokens", tokens
            )
            for start in (101, 111, 121):
                docs = [{"_id": i, "email": str(i)} for i in range(start, start + 10)]
                if batch:
                    col.insert_many(docs)
                else:
                    col.insert_one(docs[0])
        # Check persisted semantics before the operation-count assertion, so a
        # failing performance baseline still exercises old/new owner conflicts.
        error = BulkWriteError if batch else DuplicateKeyError
        for doc in ({"_id": 0, "email": "fresh"}, {"_id": 121, "email": "fresh"}):
            with pytest.raises(error):
                col.insert_many([doc]) if batch else col.insert_one(doc)
        if unique:
            for email in ("0", "121"):
                with pytest.raises(error):
                    doc = {"_id": 999, "email": email}
                    col.insert_many([doc]) if batch else col.insert_one(doc)
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        assert client.app.items.count_documents({}) == (131 if batch else 104)
        assert client.app.items.find_one({"_id": 0})["payload"] == [0]
    assert visits == {"identity": 0, "unique": 0}


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("change", ["insert", "update", "delete", "index"])
def test_warm_json_candidates_observe_peer_changes(tmp_path, batch, change):
    with TinyMongoClient(str(tmp_path), backend="json") as first:
        col = first.app.items
        col.create_index("email", unique=True)
        col.insert_one({"_id": "old", "email": "old", "other": "shared"})
        col.insert_one({"_id": "warm", "email": "warm", "other": "warm"})
        with TinyMongoClient(str(tmp_path), backend="json") as peer:
            other = peer.app.items
            if change == "insert":
                other.insert_one({"_id": "peer", "email": "new"})
            elif change == "update":
                other.update_one({"_id": "old"}, {"$set": {"email": "new"}})
            elif change == "delete":
                other.delete_one({"_id": "old"})
            else:
                other.create_index("other", unique=True)
        doc = {"_id": "new", "email": "new", "other": "shared"}
        if change == "delete":
            doc = {"_id": "old", "email": "old", "other": "shared"}
            col.insert_many([doc]) if batch else col.insert_one(doc)
            assert col.find_one({"_id": "old"}) == doc
        else:
            with pytest.raises(BulkWriteError if batch else DuplicateKeyError):
                col.insert_many([doc]) if batch else col.insert_one(doc)
            assert col.find_one({"_id": "new"}) is None


def snapshot(col, document):
    return col.table._read_single_insert_snapshot(
        document,
        ids_only=False,
        fields={"_id", "email", "other"},
        specs=tuple(col._index_specs.values()),
    )


@pytest.mark.parametrize("failure", ["conflict", "token_error"])
def test_corrupt_unique_residents_fall_back_to_full_validation(
    tmp_path, monkeypatch, failure
):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "a", "email": "a"})
        col.create_index("email", unique=True)
        storage = col.parent.tinydb._storage
        storage._insert_indexes.clear()
        if failure == "conflict":
            storage.write_table("items", {"2": {"_id": "b", "email": "a"}})
            with pytest.raises(DuplicateKeyError, match="email_1"):
                col.insert_one({"_id": "new", "email": "new"})
        else:
            original = indexes.index_entry_tokens
            fired = []

            def tokens(row, spec):
                if not fired:
                    fired.append(True)
                    raise TypeError("legacy value")
                return original(row, spec)

            monkeypatch.setattr(indexes, "index_entry_tokens", tokens)
            result = snapshot(col, {"_id": "new", "email": "new"})
            assert result.index is None
            assert result[1] == {"_id": "a", "email": "a"}


@pytest.mark.parametrize("change", ["revision", "hook"])
def test_candidate_copy_rechecks_generation_and_hooks(tmp_path, monkeypatch, change):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.create_index("email", unique=True)
        col.insert_one({"_id": "old", "email": "old"})
        original = sb.copy.deepcopy
        storage = col.parent.tinydb._storage
        fired = []

        def copied(value, *args, **kwargs):
            if not fired and isinstance(value, dict) and value.get("_id") == "old":
                fired.append(True)
                if change == "revision":
                    storage.write_table("neighbor", {"1": {"_id": "neighbor"}})
                else:
                    storage.merge_writes = False
            return original(value, *args, **kwargs)

        monkeypatch.setattr(sb.copy, "deepcopy", copied)
        selected = snapshot(col, {"_id": "new", "email": "old"})
        assert selected is None
        assert fired == [True]


def test_candidate_token_error_preserves_duplicate_id_precedence(tmp_path, monkeypatch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.create_index("email", unique=True)
        col.insert_one({"_id": "old", "email": "old"})
        original = indexes.index_entry_tokens

        def tokens(row, spec):
            if row.get("email") == "bad":
                raise TypeError("bad unique value")
            return original(row, spec)

        monkeypatch.setattr(indexes, "index_entry_tokens", tokens)
        with pytest.raises(DuplicateKeyError, match="_id:old"):
            col.insert_one({"_id": "old", "email": "bad"})
        with pytest.raises(TypeError, match="bad unique value"):
            col.insert_one({"_id": "new", "email": "bad"})


def test_normalized_conflict_copy_can_reenter_and_remove_owner(tmp_path, monkeypatch):
    address = str(tmp_path)
    with (
        TinyMongoClient(address, backend="json") as client,
        TinyMongoClient(address, backend="json") as peer,
    ):
        col = client.app.items
        col.create_index("email", unique=True)
        col.insert_one({"_id": "old", "email": "old"})
        original_clone = sb.clone_document
        original_copy = sb.copy.deepcopy
        fired = []

        def clone(value):
            result = original_clone(value)
            for row in result.get("items", {}).values():
                if row.get("_id") == "new":
                    row["email"] = "old"
            return result

        def copied(value, *args, **kwargs):
            if not fired and isinstance(value, dict) and value.get("_id") == "old":
                fired.append(True)
                peer.app.items.delete_one({"_id": "old"})
            return original_copy(value, *args, **kwargs)

        monkeypatch.setattr(sb, "clone_document", clone)
        monkeypatch.setattr(sb.copy, "deepcopy", copied)
        col.insert_one({"_id": "new", "email": "different"})
        assert fired == [True]
        assert col.find_one({"_id": "new"})["email"] == "old"
        assert col.count_documents({}) == 1


@pytest.mark.parametrize("key", [1, "01", "bad"])
def test_noncanonical_storage_keys_keep_full_snapshot(tmp_path, key):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old"})
        storage = col.table._storage._storage
        storage._insert_indexes.clear()
        assert (
            col.table._read_json_candidates(
                {key: {"_id": "old"}}, storage.revision, [{"_id": "new"}], (), ()
            )
            is None
        )


def test_unsupported_incoming_identity_keeps_full_snapshot(tmp_path, monkeypatch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old"})
        original = sb.bson_value_identity_key
        monkeypatch.setattr(
            sb,
            "bson_value_identity_key",
            lambda value: None if value == "new" else original(value),
        )
        result = col.table._read_json_insert_snapshot(documents=[{"_id": "new"}])
        assert result.index is None
        assert result[1] == {"_id": "old"}


@pytest.mark.parametrize("batch", [False, True])
def test_normalized_token_callback_retries_before_owner_lookup(
    tmp_path, monkeypatch, batch
):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.create_index("email", unique=True)
        col.insert_one({"_id": "old", "email": "old"})
        original_clone = sb.clone_document
        original_tokens = indexes.index_entry_tokens
        normalized = []
        fired = []

        def clone(value):
            result = original_clone(value)
            if any(row.get("_id") == "new" for row in result.get("items", {}).values()):
                normalized.append(True)
            return result

        def tokens(row, spec):
            if normalized and not fired and row.get("_id") == "new":
                fired.append(True)
                col.insert_one({"_id": "peer", "email": "new"})
            return original_tokens(row, spec)

        monkeypatch.setattr(sb, "clone_document", clone)
        monkeypatch.setattr(indexes, "index_entry_tokens", tokens)
        with pytest.raises(BulkWriteError if batch else DuplicateKeyError):
            doc = {"_id": "new", "email": "new"}
            col.insert_many([doc]) if batch else col.insert_one(doc)
        assert fired == [True]
        assert col.find_one({"_id": "new"}) is None
        assert col.count_documents({}) == 2
