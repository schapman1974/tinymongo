"""Array-update contracts shared by every backend and both APIs."""

import pytest

from tinymongo.errors import WriteError as TinyMongoWriteError

from .support import observe


pytestmark = pytest.mark.contract

_WRITE_ERRORS = (TinyMongoWriteError,)
try:
    from pymongo.errors import WriteError as PyMongoWriteError
except ImportError:  # pragma: no cover - development dependency guard
    PyMongoWriteError = ()  # type: ignore[assignment,misc]
else:
    _WRITE_ERRORS += (PyMongoWriteError,)


def test_push_each_slice_keeps_a_bounded_array(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": "capped", "pages": ["p1"]})

    result = collection.update_one(
        {"_id": "capped"},
        {"$push": {"pages": {"$each": ["p2", "p3"], "$slice": -2}}},
    )

    assert (result.matched_count, result.modified_count) == (1, 1)
    assert collection.find_one({"_id": "capped"})["pages"] == ["p2", "p3"]

    for number in range(4, 10):
        collection.update_one(
            {"_id": "capped"},
            {
                "$push": {
                    "pages": {
                        "$each": ["p{0}".format(number)],
                        "$slice": -2,
                    }
                }
            },
        )

    pages = collection.find_one({"_id": "capped"})["pages"]
    assert pages == ["p8", "p9"]
    assert len(pages) == 2


def test_push_modifiers_follow_mongodb_order_and_boundaries(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "position-positive", "values": [50, 60, 70, 100]},
            {
                "_id": "position-negative",
                "values": [50, 60, 20, 30, 70, 100],
            },
            {"_id": "position-high", "values": [2, 3]},
            {"_id": "position-low", "values": [2, 3]},
            {"_id": "sort-ascending", "values": [3, 1]},
            {"_id": "sort-descending", "values": [1, 3]},
            {"_id": "sort-bson-distinct", "values": [True, 1]},
            {
                "_id": "sort-document",
                "values": [
                    {"meta": {"score": 3}},
                    {"meta": {"score": 1}},
                ],
            },
            {
                "_id": "sort-compound",
                "values": [
                    {"group": "b", "score": 1},
                    {"group": "a", "score": 1},
                ],
            },
            {"_id": "slice-positive", "values": [1, 2, 3]},
            {"_id": "slice-negative", "values": [1, 2, 3]},
            {"_id": "slice-zero", "values": [1, 2, 3]},
            {"_id": "combined", "values": [3, 1]},
        ]
    )

    updates = {
        "position-positive": {"$each": [20, 30], "$position": 2},
        "position-negative": {"$each": [90, 80], "$position": -2},
        "position-high": {"$each": [4], "$position": 99},
        "position-low": {"$each": [0, 1], "$position": -99},
        "sort-ascending": {"$each": [2], "$sort": 1},
        "sort-descending": {"$each": [2], "$sort": -1},
        "sort-bson-distinct": {"$each": [], "$sort": 1},
        "sort-document": {
            "$each": [{"meta": {"score": 2}}],
            "$sort": {"meta.score": 1},
        },
        "sort-compound": {
            "$each": [{"group": "a", "score": 2}],
            "$sort": {"group": 1, "score": -1},
        },
        "slice-positive": {"$each": [4], "$slice": 2},
        "slice-negative": {"$each": [4], "$slice": -2},
        "slice-zero": {"$each": [4], "$slice": 0},
        # The deliberately scrambled keys prove semantic processing order.
        "combined": {"$slice": 3, "$sort": 1, "$each": [2, 0], "$position": 1},
    }
    results = {}
    for document_id, modifier in updates.items():
        results[document_id] = collection.update_one(
            {"_id": document_id}, {"$push": {"values": modifier}}
        )

    expected = {
        "position-positive": [50, 60, 20, 30, 70, 100],
        "position-negative": [50, 60, 20, 30, 90, 80, 70, 100],
        "position-high": [2, 3, 4],
        "position-low": [0, 1, 2, 3],
        "sort-ascending": [1, 2, 3],
        "sort-descending": [3, 2, 1],
        "sort-bson-distinct": [1, True],
        "sort-document": [
            {"meta": {"score": 1}},
            {"meta": {"score": 2}},
            {"meta": {"score": 3}},
        ],
        "sort-compound": [
            {"group": "a", "score": 2},
            {"group": "a", "score": 1},
            {"group": "b", "score": 1},
        ],
        "slice-positive": [1, 2],
        "slice-negative": [3, 4],
        "slice-zero": [],
        "combined": [0, 1, 2],
    }
    for document_id, values in expected.items():
        assert collection.find_one({"_id": document_id})["values"] == values
    assert results["sort-bson-distinct"].modified_count == 1


def test_plain_push_and_add_to_set_each_regressions(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "push", "values": []},
            {
                "_id": "set",
                "values": [1, {"a": 1, "b": 2}],
            },
        ]
    )

    collection.update_one({"_id": "push"}, {"$push": {"values": "plain"}})
    collection.update_one({"_id": "push"}, {"$push": {"values": {"name": "document"}}})
    collection.update_one({"_id": "push"}, {"$push": {"values": ["nested"]}})

    modifier = {
        "$each": [
            1.0,
            True,
            2,
            2,
            {"a": 1, "b": 2},
            {"b": 2, "a": 1},
        ]
    }
    changed = collection.update_one({"_id": "set"}, {"$addToSet": {"values": modifier}})
    unchanged = collection.update_one(
        {"_id": "set"}, {"$addToSet": {"values": modifier}}
    )

    assert collection.find_one({"_id": "push"})["values"] == [
        "plain",
        {"name": "document"},
        ["nested"],
    ]
    values = collection.find_one({"_id": "set"})["values"]
    assert values[:4] == [1, {"a": 1, "b": 2}, True, 2]
    assert list(values[4].items()) == [("b", 2), ("a", 1)]
    assert (changed.modified_count, unchanged.modified_count) == (1, 0)


def test_invalid_array_modifiers_fail_atomically(contract_target):
    collection = contract_target.collection
    original = {"_id": "invalid", "values": [1], "status": "original"}
    collection.insert_one(original)

    malformed_each = observe(
        lambda: collection.update_one(
            {"_id": "invalid"},
            {"$push": {"values": {"$each": "not-an-array"}}},
        )
    )
    unknown_modifier = observe(
        lambda: collection.update_one(
            {"_id": "invalid"},
            {
                "$set": {"status": "changed"},
                "$push": {"values": {"$each": [], "$unknown": 1}},
            },
        )
    )
    unknown_operator = observe(
        lambda: collection.update_one({"_id": "invalid"}, {"$unknown": {"values": 1}})
    )

    assert malformed_each.error is not None
    assert unknown_modifier.error is not None
    assert unknown_operator.error is not None
    assert collection.find_one({"_id": "invalid"}) == original


def test_pull_accepts_literal_and_query_operands(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "range", "values": [5, 8, 10]},
            {"_id": "literal", "values": [1, [1, 2], [2, 3]]},
            {
                "_id": "documents",
                "values": [
                    {"kind": "x", "meta": {"score": 5}},
                    {"kind": "x", "meta": {"score": 9}},
                    {"kind": "y", "meta": {"score": 10}},
                    7,
                ],
            },
        ]
    )

    collection.update_one({"_id": "range"}, {"$pull": {"values": {"$gte": 8}}})
    collection.update_one({"_id": "literal"}, {"$pull": {"values": 1}})
    collection.update_one(
        {"_id": "documents"},
        {"$pull": {"values": {"kind": "x", "meta.score": {"$gte": 8}}}},
    )

    assert collection.find_one({"_id": "range"})["values"] == [5]
    assert collection.find_one({"_id": "literal"})["values"] == [
        [1, 2],
        [2, 3],
    ]
    assert collection.find_one({"_id": "documents"})["values"] == [
        {"kind": "x", "meta": {"score": 5}},
        {"kind": "y", "meta": {"score": 10}},
        7,
    ]


def test_pull_query_operator_family_matches_mongodb(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "in", "values": ["alpha", "beta", "gamma"]},
            {"_id": "nin", "values": ["alpha", "beta", "gamma"]},
            {"_id": "regex", "values": ["alpha", "ALPHA", "beta"]},
            {"_id": "elem", "values": [[1, 3], [1, 2], ["x"], 1]},
        ]
    )

    collection.update_one(
        {"_id": "in"},
        {"$pull": {"values": {"$in": ["alpha", "gamma"]}}},
    )
    collection.update_one(
        {"_id": "nin"},
        {"$pull": {"values": {"$nin": ["alpha", "gamma"]}}},
    )
    collection.update_one(
        {"_id": "regex"},
        {"$pull": {"values": {"$regex": "^alpha$", "$options": "i"}}},
    )
    collection.update_one(
        {"_id": "elem"},
        {"$pull": {"values": {"$elemMatch": {"$gt": 2}}}},
    )

    assert collection.find_one({"_id": "in"})["values"] == ["beta"]
    assert collection.find_one({"_id": "nin"})["values"] == ["alpha", "gamma"]
    assert collection.find_one({"_id": "regex"})["values"] == ["beta"]
    assert collection.find_one({"_id": "elem"})["values"] == [
        [1, 2],
        ["x"],
        1,
    ]


def test_pull_missing_fields_are_true_noops(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "present", "values": ["remove", "keep"]},
            {"_id": "missing", "other": 1},
            {"_id": "empty", "values": []},
        ]
    )

    result = collection.update_many({}, {"$pull": {"values": "remove"}})

    assert (result.matched_count, result.modified_count) == (3, 1)
    assert collection.find_one({"_id": "present"})["values"] == ["keep"]
    assert "values" not in collection.find_one({"_id": "missing"})
    assert collection.count_documents({"values": {"$exists": False}}) == 1


def test_pull_all_uses_literal_bson_equality(contract_target):
    collection = contract_target.collection
    original = {
        "_id": "values",
        "values": [
            True,
            1,
            1.0,
            [1, 2],
            [2, 1],
            {"a": 1, "b": 2},
            {"b": 2, "a": 1},
        ],
    }
    collection.insert_many([original, {"_id": "missing", "other": 1}])

    changed = collection.update_one(
        {"_id": "values"},
        {"$pullAll": {"values": [1.0, [1, 2], {"a": 1, "b": 2}]}},
    )
    unchanged = collection.update_one({"_id": "values"}, {"$pullAll": {"values": []}})
    missing = collection.update_one({"_id": "missing"}, {"$pullAll": {"values": [1]}})

    assert (changed.modified_count, unchanged.modified_count) == (1, 0)
    assert missing.modified_count == 0
    assert collection.find_one({"_id": "values"})["values"] == [
        True,
        [2, 1],
        {"b": 2, "a": 1},
    ]
    assert "values" not in collection.find_one({"_id": "missing"})


@pytest.mark.parametrize(
    ("operator", "operand"),
    [
        ("$pull", "remove"),
        ("$push", "append"),
        ("$pullAll", ["remove"]),
    ],
)
def test_array_updates_reject_non_array_targets(contract_target, operator, operand):
    collection = contract_target.collection
    collection.insert_one({"_id": "scalar", "values": "not-an-array"})

    with pytest.raises(_WRITE_ERRORS) as caught:
        collection.update_one(
            {"_id": "scalar"},
            {operator: {"values": operand}},
        )

    assert caught.value.code == 2
    assert collection.find_one({"_id": "scalar"})["values"] == "not-an-array"


def test_pull_not_and_pull_all_operand_errors_match_mongodb(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": "invalid", "values": [1, 2]})

    with pytest.raises(_WRITE_ERRORS) as pull_not:
        collection.update_one(
            {"_id": "invalid"},
            {"$pull": {"values": {"$not": {"$eq": 2}}}},
        )
    with pytest.raises(_WRITE_ERRORS) as pull_all:
        collection.update_one(
            {"_id": "invalid"},
            {"$pullAll": {"values": "not-an-array-operand"}},
        )

    assert (pull_not.value.code, pull_all.value.code) == (2, 2)
    assert collection.find_one({"_id": "invalid"})["values"] == [1, 2]
