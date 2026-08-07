# Compatibility reports

TinyMongo turns pytest JUnit XML into deterministic JSON and Markdown
compatibility reports. The reports show the API mode, backend, and suite for
each contract cell and refuse to label an incomplete or unvalidated matrix as a
publishable baseline.

- `compatibility-report.json` is a versioned, machine-readable record.
- `compatibility-report.md` summarizes matrix integrity, scores, known gaps,
  failures, and unexpected strict-xfail passes.

Timestamps, durations, hostnames, and input paths are omitted. Absolute paths in
JUnit messages are replaced with `<ABSOLUTE_PATH>`, so identical results from
different machines render byte-for-byte identically.

## Generate the full report

Run the sync/async matrix against the six embedded backends and real MongoDB:

```bash
TINYMONGO_MONGODB_URI=mongodb://127.0.0.1:27017/?directConnection=true \
TINYMONGO_REQUIRE_MONGODB=1 \
pytest -o addopts='' -q -m contract tests/contracts \
  --junitxml=contract-results.xml
```

Then generate both reports:

```bash
python scripts/generate_compatibility_report.py contract-results.xml \
  --json-output compatibility-report.json \
  --markdown-output compatibility-report.md
```

The generator accepts multiple JUnit files. This is useful when an external
application is run once against MongoDB and again against one or more TinyMongo
backends:

```bash
python scripts/generate_compatibility_report.py \
  app-async-mongodb.xml app-async-memory.xml app-async-sqlite.xml \
  --apis async \
  --backends memory,sqlite,mongodb
```

Without output options, the default filenames are
`compatibility-report.json` and `compatibility-report.md`.

## Generate the BSON comparison report

Issue #94 has a focused contract slice for recursive, type-bracketed BSON
range comparison and sorting. Run it through both APIs and all seven configured
targets—the six embedded backends plus live MongoDB—and generate its reports
with:

```bash
TINYMONGO_MONGODB_URI='mongodb://127.0.0.1:27017/?directConnection=true' \
TINYMONGO_REQUIRE_MONGODB=1 \
pytest -o addopts='' -q -m contract \
  tests/contracts/test_bson_comparison_contract.py \
  --junitxml=bson-comparison-results.xml

python scripts/generate_compatibility_report.py \
  bson-comparison-results.xml \
  --json-output=bson-comparison-matrix.json \
  --markdown-output=bson-comparison-matrix.md
```

The resulting suite is attributed as `bson-comparison`, so its score can be
inspected independently while retaining the same completeness and
live-reference requirements as the full report.

## JUnit dimensions

Contract tests attach these properties to every testcase:

```xml
<property name="tinymongo.api" value="async" />
<property name="tinymongo.backend" value="sqlite" />
<property name="tinymongo.suite" value="talkpython" />
```

The generator prefers these explicit values. For older JUnit files it can infer
API and backend values from pytest parameter IDs such as
`test_name[async-sqlite]`, and it derives a conservative suite name from the
test classname. Ambiguous values become `unattributed` and make the baseline
incomplete.

The defaults are:

- APIs: `sync`, `async`
- backends: `memory`, `json`, `sqlite`, `sqlite-sharded`, `duckdb`, `parquet`,
  `mongodb`
- reference backend: `mongodb`

Use `--apis`, `--backends`, and `--reference-backend` only when the expected
matrix is intentionally smaller or uses different labels. Names may contain
alphanumeric or underscore segments separated by single hyphens. The name
`unattributed` is reserved.

## Publishability and scoring

A baseline is publishable only when:

- every discovered contract appears exactly once for every configured
  API/backend target;
- no expected cell is missing or skipped;
- every case has an API, backend, and suite attribution; and
- each same-API MongoDB reference contract passed.

Incomplete inputs still produce diagnostic reports. Their baseline status and
blockers make clear why the result must not be published as a compatibility
claim.

Only unique, evaluated non-MongoDB cells with a passing same-API MongoDB
reference are scored:

```text
(passed + xpassed) /
(passed + xpassed + xfailed + failed + error)
```

- A pass is compatible.
- An expected failure (`xfail`) is an evaluated, known incompatibility.
- A strict expected failure that unexpectedly passes (`xpass`) is compatible,
  but pytest still fails until the stale known-gap marker is removed.
- A failure or error is evaluated and incompatible.
- An ordinary skip makes the matrix incomplete.
- MongoDB reference cells validate behavior but are not part of TinyMongo's
  numerator or denominator.
- Duplicate, unattributed, or reference-unqualified cells are excluded and
  reported as blockers.

Scores are shown overall and by API, backend, and suite. Percentages are rounded
to two decimal places; JSON `null` means there were no eligible evaluated cells.

## JSON schema

Schema version 2 contains:

- `baseline`: completeness, publishability, expected and observed targets,
  missing/skipped/duplicate cells, attribution failures, and unqualified
  references;
- `scoring`: formula, reference backend, qualified score, and excluded cases;
- `totals`: counts for every JUnit outcome;
- `targets`: per API/backend counts, integrity, score, and test details;
- `summaries`: API, backend, and suite rollups;
- `known_gaps`: expected failures with dimensions and reasons; and
- `unexpected_passes`: strict expected failures whose behavior now passes.

The schema version changes only when the machine-readable shape changes
incompatibly.

## CI behavior

The MongoDB contract step retains its original pass or failure status. Report
generation and artifact upload use `if: always()`, so an ordinary contract
failure still produces diagnostics. The Markdown report is also appended to the
GitHub Actions job summary when generation succeeds.

If pytest stops before creating JUnit XML, CI emits a warning and uploads any
files that do exist. Report generation never turns a failed contract run into a
successful job.
