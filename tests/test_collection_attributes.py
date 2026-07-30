import asyncio

import pytest

import tinymongo as tm
from tinymongo.asyncio import AsyncTinyMongoClient


BACKENDS = ("memory", "tinydb", "sqlite", "duckdb", "parquet")


def _client_location(tmp_path, backend):
    if backend == "memory":
        return "memory://collection-attributes-{0}".format(tmp_path.name)
    return str(tmp_path / backend)


@pytest.mark.parametrize("backend", BACKENDS)
def test_collection_attributes_select_dotted_children_across_backends(
    tmp_path, backend
):
    if backend in ("duckdb", "parquet"):
        pytest.importorskip("duckdb")

    client = tm.TinyMongoClient(_client_location(tmp_path, backend), backend=backend)
    try:
        database = client.app
        parent = database.users
        child = parent.archive

        assert child is not parent
        assert child.name == "users.archive"
        assert child.full_name == "app.users.archive"
        assert parent["archive"].name == "users.archive"

        with pytest.raises(AttributeError, match="users\\._private"):
            parent._private
        with pytest.raises(AttributeError, match="_private"):
            database._private
        assert parent["_private"].name == "users._private"
        assert database["users._private"].name == "users._private"

        parent.insert_one({"_id": "parent"})
        child.insert_one({"_id": "child"})
        assert parent.find_one({})["_id"] == "parent"
        assert child.find_one({})["_id"] == "child"

        with pytest.raises(TypeError, match="find_oni.*no such method exists"):
            parent.find_oni({"_id": "parent"})
        with pytest.raises(TypeError, match="pingg.*TinyMongoDatabase"):
            database.pingg()
    finally:
        client.close()


@pytest.mark.parametrize("backend", BACKENDS)
def test_async_collection_attributes_select_dotted_children_across_backends(
    tmp_path, backend
):
    if backend in ("duckdb", "parquet"):
        pytest.importorskip("duckdb")

    async def scenario():
        client = AsyncTinyMongoClient(
            _client_location(tmp_path, "async-{0}".format(backend)),
            backend=backend,
        )
        try:
            database = client.app
            parent = database.users
            child = parent.archive

            assert child is not parent
            assert child.name == "users.archive"
            assert child.full_name == "app.users.archive"
            assert parent["archive"].name == "users.archive"

            with pytest.raises(AttributeError, match="users\\._private"):
                parent._private
            with pytest.raises(AttributeError, match="_private"):
                database._private
            assert parent["_private"].name == "users._private"
            assert database["users._private"].name == "users._private"

            await parent.insert_one({"_id": "parent"})
            await child.insert_one({"_id": "child"})
            assert (await parent.find_one({}))["_id"] == "parent"
            assert (await child.find_one({}))["_id"] == "child"

            with pytest.raises(TypeError, match="find_oni.*no such method exists"):
                parent.find_oni({"_id": "parent"})
            with pytest.raises(TypeError, match="pingg.*AsyncTinyMongoDatabase"):
                database.pingg()
        finally:
            await client.close()

    asyncio.run(scenario())
