# Talk Python acceptance run

The goal of this work is to run the real Talk Python application and its tests
against TinyMongo, not merely to approximate its query list. The repository now
contains two layers that make that handoff practical:

1. Talk-Python-derived contracts run through TinyMongo's synchronous and
   asynchronous APIs against every supported embedded backend and real MongoDB.
2. `scripts/run_pymongo_acceptance.py` starts an external pytest suite while
   `pymongo.MongoClient` and `pymongo.AsyncMongoClient` are patched to use a
   selected TinyMongo backend.

The second layer still needs to be run in the Talk Python repository. Until that
real application run happens, the roadmap's application-acceptance item remains
open.

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

## First application findings and rerun gate

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

The Mongo-compatible portions of these behaviors now run through the shared
synchronous/asynchronous matrix and real MongoDB contracts. TinyMongo's
stronger whole-input serialization preflight is covered locally because
PyMongo may split a very large input across wire batches. Before publishing
the Talk Python baseline, rerun the two reduced reproductions and the
application suite, and record the exact Talk Python commit, Python and PyMongo
versions, selected test inventory, and configuration. The runner's `--api`
value labels results; the application configuration must actually exercise
the corresponding client path.

Mike's separate TinyMongo agent reference currently describes unreleased
`master` behavior as version 1.2.0. After the compatibility branch merges,
update that guide against the merge SHA or the `v1.2.1` tag, including the
Binary codec, BSON-aware `_id` identity, `insert_many()` partial failures,
bounded sort diagnostics, exact `$unset`, BSON-aware CLI, and dotted child
collections. Also correct the stale list-only `insert_many()` signature and
defaults, `BulkWriteError` details, blanket session claim, conditional
`AsyncMongoClient` patch/import caveat, numeric-versus-bool identity wording,
explicit null `_id` handling, `_default` collection filtering, current
error/result shapes, and `bytearray` normalization. It should call PyMongo an
optional runtime dependency—not a core dependency—for ObjectId and nonzero
Binary values, patching, and conditional exception inheritance.
The same guide update should correct constructor and sync-laziness wording,
document validation as a no-op, empty-array and sort-error details,
backend-specific locking and durability, the full object-storage environment
table, portable capability and duplicate-error examples, and the fact that CLI
migration does not copy source index metadata.

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
