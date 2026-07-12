# TinyMongo roadmap

TinyMongo aims to provide strong, practical MongoDB and PyMongo compatibility
while remaining an embedded, multi-backend database.

The compatibility target is to pass at least 90% of a published,
application-focused contract suite against real MongoDB. Unsupported server
features should fail clearly instead of behaving approximately.

The live checklist is [GitHub issue #103](https://github.com/schapman1974/tinymongo/issues/103),
and progress by release is available on the
[milestones page](https://github.com/schapman1974/tinymongo/milestones).

## 1. Compatibility foundation

[View milestone](https://github.com/schapman1974/tinymongo/milestone/1)

Prerequisite backend work:

- [ ] [#69: In-memory storage backend](https://github.com/schapman1974/tinymongo/issues/69)
- [ ] [#70: PyMongo patching helper](https://github.com/schapman1974/tinymongo/issues/70)
- [ ] [#74: Field projections](https://github.com/schapman1974/tinymongo/issues/74)
- [ ] [#76: Durable indexes and unique constraints](https://github.com/schapman1974/tinymongo/issues/76)

Compatibility work:

- [ ] [#72: Real MongoDB contract suite](https://github.com/schapman1974/tinymongo/issues/72)
- [ ] [#73: Common client, collection, and cursor API](https://github.com/schapman1974/tinymongo/issues/73)
- [ ] [#87: Differential compatibility fuzzing](https://github.com/schapman1974/tinymongo/issues/87)

Exit criteria:

- Shared contracts run against real MongoDB and every applicable backend.
- Generated cases can reproduce behavioral differences.
- Memory, projection, patching, durable indexes, and common APIs are available.

## 2. Aggregation core

[View milestone](https://github.com/schapman1974/tinymongo/milestone/2)

- [ ] [#82: Shared aggregation pipeline engine](https://github.com/schapman1974/tinymongo/issues/82)
- [ ] [#96: Basic aggregation stages](https://github.com/schapman1974/tinymongo/issues/96)
- [ ] [#97: Aggregation projection stages](https://github.com/schapman1974/tinymongo/issues/97)
- [ ] [#91: Grouping and accumulators](https://github.com/schapman1974/tinymongo/issues/91)

Exit criteria:

- `Collection.aggregate()` returns a compatible cursor.
- Common filtering, ordering, projection, computed-field, and grouping
  pipelines pass real MongoDB contracts.

## 3. Advanced Mongo-style operations

[View milestone](https://github.com/schapman1974/tinymongo/milestone/3)

- [ ] [#77: Additional query and update operators](https://github.com/schapman1974/tinymongo/issues/77)
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

- [ ] [#75: Optional BSON serialization](https://github.com/schapman1974/tinymongo/issues/75)
- [ ] [#79: Backend benchmarks and compatibility reports](https://github.com/schapman1974/tinymongo/issues/79)
- [ ] [#55: ODM integration](https://github.com/schapman1974/tinymongo/issues/55)
- [ ] [#81: MongoDB document and key validation](https://github.com/schapman1974/tinymongo/issues/81)
- [ ] [#83: Remaining common BSON types](https://github.com/schapman1974/tinymongo/issues/83)
- [ ] [#93: Backend concurrency and compatibility stress tests](https://github.com/schapman1974/tinymongo/issues/93)
- [ ] [#94: BSON comparison and sort order](https://github.com/schapman1974/tinymongo/issues/94)
- [ ] [#102: Optional PyMongo-version adaptation](https://github.com/schapman1974/tinymongo/issues/102)

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

1. Complete compatibility foundations and generate a baseline score.
2. Build aggregation core.
3. Add advanced query, update, array, and bulk operations.
4. Finish BSON semantics, durable indexes, and integration hardening.
5. Implement GridFS on stable BSON, index, and cursor foundations.
6. Publish release compatibility evidence against the 90% target.
7. Build the wire-server foundation.
8. Deliver read-only Compass browsing.
9. Add Compass editing, GridFS wire clients, and broader driver support.
10. Deliver WASM/browser support as a parallel local-first track.

## Scope boundary

This roadmap does not promise replication, sharding, sessions, transactions,
or change streams. These remain explicitly unsupported unless they are
separately designed and approved.
