import asyncio
import builtins
from collections import UserDict
from contextlib import contextmanager
import runpy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import tinymongo
from tinymongo.asyncio import AsyncTinyMongoClient
from tinymongo.errors import BulkWriteError, DuplicateKeyError
from tinymongo.tinymongo import TinyMongoCollection


BACKENDS = ("memory", "tinydb", "sqlite", "duckdb", "parquet")


def _client(tmp_path, backend):
    folder = (
        "memory://insert-many-{0}".format(uuid4().hex)
        if backend == "memory"
        else str(tmp_path / backend)
    )
    return tinymongo.TinyMongoClient(folder, backend=backend)


@pytest.mark.parametrize("backend", BACKENDS)
def test_ordered_insert_many_stops_at_first_duplicate(tmp_path, backend):
    client = _client(tmp_path, backend)
    collection = client.app.items
    collection.insert_one({"_id": "seed"})
    documents = [{"name": "first"}, {"_id": "seed"}, {"name": "last"}]

    with pytest.raises(BulkWriteError) as caught:
        collection.insert_many(documents)

    details = caught.value.details
    assert details["nInserted"] == 1
    assert details["writeConcernErrors"] == []
    assert len(details["writeErrors"]) == 1
    assert details["writeErrors"][0] == {
        "index": 1,
        "code": 11000,
        "errmsg": details["writeErrors"][0]["errmsg"],
        "keyPattern": {"_id": 1},
        "keyValue": {"_id": "seed"},
        "op": documents[1],
    }
    assert "index: _id_" in details["writeErrors"][0]["errmsg"]
    assert details["writeErrors"][0]["op"] is documents[1]
    assert all("_id" in document for document in documents)
    assert {document["_id"] for document in collection.find({})} == {
        "seed",
        documents[0]["_id"],
    }
    client.close()


@pytest.mark.parametrize("backend", BACKENDS)
def test_unordered_insert_many_continues_and_reports_every_duplicate(tmp_path, backend):
    client = _client(tmp_path, backend)
    collection = client.app.items
    collection.insert_one({"_id": "seed"})
    documents = [
        {"_id": "first"},
        {"_id": "seed"},
        {"_id": "seed"},
        {"_id": "last"},
    ]

    with pytest.raises(BulkWriteError) as caught:
        collection.insert_many(documents, ordered=False)

    details = caught.value.details
    assert details["nInserted"] == 2
    assert [error["index"] for error in details["writeErrors"]] == [1, 2]
    assert [error["code"] for error in details["writeErrors"]] == [11000, 11000]
    assert {document["_id"] for document in collection.find({})} == {
        "seed",
        "first",
        "last",
    }
    client.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("ordered", "expected_inserted"),
    [(True, {"seed", "first"}), (False, {"seed", "first", "last"})],
)
def test_insert_many_applies_ordering_to_unique_index_failures(
    tmp_path, backend, ordered, expected_inserted
):
    client = _client(tmp_path, backend)
    collection = client.app.users
    collection.create_index("email", unique=True)
    collection.insert_one({"_id": "seed", "email": "taken@example.com"})
    documents = [
        {"_id": "first", "email": "first@example.com"},
        {"_id": "duplicate", "email": "taken@example.com"},
        {"_id": "last", "email": "last@example.com"},
    ]

    with pytest.raises(BulkWriteError) as caught:
        collection.insert_many(documents, ordered=ordered)

    details = caught.value.details
    assert details["nInserted"] == len(expected_inserted) - 1
    assert details["writeErrors"][0]["index"] == 1
    assert details["writeErrors"][0]["keyPattern"] == {"email": 1}
    assert details["writeErrors"][0]["keyValue"] == {"email": "taken@example.com"}
    assert "index: email_1" in details["writeErrors"][0]["errmsg"]
    assert {document["_id"] for document in collection.find({})} == expected_inserted
    client.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("ordered", [True, False])
def test_insert_many_serialization_preflight_is_all_or_nothing(
    tmp_path, backend, ordered
):
    client = _client(tmp_path, backend)
    collection = client.app.items
    documents = [
        {"name": "first"},
        {"name": "invalid", "value": object()},
        {"name": "last"},
    ]

    with pytest.raises(TypeError):
        collection.insert_many(documents, ordered=ordered)

    assert all("_id" in document for document in documents)
    assert collection.count_documents({}) == 0
    client.close()


def test_async_insert_many_uses_the_same_unordered_semantics():
    async def scenario():
        client = AsyncTinyMongoClient(
            "memory://async-insert-many-{0}".format(uuid4().hex),
            backend="memory",
        )
        collection = client.app.items
        await collection.insert_one({"_id": "seed"})
        documents = [
            {"_id": "first"},
            {"_id": "seed"},
            {"_id": "last"},
        ]

        with pytest.raises(BulkWriteError) as caught:
            await collection.insert_many(documents, ordered=False)

        assert caught.value.details["nInserted"] == 2
        assert [error["index"] for error in caught.value.details["writeErrors"]] == [1]
        assert {
            document["_id"] for document in await collection.find({}).to_list()
        } == {"seed", "first", "last"}
        await client.close()

    asyncio.run(scenario())


def test_insert_many_validates_batch_and_ordering_arguments(tmp_path):
    client = _client(tmp_path, "sqlite")
    collection = client.app.items

    with pytest.raises(TypeError, match="non-empty"):
        collection.insert_many([])
    with pytest.raises(TypeError, match="ordered"):
        collection.insert_many([{"_id": 1}], ordered=1)
    with pytest.raises(TypeError, match="each document"):
        collection.insert_many([{"_id": 1}, None])

    assert collection.count_documents({}) == 0
    client.close()


@pytest.mark.parametrize(
    "documents",
    [
        ({"_id": "tuple-one"}, {"_id": "tuple-two"}),
        iter(({"_id": "generator-one"}, {"_id": "generator-two"})),
    ],
)
def test_insert_many_accepts_document_iterables(tmp_path, documents):
    client = _client(tmp_path, "sqlite")
    collection = client.app.items

    result = collection.insert_many(documents)

    assert result.inserted_ids == [
        document["_id"] for document in collection.find({}).sort("_id", 1)
    ]
    client.close()


def test_insert_many_accepts_mutable_mapping_documents(tmp_path):
    client = _client(tmp_path, "sqlite")
    collection = client.app.items
    document = UserDict({"label": "mapping"})

    result = collection.insert_many((document,))

    assert result.inserted_ids == [document["_id"]]
    assert collection.find_one({"_id": document["_id"]})["label"] == "mapping"
    client.close()


@pytest.mark.parametrize("backend", BACKENDS)
def test_embedded_document_ids_keep_boolean_and_number_identity_distinct(
    tmp_path, backend
):
    client = _client(tmp_path, backend)
    collection = client.app.items

    result = collection.insert_many(
        [
            {"_id": {"value": True}, "label": "boolean"},
            {"_id": {"value": 1}, "label": "number"},
        ]
    )

    assert result.inserted_ids == [{"value": True}, {"value": 1}]
    assert sorted(document["label"] for document in collection.find({})) == [
        "boolean",
        "number",
    ]
    client.close()


@pytest.mark.parametrize("backend", BACKENDS)
def test_explicit_none_id_is_preserved_and_remains_unique(tmp_path, backend):
    client = _client(tmp_path, backend)
    singles = client.app.singles
    document = {"_id": None, "label": "explicit"}

    result = singles.insert_one(document)

    assert result.inserted_id is None
    assert document["_id"] is None
    assert singles.find_one({"_id": None})["label"] == "explicit"
    with pytest.raises(DuplicateKeyError):
        singles.insert_one({"_id": None, "label": "duplicate"})

    batch = client.app.batch
    batch_documents = [{"_id": None}, {"_id": "other"}]
    batch_result = batch.insert_many(batch_documents)
    assert batch_result.inserted_ids == [None, "other"]
    with pytest.raises(BulkWriteError) as caught:
        batch.insert_many([{"_id": None}], ordered=False)
    assert caught.value.details["nInserted"] == 0
    client.close()


def test_tinydb_physical_id_operations_do_not_use_array_membership(tmp_path):
    client = _client(tmp_path, "tinydb")
    collection = client.app.items
    collection.insert_many(
        [
            {"_id": 1, "kind": "scalar"},
            {"_id": [1, 2], "kind": "array"},
        ]
    )

    collection.update_one({"_id": 1}, {"$set": {"updated": True}})
    assert collection.find_one({"_id": 1})["updated"] is True
    assert collection.find_one({"_id": [1, 2]}).get("updated") is None

    collection.delete_one({"_id": 1})
    assert collection.find_one({"_id": 1}) is None
    assert collection.find_one({"_id": [1, 2]})["kind"] == "array"
    client.close()


def test_tinydb_rejects_tuple_and_list_ids_in_the_same_batch(tmp_path):
    client = _client(tmp_path, "tinydb")
    collection = client.app.items

    with pytest.raises(BulkWriteError) as caught:
        collection.insert_many([{"_id": (1, 2)}, {"_id": [1, 2]}])

    assert caught.value.details["nInserted"] == 1
    assert [document["_id"] for document in collection.find({})] == [[1, 2]]
    client.close()


def test_insert_many_all_duplicate_batch_reports_zero_inserts(tmp_path):
    client = _client(tmp_path, "tinydb")
    collection = client.app.items
    collection.insert_one({"_id": "seed"})

    with pytest.raises(BulkWriteError) as caught:
        collection.insert_many([{"_id": "seed"}], ordered=False)

    assert caught.value.details["nInserted"] == 0
    assert caught.value.details["writeErrors"][0]["index"] == 0
    assert collection.count_documents({}) == 1
    client.close()


def test_insert_many_unique_missing_field_reports_null_key(tmp_path):
    client = _client(tmp_path, "sqlite")
    collection = client.app.items
    collection.create_index("email", unique=True)
    collection.insert_one({"_id": "seed"})

    with pytest.raises(BulkWriteError) as caught:
        collection.insert_many([{"_id": "duplicate-missing"}])

    assert caught.value.details["writeErrors"][0]["keyValue"] == {"email": None}
    assert collection.count_documents({}) == 1
    client.close()


def test_bulk_write_error_remains_available_without_pymongo(monkeypatch):
    original_import = builtins.__import__

    def import_without_pymongo(name, *args, **kwargs):
        if name == "pymongo" or name.startswith("pymongo."):
            raise ImportError("pymongo intentionally unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_pymongo)
    namespace = runpy.run_path(
        str(Path(tinymongo.__file__).with_name("errors.py")),
        run_name="tinymongo_errors_without_pymongo",
    )
    error = namespace["BulkWriteError"](
        {
            "writeErrors": [{"index": 0, "code": 11000}],
            "writeConcernErrors": [],
            "nInserted": 0,
        }
    )

    assert error.code == 65
    assert error.details["writeErrors"][0]["index"] == 0
    assert error.timeout is False


def test_native_duplicate_race_is_replanned_as_a_bulk_write_error():
    class RacingEngine:
        def __init__(self):
            self.documents = []
            self.attempts = 0

        @contextmanager
        def _write_lock(self):
            yield

        def find(self, _collection, _filter):
            return [dict(document) for document in self.documents]

        def get_index_specs(self, _collection):
            return []

        def insert_many(
            self,
            _collection,
            documents,
            bypass_document_validation=False,
        ):
            self.attempts += 1
            if self.attempts == 1:
                self.documents.append({"_id": "raced"})
                raise DuplicateKeyError("concurrent duplicate")
            self.documents.extend(dict(document) for document in documents)
            return list(range(len(documents)))

    engine = RacingEngine()
    collection = TinyMongoCollection(
        "items",
        SimpleNamespace(name="app", engine=engine),
    )
    documents = [
        {"_id": "first"},
        {"_id": "raced"},
        {"_id": "last"},
    ]

    with pytest.raises(BulkWriteError) as caught:
        collection.insert_many(documents, ordered=False)

    assert engine.attempts == 2
    assert caught.value.details["nInserted"] == 2
    assert [error["index"] for error in caught.value.details["writeErrors"]] == [1]
    assert {document["_id"] for document in engine.documents} == {
        "first",
        "raced",
        "last",
    }


@pytest.mark.parametrize("ordered", [True, False])
def test_persistent_native_duplicate_still_uses_bulk_write_error_shape(ordered):
    class RejectingEngine:
        def __init__(self):
            self.attempts = 0

        @contextmanager
        def _write_lock(self):
            yield

        def find(self, _collection, _filter):
            return []

        def get_index_specs(self, _collection):
            return []

        def insert_many(
            self,
            _collection,
            _documents,
            bypass_document_validation=False,
        ):
            self.attempts += 1
            raise DuplicateKeyError("persistent native duplicate")

    engine = RejectingEngine()
    collection = TinyMongoCollection(
        "items",
        SimpleNamespace(name="app", engine=engine),
    )
    document = {"_id": "conflict"}

    with pytest.raises(BulkWriteError) as caught:
        collection.insert_many([document], ordered=ordered)

    assert engine.attempts == 3
    assert caught.value.details["nInserted"] == 0
    assert caught.value.details["writeErrors"][0]["index"] == 0
    assert caught.value.details["writeErrors"][0]["op"] is document
    assert "persistent native duplicate" in (
        caught.value.details["writeErrors"][0]["errmsg"]
    )
