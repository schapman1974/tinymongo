"""Contracts for SQLite's empty-collection optimistic insert path."""

from collections import UserDict
import importlib

import pytest

import tinymongo as tm
import tinymongo.table_backends as table_backends
from tinymongo.errors import BulkWriteError
from tinymongo.indexes import IndexSpec


core = importlib.import_module("tinymongo.tinymongo")


def _collection(tmp_path, name="items"):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app[name]
    collection.parent.engine.create_collection(collection.name)
    return client, collection


def _raw_documents(backend, collection):
    conn = backend._connect()
    try:
        return [
            table_backends._json_loads(row[0])
            for row in conn.execute(
                "SELECT data FROM {0} ORDER BY data".format(
                    table_backends._quote_identifier(collection)
                )
            ).fetchall()
        ]
    finally:
        conn.close()


def test_sqlite_empty_clean_batch_skips_conflict_planning(tmp_path, monkeypatch):
    client, collection = _collection(tmp_path)
    backend = collection.parent.engine
    original_connect = backend._connect
    statements = []

    def tracked_connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    def unexpected_planner(*_args, **_kwargs):
        raise AssertionError("an empty clean batch should skip shared planning")

    def unexpected_candidates(*_args, **_kwargs):
        raise AssertionError("an empty clean batch should skip candidate reads")

    monkeypatch.setattr(backend, "_connect", tracked_connect)
    monkeypatch.setattr(core, "_plan_insert_many", unexpected_planner)
    monkeypatch.setattr(
        backend,
        "_find_prepared_insert_conflict_candidates",
        unexpected_candidates,
    )
    monkeypatch.setattr(backend, "find", unexpected_candidates)
    documents = [{"_id": "one", "value": 1}, {"_id": "two", "value": 2}]
    try:
        result = collection.insert_many(documents)

        assert result.inserted_ids == ["one", "two"]
        begin = statements.index("BEGIN IMMEDIATE")
        empty_probe = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith('SELECT 1 FROM "items" LIMIT 1')
        )
        first_insert = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith('INSERT INTO "items"')
        )
        commit = statements.index("COMMIT")
        assert begin < empty_probe < first_insert < commit
        assert not any(
            statement.startswith('SELECT data FROM "items"') for statement in statements
        )
        assert _raw_documents(backend, collection.name) == documents
    finally:
        client.close()


@pytest.mark.parametrize(
    ("ordered", "expected_inserted", "expected_ids"),
    [
        (True, 2, {"first", "second"}),
        (False, 3, {"first", "second", "last"}),
    ],
)
def test_sqlite_empty_duplicate_rolls_back_before_planning(
    tmp_path,
    monkeypatch,
    ordered,
    expected_inserted,
    expected_ids,
):
    client, collection = _collection(tmp_path)
    backend = collection.parent.engine
    original_candidates = backend._find_prepared_insert_conflict_candidates
    original_connect = backend._connect
    original_insert_rows = backend._insert_rows_on_connection
    fallback_counts = []
    insert_attempts = 0

    def checked_candidates(*args, **kwargs):
        conn = original_connect()
        try:
            fallback_counts.append(
                conn.execute('SELECT COUNT(*) FROM "items"').fetchone()[0]
            )
        finally:
            conn.close()
        return original_candidates(*args, **kwargs)

    def counting_insert_rows(*args, **kwargs):
        nonlocal insert_attempts
        insert_attempts += 1
        return original_insert_rows(*args, **kwargs)

    monkeypatch.setattr(
        backend,
        "_find_prepared_insert_conflict_candidates",
        checked_candidates,
    )
    monkeypatch.setattr(
        backend,
        "_insert_rows_on_connection",
        counting_insert_rows,
    )
    documents = [
        {"_id": "first"},
        {"_id": "second"},
        {"_id": "first"},
        {"_id": "last"},
    ]
    try:
        with pytest.raises(BulkWriteError) as caught:
            collection.insert_many(documents, ordered=ordered)

        assert fallback_counts == [0]
        assert insert_attempts == 2
        assert caught.value.details["nInserted"] == expected_inserted
        assert [error["index"] for error in caught.value.details["writeErrors"]] == [2]
        assert caught.value.details["writeErrors"][0]["op"] is documents[2]
        assert {document["_id"] for document in collection.find({})} == expected_ids
    finally:
        client.close()


@pytest.mark.parametrize(
    ("unique", "planner_expected"),
    [(True, True), (False, False)],
)
def test_sqlite_index_gate_keeps_only_unique_indexes_on_the_planner(
    tmp_path,
    monkeypatch,
    unique,
    planner_expected,
):
    client, collection = _collection(tmp_path)
    collection.create_index("email", unique=unique)
    original_planner = core._plan_insert_many
    planner_calls = 0

    def tracked_planner(*args, **kwargs):
        nonlocal planner_calls
        planner_calls += 1
        return original_planner(*args, **kwargs)

    monkeypatch.setattr(core, "_plan_insert_many", tracked_planner)
    try:
        collection.insert_many(
            [
                {"_id": "one", "email": "one@example.com"},
                {"_id": "two", "email": "two@example.com"},
            ]
        )

        assert bool(planner_calls) is planner_expected
    finally:
        client.close()


def test_sqlite_zero_timestamp_and_custom_mapping_skip_optimistic_path(
    tmp_path,
    monkeypatch,
):
    Timestamp = pytest.importorskip("bson").Timestamp
    client, timestamp_collection = _collection(tmp_path, "timestamps")
    custom_collection = client.app.custom_mappings
    custom_collection.parent.engine.create_collection(custom_collection.name)
    backend = timestamp_collection.parent.engine
    zero = Timestamp(0, 0)

    def unexpected_optimistic_insert(*_args, **_kwargs):
        raise AssertionError("this batch requires shared planning")

    monkeypatch.setattr(
        backend,
        "_try_empty_collection_insert_many",
        unexpected_optimistic_insert,
    )
    monkeypatch.setattr(core.time_module, "time", lambda: 1000)
    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_SECONDS", 0)
    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_INCREMENT", 0)
    timestamp_documents = [
        {"_id": "one", "stamp": zero},
        {"_id": "two", "stamp": zero},
    ]
    custom_document = UserDict({"_id": "custom", "value": 1})
    try:
        timestamp_collection.insert_many(timestamp_documents)
        custom_collection.insert_many([custom_document])

        assert [document["stamp"] for document in timestamp_documents] == [zero, zero]
        assert timestamp_collection.find_one({"_id": "one"})["stamp"] == Timestamp(
            1000, 1
        )
        assert timestamp_collection.find_one({"_id": "two"})["stamp"] == Timestamp(
            1000, 2
        )
        assert custom_collection.find_one({"_id": "custom"}) == {
            "_id": "custom",
            "value": 1,
        }
    finally:
        client.close()


def test_sqlite_nonempty_legacy_row_gates_optimistic_write(tmp_path, monkeypatch):
    client, collection = _collection(tmp_path)
    backend = collection.parent.engine
    conn = backend._connect()
    try:
        conn.execute(
            'INSERT INTO "items" (_id, data) VALUES (?, ?)',
            ("same", table_backends._json_dumps({"_id": "same"})),
        )
        conn.commit()
    finally:
        conn.close()

    original_insert_rows = backend._insert_rows_on_connection
    insert_attempts = 0

    def counting_insert_rows(*args, **kwargs):
        nonlocal insert_attempts
        insert_attempts += 1
        return original_insert_rows(*args, **kwargs)

    monkeypatch.setattr(
        backend,
        "_insert_rows_on_connection",
        counting_insert_rows,
    )
    try:
        with pytest.raises(BulkWriteError) as caught:
            collection.insert_many([{"_id": "same"}])

        assert insert_attempts == 0
        assert caught.value.details["nInserted"] == 0
        assert caught.value.details["writeErrors"][0]["index"] == 0
        assert len(_raw_documents(backend, collection.name)) == 1
    finally:
        client.close()


def test_sqlite_known_nonempty_collection_skips_repeated_empty_probes(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path)
    backend = collection.parent.engine
    original_connect = backend._connect
    conn = original_connect()
    try:
        conn.execute(
            'INSERT INTO "items" (_id, data) VALUES (?, ?)',
            (
                table_backends._physical_id_key("seed"),
                table_backends._json_dumps({"_id": "seed"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    statements = []

    def tracked_connect():
        tracked = original_connect()
        tracked.set_trace_callback(statements.append)
        return tracked

    monkeypatch.setattr(backend, "_connect", tracked_connect)
    try:
        collection.insert_many([{"_id": "first"}])
        collection.insert_many([{"_id": "second"}])

        assert (
            sum(
                statement.startswith('SELECT 1 FROM "items" LIMIT 1')
                for statement in statements
            )
            == 1
        )
    finally:
        client.close()


def test_sqlite_unexpected_optimistic_error_rolls_back_and_propagates(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path)
    backend = collection.parent.engine
    original_insert_rows = backend._insert_rows_on_connection

    def partial_insert_then_error(conn, collection_name, rows):
        original_insert_rows(conn, collection_name, rows[:1])
        raise RuntimeError("injected optimistic failure")

    monkeypatch.setattr(
        backend,
        "_insert_rows_on_connection",
        partial_insert_then_error,
    )
    try:
        with pytest.raises(RuntimeError, match="injected optimistic failure"):
            collection.insert_many([{"_id": "one"}, {"_id": "two"}])

        assert _raw_documents(backend, collection.name) == []
    finally:
        client.close()


@pytest.mark.parametrize(
    "table_sql",
    [
        'CREATE TABLE "items" (_id TEXT, data TEXT NOT NULL)',
        'CREATE TABLE "items" '
        "(_id TEXT PRIMARY KEY ON CONFLICT IGNORE, data TEXT NOT NULL)",
        'CREATE TABLE "items" '
        "(_id TEXT PRIMARY KEY ON CONFLICT REPLACE, data TEXT NOT NULL)",
    ],
)
def test_sqlite_custom_table_conflict_rules_gate_optimistic_path(
    tmp_path,
    monkeypatch,
    table_sql,
):
    client, collection = _collection(tmp_path)
    backend = collection.parent.engine
    backend.drop_collection(collection.name)
    conn = backend._connect()
    try:
        conn.execute(table_sql)
        conn.commit()
    finally:
        conn.close()
    backend._ready_collections.add(collection.name)
    original_planner = core._plan_insert_many
    planner_calls = 0

    def tracked_planner(*args, **kwargs):
        nonlocal planner_calls
        planner_calls += 1
        return original_planner(*args, **kwargs)

    monkeypatch.setattr(core, "_plan_insert_many", tracked_planner)
    documents = [{"_id": "duplicate"}, {"_id": "duplicate"}]
    try:
        with pytest.raises(BulkWriteError) as caught:
            collection.insert_many(documents)

        assert planner_calls == 1
        assert caught.value.details["nInserted"] == 1
        assert caught.value.details["writeErrors"][0]["index"] == 1
    finally:
        client.close()


@pytest.mark.parametrize(
    "schema_sql",
    [
        'CREATE TRIGGER "items_noop" AFTER INSERT ON "items" ' "BEGIN SELECT 1; END",
        'CREATE UNIQUE INDEX "items_external_email" ON "items" '
        "(json_extract(data, '$.email'))",
    ],
)
def test_sqlite_external_schema_features_gate_optimistic_path(
    tmp_path,
    monkeypatch,
    schema_sql,
):
    client, collection = _collection(tmp_path)
    backend = collection.parent.engine
    conn = backend._connect()
    try:
        conn.execute(schema_sql)
        conn.commit()
    finally:
        conn.close()
    original_planner = core._plan_insert_many
    planner_calls = 0

    def tracked_planner(*args, **kwargs):
        nonlocal planner_calls
        planner_calls += 1
        return original_planner(*args, **kwargs)

    monkeypatch.setattr(core, "_plan_insert_many", tracked_planner)
    try:
        result = collection.insert_many(
            [
                {"_id": "one", "email": "one@example.com"},
                {"_id": "two", "email": "two@example.com"},
            ]
        )

        assert result.inserted_ids == ["one", "two"]
        assert planner_calls == 1
    finally:
        client.close()


def test_sqlite_optimistic_path_fails_closed_if_catalog_read_ends_transaction(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path)
    backend = collection.parent.engine
    original_specs = backend._get_index_specs_on_connection
    original_planner = core._plan_insert_many
    spec_calls = 0
    planner_calls = 0

    def committing_specs(conn, collection_name):
        nonlocal spec_calls
        spec_calls += 1
        specs = original_specs(conn, collection_name)
        if spec_calls == 2:
            conn.commit()
        return specs

    def tracked_planner(*args, **kwargs):
        nonlocal planner_calls
        planner_calls += 1
        return original_planner(*args, **kwargs)

    monkeypatch.setattr(backend, "_get_index_specs_on_connection", committing_specs)
    monkeypatch.setattr(core, "_plan_insert_many", tracked_planner)
    try:
        result = collection.insert_many([{"_id": "one"}])

        assert result.inserted_ids == ["one"]
        assert planner_calls == 1
    finally:
        client.close()


def test_sqlite_optimistic_path_rechecks_unique_specs_inside_transaction(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path)
    backend = collection.parent.engine
    original_specs = backend._get_index_specs_on_connection
    original_planner = core._plan_insert_many
    spec_calls = 0
    planner_calls = 0

    def changing_specs(conn, collection_name):
        nonlocal spec_calls
        spec_calls += 1
        if spec_calls == 1:
            return []
        if spec_calls == 2:
            return [IndexSpec("email", unique=True)]
        return original_specs(conn, collection_name)

    def tracked_planner(*args, **kwargs):
        nonlocal planner_calls
        planner_calls += 1
        return original_planner(*args, **kwargs)

    monkeypatch.setattr(backend, "_get_index_specs_on_connection", changing_specs)
    monkeypatch.setattr(core, "_plan_insert_many", tracked_planner)
    try:
        result = collection.insert_many([{"_id": "one"}])

        assert result.inserted_ids == ["one"]
        assert planner_calls == 1
    finally:
        client.close()
