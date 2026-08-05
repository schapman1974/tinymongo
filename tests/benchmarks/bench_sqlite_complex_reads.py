"""Compare warmed point and complex reads across three storage engines.

This is a local benchmark, not a pytest test.  TinyMongo SQLite, raw SQLite,
and MongoDB receive the same logical documents and the same single-field
``group`` index.  Every timed operation fully materializes and validates its
documents, so the measurements include each engine's normal result decoding.

The complex filter deliberately combines ``$and``, ``$in``, a numeric range,
and ``$mod``.  Raw SQLite expresses those predicates in SQL; TinyMongo and
MongoDB receive the equivalent MongoDB filter.  Raw SQLite is a useful lower
bound, but it does not provide TinyMongo's MongoDB-compatible BSON semantics.
MongoDB runs in a uniquely named temporary database that is dropped afterward.
"""

from __future__ import absolute_import

import argparse
import json
import os
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
import uuid

import tinymongo as tm


ENGINES = ("tinymongo-sqlite", "raw-sqlite", "mongodb")
COMPLEX_GROUPS = ("g1", "g3", "g7")
COMPLEX_LOWER_BOUND = 2500
COMPLEX_UPPER_BOUND = 7500
COMPLEX_MODULUS = 7
COMPLEX_REMAINDER = 0
COMPLEX_FILTER = {
    "$and": [
        {"group": {"$in": list(COMPLEX_GROUPS)}},
        {
            "i": {
                "$gte": COMPLEX_LOWER_BOUND,
                "$lt": COMPLEX_UPPER_BOUND,
            }
        },
        {"i": {"$mod": [COMPLEX_MODULUS, COMPLEX_REMAINDER]}},
    ]
}
_SQLITE_SYNCHRONOUS_NAMES = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}


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


def _point_targets(documents, queries):
    """Return the same deterministic, non-sequential targets for every engine."""

    generator = random.Random(1974)
    return ["doc-{0}".format(generator.randrange(documents)) for _ in range(queries)]


def _complex_expected(documents):
    return [
        document
        for document in documents
        if document["group"] in COMPLEX_GROUPS
        and COMPLEX_LOWER_BOUND <= document["i"] < COMPLEX_UPPER_BOUND
        and document["i"] % COMPLEX_MODULUS == COMPLEX_REMAINDER
    ]


def _percentile(values, percentile):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = int(round((percentile / 100.0) * (len(ordered) - 1)))
    return ordered[index]


def _metric(latencies, rows):
    seconds = sum(latencies)
    queries = len(latencies)
    return {
        "queries": queries,
        "rows": rows,
        "rows_per_query": rows / queries if queries else 0.0,
        "seconds": seconds,
        "queries_per_second": queries / seconds if seconds else 0.0,
        "rows_per_second": rows / seconds if seconds else 0.0,
        "average_ms": statistics.mean(latencies) * 1000 if latencies else 0.0,
        "median_ms": statistics.median(latencies) * 1000 if latencies else 0.0,
        "p95_ms": _percentile(latencies, 95) * 1000,
    }


def _sqlite_settings(conn):
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    return {
        "journal_mode": str(journal_mode).upper(),
        "synchronous": _SQLITE_SYNCHRONOUS_NAMES.get(synchronous, str(synchronous)),
    }


def _validate_one(found, expected, engine):
    if found is None:
        raise AssertionError(
            "{0} point lookup missed {1}".format(engine, expected["_id"])
        )
    if found != expected:
        raise AssertionError(
            "{0} point lookup returned the wrong document for {1}".format(
                engine,
                expected["_id"],
            )
        )


def _validate_many(found, expected_by_id, engine):
    if len(found) != len(expected_by_id):
        raise AssertionError(
            "{0} complex query returned {1} rows, expected {2}".format(
                engine,
                len(found),
                len(expected_by_id),
            )
        )
    found_by_id = {}
    for document in found:
        document_id = document.get("_id")
        if document_id in found_by_id:
            raise AssertionError(
                "{0} complex query returned duplicate _id {1}".format(
                    engine,
                    document_id,
                )
            )
        found_by_id[document_id] = document
    if found_by_id != expected_by_id:
        missing = sorted(set(expected_by_id) - set(found_by_id))[:5]
        extra = sorted(set(found_by_id) - set(expected_by_id))[:5]
        raise AssertionError(
            "{0} complex query returned different documents; missing={1}, "
            "extra={2}".format(engine, missing, extra)
        )


def _measure_reads(
    engine,
    read_one,
    read_complex,
    documents,
    queries,
    warmups,
):
    docs_by_id = {document["_id"]: document for document in documents}
    expected_complex = _complex_expected(documents)
    expected_complex_by_id = {
        document["_id"]: document for document in expected_complex
    }
    targets = _point_targets(len(documents), queries)

    for _ in range(warmups):
        for target in targets:
            _validate_one(read_one(target), docs_by_id[target], engine)
        _validate_many(read_complex(), expected_complex_by_id, engine)

    point_latencies = []
    for target in targets:
        started = time.perf_counter()
        found = read_one(target)
        point_latencies.append(time.perf_counter() - started)
        _validate_one(found, docs_by_id[target], engine)

    complex_latencies = []
    complex_rows = 0
    for _ in range(queries):
        started = time.perf_counter()
        found = read_complex()
        complex_latencies.append(time.perf_counter() - started)
        _validate_many(found, expected_complex_by_id, engine)
        complex_rows += len(found)

    return {
        "engine": engine,
        "available": True,
        "documents": len(documents),
        "warmups": warmups,
        "point": _metric(point_latencies, len(point_latencies)),
        "complex": _metric(complex_latencies, complex_rows),
        "complex_rows_per_query": len(expected_complex),
    }


def _run_tinymongo_sqlite(documents, queries, warmups, work_root):
    db_dir = tempfile.mkdtemp(prefix="tinymongo-complex-read-", dir=work_root)
    client = tm.TinyMongoClient(db_dir, backend="sqlite")
    try:
        collection = client.read_benchmark.records
        docs = _docs(documents)
        result = collection.insert_many(docs)
        if len(result.inserted_ids) != documents:
            raise AssertionError("TinyMongo inserted the wrong number of documents")
        collection.create_index("group")

        settings_conn = collection.parent.engine._connect()
        try:
            settings = _sqlite_settings(settings_conn)
        finally:
            settings_conn.close()

        benchmark_result = _measure_reads(
            "tinymongo-sqlite",
            lambda target: collection.find_one({"_id": target}),
            lambda: list(collection.find(COMPLEX_FILTER)),
            docs,
            queries,
            warmups,
        )
        benchmark_result["indexes"] = ["group"]
        benchmark_result["sqlite_settings"] = settings
        return benchmark_result
    finally:
        client.close()


def _run_raw_sqlite(documents, queries, warmups, work_root):
    descriptor, path = tempfile.mkstemp(
        prefix="raw-complex-read-",
        suffix=".sqlite",
        dir=work_root,
    )
    os.close(descriptor)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE records (_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        docs = _docs(documents)
        conn.executemany(
            "INSERT INTO records (_id, data) VALUES (?, ?)",
            [
                (
                    document["_id"],
                    json.dumps(
                        document,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
                for document in docs
            ],
        )
        conn.execute(
            "CREATE INDEX records_group " "ON records (json_extract(data, '$.group'))"
        )
        conn.commit()

        complex_sql = (
            "SELECT data FROM records "
            "WHERE json_extract(data, '$.group') IN (?, ?, ?) "
            "AND json_extract(data, '$.i') >= ? "
            "AND json_extract(data, '$.i') < ? "
            "AND CAST(json_extract(data, '$.i') AS INTEGER) % ? = ?"
        )
        complex_parameters = COMPLEX_GROUPS + (
            COMPLEX_LOWER_BOUND,
            COMPLEX_UPPER_BOUND,
            COMPLEX_MODULUS,
            COMPLEX_REMAINDER,
        )
        query_plan = conn.execute(
            "EXPLAIN QUERY PLAN " + complex_sql,
            complex_parameters,
        ).fetchall()
        if not any("records_group" in str(row[-1]) for row in query_plan):
            raise AssertionError("raw SQLite did not use the group index")

        def read_one(target):
            row = conn.execute(
                "SELECT data FROM records WHERE _id = ?",
                (target,),
            ).fetchone()
            return None if row is None else json.loads(row[0])

        def read_complex():
            rows = conn.execute(complex_sql, complex_parameters).fetchall()
            return [json.loads(row[0]) for row in rows]

        benchmark_result = _measure_reads(
            "raw-sqlite",
            read_one,
            read_complex,
            docs,
            queries,
            warmups,
        )
        benchmark_result["indexes"] = ["group"]
        benchmark_result["sqlite_settings"] = _sqlite_settings(conn)
        benchmark_result["complex_query_plan"] = [str(row[-1]) for row in query_plan]
        return benchmark_result
    finally:
        conn.close()


def _run_mongodb(documents, queries, warmups, mongo_uri):
    if not mongo_uri:
        return {
            "engine": "mongodb",
            "available": False,
            "reason": "set TINYMONGO_MONGODB_URI or pass --mongo-uri",
        }
    try:
        import pymongo
        from pymongo.write_concern import WriteConcern
    except ImportError:
        return {
            "engine": "mongodb",
            "available": False,
            "reason": "install the pymongo benchmark dependency",
        }

    client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except Exception as exc:
        client.close()
        return {
            "engine": "mongodb",
            "available": False,
            "reason": "MongoDB is unavailable: {0}".format(exc),
        }

    database_name = "tinymongo_read_perf_{0}_{1}".format(
        os.getpid(),
        uuid.uuid4().hex[:10],
    )
    write_concern = WriteConcern(w=1, j=True)
    database = client.get_database(database_name, write_concern=write_concern)
    collection = database.records
    docs = _docs(documents)
    try:
        insert_result = collection.insert_many(docs)
        if not insert_result.acknowledged:
            raise AssertionError("MongoDB did not acknowledge the insert batch")
        if len(insert_result.inserted_ids) != documents:
            raise AssertionError("MongoDB inserted the wrong number of documents")
        collection.create_index("group")

        benchmark_result = _measure_reads(
            "mongodb",
            lambda target: collection.find_one({"_id": target}),
            lambda: list(collection.find(COMPLEX_FILTER)),
            docs,
            queries,
            warmups,
        )
        benchmark_result["indexes"] = ["group"]
        benchmark_result["write_concern"] = write_concern.document
        return benchmark_result
    finally:
        try:
            client.drop_database(database_name)
        finally:
            client.close()


def _format_read_table(results, metric_name, description):
    rows = [
        "### {0}".format(description),
        "",
        "| Engine | Queries | Rows/query | Queries/s | Rows/s | "
        "Average ms | Median ms | p95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in results:
        if not result.get("available"):
            rows.append(
                "| {0} | unavailable | unavailable | unavailable | unavailable | "
                "unavailable | unavailable | unavailable |".format(result["engine"])
            )
            continue
        metric = result[metric_name]
        rows.append(
            "| {0} | {queries:,} | {rows_per_query:,.0f} | "
            "{queries_per_second:,.1f} | {rows_per_second:,.0f} | "
            "{average_ms:.3f} | {median_ms:.3f} | {p95_ms:.3f} |".format(
                result["engine"], **metric
            )
        )
    return rows


def format_markdown(results):
    rows = _format_read_table(
        results,
        "point",
        "Simple `_id` lookup (one fully decoded document per query)",
    )
    rows.extend(
        [
            "",
            "The complex query is `$and(group $in, i range, i $mod)` and every "
            "engine fully decodes and validates the same result documents.",
            "",
        ]
    )
    rows.extend(
        _format_read_table(
            results,
            "complex",
            "Complex read (identical fully decoded result set per query)",
        )
    )
    unavailable = [result for result in results if not result.get("available")]
    if unavailable:
        rows.extend(["", "Unavailable engines:"])
        for result in unavailable:
            rows.append("- {0}: {1}".format(result["engine"], result["reason"]))
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=int, default=10000)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--engine", action="append", choices=ENGINES)
    parser.add_argument("--work-root")
    parser.add_argument("--mongo-uri", default=os.getenv("TINYMONGO_MONGODB_URI"))
    parser.add_argument("--json-output")
    parser.add_argument("--markdown-output")
    args = parser.parse_args(argv)
    if args.docs <= 0 or args.queries <= 0:
        parser.error("--docs and --queries must both be positive")
    if args.warmups < 0:
        parser.error("--warmups must not be negative")

    work_root = args.work_root or tempfile.mkdtemp(
        prefix="tinymongo-complex-read-compare-"
    )
    os.makedirs(work_root, exist_ok=True)
    requested = args.engine or list(ENGINES)
    runners = {
        "tinymongo-sqlite": lambda: _run_tinymongo_sqlite(
            args.docs,
            args.queries,
            args.warmups,
            work_root,
        ),
        "raw-sqlite": lambda: _run_raw_sqlite(
            args.docs,
            args.queries,
            args.warmups,
            work_root,
        ),
        "mongodb": lambda: _run_mongodb(
            args.docs,
            args.queries,
            args.warmups,
            args.mongo_uri,
        ),
    }
    results = [runners[engine]() for engine in requested]
    payload = {
        "documents": args.docs,
        "queries": args.queries,
        "warmups": args.warmups,
        "work_root": work_root,
        "indexes": ["group"],
        "complex_filter": COMPLEX_FILTER,
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
        },
        "results": results,
    }
    markdown = format_markdown(results)
    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    if args.markdown_output:
        with open(args.markdown_output, "w", encoding="utf-8") as handle:
            handle.write(markdown)
            handle.write("\n")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
