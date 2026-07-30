import os
import tempfile
from tinydb.storages import Storage
import threading
from typing import Any, Dict

from .errors import StorageCorruptionError
from .bson_codec import dumps as json_dumps
from .bson_codec import loads as json_loads
from .bson_types import bson_values_equal

try:
    import pyarrow as _pa
    import pyarrow.parquet as _pq
except Exception:  # pragma: no cover - graceful fallback
    _pa = None
    _pq = None

pa: Any = _pa
pq: Any = _pq

try:
    import portalocker as _portalocker
except Exception:  # pragma: no cover
    _portalocker = None  # type: ignore[assignment]

portalocker: Any = _portalocker


# In-process reentrant locks per lock path to avoid nested portalocker
# acquisitions causing AlreadyLocked errors when the same process/thread
# re-enters storage write paths.
_local_rlocks: Dict[str, threading.RLock] = {}
_MISSING_ID = object()


def _require_pyarrow():
    if pa is None or pq is None:
        raise ImportError(
            "parquet backend requires the optional Python driver 'pyarrow'. "
            "Install it with: pip install 'tinymongo[parquet]'"
        )


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def _acquire_rlock(rlock):
    try:
        if rlock._is_owned():
            rlock.acquire()
            return False
    except Exception:
        pass

    first_acquire = rlock.acquire(blocking=False)
    if not first_acquire:
        rlock.acquire()
        # A different thread owned the process-local lock. This thread is now
        # the outer owner and must acquire the cross-process file lock too.
        return True
    return True


class ParquetStorage(Storage):
    """TinyDB Storage that persists the entire DB as a single Parquet row

    The DB dict is serialized to JSON and stored in a single column named
    'data'. To coordinate concurrent writers we use a folder-scoped lock
    file named '.tinymongo.lock'."""

    def __init__(self, path):
        self.path = path

    def read(self):
        if not os.path.exists(self.path):
            return {}

        dname = os.path.dirname(self.path) or "."
        lock_path = os.path.join(dname, ".tinymongo.lock")

        rlock = _local_rlocks.setdefault(lock_path, threading.RLock())
        first_acquire = _acquire_rlock(rlock)

        portalocker_lock = None
        try:
            if first_acquire and portalocker is not None:  # pragma: no branch
                portalocker_lock = portalocker.Lock(lock_path, timeout=30)
                portalocker_lock.acquire()

            _require_pyarrow()

            try:
                table = pq.read_table(self.path)
                if "data" not in table.column_names:
                    return {}
                data_arr = table.column("data").to_pylist()
                if not data_arr:
                    return {}
                return json_loads(data_arr[0])
            except Exception as exc:
                raise StorageCorruptionError(
                    "Cannot read Parquet database {0}: {1}".format(self.path, exc)
                ) from exc
        finally:
            if portalocker_lock is not None:  # pragma: no branch
                try:
                    portalocker_lock.release()
                except Exception:  # pragma: no cover - defensive lock fallback
                    pass
            try:
                rlock.release()
            except Exception:  # pragma: no cover - defensive lock fallback
                pass

    def write(self, data):
        # serialize the entire TinyDB dict to a JSON string
        json_str = json_dumps(data or {}, ensure_ascii=False)

        # ensure parent dir exists
        dname = os.path.dirname(self.path) or "."
        os.makedirs(dname, exist_ok=True)

        lock_path = os.path.join(dname, ".tinymongo.lock")

        # Acquire an in-process reentrant lock to allow nested calls inside
        # the same process to proceed without re-acquiring the OS-level
        # portalocker lock. Only the first acquirer in the process will
        # perform the portalocker acquisition for inter-process safety.
        rlock = _local_rlocks.setdefault(lock_path, threading.RLock())
        first_acquire = _acquire_rlock(rlock)

        portalocker_lock = None
        try:
            if first_acquire and portalocker is not None:  # pragma: no branch
                portalocker_lock = portalocker.Lock(lock_path, timeout=30)
                portalocker_lock.acquire()

            _require_pyarrow()

            # read existing parquet into dict
            existing = {}
            if os.path.exists(self.path):
                try:
                    table = pq.read_table(self.path)
                    if "data" in table.column_names:  # pragma: no branch
                        data_arr = table.column("data").to_pylist()
                        if data_arr:  # pragma: no branch
                            existing = json_loads(data_arr[0])
                except Exception as exc:
                    raise StorageCorruptionError(
                        "Cannot update Parquet database {0}: {1}".format(self.path, exc)
                    ) from exc

            # Merge incoming into existing, matching on logical `_id`.
            merged = {}
            incoming = data or {}
            for tname, table_data in existing.items():
                merged[str(tname)] = {str(k): v for k, v in (table_data or {}).items()}

            for tname, table_data in incoming.items():
                t = str(tname)
                incoming_table = {str(k): v for k, v in (table_data or {}).items()}
                existing_table = merged.get(t, {})

                ids_and_eids = [
                    (v.get("_id"), k)
                    for k, v in existing_table.items()
                    if isinstance(v, dict) and "_id" in v
                ]
                try:
                    next_eid = max(int(k) for k in existing_table.keys()) + 1
                except Exception:
                    next_eid = 1

                for k, v in incoming_table.items():
                    doc_id = (
                        v["_id"] if isinstance(v, dict) and "_id" in v else _MISSING_ID
                    )
                    existing_eid = next(
                        (
                            eid
                            for existing_id, eid in ids_and_eids
                            if bson_values_equal(existing_id, doc_id)
                        ),
                        None,
                    )
                    if doc_id is not _MISSING_ID and existing_eid is not None:
                        existing_table[existing_eid] = v
                    else:
                        existing_table[str(next_eid)] = v
                        next_eid += 1

                merged[t] = existing_table

            json_str = json_dumps(merged, ensure_ascii=False)

            # write Parquet with single column 'data'
            arr = pa.array([json_str], type=pa.string())
            table = pa.table({"data": arr})

            fd, tmp = tempfile.mkstemp(prefix="tmp", dir=dname)
            os.close(fd)
            try:
                # write with Parquet v2 layout
                pq.write_table(table, tmp, version="2.6")
                # fsync file
                try:
                    with open(tmp, "rb") as f:
                        f.flush()
                        os.fsync(f.fileno())
                except Exception:  # pragma: no cover - best-effort fsync
                    pass
                os.replace(tmp, self.path)
                _fsync_dir(dname)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:  # pragma: no cover - best-effort cleanup
                        pass
        finally:
            # Release portalocker only if we acquired it here.
            if (
                "portalocker_lock" in locals() and portalocker_lock is not None
            ):  # pragma: no cover
                try:
                    portalocker_lock.release()
                except Exception:  # pragma: no cover - best-effort lock release
                    pass
            # Release in-process reentrant lock once.
            try:
                rlock.release()
            except Exception:  # pragma: no cover - defensive lock release
                pass
