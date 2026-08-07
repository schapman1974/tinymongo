"""Command line tools for inspecting and moving TinyMongo data."""

from __future__ import absolute_import

import argparse
import copy
import os
import sys
from contextlib import contextmanager
from uuid import uuid4

from .bson_codec import dumps as bson_json_dumps
from .bson_codec import loads as bson_json_loads
from .storage_backends import is_remote_sql_backend, storage_extension
from .tinymongo import TinyMongoClient


SUPPORTED_BACKENDS = (
    "tinydb",
    "json",
    "parquet",
    "parquetv2",
    "sqlite",
    "sqlite-sharded",
    "duckdb",
    "postgres",
    "postgresql",
    "mysql",
    "mariadb",
)


def _effective_storage_uri(backend, storage_uri=None):
    if str(backend).lower() not in ("parquet", "parquetv2"):
        return None
    if storage_uri is not None:
        return storage_uri
    return os.environ.get("TINYMONGO_STORAGE_URI")


def _effective_dsn(backend, dsn=None):
    backend = str(backend).lower()
    if backend in ("postgres", "postgresql"):
        names = (
            "TINYMONGO_POSTGRES_DSN",
            "TINYMONGO_POSTGRESQL_DSN",
            "DATABASE_URL",
        )
    elif backend in ("mysql", "mariadb"):
        names = (
            "TINYMONGO_MYSQL_DSN",
            "TINYMONGO_MARIADB_DSN",
            "MYSQL_URL",
            "MARIADB_URL",
        )
    else:
        return None
    if dsn is not None:
        return dsn
    return next((os.environ[name] for name in names if os.environ.get(name)), None)


def _client(path, backend, storage_uri=None, dsn=None, sqlite_shards=None):
    options = {
        "backend": backend,
        "storage_uri": _effective_storage_uri(backend, storage_uri),
        "dsn": _effective_dsn(backend, dsn),
    }
    if sqlite_shards is not None:
        options["sqlite_shards"] = sqlite_shards
    return TinyMongoClient(path, **options)


@contextmanager
def _managed_client(path, backend, storage_uri=None, dsn=None, sqlite_shards=None):
    client = _client(
        path,
        backend,
        storage_uri=storage_uri,
        dsn=dsn,
        sqlite_shards=sqlite_shards,
    )
    try:
        yield client
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _db_names(path, backend, storage_uri=None, dsn=None, sqlite_shards=None):
    effective_storage_uri = _effective_storage_uri(backend, storage_uri)
    if is_remote_sql_backend(backend) or effective_storage_uri:
        with _managed_client(
            path,
            backend,
            storage_uri=effective_storage_uri,
            dsn=_effective_dsn(backend, dsn),
            sqlite_shards=sqlite_shards,
        ) as client:
            return client.list_database_names()
    ext = storage_extension(backend)
    if not os.path.isdir(path):
        return []
    names = []
    for filename in sorted(os.listdir(path)):
        if filename.startswith(".") or not filename.endswith(ext):
            continue
        names.append(filename[: -len(ext)])
    return names


def _load_json(path):
    if path == "-":
        return bson_json_loads(sys.stdin.read())
    with open(path, "r", encoding="utf-8") as handle:
        return bson_json_loads(handle.read())


def _dump_json(data, path, sort_keys=True):
    payload = bson_json_dumps(data, indent=2, sort_keys=sort_keys)
    if path == "-":
        sys.stdout.write(payload + "\n")
        return
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload + "\n")


def _collection_names(database):
    """Return user collections without TinyDB's internal default table."""

    modern_listing = getattr(database, "list_collection_names", None)
    if callable(modern_listing):
        names = modern_listing()
    else:
        names = database.collection_names()
    return [name for name in names if name != "_default"]


def _copy_indexes(source, target):
    """Recreate a collection's effective indexes on a staging collection."""

    for metadata in source.list_indexes():
        if metadata["name"] == "_id_":
            continue
        options = {"name": metadata["name"]}
        if metadata.get("unique"):
            options["unique"] = True
        if metadata.get("sparse"):
            options["sparse"] = True
        if metadata.get("partialFilterExpression") is not None:
            options["partialFilterExpression"] = copy.deepcopy(
                metadata["partialFilterExpression"]
            )
        target.create_index(copy.deepcopy(metadata["key"]), **options)


def _replace_collection(database, collection_name, docs):
    """Preflight a complete replacement before changing the destination."""

    # Keep validation isolated from the target client (and from CLI client
    # factories that callers may patch for remote connection setup).
    from .tinymongo import TinyMongoClient as PreflightClient

    collection = database[collection_name]
    with PreflightClient(backend="memory") as preflight_client:
        stage = preflight_client.preflight[
            "__tinymongo_stage_{0}".format(uuid4().hex[:12])
        ]
        _copy_indexes(collection, stage)
        if docs:
            stage.insert_many(copy.deepcopy(docs))

    previous = list(collection.find({}))
    try:
        collection.delete_many({})
        if docs:
            collection.insert_many(docs)
    except Exception as replacement_error:
        # Staging eliminates deterministic codec, ID, and index failures. This
        # rollback is the final guard for an environmental or concurrent error
        # between preflight and the destination write.
        try:
            collection.delete_many({})
            if previous:
                collection.insert_many(previous)
        except Exception as rollback_error:
            raise RuntimeError(
                "Collection replacement failed and the previous data could not "
                "be restored: {0}".format(replacement_error)
            ) from rollback_error
        raise


def cmd_inspect(args):
    storage_uri = _effective_storage_uri(args.backend, args.storage_uri)
    payload = {"path": args.path, "backend": args.backend, "databases": []}
    if storage_uri:
        payload["storage_uri"] = storage_uri
    shard_options = {}
    if args.sqlite_shards is not None:
        shard_options["sqlite_shards"] = args.sqlite_shards
    with _managed_client(
        args.path,
        args.backend,
        storage_uri=storage_uri,
        dsn=args.dsn,
        **shard_options,
    ) as client:
        for db_name in _db_names(
            args.path,
            args.backend,
            storage_uri=storage_uri,
            dsn=args.dsn,
            **shard_options,
        ):
            db = client[db_name]
            collections = []
            for collection_name in sorted(_collection_names(db)):
                count = db[collection_name].count()
                collections.append({"name": collection_name, "count": count})
            payload["databases"].append({"name": db_name, "collections": collections})
    _dump_json(payload, args.output)
    return 0


def cmd_list_dbs(args):
    shard_options = {}
    if args.sqlite_shards is not None:
        shard_options["sqlite_shards"] = args.sqlite_shards
    for name in _db_names(
        args.path,
        args.backend,
        storage_uri=args.storage_uri,
        dsn=args.dsn,
        **shard_options,
    ):
        print(name)
    return 0


def cmd_list_collections(args):
    with _managed_client(
        args.path,
        args.backend,
        storage_uri=args.storage_uri,
        dsn=args.dsn,
        sqlite_shards=args.sqlite_shards,
    ) as client:
        for name in sorted(_collection_names(client[args.database])):
            print(name)
    return 0


def cmd_export(args):
    with _managed_client(
        args.path,
        args.backend,
        storage_uri=args.storage_uri,
        dsn=args.dsn,
        sqlite_shards=args.sqlite_shards,
    ) as client:
        docs = list(client[args.database][args.collection].find({}))
    # Embedded-document field order is part of BSON equality and can also be
    # part of a document-valued _id. Never recursively sort exported data.
    _dump_json(docs, args.output, sort_keys=False)
    return 0


def cmd_import(args):
    docs = _load_json(args.input)
    if not isinstance(docs, list):
        raise SystemExit("import input must be a JSON array of documents")
    for doc in docs:
        if not isinstance(doc, dict):
            raise SystemExit("import input must contain only JSON objects")

    with _managed_client(
        args.path,
        args.backend,
        storage_uri=args.storage_uri,
        dsn=args.dsn,
        sqlite_shards=args.sqlite_shards,
    ) as client:
        database = client[args.database]
        collection = database[args.collection]
        if args.mode == "replace":
            _replace_collection(database, args.collection, docs)
        elif docs:
            collection.insert_many(docs)
    print("imported {0} documents".format(len(docs)))
    return 0


def cmd_migrate(args):
    source_uri = _effective_storage_uri(args.from_backend, args.source_uri)
    target_uri = _effective_storage_uri(args.to_backend, args.target_uri)
    source_dsn = _effective_dsn(args.from_backend, args.source_dsn)
    target_dsn = _effective_dsn(args.to_backend, args.target_dsn)
    source_sqlite_shards = getattr(args, "source_sqlite_shards", None)
    target_sqlite_shards = getattr(args, "target_sqlite_shards", None)
    migrated = []
    with (
        _managed_client(
            args.source,
            args.from_backend,
            storage_uri=source_uri,
            dsn=source_dsn,
            sqlite_shards=source_sqlite_shards,
        ) as source_client,
        _managed_client(
            args.target,
            args.to_backend,
            storage_uri=target_uri,
            dsn=target_dsn,
            sqlite_shards=target_sqlite_shards,
        ) as target_client,
    ):
        if args.database:
            database_names = [args.database]
        else:
            list_database_names = getattr(
                source_client,
                "list_database_names",
                None,
            )
            if callable(list_database_names):
                database_names = list_database_names()
            else:  # pragma: no cover - compatibility for injected legacy clients
                source_shard_options = {}
                if source_sqlite_shards is not None:
                    source_shard_options["sqlite_shards"] = source_sqlite_shards
                database_names = _db_names(
                    args.source,
                    args.from_backend,
                    storage_uri=source_uri,
                    dsn=source_dsn,
                    **source_shard_options,
                )
        for db_name in database_names:
            source_db = source_client[db_name]
            target_db = target_client[db_name]
            for collection_name in sorted(_collection_names(source_db)):
                docs = list(source_db[collection_name].find({}))
                _replace_collection(target_db, collection_name, docs)
                migrated.append(
                    {
                        "database": db_name,
                        "collection": collection_name,
                        "count": len(docs),
                    }
                )

    _dump_json(
        {
            "source": args.source,
            "target": args.target,
            "from_backend": args.from_backend,
            "to_backend": args.to_backend,
            "source_uri": source_uri,
            "target_uri": target_uri,
            "source_dsn_configured": bool(source_dsn),
            "target_dsn_configured": bool(target_dsn),
            "migrated": migrated,
        },
        args.output,
    )
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="tinymongo")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backend_parser = argparse.ArgumentParser(add_help=False)
    backend_parser.add_argument(
        "--backend", default="tinydb", choices=SUPPORTED_BACKENDS
    )
    backend_parser.add_argument(
        "--storage-uri",
        help="object storage URI for parquet/parquetv2, for example s3://bucket/prefix",
    )
    backend_parser.add_argument(
        "--dsn",
        help="remote SQL DSN for postgres, postgresql, mysql, or mariadb backends",
    )
    backend_parser.add_argument(
        "--sqlite-shards",
        type=int,
        help="physical shard count when creating a sqlite-sharded database",
    )

    inspect = subparsers.add_parser(
        "inspect",
        parents=[backend_parser],
        help="print database and collection counts",
    )
    inspect.add_argument("path")
    inspect.add_argument("-o", "--output", default="-")
    inspect.set_defaults(func=cmd_inspect)

    list_dbs = subparsers.add_parser(
        "list-dbs", parents=[backend_parser], help="list database names"
    )
    list_dbs.add_argument("path")
    list_dbs.set_defaults(func=cmd_list_dbs)

    list_collections = subparsers.add_parser(
        "list-collections",
        parents=[backend_parser],
        help="list collections in a database",
    )
    list_collections.add_argument("path")
    list_collections.add_argument("database")
    list_collections.set_defaults(func=cmd_list_collections)

    export = subparsers.add_parser(
        "export", parents=[backend_parser], help="export a collection to JSON"
    )
    export.add_argument("path")
    export.add_argument("database")
    export.add_argument("collection")
    export.add_argument("-o", "--output", default="-")
    export.set_defaults(func=cmd_export)

    import_cmd = subparsers.add_parser(
        "import", parents=[backend_parser], help="import JSON documents"
    )
    import_cmd.add_argument("path")
    import_cmd.add_argument("database")
    import_cmd.add_argument("collection")
    import_cmd.add_argument("input")
    import_cmd.add_argument("--mode", choices=("append", "replace"), default="append")
    import_cmd.set_defaults(func=cmd_import)

    migrate = subparsers.add_parser("migrate", help="copy data between backends")
    migrate.add_argument("source")
    migrate.add_argument("target")
    migrate.add_argument("--from-backend", default="tinydb", choices=SUPPORTED_BACKENDS)
    migrate.add_argument("--to-backend", required=True, choices=SUPPORTED_BACKENDS)
    migrate.add_argument("--source-uri")
    migrate.add_argument("--target-uri")
    migrate.add_argument("--source-dsn")
    migrate.add_argument("--target-dsn")
    migrate.add_argument("--source-sqlite-shards", type=int)
    migrate.add_argument("--target-sqlite-shards", type=int)
    migrate.add_argument("--database")
    migrate.add_argument("-o", "--output", default="-")
    migrate.set_defaults(func=cmd_migrate)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
