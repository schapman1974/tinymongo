import json
import os
import sqlite3
import tempfile
from tinydb.storages import Storage

try:
    import duckdb
except Exception:
    duckdb = None


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


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
        except Exception:
            return {}

    def write(self, data):
        json_str = json.dumps(data or {}, ensure_ascii=False)
        dname = os.path.dirname(self.path) or "."
        os.makedirs(dname, exist_ok=True)

        fd, tmp = tempfile.mkstemp(prefix="tmp", dir=dname)
        os.close(fd)
        try:
            conn = sqlite3.connect(tmp)
            cursor = conn.cursor()
            cursor.execute(
                "PRAGMA journal_mode = OFF"
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS tinydb(id INTEGER PRIMARY KEY, data TEXT)"
            )
            cursor.execute(
                "INSERT OR REPLACE INTO tinydb(id, data) VALUES(1, ?)"
                , (json_str,)
            )
            conn.commit()
            conn.close()
            os.replace(tmp, self.path)
            _fsync_dir(dname)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass


class DuckDBStorage(Storage):
    """TinyDB storage backend using DuckDB as a single-row JSON store."""

    def __init__(self, path):
        if duckdb is None:
            raise ImportError(
                "duckdb backend requires the duckdb package to be installed"
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
        except Exception:
            return {}

    def write(self, data):
        json_str = json.dumps(data or {}, ensure_ascii=False)
        dname = os.path.dirname(self.path) or "."
        os.makedirs(dname, exist_ok=True)

        fd, tmp = tempfile.mkstemp(prefix="tmp", dir=dname)
        os.close(fd)
        try:
            conn = duckdb.connect(tmp)
            conn.execute("CREATE TABLE IF NOT EXISTS tinydb(id INTEGER PRIMARY KEY, data TEXT)")
            conn.execute(
                "INSERT OR REPLACE INTO tinydb(id, data) VALUES(1, ?)",
                (json_str,)
            )
            conn.close()
            os.replace(tmp, self.path)
            _fsync_dir(dname)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass


def get_storage_class(name):
    if name is None:
        name = "tinydb"

    if isinstance(name, type):
        return name

    backend = str(name).lower()

    if backend in ("tinydb", "json"):
        from tinydb import TinyDB

        # TinyDB 3.x exposes DEFAULT_STORAGE instead of default_storage_class.
        return getattr(TinyDB, "default_storage_class", TinyDB.DEFAULT_STORAGE)
    if backend in ("parquet", "parquetv2"):
        from .parquet_storage import ParquetStorage

        return ParquetStorage
    if backend == "sqlite":
        return SQLiteStorage
    if backend == "duckdb":
        return DuckDBStorage

    raise ValueError(
        "Unsupported backend '{0}'. Supported backends: tinydb, parquet, parquetv2, sqlite, duckdb.".format(
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
    raise ValueError(
        "Unsupported backend '{0}'. Supported backends: tinydb, parquet, parquetv2, sqlite, duckdb.".format(
            name
        )
    )
