"""Issue #77 update-operator contracts shared by every target."""

import pytest

from tinymongo.errors import WriteError as TinyMongoWriteError


pytestmark = pytest.mark.contract

_WRITE_ERRORS = (TinyMongoWriteError,)
try:
    from pymongo.errors import WriteError as PyMongoWriteError
except ImportError:  # pragma: no cover - optional dependency guard
    pass
else:
    _WRITE_ERRORS += (PyMongoWriteError,)


def test_min_and_max_follow_bson_order_and_report_noops(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": "changes",
                "minimum": "text",
                "maximum": 99,
                "nested": {"score": 3},
                "array": [2],
            },
            {
                "_id": "numeric-equal",
                "minimum": 1.0,
                "maximum": 1.0,
            },
        ]
    )

    changed = collection.update_one(
        {"_id": "changes"},
        {
            "$min": {
                "minimum": 7,
                "nested.score": 2,
                "array": [1],
                "created.low": 4,
            },
            "$max": {
                "maximum": {"rank": 1},
                "created.high": 9,
            },
        },
    )
    unchanged = collection.update_one(
        {"_id": "numeric-equal"},
        {
            "$min": {"minimum": 1},
            "$max": {"maximum": 1},
        },
    )

    assert (changed.matched_count, changed.modified_count) == (1, 1)
    assert (unchanged.matched_count, unchanged.modified_count) == (1, 0)
    assert collection.find_one({"_id": "changes"}) == {
        "_id": "changes",
        "minimum": 7,
        "maximum": {"rank": 1},
        "nested": {"score": 2},
        "array": [1],
        "created": {"low": 4, "high": 9},
    }
    equal = collection.find_one({"_id": "numeric-equal"})
    assert type(equal["minimum"]) is float
    assert type(equal["maximum"]) is float


def test_min_and_max_include_null_in_whole_bson_value_order(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": "null-order",
            "min-null": None,
            "max-null": None,
            "min-number": 1,
            "max-number": 1,
        }
    )

    result = collection.update_one(
        {"_id": "null-order"},
        {
            "$min": {"min-null": 1, "min-number": None},
            "$max": {"max-null": 1, "max-number": None},
        },
    )

    assert (result.matched_count, result.modified_count) == (1, 1)
    assert collection.find_one({"_id": "null-order"}) == {
        "_id": "null-order",
        "min-null": None,
        "max-null": 1,
        "min-number": None,
        "max-number": 1,
    }


def test_rename_moves_nested_values_overwrites_and_ignores_missing_source(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": "move",
                "source": {"value": 1},
                "destination": {"stale": True},
                "profile": {"old": "nested", "keep": True},
            },
            {
                "_id": "missing",
                "destination": "unchanged",
            },
        ]
    )

    moved = collection.update_one(
        {"_id": "move"},
        {
            "$rename": {
                "source": "destination",
                "profile.old": "moved.value",
            }
        },
    )
    missing = collection.update_one(
        {"_id": "missing"},
        {"$rename": {"absent": "destination"}},
    )

    assert (moved.matched_count, moved.modified_count) == (1, 1)
    assert (missing.matched_count, missing.modified_count) == (1, 0)
    assert collection.find_one({"_id": "move"}) == {
        "_id": "move",
        "destination": {"value": 1},
        "profile": {"keep": True},
        "moved": {"value": "nested"},
    }
    assert collection.find_one({"_id": "missing"}) == {
        "_id": "missing",
        "destination": "unchanged",
    }


def test_pop_handles_front_back_nested_empty_and_missing_arrays(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": "values",
                "front": [1, 2, 3],
                "back": [1, 2, 3],
                "nested": {"values": [4, 5]},
            },
            {"_id": "empty", "values": []},
            {"_id": "missing"},
        ]
    )

    changed = collection.update_one(
        {"_id": "values"},
        {
            "$pop": {
                "front": -1,
                "back": 1,
                "nested.values": -1,
            }
        },
    )
    empty = collection.update_one({"_id": "empty"}, {"$pop": {"values": 1}})
    missing = collection.update_one(
        {"_id": "missing"}, {"$pop": {"values": 1, "nested.values": -1}}
    )

    assert (changed.matched_count, changed.modified_count) == (1, 1)
    assert (empty.matched_count, empty.modified_count) == (1, 0)
    assert (missing.matched_count, missing.modified_count) == (1, 0)
    assert collection.find_one({"_id": "values"}) == {
        "_id": "values",
        "front": [2, 3],
        "back": [1, 2],
        "nested": {"values": [5]},
    }


def test_new_update_operators_follow_numeric_array_paths(contract_target):
    collection = contract_target.collection
    collection.insert_one(
        {
            "_id": "array-paths",
            "values": [5, 8],
            "nested": [[1, 2], [3]],
            "documents": [{"score": 9}],
        }
    )

    result = collection.update_one(
        {"_id": "array-paths"},
        {
            "$min": {
                "values.0": 3,
                "values.3": 7,
                "documents.2.score": 4,
            },
            "$max": {"values.1": 10},
            "$pop": {"nested.0": 1},
        },
    )

    assert (result.matched_count, result.modified_count) == (1, 1)
    assert collection.find_one({"_id": "array-paths"}) == {
        "_id": "array-paths",
        "values": [3, 10, None, 7],
        "nested": [[1], [3]],
        "documents": [{"score": 9}, None, {"score": 4}],
    }


def test_new_update_operators_cover_upsert_update_many_and_find_and_modify(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "many-change", "group": "many", "low": 10, "high": 0},
            {"_id": "many-noop", "group": "many", "low": 1, "high": 10},
            {"_id": "find", "old": "value", "score": 1},
        ]
    )

    many = collection.update_many(
        {"group": "many"},
        {"$min": {"low": 5}, "$max": {"high": 5}},
    )
    upserted = collection.update_one(
        {"_id": "upserted"},
        {
            "$min": {"low": 3},
            "$max": {"high": 7},
            "$pop": {"missing_array": 1},
        },
        upsert=True,
    )
    after = collection.find_one_and_update(
        {"_id": "find"},
        {"$rename": {"old": "new"}, "$max": {"score": 2}},
        return_document=True,
    )

    assert (many.matched_count, many.modified_count) == (2, 1)
    assert collection.find_one({"_id": "many-change"})["low"] == 5
    assert collection.find_one({"_id": "many-change"})["high"] == 5
    assert collection.find_one({"_id": "many-noop"})["low"] == 1
    assert collection.find_one({"_id": "many-noop"})["high"] == 10
    assert upserted.upserted_id == "upserted"
    assert collection.find_one({"_id": "upserted"}) == {
        "_id": "upserted",
        "low": 3,
        "high": 7,
    }
    assert after == {"_id": "find", "new": "value", "score": 2}


def test_new_update_operators_apply_to_upsert_equality_fields(contract_target):
    collection = contract_target.collection

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


def test_malformed_new_update_operands_report_mongodb_codes(contract_target):
    collection = contract_target.collection
    original = {"_id": "original", "field": 1, "values": [1]}
    collection.insert_one(original)
    cases = (
        ("rename-non-string", {"$rename": {"field": 1}}, 2),
        ("rename-self", {"$rename": {"field": "field"}}, 2),
        ("rename-prefix", {"$rename": {"field": "field.child"}}, 2),
        ("pop-zero", {"$pop": {"values": 0}}, 9),
        ("pop-two", {"$pop": {"values": 2}}, 9),
        ("pop-fraction", {"$pop": {"values": 1.5}}, 9),
        ("pop-boolean", {"$pop": {"values": True}}, 9),
        ("pop-string", {"$pop": {"values": "1"}}, 9),
        ("pop-body", {"$pop": []}, 9),
    )

    for label, update, code in cases:
        with pytest.raises(_WRITE_ERRORS) as caught:
            collection.update_one({"_id": "original"}, update)
        assert caught.value.code == code, label
        assert collection.find_one({"_id": "original"}) == original, label


def test_update_path_conflicts_report_code_40_before_writing(contract_target):
    collection = contract_target.collection
    original = {
        "_id": "conflict",
        "field": {"child": [1, 2]},
        "other": 2,
    }
    collection.insert_one(original)
    conflicts = (
        {"$min": {"other": 1}, "$max": {"other": 3}},
        {"$set": {"field": {}}, "$pop": {"field.child": 1}},
        {"$rename": {"other": "moved"}, "$set": {"moved": 3}},
    )

    for update in conflicts:
        with pytest.raises(_WRITE_ERRORS) as caught:
            collection.update_one({"_id": "conflict"}, update)
        assert caught.value.code == 40
        assert collection.find_one({"_id": "conflict"}) == original


def test_new_update_operators_preserve_immutable_id_semantics(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": 5, "value": 1})

    unchanged = collection.update_one({"_id": 5}, {"$min": {"_id": 7}})
    assert (unchanged.matched_count, unchanged.modified_count) == (1, 0)

    for update in ({"$min": {"_id": 3}}, {"$rename": {"_id": "former_id"}}):
        with pytest.raises(_WRITE_ERRORS) as caught:
            collection.update_one({"_id": 5}, update)
        assert caught.value.code == 66
        assert collection.find_one({"_id": 5}) == {"_id": 5, "value": 1}


def test_new_update_operators_report_target_and_path_errors_atomically(
    contract_target,
):
    collection = contract_target.collection
    cases = (
        ("pop-null", {"values": None}, {"$pop": {"values": 1}}, 14),
        (
            "pop-scalar",
            {"values": "scalar"},
            {"$pop": {"values": -1}},
            14,
        ),
        (
            "pop-blocked-path",
            {"path": "scalar"},
            {"$pop": {"path.values": 1}},
            28,
        ),
        (
            "rename-out-of-array",
            {"path": [1, 2]},
            {"$rename": {"path.0": "moved"}},
            2,
        ),
        (
            "rename-into-array",
            {"path": [1, 2], "moved": 3},
            {"$rename": {"moved": "path.0"}},
            2,
        ),
    )

    for label, document, update, code in cases:
        original = {"_id": label, "status": "unchanged"}
        original.update(document)
        collection.insert_one(original)
        atomic_update = {"$set": {"status": "changed"}}
        atomic_update.update(update)

        with pytest.raises(_WRITE_ERRORS) as caught:
            collection.update_one({"_id": label}, atomic_update)

        assert caught.value.code == code, label
        assert collection.find_one({"_id": label}) == original, label
