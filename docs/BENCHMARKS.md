# Storage Backend Load Test Results

These numbers are local benchmark results from the TinyMongo storage load
benchmark. They are useful for comparing relative behavior on this machine, not
for making universal performance claims.

## SQLite performance work: 2026-08-04

The focused comparison uses the same 10,000 JSON-shaped documents and performs:

- One `insert_many()` batch.
- 200 warmed, deterministic `_id` point lookups.
- 200 warmed equality queries through an index on `group`. Each query returns
  1,000 documents, for 200,000 decoded result rows in total.
- One indexed update affecting 1,000 documents.

TinyMongo and raw SQLite both store `_id` plus a JSON document payload. Raw
SQLite is a lower-bound comparison: it does not provide TinyMongo's BSON,
MongoDB query, validation, or update semantics, and its update is native SQL.
MongoDB 8.0 ran locally through PyMongo 4.17.0 in a disposable Docker container
with a persistent Docker volume. Its collection explicitly used
`WriteConcern(w=1, j=True)`, and the benchmark verifies that both measured
writes were acknowledged. The two SQLite connections reported WAL mode with
`synchronous=NORMAL`.

Command:

```bash
TINYMONGO_MONGODB_URI='mongodb://127.0.0.1:27017/?directConnection=true' \
  .venv/bin/python tests/benchmarks/bench_sqlite_comparison.py \
    --docs 10000 \
    --queries 200 \
    --json-output /tmp/tinymongo-sqlite-comparison.json
```

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
