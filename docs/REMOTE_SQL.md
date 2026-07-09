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
to keep CLI commands consistent across all backends.

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

- `_id`: string primary key
- `data`: JSON/JSONB document payload

TinyMongo also creates `__tinymongo_collections` to track database and
collection names.

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
