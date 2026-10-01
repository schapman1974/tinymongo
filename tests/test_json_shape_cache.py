"""Warm JSON shape validation follows the validated table and file revision."""

import pytest

from tinymongo import TinyMongoClient, storage_backends as sb
from tinymongo.errors import DuplicateKeyError


@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_warm_inserts_do_not_recheck_resident_shapes(
    tmp_path, monkeypatch, unique, batch
):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        if unique:
            col.create_index("n", unique=True)
        col.insert_many([{"_id": i, "n": i, "payload": [i]} for i in range(100)])
        col.insert_one({"_id": 100, "n": 100})
        storage = col.parent.tinydb._storage
        resident_ids = {id(row) for row in storage._cached_data["items"].values()}
        checked = []

        def counted_type(value):
            if id(value) in resident_ids:
                checked.append(id(value))
            return type(value)

        with monkeypatch.context() as patch:
            patch.setattr(sb, "type", counted_type, raising=False)
            for i in range(101, 104):
                doc = {"_id": i, "n": i}
                col.insert_many([doc]) if batch else col.insert_one(doc)
        for i in (0, 103):
            with pytest.raises(DuplicateKeyError):
                col.insert_one({"_id": i, "n": 999})
            if unique:
                with pytest.raises(DuplicateKeyError):
                    col.insert_one({"_id": 999, "n": i})
        assert col.count_documents({}) == 104
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        assert client.app.items.count_documents({}) == 104
        assert client.app.items.find_one({"_id": 0})["payload"] == [0]
    assert checked == []


@pytest.mark.parametrize("change", ["cold", "stale", "replacement"])
def test_changed_table_or_revision_revalidates_residents(tmp_path, monkeypatch, change):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_many([{"_id": i} for i in range(4)])
        col.insert_one({"_id": 4})
        storage = col.parent.tinydb._storage
        rows = storage._cached_data["items"]
        if change == "cold":
            storage._insert_indexes.clear()
        elif change == "stale":
            storage._insert_indexes["items"][0] = ("stale",)
        else:
            # A distinct private table must not inherit the old table's proof,
            # even if file metadata did not change.
            rows = {str(i + 1): {"_id": i + 10} for i in range(5)}
            storage._cached_data["items"] = rows
        residents = {id(row) for row in rows.values()}
        checked = set()

        def counted_type(value):
            if id(value) in residents:
                checked.add(id(value))
            return type(value)

        monkeypatch.setattr(sb, "type", counted_type, raising=False)
        result = col.table._read_json_insert_snapshot(documents=[{"_id": 14}])
        assert result is not None
        assert checked == residents
        if change == "replacement":
            assert list(result.values()) == [{"_id": 14}]
        else:
            assert result == {}


@pytest.mark.parametrize("rows", [[], {"1": []}, {"1": {"n": 1}}])
def test_replaced_malformed_table_keeps_shape_fallback(tmp_path, rows):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": 0})
        col.insert_one({"_id": 1})
        storage = col.parent.tinydb._storage
        storage._cached_data["items"] = rows
        assert col.table._read_json_insert_snapshot(documents=[{"_id": 2}]) is None


def test_public_reads_cannot_mutate_validated_table(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        col.insert_one({"_id": 0, "payload": [0]})
        col.insert_one({"_id": 1})
        storage = col.parent.tinydb._storage
        public = storage.read_table("items")
        public["1"].pop("_id")
        public["1"]["payload"].append(99)
        col.insert_one({"_id": 2})
        assert col.find_one({"_id": 0}) == {"_id": 0, "payload": [0]}
        assert storage._insert_indexes["items"][4] is storage._cached_data["items"]
