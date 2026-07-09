# Migration Guide

This guide helps you migrate from earlier `tinymongo` versions to `1.0.0`.

## Key changes

- The package now uses `pyproject.toml` for packaging metadata.
- Default storage remains TinyDB JSON storage.
- Parquet v2, SQLite, and DuckDB are available as optional storage backends.
- A `tinymongo` CLI is available for inspecting, exporting, importing, and migrating data.
- Common update operators and lightweight in-memory equality indexes are supported.
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
- Parquet storage requires `pyarrow`; DuckDB storage requires `duckdb`.
- Select optional storage backends with `TinyMongoClient(path, backend="parquet")`, `backend="sqlite"`, or `backend="duckdb"`.
- Use `tinymongo migrate SOURCE TARGET --to-backend sqlite` to copy existing TinyDB JSON data into another backend.
- In-memory indexes created with `collection.create_index("field")` are scoped to the active collection object and are rebuilt from stored documents as needed.
- For local development, install `requirements-dev.txt` and run `pytest -q`.
