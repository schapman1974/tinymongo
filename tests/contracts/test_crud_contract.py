"""Application-focused CRUD contracts shared by every backend."""

import pytest

from tinymongo.errors import WriteError as TinyMongoWriteError

from .support import observe


pytestmark = pytest.mark.contract

_WRITE_ERRORS = (TinyMongoWriteError,)
try:
    from pymongo.errors import WriteError as PyMongoWriteError
except ImportError:  # pragma: no cover - optional dependency guard
    pass
else:
    _WRITE_ERRORS += (PyMongoWriteError,)


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


def test_unset_removes_top_level_and_nested_fields(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": "unset",
            "remove_me": True,
            "nested": {"keep": 1, "remove_me": 2},
        }
    )

    result = collection.update_one(
        {"_id": "unset"},
        {"$unset": {"remove_me": "", "nested.remove_me": ""}},
    )

    assert (result.matched_count, result.modified_count) == (1, 1)
    assert collection.find_one({"_id": "unset"}) == {
        "_id": "unset",
        "nested": {"keep": 1},
    }


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


@pytest.mark.parametrize(
    ("query", "pinned_id"),
    [
        ({"_id": 77}, 77),
        ({"_id": {"$eq": "pinned-key"}}, "pinned-key"),
        ({"_id": 88, "state": "missing"}, 88),
        ({"_id": None}, None),
    ],
)
def test_replace_upsert_preserves_equality_bound_id(contract_target, query, pinned_id):
    collection = contract_target.collection
    replacement = {"value": 7}

    result = collection.replace_one(query, replacement, upsert=True)

    assert replacement == {"value": 7}
    assert result.raw_result == {
        "n": 1,
        "nModified": 0,
        "ok": 1.0,
        "updatedExisting": False,
        "upserted": pinned_id,
    }
    expected_matched = 1 if pinned_id is None else 0
    assert (result.matched_count, result.modified_count) == (expected_matched, 0)
    assert result.did_upsert is True
    assert result.upserted_id == pinned_id
    assert collection.find_one({"_id": pinned_id}) == {
        "_id": pinned_id,
        "value": 7,
    }


def test_replace_upsert_stores_id_first(contract_target):
    collection = contract_target.collection
    replacement = {"value": 7}

    result = collection.replace_one({"_id": 77}, replacement, upsert=True)
    stored = collection.find_one({"_id": 77})

    assert result.upserted_id == 77
    assert list(stored) == ["_id", "value"]


def test_replace_upsert_accepts_bson_equal_filter_and_replacement_ids(
    contract_target,
):
    collection = contract_target.collection
    replacement = {"value": 7, "_id": 1.0}

    result = collection.replace_one(
        {"_id": 1},
        replacement,
        upsert=True,
    )
    stored = collection.find_one({"_id": 1})

    assert list(replacement) == ["value", "_id"]
    assert type(result.upserted_id) is float
    assert type(stored["_id"]) is float
    assert list(stored) == ["_id", "value"]
    assert stored["value"] == 7


def test_replace_upsert_rejects_conflicting_filter_and_replacement_ids(
    contract_target,
):
    collection = contract_target.collection

    with pytest.raises(_WRITE_ERRORS) as caught:
        collection.replace_one(
            {"_id": "filter-id"},
            {"_id": "replacement-id", "value": 7},
            upsert=True,
        )

    assert caught.value.code == 66
    assert collection.count_documents({}) == 0


def test_duplicate_id_has_a_shared_error_category(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": 1, "name": "first"})

    outcome = observe(lambda: collection.insert_one({"_id": 1, "name": "duplicate"}))

    assert outcome.error == "duplicate_key"
    assert collection.count_documents({"_id": 1}) == 1


def test_explicit_null_id_is_preserved_and_unique(contract_target):
    collection = contract_target.collection
    document = {"_id": None, "name": "explicit null"}

    result = collection.insert_one(document)
    duplicate = observe(
        lambda: collection.insert_one({"_id": None, "name": "duplicate"})
    )

    assert result.inserted_id is None
    assert document["_id"] is None
    assert duplicate.error == "duplicate_key"
    assert collection.find_one({"_id": None})["name"] == "explicit null"


def test_sort_skip_and_limit_contract(contract_target):
    collection = contract_target.collection
    collection.insert_many([{"_id": number, "score": number} for number in range(1, 6)])

    rows = list(collection.find({}, sort=[("score", 1)], skip=1, limit=2))

    assert rows == [
        {"_id": 2, "score": 2},
        {"_id": 3, "score": 3},
    ]
