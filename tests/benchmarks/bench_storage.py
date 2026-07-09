"""Local load benchmark for TinyMongo storage backends.

This is intentionally not a pytest test. Run it directly when you want local
performance numbers for README/docs updates.
"""

from __future__ import absolute_import

import argparse
import json
import os
import shutil
import statistics
import tempfile
import time

import tinymongo as tm


BACKENDS = ("tinydb", "parquet", "sqlite", "duckdb")


def _docs(count):
    return [
        {
            "_id": "doc-{0}".format(index),
            "i": index,
            "group": "g{0}".format(index % 10),
            "payload": "value-{0}".format(index),
        }
        for index in range(count)
    ]


def _time_call(func):
    started = time.perf_counter()
    result = func()
    return time.perf_counter() - started, result


def _percentile(values, percentile):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((percentile / 100.0) * (len(ordered) - 1)))
    return ordered[index]


def _available_backend(backend):
    if backend == "duckdb":
        try:
            __import__("duckdb")
        except Exception:
            return False
    if backend in ("parquet", "parquetv2"):
        try:
            __import__("pyarrow")
        except Exception:
            return False
    return True


def run_backend(backend, doc_count, query_count, work_root):
    if not _available_backend(backend):
        return {
            "backend": backend,
            "available": False,
            "reason": "optional dependency is not installed",
        }

    db_dir = os.path.join(work_root, backend)
    if os.path.exists(db_dir):
        shutil.rmtree(db_dir)
    os.makedirs(db_dir, exist_ok=True)

    client = tm.TinyMongoClient(db_dir, backend=backend)
    collection = client.loadtest.records
    docs = _docs(doc_count)

    insert_seconds, _ = _time_call(lambda: collection.insert_many(docs))
    read_seconds, all_docs = _time_call(lambda: list(collection.find({})))

    query_latencies = []
    for index in range(query_count):
        target = "doc-{0}".format(index % doc_count)
        elapsed, found = _time_call(lambda target=target: collection.find_one({"_id": target}))
        query_latencies.append(elapsed)
        if found is None:
            raise AssertionError("missing document {0}".format(target))

    update_seconds, update_result = _time_call(
        lambda: collection.update_many({"group": "g1"}, {"$inc": {"i": 1}})
    )
    delete_seconds, delete_result = _time_call(
        lambda: collection.delete_many({"group": "g2"})
    )
    remaining = collection.count()

    file_bytes = 0
    for root, _, filenames in os.walk(db_dir):
        for filename in filenames:
            file_bytes += os.path.getsize(os.path.join(root, filename))

    return {
        "backend": backend,
        "available": True,
        "documents": doc_count,
        "queries": query_count,
        "insert_seconds": insert_seconds,
        "insert_docs_per_second": doc_count / insert_seconds if insert_seconds else 0,
        "read_seconds": read_seconds,
        "read_docs_per_second": len(all_docs) / read_seconds if read_seconds else 0,
        "query_avg_ms": statistics.mean(query_latencies) * 1000,
        "query_p95_ms": _percentile(query_latencies, 95) * 1000,
        "update_seconds": update_seconds,
        "updated_docs": update_result.modified_count,
        "delete_seconds": delete_seconds,
        "deleted_docs": delete_result.deleted_count,
        "remaining_docs": remaining,
        "file_kib": file_bytes / 1024.0,
    }


def format_markdown(results):
    rows = [
        "| Backend | Insert docs/s | Read docs/s | Avg point lookup ms | p95 point lookup ms | Update s | Delete s | Size KiB |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in results:
        if not result.get("available"):
            rows.append(
                "| {backend} | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable |".format(
                    **result
                )
            )
            continue
        rows.append(
            "| {backend} | {insert_docs_per_second:,.0f} | {read_docs_per_second:,.0f} | {query_avg_ms:.3f} | {query_p95_ms:.3f} | {update_seconds:.3f} | {delete_seconds:.3f} | {file_kib:,.1f} |".format(
                **result
            )
        )
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=int, default=1000)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--backend", action="append", choices=BACKENDS)
    parser.add_argument("--work-root")
    parser.add_argument("--json-output")
    args = parser.parse_args(argv)

    backends = args.backend or list(BACKENDS)
    work_root = args.work_root or tempfile.mkdtemp(prefix="tinymongo-bench-")
    os.makedirs(work_root, exist_ok=True)

    results = [run_backend(backend, args.docs, args.queries, work_root) for backend in backends]
    payload = {
        "documents": args.docs,
        "queries": args.queries,
        "work_root": work_root,
        "results": results,
    }

    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")

    print(format_markdown(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
