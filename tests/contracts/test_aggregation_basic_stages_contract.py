"""Issue #96 contracts for basic non-transforming aggregation stages."""

import pytest

from .support import observe


pytestmark = pytest.mark.contract


def _ids(rows):
    return [row["_id"] for row in rows]


def test_sort_uses_mongodb_keys_for_missing_null_arrays_and_bson_types(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "missing"},
            {"_id": "null", "value": None},
            {"_id": "empty", "value": []},
            {"_id": "array", "value": [9, 1, 5]},
            {"_id": "number", "value": 2},
            {"_id": "string", "value": "a"},
            {"_id": "object", "value": {"score": 3}},
        ]
    )

    assert _ids(collection.aggregate([{"$sort": {"value": 1, "_id": 1}}])) == [
        "empty",
        "missing",
        "null",
        "array",
        "number",
        "string",
        "object",
    ]
    assert _ids(collection.aggregate([{"$sort": {"value": -1, "_id": 1}}])) == [
        "object",
        "string",
        "array",
        "number",
        "missing",
        "null",
        "empty",
    ]


def test_sort_traverses_dotted_array_paths_and_compounds_ties(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "items": [{"score": 8}, {"score": 2}]},
            {"_id": 2, "items": [{"score": 5}]},
            {"_id": 3, "items": [{}]},
            {"_id": 4},
            {"_id": 5, "items": []},
            {"_id": 6, "items": [{"score": [7, 1]}]},
        ]
    )

    assert _ids(collection.aggregate([{"$sort": {"items.score": 1, "_id": 1}}])) == [
        3,
        4,
        5,
        6,
        1,
        2,
    ]
    assert _ids(collection.aggregate([{"$sort": {"items.score": -1, "_id": 1}}])) == [
        1,
        6,
        2,
        3,
        4,
        5,
    ]


def test_sort_numeric_path_segments_address_array_indexes(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "items": [{"score": 9}, {"score": 1}]},
            {"_id": 2, "items": [{"score": 2}, {"score": 8}]},
        ]
    )

    assert _ids(collection.aggregate([{"$sort": {"items.1.score": 1}}])) == [1, 2]
    assert _ids(collection.aggregate([{"$sort": {"items.1.score": -1}}])) == [2, 1]


def test_compound_numeric_sort_paths_can_select_separate_array_indexes(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "items": [2, 1]},
            {"_id": 2, "items": [1, 2]},
        ]
    )

    assert _ids(collection.aggregate([{"$sort": {"items.0": 1, "items.1": 1}}])) == [
        2,
        1,
    ]


def test_compound_numeric_sort_rejects_parallel_selected_arrays(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": 1, "items": [[2], [1]]})

    outcome = observe(
        lambda: list(collection.aggregate([{"$sort": {"items.0": 1, "items.1": 1}}]))
    )

    assert outcome.error == "operation_failure"


def test_numeric_sort_path_compares_selected_array_as_a_whole_value(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "array", "items": [[2, 1]]},
            {"_id": "object", "items": [{"value": 1}]},
        ]
    )

    assert _ids(collection.aggregate([{"$sort": {"items.0": 1}}])) == [
        "object",
        "array",
    ]
    assert _ids(collection.aggregate([{"$sort": {"items.0": -1}}])) == [
        "array",
        "object",
    ]


def test_numeric_sort_path_keeps_empty_array_special_order(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "empty", "items": [[]]},
            {"_id": "missing"},
            {"_id": "null", "items": [None]},
        ]
    )

    assert _ids(collection.aggregate([{"$sort": {"items.0": 1, "_id": 1}}])) == [
        "empty",
        "missing",
        "null",
    ]
    assert _ids(collection.aggregate([{"$sort": {"items.0": -1, "_id": 1}}])) == [
        "missing",
        "null",
        "empty",
    ]


def test_sort_rejects_ambiguous_numeric_array_paths(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": 1, "items": [{"0": 7}]})

    outcome = observe(lambda: list(collection.aggregate([{"$sort": {"items.0": 1}}])))

    assert outcome.error == "operation_failure"


def test_sort_array_fanout_includes_missing_but_not_nested_arrays(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "items": [{"score": 3}, {}]},
            {"_id": 2, "items": [{"score": 2}]},
            {"_id": 3, "items": [{"score": 4}]},
            {"_id": 4, "items": [[{"score": 9}]]},
        ]
    )

    assert _ids(collection.aggregate([{"$sort": {"items.score": 1, "_id": 1}}])) == [
        1,
        4,
        2,
        3,
    ]
    assert _ids(collection.aggregate([{"$sort": {"items.score": -1, "_id": 1}}])) == [
        3,
        1,
        2,
        4,
    ]


def test_compound_sort_preserves_shared_array_element_correlation(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": "array",
                "values": [{"x": 1, "y": 9}, {"x": 2, "y": 8}],
            },
            {"_id": "scalar", "values": {"x": 1, "y": 8.5}},
        ]
    )

    assert _ids(collection.aggregate([{"$sort": {"values.x": 1, "values.y": 1}}])) == [
        "scalar",
        "array",
    ]


def test_compound_sort_rejects_parallel_arrays(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": 1, "first": [1, 2], "second": [3, 4]})

    outcome = observe(
        lambda: list(collection.aggregate([{"$sort": {"first": 1, "second": 1}}]))
    )

    assert outcome.error == "operation_failure"


def test_compound_sort_allows_nested_arrays_in_separate_parent_elements(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": 1,
                "values": [
                    {"x": [1], "y": 0},
                    {"x": 0, "y": [1]},
                ],
            },
            {"_id": 2},
        ]
    )

    assert _ids(
        collection.aggregate([{"$sort": {"values.x": 1, "values.y": 1, "_id": 1}}])
    ) == [2, 1]


def test_out_of_range_numeric_sort_path_fans_out_to_named_fields(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "values": [{"9": 7}]},
            {"_id": 2},
        ]
    )

    assert _ids(collection.aggregate([{"$sort": {"values.9": 1, "_id": 1}}])) == [2, 1]


@pytest.mark.parametrize("path", ["first.1", "first.9"])
def test_compound_sort_rejects_numeric_path_parallel_arrays(contract_target, path):
    collection = contract_target.collection
    collection.insert_one({"_id": 1, "first": [1, 2], "second": [3]})

    outcome = observe(
        lambda: list(collection.aggregate([{"$sort": {path: 1, "second": 1}}]))
    )

    assert outcome.error == "operation_failure"


def test_sort_skip_limit_and_count_compose_with_other_stages(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "keep": True, "score": 20},
            {"_id": 2, "keep": False, "score": 100},
            {"_id": 3, "keep": True, "score": 50},
            {"_id": 4, "keep": True, "score": 50},
            {"_id": 5, "keep": True, "score": 10},
            {"_id": 6, "keep": True, "score": 30},
        ]
    )

    page = list(
        collection.aggregate(
            [
                {"$match": {"keep": True}},
                {"$sort": {"score": -1, "_id": 1}},
                {"$skip": 1},
                {"$limit": 3},
                {"$project": {"_id": 1, "score": 1}},
            ]
        )
    )
    assert page == [
        {"_id": 4, "score": 50},
        {"_id": 6, "score": 30},
        {"_id": 1, "score": 20},
    ]
    assert list(
        collection.aggregate(
            [
                {"$match": {"keep": True}},
                {"$sort": {"score": -1, "_id": 1}},
                {"$skip": 1},
                {"$limit": 3},
                {"$count": "selected"},
                {"$set": {"label": "page"}},
            ]
        )
    ) == [{"selected": 3, "label": "page"}]


def test_integral_numeric_arguments_and_pagination_boundaries(contract_target):
    collection = contract_target.collection
    collection.insert_many([{"_id": value} for value in range(1, 5)])

    assert _ids(
        collection.aggregate(
            [
                {"$sort": {"_id": -1.0}},
                {"$skip": 1.0},
                {"$limit": 2.0},
            ]
        )
    ) == [3, 2]
    assert list(collection.aggregate([{"$skip": 20}])) == []
    assert _ids(collection.aggregate([{"$limit": 20}])) == [1, 2, 3, 4]


def test_empty_input_stays_empty_and_count_emits_no_document(contract_target):
    collection = contract_target.collection

    assert (
        list(
            collection.aggregate(
                [
                    {"$sort": {"value": 1}},
                    {"$skip": 0},
                    {"$limit": 1},
                ]
            )
        )
        == []
    )
    assert list(collection.aggregate([{"$count": "total"}])) == []


@pytest.mark.parametrize(
    "stage",
    [
        {"$sort": None},
        {"$sort": {}},
        {"$sort": {"value": 0}},
        {"$sort": {"value": True}},
        {"$sort": {"value.": 1}},
        {"$sort": {"field{0}".format(index): 1 for index in range(33)}},
        {"$skip": -1},
        {"$skip": 1.5},
        {"$skip": True},
        {"$limit": -1},
        {"$limit": 0},
        {"$limit": 1.5},
        {"$limit": True},
        {"$count": 1},
        {"$count": ""},
        {"$count": "$total"},
        {"$count": "nested.total"},
        {"$count": "bad\x00total"},
        {"$count": "_id"},
    ],
)
def test_basic_stage_validation_is_mongodb_shaped(contract_target, stage):
    outcome = observe(lambda: list(contract_target.collection.aggregate([stage])))

    assert outcome.error == "operation_failure"
