"""Focused validation and edge tests for TinyMongo aggregation."""

import copy
from uuid import uuid4

import pytest

import tinymongo
from tinymongo.aggregation import (
    AggregationContext,
    AggregationEngine,
    aggregation_capabilities,
)
from tinymongo.asyncio import AsyncTinyMongoClient, AsyncTinyMongoCursor
from tinymongo.bson_types import bson_value_identity_key, bson_value_sort_key
from tinymongo.errors import OperationFailure, TinyMongoNotSupportedError
from tinymongo.tinymongo import TinyMongoCursor


def _collection(name="aggregation-unit"):
    return tinymongo.TinyMongoClient(
        "memory://{0}-{1}".format(name, uuid4().hex), backend="memory"
    ).db.items


def test_empty_pipeline_returns_cloneable_snapshot_without_mutation():
    collection = _collection("aggregation-snapshot")
    stored = {"_id": 1, "nested": {"value": 2}}
    collection.insert_one(stored)
    pipeline = []

    cursor = collection.aggregate(pipeline)
    clone = cursor.clone()
    first = cursor.next()
    first["nested"]["value"] = 99

    assert isinstance(cursor, TinyMongoCursor)
    assert clone.to_list() == [{"_id": 1, "nested": {"value": 2}}]
    assert collection.find_one({"_id": 1}) == stored
    assert pipeline == []


def test_dotted_paths_arrays_and_bson_group_identity():
    collection = _collection("aggregation-paths")
    collection.insert_many(
        [
            {"_id": 1, "key": 1, "nested": {"amount": 2}},
            {"_id": 2, "key": 1.0, "nested": {"amount": 3}},
            {"_id": 3, "key": True, "nested": {"amount": 4}},
            {"_id": 4, "key": ["a", "b"], "nested": {"amount": 5}},
            {"_id": 5, "key": ["a", "b"], "nested": {"amount": 6}},
            {"_id": 6, "key": {"kind": "object"}, "nested": {"amount": 7}},
            {"_id": 7, "key": {"kind": "object"}, "nested": {"amount": 8}},
        ]
    )

    rows = collection.aggregate(
        [
            {
                "$group": {
                    "_id": "$key",
                    "total": {"$sum": "$nested.amount"},
                }
            }
        ]
    ).to_list()

    assert rows == [
        {"_id": 1, "total": 5},
        {"_id": True, "total": 4},
        {"_id": ["a", "b"], "total": 11},
        {"_id": {"kind": "object"}, "total": 15},
    ]
    context = AggregationContext()
    assert context.resolve_field_path(
        {"items": [{"score": 1}, {}, {"score": 3}]}, "$items.score"
    ) == [1, 3]
    assert context.resolve_field_path({"items": []}, "$items.score") == []
    assert context.resolve_field_path({"items": [{}, {}]}, "$items.score") == []
    missing_path = collection.aggregate(
        [{"$group": {"_id": "$nested.amount.missing", "n": {"$sum": 1}}}]
    ).to_list()
    assert missing_path == [{"_id": None, "n": 7}]


def test_shared_bson_order_rejects_unsupported_container_members():
    marker = object()

    assert bson_value_sort_key({"value": marker}) is None
    assert bson_value_sort_key([marker]) is None
    assert bson_value_identity_key({"value": marker}) is None
    assert bson_value_identity_key([marker]) is None
    assert bson_value_identity_key(marker) is None
    assert bson_value_identity_key({"a": 1, "b": 2}) != bson_value_identity_key(
        {"b": 2, "a": 1}
    )
    with pytest.raises(TinyMongoNotSupportedError, match="group.*object"):
        AggregationEngine().run(
            [{"key": marker}],
            [{"$group": {"_id": "$key", "n": {"$sum": 1}}}],
        )


@pytest.mark.parametrize("pipeline", [None, (), {}, iter(())])
def test_pipeline_must_be_a_list(pipeline):
    with pytest.raises(TypeError, match="pipeline must be a list"):
        _collection("aggregation-pipeline-type").aggregate(pipeline)


@pytest.mark.parametrize(
    "stage",
    [
        None,
        {},
        {"$match": {}, "$group": {"_id": None}},
        {"match": {}},
    ],
)
def test_pipeline_stage_shape_is_validated(stage):
    with pytest.raises(OperationFailure, match="stage"):
        _collection("aggregation-stage-shape").aggregate([stage])


def test_pipeline_is_prepared_before_collection_storage_is_opened():
    client = tinymongo.TinyMongoClient(
        "memory://aggregation-prepare-{0}".format(uuid4().hex), backend="memory"
    )
    database = client.db
    collection = database.items

    with pytest.raises(TinyMongoNotSupportedError, match=r"\$lookup"):
        collection.aggregate([{"$lookup": {}}])

    assert database.list_collection_names() == []
    assert AggregationEngine().run([{"_id": 1}], []) == [{"_id": 1}]


def test_leading_match_is_pushed_into_the_collection_query(monkeypatch):
    collection = _collection("aggregation-match-pushdown")
    collection.insert_many([{"_id": 1, "keep": True}, {"_id": 2, "keep": False}])
    queries = []
    original_find = collection.find

    def tracking_find(query, *args, **kwargs):
        queries.append(copy.deepcopy(query))
        return original_find(query, *args, **kwargs)

    monkeypatch.setattr(collection, "find", tracking_find)

    assert collection.aggregate([{"$match": {"keep": True}}]).to_list() == [
        {"_id": 1, "keep": True}
    ]
    assert queries == [{"keep": True}]

    queries.clear()
    collection.aggregate(
        [
            {"$group": {"_id": "$keep", "n": {"$sum": 1}}},
            {"$match": {"n": 1}},
        ]
    ).to_list()
    assert queries == [{}]


def test_project_includes_fields_and_computes_against_original_document():
    collection = _collection("aggregation-project-computed")
    stored = {
        "_id": 1,
        "name": "Ada",
        "count": 99,
        "zero": 0,
        "lectures": ["intro", "loops"],
        "profile": {"email": "ada@example.com", "secret": "hidden"},
    }
    collection.insert_one(stored)
    pipeline = [
        {
            "$project": {
                "_id": 0,
                "name": 1,
                "profile.email": 1,
                "alias": "$name",
                "count": {"$size": {"$ifNull": ["$lectures", []]}},
                "old_count": "$count",
                "choice": {"$ifNull": ["$missing", "$zero", "fallback"]},
                "omitted": "$missing",
            }
        }
    ]
    original_pipeline = copy.deepcopy(pipeline)

    assert collection.aggregate(pipeline).to_list() == [
        {
            "name": "Ada",
            "profile": {"email": "ada@example.com"},
            "alias": "Ada",
            "count": 2,
            "old_count": 99,
            "choice": 0,
        }
    ]
    assert pipeline == original_pipeline
    assert collection.find_one({"_id": 1}) == stored


def test_project_default_id_special_id_exclusion_and_missing_inclusion():
    collection = _collection("aggregation-project-id")
    collection.insert_one(
        {
            "_id": 1,
            "name": "Ada",
            "secret": "hidden",
            "profile": {"name": "Ada", "secret": "hidden"},
        }
    )

    assert collection.aggregate(
        [{"$project": {"name": 1, "missing": 1, "copy": "$name"}}]
    ).to_list() == [{"_id": 1, "name": "Ada", "copy": "Ada"}]
    assert collection.aggregate([{"$project": {"profile": {"name": 1}}}]).to_list() == [
        {"_id": 1, "profile": {"name": "Ada"}}
    ]
    assert collection.aggregate([{"$project": {"_id": 0}}]).to_list() == [
        {
            "name": "Ada",
            "secret": "hidden",
            "profile": {"name": "Ada", "secret": "hidden"},
        }
    ]


def test_project_ifnull_preserves_non_null_values_and_size_accepts_arrays():
    collection = _collection("aggregation-project-values")
    collection.insert_one(
        {
            "_id": 1,
            "zero": 0,
            "false": False,
            "empty": [],
            "nullable": None,
            "values": [1, 2, 3],
        }
    )

    assert collection.aggregate(
        [
            {
                "$project": {
                    "_id": 0,
                    "zero": {"$ifNull": ["$zero", 10]},
                    "false": {"$ifNull": ["$false", True]},
                    "empty": {"$ifNull": ["$empty", [1]]},
                    "fallback": {"$ifNull": ["$missing", "$nullable", "last"]},
                    "missing_fallback": {"$ifNull": ["$missing", "$also_missing"]},
                    "lazy": {"$ifNull": ["$zero", {"$size": "$nullable"}]},
                    "size": {"$size": ["$values"]},
                    "empty_size": {"$size": {"$ifNull": ["$missing", []]}},
                    "literal_size": {"$size": [[1, 2]]},
                    "literal_empty_size": {"$size": [[]]},
                }
            }
        ]
    ).to_list() == [
        {
            "zero": 0,
            "false": False,
            "empty": [],
            "fallback": "last",
            "lazy": 0,
            "size": 3,
            "empty_size": 0,
            "literal_size": 2,
            "literal_empty_size": 0,
        }
    ]


@pytest.mark.parametrize(
    "document",
    [
        {"_id": 1},
        {"_id": 1, "value": None},
        {"_id": 1, "value": "three"},
        {"_id": 1, "value": {"nested": True}},
        {"_id": 1, "value": 3},
    ],
)
def test_project_size_rejects_non_array_runtime_values(document):
    collection = _collection("aggregation-project-size-type")
    collection.insert_one(document)

    with pytest.raises(OperationFailure, match=r"\$size.*array") as caught:
        collection.aggregate([{"$project": {"size": {"$size": "$value"}}}])
    assert caught.value.code == 17124


@pytest.mark.parametrize(
    ("pipeline", "error", "message"),
    [
        ([{"$project": []}], OperationFailure, r"\$project"),
        ([{"$project": {}}], OperationFailure, "non-empty"),
        (
            [{"$project": {"value": {"$add": [1, 2]}}}],
            TinyMongoNotSupportedError,
            r"\$add",
        ),
        (
            [{"$project": {"value": "$a..b"}}],
            OperationFailure,
            "components",
        ),
        (
            [{"$project": {"value": {"$size": {"$ifNull": ["$a.$bad", []]}}}}],
            OperationFailure,
            "start with",
        ),
        (
            [{"$project": {"value": {"$ifNull": "$missing"}}}],
            OperationFailure,
            r"\$ifNull",
        ),
        (
            [{"$project": {"value": {"$ifNull": ["$missing"]}}}],
            OperationFailure,
            r"\$ifNull",
        ),
        (
            [{"$project": {"value": {"$size": []}}}],
            OperationFailure,
            r"\$size",
        ),
        (
            [{"$project": {"value": {"$size": [1, 2]}}}],
            OperationFailure,
            r"\$size",
        ),
        (
            [{"$project": {"value": {"$size": "$items", "extra": 1}}}],
            OperationFailure,
            "one operator",
        ),
        (
            [{"$project": {"parent": 1, "parent.child": "$value"}}],
            OperationFailure,
            "Path collision",
        ),
        (
            [{"$project": {"parent.child": "$value", "parent": 1}}],
            OperationFailure,
            "Path collision",
        ),
        (
            [
                {
                    "$group": {
                        "_id": None,
                        "total": {"$sum": {"$add": [1, 2]}},
                    }
                }
            ],
            TinyMongoNotSupportedError,
            r"\$add",
        ),
        (
            [{"$group": {"_id": "$a.", "total": {"$sum": 1}}}],
            OperationFailure,
            "end with",
        ),
        (
            [{"$group": {"_id": None, "total": {"$sum": "$a\x00b"}}}],
            OperationFailure,
            "null bytes",
        ),
    ],
)
def test_project_validation_fails_before_storage_read(pipeline, error, message):
    client = tinymongo.TinyMongoClient(
        "memory://aggregation-project-validation-{0}".format(uuid4().hex),
        backend="memory",
    )
    collection = client.db.items

    with pytest.raises(error, match=message):
        collection.aggregate(pipeline)
    assert client.db.list_collection_names() == []


@pytest.mark.parametrize(
    ("pipeline", "message"),
    [
        ([{"$lookup": {}}], r"\$lookup"),
        (
            [{"$group": {"_id": "$key", "value": {"$madeUp": "$value"}}}],
            r"\$madeUp",
        ),
        ([{"$group": {"_id": "literal"}}], "field path or None"),
        ([{"$group": {"_id": "$$ROOT"}}], "field path or None"),
        (
            [{"$group": {"_id": "$key", "top": {"$max": {"$add": [1, 2]}}}}],
            r"\$add",
        ),
    ],
)
def test_unsupported_features_name_the_feature(pipeline, message):
    collection = _collection("aggregation-unsupported")
    collection.insert_one({"_id": 1, "key": "one"})
    with pytest.raises(TinyMongoNotSupportedError, match=message):
        collection.aggregate(pipeline)


@pytest.mark.parametrize(
    ("pipeline", "message"),
    [
        (
            [{"$group": {"_id": None, "top": {"$max": {"$add": [1, 2]}}}}],
            r"\$add",
        ),
        (
            [{"$group": {"_id": None, "total": {"$sum": "$$ROOT"}}}],
            r"\$\$ROOT",
        ),
    ],
)
def test_unsupported_expressions_fail_before_empty_input_is_read(pipeline, message):
    with pytest.raises(TinyMongoNotSupportedError, match=message):
        _collection("aggregation-empty-unsupported").aggregate(pipeline)


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ({"$expr": {"$eq": ["$value", 1]}}, r"\$expr"),
        ({"value": {"$madeUp": 1}}, r"\$madeUp"),
        ({"$and": []}, r"\$and"),
        ({"$or": ["bad"]}, "Filter"),
        ({1: "bad"}, "field names"),
        ({"value": {"$options": "i"}}, r"\$options"),
        ({"value": {"$gt": 1, "literal": 2}}, "mix operators"),
        ({"value": {"$not": {"$madeUp": 1}}}, r"\$madeUp"),
        ({"value": {"$in": 1}}, r"\$in.*array"),
        ({"value": {"$nin": "one"}}, r"\$nin.*array"),
        ({"value": {"$all": 1}}, r"\$all.*array"),
        ({"value": {"$not": {"$in": 1}}}, r"\$in.*array"),
        (
            {"value": {"$not": {"$not": {"$madeUp": 1}}}},
            r"\$madeUp",
        ),
        (
            {"value": {"$not": {"$gt": 1, "literal": 2}}},
            "mix operators",
        ),
        ({"value": {"$not": {"$options": "i"}}}, r"\$options"),
    ],
)
def test_match_rejects_unknown_or_malformed_query_operators(query, message):
    with pytest.raises((OperationFailure, TinyMongoNotSupportedError), match=message):
        _collection("aggregation-match-validation").aggregate([{"$match": query}])


@pytest.mark.parametrize(
    "query",
    [
        {"value": {"nested": 1}},
        {"value": {"$not": {"$gt": 1}}},
    ],
)
def test_match_accepts_literal_documents_and_valid_nested_not(query):
    assert (
        _collection("aggregation-match-valid").aggregate([{"$match": query}]).to_list()
        == []
    )


@pytest.mark.parametrize(
    "pipeline",
    [
        [{"$match": []}],
        [{"$group": []}],
        [{"$group": {}}],
        [{"$group": {"_id": None, "bad.name": {"$sum": 1}}}],
        [{"$group": {"_id": None, "$bad": {"$sum": 1}}}],
        [{"$group": {"_id": None, "bad": []}}],
        [{"$group": {"_id": None, "bad": {"$sum": 1, "$max": 2}}}],
    ],
)
def test_supported_stage_arguments_are_validated(pipeline):
    with pytest.raises(OperationFailure):
        _collection("aggregation-stage-arguments").aggregate(pipeline)


@pytest.mark.parametrize(
    ("group", "code"),
    [
        ({"_id": None, "value": []}, 40234),
        ({"_id": None, "value": {}}, 40234),
        ({"_id": None, "value": {"notAnAccumulator": 1}}, 40234),
        ({"_id": None, "value": {1: 1}}, 40234),
        ({"_id": None, "bad.name": {"$sum": 1}}, 40235),
        ({"_id": None, "value": {"$sum": 1, "$max": 1}}, 40238),
        ({"_id": None, "$value": {"$sum": 1}}, 40236),
        ({"_id": None, "$value": []}, 40234),
        ({"_id": None, "bad.name": []}, 40234),
        ({"_id": None, "value": {"$sum": [1, 2]}}, 40237),
        ({"_id": None, "value": {"$madeUp": [1, 2]}}, 40237),
    ],
)
def test_group_validation_reports_mongodb_error_codes(group, code):
    with pytest.raises(OperationFailure) as caught:
        _collection("aggregation-group-validation-codes").aggregate([{"$group": group}])

    assert caught.value.code == code


def test_group_rejects_non_string_output_fields():
    with pytest.raises(OperationFailure, match="plain strings"):
        _collection("aggregation-group-non-string-output").aggregate(
            [{"$group": {"_id": None, 1: {"$sum": 1}}}]
        )


def test_sessions_options_and_unsupported_comparison_values_fail_loudly():
    collection = _collection("aggregation-options")
    collection.insert_one({"_id": 1, "value": 2})

    with pytest.raises(TinyMongoNotSupportedError, match="Sessions"):
        collection.aggregate([], session=object())
    with pytest.raises(TinyMongoNotSupportedError, match="Sessions"):
        collection.aggregate([], {})
    with pytest.raises(TinyMongoNotSupportedError, match="allowDiskUse"):
        collection.aggregate([], allowDiskUse=True)
    assert collection.aggregate([], None).to_list() == [{"_id": 1, "value": 2}]
    with pytest.raises(TypeError):
        collection.aggregate([], None, None)

    marker = object()
    with pytest.raises(TinyMongoNotSupportedError, match="object"):
        collection.aggregate([{"$group": {"_id": None, "maximum": {"$max": marker}}}])


def test_capability_descriptions_are_fresh_and_supports_is_true():
    first = aggregation_capabilities()
    second = aggregation_capabilities()
    first["stages"] = ()

    client = tinymongo.TinyMongoClient(backend="memory")
    assert second["stages"] == (
        "$match",
        "$sort",
        "$skip",
        "$limit",
        "$count",
        "$project",
        "$set",
        "$addFields",
        "$unset",
        "$group",
    )
    assert second["expressions"] == ("$ifNull", "$literal", "$size")
    assert second["accumulators"] == (
        "$addToSet",
        "$avg",
        "$first",
        "$last",
        "$max",
        "$min",
        "$push",
        "$sum",
    )
    assert client.capabilities()["aggregation"]["stages"] == (
        "$match",
        "$sort",
        "$skip",
        "$limit",
        "$count",
        "$project",
        "$set",
        "$addFields",
        "$unset",
        "$group",
    )
    assert client.supports("aggregation") is True


def test_expression_literals_are_copied_and_unsupported_paths_fail():
    context = AggregationContext()
    expression = {"literal": [1, {"value": 2}]}
    result = context.evaluate({}, expression)
    result["literal"][1]["value"] = 3

    assert expression == {"literal": [1, {"value": 2}]}
    assert context.evaluate({}, {"missing": "$missing"}) == {}
    assert context.evaluate({}, (1, 2)) == [1, 2]
    assert context.evaluate({}, {"$size": ((1, 2),)}, ("$size",)) == 2
    assert context.resolve_field_path({"reference": {"$id": 7}}, "$reference.$id") == 7
    with pytest.raises(OperationFailure, match=r"\$ifNull"):
        context.evaluate({}, {"$ifNull": ["$missing"]}, ("$ifNull",))
    with pytest.raises(TinyMongoNotSupportedError, match=r"\$add"):
        context.evaluate({}, {"$add": [1, 2]})
    for path in ("field", "$"):
        with pytest.raises(TinyMongoNotSupportedError, match="field paths"):
            context.resolve_field_path({}, path)
    with pytest.raises(TinyMongoNotSupportedError, match=r"\$\$ROOT"):
        context.resolve_field_path({}, "$$ROOT")
    context.validate_expression(1)
    context.validate_expression(["$field", (1, {"literal": 2})])


def test_repeated_group_results_are_deterministic():
    collection = _collection("aggregation-order")
    collection.insert_many(
        [
            {"_id": 1, "group": "b"},
            {"_id": 2, "group": "a"},
            {"_id": 3, "group": "b"},
        ]
    )
    pipeline = [{"$group": {"_id": "$group", "count": {"$sum": 1}}}]

    assert collection.aggregate(copy.deepcopy(pipeline)).to_list() == [
        {"_id": "b", "count": 2},
        {"_id": "a", "count": 1},
    ]
    assert collection.aggregate(copy.deepcopy(pipeline)).to_list() == [
        {"_id": "b", "count": 2},
        {"_id": "a", "count": 1},
    ]


def test_aggregate_cursor_is_a_context_manager():
    collection = _collection("aggregation-context-manager")
    collection.insert_one({"_id": 1})

    with collection.aggregate([]) as cursor:
        assert cursor.to_list() == [{"_id": 1}]
    assert cursor.alive is False


def test_sum_ignores_every_supported_non_numeric_value():
    collection = _collection("aggregation-nonnumeric-sum")
    collection.insert_many(
        [
            {"_id": 1, "value": True},
            {"_id": 2, "value": "2"},
            {"_id": 3, "value": None},
            {"_id": 4, "value": [2]},
            {"_id": 5},
        ]
    )

    assert collection.aggregate(
        [{"$group": {"_id": None, "total": {"$sum": "$value"}}}]
    ).to_list() == [{"_id": None, "total": 0}]


def test_add_to_set_rejects_values_without_a_bson_identity():
    with pytest.raises(TinyMongoNotSupportedError, match=r"\$addToSet.*object"):
        AggregationEngine().run(
            [{"_id": 1}],
            [{"$group": {"_id": None, "values": {"$addToSet": object()}}}],
        )


def test_async_aggregate_returns_async_cursor():
    async def scenario():
        client = AsyncTinyMongoClient("memory://aggregation-async", backend="memory")
        collection = client.db.items
        await collection.insert_many(
            [
                {"_id": 1, "group": "a", "value": 2, "items": [1, 2]},
                {"_id": 2, "group": "a", "value": 3, "items": None},
            ]
        )
        cursor = await collection.aggregate(
            [{"$group": {"_id": "$group", "total": {"$sum": "$value"}}}]
        )
        assert isinstance(cursor, AsyncTinyMongoCursor)
        assert cursor.alive is True
        assert await cursor.next() == {"_id": "a", "total": 5}
        await cursor.rewind()
        assert [row async for row in cursor] == [{"_id": "a", "total": 5}]
        await cursor.close()
        assert cursor.alive is False
        projected = await collection.aggregate(
            [
                {
                    "$project": {
                        "_id": 0,
                        "group": 1,
                        "count": {"$size": {"$ifNull": ["$items", []]}},
                    }
                }
            ]
        )
        assert await projected.to_list() == [
            {"group": "a", "count": 2},
            {"group": "a", "count": 0},
        ]
        empty = await collection.aggregate([{"$match": {"_id": "missing"}}])
        assert empty.alive is False
        async with await collection.aggregate([]) as managed:
            assert len(await managed.to_list()) == 2
        assert managed.alive is False
        with pytest.raises(TinyMongoNotSupportedError, match=r"\$lookup"):
            await collection.aggregate([{"$lookup": {}}])
        await client.close()

    import asyncio

    asyncio.run(scenario())
