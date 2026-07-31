"""Regressions reduced from Mike Kennedy's Talk Python acceptance run."""

from datetime import date, datetime, timedelta, timezone

import pytest

import tinymongo as tm
from tinymongo import bson_types
from tinymongo.errors import InvalidDocument
from tinymongo.indexes import TinyMongoUnsupportedWarning


bson = pytest.importorskip("bson")
Binary = bson.Binary
ObjectId = bson.ObjectId

BACKENDS = ("memory", "tinydb", "sqlite")
INSERTION_ORDER = (3, 1, 5, 2, 4)


@pytest.fixture(params=BACKENDS)
def talkpython_collection(request, tmp_path):
    """Return the same isolated collection on each backend Mike exercised."""

    client = tm.TinyMongoClient(
        str(tmp_path / request.param),
        backend=request.param,
    )
    try:
        yield client.talkpython.documents
    finally:
        client.close()


def _labels(cursor):
    return [document["label"] for document in cursor]


def test_implicit_ids_are_objectids_across_every_write_path(talkpython_collection):
    collection = talkpython_collection
    single = collection.insert_one({"kind": "single"})
    many = collection.insert_many([{"kind": "many-a"}, {"kind": "many-b"}])
    updated = collection.update_one(
        {"kind": "update-one"},
        {"$set": {"created": True}},
        upsert=True,
    )
    updated_many = collection.update_many(
        {"kind": "update-many"},
        {"$set": {"created": True}},
        upsert=True,
    )
    replaced = collection.replace_one(
        {"kind": "replacement"},
        {"kind": "replacement", "created": True},
        upsert=True,
    )
    generated_ids = [
        single.inserted_id,
        *many.inserted_ids,
        updated.upserted_id,
        updated_many.upserted_id,
        replaced.upserted_id,
    ]

    assert all(isinstance(document_id, ObjectId) for document_id in generated_ids)
    for document_id in generated_ids:
        reconstructed = ObjectId(str(document_id))
        assert collection.find_one({"_id": reconstructed})["_id"] == document_id


def test_explicit_string_generator_and_core_fallback_remain_available(monkeypatch):
    explicit = tm.generate_id()

    assert type(explicit) is str
    assert len(explicit) == 32
    int(explicit, 16)

    monkeypatch.setattr(bson_types, "_ObjectId", None)
    client = tm.TinyMongoClient(backend="memory")
    try:
        result = client.app.items.insert_one({"kind": "core-only"})

        assert type(result.inserted_id) is str
        assert len(result.inserted_id) == 32
        assert client.app.items.find_one({"_id": result.inserted_id}) is not None
    finally:
        client.close()


@pytest.mark.parametrize("operation", ["update_one", "update_many", "replace_one"])
def test_invalid_write_payload_fails_even_without_a_matching_document(
    talkpython_collection, operation
):
    collection = talkpython_collection

    with pytest.raises(InvalidDocument) as caught:
        if operation == "replace_one":
            collection.replace_one(
                {"_id": "missing"},
                {"value": {1, 2}},
            )
        else:
            getattr(collection, operation)(
                {"_id": "missing"},
                {"$unset": {"value": {1, 2}}},
            )

    assert "set" in str(caught.value)
    assert collection.count_documents({}) == 0


def test_datetime_and_object_id_sort_in_both_directions(talkpython_collection):
    """TM-003: supported BSON values must not collapse to insertion order."""

    collection = talkpython_collection
    base = datetime(2026, 1, 1)
    object_ids = {
        number: ObjectId("{0:024x}".format(number)) for number in INSERTION_ORDER
    }
    collection.insert_many(
        [
            {
                "_id": object_ids[number],
                "label": number,
                "published": base + timedelta(days=number),
            }
            for number in INSERTION_ORDER
        ]
    )

    for field in ("published", "_id"):
        assert _labels(collection.find({}).sort(field, 1)) == [1, 2, 3, 4, 5]
        assert _labels(collection.find({}).sort(field, -1)) == [5, 4, 3, 2, 1]


def test_binary_sort_uses_mongodb_length_subtype_and_byte_order(
    talkpython_collection,
):
    """BinData sorts by length, then subtype, then unsigned byte content."""

    collection = talkpython_collection
    values = {
        1: Binary(b"\xff", 128),
        2: Binary(b"\xff\xff", 0),
        3: Binary(b"\x00\x00", 128),
        4: Binary(b"\xff\x00", 128),
        5: Binary(b"\x00\x00\x00", 0),
    }
    collection.insert_many(
        [
            {"_id": "binary-{0}".format(label), "label": label, "value": values[label]}
            for label in (4, 1, 5, 3, 2)
        ]
    )

    assert _labels(collection.find({}).sort("value", 1)) == [1, 2, 3, 4, 5]
    assert _labels(collection.find({}).sort("value", -1)) == [5, 4, 3, 2, 1]


def test_mixed_naive_and_aware_datetimes_sort_as_utc(talkpython_collection):
    """Naive dates are UTC and aware dates compare by their UTC instant."""

    collection = talkpython_collection
    collection.insert_many(
        [
            {
                "_id": "late",
                "label": "late",
                "published": datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
            },
            {
                "_id": "early",
                "label": "early",
                "published": datetime(
                    2025,
                    12,
                    31,
                    21,
                    tzinfo=timezone(timedelta(hours=-3)),
                ),
            },
            {
                "_id": "middle",
                "label": "middle",
                "published": datetime(2026, 1, 1, 1),
            },
        ]
    )

    assert _labels(collection.find({}).sort("published", 1)) == [
        "early",
        "middle",
        "late",
    ]
    assert _labels(collection.find({}).sort("published", -1)) == [
        "late",
        "middle",
        "early",
    ]


def test_compound_sort_uses_datetime_to_break_ties(talkpython_collection):
    """The BSON sort key must also work in the cursor's multi-key path."""

    collection = talkpython_collection
    collection.insert_many(
        [
            {
                "_id": 1,
                "label": 1,
                "group": "a",
                "published": datetime(2026, 1, 2),
            },
            {
                "_id": 2,
                "label": 2,
                "group": "b",
                "published": datetime(2026, 1, 3),
            },
            {
                "_id": 3,
                "label": 3,
                "group": "a",
                "published": datetime(2026, 1, 3),
            },
            {
                "_id": 4,
                "label": 4,
                "group": "b",
                "published": datetime(2026, 1, 1),
            },
        ]
    )

    assert _labels(collection.find({}).sort([("group", 1), ("published", -1)])) == [
        3,
        1,
        2,
        4,
    ]


def test_unsupported_sort_type_warns_once_per_field_and_type():
    """An unsupported sort must be visible without warning for every document."""

    cursor = tm.TinyMongoCursor(
        [
            {"_id": 1, "published": date(2026, 1, 3)},
            {"_id": 2, "published": date(2026, 1, 1)},
            {"_id": 3, "published": date(2026, 1, 2)},
        ]
    )

    with pytest.warns(TinyMongoUnsupportedWarning) as caught:
        cursor.sort("published", 1)
        cursor.sort("published", -1)

    assert len(caught) == 1
    message = str(caught[0].message)
    assert "published" in message
    assert "date" in message
