import asyncio
import threading

import pytest

from tinymongo.asyncio import (
    AsyncMongoClient,
    AsyncTinyMongoClient,
    AsyncTinyMongoCursor,
)
from tinymongo.errors import InvalidOperation, TinyMongoNotSupportedError
from tinymongo.tinymongo import TinyMongoCollection


def run(coroutine):
    return asyncio.run(coroutine)


def test_async_crud_cursor_and_client_metadata():
    async def scenario():
        client = AsyncTinyMongoClient("memory://async-crud", backend="memory")
        collection = client.app.people

        result = await collection.insert_many(
            [
                {"_id": 1, "name": "one", "rank": 3},
                {"_id": 2, "name": "two", "rank": 1},
                {"_id": 3, "name": "three", "rank": 2},
            ]
        )
        assert result.inserted_ids == [1, 2, 3]
        assert await collection.count_documents({}) == 3

        cursor = collection.find({}, {"name": 1, "_id": 0})
        assert isinstance(cursor, AsyncTinyMongoCursor)
        documents = await cursor.sort("rank").skip(1).limit(1).to_list()
        assert documents == [{"name": "three"}]

        update = await collection.update_one({"_id": 2}, {"$set": {"rank": 4}})
        assert update.matched_count == 1
        assert await collection.find_one({"_id": 2}, {"rank": 1}) == {
            "_id": 2,
            "rank": 4,
        }

        deleted = await collection.delete_one({"_id": 1})
        assert deleted.deleted_count == 1
        assert await client.list_database_names() == ["app"]
        assert (await client.server_info())["tinymongo"] is True
        assert await client.supports("projections") is True

        await client.close()
        await client.close()
        with pytest.raises(InvalidOperation):
            await collection.find_one({})

    run(scenario())


def test_async_database_listing_and_drop_database():
    async def scenario():
        client = AsyncTinyMongoClient("memory://async-databases", backend="memory")
        database = client.app
        await database.items.insert_one({"_id": 1})
        await client.zeta.events.insert_one({"_id": 2})

        metadata = await client.list_databases()
        assert isinstance(metadata, AsyncTinyMongoCursor)
        metadata.sort("name", -1).skip(1).limit(1)
        assert await metadata.to_list() == [
            {"name": "app", "sizeOnDisk": 0, "empty": False}
        ]

        assert await client.drop_database(database) is None
        assert await client.list_database_names() == ["zeta"]
        assert await (await client.list_databases()).to_list() == [
            {"name": "zeta", "sizeOnDisk": 0, "empty": False}
        ]
        assert await client.drop_database("zeta") is None
        assert await client.list_database_names() == []
        assert await client.drop_database("missing") is None
        with pytest.raises(TypeError):
            await client.drop_database(object())
        await client.close()

    run(scenario())


def test_find_is_lazy_and_cursor_clone_has_independent_consumption(monkeypatch):
    calls = []
    original_find = TinyMongoCollection.find

    def recording_find(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original_find(self, *args, **kwargs)

    monkeypatch.setattr(TinyMongoCollection, "find", recording_find)

    async def scenario():
        async with AsyncMongoClient("memory://async-lazy", backend="memory") as client:
            collection = client.db.items
            await collection.insert_many([{"_id": 1, "n": 2}, {"_id": 2, "n": 1}])
            calls.clear()

            cursor = collection.find({}).sort("n")
            clone = cursor.clone()
            assert calls == []

            assert await cursor.next() == {"_id": 2, "n": 1}
            assert await cursor.to_list() == [{"_id": 1, "n": 2}]
            assert await cursor.try_next() is None
            assert calls and len(calls) == 1

            assert [document async for document in clone] == [
                {"_id": 2, "n": 1},
                {"_id": 1, "n": 2},
            ]
            assert len(calls) == 2
            await clone.rewind()
            assert await clone.to_list(1) == [{"_id": 2, "n": 1}]
            assert await clone.to_list(0) == []
            assert await clone.to_list() == [{"_id": 1, "n": 2}]

    run(scenario())


def test_storage_work_does_not_block_the_event_loop(monkeypatch):
    original_insert_one = TinyMongoCollection.insert_one
    entered = threading.Event()
    event_loop_progressed = threading.Event()

    def waiting_insert(self, *args, **kwargs):
        entered.set()
        if not event_loop_progressed.wait(timeout=10):
            raise AssertionError("event loop did not progress during storage work")
        return original_insert_one(self, *args, **kwargs)

    monkeypatch.setattr(TinyMongoCollection, "insert_one", waiting_insert)

    async def scenario():
        client = AsyncTinyMongoClient("memory://async-responsive", backend="memory")
        loop = asyncio.get_running_loop()
        loop.call_soon(event_loop_progressed.set)
        result = await client.db.items.insert_one({"_id": 1})

        assert entered.is_set()
        assert event_loop_progressed.is_set()
        assert result.inserted_id == 1
        await client.close()

    run(scenario())


def test_async_clients_reject_invalid_backends_without_opening_storage(tmp_path):
    default_client = AsyncTinyMongoClient()
    assert default_client._state._client is None
    run(default_client.close())

    native_path = tmp_path / "native"
    with pytest.raises(ValueError, match="Unsupported backend"):
        AsyncTinyMongoClient(str(native_path), "invalid-native")
    assert not native_path.exists()

    pymongo_path = tmp_path / "pymongo"
    with pytest.raises(ValueError, match="Unsupported backend"):
        AsyncMongoClient(
            "mongodb://localhost:27017",
            tinymongo_folder=str(pymongo_path),
            backend="invalid-pymongo",
        )
    assert not pymongo_path.exists()


def test_cursor_validation_close_and_limit_zero():
    async def scenario():
        client = AsyncTinyMongoClient(
            "memory://async-cursor-validation", backend="memory"
        )
        collection = client.db.items
        await collection.insert_many([{"_id": 1}, {"_id": 2}])

        with pytest.raises(TypeError):
            collection.find({}).skip(True)
        with pytest.raises(ValueError):
            collection.find({}).skip(-1)
        with pytest.raises(TypeError):
            collection.find({}).limit(1.5)

        cursor = collection.find({}).limit(0)
        assert len(await cursor.to_list()) == 2
        with pytest.raises(InvalidOperation):
            cursor.sort("_id")

        closed = collection.find({})
        await closed.close()
        assert closed.alive is False
        assert await closed.to_list() == []
        with pytest.raises(InvalidOperation):
            closed.skip(1)

        await client.close()

    run(scenario())


def test_database_helpers_and_unsupported_calls():
    async def scenario():
        async with AsyncTinyMongoClient(
            "memory://async-database", backend="memory"
        ) as client:
            database = client.get_database("app")
            collection = database.get_collection("events")
            assert database.name == "app"
            assert collection.full_name == "app.events"
            assert collection.with_options() is collection

            await collection.insert_one({"_id": "event"})
            assert await database.list_collection_names() == ["events"]
            assert await database.drop_collection(collection) is True

            assert await database.command("ping") == {"ok": 1.0}
            with pytest.raises(TinyMongoNotSupportedError):
                await database.command("serverStatus")
            cursor = await collection.aggregate([])
            assert isinstance(cursor, AsyncTinyMongoCursor)
            assert await cursor.to_list() == []

    run(scenario())


def test_remaining_supported_collection_surface(monkeypatch):
    def create_indexes(self, models, *args, **kwargs):
        return ["batch_index"]

    monkeypatch.setattr(
        TinyMongoCollection, "create_indexes", create_indexes, raising=False
    )

    async def scenario():
        client = AsyncTinyMongoClient("memory://async-surface", backend="memory")
        database = client.app
        collection = database.items

        assert repr(database) == "app"
        assert database.database == "app"
        assert repr(collection) == "items"
        assert collection.tablename == "items"
        assert collection.write_concern.document == {}
        assert collection.read_concern.document == {}
        assert (
            collection.with_options(read_concern=collection.read_concern) is collection
        )

        assert (await collection.insert({"_id": 1, "kind": "a"})).inserted_id == 1
        assert (
            await collection.insert([{"_id": 2, "kind": "a"}, {"_id": 3, "kind": "b"}])
        ).inserted_ids == [2, 3]
        assert (
            await collection.update({}, {"$set": {"seen": True}})
        ).matched_count == 3
        assert (
            await collection.update_many({"kind": "a"}, {"$inc": {"score": 1}})
        ).matched_count == 2
        assert (
            await collection.replace_one({"_id": 3}, {"kind": "c", "score": 5})
        ).matched_count == 1

        before = await collection.find_one_and_update(
            {"_id": 1}, {"$set": {"kind": "changed"}}
        )
        assert before["kind"] == "a"
        before_replace = await collection.find_one_and_replace(
            {"_id": 2}, {"kind": "replaced"}
        )
        assert before_replace["kind"] == "a"
        removed = await collection.find_one_and_delete(
            {"kind": {"$in": ["changed", "replaced"]}},
            projection={"kind": 1, "_id": 0},
            sort=[("kind", 1)],
        )
        assert removed == {"kind": "changed"}
        assert await collection.estimated_document_count() == 2
        assert await collection.count() == 2

        assert await collection.create_index("kind") == "kind_1"
        indexes = await collection.list_indexes()
        assert isinstance(indexes, AsyncTinyMongoCursor)
        assert [item["name"] async for item in indexes] == ["_id_", "kind_1"]
        assert (await collection.index_information())["kind_1"]["key"] == [("kind", 1)]
        assert await collection.distinct("kind") == ["replaced", "c"]
        await collection.drop_index("kind")
        assert await collection.create_indexes([object()]) == ["batch_index"]

        assert (await collection.remove({"_id": 1}, multi=False)).deleted_count == 0
        assert (await collection.delete_many({"kind": "replaced"})).deleted_count == 1
        assert "items" in await database.collection_names()

        with pytest.raises(TinyMongoNotSupportedError):
            await collection.bulk_write([])
        with pytest.raises(TinyMongoNotSupportedError):
            await collection.watch()
        with pytest.raises(TinyMongoNotSupportedError):
            await database.watch()
        with pytest.raises(TinyMongoNotSupportedError):
            await client.watch()
        with pytest.raises(TinyMongoNotSupportedError):
            client.start_session()

        assert await collection.drop() is True
        assert await database.drop_collection("missing") is False
        await client.close()

    run(scenario())


def test_cursor_results_do_not_alias_the_cached_documents():
    async def scenario():
        client = AsyncTinyMongoClient("memory://async-result-copies", backend="memory")
        collection = client.db.items
        await collection.insert_one({"_id": 1, "profile": {"name": "Ada"}})

        cursor = collection.find({})
        yielded = await cursor.next()
        yielded["profile"]["name"] = "changed"
        await cursor.rewind()
        assert await cursor.next() == {"_id": 1, "profile": {"name": "Ada"}}

        await cursor.rewind()
        listed = await cursor.to_list()
        listed[0]["profile"]["name"] = "changed again"
        await cursor.rewind()
        assert await cursor.next() == {"_id": 1, "profile": {"name": "Ada"}}

        await client.close()

    run(scenario())


def test_cursor_initial_options_counts_and_error_paths(monkeypatch):
    async def scenario():
        client = AsyncTinyMongoClient("memory://async-options", backend="memory")
        collection = client.db.items
        await collection.insert_many(
            [{"_id": 1, "n": 3}, {"_id": 2, "n": 2}, {"_id": 3, "n": 1}]
        )

        cursor = collection.find({}, sort=[("n", 1)], skip=1, limit=-1)
        assert cursor.alive is True
        assert await cursor.count() == 1
        assert cursor.alive is True
        assert await cursor.to_list() == [{"_id": 2, "n": 2}]
        assert cursor.alive is False
        assert await cursor.to_list() == []
        await cursor.close()

        shared = collection.find({})
        first_load, second_load = await asyncio.gather(
            shared._ensure_loaded(), shared._ensure_loaded()
        )
        assert first_load is second_load

        with pytest.raises(TypeError):
            await collection.find({}).to_list(True)
        with pytest.raises(ValueError):
            await collection.find({}).to_list(-1)

        closed = collection.find({})
        await closed.close()
        with pytest.raises(InvalidOperation):
            await closed.rewind()

        original_find = TinyMongoCollection.find

        def broken_find(self, *args, **kwargs):
            raise RuntimeError("query failed")

        monkeypatch.setattr(TinyMongoCollection, "find", broken_find)
        broken = collection.find({})
        with pytest.raises(RuntimeError, match="query failed"):
            await broken.to_list()
        await broken.close()
        monkeypatch.setattr(TinyMongoCollection, "find", original_find)

        await client.close()

    run(scenario())


def test_cursor_legacy_pagination_and_has_next_helpers():
    async def scenario():
        client = AsyncTinyMongoClient("memory://async-pagination", backend="memory")
        collection = client.db.items
        await collection.insert_many([{"_id": 1}, {"_id": 2}, {"_id": 3}, {"_id": 4}])

        cursor = collection.find({}).sort("_id").paginate(1, -2)
        assert await cursor.has_next() is True
        assert await cursor.hasNext() is True
        assert await cursor.next() == {"_id": 2}
        assert await cursor.has_next() is True
        assert await cursor.next() == {"_id": 3}
        assert await cursor.hasNext() is False

        unchanged = collection.find({}).skip(1).limit(1).paginate(None, None)
        assert await unchanged.to_list() == [{"_id": 2}]

        with pytest.raises(TypeError):
            collection.find({}).paginate(True, None)
        with pytest.raises(ValueError):
            collection.find({}).paginate(-1, None)
        with pytest.raises(TypeError):
            collection.find({}).paginate(None, False)

        started = collection.find({})
        assert await started.has_next() is True
        with pytest.raises(InvalidOperation):
            started.paginate(0, 1)

        closed = collection.find({})
        await closed.close()
        assert await closed.has_next() is False

        await client.close()

    run(scenario())


def test_closed_handles_private_attributes_and_database_context():
    async def scenario():
        unopened = AsyncTinyMongoClient("memory://async-unopened", backend="memory")
        await unopened.close()
        with pytest.raises(InvalidOperation):
            async with unopened:
                pass

        client = AsyncTinyMongoClient("memory://async-attrs", backend="memory")
        with pytest.raises(AttributeError):
            client.__getattr__("_missing")
        with pytest.raises(AttributeError):
            client.db.__getattr__("_missing")

        assert await client.database_names() == []
        assert (await client.capabilities())["backend"] == "memory"
        async with client.context as database:
            assert await database.list_collection_names() == []
        await client.close()

    run(scenario())


def test_non_default_collection_options_are_rejected():
    class Concern:
        document = {"w": 2}

    client = AsyncTinyMongoClient("memory://async-options-error", backend="memory")
    with pytest.raises(TinyMongoNotSupportedError):
        client.db.items.with_options(write_concern=Concern())
    run(client.close())


def test_close_waits_for_inflight_operations_and_calls_can_overlap():
    async def scenario():
        client = AsyncTinyMongoClient("memory://async-inflight", backend="memory")
        both_started = threading.Barrier(3)
        release_first = threading.Event()
        release_second = threading.Event()

        def wait_for(event):
            def operation(sync_client):
                both_started.wait(timeout=10)
                if not event.wait(timeout=10):
                    raise AssertionError("in-flight operation was not released")
                return sync_client.server_info()["tinymongo"]

            return operation

        first = asyncio.create_task(client._state.call(wait_for(release_first)))
        second = asyncio.create_task(client._state.call(wait_for(release_second)))
        await asyncio.to_thread(both_started.wait, 10)

        release_first.set()
        assert await first is True
        close_task = asyncio.create_task(client.close())

        async def wait_until_closed():
            while not client._state.closed:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_until_closed(), timeout=10)
        assert client._state.closed is True
        assert close_task.done() is False

        release_second.set()
        assert await second is True
        await close_task

    run(scenario())
