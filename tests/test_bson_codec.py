import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import tinymongo as tm
from tinymongo import bson_codec


ObjectId = pytest.importorskip("bson").ObjectId


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


@pytest.mark.parametrize("kind", ["datetime", "objectid", "mapping", "future-type"])
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
