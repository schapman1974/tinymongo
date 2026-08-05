"""Focused tests for shared and backend-targeted insert-many planning."""

from datetime import datetime, timezone
import importlib
from types import SimpleNamespace

import pytest

import tinymongo as tm
import tinymongo.table_backends as table_backends
from tinymongo.errors import BulkWriteError
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


def test_engine_insert_many_uses_optional_conflict_candidate_hook():
    class Engine:
        def __init__(self):
            self.candidate_calls = []

        def find(self, *_args, **_kwargs):
            raise AssertionError("the conflict-candidate hook was ignored")

        def get_index_specs(self, _collection):
            return []

        def find_insert_conflict_candidates(self, collection, documents, specs):
            self.candidate_calls.append((collection, documents, specs))
            return [{"_id": "existing"}]

        def insert_many(self, _collection, documents, **_kwargs):
            return list(range(len(documents)))

    engine = Engine()
    collection = SimpleNamespace(
        full_name="app.items",
        tablename="items",
        parent=SimpleNamespace(engine=engine),
    )
    documents = [{"_id": "existing"}, {"_id": "new"}]

    results, accepted, errors = core._execute_engine_insert_many(
        collection,
        documents,
        ordered=False,
        bypass_document_validation=False,
    )

    assert results == [0]
    assert accepted == [{"_id": "new"}]
    assert [error["index"] for error in errors] == [0]
    assert engine.candidate_calls == [("items", documents, [])]


def test_sqlite_public_bulk_insert_queries_only_incoming_id_candidates(
    tmp_path,
    monkeypatch,
):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.items
    backend = collection.parent.engine
    collection.insert_one({"_id": "late-conflict"})
    original_connect = backend._connect
    statements = []

    def tracked_connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    def unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("ordinary SQLite batches must not scan the collection")

    monkeypatch.setattr(backend, "_connect", tracked_connect)
    monkeypatch.setattr(backend, "find", unexpected_full_scan)
    try:
        documents = [
            {"_id": "new-{0}".format(index), "value": index} for index in range(1000)
        ] + [{"_id": "late-conflict"}]
        with pytest.raises(BulkWriteError) as caught:
            collection.insert_many(documents, ordered=False)
        assert caught.value.details["nInserted"] == 1000
        assert caught.value.details["writeErrors"][0]["index"] == 1000
        candidate_reads = [
            statement
            for statement in statements
            if statement.startswith('SELECT data FROM "items"')
        ]
        assert len(candidate_reads) >= 2
        assert all(" WHERE _id IN (" in statement for statement in candidate_reads)
    finally:
        client.close()


def test_sqlite_targeted_preflight_decodes_only_matching_bson_ids(
    tmp_path,
    monkeypatch,
):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.items
    collection.insert_one({"_id": 1, "value": "existing"})
    backend = collection.parent.engine
    original_loads = table_backends._json_loads
    decoded = []

    def tracked_loads(value):
        decoded.append(value)
        return original_loads(value)

    def unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("the BSON identity conflict should use a PK probe")

    monkeypatch.setattr(table_backends, "_json_loads", tracked_loads)
    monkeypatch.setattr(backend, "find", unexpected_full_scan)
    documents = [
        {"_id": 1.0, "value": "numeric duplicate"},
        {"_id": True, "value": "boolean remains distinct"},
        {"_id": 2, "value": "new"},
    ]
    try:
        with pytest.raises(BulkWriteError) as caught:
            collection.insert_many(documents, ordered=False)

        assert caught.value.details["nInserted"] == 2
        assert [error["index"] for error in caught.value.details["writeErrors"]] == [0]
        assert len(decoded) == 1
    finally:
        client.close()


def test_sqlite_unique_indexes_keep_conservative_insert_preflight(
    tmp_path,
    monkeypatch,
):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.users
    collection.create_index("email", unique=True)
    collection.insert_one({"_id": "seed", "email": "taken@example.com"})
    backend = collection.parent.engine
    original_find = backend.find
    calls = []

    def tracked_find(*args, **kwargs):
        calls.append((args, kwargs))
        return original_find(*args, **kwargs)

    monkeypatch.setattr(backend, "find", tracked_find)
    try:
        with pytest.raises(BulkWriteError):
            collection.insert_many([{"_id": "duplicate", "email": "taken@example.com"}])
        assert len(calls) == 1
    finally:
        client.close()


def test_sqlite_nonunique_indexes_keep_targeted_insert_preflight(
    tmp_path,
    monkeypatch,
):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.users
    collection.create_index("email")
    collection.insert_one({"_id": "seed", "email": "seed@example.com"})
    backend = collection.parent.engine

    def unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("nonunique indexes cannot reject an insert")

    monkeypatch.setattr(backend, "find", unexpected_full_scan)
    try:
        result = collection.insert_many([{"_id": "new", "email": "seed@example.com"}])
        assert result.inserted_ids == ["new"]
    finally:
        client.close()


@pytest.mark.parametrize(
    ("existing_id", "incoming_id"),
    [
        (0, -0.0),
        (2**60, float(2**60)),
        (int(1e23), 1e23),
    ],
)
def test_sqlite_targeted_preflight_preserves_exact_numeric_id_duplicates(
    tmp_path,
    monkeypatch,
    existing_id,
    incoming_id,
):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.numbers
    collection.insert_one({"_id": existing_id})
    backend = collection.parent.engine

    def unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("current numeric IDs should use their typed PK")

    monkeypatch.setattr(backend, "find", unexpected_full_scan)
    try:
        with pytest.raises(BulkWriteError) as caught:
            collection.insert_many([{"_id": incoming_id}])
        assert caught.value.details["writeErrors"][0]["index"] == 0
    finally:
        client.close()


@pytest.mark.parametrize(
    ("existing_id", "incoming_id"),
    [
        (True, 1),
        (2**53 + 1, float(2**53 + 1)),
        (10**23, 1e23),
    ],
)
def test_sqlite_targeted_preflight_keeps_distinct_numeric_ids_separate(
    tmp_path,
    monkeypatch,
    existing_id,
    incoming_id,
):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.numbers
    collection.insert_one({"_id": existing_id})
    backend = collection.parent.engine

    def unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("current numeric IDs should use their typed PK")

    monkeypatch.setattr(backend, "find", unexpected_full_scan)
    try:
        result = collection.insert_many([{"_id": incoming_id}])
        assert result.inserted_ids == [incoming_id]
    finally:
        client.close()


def _insert_legacy_sqlite_row(backend, collection, row_id, document):
    backend.create_collection(collection)
    conn = backend._connect()
    try:
        conn.execute(
            'INSERT INTO "{0}" (_id, data) VALUES (?, ?)'.format(collection),
            (row_id, table_backends._json_dumps(document)),
        )
        conn.commit()
    finally:
        conn.close()


def test_sqlite_targeted_preflight_finds_legacy_negative_zero(tmp_path, monkeypatch):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.legacy_zero
    backend = collection.parent.engine
    _insert_legacy_sqlite_row(
        backend,
        collection.tablename,
        "-0.0",
        {"_id": -0.0},
    )

    def unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("enumerable legacy zero aliases should use a PK probe")

    monkeypatch.setattr(backend, "find", unexpected_full_scan)
    try:
        with pytest.raises(BulkWriteError):
            collection.insert_many([{"_id": 0.0}])
    finally:
        client.close()


def test_sqlite_targeted_preflight_filters_legacy_key_false_positives(
    tmp_path,
    monkeypatch,
):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.legacy_string
    backend = collection.parent.engine
    _insert_legacy_sqlite_row(
        backend,
        collection.tablename,
        "1",
        {"_id": "1"},
    )

    def unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("the enumerable legacy key should use a PK probe")

    monkeypatch.setattr(backend, "find", unexpected_full_scan)
    try:
        result = collection.insert_many([{"_id": 1}])
        assert result.inserted_ids == [1]
    finally:
        client.close()


def test_sqlite_decimal_id_uses_safe_legacy_scan_fallback(tmp_path, monkeypatch):
    Decimal128 = pytest.importorskip("bson").Decimal128
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.legacy_decimal
    backend = collection.parent.engine
    _insert_legacy_sqlite_row(
        backend,
        collection.tablename,
        "1",
        {"_id": 1},
    )
    original_find = backend.find
    calls = []

    def tracked_find(*args, **kwargs):
        calls.append((args, kwargs))
        return original_find(*args, **kwargs)

    monkeypatch.setattr(backend, "find", tracked_find)
    try:
        with pytest.raises(BulkWriteError):
            collection.insert_many([{"_id": Decimal128("1.00")}])
        assert len(calls) == 1
    finally:
        client.close()


def test_sqlite_current_decimal_id_uses_typed_key_for_numeric_alias(
    tmp_path,
    monkeypatch,
):
    Decimal128 = pytest.importorskip("bson").Decimal128
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.current_decimal
    collection.insert_one({"_id": Decimal128("1.00")})
    backend = collection.parent.engine

    def unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("current Decimal128 rows share the typed numeric key")

    monkeypatch.setattr(backend, "find", unexpected_full_scan)
    try:
        with pytest.raises(BulkWriteError):
            collection.insert_many([{"_id": 1}])
    finally:
        client.close()


@pytest.mark.parametrize(
    ("existing_id", "incoming_id"),
    [
        (
            datetime(2026, 8, 4, 12, 30),
            datetime(2026, 8, 4, 12, 30, tzinfo=timezone.utc),
        ),
        (
            {"first": 1, "second": 2},
            {"first": 1, "second": 2},
        ),
    ],
)
def test_sqlite_non_enumerable_ids_use_legacy_scan_fallback(
    tmp_path,
    monkeypatch,
    existing_id,
    incoming_id,
):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.legacy_fallback
    collection.insert_one({"_id": existing_id})
    backend = collection.parent.engine
    original_find = backend.find
    calls = []

    def tracked_find(*args, **kwargs):
        calls.append((args, kwargs))
        return original_find(*args, **kwargs)

    monkeypatch.setattr(backend, "find", tracked_find)
    try:
        with pytest.raises(BulkWriteError):
            collection.insert_many([{"_id": incoming_id}])
        assert len(calls) == 1
    finally:
        client.close()
