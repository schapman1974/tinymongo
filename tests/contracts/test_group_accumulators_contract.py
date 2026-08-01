"""Common ``$group`` accumulator behavior shared by every target."""

import math

import pytest
from bson import Decimal128

from tinymongo.bson_types import bson_value_identity_key

from .support import observe


pytestmark = pytest.mark.contract


def _by_id(rows):
    return {row["_id"]: row for row in rows}


def test_avg_ignores_non_numeric_values_and_promotes_decimal(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "bucket": "decimal", "value": 1},
            {"_id": 2, "bucket": "decimal", "value": 2.0},
            {"_id": 3, "bucket": "decimal", "value": Decimal128("3.00")},
            {"_id": 4, "bucket": "decimal", "value": True},
            {"_id": 5, "bucket": "decimal", "value": "ignored"},
            {"_id": 6, "bucket": "decimal", "value": None},
            {"_id": 7, "bucket": "decimal"},
            {"_id": 8, "bucket": "double", "value": 1},
            {"_id": 9, "bucket": "double", "value": 2},
            {"_id": 10, "bucket": "empty", "value": False},
            {"_id": 11, "bucket": "empty", "value": [100]},
            {"_id": 12, "bucket": "mixed-exact", "value": Decimal128("1.00")},
            {"_id": 13, "bucket": "mixed-exact", "value": 2.1},
        ]
    )

    rows = _by_id(
        collection.aggregate(
            [{"$group": {"_id": "$bucket", "average": {"$avg": "$value"}}}]
        )
    )

    assert rows["decimal"] == {
        "_id": "decimal",
        "average": Decimal128("2.00"),
    }
    assert rows["double"] == {"_id": "double", "average": 1.5}
    assert isinstance(rows["double"]["average"], float)
    assert rows["empty"] == {"_id": "empty", "average": None}
    assert rows["mixed-exact"] == {
        "_id": "mixed-exact",
        "average": Decimal128("1.550000000000000044408920985006262"),
    }


def test_avg_preserves_cancellation_and_nonfinite_values(contract_target):
    collection = contract_target.collection
    grouped_values = {
        "cancel-middle": [1e16, 1.0, -1e16],
        "cancel-last": [1e16, -1e16, 1.0],
        "large-integers": [2**60, (2**60) + 2],
        "large-negative-integers": [-(2**60), -(2**60) + 2],
        "int64-cancellation": [
            7900154101625246752,
            -1259608310039654329,
            -6593466016263951012,
        ],
        "int64-cancellation-with-double": [
            7900154101625246752,
            -1259608310039654329,
            float(-6593466016263951012),
        ],
        "decimal-cancel": [
            Decimal128("1E+34"),
            1.0,
            Decimal128("-1E+34"),
        ],
        "nan": [float("nan"), 1.0],
        "infinity": [float("inf"), 1.0],
        "opposite-infinities": [float("inf"), float("-inf")],
        "decimal-infinity": [Decimal128("Infinity"), 1.0],
        "decimal-opposite-infinities": [Decimal128("Infinity"), float("-inf")],
    }
    documents = []
    document_id = 0
    for bucket, values in grouped_values.items():
        for value in values:
            documents.append({"_id": document_id, "bucket": bucket, "value": value})
            document_id += 1
    collection.insert_many(documents)

    rows = _by_id(
        collection.aggregate(
            [
                {"$sort": {"_id": 1}},
                {"$group": {"_id": "$bucket", "average": {"$avg": "$value"}}},
            ]
        )
    )

    assert rows["cancel-middle"]["average"] == 0.0
    assert rows["cancel-last"]["average"] == 1.0 / 3.0
    assert rows["large-integers"]["average"] == float((2**60) + 1)
    assert rows["large-negative-integers"]["average"] == float(-(2**60) + 1)
    assert rows["int64-cancellation"]["average"] == 1.5693258440547136e16
    assert rows["int64-cancellation-with-double"]["average"] == 1.5693258440546986e16
    assert rows["decimal-cancel"]["average"] == Decimal128(
        "0.3333333333333333333333333333333333"
    )
    assert math.isnan(rows["nan"]["average"])
    assert rows["infinity"]["average"] == float("inf")
    assert math.isnan(rows["opposite-infinities"]["average"])
    assert rows["decimal-infinity"]["average"] == Decimal128("Infinity")
    assert rows["decimal-opposite-infinities"]["average"].to_decimal().is_nan()


def test_first_last_and_push_follow_sorted_input_and_missing_rules(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "bucket": "first-missing", "rank": 2, "value": "last"},
            {"_id": 2, "bucket": "first-missing", "rank": 1},
            {"_id": 3, "bucket": "first-missing", "rank": 3, "value": None},
            {"_id": 4, "bucket": "last-missing", "rank": 1, "value": "first"},
            {"_id": 5, "bucket": "last-missing", "rank": 2, "value": None},
            {"_id": 6, "bucket": "last-missing", "rank": 3},
        ]
    )

    rows = _by_id(
        collection.aggregate(
            [
                {"$sort": {"rank": 1}},
                {
                    "$group": {
                        "_id": "$bucket",
                        "first_rank": {"$first": "$rank"},
                        "last_rank": {"$last": "$rank"},
                        "first_value": {"$first": "$value"},
                        "last_value": {"$last": "$value"},
                        "values": {"$push": "$value"},
                    }
                },
            ]
        )
    )

    assert rows["first-missing"] == {
        "_id": "first-missing",
        "first_rank": 1,
        "last_rank": 3,
        "first_value": None,
        "last_value": None,
        "values": ["last", None],
    }
    assert rows["last-missing"] == {
        "_id": "last-missing",
        "first_rank": 1,
        "last_rank": 3,
        "first_value": "first",
        "last_value": None,
        "values": ["first", None],
    }


def test_add_to_set_uses_recursive_bson_identity(contract_target):
    collection = contract_target.collection
    values = [
        1,
        1.0,
        Decimal128("1.00"),
        True,
        None,
        {"a": 1, "b": 2},
        {"a": 1.0, "b": 2},
        {"b": 2, "a": 1},
        [1, 2],
        [1.0, 2],
        [2, 1],
        float("nan"),
        Decimal128("NaN"),
    ]
    collection.insert_many(
        [{"_id": index, "value": value} for index, value in enumerate(values)]
        + [{"_id": len(values)}, {"_id": len(values) + 1}]
    )

    result = list(
        collection.aggregate(
            [{"$group": {"_id": None, "values": {"$addToSet": "$value"}}}]
        )
    )[0]["values"]
    actual_keys = {bson_value_identity_key(value) for value in result}
    expected_values = [
        1,
        True,
        None,
        {"a": 1, "b": 2},
        {"b": 2, "a": 1},
        [1, 2],
        [2, 1],
        float("nan"),
    ]
    expected_keys = {bson_value_identity_key(value) for value in expected_values}

    assert len(result) == len(expected_values)
    assert actual_keys == expected_keys
    assert any(isinstance(value, float) and math.isnan(value) for value in result)


def test_array_values_remain_whole_accumulator_values(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "value": [1, 2]},
            {"_id": 2, "value": [1, 2]},
            {"_id": 3, "value": [3, 4]},
        ]
    )

    result = list(
        collection.aggregate(
            [
                {
                    "$group": {
                        "_id": None,
                        "first": {"$first": "$value"},
                        "last": {"$last": "$value"},
                        "pushed": {"$push": "$value"},
                        "unique": {"$addToSet": "$value"},
                    }
                }
            ]
        )
    )[0]
    unique = result.pop("unique")
    assert result == {
        "_id": None,
        "first": [1, 2],
        "last": [3, 4],
        "pushed": [[1, 2], [1, 2], [3, 4]],
    }
    assert {tuple(value) for value in unique} == {(1, 2), (3, 4)}


def test_accumulators_accept_the_supported_expression_subset(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "items": [1, 2]},
            {"_id": 2, "items": []},
        ]
    )

    assert list(
        collection.aggregate(
            [
                {
                    "$group": {
                        "_id": None,
                        "literal": {"$first": {"$literal": "$not-a-path"}},
                        "arrays": {"$push": {"$literal": [1, 2]}},
                        "sizes": {"$push": {"$size": "$items"}},
                        "fallbacks": {
                            "$addToSet": {"$ifNull": ["$missing", "fallback"]}
                        },
                    }
                }
            ]
        )
    ) == [
        {
            "_id": None,
            "literal": "$not-a-path",
            "arrays": [[1, 2], [1, 2]],
            "sizes": [2, 0],
            "fallbacks": ["fallback"],
        }
    ]


def test_missing_only_arrays_and_empty_inputs_match_mongodb(contract_target):
    collection = contract_target.collection
    collection.insert_many([{"_id": 1}, {"_id": 2}])

    pipeline = [
        {
            "$group": {
                "_id": None,
                "average": {"$avg": "$missing"},
                "first": {"$first": "$missing"},
                "last": {"$last": "$missing"},
                "pushed": {"$push": "$missing"},
                "unique": {"$addToSet": "$missing"},
            }
        }
    ]
    assert list(collection.aggregate(pipeline)) == [
        {
            "_id": None,
            "average": None,
            "first": None,
            "last": None,
            "pushed": [],
            "unique": [],
        }
    ]

    collection.delete_many({})
    assert list(collection.aggregate(pipeline)) == []


@pytest.mark.parametrize(
    "operator",
    ["$addToSet", "$avg", "$first", "$last", "$max", "$min", "$push", "$sum"],
)
def test_group_accumulators_reject_array_argument_lists(contract_target, operator):
    outcome = observe(
        lambda: list(
            contract_target.collection.aggregate(
                [{"$group": {"_id": None, "value": {operator: [1, 2]}}}]
            )
        )
    )

    assert outcome.error == "operation_failure"
