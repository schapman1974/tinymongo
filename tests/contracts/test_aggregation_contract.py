"""Aggregation behavior shared by every API and storage target."""

from datetime import datetime

import pytest

from .support import observe


pytestmark = pytest.mark.contract


def _by_id(rows):
    return {row["_id"]: row for row in rows}


def test_latest_activity_by_course(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": 1,
                "user_id": "ada",
                "course_id": "python",
                "created_date": datetime(2026, 1, 1, 8),
            },
            {
                "_id": 2,
                "user_id": "ada",
                "course_id": "python",
                "created_date": datetime(2026, 1, 3, 8),
            },
            {
                "_id": 3,
                "user_id": "ada",
                "course_id": "mongo",
                "created_date": datetime(2026, 1, 2, 8),
            },
            {
                "_id": 4,
                "user_id": "grace",
                "course_id": "python",
                "created_date": datetime(2026, 1, 9, 8),
            },
        ]
    )

    rows = list(
        collection.aggregate(
            [
                {"$match": {"user_id": "ada"}},
                {
                    "$group": {
                        "_id": "$course_id",
                        "last_activity": {"$max": "$created_date"},
                    }
                },
            ]
        )
    )

    assert _by_id(rows) == {
        "python": {
            "_id": "python",
            "last_activity": datetime(2026, 1, 3, 8),
        },
        "mongo": {
            "_id": "mongo",
            "last_activity": datetime(2026, 1, 2, 8),
        },
    }


def test_first_and_last_play_by_user(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": 1,
                "course_id": "python",
                "user_id": "ada",
                "created_date": datetime(2026, 2, 4),
            },
            {
                "_id": 2,
                "course_id": "python",
                "user_id": "ada",
                "created_date": datetime(2026, 2, 1),
            },
            {
                "_id": 3,
                "course_id": "python",
                "user_id": "grace",
                "created_date": datetime(2026, 2, 3),
            },
            {
                "_id": 4,
                "course_id": "mongo",
                "user_id": "ada",
                "created_date": datetime(2026, 2, 9),
            },
        ]
    )

    rows = list(
        collection.aggregate(
            [
                {
                    "$match": {
                        "course_id": "python",
                        "user_id": {"$in": ["ada", "grace"]},
                    }
                },
                {
                    "$group": {
                        "_id": "$user_id",
                        "first_play": {"$min": "$created_date"},
                        "last_play": {"$max": "$created_date"},
                    }
                },
            ]
        )
    )

    assert _by_id(rows) == {
        "ada": {
            "_id": "ada",
            "first_play": datetime(2026, 2, 1),
            "last_play": datetime(2026, 2, 4),
        },
        "grace": {
            "_id": "grace",
            "first_play": datetime(2026, 2, 3),
            "last_play": datetime(2026, 2, 3),
        },
    }


def test_whole_collection_rollup(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": 1,
                "user_id": "ada",
                "course_id": "python",
                "created_date": datetime(2026, 3, 1),
            },
            {
                "_id": 2,
                "user_id": "ada",
                "course_id": "python",
                "created_date": datetime(2026, 3, 5),
            },
            {
                "_id": 3,
                "user_id": "grace",
                "course_id": "python",
                "created_date": datetime(2026, 3, 9),
            },
        ]
    )

    assert list(
        collection.aggregate(
            [
                {"$match": {"user_id": "ada", "course_id": "python"}},
                {
                    "$group": {
                        "_id": None,
                        "last_activity": {"$max": "$created_date"},
                    }
                },
            ]
        )
    ) == [{"_id": None, "last_activity": datetime(2026, 3, 5)}]


def test_match_reuses_query_semantics_and_can_follow_group(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": 1,
                "team": "alpha",
                "profile": {"name": "Ada", "score": 9},
            },
            {
                "_id": 2,
                "team": "alpha",
                "profile": {"name": "Grace", "score": 7},
            },
            {
                "_id": 3,
                "team": "beta",
                "profile": {"name": "Lin", "score": 10},
            },
        ]
    )

    matched = list(
        collection.aggregate(
            [
                {
                    "$match": {
                        "$and": [
                            {"profile.score": {"$gte": 7, "$lte": 9}},
                            {"profile.name": {"$regex": "^a", "$options": "i"}},
                            {"_id": {"$ne": 99}},
                        ]
                    }
                }
            ]
        )
    )
    assert [row["_id"] for row in matched] == [1]

    grouped = list(
        collection.aggregate(
            [
                {"$group": {"_id": "$team", "total": {"$sum": 1}}},
                {"$match": {"total": {"$gte": 2}}},
            ]
        )
    )
    assert grouped == [{"_id": "alpha", "total": 2}]


def test_sum_and_min_max_follow_mongodb_value_rules(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "group": "one", "amount": 2, "value": None},
            {"_id": 2, "group": "one", "amount": 1.5, "value": 5},
            {"_id": 3, "group": "one", "amount": True, "value": "text"},
            {
                "_id": 4,
                "group": "one",
                "amount": "ignored",
                "value": datetime(2026, 4, 1),
            },
            {"_id": 5, "group": "one", "amount": [100]},
            {"_id": 6, "group": "one"},
        ]
    )

    assert list(
        collection.aggregate(
            [
                {
                    "$group": {
                        "_id": "$group",
                        "count": {"$sum": 1},
                        "total": {"$sum": "$amount"},
                        "minimum": {"$min": "$value"},
                        "maximum": {"$max": "$value"},
                    }
                }
            ]
        )
    ) == [
        {
            "_id": "one",
            "count": 6,
            "total": 3.5,
            "minimum": 5,
            "maximum": datetime(2026, 4, 1),
        }
    ]


def test_null_missing_keys_and_empty_inputs(contract_target):
    collection = contract_target.collection
    assert (
        list(collection.aggregate([{"$group": {"_id": None, "n": {"$sum": 1}}}])) == []
    )

    collection.insert_many(
        [
            {"_id": 1, "bucket": None, "value": None},
            {"_id": 2},
        ]
    )
    assert list(
        collection.aggregate(
            [
                {
                    "$group": {
                        "_id": "$bucket",
                        "minimum": {"$min": "$value"},
                        "maximum": {"$max": "$value"},
                    }
                }
            ]
        )
    ) == [{"_id": None, "minimum": None, "maximum": None}]
    assert (
        list(
            collection.aggregate(
                [
                    {"$match": {"_id": "missing"}},
                    {"$group": {"_id": None, "n": {"$sum": 1}}},
                ]
            )
        )
        == []
    )


def test_dotted_paths_preserve_empty_array_traversals(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "items": []},
            {"_id": 2, "items": [{}, {}]},
            {"_id": 3, "items": [{}, {"score": 3}]},
            {"_id": 4},
        ]
    )

    rows = list(
        collection.aggregate(
            [{"$group": {"_id": "$items.score", "count": {"$sum": 1}}}]
        )
    )

    assert len(rows) == 3
    assert any(row == {"_id": [], "count": 2} for row in rows)
    assert any(row == {"_id": [3], "count": 1} for row in rows)
    assert any(row == {"_id": None, "count": 1} for row in rows)


def test_project_size_ifnull_application_rollup(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "course_id": "python", "lectures": ["a", "b"]},
            {"_id": 2, "course_id": "python", "lectures": []},
            {"_id": 3, "course_id": "python", "lectures": None},
            {"_id": 4, "course_id": "python"},
            {"_id": 5, "course_id": "mongo", "lectures": ["one"]},
            {"_id": 6, "course_id": "excluded", "lectures": [1, 2, 3]},
        ]
    )

    rows = list(
        collection.aggregate(
            [
                {"$match": {"course_id": {"$in": ["python", "mongo"]}}},
                {
                    "$project": {
                        "course_id": 1,
                        "count": {"$size": {"$ifNull": ["$lectures", []]}},
                    }
                },
                {
                    "$group": {
                        "_id": "$course_id",
                        "total": {"$sum": "$count"},
                    }
                },
            ]
        )
    )

    assert _by_id(rows) == {
        "python": {"_id": "python", "total": 2},
        "mongo": {"_id": "mongo", "total": 1},
    }


def test_project_inclusion_id_and_expression_contract(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": 1,
            "course_id": "python",
            "lectures": ["one", "two"],
            "profile": {"name": "Ada", "secret": "hidden"},
        }
    )

    assert list(
        collection.aggregate(
            [
                {
                    "$project": {
                        "course_id": 1,
                        "profile.name": 1,
                        "copied": "$profile.name",
                        "fallback": {
                            "$ifNull": ["$missing", "$profile.name", "unknown"]
                        },
                    }
                }
            ]
        )
    ) == [
        {
            "_id": 1,
            "course_id": "python",
            "profile": {"name": "Ada"},
            "copied": "Ada",
            "fallback": "Ada",
        }
    ]
    assert list(
        collection.aggregate(
            [
                {
                    "$project": {
                        "_id": 0,
                        "course_id": 1,
                        "count": {"$size": "$lectures"},
                    }
                }
            ]
        )
    ) == [{"course_id": "python", "count": 2}]


def test_project_dotted_inclusion_preserves_array_shape(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": 1,
            "items": [
                {"name": "intro", "secret": 1},
                {},
                {"name": "loops", "secret": 2},
            ],
        }
    )

    assert list(collection.aggregate([{"$project": {"_id": 0, "items.name": 1}}])) == [
        {"items": [{"name": "intro"}, {}, {"name": "loops"}]}
    ]


def test_project_expression_argument_and_lazy_evaluation_contract(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": 1,
            "zero": 0,
            "not_an_array": "bad",
            "values": [1, 2, 3],
        }
    )

    assert list(
        collection.aggregate(
            [
                {
                    "$project": {
                        "_id": 0,
                        "field_size": {"$size": ["$values"]},
                        "literal_size": {"$size": [[1, 2]]},
                        "literal_empty_size": {"$size": [[]]},
                        "lazy": {"$ifNull": ["$zero", {"$size": "$not_an_array"}]},
                        "missing_ifnull": {"$ifNull": ["$missing_one", "$missing_two"]},
                        "tuple_literal": (1, 2),
                        "tuple_size": {"$size": ((1, 2),)},
                    }
                }
            ]
        )
    ) == [
        {
            "field_size": 3,
            "literal_size": 2,
            "literal_empty_size": 0,
            "lazy": 0,
            "tuple_literal": [1, 2],
            "tuple_size": 2,
        }
    ]

    for operand in ([], [1, 2]):
        outcome = observe(
            lambda operand=operand: list(
                collection.aggregate([{"$project": {"invalid": {"$size": operand}}}])
            )
        )
        assert outcome.error == "operation_failure"


def test_aggregate_cursor_consumes_in_chunks(contract_target):
    collection = contract_target.collection
    collection.insert_many([{"_id": 1}, {"_id": 2}])

    cursor = collection.aggregate([])
    assert cursor.to_list(length=1) == [{"_id": 1}]
    assert cursor.to_list() == [{"_id": 2}]
    assert cursor.to_list() == []
