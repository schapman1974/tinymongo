"""Focused validation and edge tests for issue #96 aggregation stages."""

import copy
from datetime import date
from uuid import uuid4

import pytest

import tinymongo
from tinymongo.aggregation import AggregationEngine
from tinymongo.errors import OperationFailure, TinyMongoNotSupportedError
from tinymongo.sorting import bson_document_sort_value_key, document_sort_key


def _collection(name="aggregation-basic-stages"):
    return tinymongo.TinyMongoClient(
        "memory://{0}-{1}".format(name, uuid4().hex), backend="memory"
    ).db.items


@pytest.mark.parametrize(
    ("stage", "code"),
    [
        ({"$sort": None}, 15973),
        ({"$sort": []}, 15973),
        ({"$sort": {}}, 15976),
        ({"$sort": {"value": True}}, 15974),
        ({"$sort": {1: 1}}, 15974),
        ({"$sort": {"value": 0}}, 15975),
        ({"$sort": {"value": 2}}, 15975),
        ({"$sort": {"": 1}}, 40352),
        ({"$sort": {"value..nested": 1}}, 15998),
        ({"$sort": {"value.": 1}}, 40353),
        ({"$sort": {"$value": 1}}, 16410),
        (
            {"$sort": {"field{0}".format(index): 1 for index in range(33)}},
            13103,
        ),
        ({"$skip": -1}, 5107200),
        ({"$skip": 1.5}, 5107200),
        ({"$skip": True}, 5107200),
        ({"$skip": "1"}, 5107200),
        ({"$skip": 2**63}, 5107200),
        ({"$limit": -1}, 5107201),
        ({"$limit": 0}, 15958),
        ({"$limit": 1.5}, 5107201),
        ({"$limit": True}, 5107201),
        ({"$limit": "1"}, 5107201),
        ({"$limit": 2**63}, 5107201),
        ({"$count": 1}, 40156),
        ({"$count": ""}, 40157),
        ({"$count": "$total"}, 40158),
        ({"$count": "nested.total"}, 40160),
        ({"$count": "bad\x00total"}, 40159),
        ({"$count": "_id"}, 15948),
    ],
)
def test_invalid_stage_arguments_fail_before_collection_storage_is_opened(stage, code):
    client = tinymongo.TinyMongoClient(
        "memory://aggregation-basic-validation-{0}".format(uuid4().hex),
        backend="memory",
    )
    collection = client.db.items

    with pytest.raises(OperationFailure) as caught:
        collection.aggregate([{"$match": {}}, stage])

    assert caught.value.code == code
    assert client.db.list_collection_names() == []


def test_sort_meta_expression_is_rejected_before_collection_storage_is_opened():
    client = tinymongo.TinyMongoClient(
        "memory://aggregation-sort-meta-{0}".format(uuid4().hex),
        backend="memory",
    )
    collection = client.db.items

    with pytest.raises(TinyMongoNotSupportedError, match=r"\$sort \$meta"):
        collection.aggregate([{"$sort": {"score": {"$meta": "textScore"}}}])

    assert client.db.list_collection_names() == []


def test_integral_numeric_arguments_are_normalized_without_mutating_pipeline():
    collection = _collection("aggregation-basic-integral-numbers")
    collection.insert_many([{"_id": value} for value in range(1, 5)])
    pipeline = [
        {"$sort": {"_id": -1.0}},
        {"$skip": 1.0},
        {"$limit": 2.0},
    ]
    original = copy.deepcopy(pipeline)

    assert collection.aggregate(pipeline).to_list() == [{"_id": 3}, {"_id": 2}]
    assert pipeline == original


def test_skip_and_limit_accept_signed_64_bit_upper_bound():
    collection = _collection("aggregation-basic-int64-bound")
    collection.insert_many([{"_id": 1}, {"_id": 2}])
    maximum = 2**63 - 1

    assert collection.aggregate([{"$skip": maximum}]).to_list() == []
    assert collection.aggregate([{"$limit": maximum}]).to_list() == [
        {"_id": 1},
        {"_id": 2},
    ]


def test_count_can_feed_later_stages_and_is_empty_on_empty_input():
    collection = _collection("aggregation-basic-count")

    assert collection.aggregate([{"$count": "total"}]).to_list() == []
    collection.insert_many(
        [
            {"_id": 1, "keep": True},
            {"_id": 2, "keep": False},
            {"_id": 3, "keep": True},
        ]
    )

    assert collection.aggregate(
        [
            {"$match": {"keep": True}},
            {"$count": "total"},
            {"$project": {"_id": 0, "total": 1}},
        ]
    ).to_list() == [{"total": 2}]


def test_compound_sort_is_deterministic_and_does_not_mutate_documents():
    collection = _collection("aggregation-basic-compound-sort")
    documents = [
        {"_id": 1, "team": "b", "score": 2, "nested": {"value": "one"}},
        {"_id": 2, "team": "a", "score": 1, "nested": {"value": "two"}},
        {"_id": 3, "team": "a", "score": 3, "nested": {"value": "three"}},
        {"_id": 4, "team": "a", "score": 3, "nested": {"value": "four"}},
    ]
    collection.insert_many(copy.deepcopy(documents))

    rows = collection.aggregate(
        [{"$sort": {"team": 1, "score": -1, "_id": 1}}]
    ).to_list()

    assert [row["_id"] for row in rows] == [3, 4, 2, 1]
    rows[0]["nested"]["value"] = "changed"
    assert collection.find().sort("_id").to_list() == documents


def test_sort_rejects_ambiguous_numeric_array_path_with_mongodb_code():
    collection = _collection("aggregation-basic-ambiguous-array-index")
    collection.insert_one({"_id": 1, "items": [{"0": 7}]})

    with pytest.raises(OperationFailure) as caught:
        collection.aggregate([{"$sort": {"items.0": 1}}])

    assert caught.value.code == 16746


def test_parallel_array_error_precedes_ambiguous_numeric_path_error():
    collection = _collection("aggregation-basic-array-error-precedence")
    collection.insert_one(
        {
            "_id": 1,
            "first": [{"0": -5}, {"0": None}],
            "second": [2],
        }
    )

    with pytest.raises(OperationFailure) as caught:
        collection.aggregate([{"$sort": {"first.0": -1, "second": -1}}])

    assert caught.value.code == 2


def test_skip_and_limit_apply_at_their_pipeline_positions():
    collection = _collection("aggregation-basic-pagination-order")
    collection.insert_many(
        [
            {"_id": 1, "value": 30},
            {"_id": 2, "value": 10},
            {"_id": 3, "value": 40},
            {"_id": 4, "value": 20},
        ]
    )

    assert collection.aggregate(
        [
            {"$sort": {"value": 1}},
            {"$skip": 1},
            {"$limit": 2},
            {"$sort": {"value": -1}},
        ]
    ).to_list() == [
        {"_id": 1, "value": 30},
        {"_id": 4, "value": 20},
    ]


def test_aggregation_sort_warns_once_for_each_unsupported_field_type():
    engine = AggregationEngine()
    documents = [
        {"_id": 1, "published": date(2026, 1, 2)},
        {"_id": 2, "published": date(2026, 1, 1)},
    ]

    with pytest.warns(tinymongo.TinyMongoUnsupportedWarning) as caught:
        assert engine.run(documents, [{"$sort": {"published": 1}}]) == documents
        assert engine.run(documents, [{"$sort": {"published": -1}}]) == documents

    assert len(caught) == 1
    assert "published" in str(caught[0].message)
    assert "date" in str(caught[0].message)


def test_shared_sort_helpers_cover_missing_scalar_and_array_path_edges():
    assert bson_document_sort_value_key(object()) == (0, None)
    assert document_sort_key({"item": 1}, "item.value") == (0, None)
    assert document_sort_key({"items": [1]}, "items.9") == (0, None)
    assert document_sort_key(
        {"items": [{"score": 2}, {"score": 1}]}, "items.score"
    ) == (1, (1, 1))
    assert document_sort_key(
        {"items": [{"score": 2}, {"score": 1}]},
        "items.score",
        descending=True,
    ) == (1, (1, 2))


def test_compound_sort_joins_large_shared_arrays_without_cartesian_work(monkeypatch):
    collection = _collection("aggregation-basic-linear-multikey-sort")
    collection.insert_one(
        {
            "_id": 1,
            "values": [{"left": value, "right": 1000 - value} for value in range(1000)],
        }
    )
    from tinymongo import sorting

    calls = []
    candidate_key = sorting._candidate_key

    def counted_candidate_key(*args, **kwargs):
        calls.append(None)
        return candidate_key(*args, **kwargs)

    monkeypatch.setattr(sorting, "_candidate_key", counted_candidate_key)

    assert (
        collection.aggregate(
            [{"$sort": {"values.left": 1, "values.right": 1}}]
        ).to_list()[0]["_id"]
        == 1
    )
    assert len(calls) == 2000


def test_cursor_order_compatibility_helper_warns_and_handles_sort_direction():
    cursor = tinymongo.TinyMongoCursor([])

    with pytest.warns(tinymongo.TinyMongoUnsupportedWarning):
        assert cursor._order(object(), sort_field="value") == (0, None)

    assert cursor._order([2, 1], is_reverse=False, sort_field="value") == (
        1,
        (1, 1),
    )
