"""Non-blocking asyncio facade for TinyMongo's synchronous implementation.

The classes in this module deliberately keep the synchronous storage layer as
the single source of truth.  Every operation which can touch storage is run in
``asyncio.to_thread``.  Database and collection selection remain immediate,
as they are in PyMongo, and ``find`` returns a lazy cursor without doing I/O.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import InvalidOperation, TinyMongoNotSupportedError
from .storage_backends import get_storage_class
from .tinymongo import (
    _CompatibilityConcern,
    MongoClient,
    TinyMongoClient,
    TinyMongoCollection,
    TinyMongoCursor,
)


class _AsyncClientState:
    """Own a lazily-created synchronous client and coordinate its shutdown."""

    def __init__(self, factory: Callable[[], TinyMongoClient]):
        self._factory = factory
        self._client: Optional[TinyMongoClient] = None
        self._condition = threading.Condition()
        self._active_calls = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def _begin_call(self) -> TinyMongoClient:
        with self._condition:
            if self._closed:
                raise InvalidOperation("Cannot use a closed AsyncTinyMongoClient")
            if self._client is None:
                self._client = self._factory()
            self._active_calls += 1
            return self._client

    def _finish_call(self) -> None:
        with self._condition:
            self._active_calls -= 1
            if not self._active_calls:
                self._condition.notify_all()

    def _invoke(self, operation: Callable[[TinyMongoClient], Any]) -> Any:
        client = self._begin_call()
        try:
            return operation(client)
        finally:
            self._finish_call()

    async def call(self, operation: Callable[[TinyMongoClient], Any]) -> Any:
        """Run one complete synchronous operation outside the event loop."""

        return await asyncio.to_thread(self._invoke, operation)

    def _close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            while self._active_calls:
                self._condition.wait()
            client = self._client
            self._client = None
        if client is not None:
            client.close()

    async def close(self) -> None:
        """Wait for in-flight calls, then close storage outside the event loop."""

        await asyncio.to_thread(self._close)


class _AsyncClientBase:
    """Shared implementation for the native and PyMongo-shaped clients."""

    _sync_client_class: Any = TinyMongoClient
    _backend_position: Optional[int] = 1

    def __init__(self, *args: Any, **kwargs: Any):
        sync_client_class = self._sync_client_class
        if "backend" in kwargs:
            backend = kwargs["backend"]
        elif self._backend_position is not None and len(args) > self._backend_position:
            backend = args[self._backend_position]
        else:
            backend = "tinydb"
        # Validate without constructing the synchronous client, which keeps
        # database selection and all storage work lazy and off the event loop.
        get_storage_class(backend or "tinydb")
        self._state = _AsyncClientState(lambda: sync_client_class(*args, **kwargs))

    def __getitem__(self, name: str) -> "AsyncTinyMongoDatabase":
        return AsyncTinyMongoDatabase(self, name)

    def __getattr__(self, name: str) -> "AsyncTinyMongoDatabase":
        if name.startswith("_"):
            raise AttributeError(
                "{} object has no attribute {}".format(type(self).__name__, name)
            )
        return self[name]

    def get_database(
        self, name: str, *args: Any, **kwargs: Any
    ) -> "AsyncTinyMongoDatabase":
        """Return a database handle without opening storage."""

        return self[name]

    async def list_database_names(self, *args: Any, **kwargs: Any) -> List[str]:
        return await self._state.call(lambda client: client.list_database_names())

    async def list_databases(self, *args: Any, **kwargs: Any) -> "AsyncTinyMongoCursor":
        """Return database metadata through an asynchronously iterable cursor."""

        documents = await self._state.call(
            lambda client: client.list_databases(*args, **kwargs).to_list()
        )
        return AsyncTinyMongoCursor(documents=documents)

    async def drop_database(
        self, name_or_database: Any, *args: Any, **kwargs: Any
    ) -> None:
        """Drop a database without running storage work on the event loop."""

        if isinstance(name_or_database, AsyncTinyMongoDatabase):
            name_or_database = name_or_database.name
        await self._state.call(
            lambda client: client.drop_database(name_or_database, *args, **kwargs)
        )

    async def database_names(self, *args: Any, **kwargs: Any) -> List[str]:
        return await self.list_database_names(*args, **kwargs)

    async def server_info(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await self._state.call(lambda client: client.server_info())

    async def capabilities(self) -> Dict[str, Any]:
        return await self._state.call(lambda client: client.capabilities())

    async def supports(self, feature: str) -> bool:
        return await self._state.call(lambda client: client.supports(feature))

    def start_session(self, *args: Any, **kwargs: Any) -> Any:
        # PyMongo creates its async session handle synchronously.  TinyMongo
        # cannot provide sessions, so fail at the same call boundary.
        raise TinyMongoNotSupportedError(
            "Sessions and transactions are not supported by TinyMongo"
        )

    async def watch(self, *args: Any, **kwargs: Any) -> Any:
        return await self._state.call(lambda client: client.watch(*args, **kwargs))

    async def close(self) -> None:
        await self._state.close()

    async def __aenter__(self) -> "_AsyncClientBase":
        if self._state.closed:
            raise InvalidOperation("Cannot reuse a closed AsyncTinyMongoClient")
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_value: Optional[BaseException],
        traceback: Any,
    ) -> None:
        # Shield cleanup so cancellation of the surrounding task cannot leave
        # an opened local database behind.
        await asyncio.shield(self.close())


class AsyncTinyMongoClient(_AsyncClientBase):
    """Async counterpart to :class:`~tinymongo.TinyMongoClient`."""

    _sync_client_class = TinyMongoClient


class AsyncMongoClient(_AsyncClientBase):
    """PyMongo-shaped asynchronous client backed by local TinyMongo storage."""

    _sync_client_class = MongoClient
    _backend_position = None


class AsyncTinyMongoDatabase:
    """An immediate database handle whose operations are asynchronous."""

    def __init__(self, client: _AsyncClientBase, name: str):
        self.client = client
        self.name = name

    @property
    def database(self) -> str:
        """Compatibility alias for TinyMongo's synchronous database name."""

        return self.name

    def __repr__(self) -> str:
        return self.name

    def __getitem__(self, name: str) -> "AsyncTinyMongoCollection":
        return AsyncTinyMongoCollection(self, name)

    def __getattr__(self, name: str) -> "AsyncTinyMongoCollection":
        if name.startswith("_"):
            raise AttributeError(
                "{} object has no attribute {}".format(type(self).__name__, name)
            )
        return self[name]

    def get_collection(
        self, name: str, *args: Any, **kwargs: Any
    ) -> "AsyncTinyMongoCollection":
        """Return a collection handle without touching storage."""

        return self[name]

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        def operation(client: TinyMongoClient) -> Any:
            database = client[self.name]
            return getattr(database, method)(*args, **kwargs)

        return await self.client._state.call(operation)

    async def collection_names(self) -> List[str]:
        return await self._call("collection_names")

    async def list_collection_names(self, *args: Any, **kwargs: Any) -> List[str]:
        return await self._call("list_collection_names", *args, **kwargs)

    async def drop_collection(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(name, AsyncTinyMongoCollection):
            name = name.name
        return await self._call("drop_collection", name, *args, **kwargs)

    async def command(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("command", *args, **kwargs)

    async def watch(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("watch", *args, **kwargs)

    async def close(self) -> None:
        await self._call("close")

    async def __aenter__(self) -> "AsyncTinyMongoDatabase":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_value: Optional[BaseException],
        traceback: Any,
    ) -> None:
        await asyncio.shield(self.close())


class AsyncTinyMongoCollection:
    """Async collection facade with PyMongo-style immediate ``find``."""

    def __init__(self, database: AsyncTinyMongoDatabase, name: str):
        self.database = database
        self.name = name

    @property
    def tablename(self) -> str:
        """Compatibility alias for TinyMongo's synchronous collection name."""

        return self.name

    @property
    def full_name(self) -> str:
        return "{}.{}".format(self.database.name, self.name)

    @property
    def write_concern(self) -> _CompatibilityConcern:
        return _CompatibilityConcern()

    @property
    def read_concern(self) -> _CompatibilityConcern:
        return _CompatibilityConcern()

    def __repr__(self) -> str:
        return self.name

    def __getattr__(self, name: str) -> "AsyncTinyMongoCollection":
        """Return a dotted child collection selected by attribute."""

        if name.startswith("_"):
            full_name = "{}.{}".format(self.name, name)
            raise AttributeError(
                "{0} has no attribute {1!r}. To access the {2} collection, "
                "use database[{2!r}].".format(
                    type(self).__name__,
                    name,
                    full_name,
                )
            )
        return self[name]

    def __getitem__(self, name: str) -> "AsyncTinyMongoCollection":
        """Return a dotted child collection selected by subscription."""

        return AsyncTinyMongoCollection(
            self.database,
            "{}.{}".format(self.name, name),
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Explain method typos that resolved to a child collection."""

        if "." not in self.name:
            raise TypeError(
                "'{0}' object is not callable. If you meant to call the "
                "'{1}' method on an 'AsyncTinyMongoDatabase' object it is "
                "failing because no such method exists.".format(
                    type(self).__name__,
                    self.name,
                )
            )
        raise TypeError(
            "'{0}' object is not callable. If you meant to call the '{1}' "
            "method on a '{0}' object it is failing because no such method "
            "exists.".format(
                type(self).__name__,
                self.name.rsplit(".", 1)[-1],
            )
        )

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        def operation(client: TinyMongoClient) -> Any:
            collection = client[self.database.name][self.name]
            return getattr(collection, method)(*args, **kwargs)

        return await self.database.client._state.call(operation)

    def find(
        self,
        filter: Optional[Dict[str, Any]] = None,
        projection: Any = None,
        skip: Optional[int] = None,
        limit: Optional[int] = None,
        *args: Any,
        **kwargs: Any,
    ) -> "AsyncTinyMongoCursor":
        """Return a lazy async cursor without executing the query."""

        sort = kwargs.pop("sort", None)
        cursor = AsyncTinyMongoCursor(
            collection=self,
            filter=filter,
            projection=projection,
            find_args=args,
            find_kwargs=kwargs,
        )
        if sort is not None:
            cursor.sort(sort)
        if skip is not None:
            cursor.skip(skip)
        if limit is not None:
            cursor.limit(limit)
        return cursor

    async def find_one(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("find_one", *args, **kwargs)

    async def insert(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("insert", *args, **kwargs)

    async def insert_one(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("insert_one", *args, **kwargs)

    async def insert_many(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("insert_many", *args, **kwargs)

    async def update(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("update", *args, **kwargs)

    async def update_one(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("update_one", *args, **kwargs)

    async def update_many(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("update_many", *args, **kwargs)

    async def replace_one(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("replace_one", *args, **kwargs)

    async def find_one_and_update(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("find_one_and_update", *args, **kwargs)

    async def find_one_and_replace(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("find_one_and_replace", *args, **kwargs)

    async def find_one_and_delete(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("find_one_and_delete", *args, **kwargs)

    async def remove(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("remove", *args, **kwargs)

    async def delete_one(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("delete_one", *args, **kwargs)

    async def delete_many(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("delete_many", *args, **kwargs)

    async def count(self, *args: Any, **kwargs: Any) -> int:
        return await self._call("count", *args, **kwargs)

    async def count_documents(self, *args: Any, **kwargs: Any) -> int:
        return await self._call("count_documents", *args, **kwargs)

    async def estimated_document_count(self, *args: Any, **kwargs: Any) -> int:
        return await self._call("estimated_document_count", *args, **kwargs)

    async def create_index(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("create_index", *args, **kwargs)

    async def create_indexes(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate plural index creation when the sync surface provides it."""

        return await self._call("create_indexes", *args, **kwargs)

    async def drop_index(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("drop_index", *args, **kwargs)

    async def list_indexes(self, *args: Any, **kwargs: Any) -> "AsyncTinyMongoCursor":
        """Return index metadata through a PyMongo-shaped async cursor."""

        documents = await self._call("list_indexes", *args, **kwargs)
        return AsyncTinyMongoCursor(documents=documents)

    async def index_information(self, *args: Any, **kwargs: Any) -> Any:
        """Return index metadata in the same shape as the sync collection."""

        return await self._call("index_information", *args, **kwargs)

    async def distinct(self, *args: Any, **kwargs: Any) -> List[Any]:
        """Return the distinct values for a field without blocking the loop."""

        return await self._call("distinct", *args, **kwargs)

    async def drop(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("drop", *args, **kwargs)

    async def aggregate(self, *args: Any, **kwargs: Any) -> "AsyncTinyMongoCursor":
        """Run aggregation off-thread and return an async command cursor."""

        collection_handle = self

        def operation(client: TinyMongoClient) -> "AsyncTinyMongoCursor":
            collection = client[collection_handle.database.name][collection_handle.name]
            documents = collection.aggregate(*args, **kwargs).to_list()
            # Cursor construction deep-copies its seed, so keep that work in
            # the worker with storage and pipeline execution.
            return AsyncTinyMongoCursor(documents=documents)

        return await self.database.client._state.call(operation)

    async def bulk_write(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("bulk_write", *args, **kwargs)

    async def watch(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("watch", *args, **kwargs)

    def with_options(self, *args: Any, **kwargs: Any) -> "AsyncTinyMongoCollection":
        """Return this immutable-style handle after validating local concerns."""

        options = list(args) + [value for value in kwargs.values() if value is not None]
        for option in options:
            if getattr(option, "document", {}):
                raise TinyMongoNotSupportedError(
                    "Non-default read and write concerns are not supported"
                )
        return self


class AsyncTinyMongoCursor:
    """Lazy async cursor backed by one off-thread synchronous query."""

    def __init__(
        self,
        collection: Optional[AsyncTinyMongoCollection] = None,
        filter: Optional[Dict[str, Any]] = None,
        projection: Any = None,
        find_args: Tuple[Any, ...] = (),
        find_kwargs: Optional[Dict[str, Any]] = None,
        documents: Optional[List[Dict[str, Any]]] = None,
    ):
        self.collection = collection
        self._seed_documents = copy.deepcopy(documents)
        self._filter = copy.deepcopy(filter)
        self._projection = copy.deepcopy(projection)
        self._find_args = tuple(find_args)
        self._find_kwargs = dict(find_kwargs or {})
        self._sort: Optional[Tuple[Any, Any]] = None
        self._skip = 0
        self._limit = 0
        self._documents: Optional[List[Dict[str, Any]]] = None
        self._load_task: Optional["asyncio.Task[List[Dict[str, Any]]]"] = None
        self._position = 0
        self._closed = False

    def _check_mutable(self) -> None:
        if self._closed:
            raise InvalidOperation("Cannot modify a closed cursor")
        if self._load_task is not None or self._documents is not None:
            raise InvalidOperation("Cannot modify a cursor after it has started")

    def sort(self, key_or_list: Any, direction: Any = None) -> "AsyncTinyMongoCursor":
        self._check_mutable()
        # Reuse the synchronous cursor's pure validation logic now so malformed
        # specifications fail at configuration time, like PyMongo's cursor.
        TinyMongoCursor([]).sort(key_or_list, direction)
        self._sort = (copy.deepcopy(key_or_list), direction)
        return self

    def skip(self, count: int) -> "AsyncTinyMongoCursor":
        self._check_mutable()
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError("skip must be an integer")
        if count < 0:
            raise ValueError("skip must be non-negative")
        self._skip = count
        return self

    def limit(self, count: int) -> "AsyncTinyMongoCursor":
        self._check_mutable()
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError("limit must be an integer")
        # Negative PyMongo limits request a single batch.  Local storage has no
        # server batches, so the same absolute result bound is sufficient.
        self._limit = abs(count)
        return self

    def paginate(
        self, skip: Optional[int], limit: Optional[int]
    ) -> "AsyncTinyMongoCursor":
        """Configure the cursor window using TinyMongo's legacy helper."""

        self._check_mutable()
        if skip is not None:
            if isinstance(skip, bool) or not isinstance(skip, int):
                raise TypeError("skip must be an integer")
            if skip < 0:
                raise ValueError("skip must be non-negative")
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise TypeError("limit must be an integer")
        if skip is not None:
            self._skip = skip
        if limit is not None:
            self._limit = abs(limit)
        return self

    def clone(self) -> "AsyncTinyMongoCursor":
        clone = type(self)(
            collection=self.collection,
            filter=self._filter,
            projection=self._projection,
            find_args=self._find_args,
            find_kwargs=self._find_kwargs,
            documents=self._seed_documents,
        )
        clone._sort = copy.deepcopy(self._sort)
        clone._skip = self._skip
        clone._limit = self._limit
        return clone

    async def rewind(self) -> "AsyncTinyMongoCursor":
        if self._closed:
            raise InvalidOperation("Cannot rewind a closed cursor")
        self._position = 0
        return self

    def _load_sync(self) -> List[Dict[str, Any]]:
        if self._seed_documents is not None:
            cursor = TinyMongoCursor(copy.deepcopy(self._seed_documents))
            if self._sort is not None:
                cursor.sort(self._sort[0], self._sort[1])
            documents = list(cursor)
            if self._skip:
                documents = documents[self._skip :]
            if self._limit:
                documents = documents[: self._limit]
            return documents

        collection_handle = self.collection
        if collection_handle is None:  # pragma: no cover - constructor invariant
            raise InvalidOperation("Cursor has no collection or document source")
        state = collection_handle.database.client._state

        def operation(client: TinyMongoClient) -> List[Dict[str, Any]]:
            collection: TinyMongoCollection = client[collection_handle.database.name][
                collection_handle.name
            ]
            cursor = collection.find(
                self._filter,
                self._projection,
                None,
                None,
                *self._find_args,
                **self._find_kwargs,
            )
            if self._sort is not None:
                cursor.sort(self._sort[0], self._sort[1])
            documents = list(cursor)
            if self._skip:
                documents = documents[self._skip :]
            if self._limit:
                documents = documents[: self._limit]
            return documents

        return state._invoke(operation)

    async def _ensure_loaded(self) -> List[Dict[str, Any]]:
        if self._closed:
            return []
        if self._documents is not None:
            return self._documents
        if self._load_task is None:
            self._load_task = asyncio.create_task(asyncio.to_thread(self._load_sync))
        # Shield the shared load task.  Cancelling one waiter must not launch a
        # duplicate storage query or cancel work already running in a thread.
        documents = await asyncio.shield(self._load_task)
        self._documents = documents
        return documents

    @property
    def alive(self) -> bool:
        if self._closed:
            return False
        if self._documents is None and self._seed_documents is not None:
            start = min(self._skip, len(self._seed_documents))
            end = (
                len(self._seed_documents)
                if self._limit == 0
                else min(start + self._limit, len(self._seed_documents))
            )
            return self._position < end - start
        if self._documents is None:
            return True
        return self._position < len(self._documents)

    def __aiter__(self) -> "AsyncTinyMongoCursor":
        return self

    async def __aenter__(self) -> "AsyncTinyMongoCursor":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_value: Optional[BaseException],
        traceback: Any,
    ) -> None:
        await self.close()

    async def __anext__(self) -> Dict[str, Any]:
        documents = await self._ensure_loaded()
        if self._position >= len(documents):
            raise StopAsyncIteration
        document = documents[self._position]
        self._position += 1
        return copy.deepcopy(document)

    async def next(self) -> Dict[str, Any]:
        return await self.__anext__()

    async def try_next(self) -> Optional[Dict[str, Any]]:
        try:
            return await self.__anext__()
        except StopAsyncIteration:
            return None

    async def has_next(self) -> bool:
        """Return whether another document is available for consumption."""

        documents = await self._ensure_loaded()
        return self._position < len(documents)

    async def hasNext(self) -> bool:
        """Compatibility alias for older TinyMongo callers."""

        return await self.has_next()

    async def to_list(self, length: Optional[int] = None) -> List[Dict[str, Any]]:
        if length is not None:
            if isinstance(length, bool) or not isinstance(length, int):
                raise TypeError("length must be an integer or None")
            if length < 0:
                raise ValueError("length must be non-negative")
        documents = await self._ensure_loaded()
        end = len(documents) if length is None else self._position + length
        result = copy.deepcopy(documents[self._position : end])
        self._position += len(result)
        return result

    async def count(self, with_limit_and_skip: bool = False) -> int:
        documents = await self._ensure_loaded()
        return len(documents)

    async def close(self) -> None:
        self._closed = True
        if self._load_task is not None:
            try:
                await asyncio.shield(self._load_task)
            except Exception:
                # Closing a cursor should not replace an earlier query error.
                pass
        self._documents = []


# Short names are useful to callers that already distinguish the module as
# asynchronous, while the TinyMongo-prefixed names remain explicit and stable.
AsyncDatabase = AsyncTinyMongoDatabase
AsyncCollection = AsyncTinyMongoCollection
AsyncCursor = AsyncTinyMongoCursor


__all__ = [
    "AsyncCollection",
    "AsyncCursor",
    "AsyncDatabase",
    "AsyncMongoClient",
    "AsyncTinyMongoClient",
    "AsyncTinyMongoCollection",
    "AsyncTinyMongoCursor",
    "AsyncTinyMongoDatabase",
]
