import asyncio

import pytest

from tinymongo import AsyncMongoClient, AsyncTinyMongoClient, TinyMongoClient


def test_tiny_client_honors_tinymongo_folder_alias(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    configured = tmp_path / "configured"
    client = TinyMongoClient(tinymongo_folder=configured)
    try:
        client.app.items.insert_one({"_id": 1})
    finally:
        client.close()

    assert client._foldername == configured
    assert (configured / "app.json").is_file()
    assert not (tmp_path / "tinydb").exists()


def test_async_tiny_client_honors_tinymongo_folder_alias(tmp_path, monkeypatch):
    async def scenario():
        configured = tmp_path / "async-configured"
        client = AsyncTinyMongoClient(tinymongo_folder=configured)
        try:
            await client.app.items.insert_one({"_id": 1})
            assert await client.app.items.count_documents({}) == 1
        finally:
            await client.close()
        return configured

    monkeypatch.chdir(tmp_path)
    configured = asyncio.run(scenario())

    assert (configured / "app.json").is_file()
    assert not (tmp_path / "tinydb").exists()


def test_tiny_client_accepts_matching_folder_options(tmp_path):
    configured = tmp_path / "configured"
    client = TinyMongoClient(configured, tinymongo_folder=configured)
    try:
        assert client._foldername == configured
    finally:
        client.close()


def test_async_mongo_client_accepts_default_configuration():
    client = AsyncMongoClient()

    asyncio.run(client.close())


@pytest.mark.parametrize("client_class", [TinyMongoClient, AsyncTinyMongoClient])
def test_tiny_clients_reject_unexpected_options(client_class):
    with pytest.raises(
        TypeError, match="unexpected keyword argument 'tinymongo_fodler'"
    ):
        client_class(tinymongo_fodler="misspelled")


@pytest.mark.parametrize("client_class", [TinyMongoClient, AsyncTinyMongoClient])
def test_tiny_clients_reject_conflicting_folder_options(tmp_path, client_class):
    with pytest.raises(TypeError, match="different values for foldername"):
        client_class(
            tmp_path / "foldername",
            tinymongo_folder=tmp_path / "alias",
        )


def test_tiny_client_keeps_supported_backend_options(tmp_path):
    client = TinyMongoClient(
        tmp_path / "configured",
        threads=2,
        storage_uri="s3://ignored-for-tinydb",
        duckdb_config={"memory_limit": "1GB"},
        dsn="postgresql://ignored-for-tinydb",
    )
    try:
        assert client._threads == 2
        assert client._duckdb_config == {"memory_limit": "1GB"}
    finally:
        client.close()
