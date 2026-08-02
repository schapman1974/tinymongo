"""Focused unit coverage for the remaining issue #77 update modifiers."""

import pytest

from tinymongo import TinyMongoClient
from tinymongo import tinymongo as core
from tinymongo.errors import TinyMongoError, WriteError


def test_min_max_use_bson_order_and_preserve_equal_numeric_representation():
    original = {
        "_id": 1,
        "minimum": "text",
        "maximum": 1,
        "equal": 1.0,
        "array": [2],
    }

    updated = core._apply_update_document(
        original,
        {
            "$min": {"minimum": 5, "array": [1]},
            "$max": {"maximum": {"value": 1}, "equal": 1},
        },
    )

    assert updated == {
        "_id": 1,
        "minimum": 5,
        "maximum": {"value": 1},
        "equal": 1.0,
        "array": [1],
    }
    assert type(updated["equal"]) is float
    assert original["minimum"] == "text"


def test_min_max_create_missing_nested_fields_and_empty_operands_are_noops():
    original = {"_id": 1, "profile": {"keep": True}}

    updated = core._apply_update_document(
        original,
        {
            "$min": {"profile.low": 2, "created.low": 3},
            "$max": {"profile.high": 8, "created.high": 9},
        },
    )
    unchanged = core._apply_update_document(
        original,
        {"$min": {}, "$max": {}, "$rename": {}, "$pop": {}},
    )

    assert updated == {
        "_id": 1,
        "profile": {"keep": True, "low": 2, "high": 8},
        "created": {"low": 3, "high": 9},
    }
    assert unchanged == original


def test_min_max_and_pop_support_numeric_array_paths_and_sparse_growth():
    original = {
        "_id": 1,
        "values": [5, 8],
        "nested": [[1, 2], [3]],
        "documents": [{"score": 9}],
    }

    updated = core._apply_update_document(
        original,
        {
            "$min": {
                "values.0": 3,
                "values.3": 7,
                "documents.0.score": 4,
                "documents.2.score": 4,
            },
            "$max": {"values.1": 10},
            "$pop": {"nested.0": 1},
        },
    )

    assert updated == {
        "_id": 1,
        "values": [3, 10, None, 7],
        "nested": [[1], [3]],
        "documents": [{"score": 4}, None, {"score": 4}],
    }


def test_new_update_paths_reject_existing_scalar_and_null_ancestors():
    cases = (
        ({"_id": 1, "path": "scalar"}, {"$min": {"path.value": 1}}),
        ({"_id": 1, "path": None}, {"$max": {"path.value": 1}}),
        ({"_id": 1, "path": [None]}, {"$min": {"path.0.value": 1}}),
        ({"_id": 1, "path": "scalar"}, {"$pop": {"path.values": 1}}),
    )

    for original, update in cases:
        with pytest.raises(WriteError) as caught:
            core._apply_update_document(original, update)
        assert caught.value.code == 28


def test_rename_moves_nested_values_overwrites_and_ignores_missing_source():
    original = {
        "_id": 1,
        "source": {"value": 1},
        "destination": "stale",
        "nested": {"old": [1, 2], "keep": True},
    }

    updated = core._apply_update_document(
        original,
        {
            "$rename": {
                "source": "destination",
                "nested.old": "created.deep.value",
                "missing": "untouched",
            }
        },
    )

    assert updated == {
        "_id": 1,
        "destination": {"value": 1},
        "nested": {"keep": True},
        "created": {"deep": {"value": [1, 2]}},
    }
    assert original["source"] == {"value": 1}


@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({"$rename": {"field": 1}}, 2),
        ({"$rename": {"field": "field"}}, 2),
        ({"$rename": {"field": "field.child"}}, 2),
        ({"$rename": {"field.child": "field"}}, 2),
        ({"$rename": {"field.$": "other"}}, 2),
        ({"$rename": {"field": "other.$[item]"}}, 2),
    ],
    ids=(
        "non-string-destination",
        "same-path",
        "destination-child",
        "destination-parent",
        "positional-source",
        "filtered-destination",
    ),
)
def test_rename_rejects_invalid_paths(update, code):
    original = {"_id": 1, "field": {"child": 1}}

    with pytest.raises(WriteError) as caught:
        core._apply_update_document(original, update)

    assert isinstance(caught.value, TinyMongoError)
    assert caught.value.code == code
    assert original == {"_id": 1, "field": {"child": 1}}


def test_update_path_conflicts_and_empty_paths_report_mongodb_codes():
    original = {"_id": 1, "field": {"child": 1}, "other": 2}
    cases = (
        ({"$min": {"other": 1}, "$max": {"other": 3}}, 40),
        ({"$set": {"field": {}}, "$pop": {"field.child": 1}}, 40),
        ({"$rename": {"other": "moved"}, "$set": {"moved": 3}}, 40),
        ({"$rename": {"": "moved"}}, 56),
        ({"$min": {"field.": 1}}, 56),
        ({"$pop": []}, 9),
    )

    for update, code in cases:
        with pytest.raises(WriteError) as caught:
            core._apply_update_document(original, update)
        assert caught.value.code == code
        assert original == {"_id": 1, "field": {"child": 1}, "other": 2}


def test_pop_removes_front_back_and_handles_missing_or_empty_arrays():
    original = {
        "_id": 1,
        "front": [1, 2, 3],
        "back": [1, 2, 3],
        "empty": [],
        "nested": {"values": [4, 5]},
    }

    updated = core._apply_update_document(
        original,
        {
            "$pop": {
                "front": -1,
                "back": 1,
                "empty": 1,
                "missing": -1,
                "nested.values": -1,
            }
        },
    )

    assert updated == {
        "_id": 1,
        "front": [2, 3],
        "back": [1, 2],
        "empty": [],
        "nested": {"values": [5]},
    }
    assert original["front"] == [1, 2, 3]


@pytest.mark.parametrize("operand", [0, 2, -2, 1.5, True, "1", {}, []])
def test_pop_rejects_every_operand_except_integral_plus_or_minus_one(operand):
    original = {"_id": 1, "values": [1, 2]}

    with pytest.raises(WriteError) as caught:
        core._apply_update_document(original, {"$pop": {"values": operand}})

    assert caught.value.code == 9
    assert original == {"_id": 1, "values": [1, 2]}


@pytest.mark.parametrize(
    ("operand", "expected"),
    [(1.0, [1]), (-1.0, [2])],
)
def test_pop_accepts_integral_float_directions(operand, expected):
    updated = core._apply_update_document(
        {"_id": 1, "values": [1, 2]}, {"$pop": {"values": operand}}
    )

    assert updated["values"] == expected


@pytest.mark.parametrize("value", [None, "scalar", 1, {}])
def test_pop_rejects_non_array_targets_with_type_mismatch(value):
    original = {"_id": 1, "values": value}

    with pytest.raises(WriteError) as caught:
        core._apply_update_document(original, {"$pop": {"values": 1}})

    assert caught.value.code == 14
    assert original == {"_id": 1, "values": value}


def test_new_operator_failures_do_not_partially_apply_other_changes():
    original = {"_id": 1, "status": "original", "values": "not-an-array"}

    with pytest.raises(WriteError) as caught:
        core._apply_update_document(
            original,
            {
                "$set": {"status": "changed"},
                "$pop": {"values": 1},
            },
        )

    assert caught.value.code == 14
    assert original == {"_id": 1, "status": "original", "values": "not-an-array"}


def test_invalid_new_operators_are_preflighted_even_without_a_match(tmp_path):
    client = TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.db.items

    with pytest.raises(WriteError) as caught:
        collection.update_one({"_id": "missing"}, {"$pop": {"values": 0}})

    assert caught.value.code == 9
    assert collection.count_documents({}) == 0
    client.close()


def test_upserts_apply_new_operators_to_equality_fields(tmp_path):
    client = TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.db.items

    minimum = collection.update_one(
        {"_id": "minimum", "score": 5},
        {"$min": {"score": 3}},
        upsert=True,
    )
    maximum = collection.update_one(
        {"_id": "maximum", "score": 5},
        {"$max": {"score": 7}},
        upsert=True,
    )
    popped = collection.update_one(
        {"_id": "popped", "values": [1, 2]},
        {"$pop": {"values": 1}},
        upsert=True,
    )
    renamed = collection.update_one(
        {"_id": "renamed", "source": "value"},
        {"$rename": {"source": "destination"}},
        upsert=True,
    )

    assert minimum.upserted_id == "minimum"
    assert maximum.upserted_id == "maximum"
    assert popped.upserted_id == "popped"
    assert renamed.upserted_id == "renamed"
    assert collection.find_one({"_id": "minimum"})["score"] == 3
    assert collection.find_one({"_id": "maximum"})["score"] == 7
    assert collection.find_one({"_id": "popped"})["values"] == [1]
    assert collection.find_one({"_id": "renamed"}) == {
        "_id": "renamed",
        "destination": "value",
    }
    client.close()


def test_update_path_and_comparison_internal_error_edges():
    error_cases = (
        (lambda: core._update_path_parts(1), 9),
        (
            lambda: core._read_update_path(
                {"path": []}, "path.value", rename_role="source"
            ),
            28,
        ),
        (lambda: core._read_update_path({"path": []}, "path.value"), 28),
        (
            lambda: core._write_update_path(
                {"path": "scalar"},
                "path.value",
                1,
                rename_role="destination",
            ),
            28,
        ),
        (
            lambda: core._write_update_path(
                {"path": []},
                "path.value",
                1,
                rename_role="destination",
            ),
            28,
        ),
        (lambda: core._write_update_path({"path": []}, "path.value", 1), 28),
        (lambda: core._write_update_path({"path": [None]}, "path.0.value", 1), 28),
        (lambda: core._write_update_path("scalar", "path", 1), 28),
        (
            lambda: core._remove_update_path(
                {"path": [{"value": 1}]},
                "path.0.value",
                rename_role="source",
            ),
            2,
        ),
        (
            lambda: core._remove_update_path(
                {"path": []}, "path.value.child", rename_role="source"
            ),
            28,
        ),
        (
            lambda: core._remove_update_path(
                {"path": []}, "path.0", rename_role="source"
            ),
            2,
        ),
        (
            lambda: core._remove_update_path(
                {"path": []}, "path.value", rename_role="source"
            ),
            28,
        ),
        (
            lambda: core._remove_update_path(
                {"path": "scalar"},
                "path.value.child",
                rename_role="source",
            ),
            28,
        ),
        (
            lambda: core._remove_update_path(
                {"path": "scalar"}, "path.value", rename_role="source"
            ),
            28,
        ),
        (
            lambda: core._apply_update_document(
                {"_id": 1, "value": object()}, {"$min": {"value": 1}}
            ),
            2,
        ),
        (
            lambda: core._apply_update_document({"_id": [1, 2]}, {"$pop": {"_id": 1}}),
            66,
        ),
    )

    for operation, code in error_cases:
        with pytest.raises(WriteError) as caught:
            operation()
        assert caught.value.code == code

    missing_parent = {}
    core._remove_update_path(missing_parent, "missing.value", rename_role="source")
    assert missing_parent == {}

    existing_array_item = {"path": [{}]}
    core._write_update_path(existing_array_item, "path.0.value", 1)
    assert existing_array_item == {"path": [{"value": 1}]}
