[![TinyMongo logo](https://raw.githubusercontent.com/schapman1974/tinymongo/master/artwork/tinymongo.png)](https://tinymongo.org/)

**Website:** [tinymongo.org](https://tinymongo.org/)

[![CI](https://github.com/schapman1974/tinymongo/actions/workflows/ci.yml/badge.svg)](https://github.com/schapman1974/tinymongo/actions/workflows/ci.yml)

# Purpose

TinyMongo provides a familiar PyMongo-style document API backed by embedded
storage instead of a MongoDB server. The default backend uses
[TinyDB](https://tinydb.readthedocs.io/), with optional memory, SQLite, DuckDB,
Parquet, PostgreSQL, and MariaDB/MySQL backends.

# Status

TinyMongo supports Python 3.9 and newer. GitHub Actions tests every standard
CPython release from 3.9 through 3.14 on Linux, then installs the built wheel on
Windows x64, macOS Intel, and macOS Apple silicon. CPython 3.14's free-threaded
build is supported at the beta level for the backends described below.

# Installation

The latest stable release is 1.3.0 and can be installed from PyPI:

```bash
pip install "tinymongo==1.3.0"
```

For development, clone this repository and run `pip install -e .` from its
root. Use the `v1.3.0` tag when you need source and documentation that match
the current stable package exactly; `master` may contain later changes. See
[`CHANGELOG.md`](https://github.com/schapman1974/tinymongo/blob/master/CHANGELOG.md)
for the complete release history and any unreleased work.

The default JSON backend has a small dependency set. Optional database backends
may install native binary wheels supplied by DuckDB, PyArrow, or SQL drivers.

# Project notes

- **Roadmap:** See the [TinyMongo roadmap](https://github.com/schapman1974/tinymongo/blob/master/ROADMAP.md) for planned compatibility, GridFS, Compass, wire-server, and browser work.
- **Default storage:** TinyMongo uses TinyDB-compatible JSON storage unless another backend is selected.
- **Table-native backends:** SQLite, DuckDB, and Parquet backends store one real table/file per collection instead of one serialized database blob.
- **Concurrency:** local JSON writes use atomic replace, `fsync`, and advisory
  locks. Table-native and remote backends use their own transactional or
  file-replacement mechanisms; see the backend-specific documentation.
- **Async API:** async clients keep storage and lock waits off the event loop
  while sharing the synchronous implementation's behavior.
- **Tests & CI:** `.github/workflows/ci.yml` runs unit tests, linters, live-service
  contracts, packaging checks, and portable wheel smoke tests. Current Windows,
  macOS, and Linux lanes exercise TinyDB, SQLite, DuckDB, and Parquet; minimum
  Python lanes verify the core TinyDB and SQLite installation. See
  `requirements-dev.txt` for development dependencies.


# PyMongo-style import

TinyMongo exposes `MongoClient`, `ASCENDING`, and `DESCENDING` aliases so small
PyMongo-style scripts can be tried against local file-backed storage by changing
the import:

```python
import tinymongo as pymongo

client = pymongo.MongoClient(
    "mongodb://localhost:27017",
    serverSelectionTimeoutMS=2000,
    tinymongo_folder="/path/to/folder",
)
users = client.app.users
users.insert_one({"email": "ada@example.com", "score": 7})
users.update_one({"email": "ada@example.com"}, {"$inc": {"score": 1}})
rows = list(users.find({}).sort("score", pymongo.DESCENDING))
```

This is intended for the supported TinyMongo subset of PyMongo operations, not
for server features such as authentication, replica sets, sessions, or network
connections. MongoDB URIs, host names, ports, and recognized network connection
kwargs are accepted and ignored so existing code can be tried locally. When
PyMongo is installed, TinyMongo derives those accepted option names from that
installed version; dependency-free installs use a bundled fallback list.
Unknown or misspelled kwargs fail eagerly with `ConfigurationError`, matching
PyMongo's configuration behavior instead of silently selecting the wrong local
storage. Set `TINYMONGO_HOME` or pass `tinymongo_folder=` to choose where
TinyMongo stores files. See `examples/pymongo_dropin.py` for a runnable example.

The behavior-bearing read options are not ignored. `document_class` constructs
top-level and nested result documents—including documents inside arrays—with a
mutable mapping class such as `OrderedDict` or `bson.SON`. Datetimes are stored
as signed UTC milliseconds, just like BSON. Results are naive UTC by default;
set `tz_aware=True` for aware UTC values, and optionally pass `tzinfo=` to
convert the same instant to another timezone. These rules apply equally to the
synchronous and asynchronous PyMongo-shaped clients. Raw BSON views via
`RawBSONDocument` are not part of the embedded-storage document-class subset.

## Async API

`AsyncMongoClient` and `AsyncTinyMongoClient` expose non-blocking client,
database, collection, and cursor APIs. `find()` is immediate and lazy; storage
work begins when the cursor is awaited or iterated:

```python
from tinymongo import AsyncMongoClient


async def load_users():
    async with AsyncMongoClient(
        tinymongo_folder="./tinydb",
        backend="sqlite",
    ) as client:
        users = client.app.users
        await users.insert_one({"_id": 1, "name": "Ada", "score": 9})
        return await users.find({}).sort("score", -1).to_list(length=None)
```

Async cursors support `async for`, `to_list()`, `sort()`, `skip()`,
`limit()`, `clone()`, `rewind()`, and `close()`. Potentially blocking
storage, serialization, locking, query, and cleanup work runs outside the event
loop.

## Patch PyMongo during tests

`tinymongo.patch()` temporarily routes `pymongo.MongoClient` and
`pymongo.AsyncMongoClient` calls to TinyMongo
without making PyMongo a required TinyMongo dependency. Install PyMongo only for
tests that use the helper:

```bash
pip install "tinymongo[pymongo]"
```

The default backend is an isolated in-memory database. Clients created inside
one patch scope share data, and the original PyMongo client is restored even if
the test raises an exception:

```python
import pymongo
import tinymongo

with tinymongo.patch():
    writer = pymongo.MongoClient("mongodb://ignored")
    reader = pymongo.MongoClient()
    writer.app.users.insert_one({"name": "Ada"})
    assert reader.app.users.count_documents({}) == 1
```

The same helper can decorate a test and can select a folder and backend:

```python
import pymongo
import tinymongo


@tinymongo.patch(folder="./test-data", backend="sqlite")
def test_application_code():
    client = pymongo.MongoClient()
    assert client.server_info()["storage"] == "sqlite"
```

It can also decorate a `unittest` method:

```python
import unittest

import pymongo
import tinymongo


class ApplicationTest(unittest.TestCase):
    @tinymongo.patch()
    def test_application_code(self):
        client = pymongo.MongoClient()
        client.app.users.insert_one({"name": "Ada"})
        self.assertEqual(client.app.users.count_documents({}), 1)
```

Patch scopes may be nested. Code must look up `pymongo.MongoClient` while the
scope is active; a `MongoClient` name imported directly before the patch cannot
be replaced. Because PyMongo's module attribute is process-global, patch scopes
cannot overlap across threads. Async tests should put a `with tinymongo.patch()`
block inside the async function instead of decorating the coroutine. Prefer
`async with tinymongo.patch()` when creating async clients so cleanup can be
awaited.


# Backend options

TinyMongo defaults to TinyDB's JSON storage:

```python
    from tinymongo import TinyMongoClient

    connection = TinyMongoClient("/path/to/folder")
```

You can select another backend with the `backend` argument:

```python
    memory_connection = TinyMongoClient(backend="memory")
    parquet_connection = TinyMongoClient("/path/to/folder", backend="parquet")
    sqlite_connection = TinyMongoClient("/path/to/folder", backend="sqlite")
    duckdb_connection = TinyMongoClient("/path/to/folder", backend="duckdb")
    postgres_connection = TinyMongoClient(
        backend="postgres",
        dsn="postgresql://user:password@localhost:5432/tinymongo",
    )
```

`TinyMongoClient` and `AsyncTinyMongoClient` also accept
`tinymongo_folder=` as an alias for `foldername`. If a non-default
`foldername` is also supplied, the two values must agree. Unknown constructor
keywords raise `TypeError` instead of being silently ignored. The other
supported backend configuration keywords are `threads`, `storage_uri`,
`sqlite_shards`, `duckdb_config`, and `dsn`. `sqlite_shards` applies only to
new `sqlite-sharded` databases; the persisted count must match when reopening.

The PyMongo-shaped `MongoClient` and `AsyncMongoClient` accept the installed
PyMongo version's recognized connection kwargs for drop-in use and ignore their
network effects. Unknown or misspelled kwargs raise `ConfigurationError` before
storage opens. `document_class`, `tz_aware`, and `tzinfo` are validated before
storage opens and control returned values recursively.

Parquet can also store collection files in object storage by passing
`storage_uri` or setting `TINYMONGO_STORAGE_URI`. Object-storage Parquet is
experimental and currently uses one Parquet file per collection, so
updates/deletes rewrite that file:

```python
    s3_connection = TinyMongoClient(
        "/unused-local-path",
        backend="parquet",
        storage_uri="s3://my-bucket/tinymongo",
    )
```

When `storage_uri` is set, it fully determines the Parquet data location. The
API folder argument is optional and ignored; the CLI path argument remains a
required placeholder. Neither creates a local cache or fallback directory.
Remote SQL treats its path argument the same way. Environment-only
`TINYMONGO_STORAGE_URI` and remote DSN settings also work for CLI database
discovery and migration summaries.

Available backends:

- `memory`: Process-local storage that creates no database or lock files. Each unnamed client is isolated; a `memory://NAME` URI explicitly shares a named namespace within one process.
- `tinydb` or `json`: TinyDB-compatible JSON storage. This is the default and writes `.json` files.
- `sqlite`: Table-native SQLite storage using one SQL table per collection. This writes `.sqlite` files.
- `sqlite-sharded`: Experimental table-native SQLite storage that stripes one logical database across multiple WAL-enabled SQLite files for concurrent writers.
- `duckdb`: Table-native DuckDB storage using one DuckDB table per collection. This writes `.duckdb` files.
- `parquet` or `parquetv2`: DuckDB-managed Parquet dataset storage using one Parquet file per collection inside a `.parquet` directory.
- `postgres` or `postgresql`: Remote PostgreSQL storage using one SQL table per database collection.
- `mysql` or `mariadb`: Remote MariaDB/MySQL storage using one SQL table per database collection.

Install only the drivers you need:

```bash
pip install "tinymongo[duckdb]"
pip install "tinymongo[parquet]"
pip install "tinymongo[postgres]"
pip install "tinymongo[mysql]"
pip install "tinymongo[bson]"
pip install "tinymongo[pymongo]"
pip install "tinymongo[serialization]"
```

If an optional driver is missing, selecting that backend raises an `ImportError`
with the corresponding installation command. PyMongo is not a core runtime
dependency. It is selected at runtime for `ObjectId`, `Decimal128`, non-generic
`Binary` subtypes, patching, and conditional PyMongo exception inheritance,
and is also used by development compatibility tests. Install
`tinymongo[bson]` for BSON values or `tinymongo[pymongo]` for patching and
installed-version compatibility.

## Free-threaded Python 3.14

For CPython 3.14t, install the dependency profile that has been verified while
the GIL is disabled:

```bash
python3.14t -m pip install "tinymongo[free-threaded]"
```

This profile covers the core API, memory, TinyDB/JSON, SQLite, BSON/PyMongo,
MySQL/MariaDB, and PostgreSQL through pure Psycopg. Pure Psycopg requires the
system `libpq` library. DuckDB and Parquet are not yet available in this
profile because DuckDB does not publish a `cp314t` wheel; `psycopg-binary`
likewise has no `cp314t` distribution. The required 3.14t CI lanes verify that
the GIL remains disabled and exercise in-process concurrency, MongoDB
contracts, remote SQL, and built-package installation. Contributors using
3.14t should install `requirements-free-threaded.txt` instead of
`requirements-dev.txt`.

Free-threaded support does not make every object safe for simultaneous
mutation. Shared client, database, and collection handles support independent
operations, but applications should coordinate lifecycle calls such as
`close()` and `drop_database()` and should not consume one cursor or iterator
from multiple threads.

| Backend | Dependency | Best fit | Notes |
| --- | --- | --- | --- |
| `memory` | None | Isolated tests and temporary data | Creates no files. Named `memory://NAME` namespaces can be shared only within one process. |
| `tinydb` / `json` | TinyDB | Default local JSON files | Human-readable and simplest to inspect. |
| `sqlite` | Python standard library | Embedded transactional storage | Uses `_id` primary keys and JSON document payloads in collection tables. |
| `sqlite-sharded` | Python standard library | Experimental concurrent embedded writes | Routes stable `_id` values across independent WAL-enabled SQLite files; no daemon or custom SQLite build is required. |
| `duckdb` | `duckdb` | SQL-backed local analytics workflows | Uses real DuckDB collection tables and SQL JSON predicates where supported. |
| `parquet` / `parquetv2` | `duckdb`, `pyarrow` | Columnar local or object-storage workflows | Stores collection Parquet files that DuckDB reads and writes. |
| `postgres` / `postgresql` | `tinymongo[postgres]` | Remote transactional storage | Stores documents in PostgreSQL tables with JSONB payloads. |
| `mysql` / `mariadb` | `tinymongo[mysql]` or `tinymongo[mariadb]` | Remote transactional storage | Stores documents in MariaDB/MySQL tables with JSON payloads. |

## In-memory testing and temporary data

For test isolation or scratch data, select the memory backend without a named
address. Every client receives a separate in-memory database and creates no
files:

```python
from tinymongo import TinyMongoClient

client = TinyMongoClient(backend="memory")
client.app.users.insert_one({"name": "Ada"})
assert client.app.users.count_documents({}) == 1
client.close()
```

Use a named URI only when clients in the same process need to share data. A
named namespace remains available after a client closes and can be reopened
until the process exits:

```python
writer = TinyMongoClient("memory://shared-test", backend="memory")
writer.app.users.insert_one({"name": "Grace"})
writer.close()

reader = TinyMongoClient("memory://shared-test", backend="memory")
assert reader.app.users.find_one({"name": "Grace"}) is not None
```

Memory data never persists across process restarts, and named namespaces are not
safe for sharing between processes. Prefer unnamed clients, or unique names, in
independent tests. Use a durable backend instead when data must survive a test
run or application restart. The command-line tool intentionally omits the
memory backend because every CLI invocation exits immediately; use it through
the Python API instead.

SQLite, DuckDB, and Parquet compile supported Mongo-style filters into SQL over
the `_id` column and JSON document payload. Unsupported filter shapes fall back
to Python document matching so existing TinyMongo behavior remains available.
SQLite also uses its primary key and declared non-unique indexes for top-level
bool/int/float/string equality to restrict ordinary update candidates before
BSON decoding. Collections with user-created unique indexes retain complete
post-image validation.
Older blob-format SQLite and DuckDB files are migrated to collection tables when
opened.

Local load-test results for these backends are documented in
[backend benchmarks](https://github.com/schapman1974/tinymongo/blob/master/docs/BENCHMARKS.md).

Object-storage setup examples for S3, S3-compatible providers, Backblaze B2,
Cloudflare R2, Google Cloud Storage, Azure Blob Storage, MinIO, Wasabi, and
DigitalOcean Spaces are documented in
[the object-storage guide](https://github.com/schapman1974/tinymongo/blob/master/docs/OBJECT_STORAGE.md).
PostgreSQL and MariaDB/MySQL setup is documented in
[the remote SQL guide](https://github.com/schapman1974/tinymongo/blob/master/docs/REMOTE_SQL.md).
Remote SQL drivers are optional; if one is missing, TinyMongo raises an
`ImportError` with the exact `pip install ...` command to run.


# Command line tools

The package installs a `tinymongo` command for inspecting and moving data:

```bash
tinymongo inspect ./tinydb
tinymongo list-dbs ./tinydb
tinymongo list-collections ./tinydb my_tiny_database
tinymongo export ./tinydb my_tiny_database users -o users.json
tinymongo import ./tinydb my_tiny_database users users.json --mode replace
tinymongo migrate ./tinydb ./sqlite-db --to-backend sqlite
```

Use `--backend` with `inspect`, `list-dbs`, `list-collections`, `export`, and
`import` when reading or writing a non-default backend:

```bash
tinymongo inspect ./sqlite-db --backend sqlite
tinymongo export ./parquet-db app users --backend parquet -o users.json
tinymongo inspect ./unused --backend parquet --storage-uri s3://my-bucket/tinymongo
tinymongo migrate ./tinydb ./unused --to-backend parquet --target-uri s3://my-bucket/tinymongo
tinymongo migrate ./tinydb ./unused --to-backend postgres --target-dsn "$TINYMONGO_POSTGRES_DSN"
```

Export and import use the same tagged JSON codec as storage, so supported
datetime, ObjectId, bytes, and Binary values remain portable. Datetimes use
BSON's canonical naive-UTC millisecond representation. `bytearray` is accepted
and imports back as `bytes`. Inspection, collection listing, and migration omit
TinyDB's internal `_default` table. Export preserves embedded-document field
order, including document-valued `_id` values. Replace-mode imports and
migrations preflight the complete write with the destination's indexes before
deleting existing documents, and restore those documents if deletion or the
final insertion fails.


# Integration and stress testing

Unit tests exclude integration stress tests by default. Run the normal suite with:

```bash
pytest
```

Run local integration stress tests explicitly with:

```bash
pytest -m integration
```

The concurrent write stress tests are configurable with environment variables:

```bash
TINYMONGO_INTEGRATION_PROCS=32 \
TINYMONGO_INTEGRATION_WRITES_PER_PROC=100 \
pytest -m integration tests/integration/test_concurrent_writes.py
```

That default bulk-write run produces 3,200 concurrent writes. For a larger local
run:

```bash
TINYMONGO_INTEGRATION_PROCS=64 \
TINYMONGO_INTEGRATION_WRITES_PER_PROC=250 \
pytest -m integration tests/integration/test_concurrent_writes.py
```

The single-insert smoke test can be tuned separately:

```bash
TINYMONGO_INTEGRATION_SINGLE_PROCS=16 \
TINYMONGO_INTEGRATION_SINGLE_WRITES_PER_PROC=100 \
pytest -m integration tests/integration/test_concurrent_writes.py
```

Use `TINYMONGO_INTEGRATION_BACKEND=sqlite` or another supported backend to run
the same integration tests against a non-default backend.


# Mongo compatibility

TinyMongo intentionally implements a practical subset of PyMongo's collection
API. It supports common inserts, finds, updates, deletes, sorting, pagination,
distinct values, collection counting, `find_one_and_delete()`, database
listing/removal, index inspection, and batched index creation. Cursors support
single- and multi-key sorting, `skip()`, `limit()`, `clone()`, `close()`,
and `to_list()`; `limit(0)` means no limit.

Query support includes equality (including scalar matches against array
members), nested document paths, `$gt`, `$gte`, `$lt`, `$lte`, `$ne`,
`$nin`, `$in`, `$all`, `$and`, `$or`, `$nor`, `$not`, `$regex`,
case-insensitive `$options`, `$exists`, `$size`, `$elemMatch`, `$type`, and
`$mod`. `$comment` metadata is accepted and ignored at the top level and inside
document-form `$elemMatch` filters; using `$comment` as a field operator raises
`OperationFailure` with MongoDB error code `2`. `$size` matches an exact array
length, while `$elemMatch` requires one array member to satisfy the complete
nested condition. `$type` accepts MongoDB type names, numeric codes, or a list
of either; `$mod` follows MongoDB's integer-truncation and array-member matching
rules.

Every CRUD method that accepts a filter validates it before reading or changing
storage. Unsupported or misspelled `$`-prefixed query operators raise
`TinyMongoNotSupportedError` instead of silently returning no matches, and
malformed operands for recognized operators raise `OperationFailure`.
`$not` accepts a non-empty query document or a compiled native/BSON regex;
bare strings, numbers, lists, and empty documents raise `OperationFailure`
with MongoDB error code `2`.

MongoDB treats missing fields differently depending on the negated value.
`{"field": {"$ne": None}}` and `$nin` lists containing `None` exclude
documents where `field` is missing, while `$ne` and `$nin` with only non-null
values continue to include missing fields. The same rule applies to dotted
paths and array members across TinyMongo backends.

Update support includes `$set`, `$unset`, `$inc`, `$min`, `$max`, `$rename`,
`$push`, `$pop`, `$pull`, `$pullAll`, and `$addToSet`, including `upsert=True`.
As in PyMongo, `update_one()` and `update_many()` require update operators; use
`replace_one()` for full-document replacement. `$push` accepts `$each`,
`$position`, `$sort`, and `$slice`; `$addToSet` accepts `$each`; and `$pull`
accepts literals, document conditions, ranges, `$in`, `$nin`, `$regex` with
optional `$options`, `$elemMatch`, `$exists`, `$type`, `$ne`, `$mod`, `$all`,
`$size`, and document-field `$not`. MongoDB does not accept a top-level `$not`
as a `$pull` condition, so TinyMongo reports `WriteError` code `2` for that
shape as well; document-level `$expr` is likewise refused with MongoDB's code
`224`. `$pullAll` removes every array item BSON-equal to any literal in its
operand array.

`$pull` and `$pullAll` are true no-ops when their target field is missing: they
do not create an empty array, change an `$exists: false` result, or add to
`modified_count`.

`$min` and `$max` set a missing field or replace an existing value only when
the candidate is lower or higher in TinyMongo's MongoDB-compatible BSON order.
`$rename` moves an existing field without creating a value for a missing
source. `$pop` removes the first array item for `-1` or the last for `1`, and is
a no-op for a missing or empty array. Dotted paths are supported; the new
operators also follow numeric array positions where MongoDB permits them, while
`$rename` rejects array-element source and destination paths.

Update documents are validated before any change is stored. Non-document
operator operands, invalid or conflicting paths, traversal through a scalar,
non-array `$pop` targets, and attempts to change `_id` raise
PyMongo-compatible `WriteError` exceptions with the applicable MongoDB code,
and the complete update remains atomic. Applying `$push`, `$pull`, `$pullAll`,
or `$addToSet` to an existing null or non-array field raises `WriteError` code
`2`.

`find()` and `find_one()` accept Mongo-style inclusion and exclusion
projections. Dotted paths project nested fields, `_id` follows MongoDB's special
include/exclude rules, and projection happens after filtering and sorting:

```python
users.find({"active": True}, {"profile.email": 1, "_id": 0})
users.find_one({"email": "ada@example.com"}, {"password_hash": 0})
```

Inclusion and exclusion cannot be mixed except for `_id`. Computed expressions,
`$meta`, positional projection, and numeric array positions are rejected with a
clear error. The second positional argument to `find()` is now the PyMongo-style
projection argument; pass sorting as `sort=` or call `.sort()` on the cursor.
For unsorted SQLite scans, TinyMongo applies a normalized projection as each row
is read and releases the complete source document before reading the next row.
The common `{"_id": 1}` sweep goes further and reads only each JSON `_id` value,
so large unrequested payloads never enter Python memory. Filters still evaluate
against complete source values when Python-side matching is required.

SQLite `find()` cursors defer their scan until first consumption. For
unindexed filters handled by SQLite's existing SQL compiler, a final `skip()`
and `limit()` can therefore be applied before later payloads are read.
Python-only filters stream until that result window is filled; paths that must
merge scalar and array index candidates retain their complete-candidate
fallback. `count_documents()` uses a native SQL count for the same SQL-routed
filters and otherwise counts a row-at-a-time scan without retaining documents.
The same paths back synchronous and asynchronous collections.

This memory bound has explicit limits. Sorting must see unprojected sort keys,
so a sorted SQLite cursor falls back to complete source documents before it
projects the results. Other TinyMongo backends also continue to materialize
complete matched documents before projection. Finally, exclusion projections
that return most of each document naturally retain the size of those results.

Collections expose durable indexes and unique constraints:

```python
collection.create_index("email", unique=True, name="login_email")
collection.create_index(
    [("tenant_id", 1), ("email", 1)],
    unique=True,
    name="tenant_email",
)
collection.create_index("nickname", sparse=True)
collection.create_index(
    "email",
    unique=True,
    partialFilterExpression={"active": True},
)
collection.find({"email": "person@example.com"})
collection.list_indexes()
collection.drop_index("login_email")
```

Index definitions and unique constraints survive client restarts on persistent
backends. SQLite, PostgreSQL, and MariaDB/MySQL create native database indexes;
JSON, DuckDB, and Parquet persist metadata and enforce unique constraints through
TinyMongo. Named memory databases retain metadata while their process-local
namespace exists. Duplicate `_id` and unique-index violations raise
`DuplicateKeyError` with MongoDB-compatible code `11000`.

Plural `create_indexes()` accepts duck-typed PyMongo `IndexModel` batches
without importing PyMongo in TinyMongo's core. Ascending compound, sparse, and
partial indexes retain their complete definitions in durable catalogs and can
enforce uniqueness. Sparse indexes omit documents whose indexed fields are all
missing but include explicit `null`; partial indexes include only documents
matching their filter. Supported partial filters use literals, `$eq`,
`$exists: true`, `$gt`, `$gte`, `$in`, `$lt`, `$lte`, `$type`, `$and`, and
`$or`. Sparse and partial options cannot be combined on one index.

Performance-only declarations still degrade safely and emit
`TinyMongoUnsupportedWarning`: descending and hashed keys use ascending
equality indexes, `background` is ignored, text indexes are skipped because
`$text` queries are unsupported, and TTL indexes do not expire documents. A
unique hashed, text, or TTL declaration is rejected before any batch entry is
created because weakening it would compromise integrity.

Unique indexes support JSON scalar values, Decimal128, UUID/Binary, regex, and
flat arrays on embedded backends. Ordinary indexes treat missing and `null` as
one unique key, while sparse and partial membership follows the rules above.
Embedded compound unique indexes support one flat array field and reject
parallel arrays. Object values, ObjectId, datetime, nested arrays, non-finite
numbers, and array traversal inside a dotted index path are not supported for
unique indexes yet.
Remote SQL stores a versioned canonical token digest beside each unique-indexed
value and protects it with a native constraint. This preserves exact int/float
numeric identity across processes, including very large integers and doubles,
while keeping booleans distinct from numbers. Remote SQL still rejects all
array/multikey, Decimal128, UUID/Binary, and regex values under unique indexes
because those tokens cannot yet guarantee cross-process MongoDB multikey or
BSON identity.

SQL, DuckDB, and Parquet storage uses typed physical `_id` keys for new rows.
Existing databases with older stringified keys remain readable and mutable.
BSON-equivalent IDs share one key (`1` and `1.0`, or native bytes and Binary
subtype 0), while BSON-distinct values such as `True` and `1` or identical
binary data with different subtypes remain separate.

Cursor sorting follows MongoDB's recursive comparison order for the supported
BSON scalar, document, and array families. The supported relative order is
`MinKey`, null, numbers, strings, documents, arrays, BinData, `ObjectId`,
booleans, datetimes, `Timestamp`, regex, unscoped `Code`, scoped `Code`, and
`MaxKey`. The same order is used by aggregation `$min` and `$max` and by the
`$gt`, `$gte`, `$lt`, and `$lte` query operators, including range predicates
inside `$pull`. Range queries apply MongoDB's BSON type bracketing: numeric
representations share one family, while values from other type families do not
compare with one another. `MinKey` and `MaxKey` operands are the two exceptions:
they compare across every supported BSON type and can express an inclusive
whole-value range. Array fields expose both the whole array and its direct
members where MongoDB does, and documents and arrays are compared recursively.
Values with equal comparison keys retain their input order for ascending and
descending sorts. TinyMongo uses its shared Python matcher whenever a
backend-native or indexed predicate cannot guarantee these rules.
Extending unique-index identity to the remaining supported BSON values is
still tracked separately in the roadmap and continues to fail closed where
exact enforcement is unavailable. Update and aggregation `$min` and `$max`
share this BSON comparison order, including recursive document and array
comparisons and numeric equivalence across supported numeric representations.

## Aggregation core subset

`Collection.aggregate()` supports the production-driven core of `$match`,
`$sort`, `$skip`, `$limit`, `$count`, `$project`, `$set`, `$addFields`, `$unset`,
and `$group`. `$match` uses the same query operators as `find()`. `$project`
supports inclusion, exclusion, renamed or computed fields, nested
specifications, dotted output paths through arrays, and MongoDB's special
`_id` rules. The available projection expressions are `$ifNull`, `$literal`,
and `$size`; `$$REMOVE` conditionally omits or removes a field. `$group` accepts
a field-path or `None` `_id` and the `$addToSet`, `$avg`, `$first`, `$last`,
`$max`, `$min`, `$push`, and `$sum` accumulators:

```python
activity = events.aggregate(
    [
        {"$match": {"course_id": {"$in": course_ids}}},
        {
            "$project": {
                "course_id": 1,
                "count": {"$size": {"$ifNull": ["$lectures", []]}},
            }
        },
        {
            "$group": {
                "_id": "$course_id",
                "total": {"$sum": "$count"},
            }
        },
    ]
)
rows = activity.to_list()
```

In the async API, await `aggregate()` to obtain an async cursor, then use
`async for` or `await cursor.to_list()`. `$sort` accepts up to 32 ascending or
descending keys and shares `find().sort()` field-path, array, and BSON ordering
rules. `$skip` accepts a nonnegative 64-bit integer, `$limit` requires a positive
64-bit integer, and `$count` emits one named count document—or no document for
empty input. Integral floating-point values are accepted for sort directions,
skip, and limit like MongoDB. Compound sorts preserve values from the same
array element and reject independent parallel arrays; canonical numeric path
parts select array indexes.

`$set` and `$addFields` are aliases that retain the input document, support
nested and dotted array-aware writes, and evaluate every right-hand expression
against the original stage input. `$unset` accepts one field name or a nonempty
list of field names and follows exclusion projection behavior. Numeric
array-index paths remain unsupported for projection output.

`$min` and `$max` ignore null and missing inputs unless every input is null or
missing, in which case they return null. `$sum` and `$avg` ignore missing and
nonnumeric values; `$avg` returns null when it sees no numbers and returns a
`Decimal128` result when any input is Decimal128. `$first` and `$last` follow
the incoming document order and turn a selected missing value into null.
`$push` retains input order, skips missing values, and keeps explicit nulls;
`$addToSet` applies recursive BSON equality while making no output-order
promise. Accumulator operands may use TinyMongo's supported expressions; wrap
a literal array in `$literal` because a direct array is parsed as an invalid
argument list, matching MongoDB. An empty input produces no groups, including
for `_id: None`.
TinyMongo keeps first-seen group order for repeatable local results, but—as with
MongoDB—`$group` output order is not a public guarantee.

Other stages, accumulators, expressions, and aggregation options raise
`TinyMongoNotSupportedError` with the unsupported feature named. The structured
`client.capabilities()["aggregation"]` value lists the exact supported stages,
accumulators, and expressions; `client.supports("aggregation")` reports whether
any aggregation subset is available. In particular, `$replaceRoot`,
`$replaceWith`, `$meta` sort expressions, variables other than `$$REMOVE`, and
MongoDB's broader expression language are not part of this slice yet.

## BSON values

`datetime`, `bytes`, native `uuid.UUID`, and compiled `re.Pattern` values are
supported by every backend. Datetimes are converted to BSON's signed UTC
millisecond representation; naive inputs are treated as UTC and aware inputs
are converted to UTC. `bytearray` is accepted and reads back as `bytes`. Install
the optional BSON extra to use `bson.ObjectId`,
`bson.Decimal128`, `bson.Regex`, `bson.MinKey`, `bson.MaxKey`,
`bson.Timestamp`, `bson.Code`, or non-generic `bson.Binary` subtypes without
making PyMongo a core dependency:

```bash
pip install "tinymongo[bson]"
```

When BSON support is installed, TinyMongo gives writes that omit `_id` a native
`ObjectId`, including `insert_many()` and upsert paths. The returned value can
be reconstructed with `ObjectId(str(result.inserted_id))`. A dependency-free
installation falls back to a 32-character UUID string. The public
`tinymongo.generate_id()` helper always returns that portable UUID string for
callers, such as MongoEngine models, that explicitly want string IDs. Existing
string and integer IDs remain readable and are never rewritten.

```python
from datetime import datetime
import re
from uuid import UUID

from bson import (
    Binary,
    Code,
    Decimal128,
    MaxKey,
    MinKey,
    ObjectId,
    Regex,
    Timestamp,
)

episode_id = ObjectId()
episodes.insert_one(
    {
        "_id": episode_id,
        "created_date": datetime.now(),
        "price": Decimal128("19.95"),
        "image": b"\x89PNG\r\n\x1a\n",
        "asset_id": Binary(bytes(range(16)), subtype=4),
        "session_id": UUID("00112233-4455-6677-8899-aabbccddeeff"),
        "topic_pattern": Regex("python|mongodb", "i"),
        "local_pattern": re.compile("async", re.IGNORECASE),
        "valid_from": MinKey(),
        "valid_until": MaxKey(),
        "cluster_time": Timestamp(1722535200, 1),
        "transform": Code("return value + offset;", {"offset": 1}),
        "title": "Async Python",
    }
)
episode = episodes.find_one({"_id": episode_id})
```

JSON-backed storage uses an explicit tagged representation and restores native
values on read. Existing plain JSON files and string or integer IDs remain
compatible. Binary payloads are base64-encoded, which increases their stored
size by roughly one third. Generic subtype `0` reads back as `bytes`; other
subtypes preserve `bson.Binary.subtype`. The exact two-key mapping shape using
`__tinymongo_type_v1__` and `value` is reserved for TinyMongo's persistence
codec; new user mappings with that shape are escaped automatically. If an
older database already contains that valid tag shape as ordinary data, whether
written through an earlier API or edited manually, rename one of those keys
before upgrading so it is not interpreted as the tagged value. UUID values use
the RFC-4122 byte order and share BSON identity with subtype-4 `Binary` values.
TinyMongo consistently uses this standard UUID representation; unlike a
default PyMongo client, it does not require a per-client UUID representation
setting for native UUID values.
`MinKey`, `MaxKey`, `Timestamp`, and `Code` require the same optional BSON
extra. New `Code` writes retain their JavaScript source, distinct BSON type,
and recursively encoded scope. Earlier TinyMongo releases stored `Code` as an
ordinary string, so those legacy values cannot be distinguished from intended
strings or recovered automatically; rewrite them from an authoritative source
if the distinction matters.

Following MongoDB's write boundary, a direct, non-`_id` `Timestamp(0, 0)`
receives a process-local logical timestamp when inserted or used in a
replacement write. The same value remains literal inside nested documents or
arrays, as an `_id`, or when written by an update modifier such as `$set`.
Separate TinyMongo processes do not share this clock, so generated timestamps
are not a cross-process uniqueness mechanism.

Native compiled patterns retain their Python representation; `bson.Regex`
values retain their BSON representation, pattern, and flags. Regex identity is
the pattern plus MongoDB's canonical option string. Preserving a native
`re.Pattern` on read is a TinyMongo convenience extension; PyMongo decodes the
same wire value as `bson.Regex`. An implicit regex filter,
`$regex`, `$in`, `$nin`, `$all`, or `$not` pattern-matches strings and exact
stored regex values, while explicit `$eq` compares only stored regex identity.
Regex values used directly as operands of `$gt`, `$gte`, `$lt`, or `$lte` are
rejected with MongoDB error code `2`. A regex nested inside a document or array
range operand is comparison data instead, so TinyMongo validates its BSON
transport shape without compiling it as an executable pattern.
TinyMongo evaluates patterns with Python's `re` engine rather than MongoDB's
PCRE2 engine, so a pattern must be valid Python syntax and some advanced syntax
or matching details can differ between the two engines. A stored BSON regex
using the locale flag can still be retrieved by exact regex identity even when
Python cannot apply that flag to a Unicode string predicate.
Non-finite floats (`NaN`, positive infinity, and negative infinity) also use
strict JSON-safe tags. Remote SQL keeps the encoded document as a normal,
indexable object in its existing JSON/JSONB `data` column and stores a second
copy in the nullable text `data_ordered` column to preserve embedded-document
field order. Older rows without that copy remain readable through `data`;
rewriting one populates its ordered copy.

Sorts normalize naive datetimes as UTC and convert aware datetimes to UTC.
BinData—including UUID—sorts by length, subtype, and then unsigned bytes,
matching MongoDB; legacy subtype 2 includes its four-byte inner-length prefix
when its comparison length is calculated. Regex values sort by pattern and
canonical options, `Timestamp` sorts after datetime, unscoped `Code` sorts
before scoped `Code`, and `MinKey`/`MaxKey` bound every supported family.
Numeric `NaN` sorts below every other numeric value, matching MongoDB.
Persistence, returned values, equality, range comparisons, and sorting all use
MongoDB's signed UTC millisecond precision.

Decimal128 values retain their exact BSON representation and participate in
numeric equality and range queries, sorting, embedded-backend unique indexes,
`$inc`, and the supported `$group` numeric accumulators. Remote SQL rejects
Decimal128 values under unique indexes until its native constraints can enforce
MongoDB-equivalent numeric identity across concurrent writers. Reading
`Decimal128` requires the same optional BSON extra used to write it.

When a sort encounters a genuinely unsupported value type, TinyMongo emits one
`TinyMongoUnsupportedWarning` per field and type instead of silently returning
insertion order.

`insert_many()` accepts any iterable of document dictionaries and honors
PyMongo's `ordered` option, which defaults to `True`. An ordered batch keeps the
successful prefix and stops at the first duplicate-key error; an unordered
batch inserts every valid document and reports all duplicate failures in a
PyMongo-shaped `BulkWriteError`. Values are encoded before storage changes, so
a client-side serialization failure leaves the entire TinyMongo input
unwritten. This is a stronger whole-list guarantee than PyMongo provides for
very large inputs, which it may split across multiple wire batches.

An unsupported value raises `tinymongo.InvalidDocument` before storage is
changed. The exception retains the rejected document in its `document`
attribute, and its message identifies the collection and full nested value path,
plus the document `_id` when available and the batch index for `insert_many()`.
This extra context is more diagnostic than PyMongo's standard message while
preserving compatible exception identity. With PyMongo installed, it can be
caught as either `bson.errors.InvalidDocument` or
`pymongo.errors.PyMongoError`; dependency-free callers can catch
`tinymongo.errors.TinyMongoError`.

TinyMongo includes PyMongo-shaped contract tests that run application code with
`import pymongo` redirected to TinyMongo:

```bash
pytest tests/test_pymongo_contract.py tests/test_pymongo_dropin.py
```

The shared compatibility contracts run the same application-facing behaviors
against every embedded backend:

```bash
pytest tests/contracts
```

To include a real MongoDB server explicitly:

```bash
TINYMONGO_MONGODB_URI=mongodb://127.0.0.1:27017/?directConnection=true \
pytest -o addopts='' -q -m 'contract and mongodb' tests/contracts
```

Use `-m contract` instead of `-m 'contract and mongodb'` to run the complete
embedded-plus-MongoDB matrix in one session.

CI publishes the JUnit results together with deterministic JSON and Markdown
reports containing per-backend outcomes and a documented compatibility score.
See
[Compatibility reports](https://github.com/schapman1974/tinymongo/blob/master/docs/COMPATIBILITY_REPORTS.md)
to generate them locally and understand how passes, expected gaps, skips, and
the MongoDB reference affect the score.

The shared Talk-Python-derived contracts run through both TinyMongo API modes.
To exercise the actual application without rewriting its PyMongo call sites,
follow the
[Talk Python acceptance run](https://github.com/schapman1974/tinymongo/blob/master/docs/TALKPYTHON_ACCEPTANCE.md).

PyMongo remains optional. It is needed for these comparisons,
`tinymongo.patch()`, richer BSON values (`ObjectId`, `Binary`, `Code`,
`Decimal128`, `MaxKey`, `MinKey`, `Regex`, and `Timestamp`), and conditional
PyMongo exception inheritance, but it is not required for normal TinyMongo
clients, `datetime`, UUID, compiled `re.Pattern`, or native subtype-0 byte
storage. When it is installed, TinyMongo error classes also inherit the
matching `pymongo.errors` classes, so existing `PyMongoError` handlers continue
to work. `InvalidDocument` additionally preserves BSON's standard error
identity while remaining inside TinyMongo's portable error hierarchy.

PyMongo's full upstream driver test suite targets a real MongoDB server and
driver internals, so it is not expected to pass against TinyMongo. The contract
tests are the supported compatibility boundary for local file-backed usage.

## Backend capabilities

TinyMongo reports behavior that each configured backend can honor:

```python
client = TinyMongoClient("./data", backend="sqlite")
capabilities = client.capabilities()
print(capabilities)
print(capabilities["query_operators"]["field"])
print(capabilities["query_operators"]["ignored"])
print(capabilities["update_operators"]["operators"])
print(capabilities["update_operators"]["modifiers"])
print(capabilities["bson_types"]["pymongo"])
print(client.supports("multiprocess_writes"))
```

The capability map covers persistence, remote and object storage, table-native
storage, multiprocess writes, native indexes, projections, bulk writes,
aggregation, query and update operators, BSON types, sessions, transactions,
and change streams. `query_operators` separates top-level logical operators
from field operators and accepted-but-ignored metadata operators such as
`$comment`. `update_operators` lists every supported update operator and maps
`$push` and `$addToSet` to their accepted modifiers; its operator tuple includes
`$pullAll`. `bson_types["pymongo"]` reports the installed optional `Binary`,
`Code`, `Decimal128`, `MaxKey`, `MinKey`, `ObjectId`, `Regex`, and `Timestamp`
families.
`bson_types` separates dependency-free `native` families from the richer
`pymongo` types available when the optional BSON extra is installed. These
values are structured mappings; use `client.supports()` for a Boolean feature
check or inspect their tuples when selecting a particular operator or type.
Unknown capability names raise `ValueError` so configuration mistakes are
visible.

For local persistent backends, `multiprocess_writes=True` promises safe writes,
not parallel write throughput. TinyDB JSON, SQLite, and DuckDB clients sharing
one storage folder serialize writes across databases and collections through a
store-wide advisory lock. Parquet uses one lock per logical database directory.
Lock acquisition waits for up to 30 seconds before timing out. SQLite uses WAL
mode, so reads can continue while another process holds the write lock, but its
writes remain serialized.

The opt-in [`sqlite-sharded` backend](https://github.com/schapman1974/tinymongo/blob/master/docs/SHARDED_SQLITE.md) removes that
single-file write bottleneck by routing documents across independent SQLite
files. It remains experimental: exact `_id` operations are targeted and can
write concurrently across shards; exact-ID reads reuse bounded query-only
connections and a generation-validated manifest catalog, while broad queries
fan out and secondary unique indexes require cross-shard coordination.

Operations whose semantics TinyMongo cannot honor raise
`TinyMongoNotSupportedError`. This includes sessions, transactions, change
streams, aggregation features outside the documented core subset, bulk writes,
database commands other than the discovery-safe `ping` and `buildInfo` subset,
non-default read/write concerns, and unsupported index specifications.
`list_collection_names()` accepts PyMongo's `authorizedCollections` and
`nameOnly` server hints for ODM startup. Connection options that only describe
an ignored network target remain harmless for drop-in use.
`bypass_document_validation` is accepted as a compatibility no-op and never
disables `_id` or unique-index enforcement. Where a compatibility method
accepts a `session` keyword, `session=None` is allowed; non-`None` sessions and
`start_session()` remain unsupported.

## MongoEngine and Beanie

Basic MongoEngine CRUD is supported by passing TinyMongo as the client class.
MongoEngine's native `ObjectId` primary key is supported when `tinymongo[bson]`
is installed, so most models do not need a custom ID field:

```python
import mongoengine as me
import tinymongo

me.connect(
    "app",
    host="mongodb://localhost",
    mongo_client_class=tinymongo.MongoClient,
    tinymongo_folder="./tinydb",
    uuidRepresentation="standard",
)

class Person(me.Document):
    name = me.StringField(required=True)
```

Use `id = me.StringField(primary_key=True, default=tinymongo.generate_id)` only
when the application deliberately wants string IDs.

Beanie 2.1 can initialize against TinyMongo's async client and use its ordinary
CRUD surface without application-side shims:

```python
import beanie
import tinymongo

class PersonDocument(beanie.Document):
    name: str

client = tinymongo.AsyncMongoClient(
    tinymongo_folder="./tinydb",
    backend="sqlite",
)
await beanie.init_beanie(
    database=client.app,
    document_models=[PersonDocument],
)
```

The compatibility layer supplies Beanie's `buildInfo` discovery command,
collection-listing hints, and PyMongo-shaped update and delete reply documents.
Single-field, ascending compound, sparse, and partial unique indexes work
through the same durable index layer. Unsupported key types and unique-value
combinations retain the fail-closed behavior described above.

The tested subset covers document creation, repeated saves, queries, updates,
deletes, counts, and collection drops. Aggregation beyond the documented core
subset, sessions, and MongoDB server features remain outside TinyMongo's
compatibility scope.


# Examples

The quick start is shown below.  For a more detailed look at tinymongo,
take a look at demo.py within the repository.

```python
    from tinymongo import TinyMongoClient

    # you can include a folder name or absolute path
    # as a parameter if not it will default to "tinydb"
    connection = TinyMongoClient()

    # either creates a new database file or accesses an existing one named `my_tiny_database`
    db = connection.my_tiny_database

    # either creates a new collection or accesses an existing one named `users`
    collection = db.users

    # insert data adds a new record returns _id
    record_id = collection.insert_one({"username": "admin", "password": "admin", "module":"somemodule"}).inserted_id
    user_info = collection.find_one({"_id": record_id})  # returns the record inserted

    # you can also use it directly
    db.users.insert_one({"username": "admin"})

    # returns a list of all users of 'module'
    users = db.users.find({'module': 'module'})

    # update data and inspect matched_count or modified_count on the result
    upd = db.users.update_one({"username": "admin"}, {"$set": {"module":"someothermodule"}})

    # Sorting users by its username DESC
    # omitting `filter` returns all records
    db.users.find(sort=[('username', -1)])

    # Pagination of the results
    # Getting the first 20 records
    db.users.find(sort=[('username', -1)], skip=0, limit=20)
    # Getting next 20 records
    db.users.find(sort=[('username', -1)], skip=20, limit=20)

    # Getting the total of records
    db.users.count()

```

# Custom Storages and Serializers

> HINT: Learn more about TinyDB storages and Serializers in [documentation](https://tinydb.readthedocs.io/en/latest/usage.html#storages-middlewares)

## Custom Storages

You have to subclass `TinyMongoClient` and provide custom storages like
CachingMiddleware or other available TinyDB Extension.

### Caching Middleware

```python
    from tinymongo import TinyMongoClient
    from tinydb.storages import JSONStorage
    from tinydb.middlewares import CachingMiddleware

    class CachedClient(TinyMongoClient):
        """This client has cache"""
        @property
        def _storage(self):
            return CachingMiddleware(JSONStorage)

    connection = CachedClient('/path/to/folder')
```

> HINT: You can nest middlewares: `FirstMiddleware(SecondMiddleware(JSONStorage))`


## Serializers

To convert your data to a format that is writable to disk TinyDB uses the Python JSON module by default. It's great when only simple data types are involved but it cannot handle more complex data types like custom classes.

To support serialization of complex types you can write
your own serializers using the `tinydb-serialization` extension.

First install it with `pip install "tinymongo[serialization]"`.

## Custom serialized types

`datetime`, binary values, and optional `ObjectId` values are handled directly
by TinyMongo; they do not need a custom serializer. For application-specific
classes, subclass `TinyMongoClient` and provide a `tinydb-serialization`
middleware as described in the TinyDB extension documentation. The legacy
`tinymongo.serializers.DateTimeSerializer` remains available for existing
custom-storage integrations, but new TinyMongo storage does not require it.

# Flask-Admin

This extension can work with Flask-Admin which gives a web based administrative
panel to your TinyDB. Flask-Admin has features like filtering, search, web forms to
perform CRUD (Create, Read, Update, Delete) of the TinyDB records.

You can find the example of Flask-Admin with TinyMongo in [Flask-Admin Examples Repository](https://github.com/flask-admin/flask-admin/tree/master/examples/tinymongo)

Datetime fields work with TinyMongo's built-in storage codec.

# Contributions

Contributions are welcome! Currently, the most valuable contributions
would be:

- adding test cases
- adding functionality consistent with PyMongo
- improving documentation
- identifying bugs and issues

# Future development

Planned compatibility, aggregation, GridFS, wire-server, Compass, and browser
work is tracked in the [TinyMongo roadmap](https://github.com/schapman1974/tinymongo/blob/master/ROADMAP.md).

# License

MIT License
