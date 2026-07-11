import json
import os
import sqlite3
import tempfile
import threading
from typing import Any
from tinydb.storages import Storage
from urllib.parse import urlparse
from .parquet_storage import _acquire_rlock, _fsync_dir, _local_rlocks, portalocker
from .errors import StorageCorruptionError

try:
    import duckdb as _duckdb
except Exception:  # pragma: no cover - optional dependency fallback
    _duckdb = None  # type: ignore[assignment]

duckdb: Any = _duckdb


OBJECT_STORAGE_SCHEMES = {"s3", "gs", "gcs", "az", "azure", "abfs", "abfss"}


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
        os.makedirs(self._directory, exist_ok=True)

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
                return json.load(handle)
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
            id_to_eid = {
                value.get("_id"): key
                for key, value in existing_table.items()
                if isinstance(value, dict) and "_id" in value
            }
            try:
                next_eid = max(int(key) for key in existing_table.keys()) + 1
            except Exception:
                next_eid = 1

            for value in incoming_table.values():
                doc_id = value.get("_id") if isinstance(value, dict) else None
                if doc_id is not None and doc_id in id_to_eid:
                    existing_table[id_to_eid[doc_id]] = value
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
                            json.load(handle) or {}, payload_data
                        )
                except (OSError, ValueError, TypeError) as exc:
                    raise StorageCorruptionError(
                        "Cannot update JSON database {0}: {1}".format(self.path, exc)
                    ) from exc

            payload = json.dumps(payload_data, ensure_ascii=False)
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
            return json.loads(row[0])
        except (sqlite3.DatabaseError, ValueError, TypeError, OSError) as exc:
            raise StorageCorruptionError(
                "Cannot read SQLite database {0}: {1}".format(self.path, exc)
            ) from exc

    def write(self, data):
        json_str = json.dumps(data or {}, ensure_ascii=False)
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
            return json.loads(result[0])
        except Exception as exc:
            raise StorageCorruptionError(
                "Cannot read DuckDB database {0}: {1}".format(self.path, exc)
            ) from exc

    def write(self, data):
        json_str = json.dumps(data or {}, ensure_ascii=False)
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

    if backend in ("tinydb", "json", "postgres", "postgresql", "mysql", "mariadb"):
        return AtomicJSONStorage
    if backend in ("parquet", "parquetv2"):
        from .parquet_storage import ParquetStorage

        return ParquetStorage
    if backend == "sqlite":
        return SQLiteStorage
    if backend == "duckdb":
        return DuckDBStorage

    raise ValueError(
        "Unsupported backend '{0}'. Supported backends: tinydb, parquet, parquetv2, sqlite, duckdb, postgres, mariadb.".format(
            name
        )
    )


def storage_extension(name):
    backend = str(name).lower()
    if backend in ("tinydb", "json"):
        return ".json"
    if backend in ("parquet", "parquetv2"):
        return ".parquet"
    if backend == "sqlite":
        return ".sqlite"
    if backend == "duckdb":
        return ".duckdb"
    if backend in ("postgres", "postgresql", "mysql", "mariadb"):
        return ""
    raise ValueError(
        "Unsupported backend '{0}'. Supported backends: tinydb, parquet, parquetv2, sqlite, duckdb, postgres, mariadb.".format(
            name
        )
    )


def is_table_backend(name):
    return str(name or "tinydb").lower() in (
        "sqlite",
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
