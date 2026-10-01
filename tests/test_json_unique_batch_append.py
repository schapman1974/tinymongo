"""JSON unique batches revalidate normalized values before atomic publication."""

import pytest

from tinymongo import TinyMongoClient, storage_backends as sb
from tinymongo.errors import BulkWriteError, DuplicateKeyError


@pytest.mark.parametrize("stage", ["clone", "normalized_validation"])
@pytest.mark.parametrize("change", ["insert", "index", "hook"])
def test_batch_append_rechecks_callbacks(tmp_path, monkeypatch, stage, change):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old", "email": "old", "other": "same", "payload": [1]})
        col.create_index("email", unique=True)
        called = []
        hooks = []
        original_write = sb.MemoryTable._write

        def write(self, data):
            hooks.append(True)
            return original_write(self, data)

        def mutate():
            called.append(True)
            if change == "insert":
                col.insert_one({"_id": "peer", "email": "new"})
            elif change == "index":
                col.create_index("other", unique=True)
            else:
                monkeypatch.setattr(sb.MemoryTable, "_write", write)

        if stage == "clone":
            original = sb.clone_document

            def clone(value):
                result = original(value)
                if not called and "items" in result:
                    mutate()
                return result

            monkeypatch.setattr(sb, "clone_document", clone)
        else:
            original = sb._indexes.validate_unique_documents

            def validate(rows, specs):
                result = original(rows, specs)
                if not called and any(row.get("_id") == "new" for row in rows):
                    mutate()
                return result

            monkeypatch.setattr(sb._indexes, "validate_unique_documents", validate)
        docs = [{"_id": "new", "email": "new", "other": "same"}]
        if change == "hook":
            col.insert_many(docs)
            assert hooks
        else:
            with pytest.raises(BulkWriteError):
                col.insert_many(docs)
            assert col.find_one({"_id": "new"}) is None
        assert called == [True]
        assert col.find_one({"_id": "old"})["payload"] == [1]


@pytest.mark.parametrize("conflict", ["resident", "batch"])
def test_normalized_batch_conflict_does_not_publish(tmp_path, monkeypatch, conflict):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old", "email": "old", "payload": [1]})
        col.create_index("email", unique=True)
        original = sb.clone_document
        storage = col.table._storage._storage
        cached = storage._cached_data
        last_id = col.table._last_id

        def clone(value):
            result = original(value)
            for row in result.get("items", {}).values():
                row["email"] = "old" if conflict == "resident" else "shared"
            return result

        docs = [{"_id": "new", "email": "new"}, {"_id": "second", "email": "second"}]
        with monkeypatch.context() as patch:
            patch.setattr(sb, "clone_document", clone)
            with pytest.raises(DuplicateKeyError):
                col.insert_many(docs)
        assert storage._cached_data is cached
        assert col.table._last_id == last_id
        col.insert_many(docs)
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        assert client.app.items.count_documents({}) == 3
        assert client.app.items.find_one({"_id": "old"})["payload"] == [1]


@pytest.mark.parametrize("failure", ["clone", "serialize", "fsync", "replace"])
def test_unique_batch_failure_preserves_published_state(tmp_path, monkeypatch, failure):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old", "email": "old", "payload": [1]})
        col.create_index("email", unique=True)
        storage = col.table._storage._storage
        cached = storage._cached_data
        last_id = col.table._last_id

        def fail(*args, **kwargs):
            raise OSError("injected")

        with monkeypatch.context() as patch:
            owner, name = (
                (sb, "clone_document" if failure == "clone" else "json_dumps")
                if failure in ("clone", "serialize")
                else (sb.os, failure)
            )
            patch.setattr(owner, name, fail)
            with pytest.raises(OSError, match="injected"):
                col.insert_many([{"_id": "new", "email": "new"}])
        assert storage._cached_data is cached
        assert col.table._last_id == last_id
        col.insert_many([{"_id": "new", "email": "new"}])
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        assert client.app.items.count_documents({}) == 2
        assert client.app.items.find_one({"_id": "old"})["payload"] == [1]
