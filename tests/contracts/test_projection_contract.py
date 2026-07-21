"""Field projection contracts shared by TinyMongo and MongoDB."""

import pytest

from .support import observe


pytestmark = pytest.mark.contract


def _seed(collection):
    collection.insert_many(
        [
            {
                "_id": 1,
                "name": "Ada",
                "secret": "x",
                "score": 7,
                "profile": {"email": "ada@example.com", "age": 36},
                "items": [{"sku": "a", "qty": 1}, {"qty": 2}, {}],
            },
            {
                "_id": 2,
                "name": "Grace",
                "score": 9,
                "profile": {"age": 40},
                "items": [],
            },
        ]
    )


def test_inclusion_exclusion_and_id_projection_contract(contract_target):
    collection = contract_target.collection
    _seed(collection)

    assert list(collection.find({}, {"name": 1}).sort("_id", 1)) == [
        {"_id": 1, "name": "Ada"},
        {"_id": 2, "name": "Grace"},
    ]
    assert collection.find_one({"_id": 1}, {"name": 2, "_id": 0}) == {"name": "Ada"}
    assert collection.find_one({"_id": 1}, {"secret": 0, "_id": 1}) == {
        "_id": 1,
        "name": "Ada",
        "score": 7,
        "profile": {"email": "ada@example.com", "age": 36},
        "items": [{"sku": "a", "qty": 1}, {"qty": 2}, {}],
    }
    assert collection.find_one({"_id": 1}, {"_id.missing": 1}) == {}


def test_nested_and_array_projection_contract(contract_target):
    collection = contract_target.collection
    _seed(collection)

    assert list(collection.find({}, {"profile.email": 1}).sort("_id", 1)) == [
        {"_id": 1, "profile": {"email": "ada@example.com"}},
        {"_id": 2, "profile": {}},
    ]
    assert collection.find_one({"_id": 1}, {"items.sku": 1}) == {
        "_id": 1,
        "items": [{"sku": "a"}, {}, {}],
    }
    assert collection.find_one({"_id": 1}, {"items.qty": 0})["items"] == [
        {"sku": "a"},
        {},
        {},
    ]


def test_projection_uses_unprojected_values_for_filter_and_sort(contract_target):
    collection = contract_target.collection
    _seed(collection)

    assert list(
        collection.find({"score": {"$gte": 7}}, {"name": 1}).sort("score", -1)
    ) == [
        {"_id": 2, "name": "Grace"},
        {"_id": 1, "name": "Ada"},
    ]


def test_empty_and_invalid_projection_contract(contract_target):
    collection = contract_target.collection
    _seed(collection)
    full = collection.find_one({"_id": 1})

    assert collection.find_one({"_id": 1}, {}) == full
    assert collection.find_one({"_id": 1}, []) == full
    assert observe(
        lambda: list(collection.find({}, {"name": 1, "secret": 0}))
    ).error == ("operation_failure")
    assert (
        observe(
            lambda: list(collection.find({}, {"profile": 1, "profile.email": 1}))
        ).error
        == "operation_failure"
    )
    assert (
        observe(lambda: list(collection.find({}, {"_id": 1, "_id.value": 1}))).error
        == "operation_failure"
    )
    assert (
        observe(
            lambda: list(
                collection.find({}, {"profile": {"email": 1}, "profile.email": 1})
            )
        ).error
        == "operation_failure"
    )
