"""Shared MongoDB contracts for TM-042's indexed read shapes."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import Binary, Decimal128, Int64, ObjectId


pytestmark = pytest.mark.contract
DATE = datetime(2026, 1, 1)
OID = ObjectId("000000000000000000000071")
OTHER_OID = ObjectId("000000000000000000000072")


@pytest.mark.parametrize(
    "value, other",
    [
        (OID, OTHER_OID),
        (DATE, DATE + timedelta(days=1)),
        (Int64(71), Int64(72)),
        (Decimal128("71.00"), Decimal128("72")),
        (Binary(b"value", 0), Binary(b"other", 0)),
        (Binary(b"value", 128), Binary(b"value", 0)),
    ],
)
@pytest.mark.parametrize("operator", ["bare", "$eq", "$in"])
def test_indexed_bson_equality_and_array_members(
    contract_target, value, other, operator
):
    col = contract_target.collection
    col.insert_many(
        [
            {"_id": 1, "k": value},
            {"_id": 2, "k": [other, value]},
            {"_id": 3, "k": other},
            {"_id": 4},
        ]
    )
    operand = (
        value
        if operator == "bare"
        else {operator: [value] if operator == "$in" else value}
    )
    query = {"k": operand}
    for indexed in [False, True]:
        if indexed:
            col.create_index("k")
        assert [doc["_id"] for doc in col.find(query).sort("_id", 1)] == [1, 2]
        assert col.count_documents(query) == 2


def test_standalone_indexed_date_range_keeps_array_semantics(contract_target):
    col = contract_target.collection
    col.insert_many(
        [
            {"_id": 1, "k": DATE},
            {"_id": 2, "k": DATE + timedelta(days=1)},
            {"_id": 3, "k": DATE + timedelta(days=2)},
            {"_id": 4, "k": [DATE - timedelta(days=1), DATE + timedelta(days=2)]},
            {"_id": 5, "k": "2026-01-01"},
            {"_id": 6, "k": None},
        ]
    )
    query = {
        "k": {
            "$gte": DATE.replace(tzinfo=timezone.utc),
            "$lt": DATE + timedelta(days=1),
        }
    }
    for indexed in [False, True]:
        if indexed:
            col.create_index("k")
        assert [doc["_id"] for doc in col.find(query).sort("_id", 1)] == [1, 4]
        assert col.count_documents(query) == 2


def test_indexed_or_deduplicates_before_bounds(contract_target):
    col = contract_target.collection
    col.insert_many(
        [
            {"_id": 1, "k": OID, "n": 0},
            {"_id": 2, "k": OTHER_OID, "n": 1},
            {"_id": 3, "k": OID, "n": 1},
            {"_id": 4, "k": OTHER_OID, "n": 0},
        ]
    )
    query = {"$or": [{"k": OID}, {"n": 1}, {"k": OID}]}
    for indexed in [False, True]:
        if indexed:
            col.create_index("k")
            col.create_index("n")
        assert [doc["_id"] for doc in col.find(query).sort("_id", 1)] == [1, 2, 3]
        assert col.count_documents(query) == 3
        assert list(col.find(query, {"_id": 1}).sort("_id", 1).skip(1).limit(1)) == [
            {"_id": 2}
        ]


@pytest.mark.parametrize(
    "bounds, expected",
    [
        ({"$gte": 1, "$lt": 2}, [2, 3, 4, 7]),
        ({"$gte": 1}, [2, 3, 4, 5, 7, 8]),
        ({"$lt": 2}, [1, 2, 3, 4, 7]),
    ],
)
def test_standalone_numeric_ranges_preserve_type_and_array_semantics(
    contract_target, bounds, expected
):
    col = contract_target.collection
    values = [0, 1, Int64(1), Decimal128("1"), 2, True, [-1, 3], 2**53 + 1]
    col.insert_many([{"_id": i, "k": value} for i, value in enumerate(values, 1)])
    for indexed in [False, True]:
        if indexed:
            col.create_index("k")
        query = {"k": bounds}
        assert [doc["_id"] for doc in col.find(query).sort("_id", 1)] == expected
        assert col.count_documents(query) == len(expected)
