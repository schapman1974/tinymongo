# Storage Backend Load Test Results

These numbers are local benchmark results from the TinyMongo storage load
benchmark. They are useful for comparing relative behavior on this machine, not
for making universal performance claims.

## SQLite performance work: 2026-08-04 through 2026-08-05

### Fixed-size batch scaling (TM-040)

The earlier benchmark inserted one large batch, which could not expose the
collection-size curve reported by Mike Kennedy. The TM-040 benchmark instead
inserts 200 documents per call and reports successive collection-size windows:

```bash
.venv/bin/python tests/benchmarks/bench_sqlite_insert_scaling.py \
  --docs 75000 \
  --batch-size 200 \
  --window-size 7600 \
  --repeats 3 \
  --json-output /tmp/tinymongo-tm040-scaling.json
```

An apples-to-apples 30,000-document run compared `master` immediately before
TM-040 at commit `673734f` with the targeted primary-key preflight. Each side
used Python 3.9.6 on `macOS-26.2-arm64-arm-64bit`, the same interpreter,
document generator, batch size, decode instrumentation, and host:

| SQLite measurement | Total s | Overall docs/s | First-window docs/s | Last-window docs/s | First/last slowdown | Existing rows decoded |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Before targeted preflight | 18.904 | 1,587 | 5,005 | 935 | 5.35x | 2,235,000 |
| After targeted preflight | 3.939 | 7,615 | 7,696 | 7,090 | 1.09x | 0 |

The 75,000-document, three-run result after TM-040 had a 10.966-second median,
6,839 overall documents per second, and a 1.25x first-to-last ratio. All three
runs decoded zero existing payloads during insert preflight. These results
cover the ordinary no-unique-index path; collections with user-created unique
indexes deliberately retain complete Python validation until an exact
multikey-token ledger is justified.

### SQLite, raw SQLite, and MongoDB comparison

The focused comparison uses the same 10,000 JSON-shaped documents and performs:

- One `insert_many()` batch.
- 200 warmed, deterministic `_id` point lookups.
- 200 durable `_id` point updates using `$inc`.
- 200 warmed equality queries through an index on `group`. Each query returns
  1,000 documents, for 200,000 decoded result rows in total.
- One indexed update affecting 1,000 documents.

TinyMongo and raw SQLite both store `_id` plus a JSON document payload. Raw
SQLite is a lower-bound comparison: it does not provide TinyMongo's BSON,
MongoDB query, validation, or update semantics, and its update is native SQL.
MongoDB 8.0 ran locally through PyMongo 4.17.0 in a disposable Docker container
with a persistent Docker volume. Its collection explicitly used
`WriteConcern(w=1, j=True)`, and the benchmark verifies that every measured
write was acknowledged. The two SQLite connections reported WAL mode with
`synchronous=NORMAL`.

Command:

```bash
TINYMONGO_MONGODB_URI='mongodb://127.0.0.1:27017/?directConnection=true' \
  .venv/bin/python tests/benchmarks/bench_sqlite_comparison.py \
    --docs 10000 \
    --queries 200 \
    --json-output /tmp/tinymongo-sqlite-comparison.json
```

The original 2026-08-04 capture below predates the point-update measurement;
the 2026-08-05 follow-up records that new column separately.

| Engine | Insert docs/s | Point avg ms | Point p95 ms | Indexed queries/s | Indexed rows/s | Update docs/s | Update s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| tinymongo-sqlite | 18,985 | 0.414 | 0.470 | 116.7 | 116,688 | 5,054 | 0.198 |
| raw-sqlite | 302,158 | 0.006 | 0.007 | 596.6 | 596,596 | 84,231 | 0.012 |
| mongodb | 127,198 | 1.541 | 4.172 | 176.0 | 176,008 | 110,101 | 0.009 |

The MongoDB durability setting is stricter than SQLite's `NORMAL` setting, so
MongoDB is not receiving an unacknowledged-write advantage in this run. MongoDB
uses Docker's storage stack while SQLite uses the host filesystem, which means
the absolute write-throughput comparison remains directional rather than a
claim that the storage media are identical.

### Candidate-selective SQLite updates: 2026-08-05

The comparison driver now measures the ordinary durable
`update_one({"_id": ...}, {"$inc": ...})` path as well as the existing indexed
`update_many()` batch. The same updated driver ran against commit `8d627af`
before candidate selection and against the working branch afterward. Both runs
used 10,000 documents, 200 deterministic point updates, Python 3.9.6 on the
same macOS host, and SQLite WAL with `synchronous=NORMAL`:

| Engine or revision | Point update avg ms | Point update p95 ms | Indexed update 1,000 docs s | Indexed update docs/s |
| --- | ---: | ---: | ---: | ---: |
| TinyMongo SQLite before (`8d627af`) | 98.653 | 124.121 | 0.228 | 4,395 |
| TinyMongo SQLite after | 4.330 | 4.965 | 0.144 | 6,968 |
| Raw SQLite lower bound | 0.091 | 0.238 | 0.034 | 29,233 |

The targeted TinyMongo point update is about 22.8x faster in this run. Decode
instrumentation also changed from all 10,000 stored documents per `_id` update
to one document for a hit and zero for an ordinary miss. Declared non-unique
indexes now bound top-level bool/int/float/string equality-update candidates
too; exact BSON matching still runs before any row changes. Collections with
user-created unique indexes continue using complete post-image validation
because overlapping multikey tokens and fail-closed legacy catalogs cannot
safely rely on a simple native constraint alone.

The same 1,000-document storage benchmark was captured immediately before and
after the SQLite work on this machine:

| SQLite measurement | Insert docs/s | Point avg ms | Update 100 docs s |
| --- | ---: | ---: | ---: |
| Before | 566 | 1.821 | 2.984 |
| After | 15,046 | 0.431 | 0.023 |

That run measured approximately 27x higher batch-insert throughput, 4.2x lower
point-lookup latency, and 130x faster multi-document updates. The gains come
from linear duplicate planning, one validated update transaction, direct
primary-key reads, cached one-time SQLite setup, bounded indexed reads, and
selective array candidates. Absolute values will vary with hardware, document
size, result selectivity, durability settings, and process layout.

## Cross-backend snapshot: 2026-07-09

### Methodology

Command:

```bash
.venv/bin/python tests/benchmarks/bench_storage.py \
  --docs 1000 \
  --queries 200 \
  --json-output /tmp/tinymongo-table-backend-load-1000.json
```

Workload:

- Insert 1,000 documents with `insert_many`.
- Read all documents once with `find({})`.
- Run 200 point lookups with `find_one({"_id": ...})`.
- Update 10% of documents with `update_many({"group": "g1"}, {"$inc": {"i": 1}})`.
- Delete 10% of documents with `delete_many({"group": "g2"})`.
- Measure final storage size on disk.

Environment:

- Date: 2026-07-09
- Python: local project virtualenv
- `pyarrow`: 21.0.0
- `duckdb`: 1.4.5
- `portalocker`: 3.2.0

SQLite, DuckDB, and Parquet now use table-native storage: one collection table
or Parquet file per TinyMongo collection. Supported filters are compiled into SQL
over `_id` and JSON document payloads.

### Results

| Backend | Insert docs/s | Read docs/s | Avg point lookup ms | p95 point lookup ms | Update s | Delete s | Size KiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| tinydb | 127,840 | 491,270 | 1.458 | 1.726 | 0.496 | 0.371 | 66.4 |
| parquet | 3,489 | 112,478 | 5.698 | 6.398 | 28.357 | 0.264 | 15.4 |
| sqlite | 44,104 | 283,618 | 1.165 | 1.428 | 0.458 | 0.007 | 136.0 |
| duckdb | 2,490 | 41,844 | 22.593 | 24.983 | 2.529 | 0.086 | 1,036.0 |

### Notes

- TinyDB JSON remains very fast for this small local workload.
- SQLite has the fastest point lookups in this run because `_id` maps directly
  to a primary-key column.
- DuckDB and Parquet now use real tables/files, but the current implementation
  still opens short-lived connections and applies update documents in Python.
  Those areas are the next performance targets.
- Parquet produced the smallest storage footprint, but update-heavy workloads
  rewrite collection files and are not its best fit.

Regenerate the chart with:

```bash
.venv/bin/python tests/benchmarks/bench_storage.py
```
