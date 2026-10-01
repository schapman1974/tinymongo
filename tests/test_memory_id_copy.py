"""Detached identity planning must not reconstruct native IDs via deepcopy."""

from uuid import uuid4

import pytest

from tinymongo import TinyMongoClient, storage_backends as sb


def test_batch_avoids_deepcopy_of_resident_objectids(monkeypatch):
    ObjectId = pytest.importorskip("bson").ObjectId
    with TinyMongoClient("memory://" + uuid4().hex, backend="memory") as client:
        collection = client.app.items
        ids = [ObjectId() for _ in range(12)]
        collection.insert_many([{"_id": value} for value in ids])
        original = sb.copy.deepcopy
        copied = []

        def tracked(value, *args, **kwargs):
            if type(value) is ObjectId and value in ids:
                copied.append(value)
            return original(value, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(sb.copy, "deepcopy", tracked)
            collection.insert_many([{"_id": ObjectId()}])
        assert copied == []
        snapshot = collection.table._read_insert_snapshot(ids_only=True)
        detached = snapshot[1]["_id"]
        assert detached == ids[0]
        detached.__setstate__(ObjectId().binary)
        assert collection.find_one({"_id": ids[0]}) == {"_id": ids[0]}
        assert collection.count_documents({}) == 13


@pytest.mark.parametrize("kind", ["mutable", "subclass", "unavailable"])
def test_id_copy_preserves_deepcopy_fallback(monkeypatch, kind):
    ObjectId = pytest.importorskip("bson").ObjectId

    class CustomId(ObjectId):
        def __deepcopy__(self, memo):
            calls.append(True)
            return CustomId(self.binary)

    calls = []
    value = {"nested": [1]} if kind == "mutable" else CustomId()
    if kind == "unavailable":
        monkeypatch.setattr(sb, "_ObjectId", None)
        value = ObjectId()
    result = sb._copy_insert_id(value)
    assert result == value
    assert result is not value
    if kind == "mutable":
        result["nested"].append(2)
        assert value == {"nested": [1]}
    elif kind == "subclass":
        assert type(result) is CustomId
        assert calls == [True]
    else:
        result.__setstate__(ObjectId().binary)
        assert result != value
