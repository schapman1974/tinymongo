# Changelog

## Unreleased

### Added
- Parquet `storage_uri` support for object-storage paths such as S3-compatible,
  GCS, and Azure Blob Storage URIs.
- Environment-variable mapping for DuckDB object-storage credentials.
- CLI `--storage-uri`, `--source-uri`, and `--target-uri` options.
- Object-storage setup documentation.

## [1.1.1] - 2026-07-09

### Fixed
- Added `$nor` query support for TinyDB JSON, SQLite, DuckDB, and Parquet backends.

## [1.1.0] - 2026-07-09

### Added
- Table-native SQLite, DuckDB, and DuckDB-managed Parquet backends with one table or file per collection.
- SQL compilation for supported Mongo-style filters over `_id` and JSON document fields, with Python fallback for unsupported query shapes.
- Legacy SQLite and DuckDB blob-file migration when older storage files are opened.
- Expanded backend tests, PyMongo compatibility tests, and 100% package coverage.
- Refreshed backend benchmark documentation.

### Changed
- SQLite, DuckDB, and Parquet backends now use table-oriented storage instead of a single TinyDB-style JSON blob.

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
