# Compatibility reports

TinyMongo turns the shared contract suite's pytest JUnit XML into two
deterministic reports:

- `compatibility-report.json` is a versioned, machine-readable record with
  per-test and per-target outcomes.
- `compatibility-report.md` summarizes the score, per-target counts, known
  gaps, failures, errors, and unexpected strict-xfail passes.

The reports omit timestamps, durations, hostnames, and input paths. Common
absolute paths captured in JUnit reason messages are replaced with
`<ABSOLUTE_PATH>`, so otherwise-identical outcomes from different temporary
directories produce byte-for-byte identical output.

## Generate reports locally

First write JUnit XML while running the complete embedded-plus-MongoDB contract
matrix:

```bash
TINYMONGO_MONGODB_URI=mongodb://127.0.0.1:27017/?directConnection=true \
TINYMONGO_REQUIRE_MONGODB=1 \
pytest -o addopts='' -q -m contract tests/contracts \
  --junitxml=contract-results.xml
```

Then generate both reports, even if pytest returned a nonzero status because a
contract failed:

```bash
python scripts/generate_compatibility_report.py contract-results.xml \
  --json-output compatibility-report.json \
  --markdown-output compatibility-report.md
```

Without the output options, those same two report filenames are used in the
current directory. Use `--targets` and `--reference-target` only for a custom
contract matrix; the defaults match memory, JSON, SQLite, DuckDB, Parquet, and
MongoDB. Target names may contain alphanumeric or underscore segments separated
by single hyphens. The name `unattributed` is reserved for cases whose pytest
parameter ID cannot be matched safely.

## JSON structure

The top-level `schema_version` changes only when the machine-readable shape
changes incompatibly. Version 1 contains:

- `scoring`: the formula, reference target, and aggregate numerator,
  denominator, and percentage;
- `totals`: counts for every outcome across the input JUnit file;
- `targets`: ordered per-target roles, counts, scores, and individual test
  outcomes;
- `known_gaps`: expected failures with target, contract, test name, and reason;
- `unexpected_passes`: strict expected failures whose behavior now passes.

Percentages are rounded to two decimal places. A JSON `null` percentage means
that target had no evaluated cases.

## Scoring

A **backend-contract cell** is one contract case executed for one TinyMongo
backend. The overall score is:

```text
(passed + xpassed) /
(passed + xpassed + xfailed + failed + error)
```

- A pass is compatible.
- An expected failure (`xfail`) is an executed, known incompatibility. It stays
  in the denominator and does not count as compatible.
- A strict expected failure that unexpectedly passes (`xpass`) demonstrates
  compatible behavior, so it counts as compatible. Pytest still fails the test
  run until the stale known-gap marker is removed.
- A failure or error is evaluated but incompatible.
- An ordinary skip is reported but excluded because the behavior was not
  executed. A target with only skips has no score rather than a misleading
  zero.
- The configured reference target is excluded from TinyMongo's aggregate score
  while its outcomes remain visible. MongoDB is the default reference.
- Cases that cannot be attributed to one configured target appear under
  `unattributed` and are excluded from the aggregate rather than silently
  changing it.

The aggregate is weighted by backend-contract cells. This means a behavior gap
on three backends contributes three incompatible cells. Per-target scores make
that weighting visible.

## JUnit interpretation

Pytest writes expected failures as `<skipped type="pytest.xfail">` elements;
the generator uses that type to distinguish them from ordinary skips. Strict
unexpected passes are emitted as failures whose normalized message begins with
the exact marker `[XPASS(strict)]`; only that marker or an exact xpass result
type is classified as `xpassed`. The original pytest step remains failed.

Target names are recovered from pytest's bracketed parameter ID, such as
`test_name[sqlite]`, `test_name[value-sqlite]`, or
`test_name[value-remote-sql]`. Matching uses full configured names at hyphen
boundaries, so a target name may itself contain hyphens. A parameter ID
containing no configured target, or matching more than one configured target,
is reported as `unattributed`.

Message normalization recognizes quoted or unquoted Unix paths beginning with
`/`, Windows drive paths such as `C:\\...` or `C:/...`, and UNC paths beginning
with `\\\\`. Quoted paths may contain spaces. An unquoted path ends at
whitespace or common message punctuation; unusual unquoted paths containing
those characters may therefore be only partly redacted. Relative paths and
URLs are intentionally left intact because treating them as absolute paths
would hide useful test details.

## CI failure behavior

The MongoDB contract step runs normally and retains its original success or
failure status. Report generation and artifact upload use `if: always()`, so a
JUnit file containing ordinary contract failures still produces JSON and
Markdown diagnostics. Generating a report never converts a failed contract run
into a successful job.

If pytest stops before creating JUnit XML, CI emits a warning and uploads any
files that do exist; there is no input from which a compatibility report can be
generated.
