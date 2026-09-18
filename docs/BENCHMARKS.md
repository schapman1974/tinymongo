# Backend Benchmarks

The benchmark reports single-process baseline performance separately from
four-process contention/scaling. Every phase in a profile uses the profile's
same number of distinct spawned processes. Results are local medians, not
universal performance claims. Higher throughput and lower point-read latency
are better.

## Global comparison: 2026-08-07

Each profile used three isolated repetitions of the same 1,000 JSON-shaped
documents and deterministic query order. In the four-process profile, the
original `doc-0` through `doc-999` IDs were partitioned by the sharded SQLite
hash into batches of 243, 263, 241, and 253. Four synchronized processes
submitted those batches through `insert_many()`; raw SQLite used
`executemany()`. Before signaling readiness, every worker opened the database
and collection and forced lazy backend initialization. Process launch,
connection setup, schema/catalog checks, and phase warm-up were outside the
timer.

The four-process read-all phase had each process decode the complete collection
and reports 4,000 returned documents divided by the slowest worker duration.
The 200 warmed exact-ID targets were divided into deterministic worker streams
and report both aggregate throughput and per-operation latency. The 100 update
IDs and 100 delete IDs were partitioned into disjoint streams of individually
acknowledged exact-ID operations, so every target changed exactly once and
every sharded mutation remained shard-affine. Each phase completed and joined
all workers before the next phase began.

Every available row records the requested distinct PID count for all five
phases in every repetition. Backend order rotated between repetitions.
Persistent stores were closed, reopened, and fully validated before final size
was measured.

Environment: current TinyMongo working tree, Python 3.9.6, SQLite 3.51.0,
macOS 26.2 arm64, and an Apple M1 Pro. Experimental SQLite used four shards.

The table intentionally combines the available one-process baselines with the
four-process concurrency results. The process count is explicit on every row.

| Backend | Processes | Insert workload | Insert docs/s | Read-all docs/s | Point reads/s | Point avg ms | Point p95 ms | Update docs/s | Delete docs/s | Final KiB |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| TinyMongo Memory | 4 | not run | not run | not run | not run | not run | not run | not run | not run | N/A |
| TinyMongo TinyDB | 1 | 1 spawned `insert_many` bulk | 89,877 | 141,531 | 300 | 3.337 | 3.518 | 3 | 80 | 66.4 |
| TinyMongo TinyDB | 4 | failed: `AlreadyLocked` during update | not published | not published | not published | not published | not published | not published | not published | N/A |
| TinyMongo Parquet | 1 | 1 spawned `insert_many` bulk | 3,337 | 77,970 | 163 | 6.124 | 6.954 | 3 | 3 | 68.9 |
| TinyMongo Parquet | 4 | 4 spawned `insert_many` bulks | 954 | 311,556 | 545 | 6.250 | 6.629 | 3 | 4 | 69.0 |
| TinyMongo SQLite | 1 | 1 spawned `insert_many` bulk | 64,566 | 130,081 | 2,215 | 0.451 | 0.611 | 260 | 246 | 296.0 |
| TinyMongo SQLite | 4 | 4 spawned `insert_many` bulks | 1,296 | 320,252 | 4,120 | 0.816 | 1.317 | 117 | 119 | 300.0 |
| TinyMongo SQLite-sharded (4) | 4 | 4 spawned shard-affine `insert_many` bulks | 50,573 | 425,272 | 54,177 | 0.062 | 0.100 | 3,066 | 3,979 | 544.0 |
| TinyMongo DuckDB | 4 | not run | not run | not run | not run | not run | not run | not run | not run | N/A |
| Raw SQLite (native SQL) | 4 | 4 spawned `executemany` bulks | 263,684 | 2,329,023 | 237,883 | 0.014 | 0.021 | 15,648 | 20,122 | 136.0 |
| MongoDB | 4 | not run | not run | not run | not run | not run | not run | not run | not run | N/A |

TinyDB's prior four-process row has been retired. A fresh isolated run failed
with `AlreadyLocked` during individually acknowledged updates; retrying until a
run happened to pass would hide a real concurrency limitation. The harness
published no partial measurements from that failed run.

The paired one- and four-process profiles show different effects by phase.
Parquet's aggregate full-scan throughput scaled across CPU processes, while its
individually acknowledged mutations remained constrained by whole-file
rewrites. Standard SQLite also improved aggregate read throughput, but its one
writer and lock acquisition dominate concurrent bulk inserts and mutation
streams. Async would not remove those storage constraints.

For an unfiltered scan of up to ten shards, sharded SQLite now attaches each
read-only shard to one pooled SQLite connection and executes one `UNION ALL`
query with global natural ordering. SQLite therefore reads and merges the rows,
and Python decodes each payload once. That raised the four-shard result from
206,748 to 425,272 documents/s in its paired SQLite run. In that capture,
standard SQLite reached 483,199 documents/s, leaving the sharded scan about
12.0% lower rather than 50.5% lower. The separately captured stress-table row
above should not replace that paired comparison. Filtered scans and databases
above SQLite's default ten-attachment limit retain the established
scatter/matcher path.

TinyMongo Memory is process-local and cannot expose one shared database to four
spawned workers. DuckDB rejects multiple processes opening the same writable
database. Both are marked not run rather than silently falling back to threads
or measuring isolated databases. MongoDB was not configured for this capture;
the harness uses the same five four-process phases when a URI is supplied.

Raw SQLite uses the same `_id` primary key and compact JSON payload, but native
SQL bypasses TinyMongo's BSON handling, MongoDB-style matcher, validation,
result objects, and connection lifecycle. It is a lower-bound reference rather
than a feature-equivalent backend. MongoDB includes Docker loopback and server
overhead.

Write durability is also not identical: TinyMongo SQLite and raw SQLite used
WAL with `synchronous=NORMAL`, while sharded SQLite used WAL with
`synchronous=FULL`. When configured, MongoDB uses acknowledged
`WriteConcern(w=1, j=True)`. Other rows used their normal backend defaults.
`N/A` means the client cannot measure a meaningful persistent file size.

## Run it

Single-process baseline:

```bash
.venv/bin/python tests/benchmarks/bench_storage.py \
  --backend tinydb --backend parquet --backend sqlite \
  --docs 1000 --queries 200 --repeats 3 --workers 1 \
  --json-output /tmp/tinymongo-single-process.json
```

Four-process concurrency profile:

```bash
TINYMONGO_MONGODB_URI='mongodb://127.0.0.1:27017/?directConnection=true' \
  .venv/bin/python tests/benchmarks/bench_storage.py \
    --docs 1000 \
    --queries 200 \
    --repeats 3 \
    --sqlite-shards 4 \
    --workers 4 \
    --json-output /tmp/tinymongo-storage.json
```

MongoDB is optional; without a URI its row is reported as not run. The
benchmark starts no servers or persistent background workers. Each measured
phase creates exactly four short-lived spawned workers and joins them before
continuing. Focused regression drivers remain in `tests/benchmarks/`, but their
different workloads are intentionally not mixed into this global table.

To refresh only one backend row, pass `--backend` for only that backend:

```bash
.venv/bin/python tests/benchmarks/bench_storage.py \
  --backend sqlite-sharded \
  --docs 1000 \
  --queries 200 \
  --repeats 3 \
  --sqlite-shards 4 \
  --workers 4 \
  --json-output /tmp/tinymongo-sqlite-sharded.json
```

## TM-049 JSON/memory write scaling

`tests/benchmarks/bench_json_merge_scaling.py` measures one insert while either
its own collection grows or an untouched neighbouring collection grows beside
a fixed 200-document target. Run it from the repository with:

```bash
PYTHONPATH=. python tests/benchmarks/bench_json_merge_scaling.py
```

A local comparison on 2026-09-18 (Darwin arm64, Python 3.14.5, median of three
inserts) swapped only `AtomicJSONStorage._merge_data` between the implementation
at `a6b9f8c` and the BSON identity-map implementation. These are synthetic,
single-process measurements, not a rerun of the Talk Python application.
Other test processes were running on the host; absolute timings are illustrative.

| Backend | Growing collection | Before, 500 docs | Before, 4,000 docs | After, 500 docs | After, 4,000 docs |
| --- | --- | ---: | ---: | ---: | ---: |
| JSON | target | 181.1 ms | 11,430.5 ms | 9.4 ms | 75.6 ms |
| JSON | untouched neighbour | 203.2 ms | 10,809.3 ms | 10.1 ms | 62.5 ms |
| memory | target | 170.6 ms | 10,814.3 ms | 7.1 ms | 63.9 ms |
| memory | untouched neighbour | 193.2 ms | 10,555.9 ms | 8.0 ms | 53.6 ms |

The nested identity scan is gone. JSON serialization and memory copying still
process the database snapshot, so this change makes the merge linear rather
than making writes constant-time. Regression tests additionally count identity
work, independent of wall-clock timing, and cover recursive BSON IDs, numeric
identity, booleans, missing IDs, and legacy comparison fallbacks.

## TM-042 SQLite indexed read coverage

Run the same isolated synthetic workload against two checkouts with:

```bash
PYTHONPATH=. python tests/benchmarks/bench_sqlite_tm042.py --sizes 2000 20000 --repeats 5
# Use this script with the baseline package to reproduce the before column:
PYTHONPATH=/path/to/baseline python tests/benchmarks/bench_sqlite_tm042.py --sizes 2000 20000 --repeats 5
```

Historical measurements on 2026-09-18 compared `59b5eaa` with the original
TM-042 implementation merged in #179 (`a54a8ec`) on Darwin arm64, Python 3.14.5.
Each size uses a fresh temporary SQLite database,
five declared indexes, 200-byte body strings, and ObjectId/date/numeric/Decimal128/
Binary fields. Warm values are medians of five reads after one cold read.
These are synthetic single-process measurements; the private Talk Python
application was not rerun. Other validation ran on the host during the baseline,
so absolute timings are illustrative rather than a controlled latency guarantee.

| Query | Before, 2,000 docs | After, 2,000 docs | Before, 20,000 docs | After, 20,000 docs |
| --- | ---: | ---: | ---: | ---: |
| ObjectId equality | 28.778 ms | 0.778 ms | 251.967 ms | 1.085 ms |
| datetime equality | 32.549 ms | 0.944 ms | 296.649 ms | 0.942 ms |
| Int64 equality with residual predicate | 54.169 ms | 0.949 ms | 284.149 ms | 1.048 ms |
| Decimal128 equality | 42.917 ms | 0.878 ms | 426.659 ms | 0.978 ms |
| Binary subtype 128 equality | 27.709 ms | 1.022 ms | 279.378 ms | 0.888 ms |
| Standalone date range | 44.448 ms | 1.032 ms | 429.916 ms | 0.992 ms |
| Standalone numeric range | 43.768 ms | 0.862 ms | 381.907 ms | 0.936 ms |
| Two indexed `$or` arms | 38.268 ms | 1.061 ms | 336.768 ms | 0.883 ms |

Equalities return one row, ranges return three, and `$or` returns two. Regression
tests also assert that warm selective reads decode only the matching rows in
this scalar-only corpus, and `EXPLAIN QUERY PLAN` uses native index searches.
Arrays and ambiguous numeric representations remain conservative candidates;
collections dominated by those values can still require substantial matching.

In that implementation, the first BSON equality/date read lazily built a derived
expression index. At 20,000 documents those first reads took 229–539 ms depending
on the field, versus roughly 1 ms warm. Each derived index scans the collection once, consumes
disk space, and adds work to subsequent writes. The range and `$or` cases above
reuse indexes built by earlier equality cases; their reported cold times are
not independent first-build measurements. Date keys preserve UTC millisecond
order, while scalar equality keys preserve BSON identity and numeric equality.

With all five derived indexes present, an `_id` point update had a median of
2.912 ms at 2,000 documents and 3.086 ms at 20,000, versus 2.923 and 2.628 ms on
the baseline. This small sample does not isolate per-index maintenance cost;
it checks that these point updates remain bounded in this workload. Initial
migration before the first BSON/date read does not build these derived indexes.
Those historical indexes required every writer to register the query-key
function and prevented some plain SQLite maintenance. TM-053 replaces that
implementation with stored keys, as measured below.

The planner still declines unsafe anchors such as null/regex, partial indexes,
dotted fields, incomplete `$or` plans, and queries exceeding its bounded tree or
parameter limits. The exact BSON matcher remains the final result authority.

## TM-053 portable SQLite query keys

The same synthetic script was rerun locally for TM-053 on 2026-09-18. These
measurements use the stored-key implementation, fresh temporary databases at
each size, and five warm repetitions. They are separate from the historical
TM-042 measurements above and from Michael's private real-store retest.
Other validation was active on the host, so these timings do not isolate the
index-maintenance overhead or establish a controlled latency guarantee.

| Query | Warm, 2,000 docs | First read, 20,000 docs | Warm, 20,000 docs |
| --- | ---: | ---: | ---: |
| ObjectId equality | 0.742 ms | 599.842 ms | 0.782 ms |
| datetime equality | 1.116 ms | 456.078 ms | 1.023 ms |
| Int64 equality with residual predicate | 1.185 ms | 605.817 ms | 0.930 ms |
| Decimal128 equality | 1.012 ms | 789.270 ms | 0.899 ms |
| Binary subtype 128 equality | 1.048 ms | 874.408 ms | 0.894 ms |
| Standalone date range | 0.773 ms | 1.174 ms | 0.986 ms |
| Standalone numeric range | 0.945 ms | 1.230 ms | 0.976 ms |
| Two indexed `$or` arms | 1.042 ms | 1.405 ms | 1.098 ms |

The five equality cases each pay to add a key column, native index, and SQL
invalidation trigger, then scan the collection and write its canonical keys.
At 20,000 documents this costs 456–874 ms for a new field. The date range and
`$or` reuse keys built by earlier equality cases; their first-read values do
not measure a new key build. Ordinary numeric ranges use existing JSON
expression indexes. Warm reads still take about 1 ms in this scalar-only
workload. Stored keys and their indexes add disk and schema overhead.

An `_id` point update with five derived indexes had a median of 2.665 ms at
2,000 documents and 6.659 ms at 20,000, compared with 2.513 and 2.660 ms before
those indexes were created in the same run. Writes now invalidate cached keys
through native SQL triggers, including writes from older clients or plain
SQLite. The next relevant read refreshes only uncomputed keys under a write
transaction. These point-update timings do not include that deferred refresh;
a large intervening batch of writes makes the next read pay for a larger
refresh. Queries conservatively include keys invalidated after refresh so
concurrent writers cannot cause missing matches.

A separate 200-document check with 128 KiB payloads and five warmed BSON/date
indexes timed each update together with the following five indexed reads, so
deferred refresh was included. Median of three was 11.957 ms at `a54a8ec` and
12.349 ms with stored keys. This small local comparison found no material
large-document penalty, but is not a private application rerun.

The new query indexes contain no application-defined function. Local
portability regressions cover plain SQLite updates and inserts, `VACUUM`,
`REINDEX`, backup and dump restore, and cleanup of legacy `_bson_v1` indexes.
Stop or upgrade every original #179 process sharing the file, including readers
and writers: one of its reads can recreate those legacy indexes. Pre-#179
writers can remain. Existing explicit unique and partial indexes have separate
function requirements, outside this query-cache change.
Dropping a declared index removes its derived index and trigger and clears its
keys; the empty column remains for SQLite versions without `DROP COLUMN`.

## TM-053 external direct SQLite retest

Michael's [external #180 acceptance report](https://github.com/schapman1974/tinymongo/issues/136#issuecomment-5736577143)
compared direct SQLite at `a54a8ec` with the stored-key implementation at
`fac214c`. The latter merged as `e39357b` while he measured; he checked that
the three source files were byte-identical. These are his measurements from
serialized runs in the same session, separate from the local synthetic results
above. His environment was Python 3.14.6, PyMongo 4.17, and macOS 26.2 arm64.

| Cold first read | `a54a8ec` | `fac214c` | Ratio |
| --- | ---: | ---: | ---: |
| Synthetic, 0.6 MiB | 3.92 ms | 11.61 ms | 3.0x |
| Synthetic, 3.5 MiB | 6.41 ms | 26.77 ms | 4.2x |
| Synthetic, 25 MiB | 30.40 ms | 187.00 ms | 6.2x |
| Synthetic, 200 MiB | 199.83 ms | 1,386.07 ms | 6.9x |
| Real `opt_ins` date range, 75,617 documents | 380.29 ms | 760.54 ms | 2.0x |
| Real `episodes` date range, 563 documents | 24.62 ms | 81.28 ms | 3.3x |

Warm reads stayed approximately flat: the 200 MiB synthetic case took 59.69 ms
before and 63.81 ms after, while the real `opt_ins` range took 4.38 and 4.49 ms.
Stored keys preserve the TM-042 steady-state improvement but make their initial
materialization more expensive. Budget this work during deployment warm-up:
the first relevant query took about 1.4 seconds for his 200 MiB collection and
0.76 seconds for the real `opt_ins` collection. These measurements do not imply
that every new filter shape rebuilds the keys; queries can reuse materialized
keys for the same declared field index.

His synthetic write checks found a 1.0x before/after-index ratio at every payload
on both pins. The real `opt_ins` insert stayed near one second; migration took
11.1 versus 10.9 seconds, and application startup took 0.8 versus 1.1 seconds.
The seeded application suite passed 903 tests on both pins, and the 45-shape
query differential found no regressions. The full acceptance and the remaining
application-side type-checker gate are recorded in
[the external acceptance results](TALKPYTHON_ACCEPTANCE.md#michael-kennedys-180-acceptance-external-evidence).

Before warming or serving a shared file, stop or upgrade all original #179
readers and writers. Michael verified that one stale #179 read can recreate the
legacy function-dependent index and block writers and plain SQLite maintenance
again, even after #180 repaired the store. Pre-#179 writers do not recreate that
index and can remain. Warm-up cannot make an active mix of #179 and #180 clients
safe from this recurring incompatibility.
