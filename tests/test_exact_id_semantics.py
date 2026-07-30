"""Regression tests for exact, BSON-aware document identity."""

from collections import UserDict

import pytest

import tinymongo as tm
from tinymongo.errors import BulkWriteError, DuplicateKeyError
from tinymongo.table_backends import matches_filter
from tinymongo.tinymongo import _cached_value_matches


def test_embedded_document_equality_preserves_field_order(tmp_path):
    collection = tm.TinyMongoClient(tmp_path).db.items
    collection.insert_one(
        {
            "_id": "ordered",
            "value": {"first": 1, "second": 2},
        }
    )

    assert collection.find_one({"value": {"first": 1.0, "second": 2.0}}) is not None
    assert collection.find_one({"value": {"second": 2, "first": 1}}) is None
    assert not matches_filter(
        {"value": {"first": 1, "second": 2}},
        {"value": {"second": 2, "first": 1}},
    )


def test_reordered_embedded_document_ids_remain_distinct(tmp_path):
    collection = tm.TinyMongoClient(tmp_path).db.items
    first_id = {"first": 1, "second": 2}
    reordered_id = {"second": 2, "first": 1}

    result = collection.insert_many(
        [
            {"_id": first_id, "label": "first"},
            {"_id": reordered_id, "label": "reordered"},
        ]
    )

    assert result.inserted_ids == [first_id, reordered_id]
    assert collection.find_one({"_id": first_id})["label"] == "first"
    assert collection.find_one({"_id": reordered_id})["label"] == "reordered"


def test_equivalent_embedded_document_ids_are_duplicates(tmp_path):
    collection = tm.TinyMongoClient(tmp_path).db.items

    with pytest.raises(BulkWriteError) as error:
        collection.insert_many(
            [
                {"_id": {"number": 1}, "label": "integer"},
                {"_id": {"number": 1.0}, "label": "float"},
            ]
        )

    assert error.value.details["nInserted"] == 1


def test_exact_id_mutations_do_not_match_array_members(tmp_path):
    collection = tm.TinyMongoClient(tmp_path).db.items
    array_id = [1, 2]
    collection.insert_many(
        [
            {"_id": array_id, "label": "array"},
            {"_id": 1, "label": "scalar"},
        ]
    )

    update = collection.update_one({"_id": 1}, {"$set": {"updated": True}})
    assert update.matched_count == 1
    assert collection.find_one({"_id": 1})["updated"] is True
    assert "updated" not in collection.find_one({"_id": array_id})

    replacement = collection.replace_one({"_id": 1}, {"label": "replacement"})
    assert replacement.matched_count == 1
    assert collection.find_one({"_id": 1})["label"] == "replacement"
    assert collection.find_one({"_id": array_id})["label"] == "array"

    deletion = collection.delete_one({"_id": 1})
    assert deletion.deleted_count == 1
    assert collection.find_one({"_id": 1}) is None
    assert collection.find_one({"_id": array_id})["label"] == "array"


@pytest.mark.parametrize("backend", ["tinydb", "memory"])
def test_compound_and_logical_id_filters_keep_exact_identity(tmp_path, backend):
    client = tm.TinyMongoClient(str(tmp_path / backend), backend=backend)
    collection = client.db.items
    array_id = [1, 2]
    collection.insert_many(
        [
            {"_id": array_id, "score": 1},
            {"_id": 1, "score": 0},
        ]
    )

    compound = {"_id": 1, "score": {"$gt": 0}}
    logical = {"$and": [{"_id": 1}, {"score": {"$gt": 0}}]}
    assert collection.find_one(compound) is None
    assert collection.find_one(logical) is None
    assert collection.update_one(compound, {"$set": {"wrong": True}}).matched_count == 0

    exact_scalar = {"$and": [{"_id": 1}, {"score": 0}]}
    update = collection.update_one(exact_scalar, {"$set": {"updated": True}})
    assert update.matched_count == 1
    assert collection.find_one({"_id": 1})["updated"] is True
    assert "updated" not in collection.find_one({"_id": array_id})

    assert collection.delete_one({"_id": 1, "updated": True}).deleted_count == 1
    assert collection.find_one({"_id": 1}) is None
    assert collection.find_one({"_id": array_id}) is not None
    client.close()


@pytest.mark.parametrize(
    "backend",
    ["tinydb", "memory", "sqlite", "duckdb", "parquet"],
)
def test_mapping_subclasses_share_document_id_identity(tmp_path, backend):
    client = tm.TinyMongoClient(str(tmp_path / backend), backend=backend)
    collection = client.db.items
    document_id = {"z": 1, "a": 2}
    equivalent = UserDict([("z", 1.0), ("a", 2.0)])
    collection.insert_one({"_id": document_id, "label": "original"})

    assert collection.find_one({"_id": equivalent})["label"] == "original"
    with pytest.raises(DuplicateKeyError):
        collection.insert_one({"_id": equivalent, "label": "duplicate"})

    assert collection.count_documents({}) == 1
    client.close()


def test_exact_id_operator_queries_and_parser_write_paths(tmp_path):
    collection = tm.TinyMongoClient(tmp_path).db.items
    collection.insert_many(
        [
            {"_id": 1, "score": 1},
            {"_id": 2, "score": 2},
        ]
    )

    assert collection.find_one({"_id": {"$eq": 1}})["_id"] == 1
    assert collection.find_one({"_id": {"$in": [2]}})["_id"] == 2
    assert _cached_value_matches([1, 2], 2, None)

    update = collection.update_one(
        {"score": {"$gt": 1}},
        {"$set": {"updated": True}},
    )
    assert update.matched_count == 1
    assert collection.find_one({"_id": 2})["updated"] is True

    replacement = collection.replace_one(
        {"score": {"$lt": 2}},
        {"score": 10},
    )
    assert replacement.matched_count == 1
    assert collection.find_one({"_id": 1})["score"] == 10
