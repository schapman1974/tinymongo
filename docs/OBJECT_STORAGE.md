# Parquet Object Storage

TinyMongo's `parquet` and `parquetv2` backends can place collection Parquet
files under an object-storage URI. DuckDB performs the remote file reads and
writes, so the same collection API can target local files or object storage.

```python
from tinymongo import TinyMongoClient

client = TinyMongoClient(
    "/tmp/tinymongo-cache",
    backend="parquet",
    storage_uri="s3://my-bucket/tinymongo",
)
client.app.users.insert_one({"_id": "ada", "name": "Ada"})
```

The same value can be provided with an environment variable:

```bash
export TINYMONGO_STORAGE_URI=s3://my-bucket/tinymongo
```

TinyMongo stores each database as a `.parquet` prefix and each collection as a
Parquet file under that prefix:

```text
s3://my-bucket/tinymongo/app.parquet/users.parquet
s3://my-bucket/tinymongo/app.parquet/events.parquet
```

## CLI Usage

```bash
tinymongo inspect ./local-cache \
  --backend parquet \
  --storage-uri s3://my-bucket/tinymongo

tinymongo export ./local-cache app users \
  --backend parquet \
  --storage-uri s3://my-bucket/tinymongo \
  -o users.json

tinymongo migrate ./tinydb ./local-cache \
  --to-backend parquet \
  --target-uri s3://my-bucket/tinymongo
```

For remote-to-remote migrations, use `--source-uri` and `--target-uri`.

## Supported URI Families

TinyMongo recognizes these object-storage URI schemes:

| Scheme | Typical provider |
| --- | --- |
| `s3://` | AWS S3, Backblaze B2, Cloudflare R2, MinIO, Wasabi, DigitalOcean Spaces |
| `gs://` or `gcs://` | Google Cloud Storage |
| `az://` or `azure://` | Azure Blob Storage |
| `abfs://` or `abfss://` | Azure Data Lake Storage compatible paths |

S3-compatible providers usually work by setting an endpoint URL plus standard
AWS-style access keys.

## Environment Variables

TinyMongo maps these environment variables into DuckDB object-storage settings:

| TinyMongo env var | Common fallback | DuckDB behavior |
| --- | --- | --- |
| `TINYMONGO_S3_REGION` | `AWS_REGION`, `AWS_DEFAULT_REGION` | `s3_region` |
| `TINYMONGO_S3_ACCESS_KEY_ID` | `AWS_ACCESS_KEY_ID` | `s3_access_key_id` |
| `TINYMONGO_S3_SECRET_ACCESS_KEY` | `AWS_SECRET_ACCESS_KEY` | `s3_secret_access_key` |
| `TINYMONGO_S3_SESSION_TOKEN` | `AWS_SESSION_TOKEN` | `s3_session_token` |
| `TINYMONGO_S3_ENDPOINT` | `AWS_ENDPOINT_URL` | `s3_endpoint` |
| `TINYMONGO_S3_URL_STYLE` | | `s3_url_style` |
| `TINYMONGO_S3_USE_SSL` | | `s3_use_ssl` |
| `TINYMONGO_GCS_KEY_ID` | `GOOGLE_HMAC_KEY_ID` | creates a `gcs` secret |
| `TINYMONGO_GCS_SECRET` | `GOOGLE_HMAC_SECRET` | creates a `gcs` secret |
| `TINYMONGO_AZURE_CONNECTION_STRING` | `AZURE_STORAGE_CONNECTION_STRING` | creates an `azure` secret |

Advanced DuckDB setup can be supplied as semicolon-separated SQL:

```bash
export TINYMONGO_DUCKDB_SETUP_SQL="INSTALL httpfs; LOAD httpfs"
```

Use this for provider-specific DuckDB secrets or settings not covered by the
standard environment mapping.

## AWS S3

```bash
export TINYMONGO_STORAGE_URI=s3://my-bucket/tinymongo
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-east-1
```

## Backblaze B2

Backblaze B2 uses the S3-compatible API.

```bash
export TINYMONGO_STORAGE_URI=s3://my-bucket/tinymongo
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-west-004
export AWS_ENDPOINT_URL=s3.us-west-004.backblazeb2.com
export TINYMONGO_S3_URL_STYLE=path
```

## Cloudflare R2

```bash
export TINYMONGO_STORAGE_URI=s3://my-bucket/tinymongo
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=auto
export AWS_ENDPOINT_URL=<account-id>.r2.cloudflarestorage.com
export TINYMONGO_S3_URL_STYLE=path
```

## MinIO, Wasabi, And DigitalOcean Spaces

Use the provider endpoint and normal S3-style keys:

```bash
export TINYMONGO_STORAGE_URI=s3://my-bucket/tinymongo
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-east-1
export AWS_ENDPOINT_URL=localhost:9000
export TINYMONGO_S3_URL_STYLE=path
export TINYMONGO_S3_USE_SSL=false
```

For hosted providers, use their HTTPS endpoint and leave SSL enabled.

## Google Cloud Storage

DuckDB supports GCS through HMAC-style credentials. Create an HMAC key for the
target bucket and expose it:

```bash
export TINYMONGO_STORAGE_URI=gs://my-bucket/tinymongo
export GOOGLE_HMAC_KEY_ID=...
export GOOGLE_HMAC_SECRET=...
```

If your DuckDB version requires explicit secret SQL, use
`TINYMONGO_DUCKDB_SETUP_SQL` to create the secret before queries run.

## Azure Blob Storage

```bash
export TINYMONGO_STORAGE_URI=az://my-container/tinymongo
export AZURE_STORAGE_CONNECTION_STRING=...
```

If your DuckDB version requires explicit Azure extension setup, use:

```bash
export TINYMONGO_DUCKDB_SETUP_SQL="INSTALL azure; LOAD azure"
```

## Operational Notes

Object-storage Parquet is useful for portable datasets, analytics workflows,
and shared file-backed data. It is not a full transactional remote database.

Important tradeoffs:

- Updates and deletes rewrite a collection Parquet file.
- Concurrent writers to the same collection URI can overwrite each other.
- Object storage is usually eventually consistent around listing and metadata.
- `drop_collection()` writes an empty Parquet file for object stores when a
  direct delete API is not available through DuckDB.

For strict remote transactions, prefer a future SQL backend such as PostgreSQL
or MariaDB/MySQL rather than object-storage Parquet.

## Future SQL Backends

The planned remote transactional backend family is separate from Parquet object
storage:

```python
TinyMongoClient(backend="postgres", dsn="postgresql://user:pass@host/db")
TinyMongoClient(backend="mariadb", dsn="mysql://user:pass@host/db")
```

Those backends should use server-side tables, durable indexes, and database
transactions instead of object-file rewrites.
