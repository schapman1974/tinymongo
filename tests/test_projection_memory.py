"""TM-011 projection-pushdown regressions for SQLite collection sweeps."""

import asyncio
import gc
from types import SimpleNamespace
import tracemalloc

import tinymongo as tm
from tinymongo.tinymongo import TinyMongoCollection


def _sqlite_collection(tmp_path, name="items"):
    client = tm.TinyMongoClient(
        str(tmp_path),
        backend="sqlite",
    )
    return client, client.app[name]


def test_sqlite_projection_scan_preserves_filter_sort_and_nested_semantics(tmp_path):
    client, collection = _sqlite_collection(tmp_path)
    collection.insert_many(
        [
            {
                "_id": 1,
                "score": 2,
                "profile": {"name": "Ada", "secret": "a"},
                "tags": ["python", "db"],
            },
            {
                "_id": 2,
                "score": 3,
                "profile": {"name": "Grace", "secret": "b"},
                "tags": ["compiler"],
            },
            {
                "_id": 3,
                "score": 1,
                "profile": {"name": "Lin", "secret": "c"},
                "tags": ["python"],
            },
        ]
    )

    try:
        collection.create_index("tags")
        assert list(
            collection.find(
                {"tags": "python"},
                {"profile.name": 1, "_id": 0},
            )
        ) == [{"profile": {"name": "Ada"}}, {"profile": {"name": "Lin"}}]

        # Both cursor styles must order by the complete source document, even
        # though ``score`` is absent from the returned projection.
        expected = [
            {"_id": 2, "profile": {"name": "Grace"}},
            {"_id": 1, "profile": {"name": "Ada"}},
            {"_id": 3, "profile": {"name": "Lin"}},
        ]
        assert (
            list(collection.find({}, {"profile.name": 1}).sort("score", -1)) == expected
        )
        assert (
            list(collection.find({}, {"profile.name": 1}, sort=[("score", -1)]))
            == expected
        )
        assert list(
            collection.find(
                {"profile.name": "Ada"},
                {"profile.name": 1, "_id": 0},
            )
        ) == [{"profile": {"name": "Ada"}}]
        assert list(
            collection.find(
                {"profile": {"$exists": True}},
                {"profile.name": 1, "_id": 0},
            )
        ) == [
            {"profile": {"name": "Ada"}},
            {"profile": {"name": "Grace"}},
            {"profile": {"name": "Lin"}},
        ]
    finally:
        client.close()


def test_sqlite_id_only_projection_keeps_lossless_identifier_types(tmp_path):
    client, collection = _sqlite_collection(tmp_path)
    identifiers = [
        None,
        False,
        True,
        1.25,
        "text-id",
        2**80,
        b"binary-id",
        {"nested": 1},
        [1, "two"],
    ]
    collection.insert_many(
        [{"_id": identifier, "payload": "large"} for identifier in identifiers]
    )

    try:
        assert list(collection.find({}, {"_id": 1})) == [
            {"_id": identifier} for identifier in identifiers
        ]
        assert collection.find_one(
            {"_id": [1, "two"]},
            {"payload": 1, "_id": 0},
        ) == {"payload": "large"}
        assert (
            list(
                collection.find(
                    {"_id": ["missing"]},
                    {"payload": 1, "_id": 0},
                )
            )
            == []
        )
        assert list(
            collection.find(
                {"_id": {"$in": [[1, "two"]]}},
                {"payload": 1, "_id": 0},
            )
        ) == [{"payload": "large"}]
    finally:
        client.close()


def test_sqlite_projection_scan_falls_back_after_sql_compilation_error(
    tmp_path, monkeypatch
):
    client, collection = _sqlite_collection(tmp_path)
    collection.insert_one({"_id": 1, "name": "Ada", "body": "large"})
    engine = collection.parent.engine
    monkeypatch.setattr(
        engine.compiler,
        "compile",
        lambda filter_doc: (_ for _ in ()).throw(ValueError("unsupported SQL")),
    )

    try:
        assert list(collection.find({}, {"name": 1})) == [{"_id": 1, "name": "Ada"}]
        assert engine._project_sqlite_id(None, None) == {}
    finally:
        client.close()


def test_async_sqlite_projection_sweep_and_chained_sort(tmp_path):
    async def scenario():
        client = tm.AsyncTinyMongoClient(
            str(tmp_path),
            backend="sqlite",
        )
        collection = client.app.async_items
        await collection.insert_many(
            [
                {"_id": 1, "rank": 2, "body": "a" * 1000},
                {"_id": 2, "rank": 1, "body": "b" * 1000},
            ]
        )
        try:
            assert await collection.find({}, {"_id": 1}).to_list(None) == [
                {"_id": 1},
                {"_id": 2},
            ]
            assert await collection.find({}, {"_id": 1}).sort("rank", 1).to_list(
                None
            ) == [{"_id": 2}, {"_id": 1}]
        finally:
            await client.close()

    asyncio.run(scenario())


def test_projection_hook_is_optional_for_third_party_backends():
    class LegacyBackend:
        def __init__(self):
            self.calls = []

        def find(self, collection, filter_doc=None):
            self.calls.append((collection, filter_doc))
            return [{"_id": 1, "body": "kept in the backend result"}]

    backend = LegacyBackend()
    collection = TinyMongoCollection(
        "items",
        SimpleNamespace(engine=backend),
    )

    assert list(collection.find({}, {"_id": 1})) == [{"_id": 1}]
    assert backend.calls == [("items", {})]


def _read_peak(collection, projection):
    gc.collect()
    tracemalloc.start()
    try:
        documents = list(collection.find({}, projection))
        _, peak = tracemalloc.get_traced_memory()
        return documents, peak
    finally:
        tracemalloc.stop()


def test_sqlite_id_projection_materially_reduces_public_sweep_peak_memory(tmp_path):
    client, collection = _sqlite_collection(tmp_path, name="large_documents")
    body = "x" * 100_000
    count = 64
    collection.insert_many(
        [
            {"_id": index, "title": "document {0}".format(index), "body": body}
            for index in range(count)
        ]
    )

    try:
        projected, projected_peak = _read_peak(collection, {"_id": 1})
        complete, complete_peak = _read_peak(collection, None)
        assert projected == [{"_id": index} for index in range(count)]
        assert len(complete) == count
        assert complete_peak - projected_peak > 4_000_000
        assert projected_peak * 5 < complete_peak
    finally:
        client.close()
