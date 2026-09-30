"""JSON dirty-table caching must preserve the durable whole-file contract."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from tinymongo import TinyMongoClient
from tinymongo import storage_backends as sb
from tinymongo.bson_codec import loads
from tinymongo.errors import DuplicateKeyError, InvalidDocument, StorageCorruptionError


def test_untouched_json_tables_are_not_decoded_copied_or_serialized(
    tmp_path, monkeypatch
):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        db = client.app
        db.archive.insert_one({"_id": "large", "body": "x" * 100_000})
        storage = db.tinydb._storage
        archive = storage._cached_data["archive"]
        chunk = storage._serialized_tables["archive"]
        original_dumps = sb.json_dumps
        original_copy = sb.copy.deepcopy

        def reject_read(*args, **kwargs):
            pytest.fail("warm collection operation decoded the whole database")

        def check_dumps(value, **kwargs):
            assert value is not archive
            assert not (isinstance(value, dict) and "archive" in value)
            return original_dumps(value, **kwargs)

        def check_copy(value, *args, **kwargs):
            assert value is not archive
            assert not (isinstance(value, dict) and "archive" in value)
            return original_copy(value, *args, **kwargs)

        monkeypatch.setattr(storage, "read", reject_read)
        monkeypatch.setattr(sb, "json_dumps", check_dumps)
        monkeypatch.setattr(sb.copy, "deepcopy", check_copy)
        target = db.items
        target.insert_one({"_id": "one", "value": 1})
        target.create_index("value", unique=True)
        with pytest.raises(DuplicateKeyError):
            target.insert_one({"_id": "two", "value": 1})
        target.update_one({"_id": "one"}, {"$set": {"value": 2}})
        assert target.find_one({"_id": "one"})["value"] == 2
        assert "archive" in db.list_collection_names()
        target.delete_one({"_id": "one"})
        target.drop()
        assert storage._cached_data["archive"] is archive
        assert storage._serialized_tables["archive"] is chunk
        assert loads((tmp_path / "app.json").read_text())["archive"] == archive


def test_table_merge_replacement_bson_and_caller_isolation(tmp_path):
    path = tmp_path / "app.json"
    storage = sb.AtomicJSONStorage(str(path))
    storage.write({"neighbor": {"1": {"_id": "keep"}}})
    source = {1: {"_id": True, "nested": [1], "when": datetime(2026, 1, 1)}}
    storage.write_table("items", source)
    source[1]["nested"].append(2)
    returned = storage.read_table("items")
    returned["1"]["nested"].append(3)
    assert storage.read_table("items")["1"]["nested"] == [1]
    assert storage.read_table("items")["1"]["when"] == datetime(2026, 1, 1)
    storage.write_table("items", {1: {"_id": 1, "nested": [4]}})
    assert len(storage.read_table("items")) == 2  # bool and int IDs stay distinct
    storage.merge_writes = False
    storage.write_table("items", {})
    assert storage.read_table("items") == {}
    assert storage.read()["neighbor"] == {"1": {"_id": "keep"}}
    storage.purge_table("absent")
    storage.purge_table("items")
    assert storage.table_names() == {"neighbor"}
    storage.close()
    assert storage._cached_data == storage._serialized_tables == {}
    assert storage.table_names() == {"neighbor"}


def test_external_replace_edit_delete_corruption_and_whole_storage_write(tmp_path):
    path = tmp_path / "app.json"
    first = sb.AtomicJSONStorage(str(path))
    second = sb.AtomicJSONStorage(str(path))
    first.write_table("one", {1: {"_id": 1}})
    assert second.table_names() == {"one"}
    second.write_table("two", {1: {"_id": 2}})
    assert first.table_names() == {"one", "two"}
    second.merge_writes = False
    second.write({"replacement": {"1": {"_id": 3}}})
    assert second.table_names() == first.table_names() == {"replacement"}
    path.write_text('{"edited": {"1": {"_id": 4}}}')
    assert first.read_table("edited") == {"1": {"_id": 4}}
    path.write_text("broken")
    with pytest.raises(StorageCorruptionError):
        first.table_names()
    with pytest.raises(StorageCorruptionError):
        first.write_table("lost", {})
    assert path.read_text() == "broken"
    path.unlink()
    assert first.table_names() == set()
    path.touch()
    assert first.table_names() == set()
    first.write_table("new", {1: {"_id": 5}})
    assert second.table_names() == {"new"}


@pytest.mark.parametrize("failure", ["serialize", "replace", "fsync"])
def test_failed_write_does_not_publish_cached_values(tmp_path, monkeypatch, failure):
    path = tmp_path / "app.json"
    storage = sb.AtomicJSONStorage(str(path))
    storage.write_table("items", {1: {"_id": 1, "value": "before"}})
    original = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("injected failure")

    with monkeypatch.context() as patch:
        if failure == "serialize":
            data = {1: {"_id": 1, "value": object()}}
            error = InvalidDocument
        else:
            patch.setattr(os, failure, fail)
            data = {1: {"_id": 1, "value": "after"}}
            error = OSError
        with pytest.raises(error):
            storage.write_table("items", data)
    assert path.read_bytes() == original
    assert storage.read_table("items")["1"]["value"] == "before"
    assert not list(tmp_path.glob("tmp*"))
    storage.write_table("items", {1: {"_id": 1, "value": "retry"}})
    assert sb.AtomicJSONStorage(str(path)).read_table("items")["1"]["value"] == "retry"


def test_codec_marker_collection_names_keep_escaped_root_format(tmp_path):
    path = tmp_path / "app.json"
    storage = sb.AtomicJSONStorage(str(path))
    for name in ("__tinymongo_type_v1__", "value"):
        storage.write_table(name, {1: {"_id": name}})
    expected = {
        name: {"1": {"_id": name}} for name in ("__tinymongo_type_v1__", "value")
    }
    assert loads(path.read_text()) == expected
    assert sb.AtomicJSONStorage(str(path)).read() == expected


def test_independent_clients_keep_neighbor_writes_and_uniqueness(tmp_path):
    def insert(worker):
        with TinyMongoClient(str(tmp_path), backend="json") as client:
            items = client.app.items
            for index in range(8):
                items.insert_one(
                    {"_id": f"{worker}-{index}", "value": worker * 8 + index}
                )
            with pytest.raises(DuplicateKeyError):
                items.insert_one({"_id": "duplicate", "value": -1})

    with TinyMongoClient(str(tmp_path), backend="json") as client:
        client.app.items.create_index("value", unique=True)
        client.app.items.insert_one({"_id": "seed", "value": -1})
        client.app.archive.insert_one({"_id": "untouched"})
        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(insert, range(3)))
        assert client.app.items.count_documents({}) == 25
        assert client.app.archive.find_one({}) == {"_id": "untouched"}


def test_retained_reader_refreshes_query_and_index_caches_after_external_changes(
    tmp_path,
):
    with TinyMongoClient(str(tmp_path), backend="json") as writer:
        with TinyMongoClient(str(tmp_path), backend="json") as reader:
            items = reader.app.items
            items.create_index("value")
            assert list(items.find({"value": "new"})) == []
            writer.app.items.insert_one({"_id": 1, "value": "new"})
            assert [doc["_id"] for doc in items.find({"value": "new"})] == [1]
            writer.app.items.update_one({"_id": 1}, {"$set": {"value": "changed"}})
            assert list(items.find({"value": "new"})) == []
            assert [doc["_id"] for doc in items.find({"value": "changed"})] == [1]
            (tmp_path / "app.json").unlink()
            assert list(items.find({"value": "changed"})) == []
