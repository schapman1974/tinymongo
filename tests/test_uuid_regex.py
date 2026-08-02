"""Focused UUID and BSON regular-expression behavior."""

import re
from uuid import UUID

import pytest

import tinymongo
from tinymongo.errors import OperationFailure, TinyMongoNotSupportedError
from tinymongo.indexes import parse_index_spec
from tinymongo.table_backends import (
    _reject_remote_unique_values,
    matches_filter,
    validate_filter_operators,
    validate_regex_filter,
)
from tinymongo.tinymongo import TinyMongoCollection


bson = pytest.importorskip("bson")


def test_regex_query_forms_distinguish_predicates_from_explicit_equality():
    stored = bson.Regex("Ab.c", "i")
    documents = [
        {"value": "Abxc"},
        {"value": ["no", "Abxc"]},
        {"value": stored},
        {"value": bson.Regex("Ab.c", "iu")},
    ]

    assert [
        matches_filter(document, {"value": {"$regex": "Ab.c", "$options": "i"}})
        for document in documents
    ] == [True, True, True, False]
    assert [matches_filter(document, {"value": stored}) for document in documents] == [
        True,
        True,
        True,
        False,
    ]
    assert [
        matches_filter(document, {"value": {"$eq": stored}}) for document in documents
    ] == [False, False, True, False]


def test_regex_in_nin_all_not_and_empty_all_semantics():
    expression = bson.Regex("^a", "i")

    assert matches_filter({"value": "Alpha"}, {"value": {"$in": [expression]}})
    assert not matches_filter({"value": "Alpha"}, {"value": {"$nin": [expression]}})
    assert matches_filter({"value": "Alpha"}, {"value": {"$all": [expression]}})
    assert matches_filter(
        {"value": ["other", "Alpha"]}, {"value": {"$all": [expression]}}
    )
    assert not matches_filter({"value": "Alpha"}, {"value": {"$all": []}})
    assert not matches_filter({"value": "Alpha"}, {"value": {"$not": expression}})
    assert matches_filter({"value": "other"}, {"value": {"$not": expression}})


def test_regex_options_validation_accepts_u_and_rejects_invalid_combinations():
    validate_filter_operators({"value": {"$regex": "x", "$options": "imsxu"}})
    validate_filter_operators(
        {"value": {"$regex": bson.Regex("x", 0), "$options": "i"}}
    )

    for options, code in (("l", 51108), ("z", 51108), (1, 2)):
        with pytest.raises(OperationFailure, match="supports only") as caught:
            validate_filter_operators({"value": {"$regex": "x", "$options": options}})
        assert caught.value.code == code

    with pytest.raises(OperationFailure, match="embedded") as caught:
        validate_filter_operators(
            {"value": {"$regex": bson.Regex("x", "i"), "$options": "m"}}
        )
    assert caught.value.code == 2
    with pytest.raises(OperationFailure, match="supports only") as caught:
        matches_filter({"value": "x"}, {"value": {"$regex": "x", "$options": "z"}})
    assert caught.value.code == 51108
    with pytest.raises(OperationFailure, match="embedded") as caught:
        matches_filter(
            {"value": "x"},
            {
                "value": {
                    "$regex": bson.Regex("x", "i"),
                    "$options": "m",
                }
            },
        )
    assert caught.value.code == 2


def test_regex_locale_flag_can_still_match_an_exact_stored_regex():
    expression = bson.Regex("exact", "l")

    assert matches_filter({"value": expression}, {"value": expression})
    assert not matches_filter({"value": "exact"}, {"value": expression})


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ({"value": {"$regex": 42}}, 2),
        ({"value": {"$regex": b"bytes"}}, 2),
        ({"value": {"$regex": "["}}, 51091),
        ({"value": {"$regex": "nul\x00pattern"}}, 2),
        ({"value": bson.Regex("[")}, 51091),
        ({"value": bson.Regex("nul\x00pattern")}, 2),
        ({"value": bson.Regex(b"\xff")}, 2),
        ({"value": {"$in": [bson.Regex("[")]}}, 51091),
        ({"value": {"$nin": [bson.Regex("[")]}}, 51091),
        ({"value": {"$all": [bson.Regex("[")]}}, 51091),
        ({"value": {"$not": bson.Regex("[")}}, 51091),
        ({"value": {"$not": {"$regex": None}}}, 2),
        ({"value": {"$in": [{"$regex": "valid"}]}}, 2),
        ({"value": {"$nin": [{"$regex": "valid"}]}}, 2),
        ({"value": {"$all": [{"$regex": "valid"}]}}, 2),
    ],
)
def test_malformed_regex_queries_fail_before_persistent_storage_reads(
    tmp_path,
    query,
    code,
):
    client = tinymongo.TinyMongoClient(str(tmp_path / "invalid"), backend="sqlite")
    collection = client.app.items

    with pytest.raises(
        OperationFailure,
        match="(?i)regex|regular.expression",
    ) as caught:
        collection.find(query)
    assert caught.value.code == code

    assert client.app.list_collection_names() == []
    client.close()


def test_public_queries_preserve_exact_locale_regex_identity():
    client = tinymongo.TinyMongoClient(backend="memory")
    collection = client.app.items
    expression = bson.Regex("exact", "l")
    collection.insert_many(
        [
            {"_id": "regex", "value": expression},
            {"_id": "string", "value": "exact"},
        ]
    )

    assert [item["_id"] for item in collection.find({"value": expression})] == ["regex"]
    assert [
        item["_id"] for item in collection.find({"value": {"$regex": expression}})
    ] == ["regex"]
    client.close()


def test_regex_only_preflight_ignores_non_query_documents():
    assert validate_regex_filter(None) is None


def test_full_filter_validation_rejects_operator_documents_inside_regex_arrays():
    with pytest.raises(OperationFailure, match="accepts regex values") as caught:
        validate_filter_operators({"value": {"$in": [{"$regex": "valid"}]}})
    assert caught.value.code == 2


def test_remote_unique_indexes_fail_closed_for_extended_binary_identities():
    value = UUID("00112233-4455-6677-8899-aabbccddeeff")
    spec = parse_index_spec("value", unique=True)

    for extended in (value, bson.Binary(value.bytes, 4), re.compile("x")):
        with pytest.raises(TinyMongoNotSupportedError, match="Remote SQL.*UUID"):
            _reject_remote_unique_values([{"value": extended}], [spec])


def test_distinct_uses_recursive_bson_identity_and_keeps_unsupported_type_rules():
    class EqualValue:
        def __eq__(self, _other):
            return True

    class OtherEqualValue:
        def __eq__(self, _other):
            return True

    first = EqualValue()
    second = EqualValue()
    other_type = OtherEqualValue()

    class Collection:
        @staticmethod
        def find(_filter):
            return [
                {"value": {"number": 1}},
                {"value": {"number": 1.0}},
                {"value": {"number": True}},
                {"value": first},
                {"value": second},
                {"value": other_type},
            ]

    values = TinyMongoCollection.distinct(Collection(), "value")

    assert values[:2] == [{"number": 1}, {"number": True}]
    assert len(values) == 4
    assert type(values[2]) is EqualValue
    assert type(values[3]) is OtherEqualValue
