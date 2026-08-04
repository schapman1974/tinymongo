"""Focused tests for the linear-time shared insert-many planner."""

import importlib
from types import SimpleNamespace

import tinymongo as tm
from tinymongo.indexes import IndexSpec


core = importlib.import_module("tinymongo.tinymongo")


def _collection():
    return SimpleNamespace(full_name="app.items")


def test_bulk_insert_planning_scales_with_documents_and_unique_indexes(
    monkeypatch,
):
    existing = [
        {"_id": index, "email": "existing-{0}@example.com".format(index)}
        for index in range(100)
    ]
    documents = [
        {"_id": index, "email": "new-{0}@example.com".format(index)}
        for index in range(100, 2100)
    ]
    token_calls = 0
    original_index_tokens = core.index_tokens

    def counting_index_tokens(document, field):
        nonlocal token_calls
        token_calls += 1
        return original_index_tokens(document, field)

    def unexpected_pairwise_comparison(_left, _right):
        raise AssertionError("supported BSON IDs should use typed identity keys")

    monkeypatch.setattr(core, "index_tokens", counting_index_tokens)
    monkeypatch.setattr(core, "bson_values_equal", unexpected_pairwise_comparison)

    accepted, errors = core._plan_insert_many(
        _collection(),
        documents,
        existing,
        [IndexSpec("ignored"), IndexSpec("email", unique=True)],
        ordered=False,
    )

    assert accepted == documents
    assert errors == []
    assert token_calls == len(existing) + len(documents)


def test_bulk_insert_planning_preserves_bson_identity_and_unique_error_order():
    existing = [{"_id": {"number": 1}, "email": "taken@example.com"}]
    documents = [
        {"_id": {"number": 1.0}, "email": "different@example.com"},
        {"_id": "first", "email": "first@example.com"},
        {"_id": "unique-conflict", "email": "taken@example.com"},
        {"_id": "batch-conflict", "email": "first@example.com"},
        {"_id": "last", "email": "last@example.com"},
    ]

    accepted, errors = core._plan_insert_many(
        _collection(),
        documents,
        existing,
        [IndexSpec("email", unique=True)],
        ordered=False,
    )

    assert [document["_id"] for document in accepted] == ["first", "last"]
    assert [error["index"] for error in errors] == [0, 2, 3]
    assert errors[0]["keyPattern"] == {"_id": 1}
    assert errors[1]["keyPattern"] == {"email": 1}
    assert errors[2]["keyValue"] == {"email": "first@example.com"}


def test_bulk_insert_planning_retains_fallback_and_legacy_conflict_behavior():
    unsupported_id = object()
    accepted, errors = core._plan_insert_many(
        _collection(),
        [{"_id": unsupported_id}, {"_id": unsupported_id}],
        [],
        [],
        ordered=False,
    )

    assert accepted == [{"_id": unsupported_id}]
    assert errors[0]["index"] == 1

    # A typed candidate still checks the rare untyped fallback bucket.  This
    # is unreachable through public storage validation, but keeps the internal
    # planner's compatibility fallback exact.
    class TypedAlias:
        def __eq__(self, other):
            return other == "typed"

    accepted, errors = core._plan_insert_many(
        _collection(),
        [{"_id": "other"}, {"_id": "typed"}],
        [{"_id": TypedAlias()}],
        [],
        ordered=False,
    )
    assert accepted == [{"_id": "other"}]
    assert errors[0]["index"] == 1

    existing = [
        {"_id": "one", "email": "duplicate@example.com"},
        {"_id": "two", "email": "duplicate@example.com"},
    ]
    accepted, errors = core._plan_insert_many(
        _collection(),
        [{"_id": "three", "email": "otherwise-new@example.com"}],
        existing,
        [IndexSpec("email", unique=True)],
        ordered=True,
    )

    assert accepted == []
    assert errors[0]["keyPattern"] == {"email": 1}
    assert "documents 'one' and 'two'" in errors[0]["errmsg"]


def test_engine_insert_many_uses_prevalidated_backend_hook():
    class Engine:
        def __init__(self):
            self.received = None

        def find(self, _collection, _filter):
            return []

        def get_index_specs(self, _collection):
            return []

        def insert_many(self, *_args, **_kwargs):
            raise AssertionError("the duplicate backend preflight was used")

        def insert_many_prevalidated(self, collection, documents, **kwargs):
            self.received = (collection, documents, kwargs)
            return list(range(len(documents)))

    engine = Engine()
    collection = SimpleNamespace(
        full_name="app.items",
        tablename="items",
        parent=SimpleNamespace(engine=engine),
    )
    documents = [{"_id": 1}, {"_id": 2}]

    results, accepted, errors = core._execute_engine_insert_many(
        collection,
        documents,
        ordered=True,
        bypass_document_validation=True,
    )

    assert results == [0, 1]
    assert accepted == documents
    assert errors == []
    assert engine.received == (
        "items",
        documents,
        {"bypass_document_validation": True},
    )


def test_sqlite_public_bulk_insert_scans_existing_documents_once(
    tmp_path,
    monkeypatch,
):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.items
    backend = collection.parent.engine
    original_find = backend.find
    calls = []

    def tracked_find(*args, **kwargs):
        calls.append((args, kwargs))
        return original_find(*args, **kwargs)

    monkeypatch.setattr(backend, "find", tracked_find)
    try:
        result = collection.insert_many(
            [{"_id": index, "value": index} for index in range(100)]
        )
        assert len(result.inserted_ids) == 100
        assert len(calls) == 1
    finally:
        client.close()
