# Changelog

## [Unreleased]

### Added
- A shared, backend-independent aggregation engine for `$match`, `$sort`,
  `$skip`, `$limit`, `$count`, `$project`, `$set`, `$addFields`, `$unset`, and
  `$group`, with `$ifNull`, `$literal`, and `$size` projection expressions,
  `$$REMOVE`, field-path or null group keys, and `$addToSet`, `$avg`, `$first`,
  `$last`, `$max`, `$min`, `$push`, and `$sum` accumulators across synchronous
  and asynchronous clients.
- Cross-backend and real-MongoDB aggregation contracts based on production
  application pipelines, including full inclusion/exclusion and computed
  projection modes, dotted array writes and sorts, BSON ordering, pagination,
  counts, null/missing values, empty inputs, and async cursor behavior.
- MongoDB-style array updates for `$push` with `$each`, `$position`, `$sort`,
  and `$slice`; `$addToSet` with `$each`; and literal, query, or document
  operands for `$pull`, with atomic validation across sync and async backends.
- Exact optional Decimal128 persistence plus numeric equality, range queries,
  sorting, embedded unique indexes, `$inc`, and `$group` accumulators across
  backends.
- Native UUID and compiled-regex persistence plus optional `bson.Regex`
  round trips, BSON-aware querying, sorting, distinct values, IDs, and embedded
  unique-index identity across synchronous and asynchronous backends. UUIDs
  use standard subtype-4 BinData identity, and native or BSON regex values use
  their pattern plus canonical MongoDB options.
- MongoDB-style `$size`, `$elemMatch`, `$type`, and `$mod` query operators,
  including array-member behavior, BSON type aliases and numeric codes, and
  operand validation across synchronous and asynchronous clients.

### Changed
- Aggregation capability reporting now describes the exact supported stages,
  accumulators, and expressions instead of reporting a blanket unsupported
  value. The value therefore changed from falsey `False` to a truthy structured
  mapping; use `client.supports("aggregation")` for a Boolean check or inspect
  the mapping when selecting individual features.
- Query capability reporting now enumerates logical and field operators.
  `bson_types` likewise changed from a Boolean to a structured mapping of
  dependency-free `native` families and installed optional `pymongo` types;
  inspect its `pymongo` tuple when detecting optional BSON support.
- Remote SQL unique indexes fail closed for Decimal128 values, as they already
  do for arrays, when their native JSON constraints cannot guarantee exact
  MongoDB numeric identity across concurrent writers.
- Remote SQL unique indexes also fail closed for Binary, UUID, and regex values
  whose exact cross-process BSON identity cannot be enforced by the native JSON
  constraint.

### Fixed
- **TM-016:** `distinct()` now collapses BSON-equivalent numeric values across
  integers, doubles, and Decimal128 while keeping booleans distinct.
- **TM-018:** Every CRUD filter now rejects unsupported or misspelled query
  operators before storage access instead of silently returning no matches.
- **TM-020:** Duplicate `_id` and unique-index failures now expose MongoDB error
  code `11000` through `DuplicateKeyError`.
- **TM-021:** Applying `$addToSet` to a null or non-array field now raises
  PyMongo-compatible `WriteError` code `2` instead of a bare `ValueError`, while
  preserving update atomicity.
- Aggregation field references no longer cross a raw array nested directly
  inside another array before reaching the requested field.
- Aggregation projections preserve source BSON field order for retained fields
  and append directly computed fields in specification order.
- Cursor and aggregation sorting now traverse dotted paths through multi-item
  arrays consistently instead of treating those values as missing.
- Degraded index batches now reuse an existing equivalent effective index and
  emit one warning instead of creating duplicate metadata or native work.
- Direct equivalent index declarations under a different name now raise
  MongoDB-compatible error code 85, while exact-name retries remain idempotent
  for catalogs created by older releases.
- Unsupported-feature warnings now retain the application call site across
  synchronous calls and async executor threads.
- Embedded unique indexes now use exact numeric identity across integers,
  doubles, and Decimal128 values, including large exactly equivalent integers
  and integral-looking doubles; SQLite attempts a one-time rebuild of legacy
  expression indexes when upgrading the token format.
- If that SQLite rebuild discovers values an older unique token incorrectly
  treated as distinct, TinyMongo raises `DuplicateKeyError`, removes the unsafe
  native expression constraint, and retains fail-closed catalog enforcement so
  the conflicting row can be removed before the index is recreated.
- Typed backend IDs now encode extreme Decimal128 ratios without relying on
  Python's bounded decimal integer conversion.
- Unsorted SQLite projections now shape and release documents during the row
  scan, and `_id`-only sweeps avoid transferring unrequested JSON payloads into
  Python. Sorted cursors retain their complete-document fallback so omitted
  sort keys continue to order results correctly.

## [1.2.1] - 2026-07-31

### Added
- Required CPython 3.14 CI coverage and beta-level CPython 3.14t coverage with
  GIL-disabled unit, concurrency, live-database, and package-install checks,
  plus a dedicated free-threaded dependency profile.
- Process-local `memory` backend for isolated tests and temporary data, with
  explicit same-process sharing through named `memory://NAME` namespaces.
- The memory backend now participates in the shared MongoDB compatibility
  contract matrix.
- Deterministic JSON and Markdown compatibility reports generated from one or
  more contract JUnit files, with sync/async, backend, and suite dimensions,
  MongoDB-qualified scoring, and incomplete-matrix safeguards.
- An external pytest acceptance runner that patches both PyMongo client classes
  before application tests are imported and records report metadata.
- `tinymongo.patch()` context-manager and decorator forms for temporarily
  routing sync and async PyMongo client construction to a configurable
  TinyMongo backend.
- First-class non-blocking async client, database, collection, and lazy cursor
  APIs with awaited cleanup and off-thread storage work.
- Shared MongoDB compatibility contracts now exercise both synchronous and
  asynchronous APIs against every configured backend and reference target.
- Mongo-style inclusion and exclusion projections for `find()` and `find_one()`,
  including nested dotted paths, array behavior, and compatible `_id` handling.
- Durable single-field index metadata and unique constraints across JSON,
  memory, SQLite, DuckDB, Parquet, PostgreSQL, and MariaDB/MySQL backends, with
  native indexes for SQLite and remote SQL.
- Batched duck-typed `IndexModel` planning with enforced unique indexes and
  explicit warnings for safe performance-only degradation.
- Common PyMongo-shaped APIs for database listing/removal,
  `find_one_and_delete()`, `distinct()`, index information, and cursor
  pagination, cloning, closing, and `to_list()`.
- Tagged `datetime` and optional `ObjectId` round trips across all storage
  backends, with `bson` and `pymongo` optional dependency groups.
- Tagged `Binary`, `bytes`, and `bytearray` persistence across all storage
  backends, preserving nonzero BSON subtypes and supporting binary queries.
- A shared BSON scalar registry used by persistence and cursor sorting.

### Changed
- The second positional argument to `find()` is now the PyMongo-compatible
  projection argument. Use `sort=` or cursor `.sort()` for ordering.
- The optional `bson` and `pymongo` dependency groups now require PyMongo 4.9
  or newer, matching the async client and cursor APIs TinyMongo supports.
- Nonunique descending and hashed indexes use ascending equality indexes;
  compound indexes use their ascending leading-field prefix. Sparse and TTL
  differences emit `TinyMongoUnsupportedWarning`, while unsafe unique
  degradation still fails before creating a batch.
- `create_index()` now returns the effective index name, matching PyMongo.
- PostgreSQL and MariaDB/MySQL unique indexes reject array-valued keys because
  their native scalar constraints cannot safely guarantee multikey uniqueness
  across processes.
- `insert_many()` now honors ordered and unordered partial-failure behavior and
  accepts document iterables while raising a PyMongo-shaped `BulkWriteError`
  for duplicate writes.
- SQL, DuckDB, and Parquet backends now use BSON-aware typed physical `_id`
  keys for new rows while retaining read, replace, and delete compatibility
  with legacy stringified keys.
- Collection attributes and subscriptions now select dotted child collections,
  with private attribute access guarded like PyMongo in both APIs.

### Fixed
- Concurrent first access now creates one cached database handle per client,
  compatibility concern documents are no longer shared between instances, and
  lazy table creation for retained collection handles occurs under the write
  lock.
- `bypass_document_validation` remains a compatibility no-op: TinyMongo has no
  user-configurable validator layer, and the flag cannot bypass built-in `_id`
  or declared unique-index constraints.
- Durable index catalogs now use unambiguous collection/index identities, and
  local write locks cover the complete uniqueness preflight and write.
- File-backed deletes and find-and-modify operations now hold their collection
  lock across the complete read/write transaction; AFTER results are fetched
  by captured `_id`, and embedded-document `_id` values no longer break batch
  updates.
- Scalar equality and `$in` match array members consistently on table
  backends while exact array equality remains supported; combined `$nin`,
  `$not`, `$regex`, and `$options` queries
  share missing-field behavior across backends.
- BSON tag-shaped user dictionaries are escaped instead of being mistaken for
  encoded datetimes or ObjectIds, and SQLite unique tokens preserve integers
  outside the signed 64-bit range.
- Async cursors return isolated documents, index listings are async-iterable,
  and overlapping patch scopes in independent tasks cannot strand PyMongo's
  process-global client replacement.
- TinyMongo exceptions inherit matching PyMongo exception classes when PyMongo
  is installed, while retaining dependency-free fallbacks.
- Datetime, ObjectId, and BinData cursor sorts now follow MongoDB ordering;
  numeric `NaN` values have deterministic MongoDB ordering, and unsupported
  sort values emit a bounded diagnostic instead of failing silently.
- Generic subtype-0 `Binary`, `bytes`, and `bytearray` now share MongoDB
  equality semantics in direct queries, the `$in`, `$nin`, and `$all` query
  operators, and `_id` duplicate detection, while nonzero binary subtypes
  remain distinct.
- Non-finite floats use strict JSON-safe persistence tags. Remote SQL retains
  its normal, indexable JSON/JSONB `data` object and adds a nullable
  `data_ordered` text copy to preserve embedded-document field order. Older
  rows remain readable and gain the ordered copy when rewritten; automatic
  schema upgrades leave existing native JSON indexes in place.
- Explicit null `_id` values are preserved, and exact recursive `_id` identity
  prevents scalar/container aliases and codec-normalized tuple/list duplicates.
- Implicit IDs now use native `ObjectId` values when optional BSON support is
  installed, including insert batches and upserts, so their string form can be
  reconstructed with `ObjectId(...)`. Dependency-free writes and the explicit
  `generate_id()` helper retain portable UUID-string IDs.
- `$ne: None` and `$nin` lists containing `None` now exclude missing fields,
  matching MongoDB without changing the missing-field behavior of non-null
  negation.
- Unsupported document values now raise `InvalidDocument` before storage is
  changed, with the original document and nested path context. When PyMongo is
  installed, the error is catchable through both BSON's `InvalidDocument` and
  `PyMongoError`; dependency-free callers retain the TinyMongo error hierarchy.
- Remote SQL duplicate races are reread and replanned so ordered and unordered
  batches retain their documented partial-write behavior; database commit
  failures now propagate instead of being reported as successful writes.
- Top-level `$unset` now persists the exact TinyDB post-image instead of
  allowing removed keys to merge back into the document.
- CLI export/import now round-trips supported BSON values without reordering
  embedded-document fields, and API and CLI collection listings, inspection,
  and migration hide TinyDB's internal `_default` table. Replace imports and
  migrations preflight the complete destination write and restore prior
  documents if a destructive delete or final insertion fails.
- CLI database discovery and migration summaries now honor object-storage URIs
  and remote SQL DSNs supplied only through environment variables.
- Clients reject unknown backend names during construction, report every
  accepted alias, and consistently reject metadata/listing operations after
  close. Remote SQL and object-storage clients no longer create an unused
  local placeholder directory.

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
