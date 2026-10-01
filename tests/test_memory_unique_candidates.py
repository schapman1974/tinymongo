"""Warm native single writes visit unique conflicts, not every resident row."""

from uuid import uuid4

import pytest

from tinymongo import TinyMongoClient, indexes, storage_backends as sb
from tinymongo.errors import BulkWriteError, DuplicateKeyError


def test_warm_unique_inserts_do_not_visit_resident_rows(monkeypatch):
    address = "memory://" + uuid4().hex
    with TinyMongoClient(address, backend="memory") as client:
        col = client.app.items
        col.insert_many(
            [{"_id": i, "email": str(i), "payload": [i]} for i in range(1000)]
        )
        col.create_index("email", unique=True)
        col.insert_one({"_id": 1000, "email": "1000"})
        visited = []
        identity_calls = []
        original = indexes.index_entry_tokens
        identity = sb.bson_value_identity_key

        def tokens(row, spec):
            if row["_id"] < 1000:
                visited.append(row["_id"])
            return original(row, spec)

        def identify(value):
            identity_calls.append(value)
            return identity(value)

        monkeypatch.setattr(indexes, "index_entry_tokens", tokens)
        monkeypatch.setattr(sb, "bson_value_identity_key", identify)
        for i in range(1001, 1004):
            col.insert_one({"_id": i, "email": str(i)})
        assert visited == []
        assert len(identity_calls) < 30
        for i, email in [(2000, "0"), (2001, "1003"), (0, "fresh")]:
            with pytest.raises(DuplicateKeyError):
                col.insert_one({"_id": i, "email": email})
    with TinyMongoClient(address, backend="memory") as client:
        assert client.app.items.count_documents({}) == 1004
        assert client.app.items.find_one({"_id": 0})["payload"] == [0]


def connection(address=None):
    return TinyMongoClient(address or "memory://" + uuid4().hex, backend="memory")


def snapshot(col, document):
    return col.table._read_single_insert_snapshot(
        document,
        ids_only=False,
        fields={"_id", "email", "other"},
        specs=tuple(col._index_specs.values()),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "update",
        "delete",
        "drop",
        "purge",
        "write",
        "write_table",
        "neighbor",
        "bulk",
        "catalog",
    ],
)
@pytest.mark.parametrize("batch", [False, True])
def test_shared_cache_invalidates_after_mutation(mutation, batch):
    address = "memory://" + uuid4().hex
    with connection(address) as client, connection(address) as peer:
        col = client.app.items
        col.insert_one({"_id": "old", "email": "old"})
        col.create_index("email", unique=True)
        col.insert_one({"_id": "warm", "email": "warm"})
        storage = col.parent.tinydb._storage
        cached = storage._insert_identity_index("items")[3]
        if mutation == "update":
            peer.app.items.update_one({"_id": "old"}, {"$set": {"email": "changed"}})
        elif mutation == "delete":
            peer.app.items.delete_one({"_id": "old"})
        elif mutation == "drop":
            peer.app.items.drop()
            peer.app.items.create_index("email", unique=True)
        elif mutation == "purge":
            storage.purge_table("items")
        elif mutation in ("write", "write_table"):
            rows = {"1": {"_id": "old", "email": "changed"}}
            if mutation == "write":
                storage.write({"items": rows})
            else:
                storage.write_table("items", rows)
        elif mutation == "neighbor":
            peer.app.neighbor.insert_one({"_id": 1})
        elif mutation == "bulk":
            peer.app.items.insert_many([{"_id": "bulk", "email": "bulk"}])
        else:
            peer.app.items.drop_index("email_1")
            peer.app.items.create_index("email", name="new_name", unique=True)
        if batch:
            col.insert_many([{"_id": "new", "email": "new"}])
        else:
            col.insert_one({"_id": "new", "email": "new"})
        assert (storage._insert_identity_index("items")[3] is cached) == (
            mutation == "bulk"
        )
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": "conflict", "email": "new"})
        if mutation in ("update", "write", "write_table"):
            col.insert_one({"_id": "reuse", "email": "old"})
            with pytest.raises(DuplicateKeyError):
                col.insert_one({"_id": "conflict", "email": "changed"})
        if mutation == "delete":
            col.insert_one({"_id": "reuse", "email": "old"})


def test_unique_candidates_preserve_index_and_resident_order():
    with connection() as client:
        col = client.app.items
        # Internal ID order need not equal resident iteration order.
        col.insert_one({"_id": "a", "email": ["a", "b"], "other": "x"})
        col.insert_one({"_id": "b", "email": "c", "other": "y"})
        col.create_index("email", unique=True)
        col.create_index("other", unique=True)
        document = {"_id": "new", "email": ["c", "a"], "other": "y"}
        selected = snapshot(col, document)
        assert isinstance(selected, sb._InsertCandidates)
        assert [row["_id"] for row in selected.values()] == ["a", "b"]
        with pytest.raises(DuplicateKeyError, match="email_1: documents 'b' and 'new'"):
            col.insert_one(document)
        with pytest.raises(DuplicateKeyError, match="_id:a"):
            col.insert_one(dict(document, _id="a"))
        selected[1]["email"].append("detached")
        assert col.find_one({"_id": "a"})["email"] == ["a", "b"]


def test_concurrent_clients_share_unique_cache():
    from concurrent.futures import ThreadPoolExecutor

    address = "memory://" + uuid4().hex
    with connection(address) as a, connection(address) as b:
        a.app.items.create_index("email", unique=True)
        a.app.items.insert_one({"_id": "warm", "email": "warm"})

        def insert(args):
            client, key = args
            try:
                client.app.items.insert_one({"_id": key, "email": "same"})
                return True
            except DuplicateKeyError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(insert, [(a, "a"), (b, "b")])) == [False, True]
        assert a.app.items.count_documents({}) == 2


@pytest.mark.parametrize(
    "stage",
    ["build", "lookup", "append_tokens", "clone", "append_validation", "identity"],
)
@pytest.mark.parametrize("batch", [False, True])
def test_reentrant_callback_retries_unique_validation(monkeypatch, stage, batch):
    address = "memory://" + uuid4().hex
    with connection(address) as client, connection(address) as peer:
        col = client.app.items
        col.insert_one({"_id": "old", "email": "old"})
        col.create_index("email", unique=True)
        if stage != "build" and stage != "identity":
            col.insert_one({"_id": "warm", "email": "warm"})
        fired = []
        new_calls = []

        def mutate():
            fired.append(True)
            peer.app.items.insert_one({"_id": "peer", "email": "new"})

        if stage in ("build", "lookup", "append_tokens"):
            original = indexes.index_entry_tokens

            def tokens(row, spec):
                if row["_id"] == "new":
                    new_calls.append(True)
                if not fired and (
                    (stage == "build" and row["_id"] == "old")
                    or (stage == "lookup" and row["_id"] == "new")
                    or (stage == "append_tokens" and len(new_calls) == 3)
                ):
                    mutate()
                return original(row, spec)

            monkeypatch.setattr(indexes, "index_entry_tokens", tokens)
        elif stage == "clone":
            original = sb.clone_document

            def clone(value):
                if (
                    not fired
                    and "items" in value
                    and any(row.get("_id") == "new" for row in value["items"].values())
                ):
                    mutate()
                return original(value)

            monkeypatch.setattr(sb, "clone_document", clone)
        elif stage == "identity":
            original = sb.bson_value_identity_key

            def identify(value):
                if not fired and value == "old":
                    mutate()
                return original(value)

            monkeypatch.setattr(sb, "bson_value_identity_key", identify)
        else:
            original = indexes.validate_unique_documents

            def validate(rows, specs):
                if not fired and any(row.get("_id") == "new" for row in rows):
                    mutate()
                return original(rows, specs)

            monkeypatch.setattr(indexes, "validate_unique_documents", validate)
        with pytest.raises(BulkWriteError if batch else DuplicateKeyError):
            if batch:
                col.insert_many([{"_id": "new", "email": "new"}])
            else:
                col.insert_one({"_id": "new", "email": "new"})
        assert fired == [True]
        assert col.find_one({"_id": "new"}) is None
        assert col.find_one({"_id": "peer"})["email"] == "new"


@pytest.mark.parametrize("failure", ["conflict", "token_error"])
def test_corrupt_unique_residents_fall_back_to_full_validation(monkeypatch, failure):
    with connection() as client:
        col = client.app.items
        col.insert_one({"_id": "a", "email": "a"})
        col.create_index("email", unique=True)
        storage = col.parent.tinydb._storage
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
            assert not isinstance(result, sb._InsertCandidates)
            assert result[1] == {"_id": "a", "email": "a"}


def test_normalized_unique_tokens_are_validated_before_publish(monkeypatch):
    with connection() as client:
        col = client.app.items
        col.create_index("email", unique=True)
        col.insert_one({"_id": "old", "email": "old"})
        original = sb.clone_document

        def clone(value):
            result = original(value)
            for row in result.get("items", {}).values():
                if row.get("_id") == "new":
                    row["email"] = "old"
            return result

        monkeypatch.setattr(sb, "clone_document", clone)
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": "new", "email": "different"})
        assert col.count_documents({}) == 1


def test_failed_clone_does_not_publish_or_advance_cache(monkeypatch):
    with connection() as client:
        col = client.app.items
        col.create_index("email", unique=True)
        col.insert_one({"_id": "old", "email": "old"})
        storage = col.parent.tinydb._storage
        revision = storage.revision
        original = sb.clone_document

        def clone(value):
            if "items" in value:
                raise ValueError("serialization failed")
            return original(value)

        with monkeypatch.context() as patch:
            patch.setattr(sb, "clone_document", clone)
            with pytest.raises(ValueError, match="serialization failed"):
                col.insert_one({"_id": "new", "email": "new"})
        assert storage.revision == revision
        col.insert_one({"_id": "new", "email": "new"})
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": "again", "email": "new"})


@pytest.mark.parametrize("change", ["revision", "hook"])
def test_candidate_copy_rechecks_generation_and_hooks(monkeypatch, change):
    with connection() as client:
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
        assert not isinstance(selected, sb._InsertCandidates)
        assert selected[1]["email"] == "old"
        assert fired == [True]


def test_candidate_token_error_preserves_duplicate_id_precedence(monkeypatch):
    with connection() as client:
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


def test_normalized_conflict_copy_can_reenter_and_remove_owner(monkeypatch):
    address = "memory://" + uuid4().hex
    with connection(address) as client, connection(address) as peer:
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


@pytest.mark.parametrize("change", ["stale", "signature"])
def test_cache_has_its_own_revision_and_index_signature(change):
    with connection() as client:
        col = client.app.items
        col.create_index("email", unique=True)
        col.insert_one({"_id": "old", "email": "old"})
        storage = col.parent.tinydb._storage
        old = storage._insert_identity_index("items")[3]
        if change == "stale":
            old.revision -= 1
        else:
            # Exercise the signature guard independently of catalog revisions.
            old.signature = ()
        col.insert_one({"_id": "new", "email": "new"})
        assert storage._insert_identity_index("items")[3] is not old
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": "conflict", "email": "old"})
