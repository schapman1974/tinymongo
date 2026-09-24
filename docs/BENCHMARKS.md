# Backend benchmarks

**TinyMongo 1.3.1 · 23 September 2026 · single-process CRUD**

Same 1,000 small documents (~59 KiB of JSON) for every backend. Median of
three fresh-database runs. **All times are milliseconds; lower is better.**

| Backend | Insert 1,000 | Read one (p95) | Read all 1,000 | Update one (avg) | Delete one (avg) |
| --- | ---: | ---: | ---: | ---: | ---: |
| SQLite | 9.5 | 2.304 | 5.1 | 2.785 | 3.298 |
| SQLite-sharded (4, experimental) | 23.0 | 0.039 | 5.0 | 0.936 | 1.293 |
| JSON (TinyDB) | 7.0 | 3.076 | 4.2 | 19.582 | 9.212 |
| Memory | 10.8 | 3.017 | 4.1 | 23.285 | 19.496 |
| DuckDB | 699.2 | 53.386 | 28.4 | 174.277 | 60.691 |
| Parquet | 285.5 | 13.930 | 10.6 | 385.126 | 319.205 |
| Raw SQLite (native SQL reference) | 3.4 | 0.006 | 1.3 | 0.033 | 0.030 |

Sharded SQLite led TinyMongo's point reads and individual writes; direct SQLite
loaded the batch faster. JSON loaded quickly, but record updates averaged
19.6 ms. Memory was slower than either SQLite mode for point operations.

These are warm, small-dataset results from a development laptop with other
applications active. DuckDB/Parquet analytics, concurrent writers, and
large-collection performance need separate workloads.

<details>
<summary>Method, environment, and reproduction</summary>

- Per run: one `insert_many()` batch, one warmed full scan, 1,000 deterministic
  `_id` lookups, 100 `update_one()` calls, and 100 `delete_one()` calls.
  Read p95 is the 95th percentile per run; updates/deletes are per-operation
  averages. The table takes the median of each metric across three runs.
- Backend order rotates. Setup and warm-up are excluded. Every result is checked;
  persistent stores are reopened and their complete contents verified.
  Memory is volatile. No secondary indexes or BSON/date index builds are measured.
- Raw SQLite uses native SQL plus JSON decoding and WAL/NORMAL. Both TinyMongo
  SQLite modes used WAL/FULL. The reference bypasses MongoDB-style semantics
  and uses a fresh connection per phase, with setup excluded.
- Apple M1 Pro, 16 GiB RAM, macOS 26.6.2, Python 3.12.11, SQLite 3.49.1.
  Source: `89593ab` (1.3.1), with the single-process benchmark harness in this
  checkout. [Run data and exact dependency versions](benchmarks/2026-09-23.json).

From the repository root:

```bash
python -m pip install -e '.[bson,duckdb,parquet]'
python tests/benchmarks/bench_storage.py \
  --backend memory --backend tinydb --backend parquet \
  --backend sqlite --backend sqlite-sharded --backend duckdb \
  --backend raw-sqlite --workers 1 --sqlite-shards 4 \
  --docs 1000 --queries 1000 --repeats 3 \
  --json-output /tmp/tinymongo-benchmarks.json
```

</details>
