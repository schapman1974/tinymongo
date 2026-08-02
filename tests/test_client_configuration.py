import asyncio
from datetime import timezone
from types import SimpleNamespace

import pytest
import tinymongo.tinymongo as core

from tinymongo import (
    AsyncMongoClient,
    AsyncTinyMongoClient,
    MongoClient,
    TinyMongoClient,
)
from tinymongo.errors import ConfigurationError


def test_connection_option_catalog_tracks_installed_pymongo_or_fallback():
    try:
        from pymongo.common import VALIDATORS
    except ImportError:
        expected = core._PYMONGO_CONNECTION_OPTIONS_FALLBACK
    else:
        expected = frozenset(name.lower() for name in VALIDATORS)

    assert core._PYMONGO_CONNECTION_OPTIONS == expected


def test_connection_option_catalog_uses_dynamic_validator_names(monkeypatch):
    module = SimpleNamespace(VALIDATORS={"FutureOption": object()})
    monkeypatch.setattr(core.importlib, "import_module", lambda name: module)

    assert core._load_pymongo_connection_options() == frozenset({"futureoption"})


def test_connection_option_catalog_falls_back_when_pymongo_is_missing(monkeypatch):
    def missing(_name):
        raise ImportError("pymongo is optional")

    monkeypatch.setattr(core.importlib, "import_module", missing)

    assert (
        core._load_pymongo_connection_options()
        == core._PYMONGO_CONNECTION_OPTIONS_FALLBACK
    )


@pytest.mark.parametrize(
    "validators",
    [None, {}, [], {1: object()}],
)
def test_connection_option_catalog_rejects_malformed_validator_tables(
    monkeypatch,
    validators,
):
    module = SimpleNamespace(VALIDATORS=validators)
    monkeypatch.setattr(core.importlib, "import_module", lambda name: module)

    assert (
        core._load_pymongo_connection_options()
        == core._PYMONGO_CONNECTION_OPTIONS_FALLBACK
    )


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


@pytest.mark.parametrize("client_class", [MongoClient, AsyncMongoClient])
def test_pymongo_shaped_clients_validate_document_class_immediately(
    tmp_path,
    client_class,
):
    configured = tmp_path / "invalid-document-class"

    with pytest.raises(TypeError, match="document_class must be a subclass"):
        client_class(tinymongo_folder=configured, document_class=list)

    assert not configured.exists()


@pytest.mark.parametrize("client_class", [MongoClient, AsyncMongoClient])
@pytest.mark.parametrize("document_class", [None, False, 0, "", [], {}])
def test_pymongo_shaped_clients_treat_falsey_document_classes_as_default(
    tmp_path,
    client_class,
    document_class,
):
    client = client_class(
        tinymongo_folder=tmp_path / "default-document-class",
        document_class=document_class,
    )

    if client_class is AsyncMongoClient:
        asyncio.run(client.close())
    else:
        client.close()


@pytest.mark.parametrize("client_class", [MongoClient, AsyncMongoClient])
@pytest.mark.parametrize(
    ("options", "error", "message"),
    [
        ({"tz_aware": 1}, TypeError, "tz_aware must be True or False"),
        ({"tz_aware": "yes"}, ValueError, "must be 'true' or 'false'"),
        ({"tz_aware": "TRUE"}, ValueError, "must be 'true' or 'false'"),
        ({"tzinfo": "UTC"}, TypeError, "datetime.tzinfo"),
        (
            {"tzinfo": timezone.utc},
            ValueError,
            "cannot specify tzinfo without also setting tz_aware=True",
        ),
    ],
)
def test_pymongo_shaped_clients_validate_datetime_options_immediately(
    tmp_path,
    client_class,
    options,
    error,
    message,
):
    configured = tmp_path / "invalid-datetime-option"

    with pytest.raises(error, match=message):
        client_class(tinymongo_folder=configured, **options)

    assert not configured.exists()


@pytest.mark.parametrize("client_class", [MongoClient, AsyncMongoClient])
@pytest.mark.parametrize("tz_aware", [None, False, True, "false", "true"])
def test_pymongo_shaped_clients_accept_datetime_option_forms(
    tmp_path,
    client_class,
    tz_aware,
):
    options = {"tz_aware": tz_aware}
    if tz_aware in (True, "true"):
        options["tzInfo"] = timezone.utc
    client = client_class(
        tinymongo_folder=tmp_path / "configured",
        **options,
    )

    if client_class is AsyncMongoClient:
        asyncio.run(client.close())
    else:
        client.close()


@pytest.mark.parametrize("client_class", [TinyMongoClient, AsyncTinyMongoClient])
def test_tiny_clients_reject_unexpected_options(client_class):
    with pytest.raises(
        TypeError, match="unexpected keyword argument 'tinymongo_fodler'"
    ):
        client_class(tinymongo_fodler="misspelled")


@pytest.mark.parametrize("client_class", [MongoClient, AsyncMongoClient])
def test_pymongo_shaped_clients_reject_unknown_options_immediately(
    tmp_path,
    monkeypatch,
    client_class,
):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigurationError, match="Unknown option: tinymongo_fodler"):
        client_class(tinymongo_fodler=str(tmp_path / "configured"))

    assert not (tmp_path / "configured").exists()
    assert not (tmp_path / "tinydb").exists()


@pytest.mark.parametrize("client_class", [MongoClient, AsyncMongoClient])
def test_pymongo_shaped_clients_keep_recognized_connection_options(
    tmp_path,
    client_class,
):
    client = client_class(
        "mongodb://localhost:27017",
        tinymongo_folder=str(tmp_path / "configured"),
        serverSelectionTimeoutMS=50,
        connectTimeoutMS=50,
        retryWrites=False,
        appName="tinymongo-tests",
        event_listeners=[],
        server_selector=lambda servers: servers,
        server_api=None,
    )

    if client_class is AsyncMongoClient:
        asyncio.run(client.close())
    else:
        client.close()


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
