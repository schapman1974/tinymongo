![TinyMongo logo](https://raw.githubusercontent.com/schapman1974/tinymongo/master/artwork/tinymongo.png)

[![CI](https://github.com/schapman1974/tinymongo/actions/workflows/ci.yml/badge.svg)](https://github.com/schapman1974/tinymongo/actions/workflows/ci.yml)

# Purpose

TinyMongo provides a familiar PyMongo-style document API backed by embedded
storage instead of a MongoDB server. The default backend uses
[TinyDB](https://tinydb.readthedocs.io/), with optional memory, SQLite, DuckDB,
Parquet, PostgreSQL, and MariaDB/MySQL backends.

# Status

TinyMongo supports Python 3.9 and newer and is tested in GitHub Actions on
Python 3.9, 3.11, and 3.13.

# Installation

The latest stable release can be installed via `pip install tinymongo`.

The library is currently under rapid development and a more recent version
may be desired.

In this case, simply clone this repository, navigate
to the root project directory, and `pip install -e .`

or use `pip install -e git+https://github.com/schapman1974/tinymongo.git#egg=tinymongo`

The default JSON backend has a small dependency set. Optional database backends
may install native binary wheels supplied by DuckDB, PyArrow, or SQL drivers.

# Project notes

- **Roadmap:** See the [TinyMongo roadmap](https://github.com/schapman1974/tinymongo/blob/master/ROADMAP.md) for planned compatibility, GridFS, Compass, wire-server, and browser work.
- **Default storage:** TinyMongo uses TinyDB-compatible JSON storage unless another backend is selected.
- **Table-native backends:** SQLite, DuckDB, and Parquet backends store one real table/file per collection instead of one serialized database blob.
- **Concurrency:** writes use atomic temp-file replace and optional advisory locks (`portalocker`) to reduce corruption risk under concurrent writers.
- **Async API:** async clients keep storage and lock waits off the event loop
  while sharing the synchronous implementation's behavior.
- **Tests & CI:** a GitHub Actions workflow is included at `.github/workflows/ci.yml` to run unit tests and linters across Python versions. See `requirements-dev.txt` for dev dependencies.


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
for server features such as authentication, replica sets, aggregation pipelines,
sessions, or network connections. MongoDB URIs, host names, ports, and common
connection kwargs are accepted and ignored so existing code can be tried locally.
Set `TINYMONGO_HOME` or pass `tinymongo_folder=` to choose where TinyMongo stores
files. See `examples/pymongo_dropin.py` for a runnable example.

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

Parquet can also store collection files in object storage by passing
`storage_uri` or setting `TINYMONGO_STORAGE_URI`. Object-storage Parquet is
experimental in `1.2.0` and currently uses one Parquet file per collection, so
updates/deletes rewrite that file:

```python
    s3_connection = TinyMongoClient(
        "/local/fallback-folder",
        backend="parquet",
        storage_uri="s3://my-bucket/tinymongo",
    )
```

Available backends:

- `memory`: Process-local storage that creates no database or lock files. Each unnamed client is isolated; a `memory://NAME` URI explicitly shares a named namespace within one process.
- `tinydb` or `json`: TinyDB-compatible JSON storage. This is the default and writes `.json` files.
- `sqlite`: Table-native SQLite storage using one SQL table per collection. This writes `.sqlite` files.
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
with the corresponding installation command. PyMongo itself is not a runtime
dependency; it is used by the optional patch helper and development compatibility
tests only when those features are selected. Install `tinymongo[bson]` for
`ObjectId` values or `tinymongo[pymongo]` for patching and
installed-version compatibility.

| Backend | Dependency | Best fit | Notes |
| --- | --- | --- | --- |
| `memory` | None | Isolated tests and temporary data | Creates no files. Named `memory://NAME` namespaces can be shared only within one process. |
| `tinydb` / `json` | TinyDB | Default local JSON files | Human-readable and simplest to inspect. |
| `sqlite` | Python standard library | Embedded transactional storage | Uses `_id` primary keys and JSON document payloads in collection tables. |
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
tinymongo inspect ./local-cache --backend parquet --storage-uri s3://my-bucket/tinymongo
tinymongo migrate ./tinydb ./local-cache --to-backend parquet --target-uri s3://my-bucket/tinymongo
tinymongo migrate ./tinydb ./unused --to-backend postgres --target-dsn "$TINYMONGO_POSTGRES_DSN"
```


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
case-insensitive `$options`, and `$exists`.

Update support includes `$set`, `$unset`, `$inc`, `$push`, `$pull`, and
`$addToSet`, including `upsert=True`. As in PyMongo, `update_one()` and
`update_many()` require update operators; use `replace_one()` for full-document
replacement.

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

Collections expose durable indexes and unique constraints:

```python
collection.create_index("email", unique=True, name="login_email")
collection.find({"email": "person@example.com"})
collection.list_indexes()
collection.drop_index("login_email")
```

Index definitions and unique constraints survive client restarts on persistent
backends. SQLite, PostgreSQL, and MariaDB/MySQL create native database indexes;
JSON, DuckDB, and Parquet persist metadata and enforce unique constraints through
TinyMongo. Named memory databases retain metadata while their process-local
namespace exists.

Plural `create_indexes()` accepts duck-typed PyMongo `IndexModel` batches
without importing PyMongo in TinyMongo's core. Unique indexes are enforced.
Performance-only declarations degrade safely and emit
`TinyMongoUnsupportedWarning`: descending and hashed declarations use
ascending equality indexes, and compound declarations use their ascending
leading-field prefix. Sparse membership is not honored and TTL expiration is
not performed; both are reported explicitly. A semantically unsafe unique
declaration is rejected before any batch entry is created.

Unique indexes support scalar values and flat arrays on embedded backends;
missing and `null` share one unique key. Object values, nested arrays,
non-finite numbers, and array traversal inside a dotted index path are not
supported for unique indexes yet. Remote SQL uses native constraints for scalar
races. It rejects array values under unique indexes because its native indexes
cannot yet guarantee cross-process multikey uniqueness.

Use one consistent scalar type for `_id` values. Full BSON-aware `_id`
comparison is tracked in [#94](https://github.com/schapman1974/tinymongo/issues/94);
cross-type comparison and sort order remain outside the current BSON slice.

## ObjectId and datetime values

`datetime` values round-trip through every backend. Install the optional BSON
extra to use `bson.ObjectId` without making PyMongo a core dependency:

```bash
pip install "tinymongo[bson]"
```

```python
from datetime import datetime
from bson import ObjectId

episode_id = ObjectId()
episodes.insert_one(
    {"_id": episode_id, "created_date": datetime.now(), "title": "Async Python"}
)
episode = episodes.find_one({"_id": episode_id})
```

JSON-backed storage uses an explicit tagged representation and restores native
values on read. Existing plain JSON files and string or integer IDs remain
compatible. UUID, Decimal128, Binary, and regular-expression round trips remain
tracked in [#75](https://github.com/schapman1974/tinymongo/issues/75).

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
See [Compatibility reports](docs/COMPATIBILITY_REPORTS.md) to generate them
locally and understand how passes, expected gaps, skips, and the MongoDB
reference affect the score.

PyMongo remains optional. It is needed for these comparisons,
`tinymongo.patch()`, and `ObjectId` support, but it is not required for normal
TinyMongo clients or `datetime` storage. When it is installed, TinyMongo error
classes also inherit the matching `pymongo.errors` classes, so existing
`PyMongoError` handlers continue to work.

PyMongo's full upstream driver test suite targets a real MongoDB server and
driver internals, so it is not expected to pass against TinyMongo. The contract
tests are the supported compatibility boundary for local file-backed usage.

## Backend capabilities

TinyMongo reports behavior that each configured backend can honor:

```python
client = TinyMongoClient("./data", backend="sqlite")
print(client.capabilities())
print(client.supports("multiprocess_writes"))
```

The capability map covers persistence, remote and object storage, table-native
storage, multiprocess writes, native indexes, projections, bulk writes,
aggregation, sessions, transactions, change streams, and installed BSON support.
Unknown capability names raise `ValueError` so configuration mistakes are
visible.

Operations whose semantics TinyMongo cannot honor raise
`TinyMongoNotSupportedError`. This includes sessions, transactions, change
streams, aggregation pipelines, bulk writes, database commands, non-default
read/write concerns, and unsupported index specifications. Connection options
that only describe an ignored network target remain harmless for drop-in use.

## MongoEngine

Basic MongoEngine CRUD is supported by passing TinyMongo as the client class.
The example keeps a string primary key for maximum portability. Native
`ObjectId` values also round-trip when `tinymongo[bson]` is installed:

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
    id = me.StringField(primary_key=True, default=tinymongo.generate_id)
    name = me.StringField(required=True)
```

The tested subset covers document creation, repeated saves, queries, updates,
deletes, counts, and collection drops. Advanced aggregation, sessions, and
MongoDB server features remain outside TinyMongo's compatibility scope.


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

`datetime` and optional `ObjectId` values are handled directly by TinyMongo;
they do not need a custom serializer. For application-specific classes, subclass
`TinyMongoClient` and provide a `tinydb-serialization` middleware as described
in the TinyDB extension documentation. The legacy
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
