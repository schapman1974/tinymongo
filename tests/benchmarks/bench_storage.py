"""Run one comparable CRUD workload across TinyMongo and reference engines.

This is intentionally a local benchmark rather than a pytest test.  Every row
uses the same JSON-shaped documents. Embedded engines and raw SQLite receive
four synchronized bulk insert calls from distinct spawned processes.
Full-collection reads, exact-ID reads, disjoint-ID updates, and disjoint-ID
deletes use the same process model. Raw SQLite is a native-SQL lower bound;
MongoDB is an optional client/server reference selected with ``--mongo-uri``.
"""

from __future__ import absolute_import

import argparse
import json
import multiprocessing
import os
import platform
import queue
import random
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import tinymongo as tm
from tinymongo.table_backends import _physical_id_key


BACKENDS = (
    "memory",
    "tinydb",
    "parquet",
    "sqlite",
    "sqlite-sharded",
    "duckdb",
    "raw-sqlite",
    "mongodb",
)

_BACKEND_LABELS = {
    "memory": "TinyMongo Memory",
    "tinydb": "TinyMongo TinyDB",
    "parquet": "TinyMongo Parquet",
    "sqlite": "TinyMongo SQLite",
    "sqlite-sharded": "TinyMongo SQLite-sharded (4)",
    "duckdb": "TinyMongo DuckDB",
    "raw-sqlite": "Raw SQLite (native SQL)",
    "mongodb": "MongoDB",
}

_SQLITE_SYNCHRONOUS_NAMES = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}

_MEDIAN_KEYS = (
    "insert_seconds",
    "read_seconds",
    "point_reads_per_second",
    "point_avg_ms",
    "point_p95_ms",
    "update_seconds",
    "delete_seconds",
    "file_kib",
)


def _shard_index(document_id, shard_count):
    physical_id = _physical_id_key(document_id)
    return int(physical_id[-16:], 16) % shard_count


def _docs(count):
    """Build the natural document sequence shared by every backend."""
    return [
        {
            "_id": "doc-{0}".format(index),
            "i": index,
            "group": "g{0}".format(index % 10),
            "payload": "value-{0}".format(index),
        }
        for index in range(count)
    ]


def _time_call(operation):
    started = time.perf_counter()
    result = operation()
    return time.perf_counter() - started, result


def _percentile(values, percentile):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((percentile / 100.0) * (len(ordered) - 1)))
    return ordered[index]


def _group_count(documents, group_number):
    if documents <= group_number:
        return 0
    return ((documents - 1 - group_number) // 10) + 1


def _document_batches(documents, workers):
    batches = [[] for _ in range(workers)]
    for document in documents:
        batches[_shard_index(document["_id"], workers)].append(document)
    if any(not batch for batch in batches):
        raise ValueError(
            "the document IDs must route at least one insert to every worker"
        )
    flattened_ids = [document["_id"] for batch in batches for document in batch]
    expected_ids = [document["_id"] for document in documents]
    if sorted(flattened_ids) != sorted(expected_ids):
        raise AssertionError("concurrent insert batches changed the document IDs")
    return batches


def _mutation_target_batches(documents, group, workers):
    batches = [[] for _worker in range(workers)]
    for document in documents:
        if document["group"] == group:
            document_id = document["_id"]
            batches[_shard_index(document_id, workers)].append(document_id)
    return batches


def _point_targets(documents, queries):
    generator = random.Random(1974)
    document_ids = [document["_id"] for document in documents]
    return [
        document_ids[generator.randrange(len(document_ids))] for _ in range(queries)
    ]


def _run_simultaneously(batches, operation, prepare=None, cleanup=None):
    """Run batch operations together while excluding worker setup from timing."""

    ready = queue.Queue()
    start = threading.Event()
    abort = threading.Event()

    def invoke(worker, batch):
        resource = None
        try:
            if prepare is not None:
                resource = prepare(worker, batch)
        except BaseException:
            ready.put((worker, False))
            raise
        ready.put((worker, True))
        try:
            if not start.wait(timeout=60):
                raise RuntimeError("timed out waiting for concurrent inserts to start")
            if abort.is_set():
                return None
            result = operation(worker, batch, resource)
            finished = time.perf_counter()
            return result, finished
        finally:
            if cleanup is not None:
                cleanup(worker, batch, resource)

    with ThreadPoolExecutor(max_workers=len(batches)) as executor:
        futures = [
            executor.submit(invoke, worker, batch)
            for worker, batch in enumerate(batches)
        ]
        try:
            statuses = [ready.get(timeout=60) for _batch in batches]
        except queue.Empty as exc:
            abort.set()
            start.set()
            raise RuntimeError("timed out preparing concurrent inserts") from exc
        if not all(status for _worker, status in statuses):
            abort.set()
            start.set()
            failures = []
            for worker, future in enumerate(futures):
                try:
                    future.result()
                except BaseException as exc:
                    failures.append((worker, exc))
            if failures:
                worker, failure = failures[0]
                raise RuntimeError(
                    "concurrent insert worker {0} failed while preparing a "
                    "batch of {1} documents: {2}: {3}".format(
                        worker,
                        len(batches[worker]),
                        type(failure).__name__,
                        failure,
                    )
                ) from failure
            raise RuntimeError("concurrent insert worker setup failed")
        started = time.perf_counter()
        start.set()
        outcomes = []
        failures = []
        for worker, future in enumerate(futures):
            try:
                outcomes.append(future.result())
            except BaseException as exc:
                failures.append((worker, exc))
                outcomes.append(None)
        if failures:
            worker, failure = failures[0]
            raise RuntimeError(
                "concurrent insert worker {0} failed for a batch of {1} "
                "documents: {2}: {3}".format(
                    worker,
                    len(batches[worker]),
                    type(failure).__name__,
                    failure,
                )
            ) from failure
        elapsed = max(finished for _result, finished in outcomes) - started
        results = [result for result, _finished in outcomes]
    return elapsed, results


def _tinymongo_insert_process_worker(
    backend,
    database_path,
    sqlite_shards,
    worker,
    batch,
    ready_queue,
    start_event,
    result_queue,
):
    """Open one client in a spawned worker and write one document batch."""

    client = None
    try:
        client = _open_tinymongo_client(
            backend,
            database_path,
            sqlite_shards,
        )
        collection = client.loadtest.records
        collection.count_documents({})
        ready_queue.put((worker, "ready"))
        if not start_event.wait(timeout=60):
            raise RuntimeError("timed out waiting for concurrent inserts to start")
        started = time.perf_counter()
        insert_result = collection.insert_many(batch)
        elapsed = time.perf_counter() - started
        result_queue.put(
            (
                worker,
                "ok",
                insert_result.inserted_ids,
                elapsed,
                os.getpid(),
            )
        )
    except BaseException as exc:
        failure = (worker, "error", type(exc).__name__, str(exc))
        ready_queue.put(failure)
        result_queue.put(failure)
    finally:
        if client is not None:
            client.close()


def _run_tinymongo_insert_processes(
    backend,
    database_path,
    sqlite_shards,
    document_batches,
):
    """Run one bulk in each of several spawned TinyMongo processes."""

    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_tinymongo_insert_process_worker,
            args=(
                backend,
                database_path,
                sqlite_shards,
                worker,
                batch,
                ready_queue,
                start_event,
                result_queue,
            ),
        )
        for worker, batch in enumerate(document_batches)
    ]
    try:
        for process in processes:
            process.start()

        ready = [ready_queue.get(timeout=120) for _process in processes]
        failures = [message for message in ready if message[1] != "ready"]
        if failures:
            raise RuntimeError(
                "TinyMongo insert worker setup failed: {0!r}".format(failures)
            )

        start_event.set()
        outcomes = [
            result_queue.get(timeout=180) for _process in processes
        ]
        failures = [message for message in outcomes if message[1] != "ok"]
        if failures:
            raise RuntimeError(
                "TinyMongo insert worker failed: {0!r}".format(failures)
            )

        ordered = sorted(outcomes)
        process_ids = [message[4] for message in ordered]
        if len(set(process_ids)) != len(processes):
            raise AssertionError("inserts did not use distinct processes")
        elapsed = max(message[3] for message in ordered)
        return elapsed, [message[2] for message in ordered], process_ids
    finally:
        start_event.set()
        for process in processes:
            if process.pid is None:
                continue
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join()
        ready_queue.close()
        ready_queue.join_thread()
        result_queue.close()
        result_queue.join_thread()


def _tinymongo_point_read_process_worker(
    backend,
    database_path,
    sqlite_shards,
    worker,
    targets,
    ready_queue,
    start_event,
    result_queue,
):
    """Read one exact-ID target stream in a spawned TinyMongo process."""

    client = None
    try:
        client = _open_tinymongo_client(
            backend,
            database_path,
            sqlite_shards,
        )
        collection = client.loadtest.records
        if targets:
            warmed = collection.find_one({"_id": targets[0]})
            if warmed is None or warmed.get("_id") != targets[0]:
                raise AssertionError("sharded point-read warm-up missed")
        ready_queue.put((worker, "ready"))
        if not start_event.wait(timeout=60):
            raise RuntimeError("timed out waiting for concurrent reads to start")

        started = time.perf_counter()
        latencies = []
        for target in targets:
            read_started = time.perf_counter()
            found = collection.find_one({"_id": target})
            latencies.append(time.perf_counter() - read_started)
            if found is None or found.get("_id") != target:
                raise AssertionError(
                    "sharded point read missed {0}".format(target)
                )
        elapsed = time.perf_counter() - started
        result_queue.put(
            (worker, "ok", latencies, elapsed, os.getpid())
        )
    except BaseException as exc:
        failure = (worker, "error", type(exc).__name__, str(exc))
        ready_queue.put(failure)
        result_queue.put(failure)
    finally:
        if client is not None:
            client.close()


def _run_tinymongo_point_read_processes(
    backend,
    database_path,
    sqlite_shards,
    targets,
    workers,
):
    """Run exact-ID reads through several spawned TinyMongo processes."""

    target_batches = [[] for _index in range(workers)]
    for target in targets:
        target_batches[_shard_index(target, workers)].append(target)

    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_tinymongo_point_read_process_worker,
            args=(
                backend,
                database_path,
                sqlite_shards,
                worker,
                batch,
                ready_queue,
                start_event,
                result_queue,
            ),
        )
        for worker, batch in enumerate(target_batches)
    ]
    try:
        for process in processes:
            process.start()

        ready = [ready_queue.get(timeout=120) for _process in processes]
        failures = [message for message in ready if message[1] != "ready"]
        if failures:
            raise RuntimeError(
                "TinyMongo read worker setup failed: {0!r}".format(failures)
            )

        start_event.set()
        outcomes = [
            result_queue.get(timeout=180) for _process in processes
        ]
        failures = [message for message in outcomes if message[1] != "ok"]
        if failures:
            raise RuntimeError(
                "TinyMongo read worker failed: {0!r}".format(failures)
            )

        ordered = sorted(outcomes)
        process_ids = [message[4] for message in ordered]
        if len(set(process_ids)) != len(processes):
            raise AssertionError("reads did not use distinct processes")
        latencies = [
            latency
            for message in ordered
            for latency in message[2]
        ]
        elapsed = max(message[3] for message in ordered)
        return elapsed, latencies, process_ids, [
            len(batch) for batch in target_batches
        ]
    finally:
        start_event.set()
        for process in processes:
            if process.pid is None:
                continue
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join()
        ready_queue.close()
        ready_queue.join_thread()
        result_queue.close()
        result_queue.join_thread()


def _tinymongo_read_all_process_worker(
    backend,
    database_path,
    sqlite_shards,
    worker,
    ready_queue,
    start_event,
    result_queue,
):
    client = None
    try:
        client = _open_tinymongo_client(backend, database_path, sqlite_shards)
        collection = client.loadtest.records
        list(collection.find({}))
        ready_queue.put((worker, "ready"))
        if not start_event.wait(timeout=60):
            raise RuntimeError("timed out waiting for concurrent reads to start")
        started = time.perf_counter()
        documents = list(collection.find({}))
        elapsed = time.perf_counter() - started
        result_queue.put((worker, "ok", documents, elapsed, os.getpid()))
    except BaseException as exc:
        failure = (worker, "error", type(exc).__name__, str(exc))
        ready_queue.put(failure)
        result_queue.put(failure)
    finally:
        if client is not None:
            client.close()


def _run_tinymongo_read_all_processes(
    backend,
    database_path,
    sqlite_shards,
    workers,
):
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_tinymongo_read_all_process_worker,
            args=(
                backend,
                database_path,
                sqlite_shards,
                worker,
                ready_queue,
                start_event,
                result_queue,
            ),
        )
        for worker in range(workers)
    ]
    try:
        for process in processes:
            process.start()
        ready = [ready_queue.get(timeout=120) for _process in processes]
        failures = [message for message in ready if message[1] != "ready"]
        if failures:
            raise RuntimeError(
                "TinyMongo read-all worker setup failed: {0!r}".format(failures)
            )
        start_event.set()
        outcomes = [result_queue.get(timeout=180) for _process in processes]
        failures = [message for message in outcomes if message[1] != "ok"]
        if failures:
            raise RuntimeError(
                "TinyMongo read-all worker failed: {0!r}".format(failures)
            )
        ordered = sorted(outcomes)
        process_ids = [message[4] for message in ordered]
        if len(set(process_ids)) != workers:
            raise AssertionError("read-all did not use distinct processes")
        elapsed = max(message[3] for message in ordered)
        return elapsed, [message[2] for message in ordered], process_ids
    finally:
        start_event.set()
        for process in processes:
            if process.pid is None:
                continue
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join()
        ready_queue.close()
        ready_queue.join_thread()
        result_queue.close()
        result_queue.join_thread()


def _tinymongo_mutation_process_worker(
    phase,
    backend,
    database_path,
    sqlite_shards,
    worker,
    document_ids,
    ready_queue,
    start_event,
    result_queue,
):
    client = None
    try:
        client = _open_tinymongo_client(backend, database_path, sqlite_shards)
        collection = client.loadtest.records
        if document_ids:
            warmed = collection.find_one({"_id": document_ids[0]})
            if warmed is None or warmed.get("_id") != document_ids[0]:
                raise AssertionError("TinyMongo mutation warm-up missed")
        ready_queue.put((worker, "ready"))
        if not start_event.wait(timeout=60):
            raise RuntimeError("timed out waiting for mutation phase to start")
        started = time.perf_counter()
        if not document_ids:
            count = 0
        elif phase == "update":
            count = 0
            for document_id in document_ids:
                update_result = collection.update_one(
                    {"_id": document_id},
                    {"$inc": {"i": 1}},
                )
                if (
                    update_result.matched_count != 1
                    or update_result.modified_count != 1
                ):
                    raise AssertionError("TinyMongo update count mismatch")
                count += 1
        elif phase == "delete":
            count = 0
            for document_id in document_ids:
                delete_result = collection.delete_one({"_id": document_id})
                if delete_result.deleted_count != 1:
                    raise AssertionError("TinyMongo delete count mismatch")
                count += 1
        else:  # pragma: no cover - guarded by callers
            raise ValueError("unsupported mutation phase: {0}".format(phase))
        elapsed = time.perf_counter() - started
        result_queue.put((worker, "ok", count, elapsed, os.getpid()))
    except BaseException as exc:
        failure = (worker, "error", type(exc).__name__, str(exc))
        ready_queue.put(failure)
        result_queue.put(failure)
    finally:
        if client is not None:
            client.close()


def _run_tinymongo_mutation_processes(
    phase,
    backend,
    database_path,
    sqlite_shards,
    batches,
):
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_tinymongo_mutation_process_worker,
            args=(
                phase,
                backend,
                database_path,
                sqlite_shards,
                worker,
                batch,
                ready_queue,
                start_event,
                result_queue,
            ),
        )
        for worker, batch in enumerate(batches)
    ]
    try:
        for process in processes:
            process.start()
        ready = [ready_queue.get(timeout=120) for _process in processes]
        failures = [message for message in ready if message[1] != "ready"]
        if failures:
            raise RuntimeError(
                "TinyMongo {0} setup failed: {1!r}".format(phase, failures)
            )
        start_event.set()
        outcomes = [result_queue.get(timeout=180) for _process in processes]
        failures = [message for message in outcomes if message[1] != "ok"]
        if failures:
            raise RuntimeError(
                "TinyMongo {0} failed: {1!r}".format(phase, failures)
            )
        ordered = sorted(outcomes)
        process_ids = [message[4] for message in ordered]
        if len(set(process_ids)) != len(processes):
            raise AssertionError(
                "{0} did not use distinct processes".format(phase)
            )
        return (
            max(message[3] for message in ordered),
            sum(message[2] for message in ordered),
            process_ids,
        )
    finally:
        start_event.set()
        for process in processes:
            if process.pid is None:
                continue
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join()
        ready_queue.close()
        ready_queue.join_thread()
        result_queue.close()
        result_queue.join_thread()


def _reference_process_worker(
    phase,
    backend,
    database_path,
    mongo_uri,
    database_name,
    worker,
    payload,
    ready_queue,
    start_event,
    result_queue,
):
    resource = None
    try:
        if backend == "raw-sqlite":
            resource = sqlite3.connect(database_path, timeout=30)
            resource.execute("PRAGMA synchronous=NORMAL")
            resource.execute("PRAGMA busy_timeout=30000")
        elif backend == "mongodb":
            import pymongo
            from pymongo.write_concern import WriteConcern

            resource = pymongo.MongoClient(
                mongo_uri,
                serverSelectionTimeoutMS=3000,
            )
            resource.admin.command("ping")
            resource = (
                resource,
                resource.get_database(
                    database_name,
                    write_concern=WriteConcern(w=1, j=True),
                ).records,
            )
        else:  # pragma: no cover - guarded by callers
            raise ValueError("unsupported reference backend: {0}".format(backend))

        def point_read(target):
            if backend == "raw-sqlite":
                row = resource.execute(
                    "SELECT data FROM records WHERE _id = ?",
                    (target,),
                ).fetchone()
                return None if row is None else json.loads(row[0])
            return resource[1].find_one({"_id": target})

        if phase == "insert":
            if backend == "raw-sqlite":
                resource.execute("SELECT COUNT(*) FROM records").fetchone()
            else:
                resource[1].count_documents({})
        elif phase == "read-all":
            if backend == "raw-sqlite":
                resource.execute("SELECT data FROM records").fetchall()
            else:
                list(resource[1].find({}))
        elif phase in ("point", "update", "delete") and payload:
            warmed = point_read(payload[0])
            if warmed is None or warmed.get("_id") != payload[0]:
                raise AssertionError("reference operation warm-up missed")
        ready_queue.put((worker, "ready"))
        if not start_event.wait(timeout=60):
            raise RuntimeError("timed out waiting for benchmark phase to start")

        started = time.perf_counter()
        if phase == "insert":
            if backend == "raw-sqlite":
                rows = [
                    (
                        document["_id"],
                        json.dumps(
                            document,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                    for document in payload
                ]
                resource.executemany(
                    "INSERT INTO records (_id, data) VALUES (?, ?)",
                    rows,
                )
                resource.commit()
                result = [document["_id"] for document in payload]
            else:
                insert_result = resource[1].insert_many(payload, ordered=True)
                if not insert_result.acknowledged:
                    raise AssertionError("MongoDB insert was not acknowledged")
                result = insert_result.inserted_ids
        elif phase == "read-all":
            if backend == "raw-sqlite":
                result = [
                    json.loads(row[0])
                    for row in resource.execute(
                        "SELECT data FROM records"
                    ).fetchall()
                ]
            else:
                result = list(resource[1].find({}))
        elif phase == "point":
            result = []
            for target in payload:
                read_started = time.perf_counter()
                found = point_read(target)
                result.append(time.perf_counter() - read_started)
                if found is None or found.get("_id") != target:
                    raise AssertionError(
                        "reference point read missed {0}".format(target)
                    )
        elif phase in ("update", "delete"):
            if not payload:
                result = 0
            elif backend == "raw-sqlite":
                result = 0
                if phase == "update":
                    statement = (
                        "UPDATE records SET data = json_set("
                        "data, '$.i', json_extract(data, '$.i') + 1) "
                        "WHERE _id = ?"
                    )
                else:
                    statement = "DELETE FROM records WHERE _id = ?"
                for document_id in payload:
                    cursor = resource.execute(statement, (document_id,))
                    resource.commit()
                    if cursor.rowcount != 1:
                        raise AssertionError("raw SQLite mutation missed")
                    result += 1
            elif phase == "update":
                result = 0
                for document_id in payload:
                    update_result = resource[1].update_one(
                        {"_id": document_id},
                        {"$inc": {"i": 1}},
                    )
                    if (
                        not update_result.acknowledged
                        or update_result.modified_count != 1
                    ):
                        raise AssertionError("MongoDB update was not acknowledged")
                    result += 1
            else:
                result = 0
                for document_id in payload:
                    delete_result = resource[1].delete_one({"_id": document_id})
                    if (
                        not delete_result.acknowledged
                        or delete_result.deleted_count != 1
                    ):
                        raise AssertionError("MongoDB delete was not acknowledged")
                    result += 1
        else:  # pragma: no cover - guarded by callers
            raise ValueError("unsupported benchmark phase: {0}".format(phase))
        elapsed = time.perf_counter() - started
        result_queue.put((worker, "ok", result, elapsed, os.getpid()))
    except BaseException as exc:
        failure = (worker, "error", type(exc).__name__, str(exc))
        ready_queue.put(failure)
        result_queue.put(failure)
    finally:
        if backend == "raw-sqlite" and resource is not None:
            resource.close()
        elif backend == "mongodb" and resource is not None:
            resource[0].close()


def _run_reference_processes(
    phase,
    backend,
    database_path,
    batches,
    mongo_uri=None,
    database_name=None,
):
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_reference_process_worker,
            args=(
                phase,
                backend,
                database_path,
                mongo_uri,
                database_name,
                worker,
                batch,
                ready_queue,
                start_event,
                result_queue,
            ),
        )
        for worker, batch in enumerate(batches)
    ]
    try:
        for process in processes:
            process.start()
        ready = [ready_queue.get(timeout=120) for _process in processes]
        failures = [message for message in ready if message[1] != "ready"]
        if failures:
            raise RuntimeError(
                "{0} {1} setup failed: {2!r}".format(
                    backend,
                    phase,
                    failures,
                )
            )
        start_event.set()
        outcomes = [result_queue.get(timeout=180) for _process in processes]
        failures = [message for message in outcomes if message[1] != "ok"]
        if failures:
            raise RuntimeError(
                "{0} {1} failed: {2!r}".format(backend, phase, failures)
            )
        ordered = sorted(outcomes)
        process_ids = [message[4] for message in ordered]
        if len(set(process_ids)) != len(processes):
            raise AssertionError(
                "{0} {1} did not use distinct processes".format(backend, phase)
            )
        return (
            max(message[3] for message in ordered),
            [message[2] for message in ordered],
            process_ids,
        )
    finally:
        start_event.set()
        for process in processes:
            if process.pid is None:
                continue
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join()
        ready_queue.close()
        ready_queue.join_thread()
        result_queue.close()
        result_queue.join_thread()


def _file_size(path):
    if not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, filenames in os.walk(path):
        for filename in filenames:
            total += os.path.getsize(os.path.join(root, filename))
    return total


def _documents_by_id(documents, context):
    try:
        by_id = {document["_id"]: document for document in documents}
    except (KeyError, TypeError):
        raise AssertionError("{0} returned a malformed document".format(context))
    if len(by_id) != len(documents):
        raise AssertionError("{0} returned duplicate IDs".format(context))
    return by_id


def _validate_initial_documents(documents, expected_documents, context):
    expected = _documents_by_id(expected_documents, "generated workload")
    actual = _documents_by_id(documents, context)
    if actual != expected:
        raise AssertionError("{0} returned different documents".format(context))


def _validate_final_documents(documents, source_documents):
    expected_documents = []
    for source in source_documents:
        document = dict(source)
        group_number = int(document["group"][1:])
        if group_number == 2:
            continue
        if group_number == 1:
            document["i"] += 1
        expected_documents.append(document)
    expected = _documents_by_id(expected_documents, "expected final state")
    actual = _documents_by_id(documents, "reopened final state")
    if actual != expected:
        raise AssertionError("final documents did not match the expected state")


def _sqlite_connection_settings(conn):
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    return {
        "journal_mode": str(journal_mode).upper(),
        "synchronous": _SQLITE_SYNCHRONOUS_NAMES.get(
            synchronous,
            str(synchronous),
        ),
    }


def _collapse_sqlite_settings(settings):
    collapsed = {}
    for key in ("journal_mode", "synchronous"):
        values = sorted({item[key] for item in settings})
        collapsed[key] = values[0] if len(values) == 1 else values
    return collapsed


def _tinymongo_sqlite_settings(collection, backend):
    engine = collection.parent.engine
    connections = []
    try:
        if backend == "sqlite":
            connections.append(engine._connect())
        else:
            connections.append(engine._manifest_connect())
            connections.extend(shard._connect() for shard in engine._shards)
        return _collapse_sqlite_settings(
            [_sqlite_connection_settings(conn) for conn in connections]
        )
    finally:
        for conn in connections:
            conn.close()


def _result(
    backend,
    documents,
    queries,
    insert_seconds,
    read_seconds,
    read_count,
    point_latencies,
    update_seconds,
    updated_docs,
    delete_seconds,
    deleted_docs,
    remaining_docs,
    file_kib,
    persistence_verified,
    durability,
    insert_mode="single bulk",
    insert_workers=1,
    insert_batches=1,
):
    return {
        "backend": backend,
        "label": _BACKEND_LABELS[backend],
        "available": True,
        "documents": documents,
        "queries": queries,
        "insert_seconds": insert_seconds,
        "insert_docs_per_second": (
            documents / insert_seconds if insert_seconds else 0.0
        ),
        "read_seconds": read_seconds,
        "read_count": read_count,
        "read_docs_per_second": read_count / read_seconds if read_seconds else 0.0,
        "point_reads_per_second": (
            queries / sum(point_latencies) if point_latencies else 0.0
        ),
        "point_avg_ms": statistics.mean(point_latencies) * 1000,
        "point_p95_ms": _percentile(point_latencies, 95) * 1000,
        "update_seconds": update_seconds,
        "update_docs_per_second": (
            updated_docs / update_seconds if update_seconds else 0.0
        ),
        "updated_docs": updated_docs,
        "delete_seconds": delete_seconds,
        "delete_docs_per_second": (
            deleted_docs / delete_seconds if delete_seconds else 0.0
        ),
        "deleted_docs": deleted_docs,
        "remaining_docs": remaining_docs,
        "file_kib": file_kib,
        "persistence_verified": persistence_verified,
        "durability": durability,
        "insert_mode": insert_mode,
        "insert_workers": insert_workers,
        "insert_batches": insert_batches,
    }


def _unavailable(backend, reason):
    return {
        "backend": backend,
        "label": _BACKEND_LABELS[backend],
        "available": False,
        "reason": reason,
    }


def _availability_error(backend, mongo_uri):
    dependencies = {
        "duckdb": ("duckdb",),
        "parquet": ("duckdb", "pyarrow"),
    }
    for dependency in dependencies.get(backend, ()):
        try:
            __import__(dependency)
        except ImportError:
            return "install the {0} benchmark dependency".format(dependency)

    if backend == "raw-sqlite":
        conn = sqlite3.connect(":memory:")
        try:
            try:
                value = conn.execute(
                    "SELECT json_extract('{\"value\": 1}', '$.value')"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                return "the Python SQLite build does not provide JSON functions"
            if value != 1:
                return "the Python SQLite JSON functions returned an invalid value"
        finally:
            conn.close()

    if backend == "mongodb":
        if not mongo_uri:
            return "set TINYMONGO_MONGODB_URI or pass --mongo-uri"
        try:
            __import__("pymongo")
        except ImportError:
            return "install the pymongo benchmark dependency"
    return None


def _open_tinymongo_client(backend, database_path, sqlite_shards):
    if backend == "memory":
        return tm.TinyMongoClient(backend="memory")
    kwargs = {}
    if backend == "sqlite-sharded":
        kwargs["sqlite_shards"] = sqlite_shards
    return tm.TinyMongoClient(database_path, backend=backend, **kwargs)


def _run_tinymongo_backend(
    backend,
    documents,
    queries,
    work_root,
    sqlite_shards,
    insert_workers,
):
    run_root = tempfile.mkdtemp(prefix="{0}-".format(backend), dir=work_root)
    database_path = os.path.join(run_root, "database")
    source_documents = _docs(documents)
    document_batches = _document_batches(source_documents, insert_workers)
    expected_updates = _group_count(documents, 1)
    expected_deletes = _group_count(documents, 2)
    expected_remaining = documents - expected_deletes
    client = _open_tinymongo_client(backend, database_path, sqlite_shards)
    try:
        collection = client.loadtest.records
        collection.build_table()
        durability = {"policy": "backend default"}
        if backend in ("sqlite", "sqlite-sharded"):
            durability = _tinymongo_sqlite_settings(collection, backend)
        elif backend == "memory":
            durability = {"policy": "process memory only"}

        if backend == "sqlite-sharded":
            for worker, batch in enumerate(document_batches):
                routes = {
                    collection.parent.engine._shard_index(document["_id"])
                    for document in batch
                }
                if routes != {worker}:
                    raise AssertionError("concurrent insert batch was not shard-affine")

        insert_process_ids = None
        if backend != "memory":
            insert_seconds, inserted_id_batches, insert_process_ids = (
                _run_tinymongo_insert_processes(
                    backend,
                    database_path,
                    sqlite_shards,
                    document_batches,
                )
            )
        else:
            insert_seconds, insert_results = _run_simultaneously(
                document_batches,
                lambda _worker, batch, _resource: collection.insert_many(batch),
            )
            inserted_id_batches = [
                insert_result.inserted_ids for insert_result in insert_results
            ]
        for batch, inserted_ids in zip(document_batches, inserted_id_batches):
            if inserted_ids != [document["_id"] for document in batch]:
                raise AssertionError("TinyMongo insert returned the wrong IDs")

        read_all_process_ids = None
        if backend != "memory":
            read_seconds, read_all_results, read_all_process_ids = (
                _run_tinymongo_read_all_processes(
                    backend,
                    database_path,
                    sqlite_shards,
                    insert_workers,
                )
            )
            for worker, worker_documents in enumerate(read_all_results):
                _validate_initial_documents(
                    worker_documents,
                    source_documents,
                    "TinyMongo read-all worker {0}".format(worker),
                )
            all_documents = read_all_results[0]
            read_count = documents * insert_workers
        else:
            read_seconds, all_documents = _time_call(
                lambda: list(collection.find({}))
            )
            _validate_initial_documents(
                all_documents,
                source_documents,
                "TinyMongo read-all",
            )
            read_count = len(all_documents)

        targets = _point_targets(source_documents, queries)
        point_process_ids = None
        point_batch_sizes = None
        point_wall_seconds = None
        if backend != "memory":
            (
                point_wall_seconds,
                point_latencies,
                point_process_ids,
                point_batch_sizes,
            ) = _run_tinymongo_point_read_processes(
                backend,
                database_path,
                sqlite_shards,
                targets,
                insert_workers,
            )
        else:
            warmed = collection.find_one({"_id": targets[0]})
            if warmed is None or warmed.get("_id") != targets[0]:
                raise AssertionError("TinyMongo point-read warm-up missed")
            point_latencies = []
            for target in targets:
                elapsed, found = _time_call(
                    lambda target=target: collection.find_one({"_id": target})
                )
                if found is None or found.get("_id") != target:
                    raise AssertionError(
                        "TinyMongo point read missed {0}".format(target)
                    )
                point_latencies.append(elapsed)

        update_process_ids = None
        delete_process_ids = None
        if backend != "memory":
            update_batches = _mutation_target_batches(
                source_documents,
                "g1",
                insert_workers,
            )
            update_seconds, updated_docs, update_process_ids = (
                _run_tinymongo_mutation_processes(
                    "update",
                    backend,
                    database_path,
                    sqlite_shards,
                    update_batches,
                )
            )
            delete_batches = _mutation_target_batches(
                source_documents,
                "g2",
                insert_workers,
            )
            delete_seconds, deleted_docs, delete_process_ids = (
                _run_tinymongo_mutation_processes(
                    "delete",
                    backend,
                    database_path,
                    sqlite_shards,
                    delete_batches,
                )
            )
        else:
            update_seconds, update_result = _time_call(
                lambda: collection.update_many(
                    {"group": "g1"},
                    {"$inc": {"i": 1}},
                )
            )
            if update_result.matched_count != update_result.modified_count:
                raise AssertionError("TinyMongo update returned the wrong count")
            updated_docs = update_result.modified_count
            delete_seconds, delete_result = _time_call(
                lambda: collection.delete_many({"group": "g2"})
            )
            deleted_docs = delete_result.deleted_count
        if updated_docs != expected_updates:
            raise AssertionError("TinyMongo update returned the wrong count")
        if deleted_docs != expected_deletes:
            raise AssertionError("TinyMongo delete returned the wrong count")
        remaining_docs = collection.count_documents({})
        if remaining_docs != expected_remaining:
            raise AssertionError("TinyMongo retained the wrong document count")
        if backend == "memory":
            _validate_final_documents(
                list(collection.find({})),
                source_documents,
            )
    finally:
        client.close()

    persistence_verified = False
    if backend != "memory":
        verifier = _open_tinymongo_client(backend, database_path, sqlite_shards)
        try:
            _validate_final_documents(
                list(verifier.loadtest.records.find({})),
                source_documents,
            )
            persistence_verified = True
        finally:
            verifier.close()

    file_kib = None
    if backend != "memory":
        file_kib = _file_size(run_root) / 1024.0
    benchmark_result = _result(
        backend,
        documents,
        queries,
        insert_seconds,
        read_seconds,
        read_count,
        point_latencies,
        update_seconds,
        updated_docs,
        delete_seconds,
        deleted_docs,
        remaining_docs,
        file_kib,
        persistence_verified,
        durability,
        insert_mode="{0} concurrent insert_many bulks".format(insert_workers),
        insert_workers=insert_workers,
        insert_batches=len(document_batches),
    )
    if backend != "memory":
        qualifier = "shard-affine " if backend == "sqlite-sharded" else ""
        benchmark_result["insert_mode"] = (
            "{0} spawned {1}insert_many bulks".format(
                insert_workers,
                qualifier,
            )
        )
        benchmark_result["insert_process_ids"] = insert_process_ids
        benchmark_result["read_mode"] = (
            "{0} spawned full-collection scans".format(insert_workers)
        )
        benchmark_result["read_process_ids"] = read_all_process_ids
        benchmark_result["point_mode"] = (
            "{0} spawned {1}exact-ID streams".format(
                insert_workers,
                qualifier,
            )
        )
        benchmark_result["point_process_ids"] = point_process_ids
        benchmark_result["point_batch_sizes"] = point_batch_sizes
        benchmark_result["point_wall_seconds"] = point_wall_seconds
        benchmark_result["point_reads_per_second"] = (
            queries / point_wall_seconds if point_wall_seconds else 0.0
        )
        benchmark_result["update_mode"] = (
            "{0} spawned disjoint-ID streams".format(insert_workers)
        )
        benchmark_result["update_process_ids"] = update_process_ids
        benchmark_result["delete_mode"] = (
            "{0} spawned disjoint-ID streams".format(insert_workers)
        )
        benchmark_result["delete_process_ids"] = delete_process_ids
    if backend == "sqlite-sharded":
        benchmark_result["label"] = "TinyMongo SQLite-sharded ({0})".format(
            sqlite_shards
        )
    benchmark_result["insert_batch_sizes"] = [len(batch) for batch in document_batches]
    return benchmark_result


def _run_raw_sqlite(documents, queries, work_root, insert_workers):
    run_root = tempfile.mkdtemp(prefix="raw-sqlite-", dir=work_root)
    database_path = os.path.join(run_root, "database.sqlite")
    source_documents = _docs(documents)
    document_batches = _document_batches(source_documents, insert_workers)
    expected_updates = _group_count(documents, 1)
    expected_deletes = _group_count(documents, 2)
    expected_remaining = documents - expected_deletes
    setup_conn = sqlite3.connect(database_path, timeout=30)
    try:
        setup_conn.execute("PRAGMA journal_mode=WAL")
        setup_conn.execute("PRAGMA synchronous=NORMAL")
        setup_conn.execute("PRAGMA busy_timeout=30000")
        durability = _sqlite_connection_settings(setup_conn)
        setup_conn.execute(
            "CREATE TABLE records (_id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        setup_conn.commit()
    finally:
        setup_conn.close()

    insert_seconds, inserted_ids, insert_process_ids = _run_reference_processes(
        "insert",
        "raw-sqlite",
        database_path,
        document_batches,
    )
    expected_inserted_ids = [
        [document["_id"] for document in batch] for batch in document_batches
    ]
    if inserted_ids != expected_inserted_ids:
        raise AssertionError("raw SQLite inserted the wrong document IDs")

    read_seconds, read_all_results, read_process_ids = _run_reference_processes(
        "read-all",
        "raw-sqlite",
        database_path,
        [None for _worker in range(insert_workers)],
    )
    for worker, all_documents in enumerate(read_all_results):
        _validate_initial_documents(
            all_documents,
            source_documents,
            "raw SQLite read-all worker {0}".format(worker),
        )
    all_documents = read_all_results[0]
    read_count = documents * insert_workers

    targets = _point_targets(source_documents, queries)
    target_batches = [[] for _worker in range(insert_workers)]
    for target in targets:
        target_batches[_shard_index(target, insert_workers)].append(target)
    point_wall_seconds, point_results, point_process_ids = (
        _run_reference_processes(
            "point",
            "raw-sqlite",
            database_path,
            target_batches,
        )
    )
    point_latencies = [
        latency for worker_latencies in point_results for latency in worker_latencies
    ]

    update_batches = _mutation_target_batches(
        source_documents,
        "g1",
        insert_workers,
    )
    update_seconds, update_results, update_process_ids = _run_reference_processes(
        "update",
        "raw-sqlite",
        database_path,
        update_batches,
    )
    updated_docs = sum(update_results)
    if updated_docs != expected_updates:
        raise AssertionError("raw SQLite update returned the wrong count")
    delete_batches = _mutation_target_batches(
        source_documents,
        "g2",
        insert_workers,
    )
    delete_seconds, delete_results, delete_process_ids = _run_reference_processes(
        "delete",
        "raw-sqlite",
        database_path,
        delete_batches,
    )
    deleted_docs = sum(delete_results)
    if deleted_docs != expected_deletes:
        raise AssertionError("raw SQLite delete returned the wrong count")

    conn = sqlite3.connect(database_path, timeout=30)
    try:
        remaining_docs = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        if remaining_docs != expected_remaining:
            raise AssertionError("raw SQLite retained the wrong document count")
    finally:
        conn.close()

    verifier = sqlite3.connect(database_path, timeout=30)
    try:
        final_documents = [
            json.loads(row[0])
            for row in verifier.execute("SELECT data FROM records").fetchall()
        ]
        _validate_final_documents(final_documents, source_documents)
    finally:
        verifier.close()

    benchmark_result = _result(
        "raw-sqlite",
        documents,
        queries,
        insert_seconds,
        read_seconds,
        read_count,
        point_latencies,
        update_seconds,
        updated_docs,
        delete_seconds,
        deleted_docs,
        remaining_docs,
        _file_size(run_root) / 1024.0,
        True,
        durability,
        insert_mode="{0} spawned executemany bulks".format(insert_workers),
        insert_workers=insert_workers,
        insert_batches=len(document_batches),
    )
    benchmark_result["insert_process_ids"] = insert_process_ids
    benchmark_result["read_mode"] = (
        "{0} spawned full-collection scans".format(insert_workers)
    )
    benchmark_result["read_process_ids"] = read_process_ids
    benchmark_result["point_mode"] = (
        "{0} spawned exact-ID streams".format(insert_workers)
    )
    benchmark_result["point_process_ids"] = point_process_ids
    benchmark_result["point_batch_sizes"] = [
        len(batch) for batch in target_batches
    ]
    benchmark_result["point_wall_seconds"] = point_wall_seconds
    benchmark_result["point_reads_per_second"] = (
        queries / point_wall_seconds if point_wall_seconds else 0.0
    )
    benchmark_result["update_mode"] = (
        "{0} spawned disjoint-ID streams".format(insert_workers)
    )
    benchmark_result["update_process_ids"] = update_process_ids
    benchmark_result["delete_mode"] = (
        "{0} spawned disjoint-ID streams".format(insert_workers)
    )
    benchmark_result["delete_process_ids"] = delete_process_ids
    benchmark_result["insert_batch_sizes"] = [len(batch) for batch in document_batches]
    return benchmark_result


def _run_mongodb(documents, queries, mongo_uri, insert_workers):
    import pymongo
    from pymongo.write_concern import WriteConcern

    client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except Exception as exc:
        client.close()
        return _unavailable(
            "mongodb",
            "MongoDB is unavailable: {0}".format(exc),
        )

    database_name = "tinymongo_storage_{0}_{1}".format(
        os.getpid(),
        uuid.uuid4().hex[:10],
    )
    write_concern = WriteConcern(w=1, j=True)
    source_documents = _docs(documents)
    document_batches = _document_batches(source_documents, insert_workers)
    expected_updates = _group_count(documents, 1)
    expected_deletes = _group_count(documents, 2)
    expected_remaining = documents - expected_deletes
    verifier = None
    try:
        database = client.get_database(database_name, write_concern=write_concern)
        database.create_collection("records")
        collection = database.records
        insert_seconds, inserted_ids, insert_process_ids = (
            _run_reference_processes(
                "insert",
                "mongodb",
                None,
                document_batches,
                mongo_uri=mongo_uri,
                database_name=database_name,
            )
        )
        expected_inserted_ids = [
            [document["_id"] for document in batch]
            for batch in document_batches
        ]
        if inserted_ids != expected_inserted_ids:
            raise AssertionError("MongoDB inserted the wrong document IDs")

        read_seconds, read_all_results, read_process_ids = (
            _run_reference_processes(
                "read-all",
                "mongodb",
                None,
                [None for _worker in range(insert_workers)],
                mongo_uri=mongo_uri,
                database_name=database_name,
            )
        )
        for worker, all_documents in enumerate(read_all_results):
            _validate_initial_documents(
                all_documents,
                source_documents,
                "MongoDB read-all worker {0}".format(worker),
            )
        all_documents = read_all_results[0]
        read_count = documents * insert_workers

        targets = _point_targets(source_documents, queries)
        target_batches = [[] for _worker in range(insert_workers)]
        for target in targets:
            target_batches[_shard_index(target, insert_workers)].append(target)
        point_wall_seconds, point_results, point_process_ids = (
            _run_reference_processes(
                "point",
                "mongodb",
                None,
                target_batches,
                mongo_uri=mongo_uri,
                database_name=database_name,
            )
        )
        point_latencies = [
            latency
            for worker_latencies in point_results
            for latency in worker_latencies
        ]

        update_batches = _mutation_target_batches(
            source_documents,
            "g1",
            insert_workers,
        )
        update_seconds, update_results, update_process_ids = (
            _run_reference_processes(
                "update",
                "mongodb",
                None,
                update_batches,
                mongo_uri=mongo_uri,
                database_name=database_name,
            )
        )
        updated_docs = sum(update_results)
        if updated_docs != expected_updates:
            raise AssertionError("MongoDB update returned the wrong count")

        delete_batches = _mutation_target_batches(
            source_documents,
            "g2",
            insert_workers,
        )
        delete_seconds, delete_results, delete_process_ids = (
            _run_reference_processes(
                "delete",
                "mongodb",
                None,
                delete_batches,
                mongo_uri=mongo_uri,
                database_name=database_name,
            )
        )
        deleted_docs = sum(delete_results)
        if deleted_docs != expected_deletes:
            raise AssertionError("MongoDB delete returned the wrong count")
        remaining_docs = collection.count_documents({})
        if remaining_docs != expected_remaining:
            raise AssertionError("MongoDB retained the wrong document count")

        client.close()
        client = None
        verifier = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
        verifier.admin.command("ping")
        final_documents = list(verifier[database_name].records.find({}))
        _validate_final_documents(final_documents, source_documents)
        persistence_verified = True
        benchmark_result = _result(
            "mongodb",
            documents,
            queries,
            insert_seconds,
            read_seconds,
            read_count,
            point_latencies,
            update_seconds,
            updated_docs,
            delete_seconds,
            deleted_docs,
            remaining_docs,
            None,
            persistence_verified,
            {"write_concern": {"w": 1, "j": True}},
            insert_mode="{0} spawned acknowledged insert_many bulks".format(
                insert_workers
            ),
            insert_workers=insert_workers,
            insert_batches=len(document_batches),
        )
        benchmark_result["insert_process_ids"] = insert_process_ids
        benchmark_result["read_mode"] = (
            "{0} spawned full-collection scans".format(insert_workers)
        )
        benchmark_result["read_process_ids"] = read_process_ids
        benchmark_result["point_mode"] = (
            "{0} spawned exact-ID streams".format(insert_workers)
        )
        benchmark_result["point_process_ids"] = point_process_ids
        benchmark_result["point_batch_sizes"] = [
            len(batch) for batch in target_batches
        ]
        benchmark_result["point_wall_seconds"] = point_wall_seconds
        benchmark_result["point_reads_per_second"] = (
            queries / point_wall_seconds if point_wall_seconds else 0.0
        )
        benchmark_result["update_mode"] = (
            "{0} spawned disjoint-ID streams".format(insert_workers)
        )
        benchmark_result["update_process_ids"] = update_process_ids
        benchmark_result["delete_mode"] = (
            "{0} spawned disjoint-ID streams".format(insert_workers)
        )
        benchmark_result["delete_process_ids"] = delete_process_ids
        benchmark_result["insert_batch_sizes"] = [
            len(batch) for batch in document_batches
        ]
        return benchmark_result
    finally:
        cleanup_client = verifier if verifier is not None else client
        if cleanup_client is not None:
            try:
                cleanup_client.drop_database(database_name)
            finally:
                cleanup_client.close()


def run_backend(
    backend,
    doc_count,
    query_count,
    work_root,
    sqlite_shards=4,
    insert_workers=4,
    mongo_uri=None,
):
    if backend == "memory":
        return _unavailable(
            backend,
            "the memory backend is process-local and cannot expose one shared "
            "database to spawned workers",
        )
    if backend == "duckdb":
        return _unavailable(
            backend,
            "DuckDB does not support this shared writable database across "
            "multiple processes",
        )
    if backend == "sqlite-sharded" and insert_workers != sqlite_shards:
        raise ValueError(
            "insert_workers must equal sqlite_shards for shard-affine inserts"
        )
    availability_error = _availability_error(backend, mongo_uri)
    if availability_error:
        return _unavailable(backend, availability_error)
    if backend == "raw-sqlite":
        return _run_raw_sqlite(
            doc_count,
            query_count,
            work_root,
            insert_workers,
        )
    if backend == "mongodb":
        return _run_mongodb(
            doc_count,
            query_count,
            mongo_uri,
            insert_workers,
        )
    return _run_tinymongo_backend(
        backend,
        doc_count,
        query_count,
        work_root,
        sqlite_shards,
        insert_workers,
    )


def _aggregate_runs(backend, runs):
    if not runs:
        raise AssertionError("cannot aggregate an empty benchmark run")
    if not runs[0].get("available"):
        return runs[0]
    result = dict(runs[0])
    for key in _MEDIAN_KEYS:
        values = [run[key] for run in runs if run.get(key) is not None]
        result[key] = statistics.median(values) if values else None
    result["insert_docs_per_second"] = (
        result["documents"] / result["insert_seconds"]
        if result["insert_seconds"]
        else 0.0
    )
    result["read_docs_per_second"] = (
        result["read_count"] / result["read_seconds"]
        if result["read_seconds"]
        else 0.0
    )
    result["update_docs_per_second"] = (
        result["updated_docs"] / result["update_seconds"]
        if result["update_seconds"]
        else 0.0
    )
    result["delete_docs_per_second"] = (
        result["deleted_docs"] / result["delete_seconds"]
        if result["delete_seconds"]
        else 0.0
    )
    result["repeat_count"] = len(runs)
    result["persistence_verified"] = all(run["persistence_verified"] for run in runs)
    point_wall_times = [
        run["point_wall_seconds"]
        for run in runs
        if run.get("point_wall_seconds") is not None
    ]
    if point_wall_times:
        result["point_wall_seconds"] = statistics.median(point_wall_times)
        result["point_reads_per_second"] = (
            result["queries"] / result["point_wall_seconds"]
            if result["point_wall_seconds"]
            else 0.0
        )
    result["runs"] = runs
    result["backend"] = backend
    return result


def run_benchmark(
    backends,
    documents,
    queries,
    repeats,
    work_root,
    sqlite_shards=4,
    insert_workers=4,
    mongo_uri=None,
):
    if "sqlite-sharded" in backends and insert_workers != sqlite_shards:
        raise ValueError(
            "insert_workers must equal sqlite_shards for shard-affine inserts"
        )
    _document_batches(_docs(documents), insert_workers)
    runs = {backend: [] for backend in backends}
    unavailable = {}
    for repeat in range(repeats):
        offset = repeat % len(backends)
        ordered = list(backends[offset:]) + list(backends[:offset])
        for order, backend in enumerate(ordered):
            if backend in unavailable:
                continue
            result = run_backend(
                backend,
                documents,
                queries,
                work_root,
                sqlite_shards=sqlite_shards,
                insert_workers=insert_workers,
                mongo_uri=mongo_uri,
            )
            if not result.get("available"):
                unavailable[backend] = result
                continue
            result["repeat"] = repeat + 1
            result["execution_order"] = order + 1
            runs[backend].append(result)

    results = []
    for backend in backends:
        if backend in unavailable:
            results.append(unavailable[backend])
        else:
            results.append(_aggregate_runs(backend, runs[backend]))
    return {
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
            "sqlite": sqlite3.sqlite_version,
        },
        "workload": {
            "documents": documents,
            "queries": queries,
            "repeats": repeats,
            "sqlite_shards": sqlite_shards,
            "workers": insert_workers,
            "insert_workers": insert_workers,
        },
        "results": results,
    }


def format_markdown(results):
    rows = [
        "| Backend | Insert workload | Insert docs/s | Read-all docs/s | "
        "Point reads/s | Point avg ms | Point p95 ms | Update docs/s | Delete docs/s | "
        "Final KiB |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in results:
        if not result.get("available"):
            rows.append(
                "| {label} | not run | not run | not run | not run | not run | "
                "not run | not run | not run | N/A |".format(**result)
            )
            continue
        file_size = (
            "N/A"
            if result["file_kib"] is None
            else "{0:,.1f}".format(result["file_kib"])
        )
        rows.append(
            "| {label} | {insert_mode} | {insert_docs_per_second:,.0f} | "
            "{read_docs_per_second:,.0f} | {point_reads_per_second:,.0f} | "
            "{point_avg_ms:.3f} | "
            "{point_p95_ms:.3f} | {update_docs_per_second:,.0f} | "
            "{delete_docs_per_second:,.0f} | {file_size} |".format(
                file_size=file_size, **result
            )
        )
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=int, default=1000)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--sqlite-shards", type=int, default=4)
    parser.add_argument(
        "--workers",
        "--insert-workers",
        dest="insert_workers",
        type=int,
        default=4,
    )
    parser.add_argument("--backend", action="append", choices=BACKENDS)
    parser.add_argument("--work-root")
    parser.add_argument("--mongo-uri", default=os.getenv("TINYMONGO_MONGODB_URI"))
    parser.add_argument("--json-output")
    args = parser.parse_args(argv)
    requested = tuple(args.backend or BACKENDS)
    if args.docs <= 0 or args.queries <= 0 or args.repeats <= 0:
        parser.error("--docs, --queries, and --repeats must all be positive")
    if not 2 <= args.sqlite_shards <= 64:
        parser.error("--sqlite-shards must be between 2 and 64")
    if not 1 <= args.insert_workers <= 64:
        parser.error("--insert-workers must be between 1 and 64")
    if "sqlite-sharded" in requested and args.insert_workers != args.sqlite_shards:
        parser.error(
            "--insert-workers must equal --sqlite-shards for shard-affine inserts"
        )
    try:
        _document_batches(
            _docs(args.docs),
            args.insert_workers,
        )
    except ValueError as exc:
        parser.error(str(exc))

    def execute(work_root):
        return run_benchmark(
            requested,
            documents=args.docs,
            queries=args.queries,
            repeats=args.repeats,
            work_root=work_root,
            sqlite_shards=args.sqlite_shards,
            insert_workers=args.insert_workers,
            mongo_uri=args.mongo_uri,
        )

    if args.work_root:
        os.makedirs(args.work_root, exist_ok=True)
        payload = execute(args.work_root)
    else:
        with tempfile.TemporaryDirectory(prefix="tinymongo-bench-") as work_root:
            payload = execute(work_root)

    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    print(format_markdown(payload["results"]))
    for result in payload["results"]:
        if not result.get("available"):
            print("{0}: {1}".format(result["label"], result["reason"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
