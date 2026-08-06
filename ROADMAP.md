# TinyMongo roadmap

TinyMongo aims to provide strong, practical MongoDB and PyMongo compatibility
while remaining an embedded, multi-backend database.

The compatibility target is to pass at least 90% of a published,
application-focused contract suite against real MongoDB. Unsupported server
features should fail clearly instead of behaving approximately. Talk Python is
the first end-to-end application target: its real data layer should run through
TinyMongo's synchronous and asynchronous APIs without rewriting its data-access
call sites, with any remaining differences reported explicitly.

The live checklist is [GitHub issue #103](https://github.com/schapman1974/tinymongo/issues/103),
and progress by release is available on the
[milestones page](https://github.com/schapman1974/tinymongo/milestones).

## 1. Compatibility foundation and application acceptance

[View milestone](https://github.com/schapman1974/tinymongo/milestone/1)

Prerequisite backend work:

- [x] [#69: In-memory storage backend](https://github.com/schapman1974/tinymongo/issues/69)
- [x] [#70: PyMongo patching helper](https://github.com/schapman1974/tinymongo/issues/70)
- [x] [#74: Field projections](https://github.com/schapman1974/tinymongo/issues/74)
- [x] [#76: Durable indexes and unique constraints](https://github.com/schapman1974/tinymongo/issues/76)

### Mike Kennedy's Talk Python and agent-guide follow-up

This checklist combines Mike Kennedy's
[Talk Python acceptance blockers](https://github.com/schapman1974/tinymongo/issues/136#issuecomment-5112320332)
with the follow-up audit of his
[TinyMongo agent reference](https://github.com/mikeckennedy/python-package-guides-for-agents/blob/main/package-guides/tinymongo_reference.md):

- [x] Redirect the real Talk Python data layer through TinyMongo's asynchronous
  client on SQLite, initialize all 16 collections, and reduce the observed
  failures to backend-independent reproductions.
- [x] Complete the Talk Python slice of
  [#94: MongoDB BSON comparison and sort order](https://github.com/schapman1974/tinymongo/issues/94)
  for `datetime`, `ObjectId`, and BinData values in both directions, including
  mixed naive/aware datetimes and compound sorts.
- [x] Preserve binary payloads from `Binary`, `bytes`, and `bytearray` through
  storage and queries, including the BSON subtype and nested or large values;
  normalize `bytearray` to `bytes` on read as part of
  [#75](https://github.com/schapman1974/tinymongo/issues/75).
- [x] Apply BSON-aware equality to direct queries, the `$in`, `$nin`, and `$all`
  query operators, and `_id` identity: native bytes equal generic Binary
  subtype 0, nonzero subtypes remain distinct, booleans remain distinct from
  numbers, and equivalent numeric representations share one key.
- [x] Use typed physical `_id` keys on SQL, DuckDB, and Parquet backends without
  breaking reads, replacements, or deletes for existing stringified keys.
- [x] Generate native `ObjectId` values for implicit IDs when optional BSON
  support is installed across inserts and upserts, while keeping dependency-free
  UUID fallback IDs and the explicit string-returning `generate_id()` helper.
- [x] Match MongoDB's null-negation boundary: `$ne: None` and `$nin` lists
  containing `None` exclude missing fields, while non-null negation continues
  to include them.
- [x] Raise `InvalidDocument` before writes containing unsupported values, with
  original-document and nested-path context plus BSON, PyMongo, and
  dependency-free TinyMongo catch compatibility.
- [x] Preserve embedded-document field order and strict-JSON non-finite floats
  in remote SQL rows while continuing to read legacy rows without an ordered
  copy.
- [x] Emit one useful diagnostic per unsupported sort type and field instead of
  silently returning insertion order.
- [x] Capture the batch rejection Mike observed when unsupported Binary made
  one document unserializable, and verify the real PyMongo reference behavior.
- [x] [#140: Match `insert_many()` ordered and partial-failure semantics](https://github.com/schapman1974/tinymongo/issues/140):
  accept document iterables; ordered inserts preserve the successful prefix
  and stop at the first error; unordered inserts continue valid documents and
  report every duplicate-key write failure in a `BulkWriteError`. TinyMongo
  deliberately preflights its complete local batch, so client-side encoding
  failures remain all-or-nothing even though PyMongo may split very large
  inputs across wire batches.
- [x] Preserve exact TinyDB update post-images so top-level `$unset` really
  removes a field while nested and missing-field cases retain their expected
  behavior.
- [x] Make CLI export/import BSON-aware, safely preflight complete
  replace/migration writes, and keep TinyDB's internal `_default` table out of
  API and CLI collection listings, inspection, and migration.
- [x] Match PyMongo's dotted child-collection behavior and private-attribute
  guard for synchronous and asynchronous handles.
- [x] Apply the agent-guide audit to runtime configuration: honor
  environment-only object-storage/remote SQL settings in CLI discovery and
  migration reports, avoid unused nonlocal-storage directories, reject invalid
  backends eagerly with the complete alias list, and guard client metadata and
  listing methods after close.
- [x] Complete Mike's second-application aggregation acceptance rerun: all 9 of
  9 real application call sites pass against the current `master` aggregation
  subset, including projection edge cases and computed-field stages.
- [x] **TM-011:** Apply normalized projections during unsorted SQLite scans so
  complete source rows are released one at a time, with an `_id`-only SQL path
  that never transfers large unrequested payloads into Python. A deterministic
  public sweep regression now verifies materially lower peak memory; Mike's
  attached 400-document, 200,000-character reproduction dropped from 160.26 MB
  to 0.26 MB locally. Sorted SQLite cursors and non-SQLite backends explicitly
  retain the full-document fallback needed by their current ordering and scan
  implementations. Mike's private 559-transcript rerun confirmed projected
  single-document reads remain near 0.1 MB and separated the remaining
  unprojected limit/count work into TM-015.
- [x] **TM-015:** Defer SQLite cursor scans until the final unsorted window is
  known, push `skip`/`limit` into unindexed SQL scans or a streaming Python
  filter, and count
  SQL-compatible filters without fetching document payloads. Deterministic
  sync and async regressions verify that `find_one({})` and
  `find({}).limit(1)` decode one row, `count_documents({})` decodes none, and
  sorted cursors still consider the complete candidate set.
  - [x] Mike's private 559-transcript rerun remained bounded on both clients:
    `find_one({})` peaked near 0.1 MB and `count_documents({})` near 0.0 MB,
    with the correct count of 559.
- [x] **TM-001:** When a degraded index resolves to an effective key
  specification that already exists, warn once and skip the duplicate native
  index instead of creating redundant metadata or storage work.
- [x] **TM-002:** Document that `tinymongo.patch()` replaces PyMongo module
  attributes and therefore cannot replace client names imported before the
  patch scope begins.
- [x] **TM-012:** Document SQLite's measured multi-process behavior: writes are
  safe but store-wide and serialized, use a 30-second lock timeout, and allow
  concurrent WAL reads; describe the differing Parquet lock scope separately.
- [x] **TM-005:** Preserve application-level caller attribution for warnings
  emitted by synchronous work dispatched through the async executor.
- [x] **TM-016:** Make `distinct()` collapse BSON-equivalent integers, doubles,
  and Decimal128 values while keeping booleans distinct from numbers.
- [x] **TM-018 / #77:** Reject unsupported or misspelled query operators before
  storage access, and implement the application-prioritized `$size`,
  `$elemMatch`, `$type`, and `$mod` query slices across CRUD entry points.
- [x] Enumerate supported logical and field query operators plus dependency-free
  and optional-PyMongo BSON type families through structured capabilities.
- [x] **TM-020:** Report MongoDB duplicate-key code `11000` for duplicate `_id`
  and unique-index failures.
- [x] **TM-021:** Raise PyMongo-compatible `WriteError` code `2` when
  `$addToSet` targets a null or non-array field, without partially applying the
  update.

#### Mike's [TM-022 through TM-025 follow-up](https://github.com/schapman1974/tinymongo/issues/136#issuecomment-5156142848)

- [x] **TM-022:** Accept top-level `$comment` metadata and ignore it while
  matching, including filters that contain only a comment.
- [x] **TM-023:** Add MongoDB-compatible codes to query-validation, regex, and
  index `OperationFailure` paths.
- [x] **TM-024:** Honor `tinymongo_folder` as the legacy sync/async client's
  `foldername` alias, detect conflicting values, and reject unknown constructor
  keywords instead of silently ignoring them.
- [x] **TM-025:** Reject bare scalar, list, and empty-document `$not` operands
  with `OperationFailure` code `2` while retaining non-empty query-document and
  compiled-regex forms.

#### Mike's [TM-026 through TM-029 follow-up](https://github.com/schapman1974/tinymongo/issues/136#issuecomment-5159109461)

- [x] **TM-026:** Reject unknown or misspelled PyMongo-shaped connection kwargs
  on synchronous and asynchronous `MongoClient` with `ConfigurationError`,
  while retaining recognized connection options for drop-in use.
- [x] **TM-027:** Report code `86` for every same-name index conflict while
  preserving code `85` for the same key specification under a different name.
- [x] **TM-028:** Report code `51075` when `$options` is combined with flags
  already embedded in a native or BSON regex.
- [x] **TM-029:** Accept and ignore `$comment` in document-form `$elemMatch`
  filters, and reject it as a field operator with `OperationFailure` code `2`.

#### Mike's [TM-019 / TM-030 final fidelity follow-up](https://github.com/schapman1974/tinymongo/issues/136#issuecomment-5159617517)

- [x] **TM-019:** Store and return datetimes with BSON's signed UTC
  millisecond precision, including correct pre-epoch flooring and no-op write
  results for changes within one millisecond.
- [x] **TM-030:** Honor `document_class` recursively and apply `tz_aware` plus
  optional `tzinfo` across synchronous and asynchronous result paths.
- [x] Derive accepted connection option names from the installed PyMongo
  validator catalog, with a dependency-free fallback pinned in TinyMongo.
- [x] Match MongoDB error code `2` for unknown or misplaced operators inside
  `$elemMatch`, while preserving explicit unsupported errors for valid MongoDB
  predicates outside TinyMongo's subset.

#### Mike's [TM-031 through TM-035 array and BSON follow-up](https://github.com/schapman1974/tinymongo/issues/136#issuecomment-5162156105)

- [x] **TM-031:** Make `$pull` a true no-op for a missing field, preserving the
  missing state and an accurate `modified_count`.
- [x] **TM-032:** Support `$in`, `$nin`, `$regex` with optional `$options`, and
  `$elemMatch` operands inside `$pull`, while matching MongoDB's code `2`
  rejection for a top-level `$not` operand.
- [x] **TM-033:** Raise PyMongo-compatible `WriteError` code `2` when `$pull`
  or `$push` targets an existing null or non-array value.
- [x] **TM-034:** Add `$pullAll` with literal BSON equality, missing-field
  no-op behavior, non-array validation, and structured capability reporting.
- [x] **TM-035:** Persist, query, compare, sort, and report optional BSON
  `MinKey`, `MaxKey`, `Timestamp`, and scoped or unscoped `Code` values in
  MongoDB's BSON order across synchronous and asynchronous clients.
- [x] Retain native `re.Pattern` values as a documented TinyMongo convenience,
  and preserve TinyMongo's more diagnostic `InvalidDocument` messages with
  collection, `_id`, and full nested-path context.

#### Mike's [TM-036 through TM-038 BSON follow-up](https://github.com/schapman1974/tinymongo/issues/136#issuecomment-5169028297)

- [x] **TM-036:** Exempt `MinKey` and `MaxKey` range operands from BSON type
  bracketing so inclusive whole-range scans work through `find()`, aggregation
  `$match`, and `$pull`, including missing fields and stored boundary values.
- [x] **TM-037:** Stamp direct, non-`_id` `Timestamp(0, 0)` values during
  inserts and replacement writes with a process-local logical clock while
  preserving literal zeros in nested values, arrays, IDs, and modifier
  updates, matching MongoDB's write boundary.
- [x] **TM-038:** Extend `$pull` through the shared matcher for the remaining
  measured predicate set: `$exists`, `$type`, `$ne`, `$mod`, `$all`, `$size`,
  and document-field `$not`; preserve top-level `$not` code `2` and MongoDB's
  `$expr` refusal code `224`.

#### Mike's [TM-039 replacement-upsert follow-up](https://github.com/schapman1974/tinymongo/issues/136#issuecomment-5174549635)

- [x] **TM-039:** Preserve an `_id` pinned by a replacement-upsert top-level
  direct value or sole `$eq` predicate across every backend and synchronous or
  asynchronous client, including the returned `upserted_id` and immediate
  lookup by the same key.

#### Mike's [TM-040 SQLite batch-scaling follow-up](https://github.com/schapman1974/tinymongo/issues/136#issuecomment-5186712357)

- [x] **TM-040:** Replace repeated collection-wide SQLite duplicate preflights
  with chunked primary-key probes for the incoming `_id` candidates when no
  user-created unique index requires complete token state. Preserve exact BSON
  identity, ordered and unordered partial failures, native-race retries, and
  compatibility with released legacy-store formats through conservative
  fallbacks.
- [x] Add a fixed-200-batch scaling benchmark that reports successive
  collection-size windows and decoded existing-row counts. On the local
  30,000-document comparison, the targeted path reduced total time from
  18.90 seconds to 3.94 seconds and removed a 5.35x first-to-last slowdown.

The normalized unique-token ledger remains a possible later optimization for
bulk inserts into collections with user-created unique indexes. TM-040's
measured no-index application path no longer needs that larger migration.

#### SQLite candidate-selective reads and updates

- [x] Reuse declared top-level SQLite expression indexes as conservative
  candidate sources for complex positive `$and` reads with scalar equality or
  `$in` anchors. Push safe numeric ranges and `$mod` into SQLite, then retain
  the shared BSON matcher as final authority for arrays, extended BSON values,
  large numbers, projections, counts, and cursor bounds.
- [x] Add a reproducible 10,000-document complex-read comparison. The local
  warmed run improved TinyMongo SQLite from about 6.4 to 73.9 queries/second
  while returning the same 215 decoded rows as raw SQLite and MongoDB; raw
  SQLite reached 361.2 and MongoDB reached 136.0 queries/second.

- [x] Route exact `_id` updates through SQLite's primary key inside the existing
  `BEGIN IMMEDIATE` transaction instead of BSON-decoding the complete
  collection. Missing ordinary IDs decode no payload rows, while legacy
  container and datetime IDs retain the compatibility scan when necessary.
- [x] Reuse declared non-unique indexes for top-level bool/int/float/string
  equality to restrict `update_one()` and `update_many()` to scalar plus
  array/object candidates, then apply the exact shared BSON matcher in natural
  row order. NaN, oversized integers, rich BSON predicates, dotted fields, and
  collections with user-created unique indexes deliberately retain the
  conservative full scan.
- [x] Extend the SQLite/raw SQLite/MongoDB comparison benchmark with durable
  `_id` point updates. In the controlled 10,000-document local comparison, the
  TinyMongo point-update average fell from 98.653 ms to 4.330 ms (22.8x), and
  the indexed 1,000-document update fell from 0.228 seconds to 0.144 seconds.

- [x] **Remote SQL numeric uniqueness:** Persist versioned, fixed-width digests
  of canonical BSON scalar tokens for PostgreSQL and MariaDB/MySQL unique
  indexes. Native constraints now enforce exact cross-process equality for
  every int/float representation while keeping booleans distinct. Legacy
  unique indexes are locked, preflighted, backfilled, and upgraded lazily;
  conflicting legacy values fail closed without advancing the catalog version.
  Decimal128 remains fail-closed until the same token can be derived safely
  from its BID representation.
- [ ] Update Mike's agent guide against the `v1.3.0` tag and describe the
  expanded API as available from PyPI.
  Correct its list-only `insert_many()` signature
  and defaults, `BulkWriteError` details, blanket session-rejection claim,
  conditional `AsyncMongoClient` patch/import caveat, numeric-equivalence
  versus boolean identity rules, explicit null `_id` handling, `_default`
  collection filtering, current error/result shapes, and `bytearray`
  normalization. Also distinguish PyMongo from a core dependency: it is an
  optional runtime dependency for ObjectId and nonzero Binary values, patching,
  and conditional exception inheritance. Document the native automatic
  `ObjectId` versus explicit string `generate_id()` behavior, null-negation
  handling for missing fields, and the contextual `InvalidDocument` hierarchy.
  Correct the remaining constructor, sync-laziness, validation, sort,
  locking, environment-variable,
  capabilities-detection, portable-error-catching, index-migration wording,
  and the contradictory async `db.name` collection example identified by the
  guide audit.
- [x] Under [#77](https://github.com/schapman1974/tinymongo/issues/77), reject
  unsupported query operators with `TinyMongoNotSupportedError` instead of
  silently returning no matches, and implement `$size`, `$elemMatch`, `$type`,
  and `$mod`, plus the `$rename`, `$min`, `$max`, and `$pop` update operators
  with MongoDB-shaped path validation, error codes, and structured capability
  reporting.
- [x] Under [#94](https://github.com/schapman1974/tinymongo/issues/94), complete
  the BSON-aware `$gt`, `$gte`, `$lt`, and `$lte` contracts across recursive
  documents and arrays, BSON type bracketing, query and `$pull` paths, and
  MongoDB-compatible datetime, BinData, and regex boundaries. The focused
  sync/async matrix covers all five embedded backends plus live MongoDB.
- [ ] Extend unique-index tokens to supported BSON values through
  [#75](https://github.com/schapman1974/tinymongo/issues/75) and
  [#76](https://github.com/schapman1974/tinymongo/issues/76).
- [x] Run the real Talk Python acceptance suite against MongoDB and TinyMongo
  SQLite: both passed all 597 tests, and the 81,017-document application
  database migrated with zero rejections.
- [x] Rerun Mike's TM-009 and TM-010 reproductions plus the invalid-document
  write path after the fixes merged; all pass against the real application.
- [ ] Publish the MongoDB, memory, and SQLite report artifacts through
  [#72](https://github.com/schapman1974/tinymongo/issues/72) and
  [#136](https://github.com/schapman1974/tinymongo/issues/136).

Application-compatibility work:

- [ ] [#136: Full non-blocking PyMongo async API parity](https://github.com/schapman1974/tinymongo/issues/136)
  - [x] Public async client/database/collection/cursor facade with lazy cursors,
    off-thread storage calls, async cleanup, and sync-result parity.
  - [x] Run the application-derived compatibility contracts through both the
    synchronous and asynchronous APIs.
  - [x] Run the complete application suite through the async acceptance path:
    MongoDB and TinyMongo SQLite each passed 597 tests.
  - [x] Complete the write-heavy admin and multi-worker SQLite follow-up: all
    21 admin checks passed with no errors, lost writes, or torn reads.
  - [x] Close the TM-019/TM-030 returned-value gaps for recursive document
    classes and MongoDB-compatible datetime decoding.
- [x] [#73: Common client, collection, and cursor API](https://github.com/schapman1974/tinymongo/issues/73)
- [x] [#75: Optional BSON serialization](https://github.com/schapman1974/tinymongo/issues/75)
  - [x] Milestone 1 ObjectId, datetime, and Binary storage/query support.
  - [x] **TM-014:** Add Decimal128 round trips and numeric behavior.
  - [x] Add UUID and regular-expression round trips.
- [x] [#77: Additional query and update operators](https://github.com/schapman1974/tinymongo/issues/77)
  - [x] Talk Python query slice: scalar-to-array equality, combined `$nin`,
    `$not`, `$regex`, case-insensitive `$options`, and Mongo-correct
    null-versus-missing negation.
  - [x] Reject unsupported or misspelled query operators across CRUD filters and
    add `$size`, `$elemMatch`, `$type`, and `$mod`.
  - [x] Complete the remaining update slice with `$rename`, `$min`, `$max`, and
    `$pop`, including dotted and numeric-array paths, atomic validation,
    MongoDB-compatible errors, and structured capability reporting.
- [ ] [#102: Optional PyMongo-version adaptation](https://github.com/schapman1974/tinymongo/issues/102)
  - [x] TinyMongo errors conditionally inherit matching PyMongo errors.
  - [x] Derive accepted connection option names from the installed PyMongo
    validator catalog while retaining a dependency-free fallback.
  - [ ] Add version-matrix CI plus broader signature and result adaptation.
- [ ] [#72: Real MongoDB and application contract suite](https://github.com/schapman1974/tinymongo/issues/72)
  - [x] Generate deterministic, dimensioned compatibility reports from the
    sync/async backend and MongoDB contract matrix.
  - [x] Provide an external pytest runner that patches both PyMongo client
    classes before application tests are imported.
  - [x] Run the real Talk Python suite through the runner against the MongoDB
    reference and TinyMongo SQLite, with 597 passing tests on each.
  - [ ] Publish the reference, memory, and SQLite report artifacts.
- [ ] [#87: Differential compatibility fuzzing](https://github.com/schapman1974/tinymongo/issues/87)

Exit criteria:

- Every supported synchronous operation has a PyMongo-shaped asynchronous peer;
  immediate cursor-building calls stay immediate and blocking work stays off the
  event loop.
- Shared contracts run both APIs against real MongoDB and every applicable
  backend.
- Talk Python runs against TinyMongo's async client, and any remaining
  incompatibilities are captured as reproducible contracts or documented
  differences.
- Generated cases can reproduce behavioral differences.
- Memory, projection, patching, durable indexes, and common APIs are available.

## 2. Aggregation core

[View milestone](https://github.com/schapman1974/tinymongo/milestone/2)

- [x] [#82: Shared aggregation pipeline engine](https://github.com/schapman1974/tinymongo/issues/82)
  - [x] Land the first shared sync/async engine slice with pipeline validation,
    compatible cursors, structured capability reporting, and clear unsupported
    feature errors.
- [x] [#96: Basic aggregation stages](https://github.com/schapman1974/tinymongo/issues/96)
  - [x] Implement `$match` through the existing query matcher.
  - [x] Add `$sort`, `$skip`, `$limit`, and `$count` through the shared
    synchronous and asynchronous engine.
- [x] [#97: Aggregation projection stages](https://github.com/schapman1974/tinymongo/issues/97)
  - [x] Add the application-required `$project`, `$size`, and `$ifNull` slice.
  - [x] Complete the initial projection scope with full `$project` modes,
    `$set`, `$addFields`, `$unset`, `$literal`, and `$$REMOVE`.
- [x] [#91: Grouping and accumulators](https://github.com/schapman1974/tinymongo/issues/91)
  - [x] Support field-path and `None` group keys with `$min`, `$max`, and
    `$sum`.
  - [x] Complete the remaining accumulator scope with `$avg`, `$first`,
    `$last`, `$push`, and `$addToSet`.

Exit criteria:

- `Collection.aggregate()` returns a compatible cursor.
- Common filtering, ordering, projection, computed-field, and grouping
  pipelines pass real MongoDB contracts.

## 3. Advanced Mongo-style operations

[View milestone](https://github.com/schapman1974/tinymongo/milestone/3)

- [ ] [#78: Ordered bulk writes](https://github.com/schapman1974/tinymongo/issues/78)
- [ ] [#80: Advanced array update modifiers](https://github.com/schapman1974/tinymongo/issues/80)
- [ ] [#95: Aggregation lookup](https://github.com/schapman1974/tinymongo/issues/95)
- [ ] [#98: Aggregation unwind and array expressions](https://github.com/schapman1974/tinymongo/issues/98)
- [ ] [#99: Unordered bulk writes and detailed errors](https://github.com/schapman1974/tinymongo/issues/99)
- [ ] [#100: Positional and filtered array updates](https://github.com/schapman1974/tinymongo/issues/100)
- [ ] [#101: Replacement, upsert, sorting, and missing-field edge cases](https://github.com/schapman1974/tinymongo/issues/101)

Exit criteria:

- Common operators cover practical application workloads.
- Ordered and unordered bulk writes report compatible results and failures.
- Advanced arrays and aggregation pass differential contracts.

## 4. BSON and integration hardening

[View milestone](https://github.com/schapman1974/tinymongo/milestone/4)

### Python 3.14 runtime coverage

- [x] Add required Python 3.14 unit, 100%-coverage, live MongoDB, remote SQL,
  and built-package CI lanes.
- [x] Validate the universal wheel on Linux x64, Windows x64, macOS Intel, and
  macOS Apple silicon, including core-only and optional local-backend profiles.
- [x] Add blocking focused platform checks plus weekly/manual full Windows and
  macOS suites with cross-process SQLite stress.
- [x] Add beta-level CPython 3.14t coverage with the GIL disabled for core,
  concurrency, BSON/PyMongo, live MongoDB, pure-Psycopg PostgreSQL,
  MariaDB/MySQL, and package installation.
- [ ] Add DuckDB and Parquet to the 3.14t lane when DuckDB publishes a
  compatible free-threaded distribution; switch PostgreSQL back to the binary
  Psycopg profile there when `psycopg-binary` publishes one.

- [ ] [#79: Backend benchmarks and compatibility reports](https://github.com/schapman1974/tinymongo/issues/79)
- [ ] [#55: ODM integration](https://github.com/schapman1974/tinymongo/issues/55)
  - [x] Run the [#162 Beanie 2.1 compatibility
    spike](https://github.com/schapman1974/tinymongo/issues/162) through the real
    async ODM and close its three driver-contract blockers: `buildInfo`,
    collection-listing hints, and reply-document-shaped `raw_result` values.
  - [x] Retain MongoEngine's and Beanie's native ObjectId primary-key behavior;
    document `generate_id()` as an explicit string-ID choice rather than a
    required workaround.
  - [x] Implement integrity-preserving ascending compound, sparse, and partial
    unique indexes across durable catalogs and supported backends. Preserve
    ordered keys and membership options across restarts, use native constraints
    where available, and keep remote multikey values and other unsupported
    unique combinations fail-closed.
- [ ] [#81: MongoDB document and key validation](https://github.com/schapman1974/tinymongo/issues/81)
  - [ ] Encode and catalog local database and Parquet collection filenames so
    logical names stay beneath the storage root and remain portable and
    case-distinct on Windows and macOS filesystems.
  - [ ] Distinguish Windows drive-relative paths such as `C:folder` from
    PyMongo-style network targets.
- [ ] [#83: Remaining common BSON types](https://github.com/schapman1974/tinymongo/issues/83)
- [ ] [#93: Backend concurrency and compatibility stress tests](https://github.com/schapman1974/tinymongo/issues/93)
  - [ ] Canonicalize process-local lock identities and replace the private
    `RLock._is_owned()` dependency with owned lock state.
  - [ ] Verify failed lock acquisition and exceptional SQLite/DuckDB operations
    release locks and native file handles before replacement or cleanup.
- [x] [#94: BSON comparison and sort order](https://github.com/schapman1974/tinymongo/issues/94)

Exit criteria:

- Supported BSON values round-trip and compare consistently.
- Validation, indexes, queries, updates, sorting, and aggregation share BSON
  rules.
- Supported PyMongo versions and ODM behavior are published.
- Releases include compatibility, limitation, stress, and benchmark reports.

## 5. GridFS compatibility

[View milestone](https://github.com/schapman1974/tinymongo/milestone/9)

This release follows BSON/index hardening and precedes wire-client
compatibility.

- [ ] [#126: GridFS bucket model and API](https://github.com/schapman1974/tinymongo/issues/126)
- [ ] [#127: Upload streams and helpers](https://github.com/schapman1974/tinymongo/issues/127)
- [ ] [#128: Download streams and helpers](https://github.com/schapman1974/tinymongo/issues/128)
- [ ] [#129: File discovery and management](https://github.com/schapman1974/tinymongo/issues/129)
- [ ] [#130: Indexes, integrity checks, and cleanup](https://github.com/schapman1974/tinymongo/issues/130)
- [ ] [#131: Classic PyMongo GridFS API](https://github.com/schapman1974/tinymongo/issues/131)
- [ ] [#132: Cross-backend and wire-client contracts](https://github.com/schapman1974/tinymongo/issues/132)

Exit criteria:

- `GridFSBucket` and classic `GridFS` APIs support compatible file operations.
- Standard files and chunks collections use required indexes and integrity
  checks.
- Contracts compare streams, metadata, chunks, and errors with real MongoDB.
- Backend and future wire-client compatibility is published.

## 6. MongoDB wire server foundation

[View milestone](https://github.com/schapman1974/tinymongo/milestone/6)

- [ ] [#110: Licensing and distribution review](https://github.com/schapman1974/tinymongo/issues/110)
- [ ] [#111: Optional wire-server package boundary](https://github.com/schapman1974/tinymongo/issues/111)
- [ ] [#112: OP_MSG framing and BSON transport](https://github.com/schapman1974/tinymongo/issues/112)
- [ ] [#113: Command dispatcher and compatible errors](https://github.com/schapman1974/tinymongo/issues/113)
- [ ] [#114: Client handshake commands](https://github.com/schapman1974/tinymongo/issues/114)
- [ ] [#115: `tinymongo serve`](https://github.com/schapman1974/tinymongo/issues/115)

Exit criteria:

- Distribution constraints are documented before release.
- `tinymongo[wire]` remains isolated from normal embedded use.
- Modern clients complete transport and handshake negotiation.
- Server mode starts on loopback with conservative advertised capabilities.

## 7. Read-only Compass compatibility

[View milestone](https://github.com/schapman1974/tinymongo/milestone/7)

- [ ] [#116: Database and collection discovery](https://github.com/schapman1974/tinymongo/issues/116)
- [ ] [#117: Find cursors over the wire](https://github.com/schapman1974/tinymongo/issues/117)
- [ ] [#118: Read metadata and utility commands](https://github.com/schapman1974/tinymongo/issues/118)
- [ ] [#119: Read-only Compass tests](https://github.com/schapman1974/tinymongo/issues/119)

Exit criteria:

- Supported Compass versions browse databases, collections, documents, and
  indexes.
- Filtering, sorting, projection, pagination, count, and distinct work through
  bounded cursors.
- Unsupported features fail clearly without dropping the connection.

## 8. Compass editing and driver compatibility

[View milestone](https://github.com/schapman1974/tinymongo/milestone/8)

- [ ] [#120: Insert commands over the wire](https://github.com/schapman1974/tinymongo/issues/120)
- [ ] [#121: Update and delete commands over the wire](https://github.com/schapman1974/tinymongo/issues/121)
- [ ] [#122: `findAndModify` over the wire](https://github.com/schapman1974/tinymongo/issues/122)
- [ ] [#123: Aggregation over the wire](https://github.com/schapman1974/tinymongo/issues/123)
- [ ] [#124: Optional authentication and TLS](https://github.com/schapman1974/tinymongo/issues/124)
- [ ] [#125: Client and command compatibility matrix](https://github.com/schapman1974/tinymongo/issues/125)

Exit criteria:

- Compass performs supported inserts, updates, replacements, upserts, and
  deletes.
- The aggregation builder runs supported pipelines.
- Network exposure is protected or explicitly constrained to trusted local use.
- Supported Compass, PyMongo, mongosh, ODM, and driver versions are published.

## 9. WASM and browser SQLite

[View milestone](https://github.com/schapman1974/tinymongo/milestone/5)

This is a parallel local-first track after core backend contracts stabilize.

- [ ] [#104: Package for micropip and Pyodide](https://github.com/schapman1974/tinymongo/issues/104)
- [ ] [#105: Browser-safe locking and filesystem behavior](https://github.com/schapman1974/tinymongo/issues/105)
- [ ] [#106: SQLite under Pyodide](https://github.com/schapman1974/tinymongo/issues/106)
- [ ] [#107: IndexedDB persistence](https://github.com/schapman1974/tinymongo/issues/107)
- [ ] [#108: Async browser open and flush lifecycle](https://github.com/schapman1974/tinymongo/issues/108)
- [ ] [#109: Pyodide browser CI and example](https://github.com/schapman1974/tinymongo/issues/109)

Exit criteria:

- `micropip` installs `tinymongo[wasm,sqlite]` from a pure-Python wheel.
- SQLite runs under Pyodide without native locking assumptions.
- IndexedDB persistence survives browser reloads.
- Async open and flush provide reliable persistence around synchronous CRUD.
- Browser CI verifies installation, contracts, and reload persistence.

## Execution order

1. [x] Finish patching, projection, durable indexes, and practical batched
   `IndexModel` behavior.
2. [x] Complete the common synchronous API and build the asynchronous facade
   over the same shared semantics.
3. [x] Add the ObjectId, datetime, query, and PyMongo exception behavior
   required by the first Talk Python contracts.
4. [ ] Run Talk Python against TinyMongo's async client, turn every failure into a
   contract, and publish the baseline compatibility score.
   - [x] Exercise the Talk-Python-derived contract slice through sync and async.
   - [x] Add deterministic reporting and an external application test runner.
   - [x] Complete the real application run with Mike and record every difference.
   - [ ] Rerun the three follow-up cases and publish the dimensioned baseline.
5. [x] Build aggregation core after the real-application acceptance path works.
   - [x] Deliver the shared `$match` plus `$group`/`$min`/`$max`/`$sum` slice
     across synchronous and asynchronous APIs.
   - [x] Add the measured `$project`/`$size`/`$ifNull` slice.
   - [x] Complete the basic `$sort`, `$skip`, `$limit`, and `$count` stages.
   - [x] Complete the second-application acceptance rerun with Mike: all 9 of 9
     real application aggregation call sites pass.
6. [ ] Add advanced array, bulk, and remaining BSON operations according to measured
   compatibility gaps.
   - [x] Complete the measured #77 update slice with `$rename`, `$min`, `$max`,
     and `$pop` across synchronous and asynchronous clients.
   - [x] Complete Mike's TM-031 through TM-035 `$pull`, `$pullAll`, error-code,
     and remaining measured BSON compatibility findings.
   - [x] Complete Mike's TM-036 through TM-038 range, zero-timestamp, and
     remaining `$pull` predicate follow-ups.
7. [ ] Implement GridFS on stable BSON, index, and cursor foundations.
8. [ ] Build the wire-server foundation, followed by read-only Compass browsing and
   then editing support.
9. [ ] Deliver WASM/browser support as a parallel local-first track.

## Scope boundary

This roadmap does not promise replication, sharding, sessions, transactions,
or change streams. These remain explicitly unsupported unless they are
separately designed and approved.
