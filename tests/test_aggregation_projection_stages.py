"""Issue #97 contracts for projection-oriented aggregation stages."""

import asyncio
from collections import OrderedDict
from uuid import uuid4

import pytest

import tinymongo
from tinymongo.aggregation import AggregationEngine
from tinymongo.asyncio import AsyncTinyMongoClient, AsyncTinyMongoCursor
from tinymongo.errors import OperationFailure, TinyMongoNotSupportedError


def _collection(name):
    return tinymongo.TinyMongoClient(
        "memory://{0}-{1}".format(name, uuid4().hex), backend="memory"
    ).db.items


def _projection_document():
    return {
        "_id": 1,
        "name": "Ada",
        "secret": "hidden",
        "source": 5,
        "a": {"b": 2, "c": 3},
        "arr": [{"x": 1, "keep": "a"}, {"y": 2}, 3],
        "scalar": 5,
        "empty": [],
        "nested": [[{"a": 1}], 2],
        "profile": {"email": "ada@example.com", "secret": "hidden"},
    }


def test_project_supports_inclusion_exclusion_and_id_modes():
    collection = _collection("project-modes")
    collection.insert_one(_projection_document())

    assert collection.aggregate([{"$project": {"name": 1}}]).to_list() == [
        {"_id": 1, "name": "Ada"}
    ]
    assert collection.aggregate(
        [
            {
                "$project": {
                    "secret": 0,
                    "profile.secret": 0,
                    "arr.x": 0,
                    "_id": 1,
                }
            }
        ]
    ).to_list() == [
        {
            "_id": 1,
            "name": "Ada",
            "source": 5,
            "a": {"b": 2, "c": 3},
            "arr": [{"keep": "a"}, {"y": 2}, 3],
            "scalar": 5,
            "empty": [],
            "nested": [[{"a": 1}], 2],
            "profile": {"email": "ada@example.com"},
        }
    ]
    assert collection.aggregate([{"$project": {"_id": 0}}]).to_list() == [
        {key: value for key, value in _projection_document().items() if key != "_id"}
    ]
    nested_exclusion = _projection_document()
    nested_exclusion["a"] = {"c": 3}
    assert collection.aggregate(
        [{"$project": {"_id": 0, "a": {"b": 0}}}]
    ).to_list() == [
        {key: value for key, value in nested_exclusion.items() if key != "_id"}
    ]
    assert collection.aggregate(
        [{"$project": {"_id": False, "name": 2, "absent": 7}}]
    ).to_list() == [{"name": "Ada"}]


def test_project_computes_renames_and_nested_specifications_from_input():
    collection = _collection("project-computed")
    collection.insert_one(_projection_document())

    assert collection.aggregate(
        [
            {
                "$project": {
                    "_id": "$a.b",
                    "a": {"b": 1, "copied": "$source"},
                    "new": {"label": "literal"},
                    "renamed": "$name",
                    "missing": "$missing",
                }
            }
        ]
    ).to_list() == [
        {
            "_id": 2,
            "a": {"b": 2, "copied": 5},
            "new": {"label": "literal"},
            "renamed": "Ada",
        }
    ]


def test_project_preserves_source_order_then_appends_computed_fields():
    collection = _collection("project-output-order")
    collection.insert_one(
        {
            "_id": 1,
            "a": {"y": 2, "x": 1, "old": 0},
            "b": 2,
            "source": 9,
            "c": 3,
        }
    )

    result = collection.aggregate(
        [
            {
                "$project": {
                    "_id": 0,
                    "new_one": "$source",
                    "c": 1,
                    "new_two": "$source",
                    "b": 1,
                    "a.old": "$source",
                    "a.x": 1,
                    "a.y": "$source",
                }
            }
        ]
    ).next()

    assert list(result) == ["a", "b", "c", "new_one", "new_two"]
    assert list(result["a"]) == ["x", "old", "y"]
    assert result == {
        "a": {"x": 1, "old": 9, "y": 9},
        "b": 2,
        "c": 3,
        "new_one": 9,
        "new_two": 9,
    }


def test_project_dotted_outputs_preserve_array_shape_and_missing_shells():
    collection = _collection("project-dotted-arrays")
    collection.insert_one(_projection_document())

    assert collection.aggregate(
        [
            {
                "$project": {
                    "_id": 0,
                    "arr.x": 1,
                    "arr.z": "$source",
                    "scalar.z": "$source",
                    "empty.z": "$source",
                    "nested.z": "$source",
                    "missing.z": "$missing",
                }
            }
        ]
    ).to_list() == [
        {
            "arr": [{"x": 1, "z": 5}, {"z": 5}, {"z": 5}],
            "scalar": {"z": 5},
            "empty": [],
            "nested": [[{"z": 5}], {"z": 5}],
            "missing": {},
        }
    ]


def test_project_dotted_inclusion_skips_scalar_array_members():
    collection = _collection("project-dotted-inclusion")
    collection.insert_one(_projection_document())

    assert collection.aggregate([{"$project": {"_id": 0, "arr.x": 1}}]).to_list() == [
        {"arr": [{"x": 1}, {}]}
    ]


def test_computed_project_keeps_include_only_branch_rules():
    collection = _collection("project-computed-include-branches")
    collection.insert_one(
        {
            "_id": 1,
            "source": 5,
            "arr": [{"x": 1}, 2],
            "scalar": 3,
        }
    )

    assert collection.aggregate(
        [
            {
                "$project": {
                    "arr.x": 1,
                    "scalar.x": 1,
                    "copy": "$source",
                }
            }
        ]
    ).to_list() == [{"_id": 1, "arr": [{"x": 1}], "copy": 5}]


def test_literal_emits_constants_without_evaluating_its_operand():
    collection = _collection("project-literal")
    collection.insert_one(_projection_document())

    assert collection.aggregate(
        [
            {
                "$project": {
                    "_id": 0,
                    "numeric": {"$literal": 2},
                    "boolean": {"$literal": False},
                    "path": {"$literal": "$source"},
                    "document": {"$literal": {"$size": "$arr"}},
                    "array": {"$literal": ("$source", {"$size": "$arr"})},
                    "projection_flag": 2,
                }
            }
        ]
    ).to_list() == [
        {
            "numeric": 2,
            "boolean": False,
            "path": "$source",
            "document": {"$size": "$arr"},
            "array": ["$source", {"$size": "$arr"}],
        }
    ]


def test_pipeline_binary_constants_follow_bson_generic_subtype_decoding():
    binary = pytest.importorskip("bson.binary")
    generic = binary.Binary(b"generic", subtype=0)
    custom = binary.Binary(b"custom", subtype=128)
    collection = _collection("project-binary-literal")
    collection.insert_one({"_id": 1})

    assert collection.aggregate(
        [
            {
                "$project": {
                    "_id": 0,
                    "direct": generic,
                    "literal": {"$literal": generic},
                    "custom": {"$literal": custom},
                }
            }
        ]
    ).to_list() == [
        {
            "direct": b"generic",
            "literal": b"generic",
            "custom": custom,
        }
    ]


def test_remove_is_missing_except_inside_expression_arrays_or_literal():
    collection = _collection("project-remove")
    collection.insert_one(_projection_document())

    assert collection.aggregate(
        [
            {
                "$project": {
                    "_id": 0,
                    "name": 1,
                    "secret": "$$REMOVE",
                    "array": ["$$REMOVE"],
                    "fallback": {"$ifNull": ["$$REMOVE", "fallback"]},
                    "literal": {"$literal": "$$REMOVE"},
                    "new.deep": "$$REMOVE",
                    "arr.z": "$$REMOVE",
                }
            }
        ]
    ).to_list() == [
        {
            "name": "Ada",
            "array": [None],
            "fallback": "fallback",
            "literal": "$$REMOVE",
            "new": {},
            "arr": [{}, {}, {}],
        }
    ]


def test_remove_field_references_also_resolve_to_missing():
    collection = _collection("project-remove-field-reference")
    collection.insert_one({"_id": 1, "gone": True, "nested": {"gone": True}})

    assert collection.aggregate(
        [
            {
                "$project": {
                    "_id": 0,
                    "gone": "$$REMOVE.foo",
                    "holder.deep": "$$REMOVE.foo.bar",
                    "array": ["$$REMOVE.0"],
                }
            }
        ]
    ).to_list() == [{"holder": {}, "array": [None]}]
    assert collection.aggregate(
        [{"$set": {"gone": "$$REMOVE.foo", "nested.gone": "$$REMOVE.0"}}]
    ).to_list() == [{"_id": 1, "nested": {}}]


def test_nested_arrays_are_not_recursively_crossed_by_field_references():
    collection = _collection("project-nested-array-reference")
    collection.insert_one(_projection_document())

    assert collection.aggregate(
        [{"$project": {"_id": 0, "value": "$nested.a"}}]
    ).to_list() == [{"value": []}]


@pytest.mark.parametrize("stage_name", ["$set", "$addFields"])
def test_set_and_addfields_are_aliases_and_use_the_original_input(stage_name):
    collection = _collection("set-original-input")
    collection.insert_one(
        {
            "_id": 1,
            "name": "Ada",
            "gone": "remove me",
            "nested": {"value": 3},
        }
    )

    assert collection.aggregate(
        [
            {
                stage_name: {
                    "_id": 2,
                    "name": "new",
                    "previous": "$name",
                    "old_id": "$_id",
                    "nested_copy": "$nested.value",
                    "gone": "$missing",
                }
            }
        ]
    ).to_list() == [
        {
            "_id": 2,
            "name": "new",
            "nested": {"value": 3},
            "previous": "Ada",
            "old_id": 1,
            "nested_copy": 3,
        }
    ]


@pytest.mark.parametrize("stage_name", ["$set", "$addFields"])
def test_set_and_addfields_accept_an_empty_noop(stage_name):
    collection = _collection("set-empty")
    document = {"_id": 1, "nested": {"value": 2}}
    collection.insert_one(document)

    assert collection.aggregate([{stage_name: {}}]).to_list() == [document]


def test_set_writes_dotted_paths_through_objects_scalars_and_arrays():
    collection = _collection("set-dotted")
    collection.insert_one(_projection_document())

    assert collection.aggregate(
        [
            {
                "$set": {
                    "a.new": "$source",
                    "arr.z": "$source",
                    "scalar.z": "$source",
                    "empty.z": "$source",
                    "nested.z": "$source",
                    "missing.z": "$missing",
                }
            }
        ]
    ).to_list() == [
        {
            "_id": 1,
            "name": "Ada",
            "secret": "hidden",
            "source": 5,
            "a": {"b": 2, "c": 3, "new": 5},
            "arr": [
                {"x": 1, "keep": "a", "z": 5},
                {"y": 2, "z": 5},
                {"z": 5},
            ],
            "scalar": {"z": 5},
            "empty": [],
            "nested": [[{"a": 1, "z": 5}], {"z": 5}],
            "profile": {"email": "ada@example.com", "secret": "hidden"},
            "missing": {},
        }
    ]


def test_set_direct_engine_normalizes_tuple_containers_to_arrays():
    document = {
        "tuple_parent": ({"old": True}, 2),
        "array": [({"old": True}, 2)],
    }

    assert AggregationEngine().run(
        [document],
        [{"$set": {"tuple_parent.new": 1, "array.new": 1}}],
    ) == [
        {
            "tuple_parent": [{"old": True, "new": 1}, {"new": 1}],
            "array": [[{"old": True, "new": 1}, {"new": 1}]],
        }
    ]
    assert document == {
        "tuple_parent": ({"old": True}, 2),
        "array": [({"old": True}, 2)],
    }


def test_set_nested_specs_modify_paths_while_literal_replaces_whole_values():
    collection = _collection("set-nested-spec")
    collection.insert_one(
        {
            "_id": 1,
            "a": {"b": 2, "c": 3},
            "whole": {"old": True},
            "empty": {"old": True},
        }
    )

    assert collection.aggregate(
        [
            {
                "$set": {
                    "a": {"b": 1},
                    "whole": {"$literal": {"b": 1}},
                    "empty": {},
                }
            }
        ]
    ).to_list() == [
        {
            "_id": 1,
            "a": {"b": 1, "c": 3},
            "whole": {"b": 1},
            "empty": {},
        }
    ]


def test_set_remove_deletes_leaves_and_preserves_or_creates_containers():
    collection = _collection("set-remove")
    collection.insert_one(_projection_document())

    assert collection.aggregate(
        [
            {
                "$set": {
                    "secret": "$$REMOVE",
                    "a.b": "$$REMOVE",
                    "arr.x": "$$REMOVE",
                    "scalar.z": "$$REMOVE",
                    "new.deep": "$$REMOVE",
                }
            }
        ]
    ).to_list() == [
        {
            "_id": 1,
            "name": "Ada",
            "source": 5,
            "a": {"c": 3},
            "arr": [{"keep": "a"}, {"y": 2}, {}],
            "scalar": {},
            "empty": [],
            "nested": [[{"a": 1}], 2],
            "profile": {"email": "ada@example.com", "secret": "hidden"},
            "new": {},
        }
    ]


def test_unset_accepts_a_string_list_or_tuple_and_uses_exclusion_semantics():
    collection = _collection("unset-valid")
    collection.insert_one(_projection_document())

    assert collection.aggregate([{"$unset": "secret"}]).to_list() == [
        {key: value for key, value in _projection_document().items() if key != "secret"}
    ]

    expected = {
        "name": "Ada",
        "source": 5,
        "a": {"b": 2, "c": 3},
        "arr": [{"keep": "a"}, {"y": 2}, 3],
        "scalar": 5,
        "empty": [],
        "nested": [[{"a": 1}], 2],
        "profile": {"email": "ada@example.com"},
    }
    paths = ["_id", "secret", "profile.secret", "arr.x"]
    assert collection.aggregate([{"$unset": paths}]).to_list() == [expected]
    assert collection.aggregate([{"$unset": tuple(paths)}]).to_list() == [expected]


@pytest.mark.parametrize(
    ("pipeline", "code"),
    [
        ([{"$project": {}}], 51272),
        ([{"$project": []}], 15969),
        ([{"$project": {"a": {}}}], 51270),
        ([{"$project": {"a": {"": 1}}}], 40352),
        ([{"$project": {"value": {"$ifNull": []}}}], 1257300),
        ([{"$project": {"value": {"$size": []}}}], 16020),
        ([{"$project": OrderedDict((("a", 0), ("copy", "$source")))}], 31310),
        (
            [{"$project": OrderedDict((("a", 0), ("copy", {"$literal": 1})))}],
            31252,
        ),
        (
            [{"$project": OrderedDict((("a", 0), ("copy", {"$ifNull": []})))}],
            31252,
        ),
        ([{"$project": OrderedDict((("copy", "$source"), ("a", 0)))}], 31254),
        ([{"$project": OrderedDict((("a", 0), ("name", 1)))}], 31253),
        ([{"$project": OrderedDict((("a", 1), ("a.b", 1)))}], 31249),
        ([{"$project": OrderedDict((("a.b", 1), ("a", 1)))}], 31250),
        ([{"$set": OrderedDict((("a", 1), ("a.b", 2)))}], 40176),
        ([{"$addFields": OrderedDict((("a.b", 2), ("a", 1)))}], 40176),
        ([{"$set": []}], 40272),
        ([{"$set": {"": 1}}], 40352),
        ([{"$project": {"a.": 1}}], 40353),
        ([{"$set": {"a.": 1}}], 40353),
        ([{"$unset": "a."}], 40353),
        ([{"$project": {"$bad": 1}}], 16410),
        ([{"$unset": []}], 31119),
        ([{"$unset": ""}], 40352),
        ([{"$unset": ["", ""]}], 40352),
        ([{"$unset": ["a.", "a."]}], 40353),
        ([{"$unset": 1}], 31002),
        ([{"$unset": ["a", 1]}], 31120),
        ([{"$unset": ["a", "a"]}], 31250),
        ([{"$unset": ["a", "a.b"]}], 31249),
        ([{"$unset": ["a.b", "a"]}], 31250),
    ],
)
def test_projection_stage_errors_match_mongodb_before_storage_read(pipeline, code):
    client = tinymongo.TinyMongoClient(
        "memory://projection-errors-{0}".format(uuid4().hex), backend="memory"
    )
    collection = client.db.items

    with pytest.raises(OperationFailure) as caught:
        collection.aggregate(pipeline)

    assert caught.value.code == code
    assert client.db.list_collection_names() == []


@pytest.mark.parametrize(
    ("pipeline", "message"),
    [
        ([{"$project": {"value": {"$add": [1, 2]}}}], r"\$add"),
        ([{"$set": {"arr.0": 1}}], "numeric array"),
        ([{"$set": {"value": "$$ROOT"}}], r"\$\$ROOT"),
        ([{"$addFields": {"value": "$$CURRENT"}}], r"\$\$CURRENT"),
        ([{"$unset": "arr.0"}], "numeric array"),
    ],
)
def test_projection_stage_unsupported_features_fail_before_storage_read(
    pipeline, message
):
    client = tinymongo.TinyMongoClient(
        "memory://projection-unsupported-{0}".format(uuid4().hex), backend="memory"
    )

    with pytest.raises(TinyMongoNotSupportedError, match=message):
        client.db.items.aggregate(pipeline)

    assert client.db.list_collection_names() == []


@pytest.mark.parametrize(
    ("reference", "code"),
    [
        ("$$REMOVE.", 40353),
        ("$$REMOVE.foo.", 40353),
        ("$$REMOVE..foo", 15998),
        ("$$REMOVE.foo..bar", 15998),
        ("$$REMOVE.$foo", 16410),
    ],
)
def test_remove_field_reference_suffixes_are_validated(reference, code):
    client = tinymongo.TinyMongoClient(
        "memory://projection-remove-path-{0}".format(uuid4().hex), backend="memory"
    )

    with pytest.raises(OperationFailure) as caught:
        client.db.items.aggregate([{"$project": {"value": reference}}])

    assert caught.value.code == code
    assert client.db.list_collection_names() == []


def test_project_validates_paths_and_expressions_before_later_mode_conflicts():
    client = tinymongo.TinyMongoClient(
        "memory://projection-validation-order-{0}".format(uuid4().hex),
        backend="memory",
    )

    with pytest.raises(OperationFailure) as invalid_path:
        client.db.items.aggregate(
            [{"$project": OrderedDict((("a.", 1), ("excluded", 0)))}]
        )
    assert invalid_path.value.code == 40353

    with pytest.raises(OperationFailure, match=r"\$ifNull") as invalid_expression:
        client.db.items.aggregate(
            [{"$project": OrderedDict((("value", {"$ifNull": []}), ("excluded", 0)))}]
        )
    assert invalid_expression.value.code == 1257300
    assert client.db.list_collection_names() == []


@pytest.mark.parametrize("stage_name", ["$set", "$addFields"])
def test_set_stage_requires_a_document_before_storage_read(stage_name):
    client = tinymongo.TinyMongoClient(
        "memory://set-invalid-{0}".format(uuid4().hex), backend="memory"
    )

    with pytest.raises(OperationFailure, match="document"):
        client.db.items.aggregate([{stage_name: []}])

    assert client.db.list_collection_names() == []


def test_projection_stage_rejects_non_string_output_fields_before_read():
    client = tinymongo.TinyMongoClient(
        "memory://projection-non-string-{0}".format(uuid4().hex), backend="memory"
    )

    with pytest.raises(TypeError, match="field names"):
        client.db.items.aggregate([{"$project": {1: 1}}])

    assert client.db.list_collection_names() == []


def test_async_projection_stages_smoke():
    async def scenario():
        client = AsyncTinyMongoClient(
            "memory://projection-stages-async-{0}".format(uuid4().hex),
            backend="memory",
        )
        collection = client.db.items
        await collection.insert_many(
            [
                {
                    "_id": 1,
                    "name": "Ada",
                    "items": [1, 2],
                    "obsolete": True,
                },
                {"_id": 2, "name": "Grace", "obsolete": True},
            ]
        )

        cursor = await collection.aggregate(
            [
                {
                    "$set": {
                        "count": {"$size": {"$ifNull": ["$items", []]}},
                        "copy": "$name",
                    }
                },
                {
                    "$addFields": {
                        "literal": {"$literal": "$name"},
                        "obsolete": "$$REMOVE",
                    }
                },
                {"$unset": "items"},
                {
                    "$project": {
                        "_id": 0,
                        "name": 1,
                        "copy": 1,
                        "count": 1,
                        "literal": 1,
                    }
                },
            ]
        )

        assert isinstance(cursor, AsyncTinyMongoCursor)
        assert await cursor.to_list() == [
            {"name": "Ada", "copy": "Ada", "count": 2, "literal": "$name"},
            {
                "name": "Grace",
                "copy": "Grace",
                "count": 0,
                "literal": "$name",
            },
        ]
        await client.close()

    asyncio.run(scenario())
