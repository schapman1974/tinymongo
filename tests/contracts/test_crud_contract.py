"""Application-focused CRUD contracts shared by every backend."""

import pytest

from .support import observe


pytestmark = pytest.mark.contract


def test_insert_query_sort_and_count(contract_target):
    collection = contract_target.collection

    result = collection.insert_many(
        [
            {"_id": 1, "name": "Ada", "score": 7, "tags": ["math"]},
            {"_id": 2, "name": "Grace", "score": 9, "tags": ["code"]},
            {"_id": 3, "name": "Lin", "score": 8, "tags": ["systems"]},
        ]
    )

    assert result.inserted_ids == [1, 2, 3]
    rows = list(collection.find({"score": {"$gte": 8}}).sort("score", -1))
    assert rows == [
        {"_id": 2, "name": "Grace", "score": 9, "tags": ["code"]},
        {"_id": 3, "name": "Lin", "score": 8, "tags": ["systems"]},
    ]
    assert collection.count_documents({"score": {"$gte": 8}}) == 2


def test_array_in_query_contract(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "tags": ["math"]},
            {"_id": 2, "tags": ["code"]},
            {"_id": 3, "tags": ["systems"]},
        ]
    )

    assert collection.count_documents({"tags": {"$in": ["math", "code"]}}) == 2


def test_update_and_result_metadata(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "team": "compiler", "score": 7},
            {"_id": 2, "team": "compiler", "score": 9},
        ]
    )

    updated = collection.update_one({"_id": 1}, {"$inc": {"score": 2}})
    unchanged = collection.update_one({"_id": 1}, {"$set": {"score": 9}})
    many = collection.update_many({"team": "compiler"}, {"$set": {"active": True}})

    assert (updated.matched_count, updated.modified_count) == (1, 1)
    assert (unchanged.matched_count, unchanged.modified_count) == (1, 0)
    assert (many.matched_count, many.modified_count) == (2, 2)
    assert list(collection.find({"active": True}).sort("_id", 1)) == [
        {"_id": 1, "team": "compiler", "score": 9, "active": True},
        {"_id": 2, "team": "compiler", "score": 9, "active": True},
    ]


def test_replace_upsert_and_delete_metadata(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": 1, "name": "Ada", "active": False})

    replaced = collection.replace_one(
        {"_id": 1}, {"_id": 1, "name": "Ada", "active": True}
    )
    upserted = collection.update_one(
        {"_id": 2}, {"$set": {"name": "Grace", "active": True}}, upsert=True
    )
    deleted = collection.delete_one({"_id": 1})
    missing = collection.delete_one({"_id": 99})

    assert (replaced.matched_count, replaced.modified_count) == (1, 1)
    assert upserted.upserted_id == 2
    assert (deleted.deleted_count, missing.deleted_count) == (1, 0)
    assert collection.find_one({"_id": 2}) == {
        "_id": 2,
        "name": "Grace",
        "active": True,
    }


def test_duplicate_id_has_a_shared_error_category(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": 1, "name": "first"})

    outcome = observe(lambda: collection.insert_one({"_id": 1, "name": "duplicate"}))

    assert outcome.error == "duplicate_key"
    assert collection.count_documents({"_id": 1}) == 1


def test_sort_skip_and_limit_contract(contract_target):
    collection = contract_target.collection
    collection.insert_many([{"_id": number, "score": number} for number in range(1, 6)])

    rows = list(collection.find({}, sort=[("score", 1)], skip=1, limit=2))

    assert rows == [
        {"_id": 2, "score": 2},
        {"_id": 3, "score": 3},
    ]
