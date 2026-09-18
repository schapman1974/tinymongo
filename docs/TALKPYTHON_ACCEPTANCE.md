# Talk Python acceptance run

The goal of this work is to run the real Talk Python application and its tests
against TinyMongo, not merely to approximate its query list. The repository now
contains two layers that make that handoff practical:

1. Talk-Python-derived contracts run through TinyMongo's synchronous and
   asynchronous APIs against every supported embedded backend and real MongoDB.
2. `scripts/run_pymongo_acceptance.py` starts an external pytest suite while
   `pymongo.MongoClient` and `pymongo.AsyncMongoClient` are patched to use a
   selected TinyMongo backend.

Mike Kennedy has now run the second layer in the Talk Python repository against
both MongoDB and TinyMongo SQLite. The remaining acceptance work is to rerun
the focused follow-up cases, exercise the write-heavy admin and concurrency
paths, and publish the complete dimensioned report.

## Prepare the application environment

Use the Talk Python test environment so all of its application dependencies and
configuration are available. Install the TinyMongo checkout and pytest into
that environment:

```bash
python -m pip install -e "/path/to/tinymongo[all]" pytest
```

The runner activates the patch before pytest imports the application's test
modules. Existing `from pymongo import AsyncMongoClient` imports and client
construction therefore keep their normal call sites.

## Establish the MongoDB reference

First configure Talk Python exactly as it is normally configured for its test
MongoDB, then run the selected application suite without patching:

```bash
python /path/to/tinymongo/scripts/run_pymongo_acceptance.py \
  --api async \
  --backend mongodb \
  --suite talkpython-app \
  --junitxml talkpython-async-mongodb.xml \
  -- /path/to/talkpython/tests -q
```

`--backend mongodb` only adds report metadata; it deliberately leaves PyMongo
untouched. The application's normal environment variable or configuration must
point at the reference MongoDB.

## Try the application with TinyMongo

Run the identical tests through an isolated in-memory database:

```bash
python /path/to/tinymongo/scripts/run_pymongo_acceptance.py \
  --api async \
  --backend memory \
  --suite talkpython-app \
  --junitxml talkpython-async-memory.xml \
  -- /path/to/talkpython/tests -q
```

Then repeat with SQLite to exercise a durable backend:

```bash
python /path/to/tinymongo/scripts/run_pymongo_acceptance.py \
  --api async \
  --backend sqlite \
  --folder .talkpython-tinymongo \
  --suite talkpython-app \
  --junitxml talkpython-async-sqlite.xml \
  -- /path/to/talkpython/tests -q
```

The patch affects process-global PyMongo client classes for the duration of the
pytest session. Run these acceptance commands as separate processes rather than
inside an already-running application server.

## Generate the application report

Combine the three JUnit files into one deterministic baseline:

```bash
python /path/to/tinymongo/scripts/generate_compatibility_report.py \
  talkpython-async-mongodb.xml \
  talkpython-async-memory.xml \
  talkpython-async-sqlite.xml \
  --apis async \
  --backends memory,sqlite,mongodb \
  --json-output talkpython-compatibility.json \
  --markdown-output talkpython-compatibility.md
```

The report is publishable only when every expected target cell was executed,
the matching MongoDB reference behavior passed, and no result is unattributed.
A partial run is still rendered, but it is labeled incomplete.

## Application result and rerun gate

Mike Kennedy's first real Talk Python pass reached the asynchronous application
initializer on SQLite, opened all four database handles, and accepted the index
declarations for all 16 collections. It reduced the first blocking differences
to reusable contracts:

- datetimes and ObjectIds must sort instead of silently retaining insertion
  order;
- BinData must sort by length, subtype, and bytes;
- `Binary`, `bytes`, and `bytearray` must cross the JSON persistence boundary;
  generic subtype-0 values must compare like native bytes while other subtypes
  remain distinct;
- `insert_many()` must distinguish duplicate-key partial failures from
  client-side encoding failures; and
- synchronous and asynchronous code must preserve the same behavior across
  memory, JSON, SQLite, DuckDB, and Parquet.

After those fixes, Mike migrated all 81,017 source documents with zero
rejections and ran the real application suite through this repository's
acceptance runner. After the final follow-up in #143, MongoDB and TinyMongo
SQLite both passed all 597 tests; all nine application surfaces and all 21
admin-write checks passed. The public site rendered from TinyMongo without
MongoDB running. The final recorded TinyMongo baseline was `master` at
`07e9b40`, Python 3.14.6, PyMongo 4.17, and the SQLite backend. A fresh-memory
run initially exposed four sitemap failures, which Mike confirmed and fixed as
empty-data assumptions in the application rather than TinyMongo differences.

The Mongo-compatible behaviors also run through TinyMongo's shared
synchronous/asynchronous matrix and real MongoDB contracts. TinyMongo's
stronger whole-input serialization preflight is covered locally because
PyMongo may split a very large input across wire batches. Before publishing the
final dimensioned report, record the exact Talk Python commit, selected test
inventory, and configuration for each rerun. The runner's `--api` value labels
results; the application configuration must actually exercise the
corresponding client path.

### Follow-up compatibility fixes

Mike's next focused pass identified three more PyMongo-facing differences:

- omitted `_id` values needed to be native `ObjectId` instances so the
  application could reconstruct them from their string form;
- `$ne: None` and `$nin` lists containing `None` needed to exclude missing
  fields; and
- unsupported document values needed to raise `InvalidDocument` instead of a
  bare serialization `TypeError`.

These cases now run through the shared synchronous/asynchronous contract
matrix. TinyMongo creates native automatic IDs when optional BSON support is
available, while dependency-free writes and the explicit `generate_id()`
helper retain UUID strings. Invalid-document failures happen before storage,
retain the rejected document and nested path context, and are catchable through
both BSON's `InvalidDocument` and `PyMongoError` when PyMongo is installed.
Mike reran these cases unchanged against `07e9b40`; all passed in the focused
reproductions and the real Talk Python write paths. The main application goal
is complete with no known correctness defect in Talk Python's TinyMongo path.

Mike's separate TinyMongo agent reference should now be updated against the
`v1.3.0` tag, including the Binary codec, BSON-aware `_id` identity,
`insert_many()` partial failures,
bounded sort diagnostics, exact `$unset`, BSON-aware CLI, dotted child
collections, native automatic `ObjectId` values, null-negation behavior, and
contextual `InvalidDocument` errors. It should also describe the structured
query, update-operator, aggregation, and BSON-type capabilities plus the
expanded `$rename`, `$min`, `$max`, and `$pop` update subset. Also correct the
stale list-only
`insert_many()` signature and defaults, `BulkWriteError` details, blanket
session claim, conditional `AsyncMongoClient` patch/import caveat,
numeric-versus-bool identity wording, explicit null `_id` handling, `_default`
collection filtering, current error/result shapes, and `bytearray`
normalization. It should call PyMongo an optional runtime dependency—not a core
dependency—for ObjectId and nonzero Binary values, patching, and conditional
exception inheritance.
The same guide update should correct constructor and sync-laziness wording,
document validation as a no-op, empty-array and sort-error details,
backend-specific locking and durability, the full object-storage environment
table, portable capability and duplicate-error examples, and the fact that CLI
migration does not copy source index metadata.

Mike's final TM-019/TM-030 fidelity pass is also captured as shared contracts.
PyMongo-shaped synchronous and asynchronous clients recursively honor
`document_class`, `tz_aware`, and `tzinfo`; persisted datetimes now use BSON's
signed UTC millisecond representation. The option-name allowlist follows the
installed PyMongo validator catalog when available, and malformed operators
inside `$elemMatch` report MongoDB error code `2` without disguising valid but
unsupported predicates as malformed input.

## Handling failures

For each difference found in the real application:

1. reduce the behavior to the smallest document, operation, and assertion;
2. add it to `tests/contracts` for both sync and async APIs;
3. compare the same case with real MongoDB;
4. link the temporary expected difference to the relevant roadmap issue;
5. fix TinyMongo or document the intentional difference; and
6. rerun the same Talk Python test before updating the published baseline.

The first application pass should prioritize whether Talk Python starts, creates
its indexes, completes its service-layer tests, and shuts down cleanly. Broader
backend coverage can follow after memory and SQLite have a trustworthy baseline.

## Michael Kennedy's round-20 report (external evidence)

Michael's [2026-08-08 eight-backend report](https://github.com/schapman1974/tinymongo/issues/79#issuecomment-5224455624)
tested commit `6eedcea` against the real Talk Python application and MongoDB 8.2.
These are his results, not a local rerun or evidence that the later fixes have
passed the application's private test suite.

| Backend | Documents migrated | Query shapes | Application tests |
| --- | --- | --- | --- |
| SQLite | 81,773 / 81,773 | 17 / 17 | 813 passed |
| sharded SQLite | 81,761 / 81,761 | 17 / 17 | 813 passed |
| DuckDB | 81,761 / 81,761 | 17 / 17 | 813 passed |
| Parquet | 81,761 / 81,761 | 17 / 17 | 813 passed |
| PostgreSQL | 81,743 / 81,761; 18 rejected | 15 / 17 | 813 passed |
| MariaDB | 81,761 / 81,761 | 16 / 17 | 813 passed |
| JSON | reduced: 6,156 / 6,156 | 15 / 15 | not run |
| memory | reduced: 6,156 / 6,156 | 15 / 15 | not run |

His sampled document comparisons found no mismatches, but this must not obscure
the PostgreSQL migration refusals or incomplete query coverage. JSON and memory
could not complete the full dataset because of TM-049. Some successful page
responses on slower backends came from the application's stale RSS cache. Source
counts varied because the live database continued receiving writes.

His [harness correction](https://github.com/schapman1974/tinymongo/issues/136#issuecomment-5224456519)
also explains that earlier runs accidentally sent about 30 tests to real MongoDB.
Round 20 verified all 813 with that server unreachable. TinyMongo's own
process-wide acceptance runner did not have that hole; the previously documented
597-test baseline is unchanged. The subsequent round-21 report below evaluates
the TM-044 through TM-049 fixes against the full workload.

## Michael Kennedy's round-21 report (external evidence)

Michael's [2026-09-18 follow-up](https://github.com/schapman1974/tinymongo/issues/136#issuecomment-5734514300)
tested merge commit `7055584` after #177 against his application and MongoDB 8.2.
These are his measurements, not a local rerun or validation of subsequent changes.

- **Default JSON migration completed:** all nine collections and all 81,579
  documents, zero rejections, in 275.6 seconds wall time. Round 20 could not
  complete the full corpus on this backend.
- **903 application tests passed** with real MongoDB deliberately unreachable.
  The increase from 813 reflects new application tests. His remaining type-checker
  gate failure came from an application-side `ty` upgrade, unrelated to TinyMongo.
- The new pin read all 81,579 documents from the old store with no failing
  collections. Stable SQLite migration took 11.1 / 10.2 seconds versus
  11.7 / 10.3 seconds at the previous pin in the same session.
- The 56-script repro sweep reported 51 clean and no regressions. Three scripts
  lacked old fixture paths, one was stale because unknown client keywords now
  correctly raise `ConfigurationError`, and TM-045 remained partially open.
- Cross-shard uniqueness passed 34/34 checks at both 4 and 12 shards. Unindexed
  and unique-token-preserving updates at 20,000 documents took 0.788 and 0.344 ms;
  token-changing updates retained the full uniqueness preflight.
- JSON/memory insert benchmarks improved 58–72 times. Writes still process the
  whole database: growing an untouched neighbouring collection eightfold caused
  roughly sixfold growth in the fixed target collection's write time.

TM-044's original cases, TM-046, TM-047, and TM-048 passed. The new tuple-shaped
parallel-array case and remaining TM-045 declaration errors are covered by
`tests/contracts/test_index_validation_contract.py`, through both client APIs,
all six embedded backends, and real MongoDB. These fixes had not yet been
retested by Michael at the time of the round-21 report; his later `a54a8ec`
retest is recorded below.

The real-store sharded timings from round 19 were not remeasured. TM-042 still
returned correct results for all 45 tested filter shapes, but 28 declined index
narrowing. Neither that planner limitation nor whole-database JSON/memory write
cost is resolved by the index-validation follow-up.

## TM-042 SQLite planner follow-up (local evidence)

The follow-up merged as #179 (`a54a8ec`) extends indexed reads to BSON scalar
equality and `$in`, standalone date and ordinary numeric ranges, and `$or` whose
every arm has a safe indexed candidate source.
`tests/contracts/test_sqlite_candidate_contract.py`
checks sync and async results against MongoDB 8.2, while
`tests/test_sqlite_tm042.py` checks SQLite/sharded SQLite decoding bounds, native
index use, reopen/update/drop behavior, and conservative fallback. The
[synthetic benchmark](BENCHMARKS.md#tm-042-sqlite-indexed-read-coverage) measures
both cold and warm reads and documents index maintenance costs.

These local results did not rerun the private differential or the 903
application tests. Michael's subsequent SQLite report below covers the
differential, real-store reads, and write/migration checks. The original derived
expression index required a Python function on every writer; TM-053 below
replaces it. JSON/memory whole-database write cost remains outside this SQLite
follow-up.

## Michael Kennedy's TM-042 retest and TM-053 report (external evidence)

Michael's [retest of `a54a8ec`](https://github.com/schapman1974/tinymongo/issues/136#issuecomment-5736103996)
compared it with `7055584` on the real SQLite store, with 75,617 `opt_ins`
documents and 563 `episodes`. Warm values are medians of three in the same
session. These are Michael's measurements, not a local application rerun.

- The unanchored `opt_ins` date range returned 191 rows in 4.32 ms warm,
  down from 945.51 ms, a 219x improvement. Its first read took 372.78 ms.
  Datetime equality improved from 519.66 to 0.61 ms, or 852x. That equality's
  first read reused the index already built by the range query.
- All three original TM-042 shapes narrowed: BSON equality, standalone date
  ranges, and indexed `$or`. Of the 45 differential shapes, 27 narrowed and
  18 declined, compared with 17 and 28 previously. He found zero narrowing
  bugs and zero divergences against MongoDB 8.2.
- Inserting one document into `opt_ins` after derived indexes existed took
  1,056.15 ms versus 1,048.71 ms at the prior pin. Full migration took
  5.0 seconds versus 5.2; re-migration took 10.9 versus 9.8 seconds. Synthetic
  payload checks found no added database-size scaling from the derived index.
- Tuple parallel arrays now returned code `171`, empty partial filters were
  accepted, and the reported malformed/non-mapping declaration cases returned
  the expected codes. One partial-filter mismatch remained: an unknown field
  operator returned `67` instead of MongoDB's `2`.
- **TM-053:** One BSON/date read created a persistent expression index requiring
  the per-connection `tinymongo_bson_query_key_v1` Python function. Older writers
  and plain SQLite updates, `VACUUM`, and `REINDEX` then failed. Dropping the
  declared index restored access; he reported no data loss.

Local reproduction confirmed the maintenance failures. Backup creation and
dump generation themselves succeeded; restoring that dump without the function
failed. This qualifies the broader backup/dump failure claim in the report.
The report does not establish a new complete private application test run.
JSON/memory application tests remained blocked by the startup cost tracked in
#176.

## TM-053 and remaining partial-filter validation follow-up (local evidence)

The combined follow-up stores canonical BSON/date keys in ordinary SQLite
columns, with native indexes and SQL triggers that invalidate keys when a
document changes. Relevant reads refresh only uncomputed keys transactionally
and also include rows invalidated concurrently as conservative candidates.
Opening a store removes owned legacy `_bson_v1` indexes; queries repeat cleanup
when they discover schema changes. The new read-created schema requires no
query-key Python function. Older pre-#179 writers remain compatible, while
original #179 readers must upgrade so they cannot recreate the legacy indexes.
Explicit unique and partial indexes retain separate function requirements.

`tests/test_sqlite_query_portability.py` covers plain SQLite maintenance,
backup and restore, external writes, legacy-index cleanup, and invalidation
lifecycle. Existing SQLite candidate tests preserve exact matching and bounded
warm decoding. The [TM-053 synthetic measurements](BENCHMARKS.md#tm-053-portable-sqlite-query-keys)
keep first-build costs, warm reads, and write costs separate; a write defers key
refresh work to the next relevant read.

The same follow-up validates partial-filter syntax before restricting which
valid predicates may appear in an index. Unknown operators inside field
predicates and malformed nested operands return code `2`; valid prohibited
predicates retain code `67`. The shared index-validation contracts cover the
embedded backends, sync/async APIs, and the MongoDB reference backend. These
local checks are not a rerun of Michael's private application; his next SQLite
pass should confirm portability and query behavior against the new pin.
