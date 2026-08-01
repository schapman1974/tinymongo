"""Focused edge and error tests for MongoDB-style array updates."""

import pytest

from tinymongo import TinyMongoClient
from tinymongo import tinymongo as core
from tinymongo.errors import TinyMongoError, TinyMongoNotSupportedError, WriteError


@pytest.mark.parametrize(
    "operand, message",
    [
        ({"$each": "value"}, r"\$each requires an array"),
        ({"$slice": 2}, "modifiers require \\$each"),
        ({"$each": [], "$unknown": 1}, "does not support modifier"),
        ({"$each": [], "$position": True}, r"\$position requires an integer"),
        ({"$each": [], "$position": 1.5}, r"\$position requires an integer"),
        ({"$each": [], "$slice": False}, r"\$slice requires an integer"),
        ({"$each": [], "$slice": 1.5}, r"\$slice requires an integer"),
        ({"$each": [], "$sort": 0}, r"\$sort requires"),
        ({"$each": [], "$sort": True}, r"\$sort requires"),
        ({"$each": [], "$sort": {}}, r"\$sort requires"),
        ({"$each": [], "$sort": {"score": 0}}, "invalid sort specification"),
        ({"$each": [], "$sort": {1: 1}}, "invalid sort specification"),
    ],
)
def test_push_rejects_malformed_modifier_documents(operand, message):
    original = {"_id": 1, "values": [1]}

    with pytest.raises(WriteError, match=message) as caught:
        core._apply_update_document(original, {"$push": {"values": operand}})

    assert isinstance(caught.value, TinyMongoError)
    assert caught.value.code == 2
    assert original == {"_id": 1, "values": [1]}


@pytest.mark.parametrize(
    "operand, message",
    [
        ({"$each": "value"}, r"\$each requires an array"),
        ({"$unknown": []}, "modifiers require \\$each|does not support modifier"),
        ({"$each": [], "$slice": 1}, "does not support modifier"),
    ],
)
def test_add_to_set_rejects_malformed_modifier_documents(operand, message):
    with pytest.raises(WriteError, match=message):
        core._apply_update_document(
            {"_id": 1, "values": []}, {"$addToSet": {"values": operand}}
        )


def test_push_sort_uses_recursive_whole_value_bson_order():
    updated = core._apply_update_document(
        {"_id": 1, "values": [[3, 0], "text", [], None]},
        {"$push": {"values": {"$each": [[1]], "$sort": 1.0}}},
    )
    descending = core._apply_update_document(
        {"_id": 1, "values": [{"score": 1}, {}, {"score": None}]},
        {
            "$push": {
                "values": {
                    "$each": [{"score": 2}],
                    "$sort": {"score": -1.0},
                }
            }
        },
    )

    assert updated["values"] == [None, "text", [], [1], [3, 0]]
    assert descending["values"][0] == {"score": 2}
    assert descending["values"][1] == {"score": 1}
    assert {tuple(value) for value in descending["values"][2:]} == {
        (),
        ("score",),
    }


def test_add_to_set_uses_bson_equality_and_creates_missing_empty_array():
    original = {
        "_id": 1,
        "values": [1, {"a": 1, "b": 2}, "duplicate", "duplicate"],
    }
    updated = core._apply_update_document(
        original,
        {
            "$addToSet": {
                "values": {
                    "$each": [1.0, True, {"b": 2, "a": 1}, True],
                }
            }
        },
    )
    missing = core._apply_update_document(
        {"_id": 2}, {"$addToSet": {"values": {"$each": []}}}
    )

    assert len(updated["values"]) == 6
    assert updated["values"][-2] is True
    assert list(updated["values"][-1]) == ["b", "a"]
    assert updated["values"].count("duplicate") == 2
    assert missing["values"] == []


def test_pull_supports_query_operators_documents_logical_queries_and_exact_arrays():
    original = {
        "_id": 1,
        "values": [
            [1, 2],
            [2, 1],
            {"kind": "keep", "score": 1},
            {"kind": "drop", "score": 8},
            {"kind": "also-drop", "score": 9},
            1,
        ],
    }

    exact = core._apply_update_document(original, {"$pull": {"values": [1, 2]}})
    queried = core._apply_update_document(
        exact,
        {
            "$pull": {
                "values": {
                    "$or": [
                        {"kind": "drop"},
                        {"score": {"$gte": 9}},
                    ]
                }
            }
        },
    )
    empty_query = core._apply_update_document(queried, {"$pull": {"values": {}}})
    explicit_equality = core._apply_update_document(
        {"_id": 2, "values": [[1, 2], [2, 3], 1]},
        {"$pull": {"values": {"$eq": 1}}},
    )
    type_bracketed = core._apply_update_document(
        {"_id": 3, "values": [True, 1, 2, "3"]},
        {"$pull": {"values": {"$gte": 1}}},
    )
    conjunction = core._apply_update_document(
        {
            "_id": 4,
            "values": [
                {"kind": "keep", "score": 9},
                {"kind": "keep", "score": 1},
                {"kind": "keep"},
                {"kind": "other", "score": 9},
            ],
        },
        {
            "$pull": {
                "values": {
                    "$and": [
                        {"kind": "keep"},
                        {"score": {"$gte": 8}},
                    ]
                }
            }
        },
    )
    negated_disjunction = core._apply_update_document(
        {
            "_id": 5,
            "values": [
                {"kind": "keep", "score": 1},
                {"kind": "other", "score": 9},
                {"kind": "other", "score": 1},
            ],
        },
        {
            "$pull": {
                "values": {
                    "$nor": [
                        {"kind": "keep"},
                        {"score": {"$gte": 8}},
                    ]
                }
            }
        },
    )
    nested_literal = core._apply_update_document(
        {
            "_id": 6,
            "values": [
                {"meta": {"score": 1}},
                {"meta": {"score": 2}},
            ],
        },
        {"$pull": {"values": {"meta": {"score": 1}}}},
    )

    assert exact["values"][0] == [2, 1]
    assert queried["values"] == [[2, 1], {"kind": "keep", "score": 1}, 1]
    assert empty_query["values"] == [[2, 1], 1]
    assert explicit_equality["values"] == [[2, 3]]
    assert type_bracketed["values"] == [True, "3"]
    assert conjunction["values"] == [
        {"kind": "keep", "score": 1},
        {"kind": "keep"},
        {"kind": "other", "score": 9},
    ]
    assert negated_disjunction["values"] == [
        {"kind": "keep", "score": 1},
        {"kind": "other", "score": 9},
    ]
    assert nested_literal["values"] == [{"meta": {"score": 2}}]


@pytest.mark.parametrize(
    "condition, message",
    [
        ({"$unknown": 1}, "does not support query operator"),
        ({"score": {"$unknown": 1}}, "does not support query operator"),
        ({"$or": {"score": 1}}, "requires an array of documents"),
        ({"$and": [1]}, "requires an array of documents"),
        ({"$or": []}, "requires an array of documents"),
        ({"$not": {"$unknown": 1}}, "does not support query operator"),
        ({"$all": [2]}, "does not support query operator"),
        ({"$not": {"$eq": 2}}, "does not support query operator"),
        ({"$options": "i"}, "does not support query operator"),
        ({"$regex": "["}, "does not support query operator"),
        ({"$gte": {}}, "requires a supported scalar value"),
        (
            {"$gte": 1, "$or": [{"score": 1}]},
            "document query operator",
        ),
        (
            {"$and": [{"$gte": 1}, {"$lte": 2}]},
            "document query operator",
        ),
        (
            {"$or": [{"$eq": 1}, {"$eq": 2}]},
            "document query operator",
        ),
        (
            {"a": {"$or": [{"b": 1}, {"b": 2}]}},
            "does not support query operator",
        ),
        (
            {"score": {"$gte": 1, "plain": 2}},
            "cannot mix field and document query operators",
        ),
    ],
)
def test_pull_rejects_unsupported_or_malformed_queries(condition, message):
    with pytest.raises(WriteError, match=message):
        core._apply_update_document(
            {"_id": 1, "values": [1]}, {"$pull": {"values": condition}}
        )


def test_invalid_updates_are_preflighted_and_atomic_even_without_a_match(tmp_path):
    client = TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.db.items
    original = {"_id": 1, "status": "original", "values": [1]}
    collection.insert_one(original)

    with pytest.raises(WriteError):
        collection.update_many(
            {},
            {
                "$set": {"status": "changed"},
                "$push": {"values": {"$each": [], "$unknown": 1}},
            },
        )
    with pytest.raises(TinyMongoNotSupportedError, match="Unsupported update operator"):
        collection.update_one({"_id": "missing"}, {"$unknown": {"value": 1}})
    with pytest.raises(WriteError, match="numeric values") as invalid_inc:
        collection.update_one({"_id": "missing"}, {"$inc": {"value": "one"}})
    assert invalid_inc.value.code == 14

    with pytest.raises(WriteError, match="numeric values") as invalid_target:
        collection.update_one({"_id": 1}, {"$inc": {"status": 1}})
    assert invalid_target.value.code == 14

    assert collection.find_one({"_id": 1}) == original
    client.close()
