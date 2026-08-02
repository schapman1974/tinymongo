# Remote SQL Backends

TinyMongo can use remote transactional SQL databases for storage while keeping
the Mongo-like collection API. These backends use one server-side SQL table per
TinyMongo database and collection, plus a small metadata table for listing
databases and collections.

Supported backend names:

- `postgres` or `postgresql`
- `mysql` or `mariadb`

Install the optional driver dependencies for the backend you use:

```bash
pip install "tinymongo[postgres]"
pip install "tinymongo[mysql]"
pip install "tinymongo[mariadb]"
pip install "tinymongo[remote-sql]"
```

If a required driver is missing, TinyMongo raises an `ImportError` that includes
the exact `pip install ...` command to run.

## PostgreSQL

```python
from tinymongo import TinyMongoClient

client = TinyMongoClient(
    backend="postgres",
    dsn="postgresql://user:password@localhost:5432/tinymongo",
)

client.app.users.insert_one({"_id": "ada", "name": "Ada"})
```

The DSN can also come from an environment variable:

```bash
export TINYMONGO_POSTGRES_DSN=postgresql://user:password@localhost:5432/tinymongo
```

Fallback env vars:

- `TINYMONGO_POSTGRESQL_DSN`
- `DATABASE_URL`

PostgreSQL stores document payloads in a `JSONB` column.

## MariaDB / MySQL

```python
from tinymongo import TinyMongoClient

client = TinyMongoClient(
    backend="mariadb",
    dsn="mysql://user:password@localhost:3306/tinymongo",
)

client.app.users.insert_one({"_id": "ada", "name": "Ada"})
```

The DSN can also come from an environment variable:

```bash
export TINYMONGO_MYSQL_DSN=mysql://user:password@localhost:3306/tinymongo
```

Fallback env vars:

- `TINYMONGO_MARIADB_DSN`
- `MYSQL_URL`
- `MARIADB_URL`

MariaDB/MySQL stores document payloads in a `JSON` column.

## CLI Usage

```bash
tinymongo inspect ./unused --backend postgres --dsn "$TINYMONGO_POSTGRES_DSN"

tinymongo export ./unused app users \
  --backend postgres \
  --dsn "$TINYMONGO_POSTGRES_DSN" \
  -o users.json

tinymongo migrate ./tinydb ./unused \
  --to-backend postgres \
  --target-dsn "$TINYMONGO_POSTGRES_DSN"
```

The `path` argument is ignored for remote SQL backends, but it is still present
to keep CLI commands consistent across all backends. When the DSN flags are
omitted, CLI commands use the environment-variable precedence documented above.

## Storage Layout

Remote SQL backends create tables named with this pattern:

```text
<tinydb_database>__<collection>
```

For example:

```text
app__users
app__events
```

Each table has:

- `_id`: opaque BSON-aware primary key for new rows, with legacy stringified
  keys still readable and mutable
- `data`: JSON/JSONB document payload
- `data_ordered`: nullable `TEXT` on PostgreSQL or `LONGTEXT` on
  MariaDB/MySQL, holding an encoded copy that preserves embedded-document field
  order and strict-JSON non-finite values

TinyMongo also creates `__tinymongo_collections` to track database and
collection names and `__tinymongo_indexes` to persist index definitions.
Non-unique indexes are backed by native SQL indexes derived from the unchanged
`data` JSON object. Each unique index also has a private 64-character token
column. TinyMongo derives that token from its canonical BSON scalar identity,
so the database can enforce exact int/float equality across processes without
rounding large values through a SQL numeric type. Booleans remain distinct
from numbers. Arrays, Decimal128, UUID/Binary, and regex values remain
fail-closed under remote unique indexes.

When TinyMongo first opens a table created before 1.2.1, it adds
`data_ordered` automatically. The database account therefore needs
`ALTER TABLE` permission during the upgrade. Existing rows remain readable
with a null ordered copy, and existing native JSON indexes remain valid because
the `data` column keeps its original object representation. PostgreSQL JSONB
may already have normalized field order in legacy rows. TinyMongo can recover a
literal container `_id` from its legacy physical row key; other legacy mappings
retain the order PostgreSQL returns, not necessarily the document's original
application order.

Older remote unique indexes are upgraded on first access. TinyMongo locks the
collection, checks existing values under current BSON identity rules, adds and
backfills the token column, swaps the native unique index, and records the new
token version. If legacy values such as `int(1e23)` and `1e23` are exact BSON
duplicates, the upgrade raises `DuplicateKeyError` and leaves the catalog row
stale so a later retry also fails closed. The account needs permission to alter
tables and create or drop indexes. Do not mix older and newer TinyMongo writers
during this schema upgrade.

## Query Behavior

The first remote SQL implementation prioritizes correct TinyMongo behavior and
transactional remote storage. `_id` lookups use the SQL primary key. Other
Mongo-style filters currently read collection documents and apply TinyMongo's
Python matcher, matching the behavior used as a fallback by the local table
backends.

Future improvements can push more JSON predicates into PostgreSQL JSONB and
MariaDB/MySQL JSON SQL.

## Integration Tests

Remote SQL tests are opt-in:

```bash
TINYMONGO_POSTGRES_DSN=postgresql://user:password@localhost:5432/tinymongo \
pytest -m integration tests/integration/test_remote_sql.py

TINYMONGO_MYSQL_DSN=mysql://user:password@localhost:3306/tinymongo \
pytest -m integration tests/integration/test_remote_sql.py
```
