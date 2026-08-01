"""Projection-stage contracts shared by TinyMongo and MongoDB."""

import pytest

from .support import observe


pytestmark = pytest.mark.contract


def test_project_inclusion_exclusion_and_computed_modes(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": 1,
            "name": "Ada",
            "secret": "hidden",
            "profile": {"email": "ada@example.com", "age": 36},
            "items": [{"name": "one", "extra": 1}, {}, 3],
        }
    )

    assert list(
        collection.aggregate([{"$project": {"name": 1, "profile.email": 1}}])
    ) == [
        {
            "_id": 1,
            "name": "Ada",
            "profile": {"email": "ada@example.com"},
        }
    ]
    assert list(
        collection.aggregate([{"$project": {"secret": 0, "profile.age": 0}}])
    ) == [
        {
            "_id": 1,
            "name": "Ada",
            "profile": {"email": "ada@example.com"},
            "items": [{"name": "one", "extra": 1}, {}, 3],
        }
    ]
    assert list(
        collection.aggregate(
            [
                {
                    "$project": {
                        "_id": 0,
                        "renamed": "$name",
                        "profile": {"email": 1, "label": "$name"},
                    }
                }
            ]
        )
    ) == [
        {
            "renamed": "Ada",
            "profile": {"email": "ada@example.com", "label": "Ada"},
        }
    ]

    for invalid_project in (
        {"name": 1, "secret": 0},
        {"secret": 0, "computed": "$name"},
    ):
        outcome = observe(
            lambda invalid_project=invalid_project: list(
                collection.aggregate([{"$project": invalid_project}])
            )
        )
        assert outcome.error == "operation_failure"


def test_project_computed_dotted_paths_preserve_array_shape(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": 1,
            "title": "Course",
            "items": [{"name": "one", "extra": 1}, {}, 3],
            "scalar": "old",
            "empty": [],
        }
    )

    assert list(
        collection.aggregate(
            [
                {
                    "$project": {
                        "_id": 0,
                        "items.name": 1,
                        "items.label": "$title",
                        "created.value": "$title",
                        "scalar.value": "$title",
                        "empty.value": "$title",
                        "missing.value": "$not_there",
                    }
                }
            ]
        )
    ) == [
        {
            "items": [
                {"name": "one", "label": "Course"},
                {"label": "Course"},
                {"label": "Course"},
            ],
            "created": {"value": "Course"},
            "scalar": {"value": "Course"},
            "empty": [],
            "missing": {},
        }
    ]


def test_set_and_add_fields_are_aliases_using_original_input(contract_target):
    collection = contract_target.collection
    original = {
        "_id": 1,
        "existing": "old",
        "profile": {"name": "Ada"},
        "items": [{"name": "one"}, {}, 3],
    }
    collection.insert_one(original.copy())
    expected = {
        "_id": 1,
        "existing": "new",
        "copied": "old",
        "profile": {"name": "Ada", "label": "old"},
        "items": [
            {"name": "one", "label": "old"},
            {"label": "old"},
            {"label": "old"},
        ],
    }
    specification = {
        "existing": "new",
        "copied": "$existing",
        "missing": "$not_there",
        "profile.label": "$existing",
        "items.label": "$existing",
    }

    for stage in ("$set", "$addFields"):
        assert list(collection.aggregate([{stage: specification}])) == [expected]
        assert list(collection.aggregate([{stage: {}}])) == [original]


def test_unset_accepts_one_or_many_dotted_fields(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": 1,
            "name": "Ada",
            "secret": "hidden",
            "profile": {"email": "ada@example.com", "age": 36},
            "items": [{"name": "one", "secret": 1}, {}, 3],
        }
    )

    assert list(collection.aggregate([{"$unset": "secret"}])) == [
        {
            "_id": 1,
            "name": "Ada",
            "profile": {"email": "ada@example.com", "age": 36},
            "items": [{"name": "one", "secret": 1}, {}, 3],
        }
    ]
    assert list(
        collection.aggregate([{"$unset": ["secret", "profile.age", "items.secret"]}])
    ) == [
        {
            "_id": 1,
            "name": "Ada",
            "profile": {"email": "ada@example.com"},
            "items": [{"name": "one"}, {}, 3],
        }
    ]


def test_literal_and_remove_expression_contract(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": 1,
            "name": "Ada",
            "secret": "hidden",
            "profile": {"email": "ada@example.com", "age": 36},
            "items": [1, 2],
        }
    )

    assert list(
        collection.aggregate(
            [
                {
                    "$project": {
                        "_id": 0,
                        "kept": "$name",
                        "literal_number": {"$literal": 1},
                        "literal_bool": {"$literal": False},
                        "literal_path": {"$literal": "$name"},
                        "literal_document": {"$literal": {"$size": "$items"}},
                        "literal_array": {"$literal": ("$name", {"$size": "$items"})},
                        "dropped": "$$REMOVE",
                        "holder.drop": "$$REMOVE",
                        "array": ["$$REMOVE", "$name"],
                    }
                }
            ]
        )
    ) == [
        {
            "kept": "Ada",
            "literal_number": 1,
            "literal_bool": False,
            "literal_path": "$name",
            "literal_document": {"$size": "$items"},
            "literal_array": ["$name", {"$size": "$items"}],
            "holder": {},
            "array": [None, "Ada"],
        }
    ]
    assert list(
        collection.aggregate(
            [{"$set": {"secret": "$$REMOVE", "profile.age": "$$REMOVE"}}]
        )
    ) == [
        {
            "_id": 1,
            "name": "Ada",
            "profile": {"email": "ada@example.com"},
            "items": [1, 2],
        }
    ]


def test_field_paths_do_not_descend_through_nested_arrays(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": 1,
            "nested": [[{"score": 7}], 2],
            "direct": [{"score": 3}, [{"score": 4}], {"score": 5}],
        }
    )

    assert list(
        collection.aggregate(
            [
                {
                    "$project": {
                        "_id": 0,
                        "nested_scores": "$nested.score",
                        "direct_scores": "$direct.score",
                    }
                }
            ]
        )
    ) == [{"nested_scores": [], "direct_scores": [3, 5]}]


def test_project_preserves_embedded_source_order_for_group_identity(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "value": {"x": 1, "y": 2}},
            {"_id": 2, "value": {"y": 2, "x": 1}},
        ]
    )

    rows = list(
        collection.aggregate(
            [
                {"$project": {"_id": 0, "value.x": 1, "value.y": 1}},
                {"$group": {"_id": "$value", "count": {"$sum": 1}}},
            ]
        )
    )

    assert len(rows) == 2
    assert [row["count"] for row in rows] == [1, 1]
    assert {tuple(row["_id"]) for row in rows} == {("x", "y"), ("y", "x")}
