"""Experimental striped SQLite storage for concurrent embedded workloads.

The backend presents one logical TinyMongo database while routing documents to
several independent SQLite database files.  SQLite WAL permits readers beside
one writer in each file; using separate files therefore permits independent
writers without a daemon, broker, or custom SQLite build.

This module deliberately composes :class:`SQLiteTableBackend` instances instead
of teaching that mature backend about multiple paths.  Each child lives in its
own directory, which also gives it an independent existing TinyMongo advisory
lock.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
import weakref
from pathlib import Path
from typing import Any
from uuid import uuid4
from collections import defaultdict
from contextlib import ExitStack, contextmanager

from .bson_codec import dumps as bson_json_dumps
from .bson_codec import loads as bson_json_loads
from .bson_codec import storage_values_equal
from .errors import (
    ConfigurationError,
    DuplicateKeyError,
    OperationFailure,
    StorageCorruptionError,
)
from .indexes import IndexSpec, validate_unique_documents
from .parquet_storage import _acquire_rlock, _local_rlocks, portalocker
from .table_backends import (
    SQLiteTableBackend,
    TableBackend,
    _MISSING,
    _SQLITE_CONFLICT_QUERY_SIZE,
    _direct_id_equality,
    _physical_id_candidates,
    _positive_filter_conjuncts,
    _requires_legacy_id_scan,
    _restore_legacy_document_id,
    matches_filter,
    _physical_id_key,
    _quote_identifier,
)


_FORMAT_VERSION = 1
_DEFAULT_SHARD_COUNT = 4
_MIN_SHARD_COUNT = 2
_MAX_SHARD_COUNT = 64
_HASH_ALGORITHM = "physical-id-sha256-mod-v1"
_ORDER_COLUMN = "__tinymongo_order"
_ORDER_LOCK = threading.Lock()
_ORDER_LAST_NS = 0
_ORDER_PROCESS_NONCE = uuid4().hex[:12]
_POINT_READ_MAX_IDLE = 4
_ATTACHED_READ_MAX_IDLE = 4
_DEFAULT_SQLITE_MAX_ATTACHED = 10
_PROCESS_ID = os.getpid
_FORK_BACKENDS: weakref.WeakSet[Any] = weakref.WeakSet()
_FORK_BACKENDS_LOCK = threading.RLock()


def _prepare_backends_for_fork():
    """Close reusable SQLite handles while the parent process is still safe."""

    with _FORK_BACKENDS_LOCK:
        for backend in tuple(_FORK_BACKENDS):
            backend.close()


if hasattr(os, "register_at_fork"):  # pragma: no branch - platform capability
    os.register_at_fork(before=_prepare_backends_for_fork)


class _PointReadConnection:
    """One idle query-only connection and the file it was opened against."""

    __slots__ = ("connection", "file_identity")

    def __init__(self, connection, file_identity):
        self.connection = connection
        self.file_identity = file_identity


class _AttachedReadConnection:
    """One query-only manifest connection with every shard attached."""

    __slots__ = ("connection", "file_identities")

    def __init__(self, connection, file_identities):
        self.connection = connection
        self.file_identities = file_identities


def _new_order_tokens(count):
    """Return sortable, process-safe natural-order tokens for one operation."""

    global _ORDER_LAST_NS
    with _ORDER_LOCK:
        first = max(time.time_ns(), _ORDER_LAST_NS + 1)
        _ORDER_LAST_NS = first + count - 1
    return [
        "{0:020d}:{1}".format(first + offset, _ORDER_PROCESS_NONCE)
        for offset in range(count)
    ]


def _require_wal(conn, path, *, enable=False):
    """Require WAL and return its normalized journal-mode name."""

    row = conn.execute(
        "PRAGMA journal_mode=WAL" if enable else "PRAGMA journal_mode"
    ).fetchone()
    mode = "" if row is None or not row else str(row[0]).lower()
    if mode != "wal":
        raise ConfigurationError(
            "Sharded SQLite requires WAL on a local writable filesystem; "
            "SQLite returned {0!r} for {1!r}. In-memory, temporary, and "
            "network-mounted databases are unsupported.".format(mode, path)
        )
    return mode


class _ShardSQLiteTableBackend(SQLiteTableBackend):
    """Ordinary SQLite backend with a fail-closed WAL requirement."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wal_verified = False
        self._ready_order_collections = set()
        self._point_read_pool_lock = threading.RLock()
        self._point_read_pool_pid = os.getpid()
        self._point_read_pool_generation = 0
        self._point_read_pool_idle = []
        self._point_read_identity = None

    def _read_connect(self, *, check_same_thread=True):
        conn = super()._read_connect(check_same_thread=check_same_thread)
        # FULL preserves acknowledged transactions across an operating-system
        # crash.  WAL still amortizes its journal writes, and callers can
        # compare a faster policy later without weakening this first contract.
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _set_point_read_identity(self, identity):
        """Install the immutable shard identity checked by pooled readers."""

        if self._point_read_identity == identity:
            return
        self._retire_point_read_connections()
        self._point_read_identity = identity

    def _synchronize_point_read_pid(self):
        """Discard connections and synchronization inherited across ``fork``."""

        pid = os.getpid()
        if self._point_read_pool_pid == pid:
            return pid

        # Do not acquire the inherited lock: its owner may have been another
        # thread which no longer exists in the child process.
        inherited = self._point_read_pool_idle
        self._point_read_pool_lock = threading.RLock()
        self._point_read_pool_idle = []
        self._point_read_pool_pid = pid
        self._point_read_pool_generation += 1
        for entry in inherited:
            entry.connection.close()
        return pid

    def _point_read_file_identity(self):
        try:
            status = os.stat(self.path)
        except OSError as exc:
            raise StorageCorruptionError(
                "Sharded SQLite shard file disappeared while the backend "
                "was open: {0!r}".format(self.path)
            ) from exc
        if not stat.S_ISREG(status.st_mode):
            raise StorageCorruptionError(
                "Sharded SQLite shard path is not a file: {0!r}".format(self.path)
            )
        return status.st_dev, status.st_ino

    def _open_point_read_connection(self):
        """Open one autocommit, query-only, identity-checked WAL reader."""

        for _attempt in range(2):
            file_identity = self._point_read_file_identity()
            conn = SQLiteTableBackend._read_connect(
                self,
                check_same_thread=False,
            )
            try:
                conn.isolation_level = None
                conn.execute("PRAGMA query_only=ON")
                _require_wal(conn, self.path)
                if self._point_read_identity is not None:
                    rows = conn.execute(
                        "SELECT format_version, database_id, shard_index, "
                        "shard_count, hash_algorithm FROM __tinymongo_shard"
                    ).fetchall()
                    if rows != [self._point_read_identity]:
                        raise StorageCorruptionError(
                            "Sharded SQLite identity mismatch for pooled "
                            "reader: {0!r}".format(self.path)
                        )
                if self._point_read_file_identity() != file_identity:
                    conn.close()
                    continue
                return _PointReadConnection(conn, file_identity)
            except Exception:
                conn.close()
                raise
        raise StorageCorruptionError(
            "Sharded SQLite shard file changed repeatedly while opening a "
            "reader: {0!r}".format(self.path)
        )

    @contextmanager
    def _point_read_connection(self):
        """Lease a reusable reader without sharing it between active calls."""

        pid = self._synchronize_point_read_pid()
        with self._point_read_pool_lock:
            generation = self._point_read_pool_generation
            entry = None
            while self._point_read_pool_idle:
                candidate = self._point_read_pool_idle.pop()
                try:
                    current_identity = self._point_read_file_identity()
                except Exception:
                    candidate.connection.close()
                    raise
                if candidate.file_identity == current_identity:
                    entry = candidate
                    break
                candidate.connection.close()
            if entry is None:
                entry = self._open_point_read_connection()

        poisoned = False
        try:
            yield entry.connection
        except BaseException:
            poisoned = True
            raise
        finally:
            if entry.connection.in_transaction:
                poisoned = True
                try:
                    entry.connection.rollback()
                except sqlite3.Error:
                    pass
            with self._point_read_pool_lock:
                reusable = (
                    not poisoned
                    and self._point_read_pool_pid == pid
                    and self._point_read_pool_generation == generation
                    and len(self._point_read_pool_idle) < _POINT_READ_MAX_IDLE
                )
                if reusable:
                    self._point_read_pool_idle.append(entry)
                else:
                    entry.connection.close()

    def _retire_point_read_connections(self):
        """Close idle readers; leased readers close when returned."""

        self._synchronize_point_read_pid()
        with self._point_read_pool_lock:
            self._point_read_pool_generation += 1
            idle = self._point_read_pool_idle
            self._point_read_pool_idle = []
        for entry in idle:
            entry.connection.close()

    def close(self):
        self._retire_point_read_connections()

    def _run_point_read(self, collection, operation):
        """Run an exact lookup and recover once from a concurrent table drop."""

        for attempt in range(2):
            self.create_collection(collection)
            try:
                with self._point_read_connection() as conn:
                    return operation(conn)
            except Exception as exc:
                if not self._is_missing_collection_error(exc):
                    raise
                if attempt:
                    raise StorageCorruptionError(
                        "Sharded SQLite collection changed repeatedly during "
                        "point read"
                    ) from exc
                self._forget_collection(collection)
        raise AssertionError("unreachable Sharded SQLite point-read retry")

    def find_exact_physical_id(
        self,
        collection,
        expected_id,
        physical_id,
        filter_doc=None,
    ):
        """Return one exact-ID document without a generic cursor or list."""

        table = _quote_identifier(collection)
        candidates = _physical_id_candidates(expected_id, current=physical_id)
        filter_doc = {"_id": expected_id} if filter_doc is None else filter_doc

        def load_one(conn):
            for candidate in candidates:
                cursor = conn.execute(
                    "SELECT _id, data FROM {0} WHERE _id = ? LIMIT 1".format(table),
                    (candidate,),
                )
                try:
                    row = cursor.fetchone()
                finally:
                    cursor.close()
                if row is None:
                    continue
                row_id, payload = row
                document = _restore_legacy_document_id(
                    row_id,
                    bson_json_loads(payload),
                    requested_id=expected_id,
                )
                if matches_filter(document, filter_doc):
                    return document

            if _requires_legacy_id_scan(expected_id):
                cursor = conn.execute("SELECT _id, data FROM {0}".format(table))
                try:
                    for row_id, payload in cursor:
                        document = _restore_legacy_document_id(
                            row_id,
                            bson_json_loads(payload),
                            requested_id=expected_id,
                        )
                        if matches_filter(document, filter_doc):
                            return document
                finally:
                    cursor.close()
            return None

        return self._run_point_read(collection, load_one)

    def _ensure_sqlite_initialized(self):
        with self._sqlite_state_lock:
            if self._wal_verified:
                return
        super()._ensure_sqlite_initialized()
        conn = self._read_connect()
        try:
            _require_wal(conn, self.path)
        finally:
            conn.close()
        with self._sqlite_state_lock:
            self._wal_verified = True

    def create_collection(self, collection):
        """Create the document table and its hidden cross-shard order column."""

        super().create_collection(collection)
        with self._sqlite_state_lock:
            if collection in self._ready_order_collections:
                return
        with self._write_lock():
            with self._sqlite_state_lock:
                if collection in self._ready_order_collections:
                    return
            conn = self._connect()
            try:
                columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info({0})".format(_quote_identifier(collection))
                    ).fetchall()
                }
                if _ORDER_COLUMN not in columns:
                    conn.execute(
                        "ALTER TABLE {0} ADD COLUMN {1} TEXT".format(
                            _quote_identifier(collection),
                            _quote_identifier(_ORDER_COLUMN),
                        )
                    )
                    conn.commit()
                self._ready_order_collections.add(collection)
            finally:
                conn.close()

    def _forget_collection(self, collection):
        super()._forget_collection(collection)
        with self._sqlite_state_lock:
            self._ready_order_collections.discard(collection)
        self._retire_point_read_connections()

    def find_with_order(self, collection, filter_doc=None):
        """Return matching documents paired with their hidden natural order."""

        documents = super().find(collection, filter_doc)
        if not documents:
            return []
        filter_doc = {} if filter_doc is None else filter_doc
        physical_ids = [_physical_id_key(document["_id"]) for document in documents]

        def load_current_rows(conn):
            current_by_id = {}
            table = _quote_identifier(collection)
            order_column = _quote_identifier(_ORDER_COLUMN)
            for offset in range(
                0,
                len(physical_ids),
                _SQLITE_CONFLICT_QUERY_SIZE,
            ):
                chunk = physical_ids[offset : offset + _SQLITE_CONFLICT_QUERY_SIZE]
                rows = conn.execute(
                    "SELECT _id, data, {0} FROM {1} WHERE _id IN ({2})".format(
                        order_column,
                        table,
                        ", ".join("?" for _ in chunk),
                    ),
                    chunk,
                ).fetchall()
                for physical_id, payload, order_token in rows:
                    document = bson_json_loads(payload)
                    if matches_filter(document, filter_doc):
                        current_by_id[physical_id] = (order_token, document)
            return [
                current_by_id[physical_id]
                for physical_id in physical_ids
                if physical_id in current_by_id
            ]

        # Re-read payload and order together so a concurrent delete/reinsert
        # cannot pair an old document with a new row's order token.  The child
        # retry also turns a concurrent table drop into a coherent empty/retry
        # result instead of leaking a raw sqlite3.OperationalError.
        return self._run_collection_read(collection, load_current_rows)

    def insert_ordered_rows_on_connection(
        self,
        conn,
        collection,
        rows,
    ):
        """Insert ``(physical_id, payload, order_token)`` rows."""

        conn.executemany(
            "INSERT INTO {0} (_id, data, {1}) VALUES (?, ?, ?)".format(
                _quote_identifier(collection),
                _quote_identifier(_ORDER_COLUMN),
            ),
            rows,
        )


class ShardedSQLiteTableBackend(TableBackend):
    """Experimental SQLite backend striped by stable BSON ``_id`` identity.

    One SQLite manifest stores configuration and logical schema metadata.  It
    is not touched by ordinary document writes except for concurrent metadata
    reads.  Every physical shard uses WAL and an isolated advisory lock.

    Multi-shard batches protect against normal runtime errors by reserving all
    affected shard transactions before writing.  SQLite cannot make commits to
    separate database files power-loss atomic, so a crash between those commits
    remains an explicitly experimental limitation.
    """

    dialect = "sqlite"
    extension = ".sqlite-sharded"

    def __init__(
        self,
        path,
        threads=None,
        duckdb_config=None,
        database=None,
        dsn=None,
        sqlite_shards=None,
    ):
        if sqlite3.threadsafety == 0:
            raise ConfigurationError(
                "Sharded SQLite requires a thread-safe SQLite runtime"
            )
        if os.path.isfile(path):
            raise ConfigurationError(
                "Sharded SQLite requires a database directory, not a file: "
                "{0!r}".format(os.fspath(path))
            )
        requested_shards = self._normalize_shard_count(sqlite_shards)
        super().__init__(
            path,
            threads=threads,
            duckdb_config=duckdb_config,
            database=database,
            dsn=dsn,
        )
        self.path = os.fspath(path)
        self._owner_pid = _PROCESS_ID()
        self._manifest_path = os.path.join(self.path, "manifest.sqlite")
        self._state_lock = threading.RLock()
        self._manifest_read_lock = threading.RLock()
        self._manifest_read_conn = None
        self._manifest_read_pid = None
        self._manifest_read_epoch = 0
        self._manifest_identity_generation = None
        self._manifest_catalog_cache = {}
        self._ready_collections = set()
        self._attached_read_pool_lock = threading.RLock()
        self._attached_read_pool_pid = self._owner_pid
        self._attached_read_pool_generation = 0
        self._attached_read_pool_idle = []
        self._attached_reads_disabled = False
        os.makedirs(self.path, exist_ok=True)
        (
            self.shard_count,
            self.database_id,
            manifest_state,
        ) = self._initialize_manifest(requested_shards)
        shard_paths = tuple(
            os.path.join(
                self.path,
                "shards",
                "{0:03d}".format(index),
                "data.sqlite",
            )
            for index in range(self.shard_count)
        )
        if manifest_state == "ready":
            missing = [path for path in shard_paths if not os.path.isfile(path)]
            if missing:
                raise StorageCorruptionError(
                    "Sharded SQLite database is missing required shard file(s): "
                    "{0}".format(", ".join(repr(path) for path in missing))
                )
        self._shards = tuple(
            _ShardSQLiteTableBackend(
                shard_paths[index],
                threads=threads,
                duckdb_config=duckdb_config,
                database=database,
                dsn=dsn,
            )
            for index in range(self.shard_count)
        )
        for index, shard in enumerate(self._shards):
            shard._ensure_sqlite_initialized()
            self._initialize_shard_identity(
                shard,
                index,
                allow_initialize=manifest_state == "initializing",
            )
        self._mark_manifest_ready()
        self._reconcile_manifest_schema()
        with _FORK_BACKENDS_LOCK:
            _FORK_BACKENDS.add(self)

    @staticmethod
    def _normalize_shard_count(value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("sqlite_shards must be an integer")
        if not _MIN_SHARD_COUNT <= value <= _MAX_SHARD_COUNT:
            raise ValueError(
                "sqlite_shards must be between {0} and {1}".format(
                    _MIN_SHARD_COUNT,
                    _MAX_SHARD_COUNT,
                )
            )
        return value

    def _manifest_connect(self, *, check_same_thread=True, uri=False):
        if hasattr(self, "_owner_pid") and _PROCESS_ID() != self._owner_pid:
            raise ConfigurationError(
                "Sharded SQLite handles cannot be reused after os.fork(); "
                "open the client in a spawned or exec'd child process"
            )
        # ``TinyMongoDatabase.close()`` releases resources but database handles
        # remain reusable, like the other embedded backends.  Once startup has
        # established an immutable identity, never let sqlite3 silently create
        # a missing manifest for a retained handle after ``drop_database()``.
        if hasattr(self, "database_id") and not os.path.isfile(self._manifest_path):
            raise StorageCorruptionError(
                "Sharded SQLite manifest disappeared while the backend was open"
            )
        database = self._manifest_path
        if uri:
            database = Path(os.path.abspath(database)).as_uri()
        conn = sqlite3.connect(
            database,
            timeout=30,
            check_same_thread=check_same_thread,
            uri=uri,
        )
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _synchronize_manifest_read_pid(self):
        """Replace a potentially locked manifest reader inherited by fork."""

        pid = os.getpid()
        if self._manifest_read_pid in (None, pid):
            return pid

        inherited = self._manifest_read_conn
        self._manifest_read_lock = threading.RLock()
        self._manifest_read_conn = None
        self._manifest_read_pid = None
        self._manifest_read_epoch += 1
        self._manifest_identity_generation = None
        self._manifest_catalog_cache.clear()
        if inherited is not None:
            inherited.close()
        return pid

    @contextmanager
    def _manifest_read_connection(self):
        """Reuse one read connection for cheap cross-process catalog checks."""

        pid = self._synchronize_manifest_read_pid()
        with self._manifest_read_lock:
            if self._manifest_read_conn is None:
                self._manifest_read_conn = self._manifest_connect(
                    check_same_thread=False
                )
                _require_wal(self._manifest_read_conn, self._manifest_path)
                try:
                    rows = self._manifest_read_conn.execute(
                        "SELECT format_version, shard_count, hash_algorithm, "
                        "database_id, state FROM __tinymongo_config"
                    ).fetchall()
                except Exception as exc:
                    self._manifest_read_conn.close()
                    self._manifest_read_conn = None
                    raise StorageCorruptionError(
                        "Sharded SQLite manifest identity is unavailable"
                    ) from exc
                expected = (
                    _FORMAT_VERSION,
                    self.shard_count,
                    _HASH_ALGORITHM,
                    self.database_id,
                    "ready",
                )
                if rows != [expected]:
                    self._manifest_read_conn.close()
                    self._manifest_read_conn = None
                    raise StorageCorruptionError(
                        "Sharded SQLite manifest identity changed while the "
                        "backend was open"
                    )
                self._manifest_read_pid = pid
                self._manifest_read_epoch += 1
                self._manifest_identity_generation = None
                self._manifest_catalog_cache.clear()
            yield self._manifest_read_conn

    @contextmanager
    def _manifest_write_lock(self):
        lock_path = os.path.join(self.path, ".manifest.lock")
        rlock = _local_rlocks.setdefault(lock_path, threading.RLock())
        first_acquire = _acquire_rlock(rlock)
        file_lock = None
        try:
            if first_acquire and portalocker is not None:  # pragma: no branch
                file_lock = portalocker.Lock(lock_path, timeout=30)
                file_lock.acquire()
            yield
        finally:
            if file_lock is not None:  # pragma: no branch
                file_lock.release()
            rlock.release()

    def _initialize_manifest(self, requested_shards):
        with self._manifest_write_lock():
            is_new = not os.path.exists(self._manifest_path)
            if is_new:
                unexpected = [
                    name
                    for name in os.listdir(self.path)
                    if name not in (".manifest.lock",)
                ]
                if unexpected:
                    raise StorageCorruptionError(
                        "Sharded SQLite directory has no manifest but is not "
                        "empty: {0!r}".format(self.path)
                    )
            conn = self._manifest_connect()
            try:
                if is_new:
                    _require_wal(conn, self._manifest_path, enable=True)
                    conn.execute("PRAGMA user_version={0}".format(_FORMAT_VERSION))
                    conn.execute(
                        "CREATE TABLE __tinymongo_config ("
                        "format_version INTEGER NOT NULL, "
                        "shard_count INTEGER NOT NULL, "
                        "hash_algorithm TEXT NOT NULL, "
                        "database_id TEXT NOT NULL, "
                        "state TEXT NOT NULL)"
                    )
                    conn.execute(
                        "CREATE TABLE __tinymongo_collections ("
                        "collection_name TEXT PRIMARY KEY, "
                        "state TEXT NOT NULL)"
                    )
                    conn.execute(
                        "CREATE TABLE __tinymongo_indexes ("
                        "collection_name TEXT NOT NULL, "
                        "index_name TEXT NOT NULL, "
                        "spec_json TEXT NOT NULL, "
                        "state TEXT NOT NULL, "
                        "PRIMARY KEY (collection_name, index_name))"
                    )
                else:
                    # Existing manifests are validated before any persistent
                    # setting or schema is changed.  In particular, never
                    # overwrite a newer format's user_version while rejecting
                    # it as unsupported.
                    _require_wal(conn, self._manifest_path)
                    row = conn.execute("PRAGMA user_version").fetchone()
                    user_version = 0 if row is None else int(row[0])
                    if user_version != _FORMAT_VERSION:
                        raise StorageCorruptionError(
                            "Unsupported Sharded SQLite manifest format {0} "
                            "in {1!r}".format(user_version, self._manifest_path)
                        )
                rows = conn.execute(
                    "SELECT format_version, shard_count, hash_algorithm, "
                    "database_id, state "
                    "FROM __tinymongo_config"
                ).fetchall()
                if not rows:
                    shard_count = requested_shards or _DEFAULT_SHARD_COUNT
                    database_id = uuid4().hex
                    conn.execute(
                        "INSERT INTO __tinymongo_config "
                        "(format_version, shard_count, hash_algorithm, "
                        "database_id, state) VALUES (?, ?, ?, ?, ?)",
                        (
                            _FORMAT_VERSION,
                            shard_count,
                            _HASH_ALGORITHM,
                            database_id,
                            "initializing",
                        ),
                    )
                    conn.commit()
                    return shard_count, database_id, "initializing"
                if len(rows) != 1:
                    raise StorageCorruptionError(
                        "Sharded SQLite manifest contains multiple configurations"
                    )
                (
                    format_version,
                    shard_count,
                    hash_algorithm,
                    database_id,
                    state,
                ) = rows[0]
                if (
                    format_version != _FORMAT_VERSION
                    or hash_algorithm != _HASH_ALGORITHM
                    or isinstance(shard_count, bool)
                    or not isinstance(shard_count, int)
                    or not _MIN_SHARD_COUNT <= shard_count <= _MAX_SHARD_COUNT
                    or not isinstance(database_id, str)
                    or not database_id
                    or state not in ("initializing", "ready")
                ):
                    raise StorageCorruptionError(
                        "Unsupported or corrupt Sharded SQLite manifest in "
                        "{0!r}".format(self._manifest_path)
                    )
                if requested_shards is not None and requested_shards != shard_count:
                    raise ConfigurationError(
                        "Sharded SQLite database was created with {0} shards; "
                        "sqlite_shards={1} cannot reopen it".format(
                            shard_count,
                            requested_shards,
                        )
                    )
                return int(shard_count), database_id, state
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _initialize_shard_identity(self, shard, index, allow_initialize):
        """Create or verify one shard's immutable manifest identity."""

        expected = (
            _FORMAT_VERSION,
            self.database_id,
            index,
            self.shard_count,
            _HASH_ALGORITHM,
        )
        with shard._write_lock():
            conn = shard._connect()
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = '__tinymongo_shard'"
                ).fetchone()
                if exists is None:
                    if not allow_initialize:
                        raise StorageCorruptionError(
                            "Sharded SQLite shard {0} has no identity metadata: "
                            "{1!r}".format(index, shard.path)
                        )
                    conn.execute(
                        "CREATE TABLE __tinymongo_shard ("
                        "format_version INTEGER NOT NULL, "
                        "database_id TEXT NOT NULL, "
                        "shard_index INTEGER NOT NULL, "
                        "shard_count INTEGER NOT NULL, "
                        "hash_algorithm TEXT NOT NULL)"
                    )
                    conn.execute(
                        "INSERT INTO __tinymongo_shard "
                        "(format_version, database_id, shard_index, "
                        "shard_count, hash_algorithm) VALUES (?, ?, ?, ?, ?)",
                        expected,
                    )
                    conn.commit()
                else:
                    rows = conn.execute(
                        "SELECT format_version, database_id, shard_index, "
                        "shard_count, hash_algorithm FROM __tinymongo_shard"
                    ).fetchall()
                    if rows != [expected]:
                        raise StorageCorruptionError(
                            "Sharded SQLite identity mismatch for shard {0}: "
                            "{1!r}".format(index, shard.path)
                        )
            finally:
                conn.close()
        shard._set_point_read_identity(expected)

    def _mark_manifest_ready(self):
        with self._manifest_write_lock():
            conn = self._manifest_connect()
            try:
                cursor = conn.execute(
                    "UPDATE __tinymongo_config SET state = 'ready' "
                    "WHERE database_id = ?",
                    (self.database_id,),
                )
                if cursor.rowcount != 1:
                    raise StorageCorruptionError(
                        "Sharded SQLite manifest identity changed during startup"
                    )
                conn.commit()
            finally:
                conn.close()

    def _manifest_collection_entries(self):
        with self._manifest_read_connection() as conn:
            rows = conn.execute(
                "SELECT collection_name, state FROM __tinymongo_collections "
                "ORDER BY collection_name"
            ).fetchall()
        entries = []
        for collection, state in rows:
            if state not in ("ready", "dropping"):
                raise StorageCorruptionError(
                    "Unsupported Sharded SQLite collection state {0!r} for "
                    "collection {1!r}".format(state, collection)
                )
            entries.append((collection, state))
        return entries

    def _manifest_collections(self):
        return [
            collection
            for collection, state in self._manifest_collection_entries()
            if state == "ready"
        ]

    def _manifest_orphan_index_collections(self):
        with self._manifest_read_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT indexes.collection_name "
                "FROM __tinymongo_indexes AS indexes "
                "LEFT JOIN __tinymongo_collections AS collections "
                "ON collections.collection_name = indexes.collection_name "
                "WHERE collections.collection_name IS NULL "
                "ORDER BY indexes.collection_name"
            ).fetchall()
            return [row[0] for row in rows]

    @staticmethod
    def _decode_manifest_index_rows(collection, rows):
        entries = []
        for spec_json, state in rows:
            if state not in ("pending", "ready"):
                raise StorageCorruptionError(
                    "Unsupported Sharded SQLite index state {0!r} for "
                    "collection {1!r}".format(state, collection)
                )
            try:
                spec = IndexSpec.from_metadata(bson_json_loads(spec_json))
            except Exception as exc:
                raise StorageCorruptionError(
                    "Corrupt Sharded SQLite index metadata for collection "
                    "{0!r}".format(collection)
                ) from exc
            entries.append((spec, state))
        return entries

    def _manifest_catalog(self, collection):
        """Return collection membership and index metadata in one read."""

        with self._manifest_read_connection() as conn:
            rows = self._load_manifest_catalog_rows(conn, collection)
        return self._decode_manifest_catalog(collection, rows)

    @staticmethod
    def _load_manifest_catalog_rows(conn, collection):
        return conn.execute(
            "SELECT collections.collection_name, collections.state, "
            "indexes.spec_json, indexes.state "
            "FROM (SELECT ? AS collection_name) AS wanted "
            "LEFT JOIN __tinymongo_collections AS collections "
            "ON collections.collection_name = wanted.collection_name "
            "LEFT JOIN __tinymongo_indexes AS indexes "
            "ON indexes.collection_name = wanted.collection_name "
            "ORDER BY indexes.index_name",
            (collection,),
        ).fetchall()

    def _decode_manifest_catalog(self, collection, rows):
        exists = any(row[0] is not None for row in rows)
        collection_states = {row[1] for row in rows if row[0] is not None}
        if len(collection_states) > 1 or collection_states - {"ready", "dropping"}:
            raise StorageCorruptionError(
                "Unsupported Sharded SQLite collection state for collection "
                "{0!r}".format(collection)
            )
        if collection_states == {"dropping"}:
            raise StorageCorruptionError(
                "Sharded SQLite collection {0!r} has an interrupted drop; "
                "close and reopen the database to finish recovery".format(collection)
            )
        index_rows = [(row[2], row[3]) for row in rows if row[2] is not None]
        if not exists and index_rows:
            raise StorageCorruptionError(
                "Sharded SQLite manifest has indexes for missing collection "
                "{0!r}".format(collection)
            )
        return exists, self._decode_manifest_index_rows(collection, index_rows)

    @staticmethod
    def _manifest_data_version(conn):
        row = conn.execute("PRAGMA data_version").fetchone()
        if row is None or len(row) != 1 or isinstance(row[0], bool):
            raise StorageCorruptionError(
                "Sharded SQLite manifest generation is unavailable"
            )
        try:
            return int(row[0])
        except (TypeError, ValueError) as exc:
            raise StorageCorruptionError(
                "Sharded SQLite manifest generation is invalid"
            ) from exc

    def _manifest_generation(self):
        with self._manifest_read_connection() as conn:
            return self._validated_manifest_generation(conn)

    def _validated_manifest_generation(self, conn):
        """Return a generation whose immutable manifest identity is valid."""

        token = self._manifest_read_epoch, self._manifest_data_version(conn)
        if self._manifest_identity_generation == token:
            return token
        try:
            rows = conn.execute(
                "SELECT format_version, shard_count, hash_algorithm, "
                "database_id, state FROM __tinymongo_config"
            ).fetchall()
        except Exception as exc:
            raise StorageCorruptionError(
                "Sharded SQLite manifest identity is unavailable"
            ) from exc
        expected = (
            _FORMAT_VERSION,
            self.shard_count,
            _HASH_ALGORITHM,
            self.database_id,
            "ready",
        )
        if rows != [expected]:
            raise StorageCorruptionError(
                "Sharded SQLite manifest identity changed while the backend " "was open"
            )
        self._manifest_identity_generation = token
        return token

    def _cached_manifest_catalog(self, collection):
        """Return a catalog snapshot cached by SQLite's commit generation."""

        with self._manifest_read_connection() as conn:
            for _attempt in range(3):
                token = self._validated_manifest_generation(conn)
                cached = self._manifest_catalog_cache.get(collection)
                if cached is not None and cached[0] == token:
                    exists, entries = cached[1]
                    return token, (exists, list(entries))

                rows = self._load_manifest_catalog_rows(conn, collection)
                if token != self._validated_manifest_generation(conn):
                    continue
                exists, entries = self._decode_manifest_catalog(collection, rows)
                immutable_catalog = exists, tuple(entries)
                self._manifest_catalog_cache[collection] = (
                    token,
                    immutable_catalog,
                )
                return token, (exists, list(entries))
        raise StorageCorruptionError(
            "Sharded SQLite manifest metadata changed repeatedly during read"
        )

    def _manifest_has_collection(self, collection):
        exists, _entries = self._manifest_catalog(collection)
        return exists

    def _manifest_index_entries(self, collection):
        _exists, entries = self._manifest_catalog(collection)
        return entries

    def _store_manifest_index(self, collection, spec, state):
        conn = self._manifest_connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO __tinymongo_indexes "
                "(collection_name, index_name, spec_json, state) "
                "VALUES (?, ?, ?, ?)",
                (
                    collection,
                    spec.name,
                    bson_json_dumps(
                        spec.to_metadata(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    state,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _delete_manifest_index(self, collection, index_name):
        conn = self._manifest_connect()
        try:
            conn.execute(
                "DELETE FROM __tinymongo_indexes "
                "WHERE collection_name = ? AND index_name = ?",
                (collection, index_name),
            )
            conn.commit()
        finally:
            conn.close()

    def _set_manifest_collection_state(self, collection, state):
        conn = self._manifest_connect()
        try:
            cursor = conn.execute(
                "UPDATE __tinymongo_collections SET state = ? "
                "WHERE collection_name = ?",
                (state, collection),
            )
            if cursor.rowcount != 1:
                raise StorageCorruptionError(
                    "Sharded SQLite collection metadata changed during "
                    "operation: {0!r}".format(collection)
                )
            conn.commit()
        finally:
            conn.close()

    def _finish_collection_drop_locked(self, collection):
        """Idempotently complete a durable cross-shard collection drop."""

        failures = []
        for shard in self._shards:
            try:
                shard.drop_collection(collection)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise StorageCorruptionError(
                "Sharded SQLite could not finish dropping collection {0!r}; "
                "close and reopen the database to retry recovery".format(collection)
            ) from failures[0]

        conn = self._manifest_connect()
        try:
            conn.execute(
                "DELETE FROM __tinymongo_indexes WHERE collection_name = ?",
                (collection,),
            )
            conn.execute(
                "DELETE FROM __tinymongo_collections WHERE collection_name = ?",
                (collection,),
            )
            conn.commit()
        finally:
            conn.close()
        with self._state_lock:
            self._ready_collections.discard(collection)

    @contextmanager
    def _write_lock(self):
        """Leave public routing unlocked; mutations take child locks below."""

        yield

    @contextmanager
    def _write_shards(
        self,
        indexes,
        collection=None,
        force_global=False,
        create_missing=True,
    ):
        """Lock selected shards in order and coordinate global uniqueness.

        Metadata is checked again after shard locks are held.  This closes the
        race where a unique index is created between a writer's initial catalog
        read and its physical write, without putting the manifest's exclusive
        lock on ordinary non-unique writes.
        """

        indexes = tuple(sorted(set(indexes)))
        global_required = force_global
        while True:
            stack = ExitStack()
            try:
                if global_required:
                    stack.enter_context(self._manifest_write_lock())
                for index in indexes:
                    stack.enter_context(self._shards[index]._write_lock())
                if collection:
                    collection_exists, entries = self._manifest_catalog(collection)
                    current_specs = [spec for spec, _state in entries]
                else:
                    collection_exists = True
                    current_specs = []
                if not collection_exists and not create_missing:
                    all_indexes = tuple(self._all_shard_indexes())
                    if not global_required or indexes != all_indexes:
                        stack.close()
                        indexes = all_indexes
                        global_required = True
                        continue
                elif not collection_exists:
                    stack.close()
                    self.create_collection(collection)
                    continue
                if not global_required and any(spec.unique for spec in current_specs):
                    stack.close()
                    global_required = True
                    continue
                try:
                    yield current_specs
                finally:
                    stack.close()
                return
            except Exception:
                stack.close()
                raise

    @contextmanager
    def _operation_write_lock(self, collection, operation, args, kwargs):
        """Keep collection-level read/modify/write sequences shard-atomic."""

        atomic_operations = {
            "update_one",
            "update_many",
            "replace_one",
            "find_one_and_update",
            "find_one_and_replace",
            "find_one_and_delete",
        }
        if operation not in atomic_operations:
            yield
            return
        if (
            operation in ("update_one", "update_many")
            and kwargs.get("upsert") is not True
        ):
            # The backend's update_many_with_result hook already selects and
            # commits under one route-aware lock. Only its later upsert phase
            # needs a collection-level lock spanning the public method.
            yield
            return

        query = args[0] if args else kwargs.get("query", {})
        direct_id = self._routing_id_equality(query)
        # Upsert builders do not extract equality from every nested $and form.
        # Unless the whole predicate is one exact _id equality, reserve every
        # shard so a generated ID and the no-match decision remain atomic.
        if kwargs.get("upsert") is True and _direct_id_equality(query) is _MISSING:
            direct_id = _MISSING
        targets = (
            tuple(self._all_shard_indexes())
            if direct_id is _MISSING
            else (self._shard_index(direct_id),)
        )
        with self._write_shards(
            targets,
            collection=collection,
            create_missing=False,
        ):
            yield

    def _all_shard_indexes(self):
        return range(self.shard_count)

    def _shard_index(self, document_id):
        """Return a deterministic shard number for one BSON identifier."""

        physical_id = _physical_id_key(document_id)
        return self._shard_index_from_physical_id(physical_id)

    def _shard_index_from_physical_id(self, physical_id):
        """Route an already canonicalized identifier without hashing again."""

        return int(physical_id[-16:], 16) % self.shard_count

    @staticmethod
    def _routing_id_equality(filter_doc):
        """Return any exact ``_id`` constraint that safely narrows an AND."""

        direct_id = _direct_id_equality(filter_doc)
        if direct_id is not _MISSING:
            return direct_id
        for field, expected in _positive_filter_conjuncts(filter_doc):
            if field != "_id":
                continue
            direct_id = _direct_id_equality({"_id": expected})
            if direct_id is not _MISSING:
                return direct_id
        return _MISSING

    def _target_shards(self, filter_doc):
        direct_id = self._routing_id_equality(filter_doc)
        if direct_id is _MISSING:
            return tuple(self._all_shard_indexes())
        return (self._shard_index(direct_id),)

    def _reconcile_manifest_schema(self):
        with self._write_shards(self._all_shard_indexes(), force_global=True):
            collection_entries = self._manifest_collection_entries()
            for collection, state in collection_entries:
                if state == "dropping":
                    self._finish_collection_drop_locked(collection)
            collections = self._manifest_collections()
            orphan_indexes = self._manifest_orphan_index_collections()
            if orphan_indexes:
                raise StorageCorruptionError(
                    "Sharded SQLite manifest has indexes for missing "
                    "collection(s): {0}".format(
                        ", ".join(repr(name) for name in orphan_indexes)
                    )
                )
            physical_collections = [
                set(shard.list_collections()) for shard in self._shards
            ]
            for collection in collections:
                missing_from = [
                    index
                    for index, names in enumerate(physical_collections)
                    if collection not in names
                ]
                if missing_from:
                    raise StorageCorruptionError(
                        "Sharded SQLite collection {0!r} is missing from "
                        "shard(s) {1}".format(
                            collection,
                            ", ".join(str(index) for index in missing_from),
                        )
                    )

                entries = self._manifest_index_entries(collection)
                for spec, state in entries:
                    if state != "pending":
                        continue
                    if spec.unique:
                        try:
                            validate_unique_documents(
                                self._find_existing(collection),
                                [spec],
                            )
                        except DuplicateKeyError as exc:
                            raise StorageCorruptionError(
                                "Sharded SQLite cannot recover unique index "
                                "{0!r}: documents conflict across shards".format(
                                    spec.name
                                )
                            ) from exc
                    for shard in self._shards:
                        shard.create_index(collection, spec)
                    self._store_manifest_index(collection, spec, "ready")

                expected = {
                    spec.name: spec
                    for spec, state in self._manifest_index_entries(collection)
                    if state == "ready"
                }
                for index, shard in enumerate(self._shards):
                    actual = {
                        spec.name: spec for spec in shard.get_index_specs(collection)
                    }
                    if actual != expected:
                        raise StorageCorruptionError(
                            "Sharded SQLite index catalog mismatch for "
                            "collection {0!r} on shard {1}".format(
                                collection,
                                index,
                            )
                        )
                    conn = shard._connect()
                    try:
                        columns = {
                            row[1]
                            for row in conn.execute(
                                "PRAGMA table_info({0})".format(
                                    _quote_identifier(collection)
                                )
                            ).fetchall()
                        }
                        if not {"_id", "data", _ORDER_COLUMN} <= columns:
                            raise StorageCorruptionError(
                                "Sharded SQLite collection schema mismatch for "
                                "{0!r} on shard {1}".format(collection, index)
                            )
                        native_indexes = {
                            row[0]
                            for row in conn.execute(
                                "SELECT name FROM sqlite_master "
                                "WHERE type = 'index' AND tbl_name = ?",
                                (collection,),
                            ).fetchall()
                        }
                        expected_native_indexes = set()
                        for spec in expected.values():
                            physical_name = shard._physical_index_name(
                                collection,
                                spec,
                            )
                            required = {physical_name}
                            if spec.unique:
                                required.add(physical_name + "_lookup")
                            if "." not in spec.field:
                                required.add(shard._type_index_name(collection, spec))
                            expected_native_indexes.update(required)
                            if not required <= native_indexes:
                                raise StorageCorruptionError(
                                    "Sharded SQLite physical index mismatch for "
                                    "collection {0!r}, index {1!r}, shard {2}".format(
                                        collection,
                                        spec.name,
                                        index,
                                    )
                                )
                        unexpected_native_indexes = {
                            name
                            for name in native_indexes
                            if name.startswith("__tm_idx_")
                            and name not in expected_native_indexes
                        }
                        if unexpected_native_indexes:
                            raise StorageCorruptionError(
                                "Sharded SQLite has unexpected physical "
                                "index(es) for collection {0!r} on shard {1}: "
                                "{2}".format(
                                    collection,
                                    index,
                                    ", ".join(
                                        sorted(
                                            repr(name)
                                            for name in unexpected_native_indexes
                                        )
                                    ),
                                )
                            )
                    finally:
                        conn.close()
                self._ready_collections.add(collection)

    def _attached_shard_file_identities(self):
        return tuple(shard._point_read_file_identity() for shard in self._shards)

    def _open_attached_read_connection(self):
        """Open a query-only connection spanning the manifest and all shards."""

        file_identities = self._attached_shard_file_identities()
        # ``uri=True`` sets SQLITE_OPEN_URI on the owning connection.  SQLite
        # then recognizes the read-only ``file:...?mode=ro`` names supplied to
        # ATTACH even when its platform build does not enable URI filenames by
        # default (notably the Python.org macOS and Windows runtimes).
        conn = self._manifest_connect(check_same_thread=False, uri=True)
        try:
            conn.isolation_level = None
            for index, shard in enumerate(self._shards):
                shard_uri = Path(os.path.abspath(shard.path)).as_uri() + "?mode=ro"
                schema = "shard{0}".format(index)
                conn.execute(
                    "ATTACH DATABASE ? AS {0}".format(schema),
                    (shard_uri,),
                )
                mode_row = conn.execute(
                    "PRAGMA {0}.journal_mode".format(schema)
                ).fetchone()
                if mode_row is None or str(mode_row[0]).lower() != "wal":
                    raise ConfigurationError(
                        "Sharded SQLite attached reader requires WAL for {0!r}".format(
                            shard.path
                        )
                    )
                expected_identity = (
                    _FORMAT_VERSION,
                    self.database_id,
                    index,
                    self.shard_count,
                    _HASH_ALGORITHM,
                )
                identity_rows = conn.execute(
                    "SELECT format_version, database_id, shard_index, "
                    "shard_count, hash_algorithm FROM {0}.__tinymongo_shard".format(
                        schema
                    )
                ).fetchall()
                if identity_rows != [expected_identity]:
                    raise StorageCorruptionError(
                        "Sharded SQLite identity mismatch for attached shard "
                        "{0}: {1!r}".format(index, shard.path)
                    )
            conn.execute("PRAGMA query_only=ON")
            if self._attached_shard_file_identities() != file_identities:
                raise StorageCorruptionError(
                    "Sharded SQLite shard files changed while opening an "
                    "attached reader"
                )
            return _AttachedReadConnection(conn, file_identities)
        except Exception:
            conn.close()
            raise

    def _synchronize_attached_read_pid(self):
        pid = _PROCESS_ID()
        if self._attached_read_pool_pid == pid:
            return pid
        inherited = self._attached_read_pool_idle
        self._attached_read_pool_lock = threading.RLock()
        self._attached_read_pool_idle = []
        self._attached_read_pool_pid = pid
        self._attached_read_pool_generation += 1
        for entry in inherited:
            entry.connection.close()
        return pid

    @contextmanager
    def _attached_read_connection(self):
        """Lease one attached reader without sharing active SQLite handles."""

        pid = self._synchronize_attached_read_pid()
        file_identities = self._attached_shard_file_identities()
        with self._attached_read_pool_lock:
            generation = self._attached_read_pool_generation
            entry = None
            while self._attached_read_pool_idle:
                candidate = self._attached_read_pool_idle.pop()
                if candidate.file_identities == file_identities:
                    entry = candidate
                    break
                candidate.connection.close()
        if entry is None:
            entry = self._open_attached_read_connection()

        poisoned = False
        try:
            yield entry.connection
        except BaseException:
            poisoned = True
            raise
        finally:
            try:
                identities_match = (
                    entry.file_identities
                    == self._attached_shard_file_identities()
                )
            except Exception:
                identities_match = False
            with self._attached_read_pool_lock:
                reusable = (
                    not poisoned
                    and self._attached_read_pool_pid == pid
                    and self._attached_read_pool_generation == generation
                    and identities_match
                    and len(self._attached_read_pool_idle)
                    < _ATTACHED_READ_MAX_IDLE
                )
                if reusable:
                    self._attached_read_pool_idle.append(entry)
                else:
                    entry.connection.close()

    def _retire_attached_read_connections(self):
        self._synchronize_attached_read_pid()
        with self._attached_read_pool_lock:
            self._attached_read_pool_generation += 1
            idle = self._attached_read_pool_idle
            self._attached_read_pool_idle = []
        for entry in idle:
            entry.connection.close()

    def close(self):
        self._retire_attached_read_connections()
        self._synchronize_manifest_read_pid()
        with self._manifest_read_lock:
            if self._manifest_read_conn is not None:
                self._manifest_read_conn.close()
                self._manifest_read_conn = None
            self._manifest_read_pid = None
            self._manifest_read_epoch += 1
            self._manifest_identity_generation = None
            self._manifest_catalog_cache.clear()
        for shard in self._shards:
            shard.close()

    def list_collections(self):
        return self._manifest_collections()

    def create_collection(self, collection):
        if self._manifest_has_collection(collection):
            with self._state_lock:
                self._ready_collections.add(collection)
            return
        with self._write_shards(self._all_shard_indexes(), force_global=True):
            if self._manifest_has_collection(collection):
                with self._state_lock:
                    self._ready_collections.add(collection)
                return
            for shard in self._shards:
                shard._forget_collection(collection)
                shard.create_collection(collection)
            conn = self._manifest_connect()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO __tinymongo_collections "
                    "(collection_name, state) VALUES (?, 'ready')",
                    (collection,),
                )
                conn.commit()
            finally:
                conn.close()
            with self._state_lock:
                self._ready_collections.add(collection)

    def drop_collection(self, collection):
        states = dict(self._manifest_collection_entries())
        if collection not in states:
            return False
        with self._write_shards(
            self._all_shard_indexes(),
            force_global=True,
        ):
            states = dict(self._manifest_collection_entries())
            state = states.get(collection)
            if state is None:
                return False
            if state == "ready":
                self._set_manifest_collection_state(collection, "dropping")
            self._finish_collection_drop_locked(collection)
        return True

    def get_index_specs(self, collection):
        return [spec for spec, _state in self._manifest_index_entries(collection)]

    def create_index(self, collection, spec):
        spec = self._coerce_index_spec(spec)
        self.create_collection(collection)
        with self._write_shards(
            self._all_shard_indexes(),
            collection=collection,
            force_global=True,
        ):
            current = self.get_index_specs(collection)
            existing = self._check_index_compatibility(collection, spec, current)
            if existing is not None:
                return existing.name
            if spec.unique:
                validate_unique_documents(self._find_existing(collection), [spec])
            self._store_manifest_index(collection, spec, "pending")
            completed = []
            try:
                for shard in self._shards:
                    shard.create_index(collection, spec)
                    completed.append(shard)
            except Exception as exc:
                rollback_errors = []
                for shard in completed:
                    try:
                        shard.drop_index(collection, spec.name)
                    except Exception as rollback_exc:
                        rollback_errors.append(rollback_exc)
                if rollback_errors:
                    raise StorageCorruptionError(
                        "Sharded SQLite could not fully roll back index {0!r}; "
                        "pending recovery metadata was retained. Close and "
                        "reopen the database to finish recovery".format(spec.name)
                    ) from exc
                self._delete_manifest_index(collection, spec.name)
                raise
            self._store_manifest_index(collection, spec, "ready")
        return spec.name

    def drop_index(self, collection, name_or_field):
        with self._write_shards(
            self._all_shard_indexes(),
            collection=collection,
            force_global=True,
        ):
            specs = self.get_index_specs(collection)
            spec = next(
                (
                    item
                    for item in specs
                    if name_or_field == item.name
                    or (len(item.keys) == 1 and name_or_field == item.field)
                ),
                None,
            )
            if spec is None:
                raise OperationFailure(
                    "Index not found: {0}".format(name_or_field),
                    code=27,
                )
            dropped = []
            try:
                for shard in self._shards:
                    child_spec = next(
                        (
                            item
                            for item in shard.get_index_specs(collection)
                            if item.name == spec.name
                        ),
                        None,
                    )
                    if child_spec is not None:
                        shard.drop_index(collection, spec.name)
                        dropped.append(shard)
            except Exception as exc:
                rollback_errors = []
                for shard in dropped:
                    try:
                        shard.create_index(collection, spec)
                    except Exception as rollback_exc:
                        rollback_errors.append(rollback_exc)
                if rollback_errors:
                    self._store_manifest_index(collection, spec, "pending")
                    raise StorageCorruptionError(
                        "Sharded SQLite could not restore index {0!r} after "
                        "a failed drop; close and reopen the database before "
                        "continuing".format(spec.name)
                    ) from exc
                raise
            self._delete_manifest_index(collection, spec.name)

    def _find_existing_attached(self, collection):
        """Read one naturally ordered collection through SQLite UNION ALL."""

        table = _quote_identifier(collection)
        selects = [
            "SELECT _id, data, {0} AS order_token FROM shard{1}.{2}".format(
                _quote_identifier(_ORDER_COLUMN),
                index,
                table,
            )
            for index in range(self.shard_count)
        ]
        sql = (
            "SELECT _id, data FROM ({0}) "
            "ORDER BY order_token IS NULL, order_token"
        ).format(" UNION ALL ".join(selects))
        with self._attached_read_connection() as conn:
            rows = conn.execute(sql).fetchall()
        return [
            _restore_legacy_document_id(physical_id, bson_json_loads(payload))
            for physical_id, payload in rows
        ]

    def _find_existing(self, collection, filter_doc=None):
        filter_doc = {} if filter_doc is None else filter_doc
        direct_id = self._routing_id_equality(filter_doc)
        if direct_id is not _MISSING:
            physical_id = _physical_id_key(direct_id)
            shard = self._shards[self._shard_index_from_physical_id(physical_id)]
            document = shard.find_exact_physical_id(
                collection,
                direct_id,
                physical_id,
                filter_doc=filter_doc,
            )
            return [] if document is None else [document]
        if (
            not filter_doc
            and self.shard_count <= _DEFAULT_SQLITE_MAX_ATTACHED
            and not self._attached_reads_disabled
        ):
            try:
                return self._find_existing_attached(collection)
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "too many attached" in message:
                    self._attached_reads_disabled = True
                elif "no such table" not in message:
                    raise
                self._retire_attached_read_connections()
        ordered_documents = []
        for shard in self._shards:
            ordered_documents.extend(shard.find_with_order(collection, filter_doc))
        ordered_documents.sort(
            key=lambda item: (
                item[0] is None,
                item[0] or "",
            )
        )
        return [document for _order, document in ordered_documents]

    def _collection_read_generation(self, collection):
        token, (exists, _entries) = self._cached_manifest_catalog(collection)
        if not exists:
            self.create_collection(collection)
            token, (exists, _entries) = self._cached_manifest_catalog(collection)
        if not exists:
            return None
        with self._state_lock:
            self._ready_collections.add(collection)
        return token

    def _manifest_generation_matches(self, token):
        return self._manifest_generation() == token

    def find_one_exact_id(self, collection, document_id):
        """Return one strict exact-ID result through the point-read fast path."""

        physical_id = _physical_id_key(document_id)
        shard = self._shards[self._shard_index_from_physical_id(physical_id)]
        for _attempt in range(2):
            token = self._collection_read_generation(collection)
            if token is None:
                continue
            document = shard.find_exact_physical_id(
                collection,
                document_id,
                physical_id,
            )
            if self._manifest_generation_matches(token):
                return document
        raise StorageCorruptionError(
            "Sharded SQLite collection metadata changed repeatedly during "
            "exact-ID read"
        )

    def find(self, collection, filter_doc=None, sort=None, skip=None, limit=None):
        for _attempt in range(2):
            token = self._collection_read_generation(collection)
            if token is None:
                continue
            documents = self._find_existing(collection, filter_doc)
            if self._manifest_generation_matches(token):
                return documents
        raise StorageCorruptionError(
            "Sharded SQLite collection metadata changed repeatedly during read"
        )

    def count_documents(self, collection, filter_doc=None):
        for _attempt in range(2):
            token = self._collection_read_generation(collection)
            if token is None:
                continue
            filter_doc = {} if filter_doc is None else filter_doc
            direct_id = self._routing_id_equality(filter_doc)
            if direct_id is not _MISSING:
                physical_id = _physical_id_key(direct_id)
                document = self._shards[
                    self._shard_index_from_physical_id(physical_id)
                ].find_exact_physical_id(
                    collection,
                    direct_id,
                    physical_id,
                    filter_doc=filter_doc,
                )
                count = int(document is not None)
            else:
                count = sum(
                    shard.count_documents(collection, filter_doc)
                    for shard in self._shards
                )
            if self._manifest_generation_matches(token):
                return count
        raise StorageCorruptionError(
            "Sharded SQLite collection metadata changed repeatedly during count"
        )

    def _prepare_insert_many(
        self,
        documents,
        encoded_documents=None,
        previous=None,
    ):
        return self._shards[0]._prepare_insert_many(
            documents,
            encoded_documents=encoded_documents,
            previous=previous,
        )

    def find_insert_conflict_candidates(self, collection, documents, specs):
        if any(spec.unique for spec in specs):
            return None
        grouped = defaultdict(list)
        for document in documents:
            grouped[self._shard_index(document["_id"])].append(document)
        candidates = []
        for index, group in grouped.items():
            found = self._shards[index].find_insert_conflict_candidates(
                collection,
                group,
                [],
            )
            if found is None:
                return None
            candidates.extend(found)
        return candidates

    def _find_prepared_insert_conflict_candidates(
        self,
        collection,
        prepared_documents,
        specs,
    ):
        if any(spec.unique for spec in specs):
            return None
        grouped = defaultdict(list)
        for prepared in prepared_documents:
            grouped[self._shard_index(prepared.document["_id"])].append(prepared)
        candidates = []
        for index, group in grouped.items():
            found = self._shards[index]._find_prepared_insert_conflict_candidates(
                collection,
                group,
                [],
            )
            if found is None:
                return None
            candidates.extend(found)
        return candidates

    def insert_one_with_result(
        self,
        collection,
        document,
        bypass_document_validation=False,
    ):
        prepared = self._prepare_insert_many([document])
        results = self._insert_many_prepared(
            collection,
            prepared,
            bypass_document_validation=bypass_document_validation,
        )
        return results[0]

    def insert_many(self, collection, docs, bypass_document_validation=False):
        prepared = self._prepare_insert_many(docs)
        return self._insert_many_prepared(
            collection,
            prepared,
            bypass_document_validation=bypass_document_validation,
        )

    def insert_many_prevalidated(
        self,
        collection,
        docs,
        bypass_document_validation=False,
    ):
        prepared = self._prepare_insert_many(docs)
        return self._insert_many_prepared(
            collection,
            prepared,
            bypass_document_validation=bypass_document_validation,
        )

    def _insert_many_prepared(
        self,
        collection,
        prepared_documents,
        bypass_document_validation=False,
    ):
        del bypass_document_validation
        if not prepared_documents:
            return []
        self.create_collection(collection)
        grouped = defaultdict(list)
        order_tokens = _new_order_tokens(len(prepared_documents))
        for prepared, order_token in zip(prepared_documents, order_tokens):
            grouped[self._shard_index(prepared.document["_id"])].append(
                (prepared, order_token)
            )
        target_indexes = tuple(grouped)
        with self._write_shards(target_indexes, collection=collection) as specs:
            if any(spec.unique for spec in specs):
                validate_unique_documents(
                    self._find_existing(collection)
                    + [prepared.document for prepared in prepared_documents],
                    specs,
                )
            connections = {}
            committed = []
            try:
                for index in sorted(target_indexes):
                    conn = self._shards[index]._connect()
                    conn.execute("BEGIN IMMEDIATE")
                    connections[index] = conn
                    rows = [
                        (prepared.physical_id, prepared.payload, order_token)
                        for prepared, order_token in grouped[index]
                    ]
                    self._shards[index].insert_ordered_rows_on_connection(
                        conn,
                        collection,
                        rows,
                    )
                for index in sorted(target_indexes):
                    connections[index].commit()
                    committed.append(index)
                    with self._shards[index]._sqlite_state_lock:
                        self._shards[index]._known_nonempty_collections.add(collection)
            except sqlite3.IntegrityError as exc:
                for index, conn in connections.items():
                    if index not in committed and conn.in_transaction:
                        conn.rollback()
                if committed:
                    raise StorageCorruptionError(
                        "A sharded SQLite batch failed after another shard "
                        "committed; inspect the acknowledged documents"
                    ) from exc
                raise DuplicateKeyError(str(exc)) from exc
            except Exception as exc:
                for index, conn in connections.items():
                    if index not in committed and conn.in_transaction:
                        conn.rollback()
                if committed:
                    raise StorageCorruptionError(
                        "A sharded SQLite batch failed after another shard "
                        "committed; inspect the acknowledged documents"
                    ) from exc
                raise
            finally:
                for conn in connections.values():
                    conn.close()
        return list(range(len(prepared_documents)))

    def _unique_update(
        self,
        collection,
        filter_doc,
        update_doc,
        multi,
        validate_document=None,
    ):
        matches = self._find_existing(collection, filter_doc)
        if not multi:
            matches = matches[:1]
        if not matches:
            return [], 0, 0

        from .tinymongo import _update_document_modified

        replacements = []
        modified_count = 0
        for document in matches:
            updated = self.apply_update(document, update_doc)
            if validate_document is not None:
                validate_document(updated)
            modified_count += int(
                _update_document_modified(document, updated, update_doc)
            )
            if not storage_values_equal(document, updated):
                replacements.append((document, updated))

        replacement_by_id = {
            _physical_id_key(original["_id"]): updated
            for original, updated in replacements
        }
        post_image = [
            replacement_by_id.get(_physical_id_key(document["_id"]), document)
            for document in self._find_existing(collection)
        ]
        validate_unique_documents(post_image, self.get_index_specs(collection))
        for original, updated in replacements:
            shard = self._shards[self._shard_index(original["_id"])]
            shard.replace_one(collection, original["_id"], updated)
        return (
            [original["_id"] for original, _updated in replacements],
            len(matches),
            modified_count,
        )

    def update_many(self, collection, filter_doc, update_doc, multi=True):
        self.create_collection(collection)
        targets = self._target_shards(filter_doc)
        with self._write_shards(targets, collection=collection) as specs:
            if any(spec.unique for spec in specs):
                updated_ids, _matched, _modified = self._unique_update(
                    collection,
                    filter_doc,
                    update_doc,
                    multi,
                )
                return updated_ids
            if not multi and self._routing_id_equality(filter_doc) is _MISSING:
                matches = self._find_existing(collection, filter_doc)
                if not matches:
                    return []
                document_id = matches[0]["_id"]
                index = self._shard_index(document_id)
                return self._shards[index].update_many(
                    collection,
                    filter_doc,
                    update_doc,
                    multi=False,
                )
            updated_ids = []
            for index in targets:
                updated_ids.extend(
                    self._shards[index].update_many(
                        collection,
                        filter_doc,
                        update_doc,
                        multi=multi,
                    )
                )
                if not multi and updated_ids:
                    break
            return updated_ids

    def update_many_with_result(
        self,
        collection,
        filter_doc,
        update_doc,
        multi=True,
        validate_document=None,
    ):
        self.create_collection(collection)
        targets = self._target_shards(filter_doc)
        with self._write_shards(targets, collection=collection) as specs:
            if any(spec.unique for spec in specs):
                _ids, matched, modified = self._unique_update(
                    collection,
                    filter_doc,
                    update_doc,
                    multi,
                    validate_document=validate_document,
                )
                return matched, modified
            if not multi and self._routing_id_equality(filter_doc) is _MISSING:
                matches = self._find_existing(collection, filter_doc)
                if not matches:
                    return 0, 0
                document_id = matches[0]["_id"]
                index = self._shard_index(document_id)
                return self._shards[index].update_many_with_result(
                    collection,
                    filter_doc,
                    update_doc,
                    multi=False,
                    validate_document=validate_document,
                )
            matched = modified = 0
            for index in targets:
                shard_matched, shard_modified = self._shards[
                    index
                ].update_many_with_result(
                    collection,
                    filter_doc,
                    update_doc,
                    multi=multi,
                    validate_document=validate_document,
                )
                matched += shard_matched
                modified += shard_modified
                if not multi and matched:
                    break
            return matched, modified

    def replace_one(self, collection, doc_id, replacement):
        self.create_collection(collection)
        index = self._shard_index(doc_id)
        with self._write_shards((index,), collection=collection) as specs:
            if any(spec.unique for spec in specs):
                target_key = _physical_id_key(doc_id)
                validate_unique_documents(
                    [
                        (
                            replacement
                            if _physical_id_key(document["_id"]) == target_key
                            else document
                        )
                        for document in self._find_existing(collection)
                    ],
                    specs,
                )
            self._shards[index].replace_one(collection, doc_id, replacement)

    def delete_ids(self, collection, ids):
        if not ids:
            return
        self.create_collection(collection)
        grouped = defaultdict(list)
        for document_id in ids:
            grouped[self._shard_index(document_id)].append(document_id)
        with self._write_shards(tuple(grouped), collection=collection):
            for index in sorted(grouped):
                self._shards[index].delete_ids(collection, grouped[index])

    def delete_many(self, collection, filter_doc, multi=True):
        """Select and delete while retaining the same route-aware locks."""

        self.create_collection(collection)
        targets = self._target_shards(filter_doc)
        with self._write_shards(targets, collection=collection):
            matches = self._find_existing(collection, filter_doc)
            if not multi:
                matches = matches[:1]
            ids = [document["_id"] for document in matches]
            grouped = defaultdict(list)
            for document_id in ids:
                grouped[self._shard_index(document_id)].append(document_id)
            for index in sorted(grouped):
                self._shards[index].delete_ids(collection, grouped[index])
            return ids
