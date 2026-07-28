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
