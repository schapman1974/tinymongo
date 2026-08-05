"""Measure successive fixed-size SQLite ``insert_many`` batches.

TM-040 is about the shape of the throughput curve, not one large-batch
average. This local benchmark therefore reports each collection-size window
and counts how many existing payloads the insert preflight BSON-decodes.
"""

from __future__ import absolute_import

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time

import tinymongo as tm
import tinymongo.table_backends as table_backends


def _documents(start, count):
    return [
        {
            "_id": "doc-{0}".format(index),
            "show_id": index % 1000,
            "title": "Episode {0}".format(index),
            "published": index,
            "payload": {"tags": ["python", "database"], "active": True},
        }
        for index in range(start, start + count)
    ]


def _run_once(documents, batch_size, window_size, work_root=None):
    batch_results = []
    decoded_existing_rows = 0
    original_loads = table_backends._json_loads

    def tracked_loads(value):
        nonlocal decoded_existing_rows
        decoded_existing_rows += 1
        return original_loads(value)

    with tempfile.TemporaryDirectory(
        prefix="tinymongo-tm040-",
        dir=work_root,
    ) as folder:
        client = tm.TinyMongoClient(folder, backend="sqlite")
        try:
            collection = client.benchmark.records
            table_backends._json_loads = tracked_loads
            started = time.perf_counter()
            try:
                for offset in range(0, documents, batch_size):
                    count = min(batch_size, documents - offset)
                    batch_documents = _documents(offset, count)
                    batch_started = time.perf_counter()
                    result = collection.insert_many(batch_documents)
                    seconds = time.perf_counter() - batch_started
                    if len(result.inserted_ids) != count:
                        raise AssertionError("TinyMongo inserted the wrong batch size")
                    batch_results.append(
                        {
                            "start": offset,
                            "end": offset + count,
                            "documents": count,
                            "seconds": seconds,
                        }
                    )
            finally:
                total_seconds = time.perf_counter() - started
                table_backends._json_loads = original_loads

            conn = collection.parent.engine._connect()
            try:
                stored = conn.execute('SELECT COUNT(*) FROM "records"').fetchone()[0]
            finally:
                conn.close()
            if stored != documents:
                raise AssertionError(
                    "TinyMongo stored {0} documents, expected {1}".format(
                        stored,
                        documents,
                    )
                )
        finally:
            table_backends._json_loads = original_loads
            client.close()

    windows = []
    for window_start in range(0, documents, window_size):
        window_end = min(window_start + window_size, documents)
        batches = [
            batch
            for batch in batch_results
            if batch["start"] >= window_start and batch["end"] <= window_end
        ]
        seconds = sum(batch["seconds"] for batch in batches)
        count = sum(batch["documents"] for batch in batches)
        windows.append(
            {
                "start": window_start,
                "end": window_end,
                "documents": count,
                "seconds": seconds,
                "docs_per_second": count / seconds if seconds else 0.0,
            }
        )

    return {
        "total_seconds": total_seconds,
        "docs_per_second": documents / total_seconds if total_seconds else 0.0,
        "decoded_existing_rows": decoded_existing_rows,
        "windows": windows,
    }


def run_benchmark(documents, batch_size, window_size, repeats, work_root=None):
    runs = [
        _run_once(documents, batch_size, window_size, work_root=work_root)
        for _ in range(repeats)
    ]
    windows = []
    for index in range(len(runs[0]["windows"])):
        samples = [run["windows"][index] for run in runs]
        median_seconds = statistics.median(sample["seconds"] for sample in samples)
        windows.append(
            {
                "start": samples[0]["start"],
                "end": samples[0]["end"],
                "documents": samples[0]["documents"],
                "median_seconds": median_seconds,
                "median_docs_per_second": (
                    samples[0]["documents"] / median_seconds if median_seconds else 0.0
                ),
            }
        )

    median_total_seconds = statistics.median(run["total_seconds"] for run in runs)
    slowdowns = [
        run["windows"][0]["docs_per_second"] / run["windows"][-1]["docs_per_second"]
        for run in runs
        if run["windows"][-1]["docs_per_second"]
    ]
    return {
        "documents": documents,
        "batch_size": batch_size,
        "window_size": window_size,
        "repeats": repeats,
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
        },
        "median_total_seconds": median_total_seconds,
        "median_docs_per_second": (
            documents / median_total_seconds if median_total_seconds else 0.0
        ),
        "first_to_last_slowdown": statistics.median(slowdowns),
        "decoded_existing_rows": [run["decoded_existing_rows"] for run in runs],
        "windows": windows,
        "runs": runs,
    }


def format_markdown(result):
    rows = [
        "| Collection size | Documents | Median seconds | Median docs/s |",
        "| ---: | ---: | ---: | ---: |",
    ]
    for window in result["windows"]:
        rows.append(
            "| {start:,}-{end:,} | {documents:,} | {median_seconds:.3f} | "
            "{median_docs_per_second:,.0f} |".format(**window)
        )
    rows.extend(
        [
            "",
            "Median total: {0:.3f}s ({1:,.0f} docs/s)".format(
                result["median_total_seconds"],
                result["median_docs_per_second"],
            ),
            "First/last slowdown: {0:.2f}x".format(result["first_to_last_slowdown"]),
            "Existing rows decoded per run: {0}".format(
                result["decoded_existing_rows"]
            ),
        ]
    )
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=int, default=75000)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--window-size", type=int, default=7600)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--work-root")
    parser.add_argument("--json-output")
    args = parser.parse_args(argv)
    if min(args.docs, args.batch_size, args.window_size, args.repeats) <= 0:
        parser.error("document, batch, window, and repeat counts must be positive")
    if args.window_size % args.batch_size:
        parser.error("--window-size must be divisible by --batch-size")

    result = run_benchmark(
        args.docs,
        args.batch_size,
        args.window_size,
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
