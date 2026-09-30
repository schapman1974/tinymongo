from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import tinymongo as tm
from tinymongo.errors import DuplicateKeyError, InvalidDocument
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

        unsupported = {"_id": "unsupported", "value": {1, 2}}
        with pytest.raises(InvalidDocument) as caught:
            client.app.items.insert_one(unsupported)
        assert caught.value.document is unsupported
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


def test_collection_operations_do_not_read_or_write_untouched_memory_tables(
    monkeypatch,
):
    with tm.TinyMongoClient(backend="memory") as client:
        db = client.app
        db.archive.insert_one({"_id": "large", "body": "x" * 100_000})
        storage = db.tinydb._storage
        archive = storage._entry["data"]["archive"]

        def reject_whole_database(*args, **kwargs):
            pytest.fail("collection operation accessed the whole memory database")

        monkeypatch.setattr(
            storage_backends.MemoryStorage, "read", reject_whole_database
        )
        monkeypatch.setattr(
            storage_backends.MemoryStorage, "write", reject_whole_database
        )
        target = db.items
        target.insert_one({"_id": "one", "value": 1})
        target.create_index("value", unique=True)
        target.update_one({"_id": "one"}, {"$set": {"value": 2}})
        assert target.find_one({"_id": "one"})["value"] == 2
        assert "archive" in db.list_collection_names()
        target.delete_one({"_id": "one"})
        target.drop()
        assert storage._entry["data"]["archive"] is archive
        assert db.archive.find_one({"_id": "large"})["body"] == "x" * 100_000


def test_table_storage_preserves_merge_replacement_and_caller_isolation():
    storage = storage_backends.MemoryStorage(_memory_uri("table-storage"))
    storage.write({"neighbor": {"1": {"_id": "untouched"}}})
    source = {1: {"_id": True, "nested": {"values": [1]}}}
    storage.write_table("items", source)
    source[1]["nested"]["values"].append(2)
    storage.write_table("items", {1: {"_id": 1, "value": "numeric"}})
    result = storage.read_table("items")
    assert len(result) == 2
    assert result["1"]["nested"]["values"] == [1]
    result["1"]["nested"]["values"].append(3)
    assert storage.read_table("items")["1"]["nested"]["values"] == [1]
    storage.merge_writes = False
    storage.write_table("items", {})
    assert storage.read_table("items") == {}
    assert storage.read()["neighbor"] == {"1": {"_id": "untouched"}}
    revision = storage.revision
    storage.purge_table("items")
    assert storage.revision == revision + 1
    storage.purge_table("missing")
    assert storage.revision == revision + 1


def test_invalid_table_write_does_not_publish_partial_changes():
    storage = storage_backends.MemoryStorage(_memory_uri("invalid-table"))
    storage.write_table("items", {1: {"_id": "valid"}})
    revision = storage.revision
    with pytest.raises((TypeError, InvalidDocument)):
        storage.write_table("items", {2: {"_id": "bad", "value": object()}})
    assert storage.revision == revision
    assert storage.read_table("items") == {"1": {"_id": "valid"}}


def test_whole_memory_storage_api_remains_compatible_with_table_writes():
    storage = storage_backends.MemoryStorage(_memory_uri("whole-storage"))
    assert storage.read() is None
    assert storage.table_names() == set()
    storage.write_table("items", {1: {"_id": "first"}})
    storage.write({"items": {1: {"_id": "second"}}})
    assert {doc["_id"] for doc in storage.read_table("items").values()} == {
        "first",
        "second",
    }
    storage.merge_writes = False
    storage.write({"replacement": {"1": {"_id": "only"}}})
    assert storage.table_names() == {"replacement"}


def test_memory_insert_does_not_json_serialize_resident_builtin_rows(monkeypatch):
    from tinymongo import bson_codec

    client = tm.TinyMongoClient(backend="memory")
    collection = client.app.items
    resident = {
        "_id": "resident",
        "body": "large transcript" * 10000,
        "nested": {"values": [1]},
    }
    collection.insert_one(resident)
    original = bson_codec.json.dumps

    def reject_resident_text(value, *args, **kwargs):
        def has_resident(node):
            if isinstance(node, dict):
                return node.get("_id") == "resident" or any(
                    has_resident(child) for child in node.values()
                )
            if isinstance(node, list):
                return any(has_resident(child) for child in node)
            return False

        assert not has_resident(value), "resident row was serialized to JSON"
        return original(value, *args, **kwargs)

    monkeypatch.setattr(bson_codec.json, "dumps", reject_resident_text)
    collection.insert_one({"_id": "probe", "body": "small"})
    resident["nested"]["values"].append(2)
    fetched = collection.find_one({"_id": "resident"})
    assert fetched["nested"]["values"] == [1]
    fetched["nested"]["values"].append(3)
    assert collection.find_one({"_id": "resident"})["nested"]["values"] == [1]
    assert collection.count_documents({}) == 2
    client.close()
