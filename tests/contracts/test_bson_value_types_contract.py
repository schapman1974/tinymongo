"""TM-035 BSON value fidelity across every synchronous and asynchronous target."""

from collections import OrderedDict
from datetime import datetime, timezone
import re

import pytest

from tinymongo.bson_types import bson_identity_key
from tinymongo.errors import DuplicateKeyError as TinyMongoDuplicateKeyError


pytestmark = pytest.mark.contract
bson = pytest.importorskip("bson")
Code = bson.Code
MaxKey = bson.MaxKey
MinKey = bson.MinKey
Timestamp = bson.Timestamp

_DUPLICATE_ERRORS = (TinyMongoDuplicateKeyError,)
try:
    from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError
except ImportError:  # pragma: no cover - optional dependency guard
    pass
else:
    _DUPLICATE_ERRORS += (PyMongoDuplicateKeyError,)


def _ids(rows):
    return {row["_id"] for row in rows}


def test_tm035_new_bson_values_round_trip_at_every_depth(contract_target):
    collection = contract_target.collection
    native_regex = re.compile("native", re.IGNORECASE | re.MULTILINE)
    scoped_code = Code(
        "return nested;",
        {
            "timestamp": Timestamp(1000, 3),
            "bounds": [MinKey(), MaxKey()],
            "nested": {"code": Code("return inner;")},
        },
    )
    collection.insert_one(
        {
            "_id": "round-trip",
            "minimum": MinKey(),
            "maximum": MaxKey(),
            "timestamp": Timestamp(1_700_000_000, 17),
            "code": Code("return value;"),
            "nested": [{"scoped": scoped_code}],
            "native_regex": native_regex,
        }
    )

    restored = collection.find_one({"_id": "round-trip"})

    assert type(restored["minimum"]) is MinKey
    assert type(restored["maximum"]) is MaxKey
    assert restored["timestamp"] == Timestamp(1_700_000_000, 17)
    assert type(restored["timestamp"]) is Timestamp
    assert restored["code"] == Code("return value;")
    assert type(restored["code"]) is Code

    restored_scoped = restored["nested"][0]["scoped"]
    assert type(restored_scoped) is Code
    assert restored_scoped == scoped_code
    assert type(restored_scoped.scope["timestamp"]) is Timestamp
    assert type(restored_scoped.scope["bounds"][0]) is MinKey
    assert type(restored_scoped.scope["bounds"][1]) is MaxKey
    assert type(restored_scoped.scope["nested"]["code"]) is Code

    restored_regex = restored["native_regex"]
    if contract_target.name == "mongodb":
        assert type(restored_regex) is bson.Regex
    else:
        assert type(restored_regex) is type(native_regex)
    assert restored_regex.pattern == native_regex.pattern
    assert int(restored_regex.flags) == int(native_regex.flags)


def test_tm035_exact_equality_and_distinct_use_bson_identity(contract_target):
    collection = contract_target.collection
    values = [
        ("string", "same"),
        ("code", Code("same")),
        ("scoped-a", Code("same", {"answer": 1})),
        ("scoped-b", Code("same", {"answer": 2})),
        ("timestamp", Timestamp(1000, 1)),
        ("minimum", MinKey()),
        ("maximum", MaxKey()),
    ]
    collection.insert_many({"_id": label, "value": value} for label, value in values)

    assert _ids(collection.find({"value": {"$eq": "same"}})) == {"string"}
    assert _ids(collection.find({"value": {"$eq": Code("same")}})) == {"code"}
    assert _ids(collection.find({"value": {"$eq": Code("same", {"answer": 1})}})) == {
        "scoped-a"
    }
    assert _ids(collection.find({"value": {"$eq": Timestamp(1000, 1)}})) == {
        "timestamp"
    }
    assert _ids(collection.find({"value": {"$eq": MinKey()}})) == {"minimum"}
    assert _ids(collection.find({"value": {"$eq": MaxKey()}})) == {"maximum"}

    distinct = collection.distinct("value")
    assert len(distinct) == len(values)
    assert {bson_identity_key(value) for value in distinct} == {
        bson_identity_key(value) for _label, value in values
    }


@pytest.mark.parametrize(
    ("label", "value", "alias", "code"),
    [
        ("minimum", MinKey(), "minKey", -1),
        ("timestamp", Timestamp(1000, 1), "timestamp", 17),
        ("code", Code("return 1;"), "javascript", 13),
        (
            "scoped-code",
            Code("return value;", {"value": 1}),
            "javascriptWithScope",
            15,
        ),
        ("maximum", MaxKey(), "maxKey", 127),
    ],
)
def test_tm035_type_aliases_and_numeric_codes_match_mongodb(
    contract_target,
    label,
    value,
    alias,
    code,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": label, "value": value},
            {"_id": "ordinary-string", "value": "return 1;"},
            {"_id": "ordinary-number", "value": 17},
        ]
    )

    assert _ids(collection.find({"value": {"$type": alias}})) == {label}
    assert _ids(collection.find({"value": {"$type": code}})) == {label}


def test_tm035_sort_uses_the_complete_supported_scalar_order(contract_target):
    collection = contract_target.collection
    ordered = [
        ("minimum", MinKey()),
        ("empty-array", []),
        ("null", None),
        ("number", 1),
        ("string", "text"),
        ("object", {"value": 1}),
        ("binary", bson.Binary(b"x")),
        ("object-id", bson.ObjectId("000000000000000000000001")),
        ("boolean", False),
        ("date", datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)),
        ("timestamp", Timestamp(1000, 1)),
        ("regex", bson.Regex("pattern")),
        ("code", Code("return 1;")),
        ("scoped-code", Code("return scoped;", {"value": 1})),
        ("maximum", MaxKey()),
    ]
    collection.insert_many(
        {"_id": label, "value": value} for label, value in reversed(ordered)
    )

    assert [
        row["_id"] for row in collection.find({}).sort([("value", 1), ("_id", 1)])
    ] == [label for label, _value in ordered]
    assert [
        row["_id"] for row in collection.find({}).sort([("value", -1), ("_id", 1)])
    ] == [label for label, _value in reversed(ordered)]


def test_tm035_unique_index_distinguishes_code_from_text_and_scope(
    contract_target,
):
    collection = contract_target.collection
    collection.create_index("value", unique=True)
    collection.insert_many(
        [
            {"_id": "string", "value": "same"},
            {"_id": "code", "value": Code("same")},
            {"_id": "scoped", "value": Code("same", {"answer": 1})},
        ]
    )

    assert collection.count_documents({}) == 3
    with pytest.raises(_DUPLICATE_ERRORS):
        collection.insert_one({"_id": "duplicate-code", "value": Code("same")})
    with pytest.raises(_DUPLICATE_ERRORS):
        collection.insert_one(
            {
                "_id": "duplicate-scoped",
                "value": Code("same", {"answer": 1}),
            }
        )


@pytest.mark.client_options(document_class=OrderedDict, tz_aware=True)
def test_tm035_scoped_code_honors_recursive_client_read_options(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": "options",
            "script": Code(
                "return value;",
                {
                    "nested": {"value": 1},
                    "at": datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
                },
            ),
        }
    )

    restored = collection.find_one({"_id": "options"})
    script = restored["script"]

    assert type(restored) is OrderedDict
    assert type(script) is Code
    assert type(script.scope) is OrderedDict
    assert type(script.scope["nested"]) is OrderedDict
    assert script.scope["at"].utcoffset() == timezone.utc.utcoffset(None)
