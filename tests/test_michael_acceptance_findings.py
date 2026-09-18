"""Regression tests for Michael Kennedy's rounds 19 and 20 (TM-046–049)."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tinymongo import TinyMongoClient, bson_codec, storage_backends
from tinymongo.errors import DuplicateKeyError, InvalidDocument
from tinymongo.storage_backends import AtomicJSONStorage
from tinymongo.table_backends import PostgresTableBackend


@pytest.mark.parametrize("size", [100, 1000])
def test_merge_uses_linear_identity_work_for_untouched_neighbours(monkeypatch, size):
    storage = object.__new__(AtomicJSONStorage)
    existing = {"archive": {str(i): {"_id": i} for i in range(size)}}
    incoming = deepcopy(existing)
    incoming["settings"] = {"1": {"_id": "new"}}
    identity = Mock(wraps=storage_backends.bson_value_identity_key)
    equality = Mock(wraps=storage_backends.bson_values_equal)
    monkeypatch.setattr(storage_backends, "bson_value_identity_key", identity)
    monkeypatch.setattr(storage_backends, "bson_values_equal", equality)
    result = storage._merge_data(existing, incoming)
    assert result["archive"] == existing["archive"]
    assert list(result["settings"].values()) == [{"_id": "new"}]
    assert identity.call_count <= 2 * size + 2
    assert equality.call_count == 0
    assert "settings" not in existing


def test_merge_preserves_bson_id_identity_and_first_owner():
    storage = object.__new__(AtomicJSONStorage)
    existing = {
        "docs": {
            "1": {"_id": 1, "v": "old"},
            "2": {"_id": True},
            "3": {"_id": {"a": 1, "b": 2}},
            "4": {"_id": None},
            "5": {"_id": 1.0, "v": "duplicate legacy owner"},
        }
    }
    incoming = {
        "docs": {
            "1": {"_id": 1.0, "v": "new"},
            "2": {"_id": {"a": 1.0, "b": 2}, "v": "new"},
            "3": {"_id": {"b": 2, "a": 1}, "v": "reordered"},
            "4": {"_id": None, "v": "new"},
            "5": {"v": "missing id"},
        }
    }
    result = storage._merge_data(existing, incoming)["docs"]
    assert result["1"]["v"] == "new"
    assert result["2"] == {"_id": True}
    assert result["3"]["v"] == "new"
    assert result["4"]["v"] == "new"
    assert result["5"]["v"] == "duplicate legacy owner"
    assert len(result) == 7


@pytest.mark.parametrize(
    "backend", ["memory", "json", "sqlite", "sqlite-sharded", "duckdb", "parquet"]
)
@pytest.mark.parametrize(
    "bad",
    [{"bad\x00key": 1}, {"nested": {"bad\x00key": 1}}, {"array": [{"bad\x00key": 1}]}],
)
def test_nul_keys_rejected_before_any_write(tmp_path, backend, bad):
    with TinyMongoClient(str(tmp_path / backend), backend=backend) as client:
        col = client.app.docs
        col.insert_one({"_id": 1, "value": "original"})
        with pytest.raises(InvalidDocument, match="field names cannot contain NUL"):
            col.insert_one(dict(bad, _id=2))
        with pytest.raises(InvalidDocument):
            col.insert_many([{"_id": 3}, dict(bad, _id=4)])
        with pytest.raises(InvalidDocument):
            col.update_one({"_id": 1}, {"$set": bad})
        with pytest.raises(InvalidDocument):
            col.replace_one({"_id": 1}, dict(bad, _id=1))
        assert list(col.find({})) == [{"_id": 1, "value": "original"}]
        col.insert_one({"_id": 5, "value": "legal\x00value"})
        assert col.find_one({"_id": 5})["value"] == "legal\x00value"


def test_nul_validation_preserves_document_and_path():
    doc = {"_id": 7, "nested": [{"bad\x00key": 1}]}
    with pytest.raises(InvalidDocument) as caught:
        bson_codec.dumps(doc, document_context="collection app.docs")
    assert caught.value.document is doc
    assert "collection app.docs" in str(caught.value)
    assert "nested" in str(caught.value)
    assert "[0]" in str(caught.value)


@pytest.mark.parametrize("method", ["_execute", "_executemany"])
def test_postgres_encoding_error_translated_and_cursor_closed(method):
    backend = object.__new__(PostgresTableBackend)
    error = RuntimeError("private document content must not be echoed")
    error.sqlstate = "22P05"
    cursor = Mock()
    cursor.execute.side_effect = error
    cursor.executemany.side_effect = error
    conn = SimpleNamespace(cursor=lambda: cursor)
    with pytest.raises(InvalidDocument, match="PostgreSQL JSONB") as caught:
        getattr(backend, method)(conn, "INSERT", [])
    assert caught.value.__cause__ is error
    assert "private document content" not in str(caught.value)
    cursor.close.assert_called_once()


@pytest.mark.parametrize("shards", [4, 12])
@pytest.mark.parametrize(
    "update",
    [
        {"$set": {"note": "changed"}},
        {"$set": {"key": 0}},
        {"$set": {"tags": ["b", "a"]}},
    ],
)
def test_sharded_unique_unchanged_tokens_avoid_collection_scan(
    tmp_path, monkeypatch, shards, update
):
    with TinyMongoClient(
        str(tmp_path), backend="sqlite-sharded", sqlite_shards=shards
    ) as client:
        col = client.app.docs
        col.create_index("key", unique=True)
        col.create_index("tags", unique=True)
        col.insert_many(
            [
                {"_id": i, "key": i, "tags": ["a", "b"] if i == 0 else [str(i)]}
                for i in range(40)
            ]
        )
        engine = col.parent.engine
        original = engine._find_existing

        def checked(collection, filter_doc=None, *args, **kwargs):
            assert filter_doc, "full cross-shard collection scan"
            return original(collection, filter_doc, *args, **kwargs)

        monkeypatch.setattr(engine, "_find_existing", checked)
        for shard in engine._shards:
            original_find = shard.find

            def checked_local(
                collection, filter_doc, *args, _find=original_find, **kwargs
            ):
                assert filter_doc, "full local collection scan"
                return _find(collection, filter_doc, *args, **kwargs)

            monkeypatch.setattr(shard, "find", checked_local)
        result = col.update_one({"_id": 0}, update)
        assert result.matched_count == 1
        assert result.modified_count == (0 if update == {"$set": {"key": 0}} else 1)


def test_sharded_changed_unique_tokens_still_reject_conflicts(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="sqlite-sharded") as client:
        col = client.app.docs
        col.create_index("key", unique=True, partialFilterExpression={"active": True})
        col.insert_many(
            [
                {"_id": 1, "key": "same", "active": True},
                {"_id": 2, "key": "same", "active": False},
            ]
        )
        with pytest.raises(DuplicateKeyError):
            col.update_one({"_id": 2}, {"$set": {"active": True}})
        assert col.find_one({"_id": 2})["active"] is False


@pytest.mark.parametrize(
    "backend", ["memory", "json", "sqlite", "sqlite-sharded", "duckdb", "parquet"]
)
@pytest.mark.parametrize("unique", [False, True])
def test_parallel_arrays_rejected_for_all_compound_indexes(tmp_path, backend, unique):
    from pymongo.errors import OperationFailure

    with TinyMongoClient(str(tmp_path), backend=backend) as client:
        col = client.app.docs
        col.create_index([("a", 1), ("b", 1)], unique=unique)
        invalid = {"_id": 1, "a": ["x"], "b": ["y"]}
        for insert in [
            lambda: col.insert_one(invalid),
            lambda: col.insert_many([invalid]),
        ]:
            with pytest.raises(OperationFailure) as caught:
                insert()
            assert caught.value.code == 171
            assert col.count_documents({}) == 0
        col.insert_one({"_id": 1, "a": ["x"], "b": "y"})
        for mutate in [
            lambda: col.update_one({"_id": 1}, {"$set": {"b": ["y"]}}),
            lambda: col.update_many({}, {"$set": {"b": ["y"]}}),
            lambda: col.replace_one({"_id": 1}, invalid),
        ]:
            with pytest.raises(OperationFailure) as caught:
                mutate()
            assert caught.value.code == 171
            assert col.find_one({"_id": 1})["b"] == "y"
        unindexed = client.app.preexisting
        unindexed.insert_one(invalid)
        with pytest.raises(OperationFailure) as caught:
            unindexed.create_index([("a", 1), ("b", 1)], unique=unique)
        assert caught.value.code == 171
        assert [idx["name"] for idx in unindexed.list_indexes()] == ["_id_"]


@pytest.mark.parametrize("backend", ["memory", "sqlite", "sqlite-sharded"])
def test_parallel_arrays_outside_partial_index_remain_legal(tmp_path, backend):
    with TinyMongoClient(str(tmp_path), backend=backend) as client:
        col = client.app.docs
        col.create_index([("a", 1), ("b", 1)], partialFilterExpression={"active": True})
        col.insert_one({"_id": 1, "a": [1], "b": [2], "active": False})
        from pymongo.errors import OperationFailure

        with pytest.raises(OperationFailure) as caught:
            col.update_one({"_id": 1}, {"$set": {"active": True}})
        assert caught.value.code == 171
        assert col.find_one({"_id": 1})["active"] is False


@pytest.mark.parametrize(
    "options",
    [
        {"sparse": True, "partialFilterExpression": {"x": 1}},
        *[
            {"partialFilterExpression": {"x": condition}}
            for condition in [
                {"$ne": 1},
                {"$exists": False},
                {"$regex": "x"},
                {"$nin": [1]},
            ]
        ],
    ],
)
@pytest.mark.parametrize("batch", [False, True])
def test_invalid_index_declarations_raise_operation_failure_67(
    tmp_path, options, batch
):
    from pymongo import IndexModel
    from pymongo.errors import OperationFailure

    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        col = client.app.docs
        with pytest.raises(OperationFailure) as caught:
            if batch:
                col.create_indexes([IndexModel([("x", 1)], **options)])
            else:
                col.create_index("x", **options)
        assert caught.value.code == 67
        assert [idx["name"] for idx in col.list_indexes()] == ["_id_"]


def test_merge_custom_id_fallback_preserves_first_matching_owner():
    class CustomID:
        def __eq__(self, other):
            return other == 1

    storage = object.__new__(AtomicJSONStorage)
    existing = {"docs": {"1": {"_id": CustomID()}, "2": {"_id": 1}}}
    result = storage._merge_data(existing, {"docs": {"1": {"_id": 1, "v": "new"}}})
    assert result["docs"]["1"] == {"_id": 1, "v": "new"}
    assert result["docs"]["2"] == {"_id": 1}
    result = storage._merge_data(
        {"docs": {"1": {"_id": 1}}}, {"docs": {"1": {"_id": CustomID(), "v": "custom"}}}
    )
    assert list(result["docs"]) == ["1"]
    assert result["docs"]["1"]["v"] == "custom"


def test_sqlite_replacement_handles_row_disappearing_before_write(
    tmp_path, monkeypatch
):
    from tinymongo import table_backends

    backend = table_backends.SQLiteTableBackend(str(tmp_path / "data.sqlite"))
    backend.insert_many("docs", [{"_id": 1, "value": "original"}])
    monkeypatch.setattr(
        table_backends, "_local_matching_physical_row_id", lambda *args: None
    )
    backend.replace_one("docs", 1, {"_id": 1, "value": "replacement"})
    assert backend.find("docs", {}) == [{"_id": 1, "value": "original"}]


def test_async_clients_preserve_validation_and_index_error_contracts(tmp_path):
    import asyncio
    from tinymongo import AsyncMongoClient
    from pymongo.errors import OperationFailure

    async def exercise():
        async with AsyncMongoClient(str(tmp_path), backend="sqlite-sharded") as client:
            col = client.app.docs
            with pytest.raises(InvalidDocument):
                await col.insert_one({"_id": 1, "bad\x00key": 1})
            await col.create_index([("a", 1), ("b", 1)])
            with pytest.raises(OperationFailure) as caught:
                await col.insert_one({"_id": 1, "a": [1], "b": [2]})
            assert caught.value.code == 171
            assert await col.count_documents({}) == 0
            with pytest.raises(OperationFailure) as caught:
                await col.create_index("x", partialFilterExpression={"x": {"$ne": 1}})
            assert caught.value.code == 67

    asyncio.run(exercise())
