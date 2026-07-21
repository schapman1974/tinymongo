import asyncio
import pymongo
import pytest
import threading

import tinymongo
from tinymongo import patching, storage_backends


def test_patch_context_uses_shared_isolated_memory_and_restores_client():
    original = pymongo.MongoClient

    with tinymongo.patch() as patched_client:
        assert pymongo.MongoClient is patched_client

        first = pymongo.MongoClient(
            "mongodb://production.example",
            backend="sqlite",
            tinymongo_folder="ignored",
        )
        second = pymongo.MongoClient()
        namespace = first._memory_namespace

        assert isinstance(first, tinymongo.MongoClient)
        assert first.server_info()["storage"] == "memory"
        assert namespace == second._memory_namespace
        assert namespace.startswith("memory://__tinymongo_patch__")

        first.app.items.insert_one({"_id": "shared", "value": 42})
        assert second.app.items.find_one({"_id": "shared"})["value"] == 42
        first.close()
        second.close()

    assert pymongo.MongoClient is original
    prefix = namespace.rstrip("/") + "/"
    assert not any(
        address.startswith(prefix) for address in storage_backends._memory_registry
    )


def test_patch_restores_client_when_body_raises():
    original = pymongo.MongoClient

    with pytest.raises(RuntimeError, match="application failed"):
        with tinymongo.patch():
            assert pymongo.MongoClient is not original
            raise RuntimeError("application failed")

    assert pymongo.MongoClient is original


def test_patch_decorator_uses_explicit_folder_and_backend(tmp_path):
    original = pymongo.MongoClient
    folder = str(tmp_path / "patched-sqlite")

    @tinymongo.patch(folder=folder, backend="sqlite")
    def write_item(item_id):
        client = pymongo.MongoClient(
            "mongodb://ignored.example",
            backend="memory",
            tinymongo_folder="also-ignored",
        )
        try:
            client.app.items.insert_one({"_id": item_id})
            return client._foldername, client._backend
        finally:
            client.close()

    assert write_item(1) == (folder, "sqlite")
    assert pymongo.MongoClient is original
    assert write_item(2) == (folder, "sqlite")
    assert pymongo.MongoClient is original

    reader = tinymongo.TinyMongoClient(folder, backend="sqlite")
    try:
        assert reader.app.items.count_documents({}) == 2
    finally:
        reader.close()


def test_same_patch_object_can_be_nested_safely():
    original = pymongo.MongoClient
    patcher = tinymongo.patch()

    with patcher:
        outer_client_class = pymongo.MongoClient
        outer = pymongo.MongoClient()
        outer.app.items.insert_one({"_id": "outer"})

        with patcher:
            assert pymongo.MongoClient is not outer_client_class
            inner = pymongo.MongoClient()
            assert inner.app.items.find_one({"_id": "outer"}) is None
            inner.close()

        assert pymongo.MongoClient is outer_client_class
        assert outer.app.items.find_one({"_id": "outer"}) is not None
        outer.close()

    assert pymongo.MongoClient is original


def test_patch_scopes_must_exit_in_nested_order():
    original = pymongo.MongoClient
    outer = tinymongo.patch()
    inner = tinymongo.patch()
    outer_client = outer.__enter__()
    inner_client = inner.__enter__()

    try:
        with pytest.raises(RuntimeError, match="must exit in nested order"):
            outer.__exit__(None, None, None)
        assert pymongo.MongoClient is inner_client
    finally:
        inner.__exit__(None, None, None)
        assert pymongo.MongoClient is outer_client
        outer.__exit__(None, None, None)

    assert pymongo.MongoClient is original
    assert patching._patch_entries == []
    assert patching._patch_owner is None


def test_non_memory_patch_without_folder_uses_default_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with tinymongo.patch(backend="sqlite"):
        client = pymongo.MongoClient()
        try:
            assert client._foldername == "tinydb"
            assert client._backend == "sqlite"
        finally:
            client.close()


def test_patch_imports_pymongo_only_when_entered(monkeypatch):
    calls = []

    def missing_pymongo(name):
        calls.append(name)
        raise ModuleNotFoundError("No module named 'pymongo'", name="pymongo")

    monkeypatch.setattr(patching.importlib, "import_module", missing_pymongo)
    patcher = tinymongo.patch()

    assert calls == []
    with pytest.raises(ImportError, match="pip install pymongo"):
        with patcher:
            pass
    assert calls == ["pymongo"]


def test_overlapping_patch_scopes_in_different_threads_are_rejected():
    original = pymongo.MongoClient
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def hold_patch():
        with tinymongo.patch():
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold_patch)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(RuntimeError, match="cannot overlap across threads"):
            with tinymongo.patch():
                pass
    except Exception as error:  # pragma: no cover - diagnostic cleanup
        errors.append(error)
    finally:
        release.set()
        thread.join(timeout=5)

    assert not errors
    assert not thread.is_alive()
    assert pymongo.MongoClient is original


def test_overlapping_patch_scopes_in_different_async_tasks_are_rejected():
    original = pymongo.MongoClient

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_patch():
            async with tinymongo.patch():
                entered.set()
                await release.wait()

        async def attempt_overlap():
            await entered.wait()
            with pytest.raises(RuntimeError, match="cannot overlap"):
                async with tinymongo.patch():
                    pass
            release.set()

        await asyncio.gather(hold_patch(), attempt_overlap())

    asyncio.run(scenario())

    assert pymongo.MongoClient is original
    assert patching._patch_entries == []
    assert patching._patch_owner is None


def test_clients_created_by_patch_are_closed_at_scope_exit():
    with tinymongo.patch():
        client = pymongo.MongoClient()
        client.app.items.insert_one({"_id": 1})

    with pytest.raises(tinymongo.InvalidOperation, match="closed"):
        client.app.items.find_one({"_id": 1})


def test_async_decorators_fail_clearly():
    with pytest.raises(TypeError, match="does not decorate async functions"):

        @tinymongo.patch()
        async def async_test():
            return True


def test_patch_async_context_routes_and_restores_async_client():
    original_sync = pymongo.MongoClient
    original_async = pymongo.AsyncMongoClient

    async def scenario():
        async with tinymongo.patch() as patched_sync:
            assert pymongo.MongoClient is patched_sync
            assert pymongo.AsyncMongoClient is not original_async
            client = pymongo.AsyncMongoClient("mongodb://production.example")
            await client.app.items.insert_one({"_id": "async", "value": 42})
            assert await client.app.items.find_one({"_id": "async"}) == {
                "_id": "async",
                "value": 42,
            }
        assert client._state.closed is True

    asyncio.run(scenario())
    assert pymongo.MongoClient is original_sync
    assert pymongo.AsyncMongoClient is original_async


def test_patch_supports_pymongo_without_an_async_client(monkeypatch):
    """Older PyMongo releases only expose the synchronous client."""

    original_sync = pymongo.MongoClient
    monkeypatch.delattr(pymongo, "AsyncMongoClient")

    with tinymongo.patch() as patched_sync:
        assert pymongo.MongoClient is patched_sync
        assert not hasattr(pymongo, "AsyncMongoClient")

    assert pymongo.MongoClient is original_sync
    assert not hasattr(pymongo, "AsyncMongoClient")


def test_sync_patch_context_closes_async_clients():
    with tinymongo.patch():
        client = pymongo.AsyncMongoClient()
        assert client._state.closed is False

    assert client._state.closed is True


def test_async_patch_context_closes_sync_clients():
    async def scenario():
        async with tinymongo.patch():
            client = pymongo.MongoClient()
            client.app.items.insert_one({"_id": "sync-in-async"})

        with pytest.raises(tinymongo.InvalidOperation, match="closed"):
            client.app.items.find_one({"_id": "sync-in-async"})

    asyncio.run(scenario())


def test_patch_exit_without_entry_fails_clearly():
    with pytest.raises(RuntimeError, match="without being entered"):
        tinymongo.patch().__exit__(None, None, None)
