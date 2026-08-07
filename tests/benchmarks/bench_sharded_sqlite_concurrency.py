"""Compare concurrent individual writes on SQLite and Sharded SQLite.

This is a local benchmark, not a pytest test. Each worker uses IDs selected for
one distinct Sharded SQLite file. The ordinary SQLite run uses the same number
of threads and documents, so both paths include TinyMongo's complete public
insert contract.
"""

from __future__ import absolute_import

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import tinymongo as tm


def _routed_ids(engine, workers, writes_per_worker):
    groups = [[] for _ in range(workers)]
    candidate = 0
    while any(len(group) < writes_per_worker for group in groups):
        document_id = "concurrent-{0}".format(candidate)
        shard = engine._shard_index(document_id)
        if shard < workers and len(groups[shard]) < writes_per_worker:
            groups[shard].append(document_id)
        candidate += 1
    return groups


def _run_once(backend, workers, writes_per_worker, work_root=None):
    with tempfile.TemporaryDirectory(
        prefix="tinymongo-{0}-".format(backend),
        dir=work_root,
    ) as folder:
        options = {"sqlite_shards": workers} if backend == "sqlite-sharded" else {}
        client = tm.TinyMongoClient(folder, backend=backend, **options)
        try:
            collection = client.benchmark.records
            collection.insert_one({"_id": "warm-up", "worker": -1})
            if backend == "sqlite-sharded":
                groups = _routed_ids(
                    client.benchmark.engine,
                    workers,
                    writes_per_worker,
                )
            else:
                groups = [
                    [
                        "concurrent-{0}-{1}".format(worker, index)
                        for index in range(writes_per_worker)
                    ]
                    for worker in range(workers)
                ]

            def write_group(worker_and_ids):
                worker, document_ids = worker_and_ids
                for index, document_id in enumerate(document_ids):
                    collection.insert_one(
                        {
                            "_id": document_id,
                            "worker": worker,
                            "sequence": index,
                        }
                    )

            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as executor:
                list(executor.map(write_group, enumerate(groups)))
            seconds = time.perf_counter() - started
            written = workers * writes_per_worker
            if collection.count_documents({}) != written + 1:
                raise AssertionError("Concurrent benchmark lost documents")
            return {
                "seconds": seconds,
                "writes_per_second": written / seconds if seconds else 0.0,
            }
        finally:
            client.close()


def run_benchmark(workers, writes_per_worker, repeats, work_root=None):
    engines = {}
    for backend in ("sqlite", "sqlite-sharded"):
        runs = [
            _run_once(
                backend,
                workers,
                writes_per_worker,
                work_root=work_root,
            )
            for _ in range(repeats)
        ]
        engines[backend] = {
            "median_writes_per_second": statistics.median(
                run["writes_per_second"] for run in runs
            ),
            "runs": runs,
        }
    stable = engines["sqlite"]["median_writes_per_second"]
    sharded = engines["sqlite-sharded"]["median_writes_per_second"]
    return {
        "workers": workers,
        "writes_per_worker": writes_per_worker,
        "repeats": repeats,
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
        },
        "engines": engines,
        "sharded_speedup": sharded / stable if stable else 0.0,
    }


def format_markdown(result):
    rows = [
        "| Backend | Median writes/s | Runs |",
        "| --- | ---: | --- |",
    ]
    for backend in ("sqlite", "sqlite-sharded"):
        engine = result["engines"][backend]
        rates = ", ".join(
            "{0:,.1f}".format(run["writes_per_second"]) for run in engine["runs"]
        )
        rows.append(
            "| {0} | {1:,.1f} | {2} |".format(
                backend,
                engine["median_writes_per_second"],
                rates,
            )
        )
    rows.extend(
        [
            "",
            "Sharded speedup: {0:.2f}x".format(result["sharded_speedup"]),
            "Workload: {0} workers x {1} acknowledged insert_one writes".format(
                result["workers"],
                result["writes_per_worker"],
            ),
        ]
    )
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--writes-per-worker", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--work-root")
    parser.add_argument("--json-output")
    args = parser.parse_args(argv)
    if not 2 <= args.workers <= 64:
        parser.error("--workers must be between 2 and 64")
    if min(args.writes_per_worker, args.repeats) <= 0:
        parser.error("write and repeat counts must be positive")

    result = run_benchmark(
        args.workers,
        args.writes_per_worker,
        args.repeats,
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
