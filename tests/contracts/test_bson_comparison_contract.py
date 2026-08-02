"""Issue #94 contracts for MongoDB-compatible BSON range comparison."""

from datetime import datetime, timezone
import re

import pytest

from tinymongo.errors import OperationFailure as TinyMongoOperationFailure
from tinymongo.errors import WriteError as TinyMongoWriteError


pytestmark = pytest.mark.contract
bson = pytest.importorskip("bson")
Binary = bson.Binary
ObjectId = bson.ObjectId
Regex = bson.Regex

_OPERATION_ERRORS = (TinyMongoOperationFailure,)
_WRITE_ERRORS = (TinyMongoWriteError,)
try:
    from pymongo.errors import OperationFailure as PyMongoOperationFailure
    from pymongo.errors import WriteError as PyMongoWriteError
except ImportError:  # pragma: no cover - optional dependency guard
    pass
else:
    _OPERATION_ERRORS += (PyMongoOperationFailure,)
    _WRITE_ERRORS += (PyMongoWriteError,)


def _ids(rows):
    return {document["_id"] for document in rows}


@pytest.mark.parametrize(
    ("documents", "operand", "expected"),
    [
        pytest.param(
            [
                {"_id": "lower", "value": -1},
                {"_id": "integer-equal", "value": 1},
                {"_id": "double-equal", "value": 1.0},
                {"_id": "decimal-equal", "value": bson.Decimal128("1.00")},
                {"_id": "higher", "value": 2},
                {"_id": "array-higher", "value": [2]},
                {"_id": "boolean", "value": True},
                {"_id": "string", "value": "2"},
            ],
            bson.Decimal128("1"),
            {
                "$gt": {"higher", "array-higher"},
                "$gte": {
                    "integer-equal",
                    "double-equal",
                    "decimal-equal",
                    "higher",
                    "array-higher",
                },
                "$lt": {"lower"},
                "$lte": {
                    "lower",
                    "integer-equal",
                    "double-equal",
                    "decimal-equal",
                },
            },
            id="numeric-family",
        ),
        pytest.param(
            [
                {"_id": "lower", "value": "a"},
                {"_id": "equal", "value": "b"},
                {"_id": "higher", "value": "c"},
                {"_id": "array-higher", "value": ["c"]},
                {"_id": "number", "value": 99},
                {"_id": "object", "value": {"value": "c"}},
            ],
            "b",
            {
                "$gt": {"higher", "array-higher"},
                "$gte": {"equal", "higher", "array-higher"},
                "$lt": {"lower"},
                "$lte": {"lower", "equal"},
            },
            id="string-family",
        ),
        pytest.param(
            [
                {"_id": "false", "value": False},
                {"_id": "true", "value": True},
                {"_id": "array-true", "value": [True]},
                {"_id": "zero", "value": 0},
                {"_id": "one", "value": 1},
            ],
            False,
            {
                "$gt": {"true", "array-true"},
                "$gte": {"false", "true", "array-true"},
                "$lt": set(),
                "$lte": {"false"},
            },
            id="boolean-family",
        ),
    ],
)
def test_generated_scalar_range_matrix(
    contract_target,
    documents,
    operand,
    expected,
):
    collection = contract_target.collection
    collection.insert_many(documents)

    assert {
        operator: _ids(collection.find({"value": {operator: operand}}))
        for operator in ("$gt", "$gte", "$lt", "$lte")
    } == expected


def test_null_range_queries_include_missing_and_array_members(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "missing"},
            {"_id": "null", "value": None},
            {"_id": "array-null", "value": [None, 1]},
            {"_id": "empty-array", "value": []},
            {"_id": "zero", "value": 0},
        ]
    )

    expected = {"missing", "null", "array-null"}
    assert _ids(collection.find({"value": {"$gte": None}})) == expected
    assert _ids(collection.find({"value": {"$lte": None}})) == expected
    assert _ids(collection.find({"value": {"$gt": None}})) == set()
    assert _ids(collection.find({"value": {"$lt": None}})) == set()


def test_array_range_queries_compare_whole_and_direct_nested_arrays(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "both", "value": [0, [2]]},
            {"_id": "opposite", "value": [[0], 0]},
            {"_id": "equal", "value": [1]},
            {"_id": "longer", "value": [1, 2]},
            {"_id": "higher", "value": [2]},
            {"_id": "empty", "value": []},
            {"_id": "scalar", "value": 2},
        ]
    )

    assert _ids(collection.find({"value": {"$gt": [1]}})) == {
        "both",
        "opposite",
        "longer",
        "higher",
    }
    assert _ids(collection.find({"value": {"$gte": [1]}})) == {
        "both",
        "opposite",
        "equal",
        "longer",
        "higher",
    }
    assert _ids(collection.find({"value": {"$lt": [1]}})) == {
        "both",
        "opposite",
        "empty",
    }
    assert _ids(collection.find({"value": {"$lte": [1]}})) == {
        "both",
        "opposite",
        "equal",
        "empty",
    }
    assert _ids(collection.find({"value": {"$elemMatch": {"$gt": [1]}}})) == {"both"}
    assert _ids(collection.find({"value": {"$elemMatch": {"$lt": [1]}}})) == {
        "opposite"
    }


def test_embedded_id_fields_use_normal_array_comparison_semantics(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "array-id", "value": [{"_id": [1, 2]}]},
            {"_id": "scalar-id", "value": [{"_id": 2}]},
            {"_id": "lower-id", "value": [{"_id": 1}]},
        ]
    )

    assert _ids(collection.find({"value": {"$elemMatch": {"_id": 2}}})) == {
        "array-id",
        "scalar-id",
    }
    assert _ids(collection.find({"value": {"$elemMatch": {"_id": {"$gt": 1}}}})) == {
        "array-id",
        "scalar-id",
    }


def test_object_range_queries_use_recursive_bson_field_order(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "empty", "value": {}},
            {"_id": "lower", "value": {"a": 0}},
            {"_id": "equal", "value": {"a": 1}},
            {"_id": "higher-value", "value": {"a": 2}},
            {"_id": "higher-key", "value": {"b": 0}},
            {"_id": "higher-type", "value": {"a": "text"}},
            {
                "_id": "array-members",
                "value": [{"a": 0}, {"a": 2}],
            },
        ]
    )

    assert _ids(collection.find({"value": {"$gt": {"a": 1}}})) == {
        "higher-value",
        "higher-key",
        "higher-type",
        "array-members",
    }
    assert _ids(collection.find({"value": {"$lt": {"a": 1}}})) == {
        "empty",
        "lower",
        "array-members",
    }


def test_scalar_ranges_share_bson_keys_and_dates_use_milliseconds(
    contract_target,
):
    collection = contract_target.collection
    boundary = datetime(2026, 8, 2, 12, 0, 0, 123100, tzinfo=timezone.utc)
    same_millisecond = boundary.replace(microsecond=123900)
    next_millisecond = boundary.replace(microsecond=124000)
    collection.insert_many(
        [
            {"_id": "binary-low", "binary": Binary(b"a", subtype=0)},
            {"_id": "binary-long", "binary": Binary(b"bb", subtype=0)},
            {"_id": "binary-subtype", "binary": Binary(b"a", subtype=128)},
            {
                "_id": "objectid-low",
                "objectid": ObjectId("000000000000000000000001"),
            },
            {
                "_id": "objectid-high",
                "objectid": ObjectId("000000000000000000000002"),
            },
            {"_id": "false", "flag": False},
            {"_id": "true", "flag": True},
            {"_id": "same-ms", "when": same_millisecond},
            {"_id": "next-ms", "when": next_millisecond},
        ]
    )

    assert _ids(collection.find({"binary": {"$gt": Binary(b"a", subtype=0)}})) == {
        "binary-long",
        "binary-subtype",
    }
    assert _ids(
        collection.find({"objectid": {"$gt": ObjectId("000000000000000000000001")}})
    ) == {"objectid-high"}
    assert _ids(collection.find({"flag": {"$gt": False}})) == {"true"}
    assert _ids(collection.find({"when": {"$gt": boundary}})) == {"next-ms"}
    assert _ids(collection.find({"when": {"$gte": boundary}})) == {
        "same-ms",
        "next-ms",
    }


def test_legacy_binary_subtype_two_uses_its_encoded_length(contract_target):
    collection = contract_target.collection
    values = [
        ("generic-one", Binary(b"a", subtype=0)),
        ("uuid-one", Binary(b"a", subtype=4)),
        ("generic-two", Binary(b"aa", subtype=0)),
        ("legacy-one", Binary(b"a", subtype=2)),
    ]
    collection.insert_many(
        {"_id": label, "value": value} for label, value in reversed(values)
    )

    # PyMongo cannot decode legacy subtype-2 Binary values from returned
    # documents, so project only the comparison result identifier.
    assert [row["_id"] for row in collection.find({}, {"_id": 1}).sort("value", 1)] == [
        label for label, _value in values
    ]
    assert _ids(
        collection.find(
            {"value": {"$gt": Binary(b"a", subtype=4)}},
            {"_id": 1},
        )
    ) == {
        "generic-two",
        "legacy-one",
    }
    assert _ids(
        collection.find(
            {"value": {"$gt": Binary(b"aa", subtype=0)}},
            {"_id": 1},
        )
    ) == {"legacy-one"}


@pytest.mark.parametrize(
    "expression",
    [Regex("a", ""), Regex("[", ""), re.compile("a")],
    ids=("bson-valid", "bson-malformed", "native"),
)
def test_regex_range_operands_are_rejected_with_code_2(
    contract_target,
    expression,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "first", "value": Regex("a", "")},
            {"_id": "second", "value": Regex("b", "")},
        ]
    )

    for operator in ("$gt", "$gte", "$lt", "$lte"):
        with pytest.raises(_OPERATION_ERRORS) as caught:
            list(collection.find({"value": {operator: expression}}))
        assert caught.value.code == 2


def test_nested_regex_range_values_are_compared_without_compilation(
    contract_target,
):
    collection = contract_target.collection
    invalid = Regex("[", "")
    collection.insert_many(
        [
            {"_id": "object-equal", "value": {"regex": invalid}},
            {"_id": "object-a", "value": {"regex": Regex("a", "")}},
            {"_id": "object-b", "value": {"regex": Regex("b", "")}},
            {"_id": "array-equal", "value": [invalid]},
            {"_id": "array-a", "value": [Regex("a", "")]},
            {"_id": "array-b", "value": [Regex("b", "")]},
        ]
    )

    assert _ids(collection.find({"value": {"$eq": {"regex": invalid}}})) == {
        "object-equal"
    }
    assert _ids(collection.find({"value": {"$gt": {"regex": invalid}}})) == {
        "object-a",
        "object-b",
    }
    assert _ids(collection.find({"value": {"$lte": {"regex": invalid}}})) == {
        "object-equal"
    }
    assert _ids(collection.find({"value": {"$gt": [invalid]}})) == {
        "array-a",
        "array-b",
    }


def test_indexed_ranges_feed_every_filter_consumer(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "lower", "value": {"score": 0}, "marker": "lower"},
            {"_id": "equal", "value": {"score": 1}, "marker": "equal"},
            {"_id": "higher", "value": {"score": 2}, "marker": "higher"},
            {
                "_id": "array-higher",
                "value": [{"score": 2}],
                "marker": "array-higher",
            },
            {"_id": "other-type", "value": "z", "marker": "other-type"},
        ]
    )
    query = {"value": {"$gt": {"score": 1}}}
    expected = {"higher", "array-higher"}

    assert _ids(collection.find(query)) == expected
    collection.create_index("value")
    assert _ids(collection.find(query)) == expected
    assert collection.count_documents(query) == 2
    assert set(collection.distinct("marker", query)) == expected
    assert _ids(collection.aggregate([{"$match": query}])) == expected

    result = collection.update_many(query, {"$set": {"matched": True}})
    assert result.matched_count == 2
    assert _ids(collection.find({"matched": True})) == expected


def test_cursor_and_aggregation_sort_share_scalar_bson_order(contract_target):
    collection = contract_target.collection
    expected = [
        ("number", 0),
        ("string", "value"),
        ("object", {"value": 1}),
        ("binary", Binary(b"value", subtype=0)),
        ("objectid", ObjectId("000000000000000000000001")),
        ("boolean", False),
        ("datetime", datetime(2026, 8, 2, tzinfo=timezone.utc)),
        ("regex", Regex("value", "")),
    ]
    collection.insert_many(
        {"_id": label, "value": value} for label, value in reversed(expected)
    )

    expected_ids = [label for label, _value in expected]
    assert [row["_id"] for row in collection.find({}).sort("value", 1)] == expected_ids
    assert [
        row["_id"] for row in collection.aggregate([{"$sort": {"value": 1}}])
    ] == expected_ids


def test_pull_reuses_recursive_bson_range_comparison(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": "arrays",
                "values": [None, 0, [0, [2]], [[0], 0], [1], [1, 2], [2]],
            },
            {
                "_id": "objects",
                "values": [{}, {"a": 1}, {"a": 2}, {"b": 0}, {"a": "text"}],
            },
            {
                "_id": "regex",
                "values": [Regex("a", ""), Regex("b", "")],
            },
        ]
    )

    collection.update_one({"_id": "arrays"}, {"$pull": {"values": {"$gt": [1]}}})
    collection.update_one({"_id": "objects"}, {"$pull": {"values": {"$gt": {"a": 1}}}})

    assert collection.find_one({"_id": "arrays"})["values"] == [None, 0, [1]]
    assert collection.find_one({"_id": "objects"})["values"] == [{}, {"a": 1}]

    with pytest.raises(_WRITE_ERRORS) as caught:
        collection.update_one(
            {"_id": "regex"},
            {"$pull": {"values": {"$gt": Regex("a", "")}}},
        )
    assert caught.value.code == 2
    assert collection.find_one({"_id": "regex"})["values"] == [
        Regex("a", ""),
        Regex("b", ""),
    ]


def test_pull_document_ranges_share_missing_and_array_path_semantics(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": "null-range",
                "values": [
                    {"label": "missing"},
                    {"label": "null", "score": None},
                    {"label": "number", "score": 1},
                ],
            },
            {
                "_id": "array-fanout",
                "values": [
                    {"label": "hit", "items": [{"score": 2}]},
                    {"label": "miss", "items": [{"score": 0}]},
                ],
            },
            {
                "_id": "array-index",
                "values": [
                    {"label": "hit", "items": [2]},
                    {"label": "miss", "items": [0, 2]},
                ],
            },
            {
                "_id": "embedded-id-range",
                "values": [{"_id": [1, 2]}, {"_id": 2}, {"_id": 3}],
            },
            {
                "_id": "embedded-id-equality",
                "values": [{"_id": [1, 2]}, {"_id": 2}, {"_id": 3}],
            },
        ]
    )

    collection.update_one(
        {"_id": "null-range"},
        {"$pull": {"values": {"score": {"$gte": None}}}},
    )
    collection.update_one(
        {"_id": "array-fanout"},
        {"$pull": {"values": {"items.score": {"$gt": 1}}}},
    )
    collection.update_one(
        {"_id": "array-index"},
        {"$pull": {"values": {"items.0": {"$gt": 1}}}},
    )
    collection.update_one(
        {"_id": "embedded-id-range"},
        {"$pull": {"values": {"_id": {"$gt": 1}}}},
    )
    collection.update_one(
        {"_id": "embedded-id-equality"},
        {"$pull": {"values": {"_id": 2}}},
    )

    assert collection.find_one({"_id": "null-range"})["values"] == [
        {"label": "number", "score": 1}
    ]
    assert collection.find_one({"_id": "array-fanout"})["values"] == [
        {"label": "miss", "items": [{"score": 0}]}
    ]
    assert collection.find_one({"_id": "array-index"})["values"] == [
        {"label": "miss", "items": [0, 2]}
    ]
    assert collection.find_one({"_id": "embedded-id-range"})["values"] == []
    assert collection.find_one({"_id": "embedded-id-equality"})["values"] == [
        {"_id": 3}
    ]
