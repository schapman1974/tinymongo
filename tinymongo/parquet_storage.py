import json
import os
import tempfile
from tinydb.storages import Storage
import threading

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except Exception:  # pragma: no cover - graceful fallback
    pa = None
    pq = None

try:
    import portalocker
except Exception:  # pragma: no cover
    portalocker = None


# In-process reentrant locks per lock path to avoid nested portalocker
# acquisitions causing AlreadyLocked errors when the same process/thread
# re-enters storage write paths.
_local_rlocks = {}


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
        return False
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
            if first_acquire and portalocker is not None:
                portalocker_lock = portalocker.Lock(lock_path, timeout=30)
                portalocker_lock.acquire()

            if pq is None:
                try:
                    with open(self.path, "r", encoding="utf8") as f:
                        return json.load(f)
                except Exception:
                    return {}

            try:
                table = pq.read_table(self.path)
                if "data" not in table.column_names:
                    return {}
                data_arr = table.column("data").to_pylist()
                if not data_arr:
                    return {}
                return json.loads(data_arr[0])
            except Exception:
                return {}
        finally:
            if portalocker_lock is not None:
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
        json_str = json.dumps(data or {}, ensure_ascii=False)

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
            if pq is None:
                # No pyarrow — merge into existing JSON storage
                existing = {}
                if os.path.exists(self.path):
                    try:
                        with open(self.path, "r", encoding="utf8") as f:
                            existing = json.load(f) or {}
                    except Exception:  # pragma: no cover - corrupt existing JSON fallback
                        existing = {}

                # Merge incoming into existing, matching on logical `_id`.
                merged = {}
                incoming = data or {}
                for tname, table_data in existing.items():
                    merged[str(tname)] = {str(k): v for k, v in (table_data or {}).items()}

                for tname, table_data in incoming.items():
                    t = str(tname)
                    incoming_table = {str(k): v for k, v in (table_data or {}).items()}
                    existing_table = merged.get(t, {})

                    # map existing _id -> eid
                    id_to_eid = {v.get('_id'): k for k, v in existing_table.items() if isinstance(v, dict) and '_id' in v}
                    try:
                        next_eid = max(int(k) for k in existing_table.keys()) + 1
                    except Exception:
                        next_eid = 1

                    for k, v in incoming_table.items():
                        doc_id = v.get('_id') if isinstance(v, dict) else None
                        if doc_id is not None and doc_id in id_to_eid:
                            existing_table[id_to_eid[doc_id]] = v
                        else:
                            existing_table[str(next_eid)] = v
                            next_eid += 1

                    merged[t] = existing_table

                json_str = json.dumps(merged, ensure_ascii=False)
                fd, tmp = tempfile.mkstemp(prefix="tmp", dir=dname)
                try:
                    with os.fdopen(fd, "w", encoding="utf8") as f:
                        f.write(json_str)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self.path)
                    _fsync_dir(dname)
                finally:
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except Exception:  # pragma: no cover - best-effort cleanup
                            pass
                return

            # read existing parquet into dict
            existing = {}
            if os.path.exists(self.path):
                try:
                    table = pq.read_table(self.path)
                    if "data" in table.column_names:
                        data_arr = table.column("data").to_pylist()
                        if data_arr:
                            existing = json.loads(data_arr[0])
                except Exception:  # pragma: no cover - corrupt existing parquet fallback
                    existing = {}

            # Merge incoming into existing, matching on logical `_id`.
            merged = {}
            incoming = data or {}
            for tname, table_data in existing.items():
                merged[str(tname)] = {str(k): v for k, v in (table_data or {}).items()}

            for tname, table_data in incoming.items():
                t = str(tname)
                incoming_table = {str(k): v for k, v in (table_data or {}).items()}
                existing_table = merged.get(t, {})

                id_to_eid = {v.get('_id'): k for k, v in existing_table.items() if isinstance(v, dict) and '_id' in v}
                try:
                    next_eid = max(int(k) for k in existing_table.keys()) + 1
                except Exception:
                    next_eid = 1

                for k, v in incoming_table.items():
                    doc_id = v.get('_id') if isinstance(v, dict) else None
                    if doc_id is not None and doc_id in id_to_eid:
                        existing_table[id_to_eid[doc_id]] = v
                    else:
                        existing_table[str(next_eid)] = v
                        next_eid += 1

                merged[t] = existing_table

            json_str = json.dumps(merged, ensure_ascii=False)

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
            if 'portalocker_lock' in locals() and portalocker_lock is not None:  # pragma: no cover
                try:
                    portalocker_lock.release()
                except Exception:  # pragma: no cover - best-effort lock release
                    pass
            # Release in-process reentrant lock once.
            try:
                rlock.release()
            except Exception:  # pragma: no cover - defensive lock release
                pass
