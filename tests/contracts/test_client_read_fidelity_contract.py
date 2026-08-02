"""Configured-client read contracts shared with a real MongoDB server."""

from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import pytest


pytestmark = [
    pytest.mark.contract,
    pytest.mark.client_options(document_class=OrderedDict, tz_aware=True),
]


_STORED = datetime(
    2026,
    1,
    2,
    3,
    4,
    5,
    123456,
    tzinfo=timezone(timedelta(hours=-5)),
)
_UTC_MILLIS = datetime(
    2026,
    1,
    2,
    8,
    4,
    5,
    123000,
    tzinfo=timezone.utc,
)


def _assert_recursive_class(document):
    assert type(document) is OrderedDict
    assert type(document["inner"]) is OrderedDict
    assert type(document["items"][0]) is OrderedDict
    assert type(document["items"][0]["deep"]) is OrderedDict


def test_configured_document_and_datetime_results_match_mongodb(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": "main",
            "when": _STORED,
            "inner": {"when": _STORED},
            "items": [{"deep": {"when": _STORED}}],
        }
    )

    found = collection.find_one({"_id": "main"})
    _assert_recursive_class(found)
    assert found["when"] == _UTC_MILLIS
    assert found["when"].utcoffset() == timedelta(0)
    assert found["inner"]["when"] == _UTC_MILLIS
    assert found["items"][0]["deep"]["when"] == _UTC_MILLIS

    projected = list(
        collection.find(
            {"_id": "main"},
            {"inner": 1, "items": 1, "when": 1},
        )
    )[0]
    _assert_recursive_class(projected)

    aggregated = list(
        collection.aggregate(
            [
                {"$match": {"_id": "main"}},
                {"$project": {"inner": 1, "items": 1, "when": 1}},
            ]
        )
    )[0]
    _assert_recursive_class(aggregated)

    distinct = collection.distinct("inner")
    assert len(distinct) == 1
    assert type(distinct[0]) is OrderedDict
    assert distinct[0]["when"] == _UTC_MILLIS

    collection.insert_one({"_id": "modify", "inner": {"value": 1}})
    modified = collection.find_one_and_update(
        {"_id": "modify"},
        {"$set": {"inner.value": 2}},
        projection={"_id": 0, "inner": 1},
        return_document=True,
    )
    assert type(modified) is OrderedDict
    assert type(modified["inner"]) is OrderedDict


def test_same_millisecond_write_identity_matches_mongodb(contract_target):
    collection = contract_target.collection
    first = datetime(2026, 1, 2, 3, 4, 5, 123001, tzinfo=timezone.utc)
    same_millisecond = first.replace(microsecond=123999)
    collection.insert_one({"_id": "same-millisecond", "when": first})

    updated = collection.update_one(
        {"_id": "same-millisecond"},
        {"$set": {"when": same_millisecond}},
    )
    replaced = collection.replace_one(
        {"_id": "same-millisecond"},
        {"_id": "same-millisecond", "when": same_millisecond},
    )

    assert updated.modified_count == 0
    assert replaced.modified_count == 0
