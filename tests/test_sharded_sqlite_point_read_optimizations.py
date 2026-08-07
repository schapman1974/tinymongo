"""Focused regressions for the sharded SQLite point-read fast path."""

import builtins
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
import runpy
import select
import shutil
import signal
import sqlite3
import sys
import threading
import warnings

import pytest
import tinymongo

from tinymongo.errors import ConfigurationError, StorageCorruptionError
from tinymongo import sharded_sqlite
from tinymongo import table_backends


def _open_client(root, **options):
    return tinymongo.TinyMongoClient(
        str(root),
        backend="sqlite-sharded",
        sqlite_shards=2,
        **options,
    )


def _child_connect_tracker(monkeypatch, shard_path):
    """Record sqlite connections opened for one physical shard."""

    original_connect = sqlite3.connect
    opened = []

    def tracking_connect(database, *args, **kwargs):
        connection = original_connect(database, *args, **kwargs)
        if os.fspath(database) == os.fspath(shard_path):
            opened.append(connection)
        return connection

    # All backend modules refer to the same stdlib sqlite3 module object.
    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    return opened


def _id_for_shard(engine, shard_index):
    for ordinal in range(10_000):
        document_id = "point-read-{0}".format(ordinal)
        if engine._shard_index(document_id) == shard_index:
            return document_id
    raise AssertionError("could not find an ID for shard {0}".format(shard_index))


def test_point_reads_reuse_child_connection_and_invalidate_on_close_and_pid_change(
    tmp_path,
    monkeypatch,
):
    client = _open_client(tmp_path / "reuse")
    database = client.app
    collection = database.items
    collection.insert_one({"_id": "target", "value": 7})
    engine = database.engine
    shard = engine._shards[engine._shard_index("target")]

    # Start from an empty process-local read pool. A retained database handle
    # is deliberately reusable after close.
    database.close()
    opened = _child_connect_tracker(monkeypatch, shard.path)

    for _ in range(8):
        assert collection.find_one({"_id": "target"})["value"] == 7
    assert len(opened) == 1

    database.close()
    assert collection.find_one({"_id": "target"})["value"] == 7
    assert len(opened) == 2
    assert collection.find_one({"_id": "target"})["value"] == 7
    assert len(opened) == 2

    real_pid = os.getpid()
    monkeypatch.setattr(sharded_sqlite.os, "getpid", lambda: real_pid + 10_000)
    assert collection.find_one({"_id": "target"})["value"] == 7
    assert len(opened) == 3
    assert collection.find_one({"_id": "target"})["value"] == 7
    assert len(opened) == 3

    client.close()


def test_point_read_pool_is_thread_safe_and_bounded_by_simultaneous_readers(
    tmp_path,
    monkeypatch,
):
    client = _open_client(tmp_path / "threads")
    database = client.app
    collection = database.items
    collection.insert_one({"_id": "target", "value": "kept"})
    engine = database.engine
    shard = engine._shards[engine._shard_index("target")]
    database.close()
    opened = _child_connect_tracker(monkeypatch, shard.path)

    workers = 4

    def read_repeatedly(_worker):
        return [collection.find_one({"_id": "target"})["value"] for _ in range(20)]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(read_repeatedly, range(workers)))

    assert results == [["kept"] * 20] * workers
    assert 1 <= len(opened) <= workers
    client.close()


def test_database_error_discards_persistent_child_read_connection(
    tmp_path,
    monkeypatch,
):
    client = _open_client(tmp_path / "read-error")
    database = client.app
    collection = database.items
    collection.insert_one({"_id": "target", "value": 7})
    engine = database.engine
    shard = engine._shards[engine._shard_index("target")]
    database.close()
    opened = _child_connect_tracker(monkeypatch, shard.path)

    assert collection.find_one({"_id": "target"})["value"] == 7
    assert len(opened) == 1
    broken_connection = opened[0]
    denied_once = [False]

    def deny_one_select(action, _arg1, _arg2, _database, _trigger):
        if action == sqlite3.SQLITE_SELECT and not denied_once[0]:
            denied_once[0] = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    broken_connection.set_authorizer(deny_one_select)
    try:
        result = collection.find_one({"_id": "target"})
    except sqlite3.DatabaseError:
        result = None
    else:
        # Retrying the failed read transparently is also a valid contract.
        assert result["value"] == 7
    assert denied_once[0]

    assert collection.find_one({"_id": "target"})["value"] == 7
    assert len(opened) == 2
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        broken_connection.execute("SELECT 1")
    client.close()


def test_close_during_leased_point_read_retires_connection_after_result(
    tmp_path,
    monkeypatch,
):
    client = _open_client(tmp_path / "close-during-read")
    database = client.app
    collection = database.items
    collection.insert_one({"_id": "target", "value": 7})
    shard = database.engine._shards[database.engine._shard_index("target")]
    original_point_read_connection = shard._point_read_connection
    leased = threading.Event()
    resume = threading.Event()
    leased_connections = []

    @contextmanager
    def pause_while_leased():
        with original_point_read_connection() as connection:
            if not leased_connections:
                leased_connections.append(connection)
                leased.set()
                if not resume.wait(10):
                    raise RuntimeError("timed out waiting to resume the point read")
            yield connection

    monkeypatch.setattr(shard, "_point_read_connection", pause_while_leased)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(collection.find_one, {"_id": "target"})
        try:
            assert leased.wait(10), "point read never leased its connection"
            database.close()
            assert not future.done()
        finally:
            resume.set()
        assert future.result(timeout=10) == {"_id": "target", "value": 7}

    assert 1 <= len(leased_connections) <= 2
    database.close()
    for connection in leased_connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")
    client.close()


def test_pooled_point_reader_is_query_only_and_does_not_pin_wal_checkpoint(tmp_path):
    client = _open_client(tmp_path / "query-only")
    database = client.app
    collection = database.items
    document_id = _id_for_shard(database.engine, 0)
    collection.insert_one({"_id": document_id, "value": 0})
    shard = database.engine._shards[0]

    for _ in range(25):
        assert collection.find_one({"_id": document_id})["value"] == 0
    collection.update_one({"_id": document_id}, {"$set": {"value": 1}})
    for _ in range(25):
        assert collection.find_one({"_id": document_id})["value"] == 1

    with shard._point_read_pool_lock:
        assert len(shard._point_read_pool_idle) == 1
        pooled = shard._point_read_pool_idle[0].connection
    assert pooled.execute("PRAGMA query_only").fetchone() == (1,)
    assert pooled.in_transaction is False

    checkpoint_connection = sqlite3.connect(shard.path, timeout=30)
    try:
        checkpoint = checkpoint_connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
    finally:
        checkpoint_connection.close()
    assert checkpoint == (0, 0, 0)
    assert pooled.in_transaction is False
    client.close()


@pytest.mark.skipif(
    not hasattr(os, "fork")
    or (hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled()),
    reason="requires a GIL-enabled POSIX fork",
)
def test_warmed_point_reader_is_not_reused_after_real_fork(tmp_path, monkeypatch):
    client = _open_client(tmp_path / "fork")
    database = client.app
    collection = database.items
    collection.insert_one({"_id": "target", "value": 7})
    shard = database.engine._shards[database.engine._shard_index("target")]
    database.close()
    opened = _child_connect_tracker(monkeypatch, shard.path)
    assert collection.find_one({"_id": "target"})["value"] == 7
    assert len(opened) == 1
    inherited_connection = opened[0]
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()

    if child_pid == 0:  # pragma: no cover - assertions are reported by the parent
        os.close(read_fd)
        exit_code = 0
        try:
            collection.find_one({"_id": "target"})
        except ConfigurationError as exc:
            payload = "blocked|{0}|{1}".format(len(opened), exc)
        except BaseException as exc:
            payload = "error|{0}|{1}".format(type(exc).__name__, exc)
            exit_code = 1
        else:
            payload = "error|inherited backend unexpectedly remained usable"
            exit_code = 1
        try:
            os.write(write_fd, payload.encode("utf8", "replace"))
        finally:
            os.close(write_fd)
        os._exit(exit_code)

    os.close(write_fd)
    waited = False
    try:
        assert collection.find_one({"_id": "target"})["value"] == 7
        ready, _writable, _errors = select.select([read_fd], [], [], 15)
        if not ready:
            os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)
            waited = True
            pytest.fail("forked point-read child timed out")
        payload = os.read(read_fd, 4096).decode("utf8", "replace")
        _pid, status = os.waitpid(child_pid, 0)
        waited = True
    finally:
        os.close(read_fd)
        if not waited:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)

    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0, payload
    assert payload.startswith("blocked|1|Sharded SQLite handles cannot be reused")
    assert len(opened) == 2
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        inherited_connection.execute("SELECT 1")
    assert collection.find_one({"_id": "target"})["value"] == 7
    client.close()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows does not permit replacing an open SQLite database file",
)
def test_warmed_reader_rejects_shard_file_replaced_by_another_shard(tmp_path):
    client = _open_client(tmp_path / "replaced-shard")
    database = client.app
    collection = database.items
    document_id = _id_for_shard(database.engine, 0)
    collection.insert_one({"_id": document_id, "value": "kept"})
    target = database.engine._shards[0]
    other = database.engine._shards[1]

    for shard in (target, other):
        connection = sqlite3.connect(shard.path, timeout=30)
        try:
            assert (
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
            )
        finally:
            connection.close()

    assert collection.find_one({"_id": document_id})["value"] == "kept"
    with target._point_read_pool_lock:
        warmed_connection = target._point_read_pool_idle[0].connection

    replacement = tmp_path / "replacement-shard.sqlite"
    shutil.copy2(other.path, replacement)
    os.replace(replacement, target.path)

    try:
        with pytest.raises(StorageCorruptionError, match="identity mismatch"):
            collection.find_one({"_id": document_id})
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            warmed_connection.execute("SELECT 1")
    finally:
        client.close()


def test_manifest_catalog_join_is_cached_across_unchanged_point_reads(tmp_path):
    client = _open_client(tmp_path / "manifest-cache")
    database = client.app
    collection = database.items
    collection.insert_one({"_id": "target", "value": 7})
    engine = database.engine
    statements = []

    with engine._manifest_read_connection() as connection:
        connection.set_trace_callback(statements.append)
    try:
        for _ in range(8):
            assert collection.find_one({"_id": "target"})["value"] == 7
    finally:
        with engine._manifest_read_connection() as connection:
            connection.set_trace_callback(None)

    catalog_joins = [
        statement
        for statement in statements
        if "LEFT JOIN __tinymongo_collections" in statement
    ]
    assert len(catalog_joins) <= 1
    client.close()


def test_manifest_cache_observes_drop_recreate_from_another_backend(tmp_path):
    root = tmp_path / "drop-recreate"
    writer = _open_client(root)
    reader = _open_client(root)
    writer.app.items.insert_one({"_id": "same", "version": "old"})

    assert reader.app.items.find_one({"_id": "same"})["version"] == "old"
    writer.app.drop_collection("items")
    writer.app.items.insert_one({"_id": "same", "version": "new"})

    assert reader.app.items.find_one({"_id": "same"}) == {
        "_id": "same",
        "version": "new",
    }
    reader.close()
    writer.close()


def test_manifest_cache_fails_closed_after_external_state_change(tmp_path):
    client = _open_client(tmp_path / "external-manifest")
    database = client.app
    collection = database.items
    collection.insert_one({"_id": "target", "value": 7})
    assert collection.find_one({"_id": "target"})["value"] == 7

    connection = sqlite3.connect(database.engine._manifest_path)
    try:
        connection.execute(
            "UPDATE __tinymongo_collections SET state = 'dropping' "
            "WHERE collection_name = 'items'"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StorageCorruptionError, match="interrupted drop"):
        collection.find_one({"_id": "target"})

    # Restore the fixture so normal close/reopen cleanup is not responsible
    # for proving that the cached generation was invalidated.
    connection = sqlite3.connect(database.engine._manifest_path)
    try:
        connection.execute(
            "UPDATE __tinymongo_collections SET state = 'ready' "
            "WHERE collection_name = 'items'"
        )
        connection.commit()
    finally:
        connection.close()
    assert collection.find_one({"_id": "target"})["value"] == 7
    client.close()


def test_manifest_generation_revalidates_immutable_config(tmp_path):
    client = _open_client(tmp_path / "external-config")
    database = client.app
    collection = database.items
    collection.insert_one({"_id": "target", "value": 7})
    assert collection.find_one({"_id": "target"})["value"] == 7

    connection = sqlite3.connect(database.engine._manifest_path)
    try:
        connection.execute("UPDATE __tinymongo_config SET state = 'initializing'")
        connection.commit()
    finally:
        connection.close()

    try:
        with pytest.raises(StorageCorruptionError, match="identity changed"):
            collection.find_one({"_id": "target"})
    finally:
        connection = sqlite3.connect(database.engine._manifest_path)
        try:
            connection.execute("UPDATE __tinymongo_config SET state = 'ready'")
            connection.commit()
        finally:
            connection.close()
        client.close()


def test_exact_id_find_one_bypasses_generic_find_with_projection_and_typed_ids(
    tmp_path,
    monkeypatch,
):
    client = _open_client(tmp_path / "exact-id")
    database = client.app
    collection = database.items
    collection.insert_many(
        [
            {
                "_id": 1,
                "kind": "number",
                "nested": {"items": [1, 2]},
                "secret": "hidden",
            },
            {
                "_id": True,
                "kind": "boolean",
                "nested": {"items": [3, 4]},
                "secret": "hidden",
            },
        ]
    )

    def reject_generic_find(*_args, **_kwargs):
        raise AssertionError("exact-id find_one used the generic find path")

    monkeypatch.setattr(database.engine, "find", reject_generic_find)

    assert collection.find_one({"_id": 1.0}, {"kind": 1, "_id": 0}) == {
        "kind": "number"
    }
    assert collection.find_one({"_id": True}, {"secret": 0}) == {
        "_id": True,
        "kind": "boolean",
        "nested": {"items": [3, 4]},
    }
    assert collection.find_one({"_id": "missing"}) is None

    first = collection.find_one({"_id": True})
    first["nested"]["items"].append("changed")
    second = collection.find_one({"_id": True})
    assert second["nested"]["items"] == [3, 4]
    client.close()


def test_exact_id_find_one_computes_one_physical_key(tmp_path, monkeypatch):
    client = _open_client(tmp_path / "single-physical-key")
    collection = client.app.items
    collection.insert_one({"_id": "target", "value": 7})
    calls = []
    original_sharded_key = sharded_sqlite._physical_id_key
    original_table_key = table_backends._physical_id_key

    def count_sharded_key(value):
        calls.append(("route", value))
        return original_sharded_key(value)

    def count_table_key(value):
        calls.append(("lookup", value))
        return original_table_key(value)

    monkeypatch.setattr(sharded_sqlite, "_physical_id_key", count_sharded_key)
    monkeypatch.setattr(table_backends, "_physical_id_key", count_table_key)

    assert collection.find_one({"_id": "target"}) == {
        "_id": "target",
        "value": 7,
    }
    assert calls == [("route", "target")]
    client.close()


def test_exact_id_find_one_uses_one_row_primary_key_select(tmp_path, monkeypatch):
    client = _open_client(tmp_path / "single-row-select")
    database = client.app
    collection = database.items
    collection.insert_one({"_id": "target", "value": 7})
    shard = database.engine._shards[database.engine._shard_index("target")]
    database.close()
    original_connect = sqlite3.connect
    statements = []

    def tracing_connect(path, *args, **kwargs):
        connection = original_connect(path, *args, **kwargs)
        if os.fspath(path) == os.fspath(shard.path):
            connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracing_connect)
    assert collection.find_one({"_id": "target"})["value"] == 7

    point_selects = [
        statement
        for statement in statements
        if "FROM items" in statement.replace('"', "")
        and "WHERE _id" in statement.replace('"', "")
    ]
    assert len(point_selects) == 1
    normalized = point_selects[0].replace('"', "")
    assert "WHERE _id =" in normalized
    assert "LIMIT 1" in point_selects[0]
    client.close()


def test_exact_id_fast_path_preserves_configured_read_fidelity(
    tmp_path,
    monkeypatch,
):
    client = tinymongo.MongoClient(
        str(tmp_path / "read-fidelity"),
        backend="sqlite-sharded",
        sqlite_shards=2,
        document_class=OrderedDict,
        tz_aware=True,
    )
    database = client.app
    collection = database.items
    stored = datetime(
        2026,
        1,
        2,
        3,
        4,
        5,
        123456,
        tzinfo=timezone(timedelta(hours=-5)),
    )
    collection.insert_one(
        {
            "_id": "target",
            "when": stored,
            "nested": {"when": stored},
        }
    )

    def reject_generic_find(*_args, **_kwargs):
        raise AssertionError("exact-id find_one used the generic find path")

    monkeypatch.setattr(database.engine, "find", reject_generic_find)
    found = collection.find_one({"_id": "target"})

    assert type(found) is OrderedDict
    assert type(found["nested"]) is OrderedDict
    assert found["when"] == datetime(
        2026,
        1,
        2,
        8,
        4,
        5,
        123000,
        tzinfo=timezone.utc,
    )
    assert found["nested"]["when"] == found["when"]
    client.close()


def test_non_exact_find_one_keeps_generic_fallback(tmp_path, monkeypatch):
    client = _open_client(tmp_path / "generic-fallback")
    database = client.app
    collection = database.items
    collection.insert_many(
        [
            {"_id": "first", "kind": "skip"},
            {"_id": "second", "kind": "target"},
        ]
    )
    original_find = database.engine.find
    calls = []

    def recording_find(*args, **kwargs):
        calls.append((args, kwargs))
        return original_find(*args, **kwargs)

    monkeypatch.setattr(database.engine, "find", recording_find)
    assert collection.find_one({"kind": "target"})["_id"] == "second"
    assert calls
    client.close()


def test_module_import_without_register_at_fork_covers_portable_path(monkeypatch):
    real_hasattr = builtins.hasattr

    def without_register_at_fork(value, name):
        if value is os and name == "register_at_fork":
            return False
        return real_hasattr(value, name)

    monkeypatch.setattr(builtins, "hasattr", without_register_at_fork)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        namespace = runpy.run_module(
            "tinymongo.sharded_sqlite",
            run_name="tinymongo._sharded_sqlite_without_at_fork",
        )

    assert "_PointReadConnection" in namespace


def test_setting_the_same_point_read_identity_is_a_noop(tmp_path, monkeypatch):
    client = _open_client(tmp_path / "same-identity")
    shard = client.app.engine._shards[0]
    identity = shard._point_read_identity
    retire_calls = []
    monkeypatch.setattr(
        shard,
        "_retire_point_read_connections",
        lambda: retire_calls.append(True),
    )

    shard._set_point_read_identity(identity)

    assert retire_calls == []
    client.close()


def test_point_read_file_identity_rejects_missing_and_non_file_paths(tmp_path):
    missing = sharded_sqlite._ShardSQLiteTableBackend(str(tmp_path / "missing.sqlite"))
    with pytest.raises(StorageCorruptionError, match="file disappeared"):
        missing._point_read_file_identity()

    directory_path = tmp_path / "directory.sqlite"
    directory_path.mkdir()
    directory = sharded_sqlite._ShardSQLiteTableBackend(str(directory_path))
    with pytest.raises(StorageCorruptionError, match="path is not a file"):
        directory._point_read_file_identity()


def test_standalone_point_reader_skips_uninstalled_shard_identity(tmp_path):
    backend = sharded_sqlite._ShardSQLiteTableBackend(
        str(tmp_path / "standalone.sqlite")
    )
    backend.create_collection("items")
    assert backend._point_read_identity is None

    entry = backend._open_point_read_connection()
    try:
        assert entry.connection.execute("PRAGMA query_only").fetchone() == (1,)
    finally:
        entry.connection.close()


def test_point_reader_retries_one_file_identity_change(tmp_path, monkeypatch):
    backend = sharded_sqlite._ShardSQLiteTableBackend(str(tmp_path / "retry.sqlite"))
    backend.create_collection("items")
    identities = iter([(1, 1), (1, 2), (1, 2), (1, 2)])
    monkeypatch.setattr(
        backend,
        "_point_read_file_identity",
        lambda: next(identities),
    )

    entry = backend._open_point_read_connection()
    entry.connection.close()


def test_point_reader_rejects_repeated_file_identity_changes(tmp_path, monkeypatch):
    backend = sharded_sqlite._ShardSQLiteTableBackend(
        str(tmp_path / "repeated-change.sqlite")
    )
    backend.create_collection("items")
    identities = iter([(1, 1), (1, 2), (1, 3), (1, 4)])
    monkeypatch.setattr(
        backend,
        "_point_read_file_identity",
        lambda: next(identities),
    )

    with pytest.raises(StorageCorruptionError, match="changed repeatedly"):
        backend._open_point_read_connection()


def test_idle_point_reader_closes_when_file_identity_check_fails(
    tmp_path,
    monkeypatch,
):
    backend = sharded_sqlite._ShardSQLiteTableBackend(
        str(tmp_path / "identity-check-error.sqlite")
    )
    backend.create_collection("items")
    entry = backend._open_point_read_connection()
    backend._point_read_pool_idle.append(entry)

    def fail_identity_check():
        raise StorageCorruptionError("identity lookup failed")

    monkeypatch.setattr(backend, "_point_read_file_identity", fail_identity_check)

    with pytest.raises(StorageCorruptionError, match="identity lookup failed"):
        with backend._point_read_connection():
            pass

    assert backend._point_read_pool_idle == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        entry.connection.execute("SELECT 1")


def test_in_transaction_reader_is_poisoned_when_rollback_fails(tmp_path):
    backend = sharded_sqlite._ShardSQLiteTableBackend(
        str(tmp_path / "rollback-error.sqlite")
    )
    backend.create_collection("items")
    closed = []

    class RollbackFailingConnection:
        in_transaction = True

        def rollback(self):
            raise sqlite3.OperationalError("rollback failed")

        def close(self):
            closed.append(True)

    connection = RollbackFailingConnection()
    backend._point_read_pool_idle.append(
        sharded_sqlite._PointReadConnection(
            connection,
            backend._point_read_file_identity(),
        )
    )

    with backend._point_read_connection() as leased:
        assert leased is connection

    assert closed == [True]
    assert backend._point_read_pool_idle == []


def test_point_read_recovers_once_from_a_missing_collection(tmp_path):
    backend = sharded_sqlite._ShardSQLiteTableBackend(
        str(tmp_path / "missing-table-retry.sqlite")
    )
    attempts = []

    def missing_once(_connection):
        attempts.append(True)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("no such table: items")
        return "recovered"

    assert backend._run_point_read("items", missing_once) == "recovered"
    assert len(attempts) == 2


def test_point_read_wraps_repeated_missing_collection_errors(tmp_path):
    backend = sharded_sqlite._ShardSQLiteTableBackend(
        str(tmp_path / "missing-table-exhausted.sqlite")
    )
    attempts = []

    def always_missing(_connection):
        attempts.append(True)
        raise sqlite3.OperationalError("no such table: items")

    with pytest.raises(StorageCorruptionError, match="changed repeatedly"):
        backend._run_point_read("items", always_missing)
    assert len(attempts) == 2


def test_point_read_defensive_unreachable_guard(tmp_path, monkeypatch):
    backend = sharded_sqlite._ShardSQLiteTableBackend(
        str(tmp_path / "unreachable.sqlite")
    )
    monkeypatch.setattr(sharded_sqlite, "range", lambda _count: (), raising=False)

    with pytest.raises(AssertionError, match="unreachable"):
        backend._run_point_read("items", lambda _connection: None)


def test_exact_point_read_scans_an_unpredictable_legacy_container_id(tmp_path):
    backend = sharded_sqlite._ShardSQLiteTableBackend(
        str(tmp_path / "legacy-container.sqlite")
    )
    backend.create_collection("items")
    document_id = {"first": 1, "second": 2}
    document = {"_id": document_id, "value": "legacy"}
    conn = backend._connect()
    try:
        conn.execute(
            'INSERT INTO "items" (_id, data) VALUES (?, ?)',
            ("unpredictable-legacy-key", sharded_sqlite.bson_json_dumps(document)),
        )
        conn.commit()
    finally:
        conn.close()

    assert (
        backend.find_exact_physical_id(
            "items",
            document_id,
            sharded_sqlite._physical_id_key(document_id),
        )
        == document
    )


def test_exact_point_read_checks_later_candidates_after_filter_mismatch(
    tmp_path,
    monkeypatch,
):
    backend = sharded_sqlite._ShardSQLiteTableBackend(
        str(tmp_path / "candidate-filter-mismatch.sqlite")
    )
    backend.create_collection("items")
    conn = backend._connect()
    try:
        conn.execute(
            'INSERT INTO "items" (_id, data) VALUES (?, ?)',
            (
                "first-candidate",
                sharded_sqlite.bson_json_dumps({"_id": "target", "kind": "different"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        sharded_sqlite,
        "_physical_id_candidates",
        lambda _expected, current=None: ("first-candidate", "second-candidate"),
    )

    assert (
        backend.find_exact_physical_id(
            "items",
            "target",
            "current-candidate",
            filter_doc={"_id": "target", "kind": "wanted"},
        )
        is None
    )


def test_exact_point_read_handles_an_empty_legacy_scan(tmp_path):
    backend = sharded_sqlite._ShardSQLiteTableBackend(
        str(tmp_path / "empty-legacy-scan.sqlite")
    )
    backend.create_collection("items")
    document_id = {"first": 1, "second": 2}

    assert (
        backend.find_exact_physical_id(
            "items",
            document_id,
            sharded_sqlite._physical_id_key(document_id),
        )
        is None
    )


def test_exact_point_read_skips_a_nonmatching_legacy_scan_row(tmp_path):
    backend = sharded_sqlite._ShardSQLiteTableBackend(
        str(tmp_path / "nonmatching-legacy-scan.sqlite")
    )
    backend.create_collection("items")
    conn = backend._connect()
    try:
        conn.execute(
            'INSERT INTO "items" (_id, data) VALUES (?, ?)',
            (
                "unpredictable-legacy-key",
                sharded_sqlite.bson_json_dumps(
                    {"_id": {"other": 3}, "value": "not-target"}
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    document_id = {"first": 1, "second": 2}

    assert (
        backend.find_exact_physical_id(
            "items",
            document_id,
            sharded_sqlite._physical_id_key(document_id),
        )
        is None
    )


def test_exact_id_count_uses_the_single_shard_point_read(tmp_path, monkeypatch):
    client = _open_client(tmp_path / "exact-id-count")
    database = client.app
    collection = database.items
    collection.insert_one({"_id": "target", "kind": "wanted"})

    def reject_generic_count(*_args, **_kwargs):
        raise AssertionError("exact-id count used a generic shard count")

    for shard in database.engine._shards:
        monkeypatch.setattr(shard, "count_documents", reject_generic_count)

    assert collection.count_documents({"_id": "target"}) == 1
    assert collection.count_documents({"_id": "missing"}) == 0
    client.close()


def test_manifest_connect_rejects_simulated_forked_owner(tmp_path, monkeypatch):
    client = _open_client(tmp_path / "owner-pid")
    engine = client.app.engine

    with monkeypatch.context() as patch:
        patch.setattr(sharded_sqlite, "_PROCESS_ID", lambda: engine._owner_pid + 1)
        with pytest.raises(ConfigurationError, match="cannot be reused after os.fork"):
            engine._manifest_connect()

    client.close()


def test_manifest_pid_sync_without_an_inherited_connection_clears_cache(tmp_path):
    client = _open_client(tmp_path / "manifest-pid-no-connection")
    engine = client.app.engine
    if engine._manifest_read_conn is not None:
        engine._manifest_read_conn.close()
    engine._manifest_read_conn = None
    engine._manifest_read_pid = os.getpid() + 1
    engine._manifest_catalog_cache["stale"] = ("token", "catalog")
    old_epoch = engine._manifest_read_epoch

    assert engine._synchronize_manifest_read_pid() == os.getpid()
    assert engine._manifest_read_conn is None
    assert engine._manifest_catalog_cache == {}
    assert engine._manifest_read_epoch == old_epoch + 1
    client.close()


@pytest.mark.parametrize("row", [None, (), (True,)])
def test_manifest_data_version_rejects_unavailable_values(row):
    class Result:
        def fetchone(self):
            return row

    class Connection:
        def execute(self, statement):
            assert statement == "PRAGMA data_version"
            return Result()

    with pytest.raises(StorageCorruptionError, match="generation is unavailable"):
        sharded_sqlite.ShardedSQLiteTableBackend._manifest_data_version(Connection())


@pytest.mark.parametrize("value", ["invalid", object()])
def test_manifest_data_version_rejects_non_integer_values(value):
    class Result:
        def fetchone(self):
            return (value,)

    class Connection:
        def execute(self, statement):
            assert statement == "PRAGMA data_version"
            return Result()

    with pytest.raises(StorageCorruptionError, match="generation is invalid"):
        sharded_sqlite.ShardedSQLiteTableBackend._manifest_data_version(Connection())


def test_changed_generation_wraps_an_unavailable_manifest_identity(tmp_path):
    client = _open_client(tmp_path / "generation-identity-unavailable")
    engine = client.app.engine

    class Result:
        def fetchone(self):
            return (999,)

    class Connection:
        def execute(self, statement):
            if statement == "PRAGMA data_version":
                return Result()
            raise sqlite3.OperationalError("identity table unavailable")

    with pytest.raises(StorageCorruptionError, match="identity is unavailable"):
        engine._validated_manifest_generation(Connection())
    client.close()


def test_manifest_cache_retries_one_generation_change(tmp_path, monkeypatch):
    client = _open_client(tmp_path / "cache-retry")
    engine = client.app.engine
    versions = iter([1, 2, 2, 2])
    loads = []
    monkeypatch.setattr(
        engine,
        "_manifest_data_version",
        lambda _connection: next(versions),
    )
    monkeypatch.setattr(
        engine,
        "_load_manifest_catalog_rows",
        lambda _connection, collection: loads.append(collection)
        or [(None, None, None, None)],
    )

    _token, catalog = engine._cached_manifest_catalog("changing")

    assert catalog == (False, [])
    assert loads == ["changing", "changing"]
    client.close()


def test_manifest_cache_rejects_repeated_generation_changes(tmp_path, monkeypatch):
    client = _open_client(tmp_path / "cache-exhausted")
    engine = client.app.engine
    versions = iter([1, 2, 3, 4, 5, 6])
    monkeypatch.setattr(
        engine,
        "_manifest_data_version",
        lambda _connection: next(versions),
    )
    monkeypatch.setattr(
        engine,
        "_load_manifest_catalog_rows",
        lambda _connection, _collection: [(None, None, None, None)],
    )

    with pytest.raises(StorageCorruptionError, match="changed repeatedly"):
        engine._cached_manifest_catalog("changing")
    client.close()


def test_collection_read_generation_returns_none_when_creation_stays_missing(
    tmp_path,
    monkeypatch,
):
    client = _open_client(tmp_path / "generation-missing")
    engine = client.app.engine
    monkeypatch.setattr(
        engine,
        "_cached_manifest_catalog",
        lambda _collection: ((1, 1), (False, [])),
    )
    monkeypatch.setattr(engine, "create_collection", lambda _collection: None)

    assert engine._collection_read_generation("items") is None
    client.close()


def test_exact_id_read_exhausts_missing_generation_retries(tmp_path, monkeypatch):
    client = _open_client(tmp_path / "exact-generation-missing")
    engine = client.app.engine
    monkeypatch.setattr(engine, "_collection_read_generation", lambda _name: None)

    with pytest.raises(StorageCorruptionError, match="exact-ID read"):
        engine.find_one_exact_id("items", "target")
    client.close()


@pytest.mark.parametrize("operation", ["find", "count_documents"])
def test_collection_reads_continue_after_one_missing_generation(
    tmp_path,
    monkeypatch,
    operation,
):
    client = _open_client(tmp_path / (operation + "-generation-retry"))
    engine = client.app.engine
    engine.create_collection("items")
    generations = iter([None, (1, 1)])
    monkeypatch.setattr(
        engine,
        "_collection_read_generation",
        lambda _name: next(generations),
    )
    monkeypatch.setattr(engine, "_manifest_generation_matches", lambda _token: True)

    result = getattr(engine, operation)("items", {})

    assert result == ([] if operation == "find" else 0)
    client.close()
