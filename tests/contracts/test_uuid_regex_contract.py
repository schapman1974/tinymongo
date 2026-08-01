"""UUID and regular-expression contracts shared by every backend and API."""

import re
from uuid import UUID

import pytest

from tinymongo.bson_types import bson_identity_key


pytestmark = pytest.mark.contract
bson = pytest.importorskip("bson")
pymongo_errors = pytest.importorskip("pymongo.errors")
Binary = bson.Binary
Regex = bson.Regex


def _ids(cursor):
    return [document["_id"] for document in cursor]


def _standard_uuid_collection(contract_target):
    """Opt real MongoDB into the standard UUID representation for this test."""

    collection = contract_target.collection
    if contract_target.name != "mongodb":
        return collection

    codec_options = pytest.importorskip("bson.codec_options").CodecOptions(
        uuid_representation=pytest.importorskip(
            "bson.binary"
        ).UuidRepresentation.STANDARD
    )
    if contract_target.api == "sync":
        return collection.with_options(codec_options=codec_options)

    configured = collection._collection.with_options(codec_options=codec_options)
    return collection.__class__(configured, collection._runner)


def test_uuid_round_trip_uses_standard_binary_identity(contract_target):
    collection = _standard_uuid_collection(contract_target)
    first = UUID("00112233-4455-6677-8899-aabbccddeeff")
    second = UUID("00112233-4455-6677-8899-aabbccddeefe")
    first_binary = Binary(first.bytes, subtype=4)

    collection.insert_many(
        [
            {"_id": "second", "value": second},
            {"_id": "first", "value": first},
        ]
    )

    restored = collection.find_one({"value": first_binary})["value"]
    assert restored == first
    assert type(restored) is UUID
    assert _ids(collection.find({}).sort("value", 1)) == ["second", "first"]
    collection.insert_one({"_id": "binary-equivalent", "value": first_binary})
    distinct = collection.distinct("value")
    assert len(distinct) == 2
    assert {bson_identity_key(value) for value in distinct} == {
        bson_identity_key(first),
        bson_identity_key(second),
    }

    collection.insert_one({"_id": first, "kind": "uuid-id"})
    assert collection.find_one({"_id": first_binary})["kind"] == "uuid-id"
    with pytest.raises(pymongo_errors.DuplicateKeyError):
        collection.insert_one({"_id": first_binary, "kind": "duplicate-id"})


def test_uuid_and_subtype_four_binary_share_unique_identity(contract_target):
    collection = _standard_uuid_collection(contract_target)
    value = UUID("00112233-4455-6677-8899-aabbccddeeff")
    collection.create_index("value", unique=True)
    collection.insert_one({"_id": "uuid", "value": value})

    with pytest.raises(pymongo_errors.DuplicateKeyError):
        collection.insert_one(
            {"_id": "binary", "value": Binary(value.bytes, subtype=4)}
        )

    collection.insert_one(
        {"_id": "legacy-binary", "value": Binary(value.bytes, subtype=3)}
    )
    assert collection.count_documents({}) == 2


def test_regex_predicates_and_exact_equality_follow_mongodb(contract_target):
    collection = contract_target.collection
    native = re.compile("Ab.c", re.IGNORECASE)
    bson_i = Regex("Ab.c", "i")
    documents = [
        {"_id": "string", "value": "Abxc"},
        {"_id": "array", "value": ["no", "Abxc"]},
        {"_id": "native", "value": native},
        {"_id": "bson", "value": bson_i},
        {"_id": "other", "value": Regex("Z", 0)},
    ]
    collection.insert_many(documents)

    assert sorted(_ids(collection.find({"value": bson_i}))) == [
        "array",
        "bson",
        "string",
    ]
    assert sorted(_ids(collection.find({"value": native}))) == [
        "array",
        "native",
        "string",
    ]
    assert sorted(_ids(collection.find({"value": {"$regex": bson_i}}))) == [
        "array",
        "bson",
        "string",
    ]
    assert sorted(
        _ids(collection.find({"value": {"$regex": "Ab.c", "$options": "i"}}))
    ) == ["array", "bson", "string"]
    assert sorted(
        _ids(collection.find({"value": {"$regex": "Ab.c", "$options": "iu"}}))
    ) == ["array", "native", "string"]
    assert _ids(collection.find({"value": {"$eq": bson_i}})) == ["bson"]
    assert sorted(_ids(collection.find({"value": {"$in": [bson_i]}}))) == [
        "array",
        "bson",
        "string",
    ]
    assert sorted(_ids(collection.find({"value": {"$nin": [bson_i]}}))) == [
        "native",
        "other",
    ]
    assert sorted(_ids(collection.find({"value": {"$all": [bson_i]}}))) == [
        "array",
        "bson",
        "string",
    ]
    assert _ids(collection.find({"value": {"$all": []}})) == []
    assert sorted(_ids(collection.find({"value": {"$not": bson_i}}))) == [
        "native",
        "other",
    ]

    restored_native = collection.find_one({"_id": "native"})["value"]
    restored_bson = collection.find_one({"_id": "bson"})["value"]
    if contract_target.name == "mongodb":
        assert isinstance(restored_native, Regex)
    else:
        assert isinstance(restored_native, type(native))
        assert restored_native.pattern == native.pattern
        assert restored_native.flags == native.flags
    assert isinstance(restored_bson, Regex)
    assert bson_identity_key(restored_native) == bson_identity_key(native)
    assert bson_identity_key(restored_bson) == bson_identity_key(bson_i)


def test_regex_sort_distinct_and_unique_index_identity(contract_target):
    collection = contract_target.collection
    values = [
        ("u", Regex("same", "u")),
        ("iu", Regex("same", "iu")),
        ("imu", Regex("same", "imu")),
        ("im", Regex("same", "im")),
        ("i", Regex("same", "i")),
        ("none", Regex("same", "")),
    ]
    collection.insert_many(
        {"_id": label, "value": value} for label, value in reversed(values)
    )

    assert _ids(collection.find({}).sort("value", 1)) == [
        "none",
        "i",
        "im",
        "imu",
        "iu",
        "u",
    ]
    collection.insert_many(
        [
            {"_id": "duplicate-im", "value": Regex("same", "mi")},
            {
                "_id": "native-iu",
                "value": re.compile("same", re.IGNORECASE),
            },
        ]
    )
    assert len(collection.distinct("value")) == len(values)

    collection.delete_many({})
    collection.create_index("value", unique=True)
    native = re.compile("unique", re.IGNORECASE)
    collection.insert_one({"_id": "native", "value": native})
    with pytest.raises(pymongo_errors.DuplicateKeyError):
        collection.insert_one({"_id": "bson", "value": Regex("unique", "iu")})


def test_local_regex_ids_use_exact_identity_for_duplicate_detection(contract_target):
    if contract_target.name == "mongodb":
        pytest.skip(
            "MongoDB forbids Regex values in _id; TinyMongo retains this extension"
        )
    collection = contract_target.collection
    regex_id = Regex("^alpha$", "i")

    collection.insert_one({"_id": "ALPHA", "kind": "string"})
    collection.insert_one({"_id": regex_id, "kind": "regex"})

    assert collection.count_documents({}) == 2
    assert collection.find_one({"_id": {"$eq": regex_id}})["kind"] == "regex"
    with pytest.raises(pymongo_errors.DuplicateKeyError):
        collection.insert_one({"_id": Regex("^alpha$", "i"), "kind": "duplicate"})

    native_identity = re.compile("native", re.IGNORECASE)
    collection.insert_one({"_id": native_identity, "kind": "native"})
    with pytest.raises(pymongo_errors.DuplicateKeyError):
        collection.insert_one({"_id": Regex("native", "iu"), "kind": "duplicate"})
