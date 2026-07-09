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
  --json-output /tmp/tinymongo-storage-load-1000.json
```

Workload:

- Insert 1,000 documents with `insert_many`.
- Read all documents once with `find({})`.
- Run 200 point lookups with `find_one({"_id": ...})`.
- Update 10% of documents with `update_many({"group": "g1"}, {"$inc": {"i": 1}})`.
- Delete 10% of documents with `delete_many({"group": "g2"})`.
- Measure final storage size on disk.

Environment:

- Date: 2026-07-03
- Python: local project virtualenv
- `pyarrow`: 21.0.0
- `duckdb`: 1.4.5
- `portalocker`: 3.2.0

During Parquet runs, `pyarrow` printed macOS sandbox warnings about CPU cache
metadata lookup. The benchmark completed successfully.

## Results

| Backend | Insert docs/s | Read docs/s | Avg point lookup ms | p95 point lookup ms | Update s | Delete s | Size KiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| tinydb | 15,941 | 465,965 | 1.280 | 1.442 | 0.302 | 0.286 | 66.4 |
| parquet | 76,685 | 436,284 | 1.746 | 1.931 | 0.684 | 0.715 | 16.6 |
| sqlite | 105,547 | 491,592 | 1.397 | 1.542 | 0.465 | 0.427 | 72.0 |
| duckdb | 13,260 | 140,301 | 6.751 | 7.327 | 2.240 | 2.186 | 1,036.0 |

## Notes

- SQLite had the fastest bulk insert rate in this run.
- Parquet produced the smallest file and strong bulk insert throughput.
- TinyDB JSON remained competitive for reads and point lookups at this size.
- DuckDB worked correctly but had the largest file and slowest point lookup,
  update, and delete times in this TinyDB-storage-wrapper workload.
- A 5,000-document full matrix was intentionally not used for the chart because
  DuckDB delete performance made it too slow for a quick local documentation
  benchmark.

Regenerate the chart with:

```bash
.venv/bin/python tests/benchmarks/bench_storage.py
```
