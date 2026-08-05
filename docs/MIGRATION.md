# Migration Guide

This guide helps you migrate from earlier `tinymongo` versions to the 1.3.0
release line.

## Key changes

- The package now uses `pyproject.toml` for packaging metadata.
- Default storage remains TinyDB JSON storage.
- Parquet v2, SQLite, and DuckDB are available as table-native backends.
- A `tinymongo` CLI is available for inspecting, exporting, importing, and migrating data.
- The common update subset now includes `$rename`, `$min`, `$max`, `$pop`, and
  `$pullAll` in addition to `$set`, `$unset`, `$inc`, `$push`, `$pull`, and
  `$addToSet`. `$pull` supports ranges, `$in`, `$nin`, `$regex` with optional
  `$options`, `$elemMatch`, `$exists`, `$type`, `$ne`, `$mod`, `$all`, `$size`,
  and document-field `$not`; `$pullAll` uses literal BSON equality. The
  operators use MongoDB-compatible dotted-path, BSON comparison, array,
  immutable `_id`, and atomic error behavior across synchronous and
  asynchronous clients.
- Aggregation now includes the application-driven `$match`, `$sort`, `$skip`,
  `$limit`, `$count`, `$project`, `$set`, `$addFields`, `$unset`, and `$group`
  stages, with the documented expression and accumulator subset.
- Query support now includes `$size`, `$elemMatch`, `$type`, and `$mod`, and all
  CRUD filters reject unknown query operators before touching storage.
- Decimal128, UUID, regex, MinKey, MaxKey, Timestamp, and scoped or unscoped
  Code values join ObjectId, datetime, and Binary in the BSON-aware storage and
  comparison layer when their optional types are available.
- PyMongo-shaped clients honor recursive `document_class`, `tz_aware`, and
  `tzinfo` reads. Datetimes are stored at signed UTC millisecond precision.
- Aggregation, query, update-operator, and BSON-type capabilities are structured
  mappings. Use `client.supports()` for a Boolean check or inspect the mapping
  to select an individual feature.
- Durable single-field indexes remain supported across all backends.
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
- Update specifications are validated before writes. Code that previously
  relied on malformed operator operands, empty or conflicting paths, scalar
  path traversal, or `_id` mutation now receives a PyMongo-compatible
  `WriteError` without a partial update. `$pop` accepts only `1` or `-1`, and
  `$rename` cannot address array elements.
- `$pull` and `$pullAll` leave missing target fields absent and do not increase
  `modified_count`. `$pull`, `$pullAll`, `$push`, and `$addToSet` raise
  `WriteError` code `2` when an existing target is null or not an array. A
  top-level `$not` remains invalid inside `$pull`, matching MongoDB's code `2`.
- Install `tinymongo[bson]` to read or write `ObjectId`, non-generic `Binary`,
  `Decimal128`, BSON `Regex`, `MinKey`, `MaxKey`, `Timestamp`, and `Code`.
  Native datetime, UUID, compiled `re.Pattern`, and subtype-0 bytes remain
  dependency-free. Native compiled patterns deliberately continue to read back
  as `re.Pattern`; PyMongo normally returns `bson.Regex` for the corresponding
  BSON value.
- Direct, non-`_id` `Timestamp(0, 0)` values in inserts and replacements now
  receive a process-local logical timestamp. Nested, array, `_id`, and
  modifier-update zeros remain literal; callers that need a stored zero should
  nest it or use `$set`. Separate processes do not share the logical clock.
- New `Code` values preserve their BSON type, source, and optional recursive
  scope. Older TinyMongo releases stored `Code` as an ordinary string, so no
  migration can distinguish those values from intentional strings or recover
  them automatically. Rewrite known code fields from an authoritative source.
- `InvalidDocument` remains catchable through BSON/PyMongo when installed, but
  TinyMongo also reports the collection, document `_id`, and complete nested
  path. Keep that richer context when wrapping or logging migration failures.
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
- Remote SQL unique indexes now use private materialized token columns so native
  constraints preserve exact BSON int/float identity without SQL numeric
  rounding. Existing unique indexes upgrade lazily under a collection lock. The
  upgrade preflights and backfills current rows before replacing the native
  index; exact legacy duplicates raise `DuplicateKeyError` and keep the catalog
  version stale for a fail-closed retry. The database account needs `ALTER
  TABLE`, index creation, and index removal privileges. Stop older TinyMongo
  writers before allowing the upgraded client to migrate these indexes.
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
