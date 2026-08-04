import asyncio

import pytest

import tinymongo
from tinymongo import tinymongo as core
from tinymongo.asyncio import AsyncTinyMongoClient
from tinymongo.errors import InvalidOperation, TinyMongoNotSupportedError


EXPECTED_BUILD_INFO = {
    "version": "8.0.0",
    "versionArray": [8, 0, 0, 0],
    "ok": 1.0,
    "tinymongo": True,
}


def test_database_supports_discovery_commands_used_by_odms():
    client = tinymongo.TinyMongoClient(backend="memory")
    database = client.app
    database.items.insert_one({"_id": 1})

    assert database.command({"buildInfo": 1}) == EXPECTED_BUILD_INFO
    assert database.command("buildinfo") == EXPECTED_BUILD_INFO
    assert database.command("ping") == {"ok": 1.0}
    assert database.command({"ping": 1}) == {"ok": 1.0}
    assert database.list_collection_names(
        authorizedCollections=True,
        nameOnly=True,
    ) == ["items"]
    assert database.list_collection_names(None) == ["items"]

    detached = object.__new__(core.TinyMongoDatabase)
    detached._client = None
    assert detached.command("ping") == {"ok": 1.0}

    with pytest.raises(TinyMongoNotSupportedError, match="serverStatus"):
        database.command("serverStatus")
    with pytest.raises(TinyMongoNotSupportedError, match="Sessions"):
        database.command("ping", session=object())
    with pytest.raises(TypeError, match="must not be empty"):
        database.command({})
    with pytest.raises(TypeError, match="string or mapping"):
        database.command([])
    with pytest.raises(TypeError, match="name must be a string"):
        database.command({1: 1})
    with pytest.raises(TypeError):
        database.list_collection_names(None, None, None, None)
    with pytest.raises(TypeError, match="multiple values"):
        database.list_collection_names(None, session=None)
    with pytest.raises(TypeError, match="unexpected keyword"):
        database.list_collection_names(unknown=True)
    with pytest.raises(TinyMongoNotSupportedError, match="Filtered"):
        database.list_collection_names(filter={"name": "items"})
    with pytest.raises(TinyMongoNotSupportedError, match="Sessions"):
        database.list_collection_names(session=object())

    client.close()
    with pytest.raises(InvalidOperation):
        database.command("ping")


def test_async_database_supports_discovery_commands_used_by_odms():
    async def scenario():
        client = AsyncTinyMongoClient(backend="memory")
        try:
            database = client.app
            await database.items.insert_one({"_id": 1})

            assert await database.command({"buildInfo": 1}) == EXPECTED_BUILD_INFO
            assert await database.command("ping") == {"ok": 1.0}
            assert await database.list_collection_names(
                authorizedCollections=True,
                nameOnly=True,
            ) == ["items"]

            with pytest.raises(TinyMongoNotSupportedError, match="serverStatus"):
                await database.command("serverStatus")
        finally:
            await client.close()

    asyncio.run(scenario())
