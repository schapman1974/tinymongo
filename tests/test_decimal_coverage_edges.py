"""Focused branch coverage for Decimal128 numeric integration."""

from decimal import Decimal
import operator

import pytest

from tinymongo import aggregation
from tinymongo import bson_types
from tinymongo import indexes
from tinymongo import table_backends
from tinymongo import tinymongo as core
from tinymongo.errors import TinyMongoNotSupportedError


def test_sum_accumulator_falls_back_cleanly_without_decimal128(monkeypatch):
    monkeypatch.setattr(aggregation, "_DECIMAL128", None)
    accumulator = aggregation._SumAccumulator()

    accumulator.add(7)
    accumulator.add(0.5)
    accumulator.add(0.25)

    assert accumulator.value() == 7.75


def test_bson_numeric_helpers_reject_invalid_values_and_missing_dependency(
    monkeypatch,
):
    with pytest.raises(TypeError, match="Boolean values"):
        bson_types.bson_number_decimal(True)
    with pytest.raises(TypeError, match="Unsupported BSON numeric type"):
        bson_types.bson_number_decimal(object())

    assert bson_types._decimal128_update_operand(float("inf")) == Decimal("Infinity")
    for left, right in (("not-a-number", 1), (1, "not-a-number")):
        with pytest.raises(TypeError, match="requires two numeric values"):
            bson_types.add_bson_numbers(left, right)

    monkeypatch.setattr(bson_types, "_create_decimal128_context", None)
    with pytest.raises(RuntimeError, match="optional pymongo package"):
        bson_types.decimal128_context()

    monkeypatch.setattr(bson_types, "_Decimal128", None)
    with pytest.raises(RuntimeError, match="optional pymongo package"):
        bson_types.decimal128_from_decimal(Decimal("1"))


def test_decimal128_index_tokens_cover_zero_exact_double_and_nonfinite_values():
    bson = pytest.importorskip("bson")

    assert indexes.index_tokens({"value": bson.Decimal128("0.00")}, "value") == (
        "number:0",
    )
    assert indexes.index_tokens({"value": bson.Decimal128("0.5")}, "value") == (
        "number:0.5",
    )
    exact_integer = 2**60
    assert (
        indexes.index_tokens({"value": exact_integer}, "value")
        == indexes.index_tokens({"value": float(exact_integer)}, "value")
        == indexes.index_tokens({"value": bson.Decimal128(str(exact_integer))}, "value")
    )
    large_double = 1e23
    double_token = indexes.index_tokens({"value": large_double}, "value")
    assert (
        indexes.index_tokens(
            {"value": bson.Decimal128(str(int(large_double)))}, "value"
        )
        == double_token
    )
    assert indexes.index_tokens(
        {"value": bson.Decimal128("1E+23")}, "value"
    ) == indexes.index_tokens({"value": 10**23}, "value")
    assert (
        indexes.index_tokens({"value": bson.Decimal128("1E+23")}, "value")
        != double_token
    )
    with pytest.raises(TinyMongoNotSupportedError, match="Non-finite numbers"):
        indexes.index_tokens({"value": bson.Decimal128("Infinity")}, "value")


def test_extreme_decimal128_ids_use_safe_exact_ratio_keys():
    bson = pytest.importorskip("bson")
    extreme = bson.Decimal128("1E+6144")

    canonical = table_backends._canonical_id_value(extreme)
    assert canonical[1][1][0] == table_backends._ID_RATIO_HEX_TAG
    assert table_backends._physical_id_key(extreme) == table_backends._physical_id_key(
        bson.Decimal128.from_bid(extreme.bid)
    )
    candidates = table_backends._physical_id_candidates(extreme)
    assert candidates[0].startswith(table_backends._PHYSICAL_ID_PREFIX)
    assert candidates[1].startswith(table_backends._PHYSICAL_ID_PREFIX)
    assert candidates[0] != candidates[1]
    legacy_integer = table_backends._legacy_stringified_id(10**600)
    assert len(legacy_integer) == 601
    assert legacy_integer.startswith("1")


def test_comparison_helpers_reject_unsupported_operands_and_comparison_errors():
    assert table_backends._comparison_matches(1, object(), operator.eq) is False

    def invalid_comparison(_left, _right):
        raise ValueError("cannot compare")

    assert table_backends._comparison_matches(1, 1, invalid_comparison) is False
    assert core._pull_comparison_matches([float("nan"), 0], 1, operator.lt) is True


def test_tinydb_parser_and_writes_cover_native_non_numeric_query_routes(tmp_path):
    client = core.TinyMongoClient(str(tmp_path / "db"))
    collection = client.app.people
    collection.insert_one(
        {"_id": 1, "name": "Ada", "state": "ready", "score": "middle"}
    )

    combined = collection.parse_query({"name": "Ada", "state": "ready"})
    assert combined is not None
    for operator_query in (
        {"score": {"$gte": "first"}},
        {"score": {"$lte": "zulu"}},
        {"score": {"$lt": "zulu"}},
    ):
        assert collection.parse_query(operator_query) is not None

    updated = collection.update_one(
        {"score": {"$gte": "first"}},
        {"$set": {"seen": True}},
    )
    replaced = collection.replace_one(
        {"state": {"$lt": "zulu"}},
        {"name": "Grace", "state": "ready"},
    )

    assert updated.matched_count == 1
    assert replaced.matched_count == 1
    assert collection.find_one({"_id": 1}) == {
        "_id": 1,
        "name": "Grace",
        "state": "ready",
    }
    client.close()
