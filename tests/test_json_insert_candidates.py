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
