# Migration Guide

This guide helps you migrate from earlier `tinymongo` versions to the 1.2.1
release line.

## Key changes

- The package now uses `pyproject.toml` for packaging metadata.
- Default storage remains TinyDB JSON storage.
- Parquet v2, SQLite, and DuckDB are available as table-native backends.
- A `tinymongo` CLI is available for inspecting, exporting, importing, and migrating data.
- Common update operators and durable single-field indexes are supported.
- Local JSON writes use advisory locks, atomic replacement, and file/directory
  `fsync`; local table backends use scoped locking plus database/file
  mechanisms, remote SQL relies on database transactions, and object-storage
  Parquet remains single-writer.
- Synchronous and asynchronous operations share data semantics, but async
  database handles and cursors defer storage work while synchronous database
  selection may open storage immediately.
- Datetime, ObjectId, and binary values use TinyMongo's tagged persistence
  codec, and table-native backends use BSON-aware physical `_id` keys.
- An explicitly supplied `_id: None` is preserved and participates in duplicate
  detection instead of being replaced with a generated ID.
- `insert_many()` defaults to `ordered=True`: successful documents before a
  duplicate remain written, then the operation stops and raises
  `BulkWriteError`. Set `ordered=False` to continue other valid documents and
  collect all duplicate-key write errors.

## Installation

```bash
python3 -m pip install -U pip
pip install .
```

Install only the optional integrations you need, or install all of them:

```bash
pip install ".[bson,parquet]"
pip install ".[all]"
```

## Notes

- The default storage uses TinyDB JSON storage.
- SQLite, DuckDB, and Parquet backends now store one real table or Parquet file per collection rather than one serialized database blob.
- DuckDB support requires `duckdb`; Parquet support is DuckDB-managed and also expects `pyarrow` in development/test environments.
- Select optional storage backends with `TinyMongoClient(path, backend="parquet")`, `backend="sqlite"`, or `backend="duckdb"`.
- Older blob-format SQLite and DuckDB files are migrated to collection tables when opened.
- Use `tinymongo migrate SOURCE TARGET --to-backend sqlite` to copy existing TinyDB JSON data into another backend.
- Existing plain JSON and legacy stringified table-backend `_id` keys remain
  readable and mutable after upgrading.
- Before a bulk rewrite or migration, check legacy data for `_id` pairs that
  are BSON-equivalent, such as `1` and `1.0` or native bytes and generic
  subtype-0 `Binary`. Version 1.2.1 rejects new equivalent duplicates while
  keeping booleans distinct from numbers and preserving mapping field order.
- Remote SQL keeps the existing `data` column as a normal, indexable JSON/JSONB
  object. Version 1.2.1 adds a nullable `data_ordered` text column containing
  an encoded copy that preserves embedded-document field order. Existing rows
  with no ordered copy remain readable from `data`, and rewriting one fills
  `data_ordered`. The column is added automatically on first access, so the
  database account needs `ALTER TABLE` permission during the upgrade. Existing
  native indexes continue to target the unchanged `data` object. PostgreSQL
  JSONB may already have normalized field order in legacy rows. TinyMongo can
  recover a literal container `_id` from its legacy physical row key; other
  normalized embedded documents retain the order returned by PostgreSQL.
- Remote SQL and object-storage Parquet no longer create the unused local path
  passed for API/CLI compatibility. Environment-only object-storage URIs and
  remote DSNs participate in CLI database discovery and migration summaries.
- The exact two-key mapping shape containing `__tinymongo_type_v1__` and
  `value` is reserved for the persistence codec. If an older database contains
  a valid tag shape as ordinary user data, whether written through an earlier
  API or edited manually, rename one of those keys before upgrading so
  TinyMongo does not decode it as a tagged value.
- The CLI migrates documents, not source index metadata. It preserves and
  preflights any indexes that already exist on the target collection. On a
  fresh target, recreate the indexes with `collection.create_index()` after
  migration; new index metadata persists with the backend, and unique indexes
  are enforced for later writes. Memory-backend metadata lasts for the named
  namespace's process lifetime.
- For local development, install `requirements-dev.txt` and run `pytest -q`.
