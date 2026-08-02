"""Factories that run one contract against every supported target."""

import asyncio
import inspect
import os
import time
from uuid import uuid4

import pytest
import tinymongo
from tinymongo.asyncio import AsyncMongoClient, AsyncTinyMongoClient

from .support import ContractTarget


APIS = ("sync", "async")
BACKENDS = ("memory", "json", "sqlite", "duckdb", "parquet", "mongodb")
TARGETS = [
    pytest.param(
        (api, backend),
        id="{0}-{1}".format(api, backend),
        marks=(
            (pytest.mark.integration, pytest.mark.mongodb)
            if backend == "mongodb"
            else ()
        ),
    )
    for api in APIS
    for backend in BACKENDS
]


class _AsyncRunner:
    """Run one async target on a stable event loop from synchronous contracts."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()

    def run(self, awaitable):
        return self.loop.run_until_complete(awaitable)

    def close(self):
        self.loop.close()


class _AsyncCursorAdapter:
    """Expose async cursor behavior to the transport-neutral contract body."""

    def __init__(self, cursor, runner):
        self._cursor = cursor
        self._runner = runner

    def sort(self, *args, **kwargs):
        self._cursor.sort(*args, **kwargs)
        return self

    def skip(self, *args, **kwargs):
        self._cursor.skip(*args, **kwargs)
        return self

    def limit(self, *args, **kwargs):
        self._cursor.limit(*args, **kwargs)
        return self

    def to_list(self, length=None):
        return self._runner.run(self._cursor.to_list(length=length))

    def __iter__(self):
        return iter(self.to_list())


class _AsyncCollectionAdapter:
    """Await collection calls while preserving immediate cursor construction."""

    def __init__(self, collection, runner):
        self._collection = collection
        self._runner = runner

    def find(self, *args, **kwargs):
        return _AsyncCursorAdapter(self._collection.find(*args, **kwargs), self._runner)

    def aggregate(self, *args, **kwargs):
        cursor = self._collection.aggregate(*args, **kwargs)
        if inspect.isawaitable(cursor):
            cursor = self._runner.run(cursor)
        return _AsyncCursorAdapter(cursor, self._runner)

    def __getattr__(self, name):
        attribute = getattr(self._collection, name)
        if not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            result = attribute(*args, **kwargs)
            if inspect.isawaitable(result):
                return self._runner.run(result)
            return result

        return call


def _mongo_client(uri, client_options=None):
    try:
        from pymongo import MongoClient
    except ImportError:  # pragma: no cover - guarded by dev requirements
        pytest.fail("Real MongoDB contracts require PyMongo; run `pip install pymongo`")

    client = MongoClient(
        uri,
        serverSelectionTimeoutMS=1000,
        **dict(client_options or {}),
    )
    deadline = time.monotonic() + 15
    last_error = None
    while time.monotonic() < deadline:
        try:
            client.admin.command("ping")
            return client
        except Exception as error:  # noqa: BLE001 - retry server startup
            last_error = error
            time.sleep(0.25)
    client.close()
    raise RuntimeError("MongoDB did not become ready: {0}".format(last_error))


def _async_mongo_client(uri, runner, client_options=None):
    try:
        from pymongo import AsyncMongoClient
    except ImportError:  # pragma: no cover - guarded by dev requirements
        pytest.fail("Real MongoDB contracts require PyMongo; run `pip install pymongo`")

    client = AsyncMongoClient(
        uri,
        serverSelectionTimeoutMS=1000,
        **dict(client_options or {}),
    )
    deadline = time.monotonic() + 15
    last_error = None
    while time.monotonic() < deadline:
        try:
            runner.run(client.admin.command("ping"))
            return client
        except Exception as error:  # noqa: BLE001 - retry server startup
            last_error = error
            time.sleep(0.25)
    runner.run(client.close())
    raise RuntimeError("MongoDB did not become ready: {0}".format(last_error))


def _contract_suite(request):
    if "test_talkpython_contract.py" in request.node.nodeid:
        return "talkpython"
    if "test_bson_comparison_contract.py" in request.node.nodeid:
        return "bson-comparison"
    if "test_client_read_fidelity_contract.py" in request.node.nodeid:
        return "client-read-fidelity"
    return "core"


@pytest.fixture(params=TARGETS)
def contract_target(request, tmp_path, monkeypatch):
    """Yield an isolated collection for a TinyMongo backend or real MongoDB."""

    api, target_name = request.param
    options_marker = request.node.get_closest_marker("client_options")
    client_options = dict(options_marker.kwargs) if options_marker else {}
    database_name = "tinymongo_contract_{0}".format(uuid4().hex)
    request.node.user_properties.extend(
        [
            ("tinymongo.api", api),
            ("tinymongo.backend", target_name),
            ("tinymongo.suite", _contract_suite(request)),
        ]
    )

    if target_name == "mongodb":
        uri = os.environ.get("TINYMONGO_MONGODB_URI")
        required = os.environ.get("TINYMONGO_REQUIRE_MONGODB") == "1"
        if not uri:
            message = "Set TINYMONGO_MONGODB_URI to run real MongoDB contracts"
            if required:
                pytest.fail(message)
            pytest.skip(message)
        runner = None
        try:
            if api == "async":
                runner = _AsyncRunner()
                client = _async_mongo_client(uri, runner, client_options)
            else:
                client = _mongo_client(uri, client_options)
        except RuntimeError as error:
            if runner is not None:
                runner.close()
            if required:
                pytest.fail(str(error))
            pytest.skip(str(error))
        database = client[database_name]
        collection = database["items"]
        if runner is not None:
            collection = _AsyncCollectionAdapter(collection, runner)
        target = ContractTarget(
            name=target_name,
            api=api,
            client=client,
            database=database,
            collection=collection,
        )
        try:
            yield target
        finally:
            if runner is None:
                client.drop_database(database_name)
                client.close()
            else:
                runner.run(client.drop_database(database_name))
                runner.run(client.close())
                runner.close()
        return

    backend = "tinydb" if target_name == "json" else target_name
    if backend in ("duckdb", "parquet"):
        pytest.importorskip("duckdb")
    if backend == "parquet":
        pytest.importorskip("pyarrow")

    if backend == "memory":
        working_directory = tmp_path / "memory-cwd"
        working_directory.mkdir()
        monkeypatch.chdir(working_directory)
        arguments = ()
        options = {"backend": backend}
    else:
        arguments = (str(tmp_path / target_name),)
        options = {"backend": backend}
    runner = None
    if api == "async":
        runner = _AsyncRunner()
        client_class = AsyncMongoClient if client_options else AsyncTinyMongoClient
        client = client_class(*arguments, **options, **client_options)
    else:
        client_class = (
            tinymongo.MongoClient if client_options else tinymongo.TinyMongoClient
        )
        client = client_class(*arguments, **options, **client_options)
    database = client[database_name]
    collection = database["items"]
    if runner is not None:
        collection = _AsyncCollectionAdapter(collection, runner)
    try:
        yield ContractTarget(
            name=target_name,
            api=api,
            client=client,
            database=database,
            collection=collection,
        )
    finally:
        if runner is None:
            client.close()
        else:
            runner.run(client.close())
            runner.close()
