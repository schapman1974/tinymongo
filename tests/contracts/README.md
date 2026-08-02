# Compatibility contracts

These tests describe the application-facing behavior TinyMongo intends to share
with PyMongo and a real MongoDB server. Each contract runs through both the
synchronous and asynchronous APIs against memory, JSON, SQLite, DuckDB, and
Parquet during the normal unit suite. The same test item is marked `mongodb`
for the real server reference.

Run the embedded backend matrix:

```bash
pytest tests/contracts
```

Run the real MongoDB target explicitly:

```bash
TINYMONGO_MONGODB_URI=mongodb://localhost:27017 \
pytest -o addopts='' -q -m mongodb tests/contracts
```

Run the complete sync/async, embedded-plus-MongoDB matrix in one session with
`-m contract` and `-o addopts=''`. CI uses that form and publishes its JUnit
result file plus deterministic JSON and Markdown compatibility reports as a
workflow artifact and job summary.

To generate those reports locally, add
`--junitxml=contract-results.xml` to the pytest command and run:

```bash
python scripts/generate_compatibility_report.py contract-results.xml
```

Run only the BSON comparison slice and generate its two-API, six-target report
(memory, JSON, SQLite, DuckDB, Parquet, and the live MongoDB reference) with:

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

The scoring rules, complete command, report schema, and CI failure behavior are
documented in [Compatibility reports](../../docs/COMPATIBILITY_REPORTS.md).

CI also sets `TINYMONGO_REQUIRE_MONGODB=1`, which turns a missing or unreachable
server into a failure instead of a skip.

Temporary expected differences live in `known_differences.py`. They are strict:
when TinyMongo starts passing one of those contracts, the suite fails until the
obsolete entry is removed. New differences must link to a roadmap issue and must
not be hidden through broad output normalization.
