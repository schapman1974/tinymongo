"""Exercise an installed TinyMongo wheel outside the source checkout."""

from __future__ import absolute_import

import argparse
import asyncio
import importlib
import importlib.metadata
import platform
import tempfile
from pathlib import Path

import tinymongo as tm


def _exercise_sync(root, backend):
    address = root / backend
    documents = [
        {"_id": index, "group": "g{0}".format(index % 3), "i": index}
        for index in range(12)
    ]
    query = {
        "$and": [
            {"group": {"$in": ["g1"]}},
            {"i": {"$gte": 3, "$lt": 10}},
            {"i": {"$mod": [2, 1]}},
        ]
    }

    with tm.TinyMongoClient(address, backend=backend) as client:
        collection = client.platform.records
        collection.insert_many(documents)
        collection.create_index("group")
        assert [row["_id"] for row in collection.find(query)] == [7]

    with tm.TinyMongoClient(address, backend=backend) as client:
        collection = client.platform.records
        assert collection.count_documents({}) == len(documents)
        assert collection.find_one({"_id": 7})["group"] == "g1"


async def _exercise_async(root, backend):
    async with tm.AsyncTinyMongoClient(
        root / "async {0}".format(backend),
        backend=backend,
    ) as client:
        collection = client.platform.records
        await collection.insert_many(
            [
                {"_id": "first", "value": 1},
                {"_id": "second", "value": 2},
            ]
        )
        result = await collection.update_one(
            {"_id": "second"},
            {"$inc": {"value": 1}},
        )
        assert result.matched_count == 1
        assert await collection.find_one({"_id": "second"}) == {
            "_id": "second",
            "value": 3,
        }


def _verify_import(require_installed_wheel, extras):
    package_path = Path(tm.__file__).resolve()
    source_package = Path(__file__).resolve().parents[1] / "tinymongo"
    if require_installed_wheel and source_package in package_path.parents:
        raise AssertionError(
            "TinyMongo was imported from the checkout instead of the installed wheel"
        )

    modules = {"portalocker"}
    extra_modules = {
        "bson": {"bson"},
        "duckdb": {"duckdb"},
        "parquet": {"duckdb", "pyarrow"},
        "remote-sql": {"psycopg", "pymysql"},
        "serialization": {"tinydb_serialization"},
    }
    for extra in extras:
        modules.update(extra_modules.get(extra, ()))
    for module_name in sorted(modules):
        importlib.import_module(module_name)

    return package_path


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-installed-wheel", action="store_true")
    parser.add_argument(
        "--extras",
        default="",
        help="comma-separated installed extras whose imports should be checked",
    )
    args = parser.parse_args(argv)

    extras = {item.strip() for item in args.extras.split(",") if item.strip()}
    package_path = _verify_import(args.require_installed_wheel, extras)
    with tempfile.TemporaryDirectory(prefix="tinymongo platform Ω ") as temp_dir:
        root = Path(temp_dir)
        _exercise_sync(root, "tinydb")
        _exercise_sync(root, "sqlite")
        _exercise_sync(root, "sqlite-sharded")
        if "duckdb" in extras:
            _exercise_sync(root, "duckdb")
        if "parquet" in extras:
            _exercise_sync(root, "parquet")
        asyncio.run(_exercise_async(root, "sqlite"))
        asyncio.run(_exercise_async(root, "sqlite-sharded"))

    print(
        "TinyMongo {0} passed on Python {1} ({2}, {3}); imported from {4}".format(
            importlib.metadata.version("tinymongo"),
            platform.python_version(),
            platform.system(),
            platform.machine(),
            package_path,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
