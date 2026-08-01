"""Decimal128 behavior shared by every API and storage target."""

import pytest
from tinymongo.bson_types import bson_identity_key
from tinymongo.errors import TinyMongoNotSupportedError

from .support import observe


pytestmark = pytest.mark.contract
bson = pytest.importorskip("bson")
Decimal128 = bson.Decimal128


def _ids(rows):
    return {row["_id"] for row in rows}


def test_decimal128_extreme_ids_round_trip(contract_target):
    collection = contract_target.collection
    high = Decimal128("1E+6144")
    low = Decimal128("1E-6143")

    collection.insert_many(
        [
            {"_id": high, "label": "high"},
            {"_id": low, "label": "low"},
        ]
    )

    assert collection.find_one({"_id": Decimal128.from_bid(high.bid)})["label"] == (
        "high"
    )
    assert collection.find_one({"_id": Decimal128.from_bid(low.bid)})["label"] == (
        "low"
    )


def test_decimal128_round_trips_and_participates_in_numeric_queries(contract_target):
    collection = contract_target.collection
    exact = Decimal128("0.1")
    collection.insert_many(
        [
            {"_id": "negative", "amount": Decimal128("-2.5")},
            {"_id": "integer", "amount": 1},
            {"_id": "decimal-one", "amount": Decimal128("1.00")},
            {"_id": "exact", "amount": exact},
            {"_id": "double", "amount": 0.1},
            {"_id": "high", "amount": Decimal128("3.75")},
        ]
    )

    restored = collection.find_one({"_id": "exact"})["amount"]
    assert isinstance(restored, Decimal128)
    assert restored.bid == exact.bid
    assert _ids(collection.find({"amount": Decimal128("1")})) == {
        "integer",
        "decimal-one",
    }
    assert _ids(collection.find({"amount": Decimal128("0.1")})) == {"exact"}
    assert _ids(collection.find({"amount": 0.1})) == {"double"}
    assert _ids(collection.find({"amount": {"$gt": Decimal128("1")}})) == {"high"}
    assert _ids(collection.find({"amount": {"$gt": 1}})) == {"high"}
    assert [
        row["_id"]
        for row in collection.find(
            {"_id": {"$in": ["negative", "integer", "high"]}}
        ).sort("amount", 1)
    ] == ["negative", "integer", "high"]


def test_decimal128_nan_query_comparisons_follow_mongodb(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "nan", "amount": Decimal128("NaN")},
            {"_id": "signaling", "amount": Decimal128("sNaN")},
            {"_id": "one", "amount": 1},
        ]
    )

    assert _ids(collection.find({"amount": Decimal128("NaN")})) == {
        "nan",
        "signaling",
    }
    assert _ids(collection.find({"amount": {"$gt": Decimal128("NaN")}})) == set()
    assert _ids(collection.find({"amount": {"$lt": Decimal128("NaN")}})) == set()
    assert _ids(collection.find({"amount": {"$gt": 1}})) == set()
    assert _ids(collection.find({"amount": {"$gte": Decimal128("NaN")}})) == {
        "nan",
        "signaling",
    }
    assert _ids(collection.find({"amount": {"$lte": Decimal128("NaN")}})) == {
        "nan",
        "signaling",
    }


def test_decimal128_grouping_min_max_and_sum_use_one_numeric_family(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "first", "category": 1, "amount": Decimal128("1.00")},
            {
                "_id": "second",
                "category": Decimal128("1.0"),
                "amount": Decimal128("2.5"),
            },
            {"_id": "other", "category": 2, "amount": 4},
        ]
    )

    rows = list(
        collection.aggregate(
            [
                {"$sort": {"_id": 1}},
                {
                    "$group": {
                        "_id": "$category",
                        "total": {"$sum": "$amount"},
                        "minimum": {"$min": "$amount"},
                        "maximum": {"$max": "$amount"},
                        "count": {"$sum": 1},
                    }
                },
                {"$sort": {"_id": 1}},
            ]
        )
    )

    assert rows == [
        {
            "_id": 1,
            "total": Decimal128("3.50"),
            "minimum": Decimal128("1.00"),
            "maximum": Decimal128("2.5"),
            "count": 2,
        },
        {"_id": 2, "total": 4, "minimum": 4, "maximum": 4, "count": 1},
    ]


def test_decimal128_min_max_ties_and_literal_sum_match_mongodb(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "amount": Decimal128("1.00")},
            {"_id": 2, "amount": 1},
            {"_id": 3, "amount": 1.0},
        ]
    )

    assert list(
        collection.aggregate(
            [
                {"$sort": {"_id": 1}},
                {
                    "$group": {
                        "_id": None,
                        "minimum": {"$min": "$amount"},
                        "maximum": {"$max": "$amount"},
                        "literal_total": {"$sum": Decimal128("1.25")},
                    }
                },
            ]
        )
    ) == [
        {
            "_id": None,
            "minimum": 1.0,
            "maximum": 1.0,
            "literal_total": Decimal128("3.75"),
        }
    ]


def test_decimal128_inc_promotes_the_result(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": "balance", "amount": Decimal128("1.00")})

    result = collection.update_one(
        {"_id": "balance"}, {"$inc": {"amount": Decimal128("2.5")}}
    )

    assert result.matched_count == 1
    assert result.modified_count == 1
    assert collection.find_one({"_id": "balance"})["amount"] == Decimal128("3.50")

    collection.update_one({"_id": "balance"}, {"$set": {"amount": Decimal128("2")}})
    collection.update_one({"_id": "balance"}, {"$inc": {"amount": 0.1}})
    assert collection.find_one({"_id": "balance"})["amount"] == Decimal128(
        "2.100000000000000"
    )


def test_decimal128_representation_changes_are_persisted(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": "value", "amount": 1})

    first = collection.update_one(
        {"_id": "value"}, {"$set": {"amount": Decimal128("1.0")}}
    )
    assert first.modified_count == 1
    assert collection.find_one({"_id": "value"})["amount"].bid == Decimal128("1.0").bid

    second = collection.update_one(
        {"_id": "value"}, {"$set": {"amount": Decimal128("1.00")}}
    )
    assert second.modified_count == 1
    assert collection.find_one({"_id": "value"})["amount"].bid == Decimal128("1.00").bid

    unchanged = collection.update_one(
        {"_id": "value"}, {"$inc": {"amount": Decimal128("0.000")}}
    )
    assert unchanged.modified_count == 0
    assert collection.find_one({"_id": "value"})["amount"].bid == Decimal128("1.00").bid

    collection.update_one({"_id": "value"}, {"$set": {"amount": Decimal128("2")}})
    rounded_noop = collection.update_one(
        {"_id": "value"}, {"$inc": {"amount": Decimal128("1E-300")}}
    )
    assert rounded_noop.modified_count == 0
    assert collection.find_one({"_id": "value"})["amount"].bid == Decimal128("2").bid

    collection.update_one({"_id": "value"}, {"$set": {"amount": Decimal128("sNaN")}})
    quieted = collection.update_one(
        {"_id": "value"}, {"$inc": {"amount": Decimal128("0")}}
    )
    assert quieted.modified_count == 1
    assert collection.find_one({"_id": "value"})["amount"].bid == Decimal128("NaN").bid

    quiet_nan = collection.update_one(
        {"_id": "value"}, {"$inc": {"amount": Decimal128("0")}}
    )
    assert quiet_nan.modified_count == 1
    assert collection.find_one({"_id": "value"})["amount"].bid == Decimal128("NaN").bid


def test_decimal128_sum_keeps_double_precision_until_promotion(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "mixed": 0.1, "cancellation": 1e16},
            {"_id": 2, "mixed": 0.2, "cancellation": 1.0},
            {
                "_id": 3,
                "mixed": Decimal128("1"),
                "cancellation": -1e16,
            },
        ]
    )

    assert list(
        collection.aggregate(
            [
                {
                    "$group": {
                        "_id": None,
                        "mixed": {"$sum": "$mixed"},
                        "cancellation": {"$sum": "$cancellation"},
                    }
                }
            ]
        )
    ) == [
        {
            "_id": None,
            "mixed": Decimal128("1.300000000000000016653345369377348"),
            "cancellation": 0.0,
        }
    ]


def test_decimal128_projection_flags_and_stage_arguments(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "rank": 1, "keep": "first", "remove": "x"},
            {"_id": 2, "rank": 2, "keep": "second", "remove": "y"},
            {"_id": 3, "rank": 3, "keep": "third", "remove": "z"},
            {"_id": 4, "rank": 4, "keep": "fourth", "remove": "w"},
        ]
    )

    projected = collection.find_one({"_id": 1}, {"remove": Decimal128("0")})
    assert projected == {"_id": 1, "rank": 1, "keep": "first"}
    assert list(
        collection.aggregate(
            [
                {"$sort": {"rank": Decimal128("1")}},
                {"$skip": Decimal128("1")},
                {"$limit": Decimal128("2.0")},
                {
                    "$project": {
                        "_id": Decimal128("0"),
                        "keep": Decimal128("NaN"),
                    }
                },
            ]
        )
    ) == [{"keep": "second"}, {"keep": "third"}]


def test_decimal128_array_updates_and_distinct_reuse_numeric_identity(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": "values",
            "members": [1, Decimal128("3.0")],
            "amount": 1,
        }
    )
    collection.insert_many(
        [
            {"_id": "same", "amount": Decimal128("1.00")},
            {"_id": "decimal", "amount": Decimal128("0.1")},
            {"_id": "double", "amount": 0.1},
        ]
    )

    collection.update_one(
        {"_id": "values"},
        {"$addToSet": {"members": {"$each": [Decimal128("1.0"), 2]}}},
    )
    collection.update_one(
        {"_id": "values"},
        {
            "$push": {
                "members": {
                    "$each": [Decimal128("-1.5")],
                    "$position": Decimal128("-1"),
                    "$sort": Decimal128("1.0"),
                    "$slice": Decimal128("4.0"),
                }
            }
        },
    )
    assert collection.find_one({"_id": "values"})["members"] == [
        Decimal128("-1.5"),
        1,
        2,
        Decimal128("3.0"),
    ]

    collection.update_one(
        {"_id": "values"},
        {"$pull": {"members": {"$gte": Decimal128("2")}}},
    )
    assert collection.find_one({"_id": "values"})["members"] == [
        Decimal128("-1.5"),
        1,
    ]

    distinct = collection.distinct("amount")
    assert {bson_identity_key(value) for value in distinct} == {
        bson_identity_key(1),
        bson_identity_key(Decimal128("0.1")),
        bson_identity_key(0.1),
    }


def test_decimal128_numeric_identity_is_enforced_by_ids_and_unique_indexes(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_one({"_id": 1, "amount": 1})

    duplicate_id = observe(
        lambda: collection.insert_one(
            {"_id": Decimal128("1.0"), "amount": Decimal128("2")}
        )
    )
    assert duplicate_id.error == "duplicate_key"

    collection.create_index("amount", unique=True)
    if contract_target.name in ("postgres", "postgresql", "mysql", "mariadb"):
        with pytest.raises(TinyMongoNotSupportedError, match="Decimal128 values"):
            collection.insert_one({"_id": "decimal-tenth", "amount": Decimal128("0.1")})
        return

    collection.insert_one({"_id": "decimal-tenth", "amount": Decimal128("0.1")})
    collection.insert_one({"_id": "double-tenth", "amount": 0.1})
    collection.insert_one(
        {
            "_id": "precision-a",
            "amount": Decimal128("1.234567890123456789012345678901234"),
        }
    )
    collection.insert_one(
        {
            "_id": "precision-b",
            "amount": Decimal128("1.234567890123456789012345678901235"),
        }
    )
    collection.insert_one({"_id": "large-decimal", "amount": Decimal128("1E+23")})
    collection.insert_one({"_id": "large-double", "amount": 1e23})
    exact_double = observe(
        lambda: collection.insert_one(
            {
                "_id": "large-double-exact-decimal",
                "amount": Decimal128(str(int(1e23))),
            }
        )
    )
    assert exact_double.error == "duplicate_key"
    exact_integer = 2**60
    collection.insert_one({"_id": "exact-integer", "amount": exact_integer})
    for identifier, equivalent in (
        ("exact-integer-double", float(exact_integer)),
        ("exact-integer-decimal", Decimal128(str(exact_integer))),
    ):
        duplicate_integer = observe(
            lambda identifier=identifier, equivalent=equivalent: collection.insert_one(
                {"_id": identifier, "amount": equivalent}
            )
        )
        assert duplicate_integer.error == "duplicate_key"
    duplicate_value = observe(
        lambda: collection.insert_one(
            {"_id": "duplicate", "amount": Decimal128("1.00")}
        )
    )
    assert duplicate_value.error == "duplicate_key"
