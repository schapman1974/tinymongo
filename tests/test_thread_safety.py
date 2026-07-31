from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
import tinymongo as tm
from tinymongo import tinymongo as implementation


def test_concurrent_first_database_access_reuses_one_cached_handle(
    tmp_path, monkeypatch
):
    original_database = implementation.TinyMongoDatabase
    constructor_calls = 0
    constructor_lock = threading.Lock()
    workers = 8
    start = threading.Barrier(workers)

    def slow_database(*args, **kwargs):
        nonlocal constructor_calls
        with constructor_lock:
            constructor_calls += 1
        time.sleep(0.05)
        return original_database(*args, **kwargs)

    monkeypatch.setattr(implementation, "TinyMongoDatabase", slow_database)
    client = tm.TinyMongoClient(str(tmp_path))

    def select_database(_worker):
        start.wait()
        return client.app

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            databases = list(executor.map(select_database, range(workers)))

        assert constructor_calls == 1
        assert len({id(database) for database in databases}) == 1
        assert client._databases == {"app": databases[0]}
    finally:
        client.close()


def test_compatibility_concern_documents_are_not_shared(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path))
    try:
        collection = client.app.items
        first = collection.write_concern
        second = collection.write_concern

        first.document["w"] = "majority"

        assert second.document == {}
        assert collection.read_concern.document == {}
    finally:
        client.close()


@pytest.mark.parametrize("operation", ["update_one", "update_many", "replace_one"])
def test_shared_collection_builds_its_table_inside_the_write_lock(
    tmp_path, monkeypatch, operation
):
    client = tm.TinyMongoClient(str(tmp_path))
    client.app.items.insert_one({"_id": 1, "count": 0})
    collection = client.app.items
    original_build_table = collection.build_table
    build_calls = 0
    build_calls_lock = threading.Lock()
    workers = 2
    start = threading.Barrier(workers)

    def slow_build_table():
        nonlocal build_calls
        with build_calls_lock:
            build_calls += 1
        time.sleep(0.05)
        original_build_table()

    monkeypatch.setattr(collection, "build_table", slow_build_table)

    def write(worker):
        start.wait()
        if operation == "replace_one":
            return collection.replace_one(
                {"_id": 1},
                {"_id": 1, "count": worker},
            )
        return getattr(collection, operation)(
            {"_id": 1},
            {"$inc": {"count": 1}},
        )

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(write, range(workers)))

        assert build_calls == 1
        assert all(result.matched_count == 1 for result in results)
    finally:
        client.close()
