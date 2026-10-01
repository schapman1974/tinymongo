import copy
import os
import sqlite3
import tempfile
import threading
from typing import Any
from tinydb import TinyDB
from tinydb.database import Document, StorageProxy
from tinydb.storages import Storage
from urllib.parse import urlparse
from .bson_codec import clone as clone_document
from .bson_codec import dumps as json_dumps
from .bson_codec import loads as json_loads
from .bson_codec import storage_values_equal
from .bson_types import bson_value_identity_key, bson_values_equal
from .parquet_storage import _acquire_rlock, _fsync_dir, _local_rlocks, portalocker
from .errors import StorageCorruptionError
from .json_chunks import load_table_chunks

try:
    import duckdb as _duckdb
except Exception:  # pragma: no cover - optional dependency fallback
    _duckdb = None  # type: ignore[assignment]

duckdb: Any = _duckdb


OBJECT_STORAGE_SCHEMES = {"s3", "gs", "gcs", "az", "azure", "abfs", "abfss"}
SUPPORTED_BACKEND_NAMES = (
    "memory",
    "tinydb",
    "json",
    "parquet",
    "parquetv2",
    "sqlite",
    "sqlite-sharded",
    "duckdb",
    "postgres",
    "postgresql",
    "mysql",
    "mariadb",
)
_MISSING_ID = object()


_memory_registry: dict[str, dict[str, Any]] = {}
_memory_registry_lock = threading.RLock()


def _memory_entry(address):
    """Return the process-local storage entry for a memory database."""
    with _memory_registry_lock:
        return _memory_registry.setdefault(
            str(address),
            {"data": None, "revision": 0, "lock": threading.RLock()},
        )


def list_memory_databases(namespace):
    """List databases currently present in a process-local namespace."""
    prefix = str(namespace).rstrip("/") + "/"
    with _memory_registry_lock:
        return sorted(
            address[len(prefix) :]
            for address in _memory_registry
            if address.startswith(prefix) and address[len(prefix) :]
        )


def clear_memory_namespace(namespace):
    """Remove all databases belonging to an anonymous memory client."""
    prefix = str(namespace).rstrip("/") + "/"
    with _memory_registry_lock:
        entries = [
            (address, entry)
            for address, entry in _memory_registry.items()
            if address.startswith(prefix)
        ]
    for address, entry in entries:
        with entry["lock"]:
            with _memory_registry_lock:
                if _memory_registry.get(address) is entry:
                    _memory_registry.pop(address, None)


def clear_memory_database(address):
    """Remove one named database from the process-local storage registry."""
    with _memory_registry_lock:
        entry = _memory_registry.get(str(address))
    if entry is None:
        return
    with entry["lock"]:
        with _memory_registry_lock:
            if _memory_registry.get(str(address)) is entry:
                _memory_registry.pop(str(address), None)


def is_object_storage_uri(value):
    return urlparse(str(value or "")).scheme.lower() in OBJECT_STORAGE_SCHEMES


def join_storage_uri(base, *parts):
    if is_object_storage_uri(base):
        return "/".join(
            [str(base).rstrip("/")] + [str(part).strip("/") for part in parts]
        )
    return os.path.join(base, *parts)


class AtomicJSONStorage(Storage):
    """TinyDB JSON storage with inter-process locks and atomic replace writes."""

    def __init__(self, path):
        self.path = path
        self._directory = os.path.dirname(path) or "."
        self.merge_writes = True
        self._retain_read_chunks = False
        self._read_chunks = {}
        self._cached_signature = None
        self._cached_data = {}
        self._serialized_tables = {}
        self._serialized_documents = {}
        os.makedirs(self._directory, exist_ok=True)

    def _file_signature(self):
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            return None
        return (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )

    @property
    def revision(self):
        """Invalidate retained TinyDB query caches after external file changes."""
        return self._file_signature() or (None,)

    def _load_cached(self):
        # Called with the database lock held. External replacements, edits and
        # deletions invalidate both decoded values and serialized table chunks.
        signature = self._file_signature()
        if signature != self._cached_signature:
            # Keep read() overrides/mocks in the load path. Only the native
            # implementation supplies chunks, and public reads retain none.
            self._read_chunks = {}
            self._retain_read_chunks = (
                getattr(self.read, "__func__", None) is AtomicJSONStorage.read
            )
            try:
                data = self.read() or {}
                chunks = self._read_chunks
            finally:
                self._retain_read_chunks = False
                self._read_chunks = {}
            self._cached_data = data
            self._serialized_tables = chunks
            self._serialized_documents = {name: {} for name in chunks}
            self._cached_signature = signature
        return self._cached_data

    def table_names(self):
        rlock, lock = self._acquire_lock()
        try:
            return set(self._load_cached())
        finally:
            self._release_lock(rlock, lock)

    def read_table(self, name):
        rlock, lock = self._acquire_lock()
        try:
            data = self._load_cached()
            if name not in data:
                data = dict(data)
                data[name] = {}
                self._write_cached_tables(data, {name})
            return copy.deepcopy(data[name])
        finally:
            self._release_lock(rlock, lock)

    def _serialize_table(self, name, table):
        # Legacy or reserved-marker table shapes keep the full codec path.
        if (
            not isinstance(table, dict)
            or set(table) == {"__tinymongo_type_v1__", "value"}
            or any("\x00" in str(key) for key in table)
        ):
            return (json_dumps(table, ensure_ascii=False),), {}
        previous = self._cached_data.get(name, {})
        cached = self._serialized_documents.get(name, {})
        documents = {}
        pieces = ["{"]
        for index, (key, document) in enumerate(table.items()):
            if index:
                pieces.append(", ")
            pieces.append(json_dumps(str(key), ensure_ascii=False) + ": ")
            if key in cached and storage_values_equal(previous[key], document):
                chunk = cached[key]
            else:
                chunk = json_dumps(document, ensure_ascii=False)
            documents[key] = chunk
            pieces.append(chunk)
        pieces.append("}")
        return tuple(pieces), documents

    def _write_cached_tables(self, data, changed):
        chunks = {}
        documents = {}
        for name, table in data.items():
            if name not in changed and name in self._serialized_tables:
                chunks[name] = self._serialized_tables[name]
                documents[name] = self._serialized_documents[name]
            else:
                chunks[name], documents[name] = self._serialize_table(name, table)
        fd, tmp = tempfile.mkstemp(prefix="tmp", dir=self._directory)
        try:
            with os.fdopen(fd, "w", encoding="utf8") as handle:
                # The BSON codec escapes a mapping with exactly these keys.
                # Keep that rare database-name combination byte compatible.
                if set(data) == {"__tinymongo_type_v1__", "value"}:
                    handle.write(json_dumps(data, ensure_ascii=False))
                else:
                    handle.write("{")
                    for index, (name, chunk) in enumerate(chunks.items()):
                        if index:
                            handle.write(", ")
                        handle.write(json_dumps(name, ensure_ascii=False) + ": ")
                        handle.writelines(chunk)
                    handle.write("}")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            _fsync_dir(self._directory)
            self._cached_signature = self._file_signature()
            self._cached_data = data
            self._serialized_tables = chunks
            self._serialized_documents = documents
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def write_table(self, name, data):
        rlock, lock = self._acquire_lock()
        try:
            existing = self._load_cached()
            incoming = clone_document({name: dict(data)})
            if self.merge_writes:
                incoming = self._merge_data({name: existing.get(name, {})}, incoming)
            payload = dict(existing)
            payload[name] = incoming[name]
            self._write_cached_tables(payload, {name})
        finally:
            self._release_lock(rlock, lock)

    def purge_table(self, name):
        rlock, lock = self._acquire_lock()
        try:
            payload = dict(self._load_cached())
            if name in payload:
                del payload[name]
                self._write_cached_tables(payload, set())
        finally:
            self._release_lock(rlock, lock)

    def close(self):
        rlock, lock = self._acquire_lock()
        try:
            self._cached_signature = None
            self._cached_data = {}
            self._serialized_tables = {}
            self._serialized_documents = {}
        finally:
            self._release_lock(rlock, lock)

    def _lock_path(self):
        return os.path.join(self._directory, ".tinymongo.lock")

    def _acquire_lock(self):
        lock_path = self._lock_path()
        rlock = _local_rlocks.setdefault(lock_path, threading.RLock())
        first_acquire = _acquire_rlock(rlock)
        portalocker_lock = None
        if first_acquire and portalocker is not None:
            portalocker_lock = portalocker.Lock(lock_path, timeout=30)
            portalocker_lock.acquire()
        return rlock, portalocker_lock

    def _release_lock(self, rlock, portalocker_lock):
        if portalocker_lock is not None:
            try:
                portalocker_lock.release()
            except Exception:  # pragma: no cover - defensive lock fallback
                pass
        try:
            rlock.release()
        except Exception:  # pragma: no cover - defensive lock fallback
            pass

    def read(self):
        rlock, portalocker_lock = self._acquire_lock()
        try:
            if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
                return {}
            with open(self.path, "r", encoding="utf8") as handle:
                text = handle.read()
                if self._retain_read_chunks:
                    data, self._read_chunks = load_table_chunks(text)
                    return data
                return json_loads(text)
        except (OSError, ValueError, TypeError) as exc:
            raise StorageCorruptionError(
                "Cannot read JSON database {0}: {1}".format(self.path, exc)
            ) from exc
        finally:
            self._release_lock(rlock, portalocker_lock)

    def _merge_data(self, existing, incoming):
        merged = {}
        for table_name, table_data in (existing or {}).items():
            merged[str(table_name)] = {
                str(key): value for key, value in (table_data or {}).items()
            }

        for table_name, table_data in (incoming or {}).items():
            table = str(table_name)
            incoming_table = {
                str(key): value for key, value in (table_data or {}).items()
            }
            existing_table = merged.get(table, {})
            ids_and_eids = [
                (value.get("_id"), key)
                for key, value in existing_table.items()
                if isinstance(value, dict) and "_id" in value
            ]
            identities = {}
            fallback_ids = []
            # Keep the first owner, just as the previous linear search did.
            # Recursive keys cover document-valued IDs as well as scalars.
            for doc_id, eid in ids_and_eids:
                identity = bson_value_identity_key(doc_id)
                if identity is None:
                    fallback_ids.append((doc_id, eid))
                else:
                    identities.setdefault(identity, eid)
            try:
                next_eid = max(int(key) for key in existing_table.keys()) + 1
            except Exception:
                next_eid = 1

            for value in incoming_table.values():
                doc_id = (
                    value["_id"]
                    if isinstance(value, dict) and "_id" in value
                    else _MISSING_ID
                )
                identity = bson_value_identity_key(doc_id)
                existing_eid = identities.get(identity)
                if identity is None or fallback_ids:
                    # Custom/legacy values may compare equal to registered BSON
                    # values. Preserve their original first-match ordering.
                    existing_eid = next(
                        (
                            eid
                            for existing_id, eid in ids_and_eids
                            if bson_values_equal(existing_id, doc_id)
                        ),
                        None,
                    )
                if doc_id is not _MISSING_ID and existing_eid is not None:
                    existing_table[existing_eid] = value
                else:
                    existing_table[str(next_eid)] = value
                    next_eid += 1

            merged[table] = existing_table

        return merged

    def write(self, data):
        rlock, portalocker_lock = self._acquire_lock()
        fd, tmp = tempfile.mkstemp(prefix="tmp", dir=self._directory)
        try:
            payload_data = data or {}
            if self.merge_writes and os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf8") as handle:
                        payload_data = self._merge_data(
                            json_loads(handle.read()) or {}, payload_data
                        )
                except (OSError, ValueError, TypeError) as exc:
                    raise StorageCorruptionError(
                        "Cannot update JSON database {0}: {1}".format(self.path, exc)
                    ) from exc

            payload = json_dumps(payload_data, ensure_ascii=False)
            with os.fdopen(fd, "w", encoding="utf8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            _fsync_dir(self._directory)
        finally:
            if os.path.exists(tmp):  # pragma: no cover - os.replace removes tmp
                try:
                    os.remove(tmp)
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass
            self._release_lock(rlock, portalocker_lock)


class MemoryStorage(AtomicJSONStorage):
    """TinyDB storage shared by a named address inside the current process."""

    is_memory = True

    def __init__(self, address):
        self.address = str(address)
        self.merge_writes = True
        self._entry = _memory_entry(self.address)

    @property
    def collection_lock(self):
        """Return the lock shared by every client using this database."""
        return self._entry["lock"]

    @property
    def revision(self):
        """Return the generation used to invalidate per-collection caches."""
        with self.collection_lock:
            return self._entry["revision"]

    def read(self):
        with self.collection_lock:
            return copy.deepcopy(self._entry["data"])

    def write(self, data):
        with self.collection_lock:
            payload = clone_document(data or {})
            if self.merge_writes and self._entry["data"] is not None:
                payload = self._merge_data(self._entry["data"], payload)
            self._entry["data"] = copy.deepcopy(payload)
            self._entry["revision"] += 1

    def table_names(self):
        with self.collection_lock:
            return set(self._entry["data"] or {})

    def read_table(self, name):
        with self.collection_lock:
            if self._entry["data"] is None:
                self._entry["data"] = {}
            if name not in self._entry["data"]:
                self._entry["data"][name] = {}
                self._entry["revision"] += 1
            return copy.deepcopy(self._entry["data"][name])

    def write_table(self, name, data):
        with self.collection_lock:
            # Normalize BSON and detach caller-owned values before publishing.
            payload = clone_document({name: dict(data)})
            existing = self._entry["data"] or {}
            if self.merge_writes:
                payload = self._merge_data({name: existing.get(name, {})}, payload)
            if self._entry["data"] is None:
                self._entry["data"] = {}
            self._entry["data"][name] = payload[name]
            self._entry["revision"] += 1

    def purge_table(self, name):
        with self.collection_lock:
            data = self._entry["data"] or {}
            if name in data:
                del data[name]
                self._entry["revision"] += 1

    def close(self):
        """Memory remains available to other clients in the process."""


class MemoryStorageProxy(StorageProxy):
    """Keep TinyDB table operations inside their selected memory table."""

    def read(self):
        return {
            int(key): self._new_document(key, value)
            for key, value in self._storage.read_table(self._table_name).items()
        }

    def write(self, data):
        # TinyDB wraps loaded rows in Document to carry a non-persistent ID.
        # Strip only that exact wrapper so ordinary rows can use the codec's
        # built-in cloning path; nested BSON/subclass values keep their rules.
        rows = {
            key: dict(value) if type(value) is Document else value
            for key, value in data.items()
        }
        self._storage.write_table(self._table_name, rows)


class MemoryTinyDB(TinyDB):
    storage_proxy_class = MemoryStorageProxy

    def tables(self):
        return self._storage.table_names()

    def purge_table(self, name):
        self._table_cache.pop(name, None)
        self._storage.purge_table(name)


class SQLiteStorage(Storage):
    """TinyDB storage backend using SQLite as a single-row JSON store."""

    def __init__(self, path):
        self.path = path

    def read(self):
        if not os.path.exists(self.path):
            return {}

        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS tinydb(id INTEGER PRIMARY KEY, data TEXT)"
            )
            cursor.execute("SELECT data FROM tinydb WHERE id = 1")
            row = cursor.fetchone()
            conn.close()
            if row is None or row[0] is None:
                return {}
            return json_loads(row[0])
        except (sqlite3.DatabaseError, ValueError, TypeError, OSError) as exc:
            raise StorageCorruptionError(
                "Cannot read SQLite database {0}: {1}".format(self.path, exc)
            ) from exc

    def write(self, data):
        json_str = json_dumps(data or {}, ensure_ascii=False)
        dname = os.path.dirname(self.path) or "."
        os.makedirs(dname, exist_ok=True)

        fd, tmp = tempfile.mkstemp(prefix="tmp", dir=dname)
        os.close(fd)
        try:
            conn = sqlite3.connect(tmp)
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode = OFF")
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS tinydb(id INTEGER PRIMARY KEY, data TEXT)"
            )
            cursor.execute(
                "INSERT OR REPLACE INTO tinydb(id, data) VALUES(1, ?)", (json_str,)
            )
            conn.commit()
            conn.close()
            os.replace(tmp, self.path)
            _fsync_dir(dname)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass


class DuckDBStorage(Storage):
    """TinyDB storage backend using DuckDB as a single-row JSON store."""

    def __init__(self, path):
        if duckdb is None:
            raise ImportError(
                "duckdb backend requires the optional Python driver 'duckdb'. "
                "Install it with: pip install 'tinymongo[duckdb]'"
            )
        self.path = path

    def read(self):
        if not os.path.exists(self.path):
            return {}

        try:
            conn = duckdb.connect(self.path)
            result = conn.execute("SELECT data FROM tinydb WHERE id = 1").fetchone()
            conn.close()
            if result is None or result[0] is None:
                return {}
            return json_loads(result[0])
        except Exception as exc:
            raise StorageCorruptionError(
                "Cannot read DuckDB database {0}: {1}".format(self.path, exc)
            ) from exc

    def write(self, data):
        json_str = json_dumps(data or {}, ensure_ascii=False)
        dname = os.path.dirname(self.path) or "."
        os.makedirs(dname, exist_ok=True)

        fd, tmp = tempfile.mkstemp(prefix="tmp", dir=dname)
        os.close(fd)
        try:
            os.remove(tmp)
            conn = duckdb.connect(tmp)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tinydb(id INTEGER PRIMARY KEY, data TEXT)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO tinydb(id, data) VALUES(1, ?)", (json_str,)
            )
            conn.close()
            os.replace(tmp, self.path)
            _fsync_dir(dname)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass


def get_storage_class(name):
    if name is None:
        name = "tinydb"

    if isinstance(name, type):
        return name

    backend = str(name).lower()

    if backend == "memory":
        return MemoryStorage
    if backend in ("tinydb", "json", "postgres", "postgresql", "mysql", "mariadb"):
        return AtomicJSONStorage
    if backend in ("parquet", "parquetv2"):
        from .parquet_storage import ParquetStorage

        return ParquetStorage
    if backend in ("sqlite", "sqlite-sharded"):
        return SQLiteStorage
    if backend == "duckdb":
        return DuckDBStorage

    raise ValueError(
        "Unsupported backend '{0}'. Supported backends: {1}.".format(
            name,
            ", ".join(SUPPORTED_BACKEND_NAMES),
        )
    )


def storage_extension(name):
    backend = str(name).lower()
    if backend == "memory":
        return ""
    if backend in ("tinydb", "json"):
        return ".json"
    if backend in ("parquet", "parquetv2"):
        return ".parquet"
    if backend == "sqlite":
        return ".sqlite"
    if backend == "sqlite-sharded":
        return ".sqlite-sharded"
    if backend == "duckdb":
        return ".duckdb"
    if backend in ("postgres", "postgresql", "mysql", "mariadb"):
        return ""
    raise ValueError(
        "Unsupported backend '{0}'. Supported backends: {1}.".format(
            name,
            ", ".join(SUPPORTED_BACKEND_NAMES),
        )
    )


def is_table_backend(name):
    return str(name or "tinydb").lower() in (
        "sqlite",
        "sqlite-sharded",
        "duckdb",
        "parquet",
        "parquetv2",
        "postgres",
        "postgresql",
        "mysql",
        "mariadb",
    )


def is_remote_sql_backend(name):
    return str(name or "tinydb").lower() in (
        "postgres",
        "postgresql",
        "mysql",
        "mariadb",
    )


def get_table_backend(name):
    backend = str(name or "tinydb").lower()
    if backend == "sqlite":
        from .table_backends import SQLiteTableBackend

        return SQLiteTableBackend
    if backend == "sqlite-sharded":
        from .sharded_sqlite import ShardedSQLiteTableBackend

        return ShardedSQLiteTableBackend
    if backend == "duckdb":
        from .table_backends import DuckDBTableBackend

        return DuckDBTableBackend
    if backend in ("parquet", "parquetv2"):
        from .table_backends import ParquetDuckDBBackend

        return ParquetDuckDBBackend
    if backend in ("postgres", "postgresql"):
        from .table_backends import PostgresTableBackend

        return PostgresTableBackend
    if backend in ("mysql", "mariadb"):
        from .table_backends import MySQLTableBackend

        return MySQLTableBackend
    raise ValueError("Backend '{0}' is not table-native".format(name))
