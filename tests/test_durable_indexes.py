from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

import tinymongo
from tinymongo.errors import (
    DuplicateKeyError,
    OperationFailure,
    TinyMongoNotSupportedError,
)
from tinymongo.storage_backends import clear_memory_namespace


BACKENDS = [
    pytest.param("memory", id="memory"),
    pytest.param("tinydb", id="json"),
    pytest.param("sqlite", id="sqlite"),
    pytest.param("duckdb", id="duckdb"),
    pytest.param("parquet", id="parquet"),
]


class DurableIndexBackend:
    def __init__(self, backend, address):
        self.backend = backend
        self.address = address
        self.clients = []

    def open(self):
        client = tinymongo.TinyMongoClient(self.address, backend=self.backend)
        self.clients.append(client)
        return client

    def close(self, client):
        client.close()

    def close_all(self):
        for client in reversed(self.clients):
            client.close()
        if self.backend == "memory":
            clear_memory_namespace(self.address)


@pytest.fixture(params=BACKENDS)
def durable_index_backend(request, tmp_path):
    backend = request.param
    if backend in ("duckdb", "parquet"):
        pytest.importorskip("duckdb")
    if backend == "parquet":
        pytest.importorskip("pyarrow")

    if backend == "memory":
        address = "memory://durable-index-{0}".format(uuid4().hex)
    else:
        address = str(tmp_path / backend)

    target = DurableIndexBackend(backend, address)
    try:
        yield target
    finally:
        target.close_all()


def _indexes_by_name(collection):
    return {index["name"]: index for index in collection.list_indexes()}


def test_unique_index_and_enforcement_survive_client_restart(durable_index_backend):
    first = durable_index_backend.open()
    users = first.app.users
    assert users.create_index("email", name="login_email", unique=True) == "login_email"
    users.insert_many(
        [
            {"_id": 1, "email": "ada@example.com"},
            {"_id": 2, "email": "grace@example.com"},
        ]
    )
    durable_index_backend.close(first)

    reopened = durable_index_backend.open()
    users = reopened.app.users
    assert _indexes_by_name(users)["login_email"] == {
        "name": "login_email",
        "key": [("email", 1)],
        "unique": True,
    }

    with pytest.raises(DuplicateKeyError):
        users.insert_one({"_id": 3, "email": "ada@example.com"})
    with pytest.raises(DuplicateKeyError):
        users.update_one({"_id": 2}, {"$set": {"email": "ada@example.com"}})

    assert users.find_one({"_id": 2})["email"] == "grace@example.com"
    assert users.count_documents({}) == 2


def test_index_metadata_is_collection_scoped_and_drop_persists(
    durable_index_backend,
):
    first = durable_index_backend.open()
    users = first.app.users
    audit = first.app.audit
    users.create_index("email", name="login_email", unique=True)
    audit.create_index("created_at", name="created_at_lookup")

    assert set(_indexes_by_name(users)) == {"_id_", "login_email"}
    assert _indexes_by_name(users)["login_email"]["unique"] is True
    assert set(_indexes_by_name(audit)) == {"_id_", "created_at_lookup"}
    assert "unique" not in _indexes_by_name(audit)["created_at_lookup"]

    assert users.drop_index("login_email") is None
    durable_index_backend.close(first)

    reopened = durable_index_backend.open()
    users = reopened.app.users
    audit = reopened.app.audit
    assert users.list_indexes() == [{"name": "_id_", "key": [("_id", 1)]}]
    assert "created_at_lookup" in _indexes_by_name(audit)

    users.insert_many(
        [
            {"_id": 1, "email": "same@example.com"},
            {"_id": 2, "email": "same@example.com"},
        ]
    )
    assert audit.drop_index("created_at") is None
    with pytest.raises(OperationFailure, match="not found"):
        audit.drop_index("created_at")
    with pytest.raises(OperationFailure, match="cannot be dropped"):
        audit.drop_index("_id_")


def test_catalog_identity_cannot_collide_across_collection_and_index_names(
    durable_index_backend,
):
    client = durable_index_backend.open()
    first = client.app["a:b"]
    second = client.app["a"]

    first.create_index("value", name="c", unique=True)
    second.create_index("value", name="b:c", unique=True)
    assert set(_indexes_by_name(first)) == {"_id_", "c"}
    assert set(_indexes_by_name(second)) == {"_id_", "b:c"}

    first.drop_index("c")
    assert "b:c" in _indexes_by_name(second)


def test_drop_collection_removes_its_durable_index_catalog(durable_index_backend):
    client = durable_index_backend.open()
    users = client.app.users
    users.create_index("email", unique=True)
    users.insert_one({"_id": 1, "email": "same@example.com"})

    assert client.app.drop_collection("users") is True
    recreated = client.app.users
    assert recreated.list_indexes() == [{"name": "_id_", "key": [("_id", 1)]}]
    recreated.insert_many(
        [
            {"_id": 2, "email": "same@example.com"},
            {"_id": 3, "email": "same@example.com"},
        ]
    )


def test_unique_index_rejects_existing_duplicates_without_adding_metadata(
    durable_index_backend,
):
    client = durable_index_backend.open()
    users = client.app.users
    users.insert_many(
        [
            {"_id": 1, "email": "duplicate@example.com"},
            {"_id": 2, "email": "duplicate@example.com"},
        ]
    )

    with pytest.raises(DuplicateKeyError):
        users.create_index("email", name="login_email", unique=True)

    assert users.list_indexes() == [{"name": "_id_", "key": [("_id", 1)]}]


def test_unique_insert_operations_fail_atomically(durable_index_backend):
    client = durable_index_backend.open()
    users = client.app.users
    users.create_index("email", unique=True)
    users.insert_one({"_id": 1, "email": "ada@example.com"})

    with pytest.raises(DuplicateKeyError):
        users.insert_one({"_id": 2, "email": "ada@example.com"})
    with pytest.raises(DuplicateKeyError):
        users.insert_one(
            {"_id": 3, "email": "ada@example.com"},
            bypass_document_validation=True,
        )
    with pytest.raises(DuplicateKeyError):
        users.insert_one(
            {"_id": 1, "email": "replacement@example.com"},
            bypass_document_validation=True,
        )
    with pytest.raises(DuplicateKeyError):
        users.insert_many(
            [
                {"_id": 4, "email": "grace@example.com"},
                {"_id": 5, "email": "ada@example.com"},
            ]
        )
    with pytest.raises(DuplicateKeyError):
        users.insert_many(
            [
                {"_id": 6, "email": "hopper@example.com"},
                {"_id": 7, "email": "hopper@example.com"},
            ]
        )

    assert list(users.find({})) == [{"_id": 1, "email": "ada@example.com"}]


def test_unique_update_replace_and_upsert_fail_atomically(durable_index_backend):
    client = durable_index_backend.open()
    users = client.app.users
    users.create_index("email", unique=True)
    users.insert_many(
        [
            {"_id": 1, "email": "ada@example.com", "active": True},
            {"_id": 2, "email": "grace@example.com", "active": True},
        ]
    )

    with pytest.raises(DuplicateKeyError):
        users.update_one({"_id": 2}, {"$set": {"email": "ada@example.com"}})
    with pytest.raises(DuplicateKeyError):
        users.update_many({}, {"$set": {"email": "shared@example.com"}})
    with pytest.raises(DuplicateKeyError):
        users.replace_one(
            {"_id": 2},
            {"email": "ada@example.com", "active": False},
        )
    with pytest.raises(DuplicateKeyError):
        users.update_one(
            {"_id": 3},
            {"$set": {"email": "ada@example.com"}},
            upsert=True,
        )
    with pytest.raises(DuplicateKeyError):
        users.replace_one(
            {"_id": 4},
            {"_id": 4, "email": "ada@example.com"},
            upsert=True,
        )

    assert list(users.find({}).sort("_id", 1)) == [
        {"_id": 1, "email": "ada@example.com", "active": True},
        {"_id": 2, "email": "grace@example.com", "active": True},
    ]


def test_unique_array_semantics(durable_index_backend):
    client = durable_index_backend.open()
    values = client.app.values
    values.create_index("value", unique=True)

    values.insert_one({"_id": 1, "value": ["alpha", "alpha", "beta"]})
    values.insert_one({"_id": 2, "value": ["gamma"]})
    with pytest.raises(DuplicateKeyError):
        values.insert_one({"_id": 3, "value": ["beta", "delta"]})

    values.insert_one({"_id": 4, "value": []})
    with pytest.raises(DuplicateKeyError):
        values.insert_one({"_id": 5, "value": []})

    assert values.count_documents({}) == 3


def test_unique_null_and_missing_values_share_one_key(durable_index_backend):
    client = durable_index_backend.open()
    values = client.app.values
    values.create_index("value", unique=True)

    values.insert_one({"_id": 1})
    with pytest.raises(DuplicateKeyError):
        values.insert_one({"_id": 2, "value": None})
    with pytest.raises(DuplicateKeyError):
        values.insert_one({"_id": 3, "value": [None]})

    assert values.count_documents({}) == 1


def test_unique_scalar_types_keep_booleans_distinct_from_numbers(
    durable_index_backend,
):
    client = durable_index_backend.open()
    values = client.app.values
    values.create_index("value", unique=True)

    values.insert_one({"_id": 1, "value": 1})
    with pytest.raises(DuplicateKeyError):
        values.insert_one({"_id": 2, "value": 1.0})
    values.insert_one({"_id": 3, "value": True})

    assert values.count_documents({}) == 2


def test_unique_index_rejects_unsupported_value_shapes(durable_index_backend):
    client = durable_index_backend.open()
    values = client.app.values
    values.create_index("value", unique=True)

    with pytest.raises(TinyMongoNotSupportedError, match="Object values"):
        values.insert_one({"_id": 1, "value": {"nested": "object"}})
    with pytest.raises(TinyMongoNotSupportedError, match="Nested array"):
        values.insert_one({"_id": 2, "value": [["nested"]]})

    assert values.count_documents({}) == 0


def test_nested_unique_field_is_enforced(durable_index_backend):
    client = durable_index_backend.open()
    users = client.app.users
    users.create_index("profile.email", name="nested_email", unique=True)
    users.insert_one({"_id": 1, "profile": {"email": "ada@example.com"}})

    with pytest.raises(DuplicateKeyError):
        users.insert_one({"_id": 2, "profile": {"email": "ada@example.com"}})
    assert users.find_one({"profile.email": "ada@example.com"})["_id"] == 1


def test_unique_dotted_index_rejects_array_traversal(durable_index_backend):
    client = durable_index_backend.open()
    items = client.app.items
    items.create_index("parts.sku", unique=True)

    with pytest.raises(TinyMongoNotSupportedError, match="Array traversal"):
        items.insert_one({"_id": 1, "parts": [{"sku": "one"}]})
    assert items.count_documents({}) == 0


def test_index_creation_is_idempotent_and_rejects_conflicts(
    durable_index_backend,
):
    client = durable_index_backend.open()
    users = client.app.users
    assert (
        users.create_index([("email", 1)], name="login_email", unique=True)
        == "login_email"
    )
    assert (
        users.create_index([("email", 1)], name="login_email", unique=True)
        == "login_email"
    )

    assert users.create_index("email", name="other_name", unique=True) == "other_name"
    with pytest.raises(OperationFailure, match="different options"):
        users.create_index("username", name="login_email", unique=True)
    with pytest.raises(OperationFailure, match="different options"):
        users.create_index("email", name="login_email", unique=False)

    assert set(_indexes_by_name(users)) == {"_id_", "login_email", "other_name"}


def test_builtin_id_index_has_fixed_options(durable_index_backend):
    client = durable_index_backend.open()
    users = client.app.users

    assert users.create_index("_id") == "_id_"
    assert users.create_index("_id", name="custom_id") == "_id_"
    with pytest.raises(OperationFailure, match="not valid") as enabled:
        users.create_index("_id", unique=True)
    assert enabled.value.code == 197
    with pytest.raises(OperationFailure, match="not valid") as disabled:
        users.create_index("_id", unique=False)
    assert disabled.value.code == 197
    assert users.list_indexes() == [{"name": "_id_", "key": [("_id", 1)]}]


def test_unsupported_index_shapes_types_and_options_leave_no_metadata(
    durable_index_backend,
):
    client = durable_index_backend.open()
    users = client.app.users
    unsupported = [
        ([("first", 1), ("last", 1)], {}),
        ([("email", -1)], {}),
        (123, {}),
        (object(), {}),
        ("email", {"unique": 1}),
        ("email", {"name": ""}),
        ("email", {"sparse": True}),
        ("email", {"background": True}),
    ]

    for key, options in unsupported:
        with pytest.raises(TinyMongoNotSupportedError):
            users.create_index(key, **options)
    with pytest.raises(TinyMongoNotSupportedError):
        users.create_index("email", 1)

    assert users.list_indexes() == [{"name": "_id_", "key": [("_id", 1)]}]


def test_concurrent_unique_inserts_allow_exactly_one_writer(durable_index_backend):
    setup = durable_index_backend.open()
    setup.app.users.create_index("email", unique=True)
    durable_index_backend.close(setup)

    workers = 4
    barrier = threading.Barrier(workers)

    def insert(worker):
        client = tinymongo.TinyMongoClient(
            durable_index_backend.address,
            backend=durable_index_backend.backend,
        )
        try:
            barrier.wait(timeout=10)
            client.app.users.insert_one({"_id": worker, "email": "winner@example.com"})
            return "inserted"
        except DuplicateKeyError:
            return "duplicate"
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(insert, range(workers)))

    assert results.count("inserted") == 1
    assert results.count("duplicate") == workers - 1
    reader = durable_index_backend.open()
    assert reader.app.users.count_documents({}) == 1


def test_duckdb_unique_race_across_fresh_processes(tmp_path):
    pytest.importorskip("duckdb")
    address = str(tmp_path / "duckdb-process-race")
    setup = tinymongo.TinyMongoClient(address, backend="duckdb")
    setup.app.users.create_index("email", unique=True)
    setup.close()

    start = tmp_path / "start"
    script = """
import pathlib
import sys
import time

import tinymongo
from tinymongo.errors import DuplicateKeyError

address, ready, start, worker = sys.argv[1:]
pathlib.Path(ready).write_text("ready")
deadline = time.monotonic() + 30
while not pathlib.Path(start).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("timed out waiting for the race start signal")
    time.sleep(0.01)
client = tinymongo.TinyMongoClient(address, backend="duckdb")
try:
    client.app.users.insert_one(
        {"_id": int(worker), "email": "winner@example.com"}
    )
    print("inserted")
except DuplicateKeyError:
    print("duplicate")
finally:
    client.close()
"""
    processes = []
    ready_paths = []
    for worker in range(4):
        ready = tmp_path / "ready-{0}".format(worker)
        ready_paths.append(ready)
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    address,
                    str(ready),
                    str(start),
                    str(worker),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    try:
        deadline = time.monotonic() + 20
        while not all(path.exists() for path in ready_paths):
            for process, ready in zip(processes, ready_paths):
                if not ready.exists() and process.poll() is not None:
                    stdout, stderr = process.communicate()
                    pytest.fail(
                        "DuckDB race worker exited before ready: {0}{1}".format(
                            stdout, stderr
                        )
                    )
            if time.monotonic() >= deadline:
                pytest.fail("DuckDB race workers did not become ready")
            time.sleep(0.01)
        start.write_text("go")

        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            results.append(stdout.strip())
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    assert results.count("inserted") == 1
    assert results.count("duplicate") == 3
    reader = tinymongo.TinyMongoClient(address, backend="duckdb")
    try:
        assert reader.app.users.count_documents({}) == 1
    finally:
        reader.close()
