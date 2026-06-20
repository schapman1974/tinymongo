# Migration Guide

This guide helps you migrate from earlier `tinymongo` versions to `1.0.0`.

## Key changes

- The package now uses `pyproject.toml` for packaging metadata.
- Default storage remains TinyDB JSON storage.
- Parquet v2, SQLite, and DuckDB are available as optional storage backends.
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
- For local development, install `requirements-dev.txt` and run `pytest -q`.
