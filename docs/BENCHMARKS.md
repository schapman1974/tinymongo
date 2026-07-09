# Storage Backend Load Test Results

These numbers are local benchmark results from the TinyMongo storage load
benchmark. They are useful for comparing relative behavior on this machine, not
for making universal performance claims.

## Methodology

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

## Results

| Backend | Insert docs/s | Read docs/s | Avg point lookup ms | p95 point lookup ms | Update s | Delete s | Size KiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| tinydb | 127,840 | 491,270 | 1.458 | 1.726 | 0.496 | 0.371 | 66.4 |
| parquet | 3,489 | 112,478 | 5.698 | 6.398 | 28.357 | 0.264 | 15.4 |
| sqlite | 44,104 | 283,618 | 1.165 | 1.428 | 0.458 | 0.007 | 136.0 |
| duckdb | 2,490 | 41,844 | 22.593 | 24.983 | 2.529 | 0.086 | 1,036.0 |

## Notes

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
