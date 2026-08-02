"""Focused branch coverage for MongoDB-style query operator edge cases."""

import re

import pytest

from tinymongo import table_backends as backends
from tinymongo.errors import OperationFailure, TinyMongoNotSupportedError


@pytest.mark.parametrize(
    ("operand", "message"),
    [
        (float("nan"), "finite integral"),
        (1.5, "integral values"),
        (-1, "below the supported range"),
        (2**31, "above the supported range"),
    ],
)
def test_size_rejects_nonfinite_fractional_and_out_of_range_values(
    operand,
    message,
):
    with pytest.raises(OperationFailure, match=message) as caught:
        backends.validate_filter_operators({"value": {"$size": operand}})

    assert caught.value.code == 2


def test_mod_rejects_a_zero_divisor_with_mongodb_error_metadata():
    with pytest.raises(OperationFailure, match="divisor cannot be zero") as caught:
        backends.validate_filter_operators({"value": {"$mod": [0, 0]}})

    assert caught.value.code == 2


def test_mod_skips_nonfinite_and_out_of_int64_stored_values():
    query = {"value": {"$mod": [2, 0]}}

    assert not backends.matches_filter({"value": float("nan")}, query)
    assert not backends.matches_filter({"value": float("inf")}, query)
    assert not backends.matches_filter({"value": 2**63}, query)
    assert not backends.matches_filter({"value": -(2**63) - 1}, query)


def test_mod_treats_a_defensive_numeric_conversion_failure_as_no_match(
    monkeypatch,
):
    original_conversion = backends.bson_number_decimal

    def fail_for_stored_value(value):
        if value == 7:
            raise ArithmeticError("defensive conversion failure")
        return original_conversion(value)

    monkeypatch.setattr(backends, "bson_number_decimal", fail_for_stored_value)

    assert not backends._mod_matches(7, [2, 1])


def test_type_rejects_empty_invalid_and_wrongly_typed_operands():
    cases = [
        ([], 9, "at least one type"),
        (42, 2, "Invalid numerical BSON type code"),
        (True, 14, "number or a string"),
    ]

    for operand, code, message in cases:
        with pytest.raises(OperationFailure, match=message) as caught:
            backends.validate_filter_operators({"value": {"$type": operand}})
        assert caught.value.code == code


def test_type_deduplicates_equivalent_aliases_and_codes():
    assert backends._normalize_type_operand(["string", 2, "string"]) == frozenset(
        ("string",)
    )


def test_type_names_cover_unregistered_and_decimal_values():
    assert backends._bson_query_type_names(object()) == frozenset()

    if backends._DECIMAL128 is None:
        pytest.skip("Decimal128 query typing requires the optional BSON package")
    value = backends._DECIMAL128("1.25")

    assert backends._bson_query_type_names(value) == frozenset(("number", "decimal"))
    assert backends.matches_filter({"value": value}, {"value": {"$type": 19}})


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ({1: "value"}, "field names must be strings"),
        ({"$and": []}, "requires a non-empty array"),
        ({"value": {"$eq": 1, "literal": 1}}, "cannot mix operators"),
        ({"value": {"$in": 1}}, "requires an array"),
        ({"value": {"$options": "i"}}, "requires.*regex"),
    ],
)
def test_malformed_query_shapes_use_mongodb_parse_error_code(query, message):
    with pytest.raises(OperationFailure, match=message) as caught:
        backends.validate_filter_operators(query)

    assert caught.value.code == 2


@pytest.mark.parametrize(
    "query",
    [
        {"$expr": {"$eq": ["$value", 1]}},
        {"$jsonSchema": {"required": ["value"]}},
        {"values": {"$elemMatch": {"$jsonSchema": {"required": ["score"]}}}},
        {"values": {"$elemMatch": {"$bitsAllSet": 1}}},
    ],
)
def test_known_mongodb_operators_remain_honestly_unsupported(query):
    with pytest.raises(TinyMongoNotSupportedError):
        backends.validate_filter_operators(query)


def test_empty_elem_match_matches_only_container_array_members():
    query = {"value": {"$elemMatch": {}}}
    backends.validate_filter_operators(query)

    assert backends.matches_filter({"value": [{"kind": "document"}]}, query)
    assert backends.matches_filter({"value": [["nested-array"]]}, query)
    assert not backends.matches_filter({"value": [1, "scalar", None]}, query)
    assert not backends.matches_filter({"value": "not-an-array"}, query)


def test_query_path_candidates_cover_numeric_fanout_and_scalar_boundaries():
    assert backends._is_query_array_index("0")
    assert backends._is_query_array_index("12")
    assert not backends._is_query_array_index("")
    assert not backends._is_query_array_index("01")

    assert backends._query_path_match_candidates("value", []) == [("value", False)]
    assert backends._query_path_candidates([{"3": "named"}], ["3"]) == ["named"]
    assert backends._query_path_candidates(("zero",), ["0"]) == ["zero"]
    assert backends._query_path_candidates([{"0": "field"}, "index"], ["0"]) == [
        {"0": "field"},
        "field",
    ]
    assert backends._query_path_match_candidates([[1, 2]], ["0"]) == [([1, 2], True)]
    assert backends._query_path_match_candidates([1], ["0", "nested"]) == []
    assert backends._query_path_candidates({"value": 1}, ["value", "nested"]) == [
        backends._MISSING
    ]
    assert backends._query_path_candidates([], ["missing"]) == []


def test_multiple_query_path_candidates_preserve_operator_semantics():
    assert backends._field_path_matches([1, 2], 2)
    assert backends._field_path_matches([{"a": 1}, {"a": 2}], {"a": 2})
    assert backends._field_path_matches(
        ["Ada", "Grace"],
        {"$regex": "^a", "$options": "i"},
    )
    assert backends._field_path_matches([1, 2], {"$gt": 1})
    assert not backends._field_path_matches([1, 2], {"$gt": 3})
    assert not backends._field_path_matches([1, 2], {"$ne": 1})
    assert backends._field_path_matches(
        [backends._MISSING, backends._MISSING],
        {"$exists": False},
    )
    assert not backends._field_path_matches(
        [[1, 2]],
        1,
        indexed_endpoints=[True],
    )
    assert backends._field_path_matches(
        [[1, 2], 1],
        1,
        indexed_endpoints=[True, False],
    )
    assert backends._field_path_matches([], {"$ne": None})
    assert backends._field_path_matches([], {"$exists": False})
    assert not backends._field_path_matches([], {"$exists": True})


def test_query_null_equality_distinguishes_missing_from_zero_candidates():
    assert backends._query_values_equal(backends._MISSING, None)
    assert not backends._query_values_equal(backends._MISSING, 0)
    assert backends._field_path_matches([backends._MISSING], None)
    assert not backends._field_path_matches([], None)


def test_all_accepts_elem_match_but_rejects_other_nested_operator_documents():
    valid = {"value": {"$all": [{"$elemMatch": {"$gt": 1, "$lt": 3}}]}}
    backends.validate_filter_operators(valid)

    assert backends.matches_filter({"value": [0, 2, 4]}, valid)
    assert not backends.matches_filter({"value": [0, 4]}, valid)

    with pytest.raises(OperationFailure, match="cannot contain nested") as caught:
        backends.validate_filter_operators({"value": {"$all": [{"$gt": 1}]}})
    assert caught.value.code == 2


@pytest.mark.parametrize("operator", ["$in", "$nin"])
def test_in_and_nin_reject_nested_operator_documents(operator):
    with pytest.raises(OperationFailure, match="cannot contain nested") as caught:
        backends.validate_filter_operators({"value": {operator: [{"$gt": 1}]}})

    assert caught.value.code == 2


def test_elem_match_accepts_logical_document_queries():
    query = {
        "value": {
            "$elemMatch": {
                "$or": [
                    {"kind": "quiz"},
                    {"score": {"$gt": 8}},
                ]
            }
        }
    }
    backends.validate_filter_operators(query)

    assert backends.matches_filter(
        {"value": [{"kind": "quiz", "score": 1}]},
        query,
    )
    assert backends.matches_filter(
        {"value": [{"kind": "exam", "score": 9}]},
        query,
    )
    assert not backends.matches_filter(
        {"value": [{"kind": "exam", "score": 8}]},
        query,
    )


def test_regex_compatibility_validator_walks_every_legacy_query_shape():
    backends.validate_regex_filter(
        {
            "$and": [
                {"name": {"$regex": "^ada", "$options": "i"}},
                {"alias": {"$not": {"$regex": "^admin"}}},
            ],
            "tags": {"$in": ["python", re.compile("^database")]},
            "literal": re.compile("^value"),
            "not_literal": {"$not": re.compile("^hidden")},
        }
    )


def test_get_nested_returns_custom_default_for_missing_paths():
    marker = object()

    assert backends._get_nested({"outer": {}}, "outer.missing", marker) is marker
    assert backends._get_nested({"outer": 1}, "outer.missing", marker) is marker


@pytest.mark.parametrize("operator", ["$all", "$in", "$nin"])
def test_regex_compatibility_validator_rejects_operator_documents_in_arrays(
    operator,
):
    with pytest.raises(OperationFailure, match="accepts regex values") as caught:
        backends.validate_regex_filter({"value": {operator: [{"$regex": "^value"}]}})

    assert caught.value.code == 2
