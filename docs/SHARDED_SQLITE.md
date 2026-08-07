# Experimental sharded SQLite backend

`sqlite-sharded` is an opt-in backend for applications that want more embedded
write concurrency than one SQLite database file can provide:

```python
from tinymongo import TinyMongoClient

client = TinyMongoClient(
    "./data",
    backend="sqlite-sharded",
    sqlite_shards=4,
)
events = client.app.events
events.insert_one({"_id": "event-1", "kind": "created"})
```

The backend is experimental and does not replace the stable `sqlite` backend.
If `sqlite_shards` is omitted, a new database uses four shards. The count is
written to the database manifest and cannot be changed when reopening that
database. Values from 2 through 64 are accepted.

## Storage and concurrency model

Each logical database is a directory:

```text
app.sqlite-sharded/
├── manifest.sqlite
└── shards/
    ├── 000/data.sqlite
    ├── 001/data.sqlite
    ├── 002/data.sqlite
    └── 003/data.sqlite
```

TinyMongo hashes the canonical BSON identity of the immutable `_id` and routes
the document to one stable shard. Exact `_id` reads, updates, replacements,
and deletes open only that shard. Other filters scatter across the shards and
merge their results before public sorting, skipping, or limiting is applied.
Indexes are created in every shard, and the normal BSON matcher remains the
final result authority.

Strict, unsorted `find_one({"_id": ...})` calls use a dedicated point-read
path. TinyMongo canonicalizes and hashes the ID once, queries the current
primary-key representation with `LIMIT 1`, and checks legacy representations
only after a miss. Each shard keeps a small process-local pool of autocommit,
query-only connections; a connection is leased to only one active call and is
retired on close, error, PID change, or a shard-file replacement observed
before its next lease.

Unfiltered natural-order scans of up to ten shards use SQLite itself for the
fan-in: one pooled, query-only manifest connection attaches every shard in
read-only mode and executes a single `UNION ALL` with global order. This avoids
the former Python-side re-query, merge, and second payload decode. Attached
connections are never shared by active calls and are retired on close, error,
PID change, or shard-file replacement. Filtered scans retain the established
per-shard path so TinyMongo's matcher and index-planning semantics stay intact;
databases above SQLite's default ten attached-database limit use that path too.

The manifest's complete collection/index catalog is cached against SQLite's
`data_version`. Point reads verify that lightweight generation before and after
the shard query, retrying if another client changed metadata. This removes the
two catalog joins from steady-state point reads without hiding a concurrent
collection drop or recreation.

SQLite still permits only one writer in each database file. Four files can
therefore have as many as four active writers when their IDs route to different
shards. Writes routed to the same file queue normally. WAL lets readers use the
last committed snapshot while that shard has an active writer.

No TinyMongo worker, daemon, queue, or background thread is required. SQLite
manages its own WAL and automatic checkpoints during ordinary application
calls.

## WAL requirements

WAL is part of standard SQLite and does not require SQLite's experimental
`BEGIN CONCURRENT` branch or a custom Python build. At startup TinyMongo enables
WAL on the manifest and every shard, verifies that SQLite returned `wal`, and
fails clearly if the runtime or filesystem cannot provide it. The backend uses
`synchronous=FULL` for acknowledged-write durability.

Use a local writable filesystem. SQLite WAL depends on shared-memory sidecar
files and is not supported here on NFS, SMB, object storage, in-memory, or
temporary database targets. The directory must allow SQLite to create `-wal`
and `-shm` files.

SQLite documents a rare WAL-reset race in releases through 3.51.2, fixed in
3.51.3 with backports in 3.50.7 and 3.44.6. Use a fixed SQLite runtime for
production experiments and check the version linked to Python with
`sqlite3.sqlite_version`. TinyMongo serializes its own writes and automatic
checkpoint boundary per shard, but a separate program writing the shard files
directly bypasses that coordination and is unsupported. See SQLite's
[WAL-reset guidance](https://sqlite.org/wal.html#the_wal_reset_bug).

## Experimental boundaries

- Secondary unique indexes are checked across every shard. Writes to a
  collection with such an index use a database-level coordination lock, so
  those particular writes do not gain the full sharded concurrency benefit.
  The current experimental implementation also decodes the full logical
  collection when a unique value may change, making these point writes O(N).
- A multi-document operation can span several SQLite files. TinyMongo reserves
  and validates the affected shards before writing, but SQLite cannot make the
  final commits to separate files power-loss atomic. A process or machine crash
  between shard commits can leave a partially applied multi-shard batch.
- Scatter reads do not provide an atomic snapshot with concurrent writes across
  all files. Each shard contributes committed SQLite state; attached scans do
  not turn separate shard writes into one cross-file transaction.
- Unsorted natural order is maintained with hidden storage metadata, but code
  should use an explicit sort whenever ordering is part of application logic.
- The shard count is fixed. Resharding requires migration into a newly created
  database.
- Database removal is not a concurrent data operation. Coordinate every client
  before calling `drop_database()`; do not write from one process while another
  process removes the database directory. `close()` only releases that handle's
  resources, which are opened lazily again if the handle is reused.
- Do not reuse a client inherited through raw `os.fork()`. TinyMongo retires
  pooled SQLite handles before a registered fork and fails closed if the child
  tries to use the inherited backend. Start workers with `spawn`, or create the
  client after an `exec`, so every process owns its SQLite runtime and handles.

Close all clients before copying a database for backup, then copy the complete
`.sqlite-sharded` directory. Do not copy selected shard files or omit live WAL
state. Dedicated online backup and resharding tools remain future hardening
work.
