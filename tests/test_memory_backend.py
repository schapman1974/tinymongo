from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import tinymongo as tm
from tinymongo.errors import DuplicateKeyError
from tinymongo import storage_backends
from tinymongo.storage_backends import storage_extension


def _memory_uri(prefix):
    return "memory://{0}-{1}".format(prefix, uuid4().hex)


def test_anonymous_memory_backend_supports_crud_without_creating_files(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    client = tm.TinyMongoClient(backend="memory")
    collection = client.app.items

    inserted = collection.insert_one({"_id": "item-1", "count": 1})
    updated = collection.update_one({"_id": "item-1"}, {"$inc": {"count": 2}})

    assert inserted.inserted_id == "item-1"
    assert (updated.matched_count, updated.modified_count) == (1, 1)
    assert collection.find_one({"_id": "item-1"}) == {
        "_id": "item-1",
        "count": 3,
    }
    assert collection.delete_one({"_id": "item-1"}).deleted_count == 1
    assert collection.count_documents({}) == 0

    client.close()
    assert list(tmp_path.iterdir()) == []


def test_anonymous_memory_clients_are_isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = tm.TinyMongoClient(backend="memory")
    second = tm.TinyMongoClient(backend="memory")

    first.app.items.insert_one({"_id": "private"})

    assert first.app.items.count_documents({}) == 1
    assert second.app.items.count_documents({}) == 0

    first.close()
    second.close()
    assert list(tmp_path.iterdir()) == []


def test_named_memory_clients_share_data_and_survive_close(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    uri = _memory_uri("shared")
    writer = tm.TinyMongoClient(uri, backend="memory")
    peer = tm.TinyMongoClient(uri, backend="memory")

    writer.app.items.insert_one({"_id": "shared", "value": 42})
    assert peer.app.items.find_one({"_id": "shared"})["value"] == 42

    writer.close()
    peer.close()
    later = tm.TinyMongoClient(uri, backend="memory")
    try:
        assert later.app.items.find_one({"_id": "shared"}) == {
            "_id": "shared",
            "value": 42,
        }
    finally:
        later.close()

    assert list(tmp_path.iterdir()) == []


def test_named_reader_refreshes_a_cached_miss_after_another_client_writes():
    uri = _memory_uri("cache-refresh")
    writer = tm.TinyMongoClient(uri, backend="memory")
    reader = tm.TinyMongoClient(uri, backend="memory")
    try:
        assert list(reader.app.items.find({"kind": "new"})) == []

        writer.app.items.insert_one({"_id": "later", "kind": "new"})

        assert list(reader.app.items.find({"kind": "new"})) == [
            {"_id": "later", "kind": "new"}
        ]
    finally:
        writer.close()
        reader.close()


def test_each_retained_collection_handle_tracks_its_own_cache_revision():
    uri = _memory_uri("per-collection-cache")
    writer = tm.TinyMongoClient(uri, backend="memory")
    reader = tm.TinyMongoClient(uri, backend="memory")
    stale_items = reader.app.items
    unrelated = reader.app.audit
    try:
        assert list(stale_items.find({"kind": "new"})) == []
        assert list(unrelated.find({})) == []

        writer.app.items.insert_one({"_id": "later", "kind": "new"})
        assert list(unrelated.find({})) == []

        assert list(stale_items.find({"kind": "new"})) == [
            {"_id": "later", "kind": "new"}
        ]
    finally:
        writer.close()
        reader.close()


def test_shared_collection_invalidates_its_cached_equality_index():
    uri = _memory_uri("index-cache")
    writer = tm.TinyMongoClient(uri, backend="memory")
    reader = tm.TinyMongoClient(uri, backend="memory")
    indexed = reader.app.items
    unrelated = reader.app.audit
    try:
        writer.app.items.insert_one({"_id": 1, "name": "Ada"})
        indexed.create_index("name")
        assert list(indexed.find({"name": "Ada"})) == [{"_id": 1, "name": "Ada"}]

        writer.app.items.update_one({"_id": 1}, {"$set": {"name": "Grace"}})
        assert list(unrelated.find({})) == []

        assert list(indexed.find({"name": "Ada"})) == []
        assert list(indexed.find({"name": "Grace"})) == [{"_id": 1, "name": "Grace"}]
    finally:
        writer.close()
        reader.close()


def test_shared_find_one_and_update_refreshes_before_returning_old_document():
    uri = _memory_uri("find-and-update")
    writer = tm.TinyMongoClient(uri, backend="memory")
    reader = tm.TinyMongoClient(uri, backend="memory")
    try:
        assert reader.app.items.find_one({"_id": "later"}) is None
        writer.app.items.insert_one({"_id": "later", "count": 1})

        previous = reader.app.items.find_one_and_update(
            {"_id": "later"}, {"$inc": {"count": 1}}
        )

        assert previous == {"_id": "later", "count": 1}
        assert writer.app.items.find_one({"_id": "later"})["count"] == 2
    finally:
        writer.close()
        reader.close()


def test_memory_storage_copies_input_and_returned_documents():
    client = tm.TinyMongoClient(backend="memory")
    source = {"_id": "copy", "nested": {"count": 1}}
    try:
        client.app.items.insert_one(source)
        source["nested"]["count"] = 99
        returned = client.app.items.find_one({"_id": "copy"})
        returned["nested"]["count"] = 42

        assert client.app.items.find_one({"_id": "copy"}) == {
            "_id": "copy",
            "nested": {"count": 1},
        }
    finally:
        client.close()


def test_memory_storage_uses_the_same_json_value_rules_as_default_storage():
    client = tm.TinyMongoClient(backend="memory")
    try:
        client.app.items.insert_one({"_id": "tuple", "values": (1, 2)})
        assert client.app.items.find_one({"_id": "tuple"})["values"] == [1, 2]

        with pytest.raises(TypeError):
            client.app.items.insert_one({"_id": "unsupported", "value": object()})
        assert client.app.items.find_one({"_id": "unsupported"}) is None
    finally:
        client.close()


def test_different_named_memory_registries_are_isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = tm.TinyMongoClient(_memory_uri("first"), backend="memory")
    second = tm.TinyMongoClient(_memory_uri("second"), backend="memory")

    first.app.items.insert_one({"_id": "only-first"})

    assert second.app.items.find_one({"_id": "only-first"}) is None

    first.close()
    second.close()
    assert list(tmp_path.iterdir()) == []


def test_named_clients_share_collection_lifecycle_and_local_index_behavior():
    uri = _memory_uri("collection-lifecycle")
    writer = tm.TinyMongoClient(uri, backend="memory")
    peer = tm.TinyMongoClient(uri, backend="memory")
    collection = writer.app.items
    try:
        collection.insert_one({"_id": 1, "name": "Ada"})
        assert collection.create_index("name") == "name_1"
        assert {index["name"] for index in collection.list_indexes()} == {
            "_id_",
            "name_1",
        }
        assert collection.find_one({"name": "Ada"})["_id"] == 1

        assert peer.app.drop_collection("items") is True
        assert "items" not in peer.app.list_collection_names()
        assert collection.find_one({"_id": 1}) is None
    finally:
        writer.close()
        peer.close()


def test_named_memory_backend_handles_concurrent_client_inserts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    uri = _memory_uri("threads")
    workers = 4
    inserts_per_worker = 20

    def insert_batch(worker):
        with tm.TinyMongoClient(uri, backend="memory") as client:
            client.app.items.insert_many(
                [
                    {
                        "_id": "{0}-{1}".format(worker, index),
                        "worker": worker,
                        "index": index,
                    }
                    for index in range(inserts_per_worker)
                ]
            )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(insert_batch, range(workers)))

    reader = tm.TinyMongoClient(uri, backend="memory")
    try:
        documents = list(reader.app.items.find({}))
        assert len(documents) == workers * inserts_per_worker
        assert {document["_id"] for document in documents} == {
            "{0}-{1}".format(worker, index)
            for worker in range(workers)
            for index in range(inserts_per_worker)
        }
    finally:
        reader.close()

    assert list(tmp_path.iterdir()) == []


def test_concurrent_duplicate_id_allows_exactly_one_insert():
    uri = _memory_uri("duplicate")

    def insert_duplicate(_worker):
        with tm.TinyMongoClient(uri, backend="memory") as client:
            try:
                client.app.items.insert_one({"_id": "same"})
                return True
            except DuplicateKeyError:
                return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(insert_duplicate, range(4)))

    reader = tm.TinyMongoClient(uri, backend="memory")
    try:
        assert outcomes.count(True) == 1
        assert reader.app.items.count_documents({"_id": "same"}) == 1
    finally:
        reader.close()


def test_memory_database_listing_and_capabilities(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = tm.TinyMongoClient(backend="memory")

    assert client.list_database_names() == []

    client.alpha.items.insert_one({"_id": 1})
    client.zeta.items.insert_one({"_id": 2})
    capabilities = client.capabilities()

    assert client.list_database_names() == ["alpha", "zeta"]
    assert capabilities["backend"] == "memory"
    assert capabilities["persistent"] is False
    assert capabilities["multiprocess_writes"] is False
    assert client.supports("persistent") is False
    assert client.supports("multiprocess_writes") is False

    client.close()
    assert list(tmp_path.iterdir()) == []


def test_mongo_client_memory_uri_uses_the_named_registry_without_disk(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    shared_uri = _memory_uri("mongo-client")
    other_uri = _memory_uri("other-mongo-client")
    writer = tm.MongoClient(shared_uri, backend="memory")

    writer.app.items.insert_one({"_id": "through-mongo-client"})
    writer.close()

    reader = tm.MongoClient(shared_uri, backend="memory")
    isolated = tm.MongoClient(other_uri, backend="memory")
    try:
        assert reader.app.items.find_one({"_id": "through-mongo-client"}) == {
            "_id": "through-mongo-client"
        }
        assert isolated.app.items.find_one({"_id": "through-mongo-client"}) is None
    finally:
        reader.close()
        isolated.close()

    assert list(tmp_path.iterdir()) == []


def test_memory_uri_scheme_is_case_insensitive_for_both_client_classes():
    name = "case-{0}".format(uuid4().hex)
    writer = tm.TinyMongoClient("Memory://{0}".format(name), backend="memory")
    reader = tm.MongoClient("memory://{0}".format(name), backend="memory")
    try:
        writer.app.items.insert_one({"_id": "shared"})
        assert reader.app.items.find_one({"_id": "shared"}) == {"_id": "shared"}
    finally:
        writer.close()
        reader.close()


def test_closing_anonymous_client_clears_its_private_namespace():
    client = tm.TinyMongoClient(backend="memory")
    private_uri = client._memory_namespace
    client.app.items.insert_one({"_id": "temporary"})
    client.close()
    client.close()

    reopened = tm.TinyMongoClient(private_uri, backend="memory")
    try:
        assert reopened.list_database_names() == []
        assert reopened.app.items.find_one({"_id": "temporary"}) is None
    finally:
        reopened.close()


def test_stale_cleanup_does_not_remove_a_replacement_memory_entry():
    namespace = "memory://cleanup-{0}".format(uuid4().hex)
    address = namespace + "/app"
    replacement = {
        "data": {"items": {"1": {"_id": "replacement"}}},
        "revision": 1,
        "lock": storage_backends.threading.RLock(),
    }

    class ReplaceEntryOnAcquire:
        def __enter__(self):
            with storage_backends._memory_registry_lock:
                storage_backends._memory_registry[address] = replacement

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    stale = {"data": None, "revision": 0, "lock": ReplaceEntryOnAcquire()}
    with storage_backends._memory_registry_lock:
        storage_backends._memory_registry[address] = stale

    try:
        storage_backends.clear_memory_namespace(namespace)
        with storage_backends._memory_registry_lock:
            assert storage_backends._memory_registry[address] is replacement
    finally:
        with storage_backends._memory_registry_lock:
            storage_backends._memory_registry.pop(address, None)


def test_clear_memory_database_handles_missing_and_replaced_entries():
    address = "memory://clear-database-{0}/app".format(uuid4().hex)

    storage_backends.clear_memory_database(address)

    replacement = {
        "data": {"items": {"1": {"_id": "replacement"}}},
        "revision": 1,
        "lock": storage_backends.threading.RLock(),
    }

    class ReplaceEntryOnAcquire:
        def __enter__(self):
            with storage_backends._memory_registry_lock:
                storage_backends._memory_registry[address] = replacement

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    stale = {"data": None, "revision": 0, "lock": ReplaceEntryOnAcquire()}
    with storage_backends._memory_registry_lock:
        storage_backends._memory_registry[address] = stale

    try:
        storage_backends.clear_memory_database(address)
        with storage_backends._memory_registry_lock:
            assert storage_backends._memory_registry[address] is replacement
    finally:
        with storage_backends._memory_registry_lock:
            storage_backends._memory_registry.pop(address, None)


@pytest.mark.parametrize(
    "address",
    [
        "memory://",
        "memory://nested/path",
        "memory://name?option=1",
        "memory://name#x",
        "memory://two words",
    ],
)
def test_invalid_named_memory_addresses_fail_clearly(address):
    with pytest.raises(ValueError, match="memory://test-suite"):
        tm.TinyMongoClient(address, backend="memory")


@pytest.mark.parametrize("client_class", [tm.TinyMongoClient, tm.MongoClient])
@pytest.mark.parametrize("address", ["memroy://name", "mongodb://localhost"])
def test_other_uri_schemes_are_rejected_instead_of_silently_isolated(
    client_class, address
):
    with pytest.raises(ValueError, match="must start with memory://"):
        client_class(address, backend="memory")


def test_memory_storage_extension_is_empty():
    assert storage_extension("memory") == ""
