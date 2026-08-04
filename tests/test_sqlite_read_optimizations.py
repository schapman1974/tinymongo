"""Regression tests for SQLite point and declared-index read fast paths."""

import asyncio
from contextlib import contextmanager
import re
import sqlite3

import pytest

import tinymongo as tm
from tinymongo import table_backends
from tinymongo.errors import DuplicateKeyError
from tinymongo.indexes import parse_index_spec


def _collection(tmp_path, name="items"):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    return client, client.app[name]


def _track_sqlite_work(monkeypatch):
    """Record statements and payload decodes without depending on backend hooks."""

    statements = []
    decodes = []
    original_connect = table_backends.sqlite3.connect
    original_loads = table_backends._json_loads

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    def tracked_loads(value):
        decodes.append(value)
        return original_loads(value)

    monkeypatch.setattr(table_backends.sqlite3, "connect", traced_connect)
    monkeypatch.setattr(table_backends, "_json_loads", tracked_loads)
    return statements, decodes


def _payload_selects(statements):
    return [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and " DATA " in " {0} ".format(statement.upper())
    ]


def _assert_primary_key_lookup(statements):
    payload_selects = _payload_selects(statements)
    assert payload_selects
    assert all(" WHERE " in statement.upper() for statement in payload_selects)
    assert any("_ID" in statement.upper() for statement in payload_selects)


def test_sqlite_direct_id_hit_and_miss_use_bounded_primary_key_reads(
    tmp_path, monkeypatch
):
    client, collection = _collection(tmp_path, "point_reads")
    collection.insert_many(
        [
            {"_id": index, "name": "item-{0}".format(index), "body": "x" * 10_000}
            for index in range(12)
        ]
    )
    statements, decodes = _track_sqlite_work(monkeypatch)

    try:
        assert collection.find_one({"_id": 11})["name"] == "item-11"
        assert len(decodes) == 1
        _assert_primary_key_lookup(statements)

        statements.clear()
        decodes.clear()
        assert collection.find_one({"_id": 99}) is None
        assert decodes == []
        _assert_primary_key_lookup(statements)
    finally:
        client.close()


def test_sqlite_regex_id_and_logical_id_filters_keep_general_read_paths(tmp_path):
    client, collection = _collection(tmp_path, "general_id_reads")
    collection.insert_many(
        [
            {"_id": "alpha", "value": 1},
            {"_id": "beta", "value": 2},
        ]
    )

    try:
        assert collection.find_one({"_id": re.compile("^a")})["_id"] == "alpha"
        logical = {"$and": [{"_id": "beta"}]}
        assert [row["_id"] for row in collection.find(logical).limit(1)] == ["beta"]
        assert collection.count_documents(logical) == 1
    finally:
        client.close()


def test_sqlite_direct_id_eq_projection_count_and_cursor_bounds(tmp_path, monkeypatch):
    client, collection = _collection(tmp_path, "point_consumers")
    collection.insert_many(
        [
            {"_id": 1, "name": "Ada", "body": "x" * 10_000},
            {"_id": 2, "name": "Grace", "body": "y" * 10_000},
        ]
    )
    statements, decodes = _track_sqlite_work(monkeypatch)

    try:
        assert collection.find_one({"_id": {"$eq": 2}}, {"name": 1, "_id": 0}) == {
            "name": "Grace"
        }
        assert len(decodes) == 1
        _assert_primary_key_lookup(statements)

        statements.clear()
        decodes.clear()
        assert collection.count_documents({"_id": {"$eq": 2}}) == 1
        assert collection.count_documents({"_id": {"$eq": 99}}) == 0
        assert len(decodes) == 1
        _assert_primary_key_lookup(statements)

        assert list(collection.find({"_id": 2}).skip(1).limit(1)) == []
    finally:
        client.close()


def test_sqlite_direct_id_keeps_typed_and_legacy_string_collisions_exact(tmp_path):
    client, collection = _collection(tmp_path, "legacy_ids")
    backend = collection.parent.engine
    backend.create_collection(collection.name)
    connection = backend._connect()
    try:
        connection.execute(
            'INSERT INTO "legacy_ids" (_id, data) VALUES (?, ?)',
            ("1", table_backends._json_dumps({"_id": 1, "kind": "legacy-number"})),
        )
        connection.execute(
            'INSERT INTO "legacy_ids" (_id, data) VALUES (?, ?)',
            (
                "legacy-container-row",
                table_backends._json_dumps({"_id": [1, 2], "kind": "legacy-container"}),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    collection.insert_many(
        [
            {"_id": "1", "kind": "current-string"},
            {"_id": True, "kind": "boolean"},
        ]
    )

    try:
        assert collection.find_one({"_id": 1})["kind"] == "legacy-number"
        assert collection.find_one({"_id": 1.0})["kind"] == "legacy-number"
        assert collection.find_one({"_id": "1"})["kind"] == "current-string"
        assert collection.find_one({"_id": True})["kind"] == "boolean"
        assert collection.find_one({"_id": [1, 2]})["kind"] == "legacy-container"
        assert collection.find_one({"_id": {"$eq": "1"}})["kind"] == ("current-string")
    finally:
        client.close()


def test_sqlite_extended_bson_id_uses_the_bounded_primary_key_path(
    tmp_path, monkeypatch
):
    bson = pytest.importorskip("bson")
    client, collection = _collection(tmp_path, "bson_points")
    target = bson.ObjectId("00000000000000000000000b")
    collection.insert_many(
        [{"_id": index, "body": "x" * 10_000} for index in range(12)]
        + [{"_id": target, "body": "target"}]
    )
    statements, decodes = _track_sqlite_work(monkeypatch)

    try:
        assert collection.find_one({"_id": target}) == {
            "_id": target,
            "body": "target",
        }
        assert len(decodes) == 1
        _assert_primary_key_lookup(statements)
    finally:
        client.close()


def test_sqlite_repeated_point_reads_do_not_repeat_setup_sql(tmp_path, monkeypatch):
    client, collection = _collection(tmp_path, "initialized_reads")
    collection.insert_one({"_id": 1, "name": "Ada"})
    statements, _decodes = _track_sqlite_work(monkeypatch)

    try:
        for _ in range(3):
            assert collection.find_one({"_id": 1})["name"] == "Ada"

        setup_sql = "\n".join(statements).upper()
        assert "CREATE TABLE" not in setup_sql
        assert "JOURNAL_MODE" not in setup_sql
        assert "NAME='TINYDB'" not in setup_sql
        assert "NAME = 'TINYDB'" not in setup_sql
    finally:
        client.close()


def test_sqlite_double_checked_lifecycle_cache_rechecks(tmp_path, monkeypatch):
    backend = table_backends.SQLiteTableBackend(str(tmp_path / "lifecycle.sqlite"))

    @contextmanager
    def initialize_while_waiting():
        backend._sqlite_initialized = True
        yield

    monkeypatch.setattr(backend, "_write_lock", initialize_while_waiting)
    backend._ensure_sqlite_initialized()

    backend._sqlite_initialized = True

    @contextmanager
    def create_collection_while_waiting():
        backend._ready_collections.add("items")
        yield

    monkeypatch.setattr(backend, "_write_lock", create_collection_while_waiting)
    backend.create_collection("items")
    backend._migrate_legacy_blob()

    spec = parse_index_spec("value")

    @contextmanager
    def create_index_while_waiting():
        backend._ready_type_indexes.add(("items", spec.name))
        yield

    backend._ready_type_indexes.clear()
    monkeypatch.setattr(backend, "_write_lock", create_index_while_waiting)
    backend._ensure_type_index_on_connection(None, "items", spec)


def test_sqlite_initialized_collection_recovers_after_an_external_drop(tmp_path):
    first, collection = _collection(tmp_path, "externally_dropped")
    collection.insert_one({"_id": 1, "name": "before"})
    second = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    try:
        assert second.app.drop_collection("externally_dropped") is True
        assert collection.find_one({"_id": 1}) is None
        collection.insert_one({"_id": 2, "name": "after"})
        assert collection.find_one({"_id": 2})["name"] == "after"
    finally:
        second.close()
        first.close()


def test_async_sqlite_direct_id_uses_same_exact_fast_path(tmp_path, monkeypatch):
    sync_client, collection = _collection(tmp_path, "async_points")
    collection.insert_many(
        [
            {"_id": 1, "name": "Ada", "body": "x" * 10_000},
            {"_id": 2, "name": "Grace", "body": "y" * 10_000},
        ]
    )
    sync_client.close()
    statements, decodes = _track_sqlite_work(monkeypatch)

    async def scenario():
        client = tm.AsyncTinyMongoClient(str(tmp_path), backend="sqlite")
        try:
            found = await client.app.async_points.find_one(
                {"_id": {"$eq": 2}}, {"name": 1, "_id": 0}
            )
            assert found == {"name": "Grace"}
            assert await client.app.async_points.count_documents({"_id": 99}) == 0
        finally:
            await client.close()

    asyncio.run(scenario())
    assert len(decodes) == 1
    _assert_primary_key_lookup(statements)


def test_sqlite_declared_index_unions_scalar_and_array_members(tmp_path):
    client, collection = _collection(tmp_path, "indexed_union")
    collection.insert_many(
        [
            {"_id": "scalar", "value": "match"},
            {"_id": "array", "value": ["other", "match"]},
            {"_id": "other", "value": "other"},
            {"_id": "number", "value": 1},
            {"_id": "boolean", "value": True},
            {"_id": "object-array", "value": [{"nested": 1}]},
        ]
    )
    collection.create_index("value", name="value_lookup")

    try:
        assert {row["_id"] for row in collection.find({"value": "match"})} == {
            "scalar",
            "array",
        }
        assert {row["_id"] for row in collection.find({"value": {"$eq": "match"}})} == {
            "scalar",
            "array",
        }
        assert collection.count_documents({"value": {"$eq": "match"}}) == 2
        assert {row["_id"] for row in collection.find({"value": True})} == {"boolean"}
        assert {row["_id"] for row in collection.find({"value": {"$eq": True}})} == {
            "boolean"
        }
        assert {row["_id"] for row in collection.find({"value": 1})} == {"number"}
    finally:
        client.close()


def test_sqlite_bounded_index_read_stops_after_enough_exact_matches(
    tmp_path, monkeypatch
):
    client, collection = _collection(tmp_path, "indexed_bounds")
    collection.insert_many(
        [{"_id": index, "value": "match", "body": "x" * 10_000} for index in range(20)]
        + [{"_id": 100, "value": ["match"], "body": "y" * 10_000}]
    )
    collection.create_index("value")
    _statements, decodes = _track_sqlite_work(monkeypatch)

    try:
        assert len(list(collection.find({"value": "match"}).limit(1))) == 1
        assert len(decodes) == 1

        decodes.clear()
        assert len(list(collection.find({"value": "match"}).skip(2).limit(1))) == 1
        assert len(decodes) == 3
    finally:
        client.close()


def test_sqlite_indexed_array_candidates_decode_only_matching_rows(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path, "indexed_array_candidates")
    collection.insert_many(
        [
            {"_id": index, "tags": ["match" if index == 197 else "other"]}
            for index in range(250)
        ]
    )
    collection.create_index("tags")
    _statements, decodes = _track_sqlite_work(monkeypatch)

    try:
        assert [row["_id"] for row in collection.find({"tags": "match"})] == [197]
        assert len(decodes) == 1
    finally:
        client.close()


def test_sqlite_external_table_drop_rebuilds_cataloged_native_indexes(tmp_path):
    client, collection = _collection(tmp_path, "raw_drop_recovery")
    collection.insert_one({"_id": 1, "value": "before"})
    collection.create_index("value", name="value_lookup")
    backend = collection.parent.engine
    spec = parse_index_spec("value", name="value_lookup")
    physical_name = backend._physical_index_name(collection.name, spec)

    connection = sqlite3.connect(backend.path)
    try:
        connection.execute('DROP TABLE "raw_drop_recovery"')
        connection.commit()
    finally:
        connection.close()

    try:
        assert list(collection.find({"value": "before"})) == []
        collection.insert_one({"_id": 2, "value": "after"})
        assert collection.find_one({"value": "after"})["_id"] == 2

        connection = sqlite3.connect(backend.path)
        try:
            native_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
        finally:
            connection.close()
        assert physical_name in native_names
        assert physical_name + "_types" in native_names
    finally:
        client.close()


def test_sqlite_indexed_read_propagates_catalog_failures(tmp_path, monkeypatch):
    client, collection = _collection(tmp_path, "indexed_catalog_failure")
    collection.insert_one({"_id": 1, "value": "match"})
    collection.create_index("value")
    backend = collection.parent.engine

    def fail_catalog(_connection, _collection_name):
        raise DuplicateKeyError("unsafe index migration")

    monkeypatch.setattr(
        backend,
        "_get_query_index_specs_on_connection",
        fail_catalog,
    )
    try:
        with pytest.raises(DuplicateKeyError, match="unsafe index migration"):
            list(collection.find({"value": "match"}))
    finally:
        client.close()


def test_sqlite_indexed_huge_integer_falls_back_to_exact_python_matcher(tmp_path):
    client, collection = _collection(tmp_path, "indexed_huge_integer")
    huge = 2**100
    collection.insert_one({"_id": 1, "value": huge})
    collection.create_index("value")

    try:
        assert collection.find_one({"value": huge})["_id"] == 1
    finally:
        client.close()


def test_sqlite_find_falls_back_after_general_sql_compiler_failure(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path, "general_find_fallback")
    collection.insert_many(
        [
            {"_id": 1, "present": True},
            {"_id": 2},
        ]
    )
    backend = collection.parent.engine

    def fail_compile(_filter):
        raise RuntimeError("compiler unavailable")

    monkeypatch.setattr(backend.compiler, "compile", fail_compile)
    try:
        assert [
            row["_id"]
            for row in backend.find(collection.name, {"present": {"$exists": True}})
        ] == [1]
    finally:
        client.close()


def test_sqlite_rebuilds_missing_unique_and_dotted_index_storage(tmp_path):
    client, unique_collection = _collection(tmp_path, "unique_rebuild")
    unique_collection.insert_many(
        [
            {"_id": 1, "value": "one"},
            {"_id": 2, "value": "two"},
        ]
    )
    unique_collection.create_index("value", name="value_unique", unique=True)
    backend = unique_collection.parent.engine
    unique_spec = parse_index_spec("value", name="value_unique", unique=True)
    unique_name = backend._physical_index_name(unique_collection.name, unique_spec)

    connection = sqlite3.connect(backend.path)
    try:
        for name in (unique_name, unique_name + "_lookup", unique_name + "_types"):
            connection.execute(
                "DROP INDEX IF EXISTS {0}".format(
                    table_backends._quote_identifier(name)
                )
            )
        connection.commit()
    finally:
        connection.close()

    try:
        assert unique_collection.find_one({"value": "one"})["_id"] == 1

        dotted_spec = parse_index_spec("profile.name", name="profile_name")
        backend.create_index(unique_collection.name, dotted_spec)
        with backend._sqlite_state_lock:
            backend._ready_type_indexes.discard(
                (unique_collection.name, dotted_spec.name)
            )
        assert backend.create_index(unique_collection.name, dotted_spec) == (
            dotted_spec.name
        )

        connection = sqlite3.connect(backend.path)
        try:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
        finally:
            connection.close()
        assert unique_name in names
        assert unique_name + "_lookup" in names
        assert unique_name + "_types" in names
        dotted_name = backend._physical_index_name(
            unique_collection.name,
            dotted_spec,
        )
        assert dotted_name in names
        assert dotted_name + "_types" not in names
    finally:
        client.close()


def test_sqlite_companion_type_index_covers_multikey_candidate_plan(tmp_path):
    client, collection = _collection(tmp_path, "indexed_plan")
    collection.insert_many(
        [
            {"_id": index, "value": ["match"] if index == 7 else "other"}
            for index in range(12)
        ]
    )
    collection.create_index("value", name="value_lookup")
    backend = collection.parent.engine
    spec = parse_index_spec("value", name="value_lookup")
    physical_name = backend._physical_index_name(collection.name, spec)
    path = table_backends._sql_literal(table_backends._json_path("value"))
    connection = sqlite3.connect(backend.path)
    try:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT data FROM {table} "
            "WHERE json_type(data, {path}) IN ('array', 'object')".format(
                table=table_backends._quote_identifier(collection.name),
                path=path,
            )
        ).fetchall()
    finally:
        connection.close()
        client.close()

    details = " ".join(str(row[-1]) for row in plan)
    assert "USING INDEX" in details.upper()
    assert physical_name in details
