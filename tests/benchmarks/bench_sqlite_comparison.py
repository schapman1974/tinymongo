"""Compare TinyMongo SQLite with raw SQLite and a real MongoDB server.

This is a local benchmark, not a pytest test.  It intentionally uses the same
JSON document shape for TinyMongo and raw SQLite so the raw result is a useful
lower bound for TinyMongo's compatibility-layer overhead rather than a test of
a different relational schema.  Raw SQLite uses native SQL for its update, so
it does not provide MongoDB's full update semantics.  MongoDB runs in a uniquely
named temporary database that is dropped at the end of the run.
"""

from __future__ import absolute_import

import argparse
import json
import os
import random
import sqlite3
import statistics
import tempfile
import time
import uuid

import tinymongo as tm


ENGINES = ("tinymongo-sqlite", "raw-sqlite", "mongodb")
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


def _time_call(operation):
    started = time.perf_counter()
    result = operation()
    return time.perf_counter() - started, result


def _percentile(values, percentile):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = int(round((percentile / 100.0) * (len(ordered) - 1)))
    return ordered[index]


def _group_count(documents, group_number):
    """Return how many generated documents belong to one benchmark group."""

    if documents <= group_number:
        return 0
    return ((documents - 1 - group_number) // 10) + 1


def _expected_indexed_rows(documents, queries):
    return sum(_group_count(documents, index % 10) for index in range(queries))


def _point_targets(documents, queries):
    """Return the same deterministic, non-sequential lookup set for each engine."""

    generator = random.Random(1974)
    return ["doc-{0}".format(generator.randrange(documents)) for _ in range(queries)]


def _result(
    engine,
    documents,
    queries,
    insert_seconds,
    point_latencies,
    point_update_latencies,
    indexed_seconds,
    indexed_rows,
    update_seconds,
    updated_docs,
):
    return {
        "engine": engine,
        "available": True,
        "documents": documents,
        "queries": queries,
        "insert_seconds": insert_seconds,
        "insert_docs_per_second": (
            documents / insert_seconds if insert_seconds else 0.0
        ),
        "point_avg_ms": statistics.mean(point_latencies) * 1000,
        "point_p95_ms": _percentile(point_latencies, 95) * 1000,
        "point_update_avg_ms": statistics.mean(point_update_latencies) * 1000,
        "point_update_p95_ms": _percentile(point_update_latencies, 95) * 1000,
        "indexed_seconds": indexed_seconds,
        "indexed_queries_per_second": (
            queries / indexed_seconds if indexed_seconds else 0.0
        ),
        "indexed_rows_per_second": (
            indexed_rows / indexed_seconds if indexed_seconds else 0.0
        ),
        "indexed_rows": indexed_rows,
        "update_seconds": update_seconds,
        "update_docs_per_second": (
            updated_docs / update_seconds if update_seconds else 0.0
        ),
        "updated_docs": updated_docs,
    }


def _sqlite_settings(conn):
    """Capture the durability settings used by one SQLite benchmark run."""

    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    return {
        "journal_mode": str(journal_mode).upper(),
        "synchronous": _SQLITE_SYNCHRONOUS_NAMES.get(synchronous, str(synchronous)),
    }


def _run_tinymongo_sqlite(documents, queries, work_root):
    # Never remove a fixed path supplied by the caller. A unique child also
    # makes concurrent benchmark runs safe.
    db_dir = tempfile.mkdtemp(prefix="tinymongo-sqlite-", dir=work_root)
    client = tm.TinyMongoClient(db_dir, backend="sqlite")
    try:
        return _run_tinymongo_sqlite_client(client, documents, queries)
    finally:
        client.close()


def _run_tinymongo_sqlite_client(client, documents, queries):
    collection = client.comparison.records
    settings_conn = collection.parent.engine._connect()
    try:
        sqlite_settings = _sqlite_settings(settings_conn)
    finally:
        settings_conn.close()
    docs = _docs(documents)

    insert_seconds, result = _time_call(lambda: collection.insert_many(docs))
    if len(result.inserted_ids) != documents:
        raise AssertionError("TinyMongo inserted the wrong number of documents")
    collection.create_index("group")

    # Warm SQLite's page cache before measuring lookup latency.
    targets = _point_targets(documents, queries)
    if collection.find_one({"_id": targets[0]}) is None:
        raise AssertionError("TinyMongo point lookup warm-up missed a document")
    point_latencies = []
    for target in targets:
        elapsed, found = _time_call(
            lambda target=target: collection.find_one({"_id": target})
        )
        if found is None or found["_id"] != target:
            raise AssertionError("TinyMongo point lookup missed {0}".format(target))
        point_latencies.append(elapsed)

    point_update_latencies = []
    for target in targets:
        elapsed, update_result = _time_call(
            lambda target=target: collection.update_one(
                {"_id": target},
                {"$inc": {"point_updates": 1}},
            )
        )
        if update_result.matched_count != 1 or update_result.modified_count != 1:
            raise AssertionError("TinyMongo point update missed {0}".format(target))
        point_update_latencies.append(elapsed)

    def indexed_queries():
        rows = 0
        for index in range(queries):
            group = "g{0}".format(index % 10)
            rows += len(list(collection.find({"group": group})))
        return rows

    if len(list(collection.find({"group": "g0"}))) != _group_count(documents, 0):
        raise AssertionError("TinyMongo indexed lookup warm-up returned wrong rows")
    indexed_seconds, indexed_rows = _time_call(indexed_queries)
    expected_rows = _expected_indexed_rows(documents, queries)
    if indexed_rows != expected_rows:
        raise AssertionError(
            "TinyMongo indexed queries returned {0} rows, expected {1}".format(
                indexed_rows, expected_rows
            )
        )

    update_seconds, update_result = _time_call(
        lambda: collection.update_many({"group": "g1"}, {"$inc": {"i": 1}})
    )
    expected_updates = _group_count(documents, 1)
    if update_result.modified_count != expected_updates:
        raise AssertionError(
            "TinyMongo updated {0} rows, expected {1}".format(
                update_result.modified_count, expected_updates
            )
        )
    if documents > 1:
        updated = collection.find_one({"_id": "doc-1"})
        if updated is None or updated["i"] != 2:
            raise AssertionError("TinyMongo update produced the wrong value")
    benchmark_result = _result(
        "tinymongo-sqlite",
        documents,
        queries,
        insert_seconds,
        point_latencies,
        point_update_latencies,
        indexed_seconds,
        indexed_rows,
        update_seconds,
        update_result.modified_count,
    )
    benchmark_result["sqlite_settings"] = sqlite_settings
    return benchmark_result


def _run_raw_sqlite(documents, queries, work_root):
    descriptor, path = tempfile.mkstemp(
        prefix="raw-sqlite-", suffix=".sqlite", dir=work_root
    )
    os.close(descriptor)
    conn = sqlite3.connect(path, timeout=30)
    try:
        return _run_raw_sqlite_connection(conn, documents, queries)
    finally:
        conn.close()


def _run_raw_sqlite_connection(conn, documents, queries):
    conn.execute("PRAGMA journal_mode=WAL")
    sqlite_settings = _sqlite_settings(conn)
    conn.execute("CREATE TABLE records (_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    docs = _docs(documents)

    def insert_all():
        rows = [
            (
                document["_id"],
                json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            )
            for document in docs
        ]
        conn.executemany("INSERT INTO records (_id, data) VALUES (?, ?)", rows)
        conn.commit()

    insert_seconds, _ = _time_call(insert_all)
    conn.execute(
        "CREATE INDEX records_group ON records (json_extract(data, '$.group'))"
    )
    conn.commit()

    targets = _point_targets(documents, queries)
    if (
        conn.execute("SELECT data FROM records WHERE _id = ?", (targets[0],)).fetchone()
        is None
    ):
        raise AssertionError("raw SQLite point lookup warm-up missed a document")
    point_latencies = []
    for target in targets:

        def point_lookup(target=target):
            row = conn.execute(
                "SELECT data FROM records WHERE _id = ?", (target,)
            ).fetchone()
            return None if row is None else json.loads(row[0])

        elapsed, found = _time_call(point_lookup)
        if found is None or found["_id"] != target:
            raise AssertionError("raw SQLite point lookup missed {0}".format(target))
        point_latencies.append(elapsed)

    point_update_latencies = []
    for target in targets:

        def point_update(target=target):
            cursor = conn.execute(
                "UPDATE records SET data = json_set("
                "data, '$.point_updates', "
                "COALESCE(json_extract(data, '$.point_updates'), 0) + 1) "
                "WHERE _id = ?",
                (target,),
            )
            conn.commit()
            return cursor.rowcount

        elapsed, updated = _time_call(point_update)
        if updated != 1:
            raise AssertionError("raw SQLite point update missed {0}".format(target))
        point_update_latencies.append(elapsed)

    def indexed_queries():
        rows_found = 0
        for index in range(queries):
            group = "g{0}".format(index % 10)
            rows = conn.execute(
                "SELECT data FROM records WHERE json_extract(data, '$.group') = ?",
                (group,),
            ).fetchall()
            rows_found += len([json.loads(row[0]) for row in rows])
        return rows_found

    query_plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT data FROM records "
        "WHERE json_extract(data, '$.group') = ?",
        ("g0",),
    ).fetchall()
    if not any("records_group" in str(row[-1]) for row in query_plan):
        raise AssertionError("raw SQLite did not use the requested group index")
    warm_rows = conn.execute(
        "SELECT data FROM records WHERE json_extract(data, '$.group') = ?",
        ("g0",),
    ).fetchall()
    if len(warm_rows) != _group_count(documents, 0):
        raise AssertionError("raw SQLite indexed lookup warm-up returned wrong rows")
    for row in warm_rows:
        json.loads(row[0])
    indexed_seconds, indexed_rows = _time_call(indexed_queries)
    expected_rows = _expected_indexed_rows(documents, queries)
    if indexed_rows != expected_rows:
        raise AssertionError(
            "raw SQLite indexed queries returned {0} rows, expected {1}".format(
                indexed_rows, expected_rows
            )
        )

    def update_group():
        cursor = conn.execute(
            "UPDATE records SET data = json_set("
            "data, '$.i', json_extract(data, '$.i') + 1) "
            "WHERE json_extract(data, '$.group') = ?",
            ("g1",),
        )
        conn.commit()
        return cursor.rowcount

    update_seconds, updated_docs = _time_call(update_group)
    expected_updates = _group_count(documents, 1)
    if updated_docs != expected_updates:
        raise AssertionError(
            "raw SQLite updated {0} rows, expected {1}".format(
                updated_docs, expected_updates
            )
        )
    if documents > 1:
        updated = conn.execute(
            "SELECT data FROM records WHERE _id = ?", ("doc-1",)
        ).fetchone()
        if updated is None or json.loads(updated[0])["i"] != 2:
            raise AssertionError("raw SQLite update produced the wrong value")
    benchmark_result = _result(
        "raw-sqlite",
        documents,
        queries,
        insert_seconds,
        point_latencies,
        point_update_latencies,
        indexed_seconds,
        indexed_rows,
        update_seconds,
        updated_docs,
    )
    benchmark_result["sqlite_settings"] = sqlite_settings
    return benchmark_result


def _run_mongodb(documents, queries, mongo_uri):
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

    database_name = "tinymongo_perf_{0}_{1}".format(os.getpid(), uuid.uuid4().hex[:10])
    write_concern = WriteConcern(w=1, j=True)
    database = client.get_database(database_name, write_concern=write_concern)
    collection = database.records
    docs = _docs(documents)
    try:
        insert_seconds, result = _time_call(lambda: collection.insert_many(docs))
        if not result.acknowledged:
            raise AssertionError("MongoDB did not acknowledge the insert batch")
        if len(result.inserted_ids) != documents:
            raise AssertionError("MongoDB inserted the wrong number of documents")
        collection.create_index("group")

        targets = _point_targets(documents, queries)
        if collection.find_one({"_id": targets[0]}) is None:
            raise AssertionError("MongoDB point lookup warm-up missed a document")
        point_latencies = []
        for target in targets:
            elapsed, found = _time_call(
                lambda target=target: collection.find_one({"_id": target})
            )
            if found is None or found["_id"] != target:
                raise AssertionError("MongoDB point lookup missed {0}".format(target))
            point_latencies.append(elapsed)

        point_update_latencies = []
        for target in targets:
            elapsed, update_result = _time_call(
                lambda target=target: collection.update_one(
                    {"_id": target},
                    {"$inc": {"point_updates": 1}},
                )
            )
            if not update_result.acknowledged:
                raise AssertionError("MongoDB did not acknowledge the point update")
            if update_result.matched_count != 1 or update_result.modified_count != 1:
                raise AssertionError("MongoDB point update missed {0}".format(target))
            point_update_latencies.append(elapsed)

        def indexed_queries():
            rows = 0
            for index in range(queries):
                group = "g{0}".format(index % 10)
                rows += len(list(collection.find({"group": group})))
            return rows

        if len(list(collection.find({"group": "g0"}))) != _group_count(documents, 0):
            raise AssertionError("MongoDB indexed lookup warm-up returned wrong rows")
        indexed_seconds, indexed_rows = _time_call(indexed_queries)
        expected_rows = _expected_indexed_rows(documents, queries)
        if indexed_rows != expected_rows:
            raise AssertionError(
                "MongoDB indexed queries returned {0} rows, expected {1}".format(
                    indexed_rows, expected_rows
                )
            )

        update_seconds, update_result = _time_call(
            lambda: collection.update_many({"group": "g1"}, {"$inc": {"i": 1}})
        )
        if not update_result.acknowledged:
            raise AssertionError("MongoDB did not acknowledge the update batch")
        expected_updates = _group_count(documents, 1)
        if update_result.modified_count != expected_updates:
            raise AssertionError(
                "MongoDB updated {0} rows, expected {1}".format(
                    update_result.modified_count, expected_updates
                )
            )
        if documents > 1:
            updated = collection.find_one({"_id": "doc-1"})
            if updated is None or updated["i"] != 2:
                raise AssertionError("MongoDB update produced the wrong value")
        benchmark_result = _result(
            "mongodb",
            documents,
            queries,
            insert_seconds,
            point_latencies,
            point_update_latencies,
            indexed_seconds,
            indexed_rows,
            update_seconds,
            update_result.modified_count,
        )
        benchmark_result["write_concern"] = write_concern.document
        return benchmark_result
    finally:
        try:
            client.drop_database(database_name)
        finally:
            client.close()


def format_markdown(results):
    rows = [
        "| Engine | Insert docs/s | Point read avg ms | Point read p95 ms | "
        "Point update avg ms | Point update p95 ms | "
        "Indexed queries/s | Indexed rows/s | Update docs/s | Update s |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in results:
        if not result.get("available"):
            rows.append(
                "| {0} | unavailable | unavailable | unavailable | unavailable | "
                "unavailable | unavailable | unavailable | unavailable | "
                "unavailable |".format(result["engine"])
            )
            continue
        rows.append(
            "| {engine} | {insert_docs_per_second:,.0f} | {point_avg_ms:.3f} | "
            "{point_p95_ms:.3f} | {point_update_avg_ms:.3f} | "
            "{point_update_p95_ms:.3f} | {indexed_queries_per_second:,.1f} | "
            "{indexed_rows_per_second:,.0f} | {update_docs_per_second:,.0f} | "
            "{update_seconds:.3f} |".format(**result)
        )
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=int, default=10000)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--engine", action="append", choices=ENGINES)
    parser.add_argument("--work-root")
    parser.add_argument("--mongo-uri", default=os.getenv("TINYMONGO_MONGODB_URI"))
    parser.add_argument("--json-output")
    args = parser.parse_args(argv)
    if args.docs <= 0 or args.queries <= 0:
        parser.error("--docs and --queries must both be positive")

    work_root = args.work_root or tempfile.mkdtemp(prefix="tinymongo-compare-")
    os.makedirs(work_root, exist_ok=True)
    requested = args.engine or list(ENGINES)
    runners = {
        "tinymongo-sqlite": lambda: _run_tinymongo_sqlite(
            args.docs, args.queries, work_root
        ),
        "raw-sqlite": lambda: _run_raw_sqlite(args.docs, args.queries, work_root),
        "mongodb": lambda: _run_mongodb(args.docs, args.queries, args.mongo_uri),
    }
    results = [runners[engine]() for engine in requested]
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
    for result in results:
        if not result.get("available"):
            print("{0}: {1}".format(result["engine"], result["reason"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
