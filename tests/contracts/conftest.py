"""Factories that run one contract against every supported target."""

import os
import time
from uuid import uuid4

import pytest
import tinymongo

from .support import ContractTarget


TARGETS = [
    pytest.param("json", id="json"),
    pytest.param("sqlite", id="sqlite"),
    pytest.param("duckdb", id="duckdb"),
    pytest.param("parquet", id="parquet"),
    pytest.param(
        "mongodb",
        id="mongodb",
        marks=(pytest.mark.integration, pytest.mark.mongodb),
    ),
]


def _mongo_client(uri):
    try:
        from pymongo import MongoClient
    except ImportError:  # pragma: no cover - guarded by dev requirements
        pytest.fail("Real MongoDB contracts require PyMongo; run `pip install pymongo`")

    client = MongoClient(uri, serverSelectionTimeoutMS=1000)
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


@pytest.fixture(params=TARGETS)
def contract_target(request, tmp_path):
    """Yield an isolated collection for a TinyMongo backend or real MongoDB."""

    target_name = request.param
    database_name = "tinymongo_contract_{0}".format(uuid4().hex)

    if target_name == "mongodb":
        uri = os.environ.get("TINYMONGO_MONGODB_URI")
        required = os.environ.get("TINYMONGO_REQUIRE_MONGODB") == "1"
        if not uri:
            message = "Set TINYMONGO_MONGODB_URI to run real MongoDB contracts"
            if required:
                pytest.fail(message)
            pytest.skip(message)
        try:
            client = _mongo_client(uri)
        except RuntimeError as error:
            if required:
                pytest.fail(str(error))
            pytest.skip(str(error))
        database = client[database_name]
        target = ContractTarget(
            name=target_name,
            client=client,
            database=database,
            collection=database["items"],
        )
        try:
            yield target
        finally:
            client.drop_database(database_name)
            client.close()
        return

    backend = "tinydb" if target_name == "json" else target_name
    if backend in ("duckdb", "parquet"):
        pytest.importorskip("duckdb")
    if backend == "parquet":
        pytest.importorskip("pyarrow")

    client = tinymongo.TinyMongoClient(
        str(tmp_path / target_name),
        backend=backend,
    )
    database = client[database_name]
    try:
        yield ContractTarget(
            name=target_name,
            client=client,
            database=database,
            collection=database["items"],
        )
    finally:
        client.close()
