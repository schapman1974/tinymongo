"""Native appends stage only new row text and publish after persistence."""

import sys

import pytest

from tinymongo import TinyMongoClient, storage_backends as sb


@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_warm_append_skips_resident_serializer(tmp_path, unique, batch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        if unique:
            col.create_index("n", unique=True)
        col.insert_many([{"_id": i, "n": i} for i in range(100)])
        calls = []
        code = sb.AtomicJSONStorage._serialize_table_parts.__code__

        def profile(frame, event, arg):
            if event == "call" and frame.f_code is code:
                calls.append(frame.f_locals["name"])

        prior = sys.getprofile()
        try:
            sys.setprofile(profile)
            if batch:
                col.insert_many([{"_id": 101, "n": 101}, {"_id": 102, "n": 102}])
            else:
                col.insert_one({"_id": 101, "n": 101})
        finally:
            sys.setprofile(prior)
        assert "items" not in calls
        assert col.count_documents({}) == (102 if batch else 101)
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        assert client.app.items.find_one({"_id": 101})["n"] == 101


@pytest.mark.parametrize("failure", ["encode", "fsync", "replace"])
def test_append_failure_preserves_disk_and_caches(tmp_path, monkeypatch, failure):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old"})
        path = tmp_path / "app.json"
        before = path.read_bytes()
        storage = col.parent.tinydb._storage
        caches = (
            storage._serialized_tables,
            storage._serialized_documents,
            storage._serialized_keys,
        )

        def fail(*args, **kwargs):
            raise OSError("injected")

        with monkeypatch.context() as patch:
            if failure == "encode":
                patch.setattr(sb, "json_dumps", fail)
            else:
                patch.setattr(sb.os, failure, fail)
            with pytest.raises(OSError, match="injected"):
                col.insert_one({"_id": "new"})
        assert path.read_bytes() == before
        assert all(
            old is current
            for old, current in zip(
                caches,
                (
                    storage._serialized_tables,
                    storage._serialized_documents,
                    storage._serialized_keys,
                ),
            )
        )
        col.insert_one({"_id": "new"})
        assert col.count_documents({}) == 2
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        assert client.app.items.count_documents({}) == 2


@pytest.mark.parametrize("batch", [False, True])
def test_encoding_callback_write_retries(tmp_path, monkeypatch, batch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old"})
        original = sb.json_dumps
        invoked = []

        def reenter(value, **kwargs):
            if isinstance(value, dict) and value.get("_id") == "new" and not invoked:
                invoked.append(True)
                col.insert_one({"_id": "nested"})
            return original(value, **kwargs)

        monkeypatch.setattr(sb, "json_dumps", reenter)
        if batch:
            col.insert_many([{"_id": "new"}, {"_id": "other"}])
        else:
            col.insert_one({"_id": "new"})
        assert invoked
        assert {row["_id"] for row in col.find({})} == (
            {"old", "nested", "new", "other"} if batch else {"old", "nested", "new"}
        )


def test_encoding_hook_change_falls_back(tmp_path, monkeypatch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": "old"})
        original = sb.json_dumps
        serialize = sb.AtomicJSONStorage._serialize_table
        calls = []
        invoked = []

        def custom(self, name, table):
            calls.append(name)
            return serialize(self, name, table)

        def reenter(value, **kwargs):
            if isinstance(value, dict) and value.get("_id") == "new" and not invoked:
                invoked.append(True)
                monkeypatch.setattr(sb.AtomicJSONStorage, "_serialize_table", custom)
            return original(value, **kwargs)

        monkeypatch.setattr(sb, "json_dumps", reenter)
        col.insert_one({"_id": "new"})
        assert "items" in calls
        assert col.count_documents({}) == 2
