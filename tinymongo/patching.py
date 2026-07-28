"""Temporarily route PyMongo client construction to TinyMongo."""

from contextlib import ContextDecorator
import asyncio
import importlib
import inspect
import threading
from uuid import uuid4

from .asyncio import AsyncMongoClient
from .storage_backends import clear_memory_namespace
from .tinymongo import MongoClient


_patch_state_lock = threading.RLock()
_patch_owner = None
_patch_entries: list[tuple] = []


def _patch_context_owner():
    """Identify the thread and, when applicable, the current asyncio task."""

    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


def _import_pymongo():
    """Import PyMongo only when patching is requested."""
    try:
        return importlib.import_module("pymongo")
    except ImportError as exc:
        raise ImportError(
            "tinymongo.patch requires the optional PyMongo package. "
            "Install it with: pip install pymongo"
        ) from exc


def _patch_folder(folder, backend):
    """Resolve storage for one patch entry and its optional cleanup target."""
    if folder is not None:
        return folder, None
    if str(backend).lower() == "memory":
        namespace = "memory://__tinymongo_patch__{0}".format(uuid4().hex)
        return namespace, namespace
    return "tinydb", None


def _configured_client(folder, backend, clients):
    """Build a class that forces the patch scope's TinyMongo configuration."""

    class ConfiguredMongoClient(MongoClient):
        def __init__(self, *args, **kwargs):
            options = dict(kwargs)
            options["backend"] = backend
            options["tinymongo_folder"] = folder
            super(ConfiguredMongoClient, self).__init__(*args, **options)
            clients.append(self)

    ConfiguredMongoClient.__name__ = "MongoClient"
    ConfiguredMongoClient.__qualname__ = "MongoClient"
    return ConfiguredMongoClient


def _configured_async_client(folder, backend, clients):
    """Build an async client class forced to the patch scope's storage."""

    class ConfiguredAsyncMongoClient(AsyncMongoClient):
        def __init__(self, *args, **kwargs):
            options = dict(kwargs)
            options["backend"] = backend
            options["tinymongo_folder"] = folder
            super(ConfiguredAsyncMongoClient, self).__init__(*args, **options)
            clients.append(self)

    ConfiguredAsyncMongoClient.__name__ = "AsyncMongoClient"
    ConfiguredAsyncMongoClient.__qualname__ = "AsyncMongoClient"
    return ConfiguredAsyncMongoClient


class _TinyMongoPatch(ContextDecorator):
    """Reusable context/decorator implementation for :func:`patch`."""

    def __init__(self, folder=None, backend="memory"):
        self.folder = folder
        self.backend = backend
        self._restore_stack = []

    def __enter__(self):
        global _patch_owner

        owner = _patch_context_owner()
        with _patch_state_lock:
            if _patch_owner is not None and _patch_owner != owner:
                raise RuntimeError(
                    "tinymongo.patch changes process-global PyMongo state and "
                    "cannot overlap across threads or async tasks"
                )

            clients = []
            async_clients = []
            pymongo = _import_pymongo()
            folder, cleanup_namespace = _patch_folder(self.folder, self.backend)
            original = pymongo.MongoClient
            replacement = _configured_client(folder, self.backend, clients)
            pymongo.MongoClient = replacement
            original_async = getattr(pymongo, "AsyncMongoClient", None)
            if original_async is not None:
                pymongo.AsyncMongoClient = _configured_async_client(
                    folder, self.backend, async_clients
                )
            entry = (
                self,
                pymongo,
                original,
                original_async,
                cleanup_namespace,
                clients,
                async_clients,
            )
            _patch_entries.append(entry)
            _patch_owner = owner
            self._restore_stack.append(entry)
            return replacement

    def _restore(self):
        global _patch_owner

        with _patch_state_lock:
            if not self._restore_stack or not _patch_entries:
                raise RuntimeError("tinymongo.patch scope exited without being entered")
            entry = self._restore_stack[-1]
            if _patch_entries[-1] is not entry:
                raise RuntimeError("tinymongo.patch scopes must exit in nested order")

            self._restore_stack.pop()
            _patch_entries.pop()
            (
                _,
                pymongo,
                original,
                original_async,
                cleanup_namespace,
                clients,
                async_clients,
            ) = entry
            pymongo.MongoClient = original
            if original_async is not None:
                pymongo.AsyncMongoClient = original_async
            if not _patch_entries:
                _patch_owner = None
        return cleanup_namespace, clients, async_clients

    @staticmethod
    def _cleanup_namespace(cleanup_namespace):
        if cleanup_namespace is not None:
            clear_memory_namespace(cleanup_namespace)

    def __exit__(self, exc_type, exc_value, traceback):
        cleanup_namespace, clients, async_clients = self._restore()
        for client in clients:
            client.close()
        for client in async_clients:
            client._state._close()
        self._cleanup_namespace(cleanup_namespace)
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_value, traceback):
        cleanup_namespace, clients, async_clients = self._restore()
        for client in clients:
            client.close()
        for client in async_clients:
            await client.close()
        self._cleanup_namespace(cleanup_namespace)
        return False

    def __call__(self, function):
        if inspect.iscoroutinefunction(function):
            raise TypeError(
                "tinymongo.patch does not decorate async functions; "
                "use a with block inside the async function"
            )
        return super(_TinyMongoPatch, self).__call__(function)


def patch(folder=None, backend="memory"):
    """Temporarily replace ``pymongo.MongoClient`` with TinyMongo.

    The default memory backend is isolated to one patch scope while clients
    created inside that scope share data. Pass ``folder`` and ``backend`` to
    select persistent storage instead. The returned object works as both a
    context manager and a decorator.
    """
    return _TinyMongoPatch(folder=folder, backend=backend)
