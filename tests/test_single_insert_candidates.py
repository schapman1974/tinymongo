"""Single inserts must not decode unrelated SQLite or DuckDB documents."""

import asyncio
from datetime import datetime

import pytest

import tinymongo as tm
import tinymongo.table_backends as backends
from tinymongo.errors import BulkWriteError, DuplicateKeyError, OperationFailure


@pytest.fixture(params=["sqlite", "duckdb"])
def collection(request, tmp_path):
    if request.param == "duckdb":
        pytest.importorskip("duckdb")
    with tm.TinyMongoClient(str(tmp_path), backend=request.param) as client:
        yield client.app.items


def _reject_scan(*args, **kwargs):
    raise AssertionError("single insert scanned the entire collection")


def test_single_insert_does_not_read_unrelated_payloads(collection, monkeypatch):
    collection.insert_many([{"_id": str(i), "body": "x" * 8192} for i in range(250)])
    backend = collection.parent.engine
    original_loads = backends._json_loads
    decoded = []

    def tracked_loads(payload):
        decoded.append(payload)
        return original_loads(payload)

    with monkeypatch.context() as patch:
        patch.setattr(backend, "find", _reject_scan)
        patch.setattr(backends, "_json_loads", tracked_loads)
        result = collection.insert_one({"_id": "new", "body": "small"})
        assert result.acknowledged
        assert result.inserted_id == "new"
        assert result.eid == 0
        assert decoded == []
        with pytest.raises(DuplicateKeyError) as caught:
            collection.insert_one({"_id": "new", "body": "replacement"})
        assert not isinstance(caught.value, BulkWriteError)
        assert len(decoded) == 1
    assert collection.find_one({"_id": "new"})["body"] == "small"


@pytest.mark.parametrize(
    "existing,incoming,duplicate",
    [
        (1, 1.0, True),
        (0, -0.0, True),
        (True, 1, False),
        (2**53 + 1, float(2**53 + 1), False),
        (None, None, True),
    ],
)
def test_single_insert_keeps_bson_id_identity(
    collection, monkeypatch, existing, incoming, duplicate
):
    collection.insert_one({"_id": existing})
    monkeypatch.setattr(collection.parent.engine, "find", _reject_scan)
    if duplicate:
        with pytest.raises(DuplicateKeyError):
            collection.insert_one({"_id": incoming})
    else:
        assert collection.insert_one({"_id": incoming}).inserted_id == incoming


@pytest.mark.parametrize(
    "physical_id,stored_id,incoming,duplicate",
    [("-0.0", -0.0, 0, True), ("1", "1", 1, False), ("1", 1, 1.0, True)],
)
def test_single_insert_checks_legacy_candidates(
    collection, monkeypatch, physical_id, stored_id, incoming, duplicate
):
    backend = collection.parent.engine
    backend.create_collection(collection.name)
    conn = backend._connect()
    try:
        conn.execute(
            'INSERT INTO "items" (_id, data) VALUES (?, ?)',
            (physical_id, backends._json_dumps({"_id": stored_id})),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(backend, "find", _reject_scan)
    if duplicate:
        with pytest.raises(DuplicateKeyError):
            collection.insert_one({"_id": incoming})
    else:
        assert collection.insert_one({"_id": incoming}).inserted_id == incoming


def test_single_insert_keeps_legacy_fallback(collection, monkeypatch):
    doc_id = datetime(2026, 9, 30)
    collection.insert_one({"_id": doc_id})
    backend = collection.parent.engine
    original_find = backend.find
    scans = []

    def find(*args, **kwargs):
        scans.append(args)
        return original_find(*args, **kwargs)

    monkeypatch.setattr(backend, "find", find)
    with pytest.raises(DuplicateKeyError):
        collection.insert_one({"_id": doc_id})
    assert len(scans) == 1


@pytest.mark.parametrize(
    "keys,options",
    [
        ("email", {"unique": True}),
        ([("email", 1), ("group", 1)], {"unique": True}),
        ("email", {"unique": True, "sparse": True}),
        ("email", {"unique": True, "partialFilterExpression": {"active": True}}),
    ],
)
def test_single_insert_preserves_secondary_uniqueness(collection, keys, options):
    collection.create_index(keys, **options)
    collection.insert_one({"_id": "seed", "email": "same", "group": 1, "active": True})
    with pytest.raises(DuplicateKeyError):
        collection.insert_one(
            {"_id": "duplicate", "email": "same", "group": 1, "active": True},
            bypass_document_validation=True,
        )
    assert collection.count_documents({}) == 1


def test_single_insert_validates_nonunique_parallel_arrays(collection, monkeypatch):
    collection.create_index([("left", 1), ("right", 1)])
    monkeypatch.setattr(collection.parent.engine, "find", _reject_scan)
    collection.insert_one({"_id": "valid", "left": [1], "right": 2})
    with pytest.raises(OperationFailure) as caught:
        collection.insert_one({"_id": "invalid", "left": [1], "right": [2]})
    assert caught.value.code == 171


def test_async_single_insert_uses_candidates(collection, tmp_path, monkeypatch):
    collection.insert_many([{"_id": str(i)} for i in range(100)])
    backend = collection.parent.engine

    async def insert():
        async with tm.AsyncTinyMongoClient(
            str(tmp_path), backend=backend.dialect
        ) as client:
            result = await client.app.items.insert_one({"_id": "async"})
            assert result.inserted_id == "async"
            with pytest.raises(DuplicateKeyError):
                await client.app.items.insert_one({"_id": "async"})

    with monkeypatch.context() as patch:
        patch.setattr(type(backend), "find", _reject_scan)
        asyncio.run(insert())
    assert collection.find_one({"_id": "async"}) == {"_id": "async"}


def test_duckdb_native_batch_failure_rolls_back(tmp_path):
    pytest.importorskip("duckdb")
    backend = backends.DuckDBTableBackend(str(tmp_path / "atomic.duckdb"))
    try:
        backend.insert_many("items", [{"_id": "exists"}])
        with pytest.raises(DuplicateKeyError):
            backend.insert_many_prevalidated(
                "items", [{"_id": "new"}, {"_id": "exists"}]
            )
        assert backend.find("items", {}) == [{"_id": "exists"}]
    finally:
        backend.close()


def test_backend_without_single_insert_hook_keeps_compatibility(
    collection, monkeypatch
):
    monkeypatch.setattr(collection.parent.engine, "insert_one_with_result", None)
    result = collection.insert_one({"_id": "fallback"})
    assert result.inserted_id == "fallback"
    assert result.eid == 0
    with pytest.raises(DuplicateKeyError):
        collection.insert_one({"_id": "fallback"})
