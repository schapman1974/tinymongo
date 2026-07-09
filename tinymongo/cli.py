"""Command line tools for inspecting and moving TinyMongo data."""

from __future__ import absolute_import

import argparse
import json
import os
import sys

from .storage_backends import storage_extension
from .tinymongo import TinyMongoClient


SUPPORTED_BACKENDS = ("tinydb", "json", "parquet", "parquetv2", "sqlite", "duckdb")


def _client(path, backend, storage_uri=None):
    return TinyMongoClient(path, backend=backend, storage_uri=storage_uri)


def _db_names(path, backend, storage_uri=None):
    if storage_uri:
        return _client(path, backend, storage_uri=storage_uri).list_database_names()
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
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _dump_json(data, path):
    if path == "-":
        json.dump(data, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")


def cmd_inspect(args):
    client = _client(args.path, args.backend, storage_uri=args.storage_uri)
    payload = {"path": args.path, "backend": args.backend, "databases": []}
    if args.storage_uri:
        payload["storage_uri"] = args.storage_uri
    for db_name in _db_names(args.path, args.backend, storage_uri=args.storage_uri):
        db = client[db_name]
        collections = []
        for collection_name in sorted(db.collection_names()):
            count = db[collection_name].count()
            collections.append({"name": collection_name, "count": count})
        payload["databases"].append({"name": db_name, "collections": collections})
    _dump_json(payload, args.output)
    return 0


def cmd_list_dbs(args):
    for name in _db_names(args.path, args.backend, storage_uri=args.storage_uri):
        print(name)
    return 0


def cmd_list_collections(args):
    client = _client(args.path, args.backend, storage_uri=args.storage_uri)
    for name in sorted(client[args.database].collection_names()):
        print(name)
    return 0


def cmd_export(args):
    client = _client(args.path, args.backend, storage_uri=args.storage_uri)
    docs = list(client[args.database][args.collection].find({}))
    _dump_json(docs, args.output)
    return 0


def cmd_import(args):
    docs = _load_json(args.input)
    if not isinstance(docs, list):
        raise SystemExit("import input must be a JSON array of documents")
    for doc in docs:
        if not isinstance(doc, dict):
            raise SystemExit("import input must contain only JSON objects")

    client = _client(args.path, args.backend, storage_uri=args.storage_uri)
    collection = client[args.database][args.collection]
    if args.mode == "replace":
        collection.delete_many({})
    if docs:
        collection.insert_many(docs)
    print("imported {0} documents".format(len(docs)))
    return 0


def cmd_migrate(args):
    source_client = _client(args.source, args.from_backend, storage_uri=args.source_uri)
    target_client = _client(args.target, args.to_backend, storage_uri=args.target_uri)

    database_names = [args.database] if args.database else _db_names(
        args.source, args.from_backend, storage_uri=args.source_uri
    )
    migrated = []
    for db_name in database_names:
        source_db = source_client[db_name]
        target_db = target_client[db_name]
        for collection_name in sorted(source_db.collection_names()):
            docs = list(source_db[collection_name].find({}))
            target_collection = target_db[collection_name]
            target_collection.delete_many({})
            if docs:
                target_collection.insert_many(docs)
            migrated.append(
                {"database": db_name, "collection": collection_name, "count": len(docs)}
            )

    _dump_json(
        {
            "source": args.source,
            "target": args.target,
            "from_backend": args.from_backend,
            "to_backend": args.to_backend,
            "source_uri": args.source_uri,
            "target_uri": args.target_uri,
            "migrated": migrated,
        },
        args.output,
    )
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="tinymongo")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backend_parser = argparse.ArgumentParser(add_help=False)
    backend_parser.add_argument("--backend", default="tinydb", choices=SUPPORTED_BACKENDS)
    backend_parser.add_argument(
        "--storage-uri",
        help="object storage URI for parquet/parquetv2, for example s3://bucket/prefix",
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
