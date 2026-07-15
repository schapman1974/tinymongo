# Compatibility contracts

These tests describe the application-facing behavior TinyMongo intends to share
with PyMongo and a real MongoDB server. Each contract runs against memory, JSON,
SQLite, DuckDB, and Parquet during the normal unit suite. The same test item is
marked `mongodb` for the real server target.

Run the embedded backend matrix:

```bash
pytest tests/contracts
```

Run the real MongoDB target explicitly:

```bash
TINYMONGO_MONGODB_URI=mongodb://localhost:27017 \
pytest -o addopts='' -q -m mongodb tests/contracts
```

Run the complete embedded-plus-MongoDB matrix in one session with `-m contract`
and `-o addopts=''`. CI uses that form and publishes its JUnit result file as a
workflow artifact.

CI also sets `TINYMONGO_REQUIRE_MONGODB=1`, which turns a missing or unreachable
server into a failure instead of a skip.

Temporary expected differences live in `known_differences.py`. They are strict:
when TinyMongo starts passing one of those contracts, the suite fails until the
obsolete entry is removed. New differences must link to a roadmap issue and must
not be hidden through broad output normalization.
