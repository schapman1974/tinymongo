"""High-signal compatibility contracts derived from the Talk Python application."""

from datetime import datetime, timedelta

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
        assert any("api_key_lookup" in message for message in warning_messages)
        assert any("optional_slug" in message for message in warning_messages)
        assert any("expire_geo_lookup" in message for message in warning_messages)
    collection.insert_one({"_id": 1, "email": "mike@example.com"})
    duplicate = observe(
        lambda: collection.insert_one({"_id": 2, "email": "mike@example.com"})
    )

    assert set(created) == {
        "newest_show",
        "is_published",
        "published_show_priority",
        "api_key_lookup",
        "optional_slug",
        "email_unique",
        "expire_geo_lookup",
    }
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
