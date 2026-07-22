# Migration Guide

This guide helps you migrate from earlier `tinymongo` versions to `1.0.0`.

## Key changes

- The package now uses `pyproject.toml` for packaging metadata.
- Default storage remains TinyDB JSON storage.
- Parquet v2, SQLite, and DuckDB are available as table-native backends.
- A `tinymongo` CLI is available for inspecting, exporting, importing, and migrating data.
- Common update operators and durable single-field indexes are supported.
- Concurrent writes are safer due to atomic file replace and optional file locks.
- Optional `uv` extras are available for dependency-managed ASGI usage.

## Installation

```bash
python3 -m pip install -U pip
pip install .
```

Or with the optional uv support:

```bash
pip install .[uv]
```

## Notes

- The default storage uses TinyDB JSON storage.
- SQLite, DuckDB, and Parquet backends now store one real table or Parquet file per collection rather than one serialized database blob.
- DuckDB support requires `duckdb`; Parquet support is DuckDB-managed and also expects `pyarrow` in development/test environments.
- Select optional storage backends with `TinyMongoClient(path, backend="parquet")`, `backend="sqlite"`, or `backend="duckdb"`.
- Older blob-format SQLite and DuckDB files are migrated to collection tables when opened.
- Use `tinymongo migrate SOURCE TARGET --to-backend sqlite` to copy existing TinyDB JSON data into another backend.
- Indexes created by earlier releases were collection-handle scoped and cannot
  be migrated automatically. Recreate them once with `collection.create_index()`;
  new index metadata persists with the backend, and unique indexes are enforced
  for later writes. Memory-backend metadata lasts for the named namespace's
  process lifetime.
- For local development, install `requirements-dev.txt` and run `pytest -q`.
