"""Query-validation and write-error contracts shared by every target."""

from collections import UserDict

import pytest

from tinymongo.errors import DuplicateKeyError as TinyMongoDuplicateKeyError
from tinymongo.errors import OperationFailure as TinyMongoOperationFailure
from tinymongo.errors import TinyMongoNotSupportedError
from tinymongo.errors import WriteError as TinyMongoWriteError


pytestmark = pytest.mark.contract

_DUPLICATE_KEY_ERRORS = (TinyMongoDuplicateKeyError,)
_OPERATION_ERRORS = (TinyMongoOperationFailure,)
_WRITE_ERRORS = (TinyMongoWriteError,)
try:
    from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError
    from pymongo.errors import OperationFailure as PyMongoOperationFailure
    from pymongo.errors import WriteError as PyMongoWriteError
except ImportError:  # pragma: no cover - optional dependency guard
    pass
else:
    _DUPLICATE_KEY_ERRORS += (PyMongoDuplicateKeyError,)
    _OPERATION_ERRORS += (PyMongoOperationFailure,)
    _WRITE_ERRORS += (PyMongoWriteError,)

_QUERY_REJECTION_ERRORS = _OPERATION_ERRORS + (TinyMongoNotSupportedError,)


def _ids(rows):
    return sorted(document["_id"] for document in rows)


def _run_filter_operation(collection, operation, query):
    """Exercise every PyMongo-style CRUD entry point that accepts a filter."""

    if operation == "find":
        return list(collection.find(query))
    if operation == "find_one":
        return collection.find_one(query)
    if operation == "count_documents":
        return collection.count_documents(query)
    if operation == "distinct":
        return collection.distinct("value", query)
    if operation == "update_one":
        return collection.update_one(query, {"$set": {"touched": True}})
    if operation == "update_many":
        return collection.update_many(query, {"$set": {"touched": True}})
    if operation == "replace_one":
        return collection.replace_one(query, {"value": "replacement"})
    if operation == "delete_one":
        return collection.delete_one(query)
    if operation == "delete_many":
        return collection.delete_many(query)
    if operation == "find_one_and_update":
        return collection.find_one_and_update(
            query,
            {"$set": {"touched": True}},
        )
    if operation == "find_one_and_replace":
        return collection.find_one_and_replace(
            query,
            {"value": "replacement"},
        )
    if operation == "find_one_and_delete":
        return collection.find_one_and_delete(query)
    raise AssertionError("Unknown test operation: {0}".format(operation))


def test_size_matches_arrays_with_the_exact_requested_length(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "empty", "values": []},
            {"_id": "one", "values": [1]},
            {"_id": "two", "values": [1, 2]},
            {"_id": "nested-two", "values": [[1], [2, 3]]},
            {"_id": "scalar", "values": "not-an-array"},
            {"_id": "missing"},
        ]
    )

    assert _ids(collection.find({"values": {"$size": 0}})) == ["empty"]
    assert _ids(collection.find({"values": {"$size": 2}})) == [
        "nested-two",
        "two",
    ]


def test_elem_match_requires_one_array_member_to_satisfy_every_condition(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {
                "_id": "same-document",
                "results": [
                    {"kind": "quiz", "score": 8},
                    {"kind": "exam", "score": 10},
                ],
                "values": [2, 7, 12],
            },
            {
                "_id": "split-documents",
                "results": [
                    {"kind": "quiz", "score": 3},
                    {"kind": "exam", "score": 8},
                ],
                "values": [1, 3, 12],
            },
            {
                "_id": "scalar-field",
                "results": {"kind": "quiz", "score": 8},
                "values": 7,
            },
        ]
    )

    assert _ids(
        collection.find(
            {
                "results": {
                    "$elemMatch": {
                        "kind": "quiz",
                        "score": {"$gte": 8},
                    }
                }
            }
        )
    ) == ["same-document"]
    assert _ids(
        collection.find({"values": {"$elemMatch": {"$gte": 5, "$lt": 10}}})
    ) == ["same-document"]


def test_elem_match_document_paths_traverse_nested_arrays(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "nested-array", "values": [[1, 2], [3]]},
            {
                "_id": "nested-document",
                "values": [{"a": [{"b": 1}, {"b": 2}]}],
            },
            {"_id": "raw-nested-document", "values": [[{"x": 2}]]},
        ]
    )

    assert (
        collection.find_one(
            {
                "_id": "nested-array",
                "values": {"$elemMatch": {"0": 1}},
            }
        )["_id"]
        == "nested-array"
    )
    assert (
        collection.find_one(
            {
                "_id": "nested-array",
                "values": {"$elemMatch": {"missing": {"$exists": False}}},
            }
        )["_id"]
        == "nested-array"
    )
    assert (
        collection.find_one(
            {
                "_id": "nested-document",
                "values": {"$elemMatch": {"a.b": {"$type": "int"}}},
            }
        )["_id"]
        == "nested-document"
    )
    assert (
        _ids(
            collection.find(
                {
                    "_id": "raw-nested-document",
                    "values": {"$elemMatch": {"x": 2}},
                }
            )
        )
        == []
    )
    assert (
        _ids(
            collection.find(
                {
                    "_id": "raw-nested-document",
                    "values": {"$all": [{"$elemMatch": {"x": 2}}]},
                }
            )
        )
        == []
    )
    assert (
        collection.find_one(
            {
                "_id": "nested-document",
                "values": {"$elemMatch": {"a.0.b": 1}},
            }
        )["_id"]
        == "nested-document"
    )

    for condition in (
        1,
        {"$type": "int"},
        {"$mod": [2, 1]},
        {"$gt": 0},
        {"$all": [1, 2]},
    ):
        assert (
            collection.find_one({"_id": "nested-array", "values.0": condition}) is None
        )
    for condition in (
        [1, 2],
        {"$size": 2},
        {"$type": "array"},
        {"$elemMatch": {"$gt": 1}},
    ):
        assert (
            collection.find_one({"_id": "nested-array", "values.0": condition})["_id"]
            == "nested-array"
        )


def test_elem_match_null_conditions_match_missing_document_fields(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "raw-array", "values": [[1, 2]]},
            {"_id": "missing-field", "values": [{"other": 1}]},
            {"_id": "explicit-null", "values": [{"value": None}]},
        ]
    )

    for condition in (
        None,
        {"$eq": None},
        {"$in": [None]},
        {"$all": [None]},
    ):
        assert _ids(
            collection.find({"values": {"$elemMatch": {"value": condition}}})
        ) == ["explicit-null", "missing-field", "raw-array"]


def test_dotted_query_paths_traverse_document_arrays_not_raw_nested_arrays(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "documents", "items": [{"score": 1}, {"score": 5}]},
            {"_id": "partly-missing", "items": [{"other": 1}, {"score": 2}]},
            {"_id": "raw-array", "items": [[{"score": 1}]]},
        ]
    )

    assert _ids(collection.find({"items.score": 1})) == ["documents"]
    assert _ids(collection.find({"items.score": {"$type": "int"}})) == [
        "documents",
        "partly-missing",
    ]
    assert _ids(collection.find({"items.score": {"$exists": False}})) == ["raw-array"]
    assert _ids(collection.find({"items.score": {"$ne": None}})) == [
        "documents",
        "raw-array",
    ]
    assert _ids(collection.find({"items.score": {"$nin": [None]}})) == [
        "documents",
        "raw-array",
    ]

    collection.create_index("items.score")
    assert _ids(collection.find({"items.score": 1})) == ["documents"]


def test_null_equality_keeps_missing_fields_visible_after_indexing(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "missing"},
            {"_id": "null", "value": None},
            {"_id": "other", "value": 1},
        ]
    )

    assert _ids(collection.find({"value": None})) == ["missing", "null"]
    collection.create_index("value")
    assert _ids(collection.find({"value": None})) == ["missing", "null"]


def test_tuple_equality_keeps_array_matches_visible_after_indexing(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "exact-array", "value": [1, 2]},
            {"_id": "array-member", "value": [[1, 2]]},
            {"_id": "other", "value": [2, 3]},
        ]
    )

    assert _ids(collection.find({"value": (1, 2)})) == [
        "array-member",
        "exact-array",
    ]
    collection.create_index("value")
    assert _ids(collection.find({"value": (1, 2)})) == [
        "array-member",
        "exact-array",
    ]


def test_continued_numeric_paths_ignore_scalar_index_dead_ends(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "scalar-index", "items": [1, 2]},
            {"_id": "numeric-field", "items": [1, {"0": {"score": 3}}]},
        ]
    )

    assert _ids(collection.find({"items.0.score": {"$ne": None}})) == [
        "numeric-field",
        "scalar-index",
    ]
    assert _ids(collection.find({"items.0.score": {"$nin": [None]}})) == [
        "numeric-field",
        "scalar-index",
    ]
    assert _ids(collection.find({"items.0.score": 3})) == ["numeric-field"]


def test_numeric_query_paths_consider_array_indexes_and_document_field_names(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "array-index", "items": ["index-zero"]},
            {"_id": "first-field", "items": [{"0": "field-zero"}]},
            {
                "_id": "later-field",
                "items": [{"other": 1}, {"0": "later-zero"}],
            },
            {"_id": "indexed-array-only", "items": [[1, 2]]},
            {"_id": "mixed", "items": [[1, 2], {"0": 1}]},
        ]
    )

    assert _ids(collection.find({"items.0": "index-zero"})) == ["array-index"]
    assert _ids(collection.find({"items.0": "field-zero"})) == ["first-field"]
    assert _ids(collection.find({"items.0": "later-zero"})) == ["later-field"]
    assert _ids(collection.find({"items.0": 1})) == ["mixed"]


def test_mapping_filters_work_for_new_operators_and_dotted_paths(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "match", "values": [1, 2], "items": [{"score": 1}]},
            {"_id": "other", "values": [1], "items": [{"score": 2}]},
        ]
    )

    assert _ids(collection.find(UserDict({"values": UserDict({"$size": 2})}))) == [
        "match"
    ]
    assert _ids(collection.find(UserDict({"items.score": 1}))) == ["match"]
    assert _ids(
        collection.find(
            UserDict({"items": UserDict({"$elemMatch": UserDict({"score": 1})})})
        )
    ) == ["match"]


def test_type_supports_aliases_codes_lists_and_array_element_matching(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "string", "value": "hello"},
            {"_id": "int", "value": 7},
            {"_id": "long", "value": 2**40},
            {"_id": "double", "value": 7.5},
            {"_id": "bool", "value": True},
            {"_id": "array", "value": ["hello", 7]},
            {"_id": "object", "value": {"nested": 1}},
            {"_id": "null", "value": None},
        ]
    )

    assert _ids(collection.find({"value": {"$type": "string"}})) == [
        "array",
        "string",
    ]
    assert _ids(collection.find({"value": {"$type": 2}})) == [
        "array",
        "string",
    ]
    assert _ids(collection.find({"value": {"$type": "int"}})) == [
        "array",
        "int",
    ]
    assert _ids(collection.find({"value": {"$type": "array"}})) == ["array"]
    assert _ids(collection.find({"value": {"$type": ["long", "double"]}})) == [
        "double",
        "long",
    ]
    assert _ids(collection.find({"value": {"$type": "number"}})) == [
        "array",
        "double",
        "int",
        "long",
    ]


def test_mod_uses_mongodb_integer_truncation_and_array_member_rules(
    contract_target,
):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": "ten", "value": 10},
            {"_id": "eleven", "value": 11},
            {"_id": "fraction", "value": 10.9},
            {"_id": "negative", "value": -10},
            {"_id": "array", "value": [1, 14]},
            {"_id": "string", "value": "10"},
        ]
    )

    expected = ["array", "fraction", "ten"]
    assert _ids(collection.find({"value": {"$mod": [4, 2]}})) == expected
    assert _ids(collection.find({"value": {"$mod": [4.9, 2.9]}})) == expected
    assert _ids(collection.find({"value": {"$mod": [4, -2]}})) == ["negative"]


def test_recognized_query_operators_reject_malformed_operands(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": "original", "value": [1, 2]})
    malformed_queries = [
        {"value": {"$size": "2"}},
        {"value": {"$elemMatch": 1}},
        {"value": {"$type": "not-a-real-bson-type"}},
        {"value": {"$mod": [4]}},
    ]

    for query in malformed_queries:
        with pytest.raises(_OPERATION_ERRORS):
            list(collection.find(query))


def test_find_rejects_top_level_and_field_operator_typos(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": "original", "value": 1})

    for query in ({"$madeUp": 1}, {"value": {"$madeUp": 1}}):
        with pytest.raises(_QUERY_REJECTION_ERRORS):
            list(collection.find(query))


def test_every_filtering_crud_entrypoint_rejects_operator_typos(contract_target):
    collection = contract_target.collection
    original = {"_id": "original", "value": 1}
    collection.insert_one(original)
    operations = (
        "find",
        "find_one",
        "count_documents",
        "distinct",
        "update_one",
        "update_many",
        "replace_one",
        "delete_one",
        "delete_many",
        "find_one_and_update",
        "find_one_and_replace",
        "find_one_and_delete",
    )
    typo = {"value": {"$madeUp": 1}}

    for operation in operations:
        with pytest.raises(_QUERY_REJECTION_ERRORS):
            _run_filter_operation(collection, operation, typo)
        assert collection.find_one({"_id": "original"}) == original


def test_distinct_rejects_falsey_non_document_filters(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": "original", "value": 1})

    for malformed_filter in ([], (), 0, False, ""):
        with pytest.raises(_OPERATION_ERRORS) as caught:
            collection.distinct("value", malformed_filter)
        assert caught.value.code == 14


def test_duplicate_key_errors_report_code_11000(contract_target):
    collection = contract_target.collection
    collection.insert_one({"_id": "first", "email": "ada@example.test"})

    with pytest.raises(_DUPLICATE_KEY_ERRORS) as duplicate_id:
        collection.insert_one({"_id": "first", "email": "grace@example.test"})
    assert duplicate_id.value.code == 11000

    collection.create_index("email", unique=True)
    with pytest.raises(_DUPLICATE_KEY_ERRORS) as duplicate_index:
        collection.insert_one({"_id": "second", "email": "ada@example.test"})
    assert duplicate_index.value.code == 11000


def test_add_to_set_non_array_errors_report_code_2_and_leave_document_atomic(
    contract_target,
):
    collection = contract_target.collection
    originals = [
        {"_id": "scalar", "values": "not-an-array", "status": "original"},
        {"_id": "null", "values": None, "status": "original"},
    ]
    collection.insert_many(originals)

    for method in (collection.update_one, collection.update_many):
        for original in originals:
            with pytest.raises(_WRITE_ERRORS) as caught:
                method(
                    {"_id": original["_id"]},
                    {
                        "$set": {"status": "changed"},
                        "$addToSet": {"values": "new"},
                    },
                )
            assert caught.value.code == 2
            assert collection.find_one({"_id": original["_id"]}) == original
