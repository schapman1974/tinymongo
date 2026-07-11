# Changelog

## [1.2.0] - 2026-07-11

### Added
- PyMongo-style `upsert=True` support for `update_one()`, `update_many()`, and
  `replace_one()` across JSON and table-native backends.
- Basic MongoEngine compatibility for document creation, repeated saves,
  queries, atomic updates, deletes, counts, and collection drops.
- Client and database context-manager support with deterministic resource
  cleanup.
- Explicit storage, corruption, and lock exception types.
- Optional dependency groups for DuckDB, Parquet, serialization, PostgreSQL,
  MariaDB/MySQL, and complete backend installations.

### Changed
- `update_one()` and `update_many()` now follow PyMongo semantics and require
  update operators; callers must use `replace_one()` for document replacement.
- PyMongo, MongoEngine, DuckDB, PyArrow, and serialization support are no longer
  mandatory core dependencies.
- TinyDB now allows compatible 3.x releases with `tinydb>=3.2.1,<4`.
- Python 3.9 is now the minimum supported version, with CI coverage through
  Python 3.13.
- Update results now distinguish matched and modified counts and expose
  PyMongo-shaped raw result metadata.

### Fixed
- Parquet writes now acquire advisory locks, and table-native Parquet
  read-modify-write operations use inter-process locking and atomic replacement.
- Missing-document `delete_one()` calls now return a zero-count result.
- Corrupt storage and invalid updates now raise explicit errors instead of being
  silently treated as empty or unmatched operations.
- Test database paths are isolated so the unit suite no longer rewrites tracked
  repository fixtures.

### Quality
- Package statement and branch coverage is enforced at 100% in CI.
- Black, Ruff, and mypy checks are enforced rather than advisory.

## [1.1.2] - 2026-07-09

### Added
- Parquet `storage_uri` support for object-storage paths such as S3-compatible,
  GCS, and Azure Blob Storage URIs. This is experimental and currently uses one
  Parquet file per collection.
- Environment-variable mapping for DuckDB object-storage credentials.
- CLI `--storage-uri`, `--source-uri`, and `--target-uri` options.
- Object-storage setup documentation.
- Remote PostgreSQL and MariaDB/MySQL table backends with DSN/env-var
  configuration.
- CLI `--dsn`, `--source-dsn`, and `--target-dsn` options.
- Remote SQL setup documentation and opt-in integration tests.
- Install-command guidance when optional backend drivers are missing.

### Notes
- Object-storage Parquet in this release is best suited for experimentation,
  portable datasets, and single-writer workflows. Updates and deletes rewrite
  the collection file; use PostgreSQL or MariaDB/MySQL for remote transactional
  workloads.

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
