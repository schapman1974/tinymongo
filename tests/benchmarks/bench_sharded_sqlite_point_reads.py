"""Measure warmed exact-ID reads through Sharded SQLite.

This is a local benchmark, not a pytest test.  It exercises TinyMongo's public
``find_one`` API, validates every returned ID, and uses ``spawn`` so workers
never inherit live SQLite handles.
"""

from __future__ import absolute_import

import argparse
import json
import multiprocessing
import os
import platform
import statistics
import sys
import tempfile
import time

import tinymongo as tm


_PAYLOAD_BYTES = 256
_SEED_BATCH_SIZE = 20000
_WORKER_QUERY_COUNT = 8192
_WORKER_WARM_READS = 2048


def _document_id(index):
    return "document-{0:09d}".format(index)


def _read_indexes(document_count, count, offset=0):
    """Return a reproducible, non-sequential exact-ID workload."""

    return [(offset + index * 7919) % document_count for index in range(count)]


def _open_collection(path, workers):
    client = tm.TinyMongoClient(
        path,
        backend="sqlite-sharded",
        sqlite_shards=workers,
    )
    return client, client.benchmark.records


def _validate_result(result, expected_index):
    expected_id = _document_id(expected_index)
    if result is None or result.get("_id") != expected_id:
        raise AssertionError(
            "point read returned {0!r}, expected _id {1!r}".format(
                result,
                expected_id,
            )
        )
    if result.get("sequence") != expected_index:
        raise AssertionError(
            "point read returned sequence {0!r}, expected {1!r}".format(
                result.get("sequence"),
                expected_index,
            )
        )


def _seed_database(path, document_count, workers):
    client, collection = _open_collection(path, workers)
    try:
        payload = "x" * _PAYLOAD_BYTES
        started = time.perf_counter()
        for start in range(0, document_count, _SEED_BATCH_SIZE):
            stop = min(document_count, start + _SEED_BATCH_SIZE)
            documents = [
                {
                    "_id": _document_id(index),
                    "sequence": index,
                    "bucket": index % 97,
                    "payload": payload,
                }
                for index in range(start, stop)
            ]
            result = collection.insert_many(documents)
            if len(result.inserted_ids) != len(documents):
                raise AssertionError("seed insert returned the wrong ID count")
        elapsed = time.perf_counter() - started
        stored = collection.count_documents({})
        if stored != document_count:
            raise AssertionError(
                "seed stored {0} documents, expected {1}".format(
                    stored,
                    document_count,
                )
            )
        return elapsed
    finally:
        client.close()


def _summary(rates):
    mean = statistics.mean(rates)
    coefficient_of_variation = statistics.pstdev(rates) / mean if mean else 0.0
    return {
        "median_reads_per_second": statistics.median(rates),
        "max_reads_per_second": max(rates),
        "coefficient_of_variation": coefficient_of_variation,
    }


def _run_single_process(path, document_count, reads, repeats, workers):
    client, collection = _open_collection(path, workers)
    try:
        warm_indexes = _read_indexes(
            document_count,
            min(reads, _WORKER_QUERY_COUNT),
            offset=17,
        )
        for index in warm_indexes:
            _validate_result(
                collection.find_one({"_id": _document_id(index)}),
                index,
            )

        runs = []
        for repeat in range(repeats):
            indexes = _read_indexes(
                document_count,
                reads,
                offset=(repeat + 1) * 104729,
            )
            started = time.perf_counter()
            for index in indexes:
                _validate_result(
                    collection.find_one({"_id": _document_id(index)}),
                    index,
                )
            elapsed = time.perf_counter() - started
            runs.append(
                {
                    "run": repeat + 1,
                    "reads": reads,
                    "seconds": elapsed,
                    "reads_per_second": reads / elapsed if elapsed else 0.0,
                }
            )

        result = {"runs": runs}
        result.update(_summary([run["reads_per_second"] for run in runs]))
        return result
    finally:
        client.close()


def _multi_process_worker(
    path,
    document_count,
    workers,
    worker,
    seconds,
    ready_queue,
    start_event,
    result_queue,
):
    client = None
    try:
        client, collection = _open_collection(path, workers)
        indexes = _read_indexes(
            document_count,
            min(document_count, _WORKER_QUERY_COUNT),
            offset=(worker + 1) * 104729,
        )
        for index in indexes[:_WORKER_WARM_READS]:
            _validate_result(
                collection.find_one({"_id": _document_id(index)}),
                index,
            )

        ready_queue.put((worker, "ready"))
        if not start_event.wait(60):
            raise RuntimeError("timed out waiting for the benchmark start")

        started = time.perf_counter()
        deadline = started + seconds
        reads = 0
        while time.perf_counter() < deadline:
            index = indexes[reads % len(indexes)]
            _validate_result(
                collection.find_one({"_id": _document_id(index)}),
                index,
            )
            reads += 1
        elapsed = time.perf_counter() - started
        result_queue.put((worker, "ok", reads, elapsed))
    except BaseException as exc:
        failure = (worker, "error", type(exc).__name__, str(exc))
        ready_queue.put(failure)
        result_queue.put(failure)
    finally:
        if client is not None:
            client.close()


def _run_multi_process_once(
    path,
    document_count,
    seconds,
    workers,
    run_number,
):
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_multi_process_worker,
            args=(
                path,
                document_count,
                workers,
                worker,
                seconds,
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
            raise RuntimeError("worker setup failed: {0!r}".format(failures))

        start_event.set()
        worker_results = [
            result_queue.get(timeout=seconds + 120) for _process in processes
        ]
        failures = [message for message in worker_results if message[1] != "ok"]
        if failures:
            raise RuntimeError("worker point read failed: {0!r}".format(failures))

        ordered = sorted(worker_results)
        elapsed = max(message[3] for message in ordered)
        reads = sum(message[2] for message in ordered)
        return {
            "run": run_number,
            "reads": reads,
            "seconds": elapsed,
            "reads_per_second": reads / elapsed if elapsed else 0.0,
            "workers": [
                {
                    "worker": message[0],
                    "reads": message[2],
                    "seconds": message[3],
                    "reads_per_second": (
                        message[2] / message[3] if message[3] else 0.0
                    ),
                }
                for message in ordered
            ],
        }
    finally:
        start_event.set()
        for process in processes:
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join()
        ready_queue.close()
        ready_queue.join_thread()
        result_queue.close()
        result_queue.join_thread()


def _run_multi_process(
    path,
    document_count,
    seconds,
    repeats,
    workers,
):
    runs = [
        _run_multi_process_once(
            path,
            document_count,
            seconds,
            workers,
            repeat + 1,
        )
        for repeat in range(repeats)
    ]
    result = {"runs": runs}
    result.update(_summary([run["reads_per_second"] for run in runs]))
    return result


def run_benchmark(
    documents,
    reads,
    seconds,
    repeats,
    workers,
    work_root=None,
):
    with tempfile.TemporaryDirectory(
        prefix="tinymongo-sharded-point-reads-",
        dir=work_root,
    ) as folder:
        database_path = os.path.join(folder, "database")
        seed_seconds = _seed_database(database_path, documents, workers)
        single_process = _run_single_process(
            database_path,
            documents,
            reads,
            repeats,
            workers,
        )
        multi_process = _run_multi_process(
            database_path,
            documents,
            seconds,
            repeats,
            workers,
        )

    return {
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
        },
        "workload": {
            "documents": documents,
            "payload_bytes": _PAYLOAD_BYTES,
            "single_reads_per_run": reads,
            "multi_seconds_per_run": seconds,
            "repeats": repeats,
            "workers": workers,
            "start_method": "spawn",
            "seed_seconds": seed_seconds,
        },
        "single_process": single_process,
        "multi_process": multi_process,
    }


def format_markdown(result):
    rows = [
        "| Mode | Run | Reads | Seconds | Reads/s |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    modes = (
        ("single process", result["single_process"]),
        (
            "{0} processes".format(result["workload"]["workers"]),
            result["multi_process"],
        ),
    )
    for label, mode in modes:
        for run in mode["runs"]:
            rows.append(
                "| {0} | {1} | {2:,} | {3:.3f} | {4:,.1f} |".format(
                    label,
                    run["run"],
                    run["reads"],
                    run["seconds"],
                    run["reads_per_second"],
                )
            )

    rows.extend(
        [
            "",
            "| Mode | Median reads/s | Max reads/s | CV |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for label, mode in modes:
        rows.append(
            "| {0} | {1:,.1f} | {2:,.1f} | {3:.2%} |".format(
                label,
                mode["median_reads_per_second"],
                mode["max_reads_per_second"],
                mode["coefficient_of_variation"],
            )
        )
    rows.extend(
        [
            "",
            "Workload: {0:,} warmed documents, {1}-byte payload, "
            "{2} spawned workers".format(
                result["workload"]["documents"],
                result["workload"]["payload_bytes"],
                result["workload"]["workers"],
            ),
        ]
    )
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=int, default=50000)
    parser.add_argument("--reads", type=int, default=10000)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--work-root")
    parser.add_argument("--json-output")
    args = parser.parse_args(argv)
    if min(args.docs, args.reads, args.seconds, args.repeats) <= 0:
        parser.error("document, read, second, and repeat counts must be positive")
    if not 2 <= args.workers <= 64:
        parser.error("--workers must be between 2 and 64")
    if args.work_root and not os.path.isdir(args.work_root):
        parser.error("--work-root must be an existing directory")

    result = run_benchmark(
        documents=args.docs,
        reads=args.reads,
        seconds=args.seconds,
        repeats=args.repeats,
        workers=args.workers,
        work_root=args.work_root,
    )
    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
    print(format_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
