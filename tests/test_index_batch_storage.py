"""Index batches persist once without losing sequential failure semantics."""

from uuid import uuid4

import pytest

from tinymongo import TinyMongoClient
from tinymongo.errors import DuplicateKeyError, OperationFailure
from tinymongo.indexes import INDEX_CATALOG_TABLE, IndexSpec
from tinymongo.storage_backends import AtomicJSONStorage, MemoryStorage


@pytest.fixture(params=["json", "memory"])
def target(request, tmp_path):
    backend = request.param
    address = str(tmp_path) if backend == "json" else "memory://" + uuid4().hex
    with TinyMongoClient(address, backend=backend) as client:
        yield client, address, backend


def test_batch_persists_once_and_retry_does_not_write(target, monkeypatch):
    client, address, backend = target
    client.app.archive.insert_one({"_id": "archive", "body": "x" * 100000})
    col = client.app.docs
    col.insert_one({"_id": 1, "email": "one"})
    storage = AtomicJSONStorage if backend == "json" else MemoryStorage
    original = storage.write_table
    writes = []

    def write(self, name, *args, **kwargs):
        writes.append(name)
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(storage, "write_table", write)
    models = [{"key": {f"field{i}": 1}} for i in range(20)]
    models += [{"key": {"email": 1}, "unique": True}, {"key": {"_id": 1}}]
    expected = [f"field{i}_1" for i in range(20)] + ["email_1", "_id_"]
    assert col.create_indexes(models) == expected
    assert writes == [INDEX_CATALOG_TABLE]
    writes.clear()
    assert col.create_indexes(models + models[:1]) == expected + expected[:1]
    assert col.create_indexes([]) == []
    assert writes == []
    with TinyMongoClient(address, backend=backend) as other:
        assert {i["name"] for i in other.app.docs.list_indexes()} == set(expected)
        with pytest.raises(DuplicateKeyError):
            other.app.docs.insert_one({"_id": 2, "email": "one"})
        assert other.app.archive.find_one({"_id": "archive"})["body"] == "x" * 100000


@pytest.mark.parametrize("failure", ["unique", "name", "equivalent"])
def test_prior_indexes_survive_later_batch_failure(target, failure):
    client, address, backend = target
    col = client.app.docs
    col.insert_many([{"_id": 1, "email": "same"}, {"_id": 2, "email": "same"}])
    bad = {
        "unique": {"key": {"email": 1}, "unique": True},
        "name": {"key": {"different": 1}, "name": "first"},
        "equivalent": {"key": {"field": 1}, "name": "another"},
    }[failure]
    with pytest.raises((DuplicateKeyError, OperationFailure)):
        col.create_indexes([{"key": {"field": 1}, "name": "first"}, bad])
    with TinyMongoClient(address, backend=backend) as other:
        assert {i["name"] for i in other.app.docs.list_indexes()} == {"_id_", "first"}
    assert col.create_index("field", name="first") == "first"


def test_failed_catalog_write_discards_staged_metadata(target, monkeypatch):
    client, address, backend = target
    col = client.app.docs
    col.insert_one({"_id": 1, "email": "same"})
    storage = AtomicJSONStorage if backend == "json" else MemoryStorage
    original = storage.write_table
    calls = []

    def fail(self, name, *args, **kwargs):
        calls.append(name)
        raise OSError("catalog unavailable")

    monkeypatch.setattr(storage, "write_table", fail)
    with pytest.raises(OSError, match="catalog unavailable"):
        col.create_indexes([{"key": {"email": 1}, "unique": True}])
    assert calls == [INDEX_CATALOG_TABLE]
    assert col._index_specs == {}
    monkeypatch.setattr(storage, "write_table", original)
    col.insert_one({"_id": 2, "email": "same"})
    with TinyMongoClient(address, backend=backend) as other:
        assert [i["name"] for i in other.app.docs.list_indexes()] == ["_id_"]
    assert col.create_indexes([{"key": {"other": 1}}]) == ["other_1"]


def test_legacy_promotion_flushes_prior_indexes(target):
    client, address, backend = target
    col = client.app.docs
    legacy = IndexSpec("a", name="legacy", metadata_version=1)
    document = col._index_document(legacy)
    document["spec"] = {
        "v": 1,
        "key": [["a", 1]],
        "unique": False,
        "name": "legacy",
    }
    col.parent.tinydb.table(INDEX_CATALOG_TABLE).insert(document)
    assert col.create_indexes(
        [
            {"key": {"first": 1}},
            {"key": {"a": 1, "b": 1}, "name": "legacy"},
            {"key": {"last": 1}},
        ]
    ) == ["first_1", "legacy", "last_1"]
    with TinyMongoClient(address, backend=backend) as other:
        indexes = {i["name"]: i for i in other.app.docs.list_indexes()}
        assert set(indexes) == {"_id_", "first_1", "legacy", "last_1"}
        assert indexes["legacy"]["key"] == [("a", 1), ("b", 1)]
