# Changelog

## [1.0.0] - 2026-06-19

### Added
- TinyDB JSON remains the default storage backend.
- Table-native SQLite, DuckDB, and DuckDB-managed Parquet storage backends.
- `tinymongo` CLI with inspect, list, export, import, and migrate commands.
- Opt-in integration stress tests for concurrent multi-process writes.
- Atomic multi-writer file operations with temp-file replace and optional advisory locking via `portalocker`.
- Comprehensive Mongo-like query coverage and new tests for `$and`, `$or`, `$not`, `$in`, `$all`, nested document queries, and concurrency.
- Additional update operator support for `$set`, `$unset`, `$inc`, `$push`, `$pull`, and `$addToSet`.
- Lightweight in-memory collection index APIs for repeated equality lookups.
- GitHub Actions CI workflow for Python 3.9, 3.10, and 3.11.
- Optional `uv` extra support via `uvicorn`.
- A new benchmark harness comparing Parquet storage to TinyDB JSON storage.
- SQL-backed collection tables with `_id` primary keys and JSON document payloads for SQLite and DuckDB.
- DuckDB-managed Parquet datasets with one Parquet file per collection.
- `pyproject.toml` packaging and modern Python packaging support.

### Changed
- Removed legacy `setup.py` and migrated packaging metadata to `pyproject.toml`.
- Bumped package major version to `1.0.0` for this revamp.
- Added developer requirements and linting/type-checking configuration.

### Fixed
- Parquet writer compatibility issues with older `pyarrow` versions.
- Stale documentation that incorrectly described Parquet as the default backend.
