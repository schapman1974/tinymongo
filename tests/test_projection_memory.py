"""TM-011 projection and TM-015 bounded-read regressions for SQLite scans."""

import asyncio
import gc
from types import SimpleNamespace
import tracemalloc

import tinymongo as tm
import tinymongo.table_backends as table_backends
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

    assert list(collection.find({}).limit(1)) == [
        {"_id": 1, "body": "kept in the backend result"}
    ]
    assert collection.count_documents({}) == 1
    assert backend.calls == [("items", {}), ("items", {}), ("items", {})]


def test_projected_legacy_backend_reloads_complete_rows_before_sorting():
    class ProjectedLegacyBackend:
        def find(self, collection, filter_doc=None):
            return [
                {"_id": 1, "rank": 1, "body": "first"},
                {"_id": 2, "rank": 2, "body": "second"},
            ]

        def find_projected(self, collection, filter_doc, projection):
            return [{"_id": 1}, {"_id": 2}]

    collection = TinyMongoCollection(
        "items",
        SimpleNamespace(engine=ProjectedLegacyBackend()),
    )

    assert list(collection.find({}, {"_id": 1}).sort("rank", -1)) == [
        {"_id": 2},
        {"_id": 1},
    ]


def test_legacy_bounded_backend_without_projected_window_hook():
    class LegacyBoundedBackend:
        def find_bounded(self, collection, filter_doc=None, skip=0, limit=0):
            documents = [
                {"_id": 1, "body": "first"},
                {"_id": 2, "body": "second"},
            ]
            end = None if not limit else skip + limit
            return documents[skip:end]

        def find_projected(self, collection, filter_doc, projection):
            return [{"_id": 1}, {"_id": 2}]

    collection = TinyMongoCollection(
        "items",
        SimpleNamespace(engine=LegacyBoundedBackend()),
    )

    assert list(collection.find({}, {"_id": 1}).skip(1).limit(1)) == [{"_id": 2}]


def test_sqlite_subclass_legacy_projection_override_keeps_three_arg_hook(tmp_path):
    class LegacyProjectedSQLiteBackend(table_backends.SQLiteTableBackend):
        def __init__(self, path):
            super().__init__(path)
            self.projected_calls = 0

        def find_projected(self, collection, filter_doc, projection):
            self.projected_calls += 1
            return iter(super().find_projected(collection, filter_doc, projection))

    backend = LegacyProjectedSQLiteBackend(str(tmp_path / "legacy.sqlite"))
    backend.insert_many(
        "items",
        [
            {"_id": 1, "body": "first"},
            {"_id": 2, "body": "second"},
        ],
    )
    collection = TinyMongoCollection(
        "items",
        SimpleNamespace(engine=backend),
    )

    assert list(collection.find({}, {"_id": 1}).limit(1)) == [{"_id": 1}]
    assert backend.projected_calls == 1


def _track_sqlite_reads(monkeypatch, engine):
    decodes = []
    statements = []
    original_loads = table_backends._json_loads
    original_connect = engine._connect

    def tracked_loads(value):
        decodes.append(value)
        return original_loads(value)

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(table_backends, "_json_loads", tracked_loads)
    monkeypatch.setattr(engine, "_connect", traced_connect)
    return decodes, statements


def _payload_selects(statements):
    return [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT DATA FROM")
    ]


def test_sqlite_single_result_paths_push_limit_into_payload_scan(tmp_path, monkeypatch):
    client, collection = _sqlite_collection(tmp_path, name="bounded_reads")
    collection.insert_many(
        [{"_id": index, "rank": index, "body": "x" * 10_000} for index in range(12)]
    )
    decodes, statements = _track_sqlite_reads(monkeypatch, collection.parent.engine)

    try:
        operations = (
            lambda: collection.find_one({}),
            lambda: list(collection.find({}, limit=1))[0],
            lambda: list(collection.find({}).limit(1))[0],
        )
        for operation in operations:
            decodes.clear()
            statements.clear()
            assert operation()["_id"] == 0
            assert len(decodes) == 1
            payload_selects = _payload_selects(statements)
            assert len(payload_selects) == 1
            assert "LIMIT 1" in payload_selects[0].upper()

        decodes.clear()
        statements.clear()
        assert collection.find_one({}, {"_id": 1}) == {"_id": 0}
        assert decodes == []
        assert any("LIMIT 1" in sql.upper() for sql in statements)

        decodes.clear()
        assert collection.find_one({"_id": 11})["_id"] == 11
        assert len(decodes) == 1

        query = {"rank": 0}
        cursor = collection.find(query).limit(1)
        query["rank"] = 11
        assert list(cursor)[0]["_id"] == 0
    finally:
        client.close()


def test_sqlite_python_filtered_limit_stops_after_first_match(tmp_path, monkeypatch):
    client, collection = _sqlite_collection(tmp_path, name="filtered_reads")
    collection.insert_many(
        [
            {
                "_id": index,
                "nested": {"selected": index == 3},
                "body": "x" * 10_000,
            }
            for index in range(12)
        ]
    )
    decodes, _statements = _track_sqlite_reads(
        monkeypatch,
        collection.parent.engine,
    )

    try:
        for operation in (
            lambda: collection.find_one({"nested.selected": True}),
            lambda: list(collection.find({"nested.selected": True}).limit(1))[0],
        ):
            decodes.clear()
            assert operation()["_id"] == 3
            assert len(decodes) == 4

        decodes.clear()
        assert collection.find_one({"nested.selected": "missing"}) is None
        assert len(decodes) == 12

        decodes.clear()
        assert (
            list(
                collection.find({"nested.selected": {"$exists": True}}).skip(2).limit(1)
            )[0]["_id"]
            == 2
        )
        assert len(decodes) == 3
    finally:
        client.close()


def test_sqlite_native_count_does_not_read_payloads(tmp_path, monkeypatch):
    client, collection = _sqlite_collection(tmp_path, name="native_counts")
    collection.insert_many(
        [
            {"_id": index, "kind": "even" if index % 2 == 0 else "odd"}
            for index in range(12)
        ]
    )
    decodes, statements = _track_sqlite_reads(monkeypatch, collection.parent.engine)

    try:
        for operation, expected in (
            (lambda: collection.count_documents({}), 12),
            (
                lambda: collection.count_documents({"kind": {"$exists": True}}),
                12,
            ),
            (collection.estimated_document_count, 12),
        ):
            decodes.clear()
            statements.clear()
            assert operation() == expected
            assert decodes == []
            assert any("SELECT COUNT(*)" in sql.upper() for sql in statements)
            assert _payload_selects(statements) == []
    finally:
        client.close()


def test_sqlite_python_filtered_count_streams_exact_documents(tmp_path, monkeypatch):
    client, collection = _sqlite_collection(tmp_path, name="filtered_counts")
    collection.insert_many(
        [{"_id": index, "values": list(range(index % 4))} for index in range(12)]
    )
    decodes, statements = _track_sqlite_reads(monkeypatch, collection.parent.engine)

    try:
        assert collection.count_documents({"values": {"$size": 2}}) == 3
        assert len(decodes) == 12
        assert len(_payload_selects(statements)) == 1
    finally:
        client.close()


def test_sqlite_cursor_window_reconfiguration_and_sort_precedence(tmp_path):
    client, collection = _sqlite_collection(tmp_path, name="cursor_windows")
    collection.insert_many([{"_id": index, "rank": 10 - index} for index in range(6)])

    try:
        assert [doc["_id"] for doc in collection.find({}).limit(1).skip(2)] == [2]
        assert [doc["_id"] for doc in collection.find({}).limit(1).limit(0)] == list(
            range(6)
        )
        assert [doc["_id"] for doc in collection.find({}).skip(4)] == [4, 5]
        assert [doc["_id"] for doc in collection.find({}).limit(1).sort("rank", 1)] == [
            5
        ]
        cursor = collection.find({}, {"_id": 1}).skip(2).limit(1)
        assert list(cursor.clone()) == [{"_id": 2}]
        assert list(cursor) == [{"_id": 2}]
        assert list(cursor.rewind()) == [{"_id": 2}]

        sorted_cursor = collection.find({}).sort("rank", -1).limit(2)
        assert [doc["_id"] for doc in sorted_cursor.clone()] == [0, 1]
        assert [doc["_id"] for doc in sorted_cursor] == [0, 1]

        lazy = collection.find({})
        assert lazy.alive is True
        lazy.close()
        assert lazy.alive is False

        empty_cursor = client.app.empty.find({})
        assert empty_cursor.alive is True
        assert empty_cursor.has_next() is False
        assert empty_cursor.hasNext() is False
        assert empty_cursor.alive is False
    finally:
        client.close()


def test_sqlite_bounded_and_count_fallbacks_after_compiler_error(tmp_path, monkeypatch):
    client, collection = _sqlite_collection(tmp_path, name="fallbacks")
    collection.insert_many([{"_id": index, "kind": "match"} for index in range(4)])
    engine = collection.parent.engine
    monkeypatch.setattr(
        engine.compiler,
        "compile",
        lambda filter_doc: (_ for _ in ()).throw(ValueError("unsupported SQL")),
    )

    try:
        assert list(collection.find({}).limit(1)) == [{"_id": 0, "kind": "match"}]
        assert collection.count_documents({}) == 4
    finally:
        client.close()


def test_sqlite_indexed_sorted_find_and_projected_filtered_skip(tmp_path):
    client, collection = _sqlite_collection(tmp_path, name="indexed_sorted")
    collection.insert_many(
        [
            {"_id": 1, "tags": ["python"], "name": "Ada"},
            {"_id": 2, "tags": ["other"], "name": "Grace"},
            {"_id": 3, "tags": ["python"], "name": "Lin"},
        ]
    )
    collection.create_index("tags")

    try:
        assert list(collection.find({"tags": "python"}).sort("_id", -1)) == [
            {"_id": 3, "tags": ["python"], "name": "Lin"},
            {"_id": 1, "tags": ["python"], "name": "Ada"},
        ]
        assert list(
            collection.find(
                {"_id": {"$in": [1, 3]}},
                {"name": 1, "_id": 0},
            )
            .skip(1)
            .limit(1)
        ) == [{"name": "Lin"}]
    finally:
        client.close()


def test_async_sqlite_single_result_and_count_pushdown(tmp_path, monkeypatch):
    sync_client, sync_collection = _sqlite_collection(
        tmp_path,
        name="async_bounded_reads",
    )
    sync_collection.insert_many(
        [{"_id": index, "body": "x" * 10_000} for index in range(12)]
    )
    sync_client.close()
    decodes, _statements = _track_sqlite_reads(
        monkeypatch,
        sync_collection.parent.engine,
    )

    async def scenario():
        client = tm.AsyncTinyMongoClient(str(tmp_path), backend="sqlite")
        collection = client.app.async_bounded_reads
        try:
            decodes.clear()
            assert (await collection.find_one({}))["_id"] == 0
            assert len(decodes) == 1

            decodes.clear()
            assert await collection.find({}).limit(1).to_list(None) == [
                {"_id": 0, "body": "x" * 10_000}
            ]
            assert len(decodes) == 1

            decodes.clear()
            assert await collection.count_documents({}) == 12
            assert decodes == []
        finally:
            await client.close()

    asyncio.run(scenario())


def _read_peak(collection, projection):
    gc.collect()
    tracemalloc.start()
    try:
        documents = list(collection.find({}, projection))
        _, peak = tracemalloc.get_traced_memory()
        return documents, peak
    finally:
        tracemalloc.stop()


def _operation_peak(operation):
    gc.collect()
    tracemalloc.start()
    try:
        result = operation()
        _, peak = tracemalloc.get_traced_memory()
        return result, peak
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
        first, first_peak = _operation_peak(lambda: collection.find_one({}))
        counted, count_peak = _operation_peak(lambda: collection.count_documents({}))
        projected, projected_peak = _read_peak(collection, {"_id": 1})
        complete, complete_peak = _read_peak(collection, None)
        assert first["_id"] == 0
        assert counted == count
        assert projected == [{"_id": index} for index in range(count)]
        assert len(complete) == count
        assert complete_peak - projected_peak > 4_000_000
        assert projected_peak * 5 < complete_peak
        assert first_peak * 5 < complete_peak
        assert count_peak * 5 < complete_peak
    finally:
        client.close()
