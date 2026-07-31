import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import tinymongo as tm
from tinymongo import bson_codec, bson_types
from tinymongo.errors import InvalidDocument, TinyMongoError


bson = pytest.importorskip("bson")
ObjectId = bson.ObjectId
Binary = bson.Binary


def test_invalid_document_matches_tinymongo_bson_and_pymongo_hierarchies():
    from bson.errors import InvalidDocument as BsonInvalidDocument
    from pymongo.errors import InvalidDocument as PyMongoInvalidDocument
    from pymongo.errors import PyMongoError

    document = {"value": {1, 2}}

    with pytest.raises(InvalidDocument) as caught:
        bson_codec.dumps(document)

    error = caught.value
    assert isinstance(error, TinyMongoError)
    assert isinstance(error, BsonInvalidDocument)
    assert isinstance(error, PyMongoInvalidDocument)
    assert isinstance(error, PyMongoError)
    assert not isinstance(error, TypeError)
    assert error.document is document


def test_invalid_document_reports_nested_path_and_write_context():
    document = {
        "_id": "broken",
        "outer": {"items": [{"valid": 1}, {"unsupported": {1, 2}}]},
    }

    with pytest.raises(InvalidDocument) as caught:
        bson_codec.dumps(
            document,
            document_context="collection 'app.items', document index 4",
        )

    message = str(caught.value)
    assert caught.value.document is document
    assert "collection 'app.items', document index 4" in message
    assert "$['outer']['items'][1]['unsupported']" in message
    assert "cannot encode object" in message
    assert "<class 'set'>" in message


def test_invalid_document_bounds_the_offending_value_representation():
    class VerboseUnsupportedValue:
        def __repr__(self):
            return "unsupported-" + ("x" * 500)

    document = {"value": VerboseUnsupportedValue()}

    with pytest.raises(InvalidDocument) as caught:
        bson_codec.dumps(document)

    message = str(caught.value)
    assert "unsupported-" in message
    assert "..." in message
    assert "x" * 200 not in message


def _extended_document():
    created = datetime(
        2026, 7, 19, 9, 30, 45, 123456, tzinfo=timezone(timedelta(hours=-4))
    )
    document_id = ObjectId()
    return {
        "_id": document_id,
        "created": created,
        "nested": {
            "owner_id": ObjectId(),
            "history": [
                {"at": created - timedelta(days=1)},
                ObjectId(),
            ],
        },
    }


def test_codec_round_trips_nested_object_ids_and_aware_datetimes():
    document = _extended_document()

    restored = bson_codec.loads(bson_codec.dumps(document))

    assert restored == document
    assert isinstance(restored["_id"], ObjectId)
    assert restored["created"].tzinfo is not None
    assert restored["created"].utcoffset() == timedelta(hours=-4)
    assert isinstance(restored["nested"]["owner_id"], ObjectId)
    assert isinstance(restored["nested"]["history"][0]["at"], datetime)
    assert isinstance(restored["nested"]["history"][1], ObjectId)


def test_codec_tags_are_valid_readable_json():
    document = _extended_document()

    serialized = bson_codec.dumps(document, ensure_ascii=False, indent=2)
    encoded = json.loads(serialized)

    assert "__tinymongo_type_v1__" in serialized
    assert encoded["_id"] == {
        "__tinymongo_type_v1__": "objectid",
        "value": str(document["_id"]),
    }
    assert encoded["created"] == {
        "__tinymongo_type_v1__": "datetime",
        "value": document["created"].isoformat(),
    }


def test_codec_loads_legacy_plain_json_without_changing_it():
    legacy = '{"_id": 1, "nested": {"active": true}, "items": [null, "x"]}'

    assert bson_codec.loads(legacy) == {
        "_id": 1,
        "nested": {"active": True},
        "items": [None, "x"],
    }


def test_codec_preserves_unknown_exact_tag_mappings():
    tagged = {
        "__tinymongo_type_v1__": "future-type",
        "value": {"nested": [1, 2]},
    }

    assert bson_codec.decode_value(tagged) == tagged


@pytest.mark.parametrize(
    "payload",
    [
        "not-an-item-list",
        [["missing-value"]],
    ],
)
def test_codec_preserves_malformed_escaped_mapping_tags(payload):
    tagged = {
        "__tinymongo_type_v1__": "mapping",
        "value": payload,
    }

    assert bson_codec.decode_value(tagged) == tagged


@pytest.mark.parametrize(
    "kind", ["datetime", "objectid", "binary", "mapping", "future-type"]
)
def test_codec_escapes_user_mappings_that_look_like_internal_tags(kind):
    tagged = {
        "__tinymongo_type_v1__": kind,
        "value": "2026-01-02T03:04:05",
    }

    assert bson_codec.loads(bson_codec.dumps(tagged)) == tagged


def test_internal_tag_shaped_mapping_round_trips_through_public_storage():
    tagged = {
        "__tinymongo_type_v1__": "datetime",
        "value": "2026-01-02T03:04:05",
    }
    client = tm.TinyMongoClient(backend="memory")
    collection = client.app.events

    collection.insert_one({"_id": 1, "payload": tagged})

    assert collection.find_one({"_id": 1})["payload"] == tagged
    client.close()


def test_object_id_decode_explains_optional_dependency(monkeypatch):
    encoded = {
        "__tinymongo_type_v1__": "objectid",
        "value": str(ObjectId()),
    }
    monkeypatch.setattr(bson_codec, "_ObjectId", None)

    assert bson_codec.bson_available() is False
    with pytest.raises(ImportError, match=r"pip install 'tinymongo\[bson\]'"):
        bson_codec.decode_value(encoded)


@pytest.mark.parametrize("value", [b"\x00\xffabc", bytearray(b"\x00\xffabc")])
def test_codec_round_trips_native_binary_values_as_bytes(value):
    encoded = bson_codec.encode_value(value)

    assert encoded == {
        "__tinymongo_type_v1__": "binary",
        "value": {"base64": "AP9hYmM=", "subtype": 0},
    }
    restored = bson_codec.decode_value(encoded)
    assert restored == bytes(value)
    assert type(restored) is bytes


@pytest.mark.parametrize("subtype", [0, 3, 4, 128])
def test_codec_round_trips_bson_binary_subtypes(subtype):
    payload = bytes(range(16)) if subtype == 4 else b"\x01payload"
    value = Binary(payload, subtype=subtype)

    encoded = bson_codec.encode_value(value)
    restored = bson_codec.decode_value(encoded)

    assert encoded["value"]["subtype"] == subtype
    assert bytes(restored) == bytes(value)
    if subtype == 0:
        assert type(restored) is bytes
    else:
        assert isinstance(restored, Binary)
        assert restored.subtype == subtype


def test_binary_subclass_is_encoded_before_native_bytes():
    encoded = bson_codec.encode_value(Binary(bytes(range(16)), subtype=4))

    assert encoded["value"] == {
        "base64": "AAECAwQFBgcICQoLDA0ODw==",
        "subtype": 4,
    }


def test_generic_binary_decodes_without_optional_bson(monkeypatch):
    encoded = {
        "__tinymongo_type_v1__": "binary",
        "value": {"base64": "AAE=", "subtype": 0},
    }
    monkeypatch.setattr(bson_codec, "_Binary", None)

    assert bson_codec.decode_value(encoded) == b"\x00\x01"


def test_nonzero_binary_subtype_explains_optional_dependency(monkeypatch):
    encoded = {
        "__tinymongo_type_v1__": "binary",
        "value": {"base64": "AAE=", "subtype": 4},
    }
    monkeypatch.setattr(bson_codec, "_Binary", None)

    with pytest.raises(
        ImportError,
        match=r"Binary values with a non-zero subtype.*tinymongo\[bson\]",
    ):
        bson_codec.decode_value(encoded)


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-mapping",
        {"base64": "AAE="},
        {"base64": 123, "subtype": 0},
        {"base64": "not base64!", "subtype": 0},
        {"base64": "AAE=", "subtype": True},
        {"base64": "AAE=", "subtype": 256},
    ],
)
def test_codec_preserves_malformed_binary_tags(payload):
    tagged = {
        "__tinymongo_type_v1__": "binary",
        "value": payload,
    }

    assert bson_codec.decode_value(tagged) == tagged


@pytest.mark.parametrize(
    "value",
    [
        b"raw",
        bytearray(b"mutable"),
        Binary(bytes(range(16)), subtype=4),
        {"nested": [b"raw"]},
    ],
)
def test_binary_values_require_python_filter_comparison(value):
    assert bson_codec.contains_extended_value(value) is True


@pytest.mark.parametrize(
    ("value", "payload"),
    [
        (float("nan"), "nan"),
        (float("inf"), "infinity"),
        (float("-inf"), "-infinity"),
    ],
)
def test_nonfinite_floats_use_strict_json_tags(value, payload):
    encoded = bson_codec.encode_value(value)
    serialized = bson_codec.dumps({"value": value})
    restored = bson_codec.loads(serialized)["value"]

    assert encoded == {
        "__tinymongo_type_v1__": "float",
        "value": payload,
    }
    assert "NaN" not in serialized
    assert "Infinity" not in serialized
    assert bson_codec.contains_extended_value(value)
    if payload == "nan":
        assert restored != restored
    else:
        assert restored == value


def test_malformed_nonfinite_float_tag_is_preserved():
    tagged = {
        "__tinymongo_type_v1__": "float",
        "value": "not-a-float",
    }

    assert bson_codec.decode_value(tagged) == tagged


def test_bson_type_registry_uses_mongodb_scalar_order_and_subclass_precedence():
    assert bson_types.bson_scalar_sort_key(None) == (0, None)
    assert bson_types.bson_scalar_sort_key(1) == (1, (1, 1))
    assert bson_types.bson_scalar_sort_key("text") == (2, "text")
    assert bson_types.bson_scalar_sort_key(Binary(b"x", subtype=128)) == (
        5,
        (1, 128, b"x"),
    )
    assert bson_types.bson_scalar_sort_key(b"x") == (5, (1, 0, b"x"))
    assert bson_types.bson_scalar_sort_key(ObjectId("000000000000000000000001")) == (
        6,
        b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01",
    )
    assert bson_types.bson_type_spec(True).name == "boolean"
    assert bson_types.bson_type_spec(1).name == "number"


def test_bson_type_registry_normalizes_naive_and_aware_dates_to_utc():
    naive = datetime(2026, 7, 20, 12, 30)
    aware = datetime(
        2026,
        7,
        20,
        8,
        30,
        tzinfo=timezone(timedelta(hours=-4)),
    )

    assert bson_types.bson_scalar_sort_key(naive) == bson_types.bson_scalar_sort_key(
        aware
    )


@pytest.mark.parametrize("backend", ["memory", "tinydb", "sqlite", "duckdb", "parquet"])
def test_extended_values_round_trip_through_public_backends(tmp_path, backend):
    if backend in ("duckdb", "parquet"):
        pytest.importorskip("duckdb")

    document = _extended_document()
    if backend == "memory":
        location = "memory://bson-codec-{0}".format(uuid4().hex)
    else:
        location = str(tmp_path / backend)

    writer = tm.TinyMongoClient(location, backend=backend)
    writer.app.events.insert_one(document)
    writer.close()

    reader = tm.TinyMongoClient(location, backend=backend)
    try:
        restored = reader.app.events.find_one({"_id": document["_id"]})

        assert restored == document
        assert reader.app.events.find_one({"created": document["created"]}) == document
    finally:
        reader.close()


@pytest.mark.parametrize("backend", ["memory", "tinydb", "sqlite", "duckdb", "parquet"])
def test_binary_values_round_trip_through_public_backends(tmp_path, backend):
    if backend in ("duckdb", "parquet"):
        pytest.importorskip("duckdb")
    if backend == "parquet":
        pytest.importorskip("pyarrow")

    binary_id = b"binary-id"
    uuid_binary = Binary(bytes(range(16)), subtype=4)
    blob = b"\x89PNG\r\n\x1a\n" + (b"x" * 100_000)
    if backend == "memory":
        location = "memory://binary-codec-{0}".format(uuid4().hex)
    else:
        location = str(tmp_path / backend)

    writer = tm.TinyMongoClient(location, backend=backend)
    writer.app.assets.insert_one(
        {
            "_id": binary_id,
            "blob": blob,
            "mutable": bytearray(b"abc"),
            "nested": {"tokens": [uuid_binary]},
        }
    )
    writer.close()

    reader = tm.TinyMongoClient(location, backend=backend)
    try:
        restored = reader.app.assets.find_one({"nested.tokens": uuid_binary})

        assert restored["_id"] == binary_id
        assert type(restored["_id"]) is bytes
        assert restored["blob"] == blob
        assert restored["mutable"] == b"abc"
        assert type(restored["mutable"]) is bytes
        assert isinstance(restored["nested"]["tokens"][0], Binary)
        assert restored["nested"]["tokens"][0].subtype == 4
        assert bytes(restored["nested"]["tokens"][0]) == bytes(uuid_binary)
    finally:
        reader.close()


def test_json_backend_persists_readable_tags(tmp_path):
    location = tmp_path / "json"
    document = _extended_document()
    client = tm.TinyMongoClient(str(location), backend="tinydb")
    client.app.events.insert_one(document)
    client.close()

    raw = (location / "app.json").read_text(encoding="utf8")

    assert '"__tinymongo_type_v1__": "objectid"' in raw
    assert '"__tinymongo_type_v1__": "datetime"' in raw
    assert str(document["_id"]) in raw
    assert document["created"].isoformat() in raw
