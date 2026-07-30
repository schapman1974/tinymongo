"""Mongo-compatible ``$unset`` behavior across embedded backends."""

import asyncio
from uuid import uuid4

import pytest

from tinymongo import TinyMongoClient
from tinymongo.asyncio import AsyncTinyMongoClient


BACKENDS = ("memory", "tinydb", "sqlite", "duckdb", "parquet")


def _location(tmp_path, backend, prefix):
    if backend in ("duckdb", "parquet"):
        pytest.importorskip("duckdb")
    if backend == "memory":
        return "memory://{0}-{1}".format(prefix, uuid4().hex)
    return str(tmp_path / "{0}-{1}".format(prefix, backend))


def _seed_documents():
    return [
        {
            "_id": 1,
            "group": "many",
            "obsolete": "remove",
            "nested": {"obsolete": True, "keep": 1},
            "keep": "one",
        },
        {
            "_id": 2,
            "group": "many",
            "obsolete": "remove",
            "nested": {"keep": 2},
            "keep": "two",
        },
        {
            "_id": 3,
            "group": "many",
            "nested": {"keep": 3},
            "keep": "three",
        },
    ]


def _assert_post_images(documents):
    assert documents == [
        {
            "_id": 1,
            "group": "many",
            "nested": {"keep": 1},
            "keep": "one",
        },
        {
            "_id": 2,
            "group": "many",
            "nested": {"keep": 2},
            "keep": "two",
        },
        {
            "_id": 3,
            "group": "many",
            "nested": {"keep": 3},
            "keep": "three",
        },
    ]


@pytest.mark.parametrize("backend", BACKENDS)
def test_unset_removes_top_level_and_nested_fields_on_embedded_backends(
    tmp_path, backend
):
    client = TinyMongoClient(
        _location(tmp_path, backend, "sync-unset"),
        backend=backend,
    )
    collection = client.app.items
    collection.insert_many(_seed_documents())

    one = collection.update_one(
        {"_id": 1},
        {
            "$unset": {
                "obsolete": "",
                "nested.obsolete": "",
                "missing": "",
                "nested.missing": "",
            }
        },
    )
    assert one.matched_count == 1
    assert one.modified_count == 1

    one_noop = collection.update_one(
        {"_id": 1},
        {"$unset": {"obsolete": "", "nested.obsolete": "", "missing": ""}},
    )
    assert one_noop.matched_count == 1
    assert one_noop.modified_count == 0

    many = collection.update_many(
        {"group": "many"},
        {
            "$unset": {
                "obsolete": "",
                "nested.obsolete": "",
                "missing": "",
                "nested.missing": "",
            }
        },
    )
    assert many.matched_count == 3
    assert many.modified_count == 1
    _assert_post_images(list(collection.find({}).sort("_id")))

    many_noop = collection.update_many(
        {"group": "many"},
        {"$unset": {"obsolete": "", "nested.obsolete": "", "missing": ""}},
    )
    assert many_noop.matched_count == 3
    assert many_noop.modified_count == 0
    client.close()


@pytest.mark.parametrize("backend", BACKENDS)
def test_async_unset_has_the_same_embedded_backend_semantics(tmp_path, backend):
    async def scenario():
        client = AsyncTinyMongoClient(
            _location(tmp_path, backend, "async-unset"),
            backend=backend,
        )
        collection = client.app.items
        await collection.insert_many(_seed_documents())

        result = await collection.update_many(
            {"group": "many"},
            {
                "$unset": {
                    "obsolete": "",
                    "nested.obsolete": "",
                    "missing": "",
                    "nested.missing": "",
                }
            },
        )
        assert result.matched_count == 3
        assert result.modified_count == 2
        _assert_post_images(await collection.find({}).sort("_id").to_list())

        noop = await collection.update_many(
            {"group": "many"},
            {"$unset": {"obsolete": "", "nested.obsolete": "", "missing": ""}},
        )
        assert noop.matched_count == 3
        assert noop.modified_count == 0
        await client.close()

    asyncio.run(scenario())
