"""High-signal compatibility contracts derived from the Talk Python application."""

from datetime import datetime, timedelta, timezone

import pytest

from tinymongo.indexes import TinyMongoUnsupportedWarning

from .support import observe


pytestmark = pytest.mark.contract


def _ids(documents):
    return [document["_id"] for document in documents]


def test_projection_none_absent_fields_and_integer_id(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": 42,
            "title": "Python Bytes",
            "description": "A short episode summary",
        }
    )

    full = collection.find_one({"_id": 42}, None)
    projected = collection.find_one({"_id": 42}, {"title": 1, "_id": 0})

    assert full == {
        "_id": 42,
        "title": "Python Bytes",
        "description": "A short episode summary",
    }
    assert type(full["_id"]) is int
    assert projected == {"title": "Python Bytes"}
    assert "description" not in projected
    assert collection.find_one({"_id": 404}) is None


def test_find_one_sort_returns_the_newest_match(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "show_id": 201, "is_published": True},
            {"_id": 2, "show_id": 203, "is_published": True},
            {"_id": 3, "show_id": 202, "is_published": False},
        ]
    )

    newest = collection.find_one({"is_published": True}, sort=[("show_id", -1)])

    assert newest["_id"] == 2


def test_single_and_multi_key_sort_break_ties(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "show_id": 101, "times": 5, "last_missed": 10},
            {"_id": 2, "show_id": 103, "times": 5, "last_missed": 30},
            {"_id": 3, "show_id": 102, "times": 4, "last_missed": 99},
        ]
    )

    by_show_id = collection.find({}).sort("show_id", -1)
    by_miss_count = collection.find({}).sort([("times", -1), ("last_missed", -1)])

    assert _ids(by_show_id) == [2, 3, 1]
    assert _ids(by_miss_count) == [2, 1, 3]


def test_cursor_to_list_accepts_both_application_spellings(contract_target):
    collection = contract_target.collection
    collection.insert_many([{"_id": number} for number in range(3)])

    implicit_length = collection.find({}).sort("_id", 1).to_list()
    unlimited_length = collection.find({}).sort("_id", 1).to_list(length=None)

    assert isinstance(implicit_length, list)
    assert isinstance(unlimited_length, list)
    assert _ids(implicit_length) == [0, 1, 2]
    assert _ids(unlimited_length) == [0, 1, 2]


def test_cursor_skip_limit_windows_and_past_end(contract_target):
    collection = contract_target.collection
    collection.insert_many([{"_id": number} for number in range(6)])

    window = collection.find({}).sort("_id", 1).skip(2).limit(3).to_list(length=None)
    past_end = collection.find({}).sort("_id", 1).skip(20).to_list(length=None)

    assert _ids(window) == [2, 3, 4]
    assert past_end == []


def test_cursor_limit_zero_means_unlimited(contract_target):
    collection = contract_target.collection
    collection.insert_many([{"_id": number} for number in range(4)])

    documents = list(collection.find({}).sort("_id", 1).limit(0))

    assert _ids(documents) == [0, 1, 2, 3]


def test_scalar_equality_matches_an_array_member(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "guest_ids": ["mike", "brian"]},
            {"_id": 2, "guest_ids": ["carol"]},
            {"_id": 3, "guest_ids": "mike"},
        ]
    )

    assert sorted(_ids(collection.find({"guest_ids": "mike"}))) == [1, 3]


def test_dot_notation_matches_an_embedded_document(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "membership": {"memberful_id": "member-42"}},
            {"_id": 2, "membership": {"memberful_id": "member-99"}},
            {"_id": 3},
        ]
    )

    assert collection.find_one({"membership.memberful_id": "member-42"})["_id"] == 1


def test_not_regex_with_case_insensitive_options(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "title": "Talk Python To Me"},
            {"_id": 2, "title": "PYTHON NEWS"},
            {"_id": 3, "title": "MongoDB internals"},
            {"_id": 4},
        ]
    )

    documents = collection.find(
        {"title": {"$not": {"$regex": "python", "$options": "i"}}}
    )

    assert sorted(_ids(documents)) == [3, 4]


def test_nin_excludes_values_and_includes_missing_fields(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "status": "public"},
            {"_id": 2, "status": "private"},
            {"_id": 3, "status": "archived"},
            {"_id": 4},
        ]
    )

    documents = collection.find({"status": {"$nin": ["private", "archived"]}})

    assert sorted(_ids(documents)) == [1, 4]


def test_null_negation_distinguishes_missing_and_non_null_fields(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "value": "real"},
            {"_id": 2, "value": ""},
            {"_id": 3, "value": None},
            {"_id": 4},
            {"_id": 5, "value": "other"},
        ]
    )

    expectations = [
        ({"value": {"$nin": [None, ""]}}, [1, 5]),
        ({"value": {"$nin": (None, "")}}, [1, 5]),
        ({"value": {"$ne": None}}, [1, 2, 5]),
        ({"value": {"$ne": ""}}, [1, 3, 4, 5]),
        ({"value": {"$nin": ["real"]}}, [2, 3, 4, 5]),
        ({"value": {"$not": {"$regex": "^r"}}}, [2, 3, 4, 5]),
        ({"value": {"$exists": True}}, [1, 2, 3, 5]),
        ({"value": {"$exists": False}}, [4]),
    ]

    for query, expected_ids in expectations:
        assert sorted(_ids(collection.find(query))) == expected_ids


def test_nin_and_negated_regex_share_one_field_specification(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "title": "Private episode"},
            {"_id": 2, "title": "Python News"},
            {"_id": 3, "title": "MongoDB internals"},
            {"_id": 4},
        ]
    )

    documents = collection.find(
        {
            "title": {
                "$nin": ["Private episode"],
                "$not": {"$regex": "python", "$options": "i"},
            }
        }
    )

    assert sorted(_ids(documents)) == [3, 4]


def test_write_result_metadata_used_by_the_application(contract_target):
    collection = contract_target.collection

    inserted = collection.insert_one({"_id": 1, "status": "ready"})
    unchanged = collection.update_one({"_id": 1}, {"$set": {"status": "ready"}})
    missing = collection.update_one({"_id": 404}, {"$set": {"status": "ready"}})
    collection.insert_many(
        [
            {"_id": 2, "status": "delete"},
            {"_id": 3, "status": "delete"},
        ]
    )
    deleted_one = collection.delete_one({"_id": 1})
    deleted_many = collection.delete_many({"status": "delete"})

    assert inserted.inserted_id == 1
    assert (unchanged.matched_count, unchanged.modified_count) == (1, 0)
    assert (missing.matched_count, missing.modified_count) == (0, 0)
    assert deleted_one.deleted_count == 1
    assert deleted_many.deleted_count == 2


def test_inc_creates_a_missing_counter(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": 1, "show_id": 42})

    result = collection.update_one({"show_id": 42}, {"$inc": {"download_totals": 1}})

    assert (result.matched_count, result.modified_count) == (1, 1)
    assert collection.find_one({"_id": 1})["download_totals"] == 1


def test_replace_one_preserves_id_and_replaces_the_full_document(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {"_id": 1, "title": "Old title", "obsolete": True, "views": 10}
    )

    replaced = collection.replace_one({"_id": 1}, {"title": "New title", "views": 11})
    unchanged = collection.replace_one(
        {"_id": 1}, {"_id": 1, "title": "New title", "views": 11}
    )
    missing = collection.replace_one({"_id": 404}, {"title": "Missing"})

    assert (replaced.matched_count, replaced.modified_count) == (1, 1)
    assert (unchanged.matched_count, unchanged.modified_count) == (1, 0)
    assert (missing.matched_count, missing.modified_count) == (0, 0)
    assert collection.find_one({"_id": 1}) == {
        "_id": 1,
        "title": "New title",
        "views": 11,
    }


def test_distinct_returns_scalar_field_values(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "user_agent": "Firefox"},
            {"_id": 2, "user_agent": "Safari"},
            {"_id": 3, "user_agent": "Firefox"},
            {"_id": 4},
        ]
    )

    assert sorted(collection.distinct("user_agent")) == ["Firefox", "Safari"]


def test_create_indexes_accepts_the_real_mixed_batch_and_enforces_unique(
    contract_target,
):
    pymongo = pytest.importorskip("pymongo")
    collection = contract_target.collection
    models = [
        pymongo.IndexModel([("show_id", -1)], name="newest_show"),
        pymongo.IndexModel([("is_published", 1)], name="is_published"),
        pymongo.IndexModel(
            [("is_published", 1), ("show_id", -1)],
            name="published_show_priority",
        ),
        pymongo.IndexModel([("api_key", "hashed")], name="api_key_lookup"),
        pymongo.IndexModel([("optional_slug", 1)], name="optional_slug", sparse=True),
        pymongo.IndexModel([("email", 1)], name="email_unique", unique=True),
        pymongo.IndexModel(
            [("created_date", 1)],
            name="expire_geo_lookup",
            expireAfterSeconds=63_072_000,
        ),
    ]

    if contract_target.name == "mongodb":
        created = collection.create_indexes(models)
    else:
        with pytest.warns(TinyMongoUnsupportedWarning) as captured:
            created = collection.create_indexes(models)
        assert len(captured) == 5
        warning_messages = [str(item.message) for item in captured]
        assert any("newest_show" in message for message in warning_messages)
        assert any("published_show_priority" in message for message in warning_messages)
        assert any(
            "existing index 'is_published'" in message for message in warning_messages
        )
        assert any("api_key_lookup" in message for message in warning_messages)
        assert any("optional_slug" in message for message in warning_messages)
        assert any("expire_geo_lookup" in message for message in warning_messages)
    collection.insert_one({"_id": 1, "email": "mike@example.com"})
    duplicate = observe(
        lambda: collection.insert_one({"_id": 2, "email": "mike@example.com"})
    )

    expected_created = {
        "newest_show",
        "is_published",
        "api_key_lookup",
        "optional_slug",
        "email_unique",
        "expire_geo_lookup",
    }
    if contract_target.name == "mongodb":
        expected_created.add("published_show_priority")
    assert set(created) == expected_created
    assert duplicate.error == "duplicate_key"


def test_object_id_and_datetime_round_trip_and_range_query(contract_target):
    bson = pytest.importorskip("bson")
    collection = contract_target.collection
    episode_id = bson.ObjectId()
    created_date = datetime(2025, 2, 3, 12, 30, 45, 123000)
    collection.insert_one(
        {"_id": episode_id, "title": "Async Python", "created_date": created_date}
    )

    document = collection.find_one({"_id": episode_id})
    in_range = list(
        collection.find(
            {
                "created_date": {
                    "$gt": created_date - timedelta(seconds=1),
                    "$lt": created_date + timedelta(seconds=1),
                }
            }
        )
    )

    assert document["_id"] == episode_id
    assert isinstance(document["_id"], bson.ObjectId)
    assert document["created_date"] == created_date
    assert isinstance(document["created_date"], datetime)
    assert _ids(in_range) == [episode_id]


def test_generated_id_uses_the_standard_object_id_round_trip(contract_target):
    bson = pytest.importorskip("bson")
    collection = contract_target.collection
    document = {"title": "A newly-created episode"}

    result = collection.insert_one(document)
    reconstructed = bson.ObjectId(str(result.inserted_id))
    found = collection.find_one({"_id": reconstructed})

    assert isinstance(result.inserted_id, bson.ObjectId)
    assert document["_id"] == result.inserted_id
    assert len(str(result.inserted_id)) == 24
    assert reconstructed == result.inserted_id
    assert found["title"] == document["title"]


def test_invalid_document_has_a_bson_compatible_error(contract_target):
    bson_errors = pytest.importorskip("bson.errors")
    pymongo_errors = pytest.importorskip("pymongo.errors")
    collection = contract_target.collection
    document = {"payload": {"tags": {1, 2, 3}}}

    with pytest.raises(bson_errors.InvalidDocument) as caught:
        collection.insert_one(document)

    if contract_target.name != "mongodb" and contract_target.api == "sync":
        assert collection.name not in collection.database.list_collection_names()
    assert collection.count_documents({}) == 0
    if contract_target.name != "mongodb":
        assert isinstance(caught.value, pymongo_errors.PyMongoError)
        assert caught.value.document is document
        assert collection.full_name in str(caught.value)
        assert "payload" in str(caught.value)
        assert "tags" in str(caught.value)
        assert "set" in str(caught.value)


def test_binary_round_trip_query_and_mongodb_sort_order(contract_target):
    bson = pytest.importorskip("bson")
    collection = contract_target.collection
    uuid_binary = bson.Binary(bytes(range(16)), subtype=4)
    sort_values = [
        ("short-custom", bson.Binary(b"\xff", subtype=128)),
        ("two-generic", b"\xff\xff"),
        ("two-custom-low", bson.Binary(b"\x00\x00", subtype=128)),
        ("two-custom-high", bson.Binary(b"\xff\x00", subtype=128)),
        ("three-generic", b"\x00\x00\x00"),
    ]
    collection.insert_many(
        [
            {
                "_id": label,
                "label": label,
                "value": value,
                "nested": {"token": uuid_binary},
            }
            for label, value in reversed(sort_values)
        ]
    )

    by_binary = list(collection.find({}).sort("value", 1))
    matched = collection.find_one({"nested.token": uuid_binary})

    assert [document["label"] for document in by_binary] == [
        label for label, _value in sort_values
    ]
    assert matched is not None
    assert isinstance(matched["value"], (bytes, bson.Binary))
    assert isinstance(matched["nested"]["token"], bson.Binary)
    assert matched["nested"]["token"].subtype == 4
    assert bytes(matched["nested"]["token"]) == bytes(uuid_binary)


def test_generic_binary_equality_matches_native_bytes_and_query_operators(
    contract_target,
):
    bson = pytest.importorskip("bson")
    collection = contract_target.collection
    generic = bson.Binary(b"talk-python", subtype=0)
    collection.insert_many(
        [
            {
                "_id": "binary-object",
                "value": generic,
                "values": [generic, "other"],
            },
            {
                "_id": "native-bytes",
                "value": b"python-bytes",
                "values": [b"python-bytes"],
            },
        ]
    )

    assert collection.find_one({"value": b"talk-python"})["_id"] == "binary-object"
    assert collection.find_one({"value": generic})["_id"] == "binary-object"
    if contract_target.name != "mongodb":
        # TinyMongo accepts bytearray as a convenience and canonicalizes it to
        # BSON's generic binary subtype. PyMongo rejects bytearray at encoding.
        assert (
            collection.find_one({"value": bytearray(b"talk-python")})["_id"]
            == "binary-object"
        )
    assert (
        collection.find_one({"value": bson.Binary(b"python-bytes", subtype=0)})["_id"]
        == "native-bytes"
    )
    assert collection.find_one({"values": generic})["_id"] == "binary-object"
    assert collection.find_one({"values": {"$in": [generic]}})["_id"] == "binary-object"
    assert (
        collection.find_one({"values": {"$all": [b"talk-python"]}})["_id"]
        == "binary-object"
    )
    assert sorted(_ids(collection.find({"value": {"$nin": [b"talk-python"]}}))) == [
        "native-bytes"
    ]


def test_binary_ids_use_bson_equality_without_losing_subtype(contract_target):
    bson = pytest.importorskip("bson")
    pymongo_errors = pytest.importorskip("pymongo.errors")
    collection = contract_target.collection
    raw = bytes(range(16))
    generic_id = bson.Binary(raw, subtype=0)
    uuid_id = bson.Binary(raw, subtype=4)

    collection.insert_one({"_id": generic_id, "kind": "generic"})

    assert collection.find_one({"_id": raw})["kind"] == "generic"
    with pytest.raises(pymongo_errors.DuplicateKeyError):
        collection.insert_one({"_id": raw, "kind": "duplicate"})

    collection.insert_one({"_id": uuid_id, "kind": "uuid"})
    assert collection.find_one({"_id": uuid_id})["kind"] == "uuid"
    assert sorted(document["kind"] for document in collection.find({})) == [
        "generic",
        "uuid",
    ]
    collection.update_one({"_id": uuid_id}, {"$set": {"updated": True}})
    assert collection.find_one({"_id": raw}).get("updated") is None
    assert collection.find_one({"_id": uuid_id})["updated"] is True
    collection.delete_one({"_id": raw})
    assert collection.find_one({"_id": raw}) is None
    assert collection.find_one({"_id": uuid_id}) is not None


def test_boolean_and_numeric_ids_are_bson_distinct(contract_target):
    pymongo_errors = pytest.importorskip("pymongo.errors")
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "kind": "number"},
            {"_id": True, "kind": "boolean"},
        ]
    )

    assert collection.find_one({"_id": 1})["kind"] == "number"
    assert collection.find_one({"_id": True})["kind"] == "boolean"
    assert collection.find_one({"_id": 1.0})["kind"] == "number"
    with pytest.raises(pymongo_errors.DuplicateKeyError):
        collection.insert_one({"_id": 1.0, "kind": "duplicate-number"})
    collection.update_one({"_id": True}, {"$set": {"updated": True}})
    assert collection.find_one({"_id": 1}).get("updated") is None
    assert collection.find_one({"_id": True})["updated"] is True
    collection.delete_one({"_id": 1.0})
    assert collection.find_one({"_id": 1}) is None
    assert collection.find_one({"_id": True}) is not None


def test_mixed_timezone_datetimes_sort_by_utc_instant(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": "late",
                "published": datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
            },
            {
                "_id": "early",
                "published": datetime(
                    2025,
                    12,
                    31,
                    21,
                    tzinfo=timezone(timedelta(hours=-3)),
                ),
            },
            {"_id": "middle", "published": datetime(2026, 1, 1, 1)},
        ]
    )

    assert _ids(collection.find({}).sort("published", 1)) == [
        "early",
        "middle",
        "late",
    ]
    assert _ids(collection.find({}).sort("published", -1)) == [
        "late",
        "middle",
        "early",
    ]


@pytest.mark.parametrize(
    ("ordered", "expected_ids", "expected_inserted"),
    [
        (True, [1], 1),
        (False, [1, 2], 2),
    ],
)
def test_insert_many_reports_compatible_partial_failures(
    contract_target,
    ordered,
    expected_ids,
    expected_inserted,
):
    pymongo_errors = pytest.importorskip("pymongo.errors")
    collection = contract_target.collection
    documents = [
        {"_id": 1, "label": "first"},
        {"_id": 1, "label": "duplicate"},
        {"_id": 2, "label": "last"},
    ]

    with pytest.raises(pymongo_errors.BulkWriteError) as caught:
        collection.insert_many(documents, ordered=ordered)

    details = caught.value.details
    assert _ids(collection.find({}).sort("_id", 1)) == expected_ids
    assert details["nInserted"] == expected_inserted
    assert details["writeConcernErrors"] == []
    assert [(error["index"], error["code"]) for error in details["writeErrors"]] == [
        (1, 11000)
    ]


def test_insert_many_accepts_a_document_generator(contract_target):
    collection = contract_target.collection
    documents = ({"_id": number, "label": str(number)} for number in range(3))

    result = collection.insert_many(documents)

    assert result.inserted_ids == [0, 1, 2]
    assert _ids(collection.find({}).sort("_id", 1)) == [0, 1, 2]


def test_duplicate_errors_are_catchable_as_pymongo_errors(contract_target):
    pymongo_errors = pytest.importorskip("pymongo.errors")
    collection = contract_target.collection
    collection.insert_one({"_id": 42})

    caught = None
    try:
        collection.insert_one({"_id": 42})
    except pymongo_errors.PyMongoError as error:
        caught = error

    assert isinstance(caught, pymongo_errors.DuplicateKeyError)
