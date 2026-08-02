import asyncio
import copy
from collections import OrderedDict, UserDict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from tinymongo import AsyncMongoClient, MongoClient
from tinymongo.storage_backends import clear_memory_namespace


_BACKENDS = ("memory", "tinydb", "sqlite", "duckdb", "parquet")
_STORED = datetime(
    2026,
    1,
    2,
    3,
    4,
    5,
    123456,
    tzinfo=timezone(timedelta(hours=-5)),
)
_UTC_MILLIS = datetime(2026, 1, 2, 8, 4, 5, 123000)
_BEFORE_EPOCH = datetime(1969, 12, 31, 23, 59, 59, 999999)


def _client_location(tmp_path, backend):
    if backend == "memory":
        return "memory://read-fidelity-{0}".format(uuid4().hex)
    return str(tmp_path / backend)


def _require_backend_dependencies(backend):
    if backend in ("duckdb", "parquet"):
        pytest.importorskip("duckdb")
    if backend == "parquet":
        pytest.importorskip("pyarrow")


def _assert_recursive_document_class(document, document_class):
    assert type(document) is document_class
    assert type(document["inner"]) is document_class
    assert type(document["items"][0]) is document_class
    assert type(document["items"][0]["deep"]) is document_class


def _close_clients(location, *clients):
    for client in clients:
        client.close()
    if str(location).startswith("memory://"):
        clear_memory_namespace(location)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_sync_mongo_client_materializes_documents_and_datetimes_across_backends(
    tmp_path,
    backend,
):
    _require_backend_dependencies(backend)
    location = _client_location(tmp_path, backend)
    client = MongoClient(
        location,
        backend=backend,
        document_class=OrderedDict,
    )
    collection = client.app.items
    source = {
        "_id": "main",
        "when": _STORED,
        "before_epoch": _BEFORE_EPOCH,
        "inner": {"when": _STORED},
        "items": [{"deep": {"when": _STORED}}],
    }
    original = copy.deepcopy(source)

    collection.insert_one(source)

    assert source == original
    found = collection.find_one({"_id": "main"})
    _assert_recursive_document_class(found, OrderedDict)
    assert found["when"] == _UTC_MILLIS
    assert found["when"].tzinfo is None
    assert found["before_epoch"] == datetime(1969, 12, 31, 23, 59, 59, 999000)
    assert found["inner"]["when"] == _UTC_MILLIS
    assert found["items"][0]["deep"]["when"] == _UTC_MILLIS

    projected = collection.find_one(
        {"_id": "main"},
        {"inner": 1, "items": 1, "when": 1},
    )
    _assert_recursive_document_class(projected, OrderedDict)

    cursor = collection.find({"_id": "main"})
    indexed = cursor[0]
    listed = cursor.clone().to_list()
    _assert_recursive_document_class(indexed, OrderedDict)
    _assert_recursive_document_class(listed[0], OrderedDict)

    aggregated = collection.aggregate(
        [
            {"$match": {"_id": "main"}},
            {"$project": {"inner": 1, "items": 1, "when": 1}},
        ]
    ).next()
    _assert_recursive_document_class(aggregated, OrderedDict)

    distinct = collection.distinct("inner")
    assert len(distinct) == 1
    assert type(distinct[0]) is OrderedDict
    assert distinct[0]["when"] == _UTC_MILLIS

    collection.insert_many(
        [
            {"_id": "update", "inner": {"value": 1}},
            {"_id": "replace", "inner": {"value": 1}},
            {"_id": "delete", "inner": {"value": 1}},
        ]
    )
    updated = collection.find_one_and_update(
        {"_id": "update"},
        {"$set": {"inner.value": 2}},
        projection={"_id": 0, "inner": 1},
        return_document=True,
    )
    replaced = collection.find_one_and_replace(
        {"_id": "replace"},
        {"inner": {"value": 2}},
        projection={"_id": 0, "inner": 1},
        return_document=True,
    )
    deleted = collection.find_one_and_delete(
        {"_id": "delete"},
        projection={"_id": 0, "inner": 1},
    )
    for returned in (updated, replaced, deleted):
        assert type(returned) is OrderedDict
        assert type(returned["inner"]) is OrderedDict

    database_metadata = client.list_databases().to_list()
    assert database_metadata
    assert type(database_metadata[0]) is dict
    assert type(collection.list_indexes()[0]) is dict
    assert type(collection.index_information()) is dict

    _close_clients(location, client)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_sync_mongo_client_honors_aware_and_custom_timezone_reads(
    tmp_path,
    backend,
):
    _require_backend_dependencies(backend)
    location = _client_location(tmp_path, backend)
    writer = MongoClient(location, backend=backend)
    writer.app.items.insert_one({"_id": 1, "when": _STORED})
    writer.close()

    aware = MongoClient(location, backend=backend, tz_aware="true")
    aware_value = aware.app.items.find_one({"_id": 1})["when"]
    assert aware_value == _UTC_MILLIS.replace(tzinfo=timezone.utc)
    assert aware_value.utcoffset() == timedelta(0)

    eastern = timezone(timedelta(hours=-4))
    converted = MongoClient(
        location,
        backend=backend,
        tz_aware=True,
        tzinfo=eastern,
    )
    converted_value = converted.app.items.find_one({"_id": 1})["when"]
    assert converted_value == datetime(
        2026,
        1,
        2,
        4,
        4,
        5,
        123000,
        tzinfo=eastern,
    )

    _close_clients(location, aware, converted)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_same_millisecond_datetime_writes_are_noops_across_backends(
    tmp_path,
    backend,
):
    _require_backend_dependencies(backend)
    location = _client_location(tmp_path, backend)
    client = MongoClient(location, backend=backend)
    collection = client.app.items
    first = datetime(2026, 1, 2, 3, 4, 5, 123001, tzinfo=timezone.utc)
    same_millisecond = first.replace(microsecond=123999)
    collection.insert_one({"_id": 1, "when": first})

    updated = collection.update_one(
        {"_id": 1},
        {"$set": {"when": same_millisecond}},
    )
    replaced = collection.replace_one(
        {"_id": 1},
        {"_id": 1, "when": same_millisecond},
    )

    assert updated.modified_count == 0
    assert replaced.modified_count == 0
    assert collection.find_one({"_id": 1})["when"] == datetime(
        2026,
        1,
        2,
        3,
        4,
        5,
        123000,
    )

    _close_clients(location, client)


def test_document_class_supports_mutable_mapping_classes_and_client_isolation(
    tmp_path,
):
    location = str(tmp_path / "shared")
    writer = MongoClient(location, backend="sqlite")
    writer.app.items.insert_one(
        {"_id": 1, "inner": {"value": 1}, "items": [{"deep": {"value": 2}}]}
    )
    writer.close()

    user_dict_client = MongoClient(
        location,
        backend="sqlite",
        document_class=UserDict,
    )
    ordered_client = MongoClient(
        location,
        backend="sqlite",
        document_class=OrderedDict,
    )
    user_document = user_dict_client.app.items.find_one({"_id": 1})
    ordered_document = ordered_client.app.items.find_one({"_id": 1})

    _assert_recursive_document_class(user_document, UserDict)
    _assert_recursive_document_class(ordered_document, OrderedDict)
    user_document["inner"]["value"] = 99
    assert ordered_client.app.items.find_one({"_id": 1})["inner"]["value"] == 1

    _close_clients(location, user_dict_client, ordered_client)


@pytest.mark.parametrize("document_class", [dict[str, object], UserDict[str, object]])
def test_document_class_supports_parameterized_mutable_mapping_aliases(
    tmp_path,
    document_class,
):
    client = MongoClient(
        str(tmp_path / "generic-alias"),
        backend="sqlite",
        document_class=document_class,
    )
    client.app.items.insert_one({"_id": 1, "inner": {"value": 1}})

    document = client.app.items.find_one({"_id": 1})
    assert type(document) is document_class.__origin__
    assert type(document["inner"]) is document_class.__origin__

    client.close()


def test_document_class_supports_bson_son(tmp_path):
    son = pytest.importorskip("bson.son").SON
    client = MongoClient(
        str(tmp_path / "son"),
        backend="sqlite",
        document_class=son,
    )
    client.app.items.insert_one({"_id": 1, "inner": {}, "items": [{"deep": {}}]})

    _assert_recursive_document_class(
        client.app.items.find_one({"_id": 1}),
        son,
    )

    client.close()


@pytest.mark.parametrize("backend", _BACKENDS)
def test_async_mongo_client_matches_recursive_sync_read_fidelity(
    tmp_path,
    backend,
):
    _require_backend_dependencies(backend)
    location = _client_location(tmp_path, backend)

    async def scenario():
        client = AsyncMongoClient(
            location,
            backend=backend,
            document_class=OrderedDict,
            tz_aware=True,
        )
        collection = client.app.items
        try:
            await collection.insert_one(
                {
                    "_id": "main",
                    "when": _STORED,
                    "inner": {"when": _STORED},
                    "items": [{"deep": {"when": _STORED}}],
                }
            )
            found = await collection.find_one({"_id": "main"})
            _assert_recursive_document_class(found, OrderedDict)
            assert found["when"] == _UTC_MILLIS.replace(tzinfo=timezone.utc)

            cursor = collection.find({"_id": "main"})
            cloned = cursor.clone()
            listed = await cursor.to_list()
            cloned_list = await cloned.to_list()
            _assert_recursive_document_class(listed[0], OrderedDict)
            _assert_recursive_document_class(cloned_list[0], OrderedDict)

            aggregate = await collection.aggregate(
                [{"$project": {"inner": 1, "items": 1, "when": 1}}]
            )
            aggregated = await aggregate.to_list()
            _assert_recursive_document_class(aggregated[0], OrderedDict)

            distinct = await collection.distinct("inner")
            assert type(distinct[0]) is OrderedDict

            await collection.insert_one({"_id": "update", "inner": {"value": 1}})
            updated = await collection.find_one_and_update(
                {"_id": "update"},
                {"$set": {"inner.value": 2}},
                projection={"_id": 0, "inner": 1},
                return_document=True,
            )
            assert type(updated) is OrderedDict
            assert type(updated["inner"]) is OrderedDict
        finally:
            await client.close()

    asyncio.run(scenario())
    if str(location).startswith("memory://"):
        clear_memory_namespace(location)
