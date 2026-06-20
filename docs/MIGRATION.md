# Migration Guide

This guide helps you migrate from earlier `tinymongo` versions to `1.0.0`.

## Key changes

- The package now uses `pyproject.toml` for packaging metadata.
- Default storage is now Parquet v2 (`pyarrow`) instead of TinyDB JSON files.
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

- The default storage uses Parquet and requires `pyarrow`.
- If `pyarrow` is not installed, `tinymongo` falls back to TinyDB JSON storage.
- For local development, install `requirements-dev.txt` and run `pytest -q`.
