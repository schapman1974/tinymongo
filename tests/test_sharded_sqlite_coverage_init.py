from contextlib import contextmanager
import os
import shutil
import threading

import pytest

from tinymongo.errors import (
    ConfigurationError,
    OperationFailure,
    StorageCorruptionError,
)
from tinymongo.indexes import IndexSpec
from tinymongo import sharded_sqlite
from tinymongo.sharded_sqlite import (
    ShardedSQLiteTableBackend,
    _ShardSQLiteTableBackend,
)
from tinymongo.table_backends import SQLiteTableBackend


def _backend(tmp_path, name="database"):
    return ShardedSQLiteTableBackend(
        str(tmp_path / name),
        sqlite_shards=2,
    )


def _id_for_shard(backend, target):
    for candidate in range(10_000):
        document_id = "shard-{0}-{1}".format(target, candidate)
        if backend._shard_index(document_id) == target:
            return document_id
    raise AssertionError("could not find an id for shard {0}".format(target))


def _write_manifest_config(directory, rows):
    directory.mkdir()
    conn = sharded_sqlite.sqlite3.connect(str(directory / "manifest.sqlite"))
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("PRAGMA user_version={0}".format(sharded_sqlite._FORMAT_VERSION))
        conn.execute(
            "CREATE TABLE __tinymongo_config ("
            "format_version INTEGER NOT NULL, "
            "shard_count INTEGER NOT NULL, "
            "hash_algorithm TEXT NOT NULL, "
            "database_id TEXT NOT NULL, "
            "state TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO __tinymongo_config VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class _ExecuteFailingConnection:
    """Delegate to SQLite while injecting one deterministic statement failure."""

    def __init__(self, connection, should_fail):
        self._connection = connection
        self._should_fail = should_fail

    def execute(self, statement, parameters=()):
        if self._should_fail(statement):
            raise OSError("injected SQLite statement failure")
        return self._connection.execute(statement, parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.mark.parametrize(
    ("row", "reported_mode"),
    [
        (None, "''"),
        ((), "''"),
        (("delete",), "'delete'"),
    ],
)
def test_require_wal_fails_closed_for_missing_empty_and_non_wal_rows(
    row,
    reported_mode,
):
    class Result:
        def fetchone(self):
            return row

    class Connection:
        def execute(self, statement):
            assert statement == "PRAGMA journal_mode"
            return Result()

    with pytest.raises(ConfigurationError, match="requires WAL") as caught:
        sharded_sqlite._require_wal(Connection(), "unusable.sqlite")

    assert reported_mode in str(caught.value)
    assert "unusable.sqlite" in str(caught.value)


def test_shard_collection_creation_rechecks_order_column_cache(tmp_path, monkeypatch):
    backend = _ShardSQLiteTableBackend(str(tmp_path / "shard.sqlite"))
    backend._ensure_sqlite_initialized()
    SQLiteTableBackend.create_collection(backend, "items")
    backend._ready_order_collections.clear()

    @contextmanager
    def collection_created_while_waiting():
        backend._ready_order_collections.add("items")
        yield

    monkeypatch.setattr(backend, "_write_lock", collection_created_while_waiting)

    backend.create_collection("items")

    assert backend._ready_order_collections == {"items"}


def test_sharded_backend_rejects_a_non_threadsafe_sqlite_runtime(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(sharded_sqlite.sqlite3, "threadsafety", 0)

    with pytest.raises(ConfigurationError, match="thread-safe SQLite runtime"):
        ShardedSQLiteTableBackend(str(tmp_path / "database"), sqlite_shards=2)


def test_sharded_backend_rejects_a_file_as_its_database_directory(tmp_path):
    path = tmp_path / "database"
    path.write_text("not a directory", encoding="utf8")

    with pytest.raises(ConfigurationError, match="database directory, not a file"):
        ShardedSQLiteTableBackend(str(path), sqlite_shards=2)


def test_new_sharded_directory_rejects_unclaimed_existing_contents(tmp_path):
    path = tmp_path / "database"
    path.mkdir()
    (path / "unrelated.txt").write_text("keep me", encoding="utf8")

    with pytest.raises(StorageCorruptionError, match="no manifest but is not empty"):
        ShardedSQLiteTableBackend(str(path), sqlite_shards=2)


def test_manifest_rejects_multiple_configuration_rows(tmp_path):
    path = tmp_path / "multiple-configurations"
    valid = (
        sharded_sqlite._FORMAT_VERSION,
        2,
        sharded_sqlite._HASH_ALGORITHM,
        "database-id",
        "ready",
    )
    _write_manifest_config(path, [valid, valid])

    with pytest.raises(StorageCorruptionError, match="multiple configurations"):
        ShardedSQLiteTableBackend(str(path))


@pytest.mark.parametrize(
    "config",
    [
        (2, 2, sharded_sqlite._HASH_ALGORITHM, "database-id", "ready"),
        (
            sharded_sqlite._FORMAT_VERSION,
            2,
            "unknown-hash",
            "database-id",
            "ready",
        ),
        (
            sharded_sqlite._FORMAT_VERSION,
            1,
            sharded_sqlite._HASH_ALGORITHM,
            "database-id",
            "ready",
        ),
        (
            sharded_sqlite._FORMAT_VERSION,
            65,
            sharded_sqlite._HASH_ALGORITHM,
            "database-id",
            "ready",
        ),
        (
            sharded_sqlite._FORMAT_VERSION,
            2,
            sharded_sqlite._HASH_ALGORITHM,
            "",
            "ready",
        ),
        (
            sharded_sqlite._FORMAT_VERSION,
            2,
            sharded_sqlite._HASH_ALGORITHM,
            sharded_sqlite.sqlite3.Binary(b"database-id"),
            "ready",
        ),
        (
            sharded_sqlite._FORMAT_VERSION,
            2,
            sharded_sqlite._HASH_ALGORITHM,
            "database-id",
            "unknown-state",
        ),
    ],
    ids=[
        "format",
        "hash",
        "too-few-shards",
        "too-many-shards",
        "empty-database-id",
        "non-text-database-id",
        "state",
    ],
)
def test_manifest_rejects_unsupported_or_corrupt_configuration(tmp_path, config):
    path = tmp_path / "corrupt-{0}".format(config[1])
    _write_manifest_config(path, [config])

    with pytest.raises(StorageCorruptionError, match="Unsupported or corrupt"):
        ShardedSQLiteTableBackend(str(path))


def test_manifest_rejects_and_preserves_an_unsupported_sqlite_user_version(tmp_path):
    path = tmp_path / "future-manifest"
    config = (
        sharded_sqlite._FORMAT_VERSION,
        2,
        sharded_sqlite._HASH_ALGORITHM,
        "database-id",
        "ready",
    )
    _write_manifest_config(path, [config])
    manifest_path = path / "manifest.sqlite"
    conn = sharded_sqlite.sqlite3.connect(str(manifest_path))
    try:
        conn.execute("PRAGMA user_version=99")
    finally:
        conn.close()

    with pytest.raises(StorageCorruptionError, match="manifest format 99"):
        ShardedSQLiteTableBackend(str(path))

    conn = sharded_sqlite.sqlite3.connect(str(manifest_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (99,)
    finally:
        conn.close()


def test_ready_manifest_rejects_a_missing_shard_file(tmp_path):
    backend = _backend(tmp_path, "missing-shard")
    path = backend.path
    missing_shard = backend._shards[1].path
    backend.close()
    os.remove(missing_shard)

    with pytest.raises(StorageCorruptionError, match="missing required shard"):
        ShardedSQLiteTableBackend(path, sqlite_shards=2)


def test_initializing_manifest_recreates_a_missing_shard_and_becomes_ready(tmp_path):
    backend = _backend(tmp_path, "resume-initialization")
    path = backend.path
    manifest_path = backend._manifest_path
    database_id = backend.database_id
    missing_shard = backend._shards[1].path
    backend.close()

    conn = sharded_sqlite.sqlite3.connect(manifest_path)
    try:
        conn.execute("UPDATE __tinymongo_config SET state = 'initializing'")
        conn.commit()
    finally:
        conn.close()
    os.remove(missing_shard)

    reopened = ShardedSQLiteTableBackend(path, sqlite_shards=2)
    try:
        conn = sharded_sqlite.sqlite3.connect(manifest_path)
        try:
            config = conn.execute(
                "SELECT database_id, state FROM __tinymongo_config"
            ).fetchone()
        finally:
            conn.close()
        shard_conn = sharded_sqlite.sqlite3.connect(missing_shard)
        try:
            identity = shard_conn.execute(
                "SELECT database_id, shard_index, shard_count " "FROM __tinymongo_shard"
            ).fetchone()
        finally:
            shard_conn.close()

        assert config == (database_id, "ready")
        assert identity == (database_id, 1, 2)
    finally:
        reopened.close()


def test_ready_manifest_rejects_a_shard_without_identity_metadata(tmp_path):
    backend = _backend(tmp_path, "missing-identity")
    path = backend.path
    shard_path = backend._shards[0].path
    backend.close()
    conn = sharded_sqlite.sqlite3.connect(shard_path)
    try:
        conn.execute("DROP TABLE __tinymongo_shard")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StorageCorruptionError, match="no identity metadata"):
        ShardedSQLiteTableBackend(path, sqlite_shards=2)


def test_reopen_rejects_swapped_shard_files(tmp_path):
    backend = _backend(tmp_path, "swapped-identity")
    path = backend.path
    first = backend._shards[0].path
    second = backend._shards[1].path
    temporary = str(tmp_path / "shard-swap.sqlite")
    backend.close()
    shutil.move(first, temporary)
    shutil.move(second, first)
    shutil.move(temporary, second)

    with pytest.raises(StorageCorruptionError, match="identity mismatch for shard 0"):
        ShardedSQLiteTableBackend(path, sqlite_shards=2)


@pytest.mark.parametrize("damage", ["empty", "duplicate"])
def test_reopen_rejects_corrupt_shard_identity_rows(tmp_path, damage):
    backend = _backend(tmp_path, "corrupt-identity-{0}".format(damage))
    path = backend.path
    shard_path = backend._shards[0].path
    backend.close()
    conn = sharded_sqlite.sqlite3.connect(shard_path)
    try:
        if damage == "empty":
            conn.execute("DELETE FROM __tinymongo_shard")
        else:
            conn.execute(
                "INSERT INTO __tinymongo_shard " "SELECT * FROM __tinymongo_shard"
            )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StorageCorruptionError, match="identity mismatch"):
        ShardedSQLiteTableBackend(path, sqlite_shards=2)


def test_mark_ready_rejects_a_manifest_identity_change(tmp_path):
    backend = _backend(tmp_path, "changed-manifest-identity")
    original_database_id = backend.database_id
    backend.database_id = "a-different-database-id"

    with pytest.raises(StorageCorruptionError, match="identity changed during startup"):
        backend._mark_manifest_ready()

    conn = sharded_sqlite.sqlite3.connect(backend._manifest_path)
    try:
        config = conn.execute(
            "SELECT database_id, state FROM __tinymongo_config"
        ).fetchone()
    finally:
        conn.close()
    assert config == (original_database_id, "ready")


def test_write_shards_retries_when_a_unique_index_appears_while_locking(
    tmp_path,
    monkeypatch,
):
    backend = _backend(tmp_path)
    backend.create_collection("users")
    unique = IndexSpec("email", name="unique_email", unique=True)
    catalog_reads = iter(
        [
            (True, [(unique, "ready")]),
            (True, [(unique, "ready")]),
        ]
    )
    reads = []

    def changing_catalog(collection):
        assert collection == "users"
        reads.append(collection)
        return next(catalog_reads)

    monkeypatch.setattr(backend, "_manifest_catalog", changing_catalog)

    with backend._write_shards([0], collection="users") as locked_specs:
        assert locked_specs == [unique]

    assert reads == ["users"] * 2


def test_reopen_rejects_cross_shard_conflicts_in_pending_unique_index(tmp_path):
    backend = _backend(tmp_path)
    backend.create_collection("users")
    first_id = _id_for_shard(backend, 0)
    second_id = _id_for_shard(backend, 1)
    backend._shards[0].insert_many(
        "users",
        [{"_id": first_id, "email": "same@example.com"}],
    )
    backend._shards[1].insert_many(
        "users",
        [{"_id": second_id, "email": "same@example.com"}],
    )
    backend._store_manifest_index(
        "users",
        IndexSpec("email", name="unique_email", unique=True),
        "pending",
    )
    path = backend.path
    backend.close()

    with pytest.raises(
        StorageCorruptionError, match="documents conflict across shards"
    ):
        ShardedSQLiteTableBackend(path, sqlite_shards=2)


def test_collection_creation_rechecks_ready_cache_after_taking_shard_locks(
    tmp_path,
    monkeypatch,
):
    backend = _backend(tmp_path)

    @contextmanager
    def collection_created_while_waiting(*args, **kwargs):
        backend._ready_collections.add("items")
        yield []

    monkeypatch.setattr(backend, "_write_shards", collection_created_while_waiting)

    backend.create_collection("items")

    assert backend._ready_collections == {"items"}


def test_drop_missing_collection_returns_false(tmp_path):
    backend = _backend(tmp_path)

    assert backend.drop_collection("missing") is False


def test_interrupted_collection_drop_is_tombstoned_and_finished_on_reopen(
    tmp_path,
    monkeypatch,
):
    backend = _backend(tmp_path, "interrupted-collection-drop")
    backend.insert_many(
        "items",
        [
            {"_id": _id_for_shard(backend, 0), "value": 0},
            {"_id": _id_for_shard(backend, 1), "value": 1},
        ],
    )
    path = backend.path
    original_drop = backend._shards[1].drop_collection
    failed_once = [False]

    def fail_second_shard_once(collection):
        if not failed_once[0]:
            failed_once[0] = True
            raise OSError("simulated shard drop failure")
        return original_drop(collection)

    monkeypatch.setattr(
        backend._shards[1],
        "drop_collection",
        fail_second_shard_once,
    )

    with pytest.raises(StorageCorruptionError, match="could not finish dropping"):
        backend.drop_collection("items")
    assert backend.list_collections() == []
    with pytest.raises(StorageCorruptionError, match="interrupted drop"):
        backend.find("items", {})
    backend.close()

    reopened = ShardedSQLiteTableBackend(path, sqlite_shards=2)
    try:
        assert reopened.list_collections() == []
        assert all(
            "items" not in shard.list_collections() for shard in reopened._shards
        )
    finally:
        reopened.close()


def test_create_index_retry_is_idempotent(tmp_path):
    backend = _backend(tmp_path)
    spec = IndexSpec("status", name="status_lookup")

    assert backend.create_index("items", spec) == "status_lookup"
    assert backend.create_index("items", spec) == "status_lookup"
    assert [item.name for item in backend.get_index_specs("items")] == ["status_lookup"]


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_create_index_failure_removes_pending_metadata_and_rolls_back_children(
    tmp_path,
    monkeypatch,
    rollback_fails,
):
    backend = _backend(tmp_path, "rollback-{0}".format(rollback_fails))
    backend.create_collection("items")
    spec = IndexSpec("status", name="status_lookup")
    rolled_back = []

    monkeypatch.setattr(
        backend._shards[0],
        "create_index",
        lambda collection, item: item.name,
    )

    def fail_creation(collection, item):
        raise RuntimeError("second shard rejected index")

    monkeypatch.setattr(backend._shards[1], "create_index", fail_creation)

    def rollback(collection, name):
        rolled_back.append((collection, name))
        if rollback_fails:
            raise RuntimeError("rollback also failed")

    monkeypatch.setattr(backend._shards[0], "drop_index", rollback)

    expected_error = StorageCorruptionError if rollback_fails else RuntimeError
    expected_message = (
        "could not fully roll back" if rollback_fails else "second shard rejected index"
    )
    with pytest.raises(expected_error, match=expected_message):
        backend.create_index("items", spec)

    assert rolled_back == [("items", "status_lookup")]
    assert backend.get_index_specs("items") == ([spec] if rollback_fails else [])


def test_sqlite_child_create_index_rolls_back_native_ddl_with_catalog_failure(
    tmp_path,
    monkeypatch,
):
    backend = _ShardSQLiteTableBackend(str(tmp_path / "create-index.sqlite"))
    backend.create_collection("items")
    spec = IndexSpec("email", name="email_unique", unique=True)
    original_connect = backend._connect

    def connect_with_catalog_failure():
        return _ExecuteFailingConnection(
            original_connect(),
            lambda statement: statement.startswith("INSERT INTO")
            and "__tinymongo_indexes" in statement,
        )

    with monkeypatch.context() as patch:
        patch.setattr(backend, "_connect", connect_with_catalog_failure)
        with pytest.raises(OSError, match="injected SQLite statement failure"):
            backend.create_index("items", spec)

    physical_name = backend._physical_index_name("items", spec)
    conn = backend._connect()
    try:
        native_indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert backend.get_index_specs("items") == []
    assert not {
        physical_name,
        physical_name + "_lookup",
        backend._type_index_name("items", spec),
    }.intersection(native_indexes)


def test_drop_missing_index_uses_mongodb_error_code(tmp_path):
    backend = _backend(tmp_path)
    backend.create_collection("items")

    with pytest.raises(OperationFailure, match="Index not found") as caught:
        backend.drop_index("items", "missing")

    assert caught.value.code == 27


def test_drop_index_tolerates_a_child_where_the_index_is_already_missing(tmp_path):
    backend = _backend(tmp_path)
    spec = IndexSpec("status", name="status_lookup")
    backend.create_index("items", spec)
    backend._shards[0].drop_index("items", spec.name)

    backend.drop_index("items", spec.name)

    assert backend.get_index_specs("items") == []
    assert all(shard.get_index_specs("items") == [] for shard in backend._shards)


def test_drop_index_failure_restores_earlier_children_and_keeps_manifest(
    tmp_path,
    monkeypatch,
):
    backend = _backend(tmp_path)
    spec = IndexSpec("status", name="status_lookup")
    backend.create_index("items", spec)
    first, second = backend._shards
    original_first_create = first.create_index
    recreate_attempts = []

    def fail_later_drop(collection, name):
        raise RuntimeError("later shard rejected index drop")

    monkeypatch.setattr(second, "drop_index", fail_later_drop)

    def recreate_then_report_failure(collection, item):
        recreate_attempts.append((collection, item.name))
        original_first_create(collection, item)
        raise RuntimeError("recreate reported a failure after restoring the index")

    monkeypatch.setattr(first, "create_index", recreate_then_report_failure)

    with pytest.raises(StorageCorruptionError, match="could not restore index"):
        backend.drop_index("items", spec.name)

    assert recreate_attempts == [("items", "status_lookup")]
    assert backend.get_index_specs("items") == [spec]
    assert all(shard.get_index_specs("items") == [spec] for shard in backend._shards)


def test_sqlite_child_drop_index_rolls_back_partially_executed_native_ddl(
    tmp_path,
    monkeypatch,
):
    path = str(tmp_path / "drop-index.sqlite")
    backend = _ShardSQLiteTableBackend(path)
    backend.create_collection("items")
    spec = IndexSpec("email", name="email_unique", unique=True)
    backend.create_index("items", spec)
    original_connect = backend._connect
    drop_count = 0

    def fail_second_drop(statement):
        nonlocal drop_count
        if not statement.startswith("DROP INDEX"):
            return False
        drop_count += 1
        return drop_count == 2

    def connect_with_drop_failure():
        return _ExecuteFailingConnection(original_connect(), fail_second_drop)

    with monkeypatch.context() as patch:
        patch.setattr(backend, "_connect", connect_with_drop_failure)
        with pytest.raises(OSError, match="injected SQLite statement failure"):
            backend.drop_index("items", spec.name)

    physical_name = backend._physical_index_name("items", spec)
    conn = backend._connect()
    try:
        native_indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert backend.get_index_specs("items") == [spec]
    assert {
        physical_name,
        physical_name + "_lookup",
        backend._type_index_name("items", spec),
    } <= native_indexes

    reopened = _ShardSQLiteTableBackend(path)
    assert reopened.get_index_specs("items") == [spec]


@pytest.mark.parametrize(
    ("state", "spec_json", "message"),
    [
        ("unknown", None, "Unsupported Sharded SQLite index state"),
        ("ready", '{"not": "index metadata"}', "Corrupt Sharded SQLite index"),
    ],
    ids=["state", "metadata"],
)
def test_reopen_rejects_invalid_manifest_index_rows(
    tmp_path,
    state,
    spec_json,
    message,
):
    backend = _backend(tmp_path, "invalid-index-{0}".format(state))
    spec = IndexSpec("status", name="status_lookup")
    backend.create_index("items", spec)
    manifest_path = backend._manifest_path
    path = backend.path
    backend.close()

    conn = sharded_sqlite.sqlite3.connect(manifest_path)
    try:
        if spec_json is None:
            conn.execute(
                "UPDATE __tinymongo_indexes SET state = ? "
                "WHERE collection_name = ? AND index_name = ?",
                (state, "items", spec.name),
            )
        else:
            conn.execute(
                "UPDATE __tinymongo_indexes SET spec_json = ?, state = ? "
                "WHERE collection_name = ? AND index_name = ?",
                (spec_json, state, "items", spec.name),
            )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StorageCorruptionError, match=message):
        ShardedSQLiteTableBackend(path, sqlite_shards=2)


def test_reopen_rejects_an_index_row_for_a_missing_collection(tmp_path):
    backend = _backend(tmp_path, "orphan-index")
    backend._store_manifest_index(
        "missing",
        IndexSpec("status", name="status_lookup"),
        "ready",
    )
    with pytest.raises(StorageCorruptionError, match="indexes for missing collection"):
        backend._manifest_catalog("missing")
    path = backend.path
    backend.close()

    with pytest.raises(StorageCorruptionError, match="indexes for missing collection"):
        ShardedSQLiteTableBackend(path, sqlite_shards=2)


def test_reopen_rejects_an_unexpected_native_tinymongo_index(tmp_path):
    backend = _backend(tmp_path, "orphan-native-index")
    backend.create_collection("items")
    shard = backend._shards[0]
    conn = shard._connect()
    try:
        conn.execute(
            'CREATE UNIQUE INDEX "__tm_idx_ffffffffffffffffffffffffffffffff" '
            'ON "items" (_id)'
        )
        conn.commit()
    finally:
        conn.close()
    path = backend.path
    backend.close()

    with pytest.raises(StorageCorruptionError, match="unexpected physical index"):
        ShardedSQLiteTableBackend(path, sqlite_shards=2)


def test_ready_unique_index_reopens_without_document_wide_revalidation(
    tmp_path,
    monkeypatch,
):
    backend = _backend(tmp_path, "ready-unique")
    spec = IndexSpec("email", name="unique_email", unique=True)
    backend.create_index("users", spec)
    backend.insert_one_with_result(
        "users",
        {"_id": "owner", "email": "owner@example.com"},
    )
    path = backend.path
    backend.close()

    def reject_revalidation(*args, **kwargs):
        raise AssertionError("READY indexes must not rescan every document")

    monkeypatch.setattr(
        sharded_sqlite,
        "validate_unique_documents",
        reject_revalidation,
    )

    reopened = ShardedSQLiteTableBackend(path, sqlite_shards=2)
    try:
        assert reopened.get_index_specs("users") == [spec]
        assert reopened.find("users", {"_id": "owner"}) == [
            {"_id": "owner", "email": "owner@example.com"}
        ]
    finally:
        reopened.close()


def test_reopen_rejects_a_manifest_collection_missing_from_one_shard(tmp_path):
    backend = _backend(tmp_path, "missing-collection")
    backend.create_collection("items")
    path = backend.path
    shard_path = backend._shards[1].path
    backend.close()
    conn = sharded_sqlite.sqlite3.connect(shard_path)
    try:
        conn.execute('DROP TABLE "items"')
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StorageCorruptionError, match=r"missing from shard\(s\) 1"):
        ShardedSQLiteTableBackend(path, sqlite_shards=2)


def test_reopen_rejects_a_collection_without_the_order_column(tmp_path):
    backend = _backend(tmp_path, "missing-order-column")
    backend.create_collection("items")
    path = backend.path
    shard_path = backend._shards[0].path
    backend.close()
    conn = sharded_sqlite.sqlite3.connect(shard_path)
    try:
        conn.execute(
            'ALTER TABLE "items" DROP COLUMN "{0}"'.format(sharded_sqlite._ORDER_COLUMN)
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StorageCorruptionError, match="collection schema mismatch"):
        ShardedSQLiteTableBackend(path, sqlite_shards=2)


def test_reopen_rejects_a_missing_child_index_catalog_entry(tmp_path):
    backend = _backend(tmp_path, "missing-child-catalog")
    spec = IndexSpec("status", name="status_lookup")
    backend.create_index("items", spec)
    path = backend.path
    shard_path = backend._shards[0].path
    backend.close()
    conn = sharded_sqlite.sqlite3.connect(shard_path)
    try:
        conn.execute(
            "DELETE FROM __tinymongo_indexes "
            "WHERE collection_name = ? AND index_name = ?",
            ("items", spec.name),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StorageCorruptionError, match="index catalog mismatch"):
        ShardedSQLiteTableBackend(path, sqlite_shards=2)


def test_reopen_rejects_a_missing_native_physical_index(tmp_path):
    backend = _backend(tmp_path, "missing-physical-index")
    spec = IndexSpec("status", name="status_lookup")
    backend.create_index("items", spec)
    path = backend.path
    shard = backend._shards[0]
    shard_path = shard.path
    physical_name = shard._physical_index_name("items", spec)
    backend.close()
    conn = sharded_sqlite.sqlite3.connect(shard_path)
    try:
        conn.execute('DROP INDEX "{0}"'.format(physical_name))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StorageCorruptionError, match="physical index mismatch"):
        ShardedSQLiteTableBackend(path, sqlite_shards=2)


def test_pending_index_is_completed_and_marked_ready_on_reopen(tmp_path):
    backend = _backend(tmp_path, "pending-index")
    spec = IndexSpec("status", name="status_lookup")
    backend.create_collection("items")
    backend._store_manifest_index("items", spec, "pending")
    path = backend.path
    manifest_path = backend._manifest_path
    backend.close()

    reopened = ShardedSQLiteTableBackend(path, sqlite_shards=2)
    try:
        conn = sharded_sqlite.sqlite3.connect(manifest_path)
        try:
            state = conn.execute(
                "SELECT state FROM __tinymongo_indexes "
                "WHERE collection_name = ? AND index_name = ?",
                ("items", spec.name),
            ).fetchone()
        finally:
            conn.close()

        assert state == ("ready",)
        assert all(
            shard.get_index_specs("items") == [spec] for shard in reopened._shards
        )
    finally:
        reopened.close()


def test_failed_drop_retains_pending_state_and_recovers_on_reopen(
    tmp_path,
    monkeypatch,
):
    backend = _backend(tmp_path, "failed-drop-recovery")
    spec = IndexSpec("status", name="status_lookup")
    backend.create_index("items", spec)
    first, second = backend._shards
    path = backend.path
    manifest_path = backend._manifest_path

    with monkeypatch.context() as patch:

        def fail_later_drop(collection, name):
            raise RuntimeError("later shard rejected index drop")

        def fail_rollback(collection, item):
            raise RuntimeError("earlier shard could not restore index")

        patch.setattr(second, "drop_index", fail_later_drop)
        patch.setattr(first, "create_index", fail_rollback)

        with pytest.raises(StorageCorruptionError, match="could not restore index"):
            backend.drop_index("items", spec.name)

    conn = sharded_sqlite.sqlite3.connect(manifest_path)
    try:
        state = conn.execute(
            "SELECT state FROM __tinymongo_indexes "
            "WHERE collection_name = ? AND index_name = ?",
            ("items", spec.name),
        ).fetchone()
    finally:
        conn.close()
    assert state == ("pending",)
    backend.close()

    reopened = ShardedSQLiteTableBackend(path, sqlite_shards=2)
    try:
        conn = sharded_sqlite.sqlite3.connect(manifest_path)
        try:
            recovered_state = conn.execute(
                "SELECT state FROM __tinymongo_indexes "
                "WHERE collection_name = ? AND index_name = ?",
                ("items", spec.name),
            ).fetchone()
        finally:
            conn.close()

        assert recovered_state == ("ready",)
        assert all(
            shard.get_index_specs("items") == [spec] for shard in reopened._shards
        )
    finally:
        reopened.close()


def test_manifest_read_connection_reopens_after_close_and_on_pid_change(
    tmp_path,
    monkeypatch,
):
    backend = _backend(tmp_path, "manifest-read-connection")
    original = backend._manifest_read_conn
    assert original is not None

    backend.close()
    assert backend._manifest_read_conn is None
    assert backend.list_collections() == []
    reopened = backend._manifest_read_conn
    assert reopened is not None
    assert reopened is not original

    original_pid = backend._manifest_read_pid
    monkeypatch.setattr(sharded_sqlite.os, "getpid", lambda: original_pid + 1)
    assert backend.list_collections() == []
    after_pid_change = backend._manifest_read_conn
    assert after_pid_change is not None
    assert after_pid_change is not reopened
    assert backend._manifest_read_pid == original_pid + 1
    with pytest.raises(
        sharded_sqlite.sqlite3.ProgrammingError, match="closed database"
    ):
        reopened.execute("SELECT 1")
    backend.close()
    backend.close()


def test_closed_backend_does_not_recreate_a_missing_manifest(tmp_path):
    backend = _backend(tmp_path, "missing-manifest-after-close")
    manifest_path = backend._manifest_path
    backend.close()
    os.remove(manifest_path)

    with pytest.raises(StorageCorruptionError, match="manifest disappeared"):
        backend.list_collections()

    assert not os.path.exists(manifest_path)


def test_manifest_catalog_is_one_snapshot_when_an_index_is_created_mid_read(tmp_path):
    path = tmp_path / "manifest-snapshot"
    reader = ShardedSQLiteTableBackend(str(path), sqlite_shards=2)
    writer = ShardedSQLiteTableBackend(str(path), sqlite_shards=2)
    spec = IndexSpec("status", name="status_lookup")
    paused = threading.Event()
    resume = threading.Event()
    outcome = {}
    paused_once = [False]
    raw_connection = reader._manifest_read_conn
    assert raw_connection is not None

    def pause_after_result_is_read():
        if paused_once[0]:
            return
        paused_once[0] = True
        paused.set()
        if not resume.wait(10):
            raise RuntimeError("timed out waiting for the manifest writer")

    class PausingCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchone(self):
            row = self._cursor.fetchone()
            pause_after_result_is_read()
            return row

        def fetchall(self):
            rows = self._cursor.fetchall()
            pause_after_result_is_read()
            return rows

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class PausingConnection:
        def execute(self, *args, **kwargs):
            return PausingCursor(raw_connection.execute(*args, **kwargs))

        def __getattr__(self, name):
            return getattr(raw_connection, name)

    # A legacy two-SELECT implementation would pause after reading membership,
    # then observe B's new index in its second SELECT and falsely report an
    # orphan. The one-statement implementation has already read one coherent
    # result before this same logical pause point.
    reader._manifest_read_conn = PausingConnection()

    def read_catalog():
        try:
            outcome["catalog"] = reader._manifest_catalog("items")
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=read_catalog)
    try:
        thread.start()
        assert paused.wait(10), "catalog read did not reach its pause point"
        assert writer.create_index("items", spec) == spec.name
    finally:
        resume.set()
        thread.join(timeout=10)

    try:
        assert not thread.is_alive()
        assert "error" not in outcome
        assert outcome["catalog"] == (False, [])
        assert reader._manifest_catalog("items") == (True, [(spec, "ready")])
    finally:
        reader.close()
        writer.close()


def test_find_with_order_discards_a_row_that_stops_matching_before_reload(
    tmp_path,
    monkeypatch,
):
    backend = _ShardSQLiteTableBackend(str(tmp_path / "stale-filter.sqlite"))
    backend.insert_many("items", [{"_id": "one", "active": True}])
    original_find = SQLiteTableBackend.find

    def find_then_change_row(self, collection, filter_doc=None, **kwargs):
        documents = original_find(self, collection, filter_doc, **kwargs)
        conn = self._connect()
        try:
            conn.execute(
                'UPDATE "items" SET data = ?',
                (sharded_sqlite.bson_json_dumps({"_id": "one", "active": False}),),
            )
            conn.commit()
        finally:
            conn.close()
        return documents

    monkeypatch.setattr(SQLiteTableBackend, "find", find_then_change_row)

    assert backend.find_with_order("items", {"active": True}) == []


def test_manifest_read_wraps_an_unavailable_identity_table(tmp_path):
    backend = _backend(tmp_path, "unavailable-manifest-identity")
    manifest_path = backend._manifest_path
    backend.close()
    conn = sharded_sqlite.sqlite3.connect(manifest_path)
    try:
        conn.execute("DROP TABLE __tinymongo_config")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StorageCorruptionError, match="identity is unavailable"):
        backend.list_collections()

    assert backend._manifest_read_conn is None


def test_invalid_collection_state_fails_in_listing_and_catalog_reads(tmp_path):
    backend = _backend(tmp_path, "invalid-collection-state")
    backend.create_collection("items")
    conn = sharded_sqlite.sqlite3.connect(backend._manifest_path)
    try:
        conn.execute(
            "UPDATE __tinymongo_collections SET state = 'unknown' "
            "WHERE collection_name = 'items'"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StorageCorruptionError, match="collection state 'unknown'"):
        backend._manifest_collection_entries()
    with pytest.raises(StorageCorruptionError, match="collection state for"):
        backend._manifest_catalog("items")


def test_setting_state_for_missing_collection_fails_closed(tmp_path):
    backend = _backend(tmp_path, "missing-collection-state")

    with pytest.raises(StorageCorruptionError, match="metadata changed"):
        backend._set_manifest_collection_state("missing", "dropping")


def test_public_write_lock_and_write_shards_create_missing_collection(tmp_path):
    backend = _backend(tmp_path, "write-lock-creation")

    with backend._write_lock():
        assert backend.list_collections() == []

    with backend._write_shards([0], collection="items") as specs:
        assert specs == []

    assert backend.list_collections() == ["items"]
    assert all("items" in shard.list_collections() for shard in backend._shards)


def test_dotted_index_reopens_without_a_scalar_type_index(tmp_path):
    backend = _backend(tmp_path, "dotted-index")
    spec = IndexSpec("profile.status", name="profile_status")
    backend.create_index("items", spec)
    path = backend.path
    backend.close()

    reopened = ShardedSQLiteTableBackend(path, sqlite_shards=2)
    try:
        assert reopened.get_index_specs("items") == [spec]
    finally:
        reopened.close()


def test_create_collection_rechecks_manifest_after_taking_locks(
    tmp_path,
    monkeypatch,
):
    backend = _backend(tmp_path, "collection-create-race")

    @contextmanager
    def another_client_finishes_creation(*args, **kwargs):
        for shard in backend._shards:
            shard.create_collection("items")
        conn = backend._manifest_connect()
        try:
            conn.execute(
                "INSERT INTO __tinymongo_collections "
                "(collection_name, state) VALUES ('items', 'ready')"
            )
            conn.commit()
        finally:
            conn.close()
        yield []

    monkeypatch.setattr(
        backend,
        "_write_shards",
        another_client_finishes_creation,
    )

    backend.create_collection("items")

    assert backend._ready_collections == {"items"}


def test_drop_collection_rechecks_when_another_client_finishes_first(
    tmp_path,
    monkeypatch,
):
    backend = _backend(tmp_path, "collection-drop-race")
    backend.create_collection("items")

    @contextmanager
    def another_client_finishes_drop(*args, **kwargs):
        backend._set_manifest_collection_state("items", "dropping")
        backend._finish_collection_drop_locked("items")
        yield []

    monkeypatch.setattr(backend, "_write_shards", another_client_finishes_drop)

    assert backend.drop_collection("items") is False
    assert backend.list_collections() == []


def test_drop_collection_resumes_an_existing_dropping_state(tmp_path):
    backend = _backend(tmp_path, "resume-collection-drop")
    backend.create_collection("items")
    backend._set_manifest_collection_state("items", "dropping")

    assert backend.drop_collection("items") is True
    assert backend.list_collections() == []


def test_drop_index_reraises_original_error_after_successful_restore(
    tmp_path,
    monkeypatch,
):
    backend = _backend(tmp_path, "successful-drop-restore")
    spec = IndexSpec("status", name="status_lookup")
    backend.create_index("items", spec)

    def fail_later_drop(collection, name):
        raise RuntimeError("later shard rejected index drop")

    monkeypatch.setattr(backend._shards[1], "drop_index", fail_later_drop)

    with pytest.raises(RuntimeError, match="later shard rejected index drop"):
        backend.drop_index("items", spec.name)

    assert backend.get_index_specs("items") == [spec]
    assert all(shard.get_index_specs("items") == [spec] for shard in backend._shards)


@pytest.mark.parametrize("operation", ["find", "count_documents"])
@pytest.mark.parametrize("recovers", [True, False], ids=["recovers", "exhausted"])
def test_reads_retry_when_collection_metadata_changes(
    tmp_path,
    monkeypatch,
    operation,
    recovers,
):
    backend = _backend(
        tmp_path,
        "{0}-{1}".format(operation, "recovers" if recovers else "exhausted"),
    )
    backend.create_collection("items")
    generation_matches = iter([False, recovers])
    monkeypatch.setattr(
        backend,
        "_manifest_generation_matches",
        lambda token: next(generation_matches),
    )

    method = getattr(backend, operation)
    if recovers:
        assert method("items", {}) == ([] if operation == "find" else 0)
    else:
        message = "during read" if operation == "find" else "during count"
        with pytest.raises(StorageCorruptionError, match=message):
            method("items", {})
