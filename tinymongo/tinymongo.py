"""Acts like a Pymongo client to TinyDB"""
# coding: utf-8

from __future__ import absolute_import

import copy
from functools import reduce
import logging
import os
from math import ceil
from uuid import uuid4

from tinydb import Query, TinyDB, where
from .storage_backends import (
    get_storage_class,
    get_table_backend,
    is_table_backend,
    storage_extension,
)
# from .results import InsertOneResult, InsertManyResult, UpdateResult, DeleteResult
# from .errors import DuplicateKeyError
from .results import InsertOneResult, InsertManyResult, UpdateResult, DeleteResult
from .errors import DuplicateKeyError
try:
    basestring
except NameError:
    basestring = str


logger = logging.getLogger(__name__)

ASCENDING = 1
DESCENDING = -1


def Q(query, key):
    return reduce(
        lambda partial_query, field: partial_query[field], key.split("."), query
    )


_MISSING = object()


def _get_nested(doc, path, default=_MISSING):
    current = doc
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _set_nested(doc, path, value):
    current = doc
    parts = path.split(".")
    for key in parts[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[parts[-1]] = value


def _unset_nested(doc, path):
    current = doc
    parts = path.split(".")
    for key in parts[:-1]:
        current = current.get(key)
        if not isinstance(current, dict):
            return
    current.pop(parts[-1], None)


def _apply_update_document(item, update_doc):
    updated = copy.deepcopy(item)
    operator_keys = [key for key in update_doc if key.startswith("$")]

    if not operator_keys:
        replacement = copy.deepcopy(update_doc)
        if u"_id" not in replacement:
            replacement[u"_id"] = item[u"_id"]
        return replacement

    for operator, changes in update_doc.items():
        if not isinstance(changes, dict):
            raise ValueError("{0} update requires a dict".format(operator))

        if operator == u"$set":
            for path, value in changes.items():
                _set_nested(updated, path, value)
        elif operator == u"$unset":
            for path in changes:
                _unset_nested(updated, path)
        elif operator == u"$inc":
            for path, value in changes.items():
                current = _get_nested(updated, path, 0)
                _set_nested(updated, path, current + value)
        elif operator == u"$push":
            for path, value in changes.items():
                current = _get_nested(updated, path, [])
                if not isinstance(current, list):
                    raise ValueError("$push target must be a list")
                current = list(current)
                current.append(value)
                _set_nested(updated, path, current)
        elif operator == u"$pull":
            for path, value in changes.items():
                current = _get_nested(updated, path, [])
                if not isinstance(current, list):
                    raise ValueError("$pull target must be a list")
                _set_nested(updated, path, [item for item in current if item != value])
        elif operator == u"$addToSet":
            for path, value in changes.items():
                current = _get_nested(updated, path, [])
                if not isinstance(current, list):
                    raise ValueError("$addToSet target must be a list")
                current = list(current)
                if value not in current:
                    current.append(value)
                _set_nested(updated, path, current)
        else:
            raise ValueError("Unsupported update operator: {0}".format(operator))

    updated[u"_id"] = item[u"_id"]
    return updated


def _is_operator_update(update_doc):
    return any(key.startswith("$") for key in update_doc)


def _simple_equality_filter(_filter):
    if not isinstance(_filter, dict) or len(_filter) != 1:
        return None
    field, value = next(iter(_filter.items()))
    if field.startswith("$") or isinstance(value, (dict, list)):
        return None
    return field, value


def _looks_like_network_target(host, port=None):
    if port is not None or isinstance(host, (list, tuple)):
        return True
    if not isinstance(host, str):
        return False

    target = host.strip()
    if "://" in target or "," in target:
        return True
    if target in ("localhost", "127.0.0.1", "::1"):
        return True
    if target.startswith("[") and "]" in target:
        return True
    return ":" in target and not os.path.isabs(target)


def _folder_from_mongo_client_args(host, port, kwargs):
    folder = (
        kwargs.pop("tinymongo_folder", None)
        or kwargs.pop("tinymongo_path", None)
        or kwargs.pop("foldername", None)
        or os.environ.get("TINYMONGO_HOME")
    )
    if folder is not None:
        return folder
    if host is None or _looks_like_network_target(host, port):
        return u"tinydb"
    return host


class TinyMongoClient(object):
    """Represents the Tiny `db` client"""

    def __init__(self, foldername=u"tinydb", backend="tinydb", **kwargs):
        """Initialize container folder and choose a storage backend."""
        self._foldername = foldername
        self._backend = backend or "tinydb"
        self._threads = kwargs.get("threads")
        try:
            os.makedirs(foldername, exist_ok=True)
        except OSError as x:
            logger.info("{}".format(x))

    @property
    def _storage(self):
        """Return the TinyDB storage class for the configured backend."""
        return get_storage_class(self._backend)

    def __getitem__(self, key):
        """Gets a new or existing database based in key"""
        return self._get_db(key)

    def _get_db_path(self, key):
        return os.path.join(self._foldername, key + storage_extension(self._backend))

    def _get_db(self, key):
        path = self._get_db_path(key)
        if is_table_backend(self._backend):
            engine_class = get_table_backend(self._backend)
            return TinyMongoDatabase(
                key, path, self._storage, engine=engine_class(path, self._threads)
            )
        return TinyMongoDatabase(key, path, self._storage)

    def close(self):
        """Do nothing"""
        pass

    def server_info(self):
        """Return local TinyMongo metadata in the shape of PyMongo's call."""
        return {
            "version": "tinymongo",
            "storage": self._backend,
            "localPath": self._foldername,
            "tinymongo": True,
        }

    def list_database_names(self):
        """Return database names found in the configured local storage folder."""
        extension = storage_extension(self._backend)
        if not os.path.isdir(self._foldername):
            return []
        names = []
        for filename in os.listdir(self._foldername):
            if filename.endswith(extension):
                names.append(filename[: -len(extension)])
        return sorted(names)

    def database_names(self):
        """Compatibility alias for older PyMongo versions."""
        return self.list_database_names()

    def __getattr__(self, name):
        """Gets a new or existing database based in attribute."""
        if name.startswith("_"):
            raise AttributeError("{} object has no attribute {}".format(type(self).__name__, name))
        return self._get_db(name)


class MongoClient(TinyMongoClient):
    """PyMongo-style client that stores data locally with TinyMongo.

    Network hosts, ports, and MongoDB URIs are accepted for drop-in ergonomics
    but ignored. Use ``tinymongo_folder`` or ``TINYMONGO_HOME`` to choose the
    local storage folder while leaving PyMongo-shaped code mostly unchanged.
    """

    def __init__(
        self,
        host=None,
        port=None,
        document_class=None,
        tz_aware=None,
        connect=None,
        type_registry=None,
        **kwargs
    ):
        backend = kwargs.pop("backend", "tinydb")
        foldername = _folder_from_mongo_client_args(host, port, kwargs)
        super(MongoClient, self).__init__(foldername=foldername, backend=backend)


class TinyMongoDatabase(object):
    """Representation of a Pymongo database"""

    def __init__(self, database, path, storage, engine=None):
        """Initialize a TinyDB file named as the db name in the given folder."""
        self.database = database
        self._path = path
        self._foldername = os.path.dirname(path) or "."
        self._storage = storage
        self.engine = engine
        self.tinydb = None if engine is not None else TinyDB(path, storage=storage)

    def _refresh_table(self):
        """Reload the TinyDB database from disk to pick up external writes."""
        if self.engine is not None:
            return
        try:
            self.tinydb.close()
        except Exception:
            pass
        self.tinydb = TinyDB(self._path, storage=self._storage)

    def __getattr__(self, name):
        """Gets a new or existing collection"""
        return TinyMongoCollection(name, self)

    def __getitem__(self, name):
        """Gets a new or existing collection"""
        return TinyMongoCollection(name, self)

    def collection_names(self):
        """Get a list of all the collection names in this database"""
        if self.engine is not None:
            return self.engine.list_collections()
        return list(self.tinydb.tables())

    def list_collection_names(self):
        """Compatibility alias for modern PyMongo."""
        return [name for name in self.collection_names() if name != "_default"]


class TinyMongoCollection(object):
    """
    This class represents a collection and all of the operations that are
    commonly performed on a collection
    """

    def __init__(self, table, parent=None):
        """
        Initilialize the collection

        :param table: the table name
        :param parent: the parent db name
        """
        self.tablename = table
        self.table = None
        self.parent = parent
        self._indexes = set()
        self._index_cache = {}

    def __repr__(self):
        """Return collection name"""
        return self.tablename

    def __getattr__(self, name):
        """
        If attr is not found return self
        :param name:
        :return:
        """
        # if self.table is None:
        #     self.tablename += u"." + name
        if self.table is None:
            self.build_table()
        return self

    def build_table(self):
        """
        Builds a new tinydb table at the parent database
        :return:
        """
        if self.parent.engine is not None:
            self.parent.engine.create_collection(self.tablename)
            return
        self.table = self.parent.tinydb.table(self.tablename)

    def _refresh_table(self):
        """Reload the TinyDB database from disk and reset the table object."""
        self.parent._refresh_table()
        self.table = self.parent.tinydb.table(self.tablename)
        self._index_cache = {}

    def create_index(self, key):
        """Create an in-memory equality index for this collection instance."""
        if self.parent.engine is not None:
            return self.parent.engine.create_index(self.tablename, key)
        self._indexes.add(key)
        self._index_cache.pop(key, None)
        return key

    def drop_index(self, key):
        """Drop an in-memory equality index for this collection instance."""
        if self.parent.engine is not None:
            return self.parent.engine.drop_index(self.tablename, key)
        self._indexes.discard(key)
        self._index_cache.pop(key, None)

    def list_indexes(self):
        """Return index metadata for this collection instance."""
        if self.parent.engine is not None:
            return self.parent.engine.list_indexes(self.tablename)
        indexes = [{"name": "_id_", "key": [("_id", 1)]}]
        for key in sorted(self._indexes):
            indexes.append({"name": "{0}_1".format(key), "key": [(key, 1)]})
        return indexes

    def _invalidate_indexes(self):
        self._index_cache = {}

    def _get_index(self, key):
        if key not in self._indexes:
            return None
        if key in self._index_cache:
            return self._index_cache[key]
        if self.table is None:
            self.build_table()
        index = {}
        for doc in self.table.all():
            value = _get_nested(doc, key)
            if value is _MISSING:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                try:
                    index.setdefault(item, []).append(doc)
                except TypeError:
                    continue
        self._index_cache[key] = index
        return index

    def _acquire_collection_lock(self):
        lock_path = os.path.join(self.parent._foldername, ".tinymongo.lock")
        try:
            from .parquet_storage import _local_rlocks, _acquire_rlock, portalocker
            import threading

            rlock = _local_rlocks.setdefault(lock_path, threading.RLock())
            first_acquire = _acquire_rlock(rlock)
            portalocker_lock = None
            if first_acquire and portalocker is not None:
                portalocker_lock = portalocker.Lock(lock_path, timeout=30)
                portalocker_lock.acquire()
            return rlock, portalocker_lock
        except Exception:
            return None, None

    def _release_collection_lock(self, rlock, portalocker_lock):
        if portalocker_lock is not None:
            try:
                portalocker_lock.release()
            except Exception:
                pass
        if rlock is not None:
            try:
                rlock.release()
            except Exception:
                pass

    def _set_storage_merge_writes(self, enabled):
        storage = getattr(self.parent.tinydb, "_storage", None)
        if hasattr(storage, "merge_writes"):
            storage.merge_writes = enabled

    def count(self):
        """
        Counts the documents in the collection.
        :return: Integer representing the number of documents in the collection.
        """
        return self.find().count()

    def count_documents(self, filter=None):
        """
        Counts the documents in the collection.
        :return: Integer representing the number of documents in the collection.
        """
        return self.find(filter).count()

    def drop(self, **kwargs):
        """
        Removes a collection from the database.
        **kwargs only because of the optional "writeConcern" field, but does nothing in the TinyDB database.
        :return: Returns True when successfully drops a collection. Returns False when collection to drop does not
        exist.
        """
        if self.parent.engine is not None:
            return self.parent.engine.drop_collection(self.tablename)
        if self.table:
            self._set_storage_merge_writes(False)
            try:
                drop_table = getattr(self.parent.tinydb, "drop_table", None)
                if drop_table is not None:
                    drop_table(self.tablename)
                else:
                    self.parent.tinydb.purge_table(self.tablename)
                self.table = None
                return True
            finally:
                self._set_storage_merge_writes(True)
            # from tinydb.database import TinyDB
            # TinyDB().
        else:
            return False

    def insert(self, docs, *args, **kwargs):
        """Backwards compatibility with insert"""
        if isinstance(docs, list):
            return self.insert_many(docs, *args, **kwargs)
        else:
            return self.insert_one(docs, *args, **kwargs)

    def insert_one(self, doc, *args, **kwargs):
        """
        Inserts one document into the collection
        If contains '_id' key it is used, else it is generated.
        :param doc: the document
        :return: InsertOneResult
        """
        if self.parent.engine is not None:
            if not isinstance(doc, dict):
                raise ValueError(u'"doc" must be a dict')
            if "_id" in doc and doc["_id"] is not None:
                _id = doc[u"_id"] = doc["_id"]
            else:
                _id = doc[u"_id"] = generate_id()
            result = self.parent.engine.insert_many(
                self.tablename,
                [doc],
                bypass_document_validation=kwargs.get("bypass_document_validation")
                is True,
            )
            return InsertOneResult(eid=result[0] if result else None, inserted_id=_id)

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            if self.table is None:
                self.build_table()
            self._refresh_table()
            if not isinstance(doc, dict):
                raise ValueError(u'"doc" must be a dict')

            # Respect explicit falsy ids (0, False) — only generate when _id
            # is missing or None.
            if "_id" in doc and doc["_id"] is not None:
                _id = doc[u"_id"] = doc["_id"]
            else:
                _id = doc[u"_id"] = generate_id()

            bypass_document_validation = kwargs.get("bypass_document_validation")
            if bypass_document_validation is True:
                # insert doc without validation of duplicated `_id`
                eid = self.table.insert(doc)
            else:
                existing = self.find_one({"_id": _id})
                if existing is None:
                    eid = self.table.insert(doc)
                else:
                    raise DuplicateKeyError(
                        u"_id:{0} already exists in collection:{1}".format(
                            _id, self.tablename
                        )
                    )

            self._invalidate_indexes()
            return InsertOneResult(eid=eid, inserted_id=_id)
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    def insert_many(self, docs, *args, **kwargs):
        """
        Inserts several documents into the collection
        :param docs: a list of documents
        :return: InsertManyResult
        """
        if self.parent.engine is not None:
            if not isinstance(docs, list):
                raise ValueError(u'"insert_many" requires a list input')
            _ids = []
            for doc in docs:
                if "_id" in doc and doc["_id"] is not None:
                    _id = doc[u"_id"] = doc["_id"]
                else:
                    _id = doc[u"_id"] = generate_id()
                _ids.append(_id)
            results = self.parent.engine.insert_many(
                self.tablename,
                docs,
                bypass_document_validation=kwargs.get("bypass_document_validation")
                is True,
            )
            return InsertManyResult(eids=results, inserted_ids=_ids)

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            if self.table is None:
                self.build_table()
            self._refresh_table()
            if not isinstance(docs, list):
                raise ValueError(u'"insert_many" requires a list input')

            bypass_document_validation = kwargs.get("bypass_document_validation")
            if bypass_document_validation is not True:
                # get all _id in once, to reduce I/O. (without projection)
                existing = [doc["_id"] for doc in self.find({})]

            _ids = list()
            for doc in docs:

                # Respect explicit falsy ids (0, False) — only generate when
                # _id is missing or None.
                if "_id" in doc and doc["_id"] is not None:
                    _id = doc[u"_id"] = doc["_id"]
                else:
                    _id = doc[u"_id"] = generate_id()

                if bypass_document_validation is not True:
                    if _id in existing:
                        raise DuplicateKeyError(
                            u"_id:{0} already exists in collection:{1}".format(
                                _id, self.tablename
                            )
                        )
                    existing.append(_id)

                _ids.append(_id)

            results = self.table.insert_multiple(docs)
            self._invalidate_indexes()

            return InsertManyResult(
                eids=[eid for eid in results],
                inserted_ids=[inserted_id for inserted_id in _ids],
            )
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    def parse_query(self, query):
        """
        Creates a tinydb Query() object from the query dict

        :param query: object containing the dictionary representation of the
        query
        :return: composite Query()
        """
        logger.debug(u"query to parse2: {}".format(query))

        # this should find all records
        if query == {} or query is None:
            return Query()._id != u"-1"  # noqa

        q = None
        # find the final result of the generator
        for c in self.parse_condition(query):
            if q is None:
                q = c
            else:
                q = q & c

        logger.debug(u"new query item2: {}".format(q))

        return q

    def parse_condition(self, query, prev_key=None, last_prev_key=None):
        """
        Creates a recursive generator for parsing some types of Query()
        conditions

        :param query: Query object
        :param prev_key: The key at the next-higher level
        :return: generator object, the last of which will be the complete
        Query() object containing all conditions
        """
        # use this to determine gt/lt/eq on prev_query
        logger.debug(u"query: {} prev_query: {}".format(query, prev_key))

        q = Query()
        conditions = None

        # deal with the {'name': value} case by injecting a previous key
        if not prev_key:
            temp_query = copy.deepcopy(query)
            k, v = temp_query.popitem()
            prev_key = k

        # deal with the conditions
        for key, value in query.items():
            logger.debug(u"conditions: {} {}".format(key, value))

            if key == u"$gte":
                conditions = (
                    (Q(q, prev_key) >= value)
                    if not conditions and prev_key != "$not"
                    else (conditions & (Q(q, prev_key) >= value))
                    if prev_key != "$not"
                    else (q[last_prev_key] < value)
                )
            elif key == u"$gt":
                conditions = (
                    (Q(q, prev_key) > value)
                    if not conditions and prev_key != "$not"
                    else (conditions & (Q(q, prev_key) > value))
                    if prev_key != "$not"
                    else (q[last_prev_key] <= value)
                )
            elif key == u"$lte":
                conditions = (
                    (Q(q, prev_key) <= value)
                    if not conditions and prev_key != "$not"
                    else (conditions & (Q(q, prev_key) <= value))
                    if prev_key != "$not"
                    else (q[last_prev_key] > value)
                )
            elif key == u"$lt":
                conditions = (
                    (Q(q, prev_key) < value)
                    if not conditions and prev_key != "$not"
                    else (conditions & (Q(q, prev_key) < value))
                    if prev_key != "$not"
                    else (q[last_prev_key] >= value)
                )
            elif key == u"$ne":
                conditions = (
                    (Q(q, prev_key) != value)
                    if not conditions and prev_key != "$not"
                    else (conditions & (Q(q, prev_key) != value))
                    if prev_key != "$not"
                    else (q[last_prev_key] == value)
                )
            elif key == u"$not":
                if not isinstance(value, dict) and not isinstance(value, list):
                    conditions = (
                        (Q(q, prev_key) != value)
                        if not conditions and prev_key != "$not"
                        else (conditions & (Q(q, prev_key) != value))
                        if prev_key != "$not"
                        else (q[last_prev_key] >= value)
                    )
                else:
                    # let the value's condition be parsed below
                    pass
            elif key == u"$regex":
                value = value.replace("\\\\\\", "|||")
                value = value.replace("\\\\", "|||")
                regex = value.replace("\\", "")
                regex = regex.replace("|||", "\\")
                currCond = where(prev_key).matches(regex)
                conditions = currCond if not conditions else (conditions & currCond)
            elif key == u"$nin":
                # Build a conjunctive condition: field != each value
                vals = value if isinstance(value, list) else [value]
                nin_cond = None
                for v in vals:
                    term = Q(q, prev_key) != v
                    nin_cond = term if nin_cond is None else (nin_cond & term)
                conditions = nin_cond if not conditions else (conditions & nin_cond)
            elif key == u"$exists":
                exists_cond = Q(q, prev_key).exists()
                if value:
                    conditions = exists_cond if not conditions else conditions & exists_cond
                else:
                    conditions = ~exists_cond if not conditions else conditions & ~exists_cond
            elif key in ["$and", "$or", "$nor", "$in", "$all"]:
                pass
            else:

                # don't want to use the previous key if this is a secondary key
                # (fixes multiple item query that includes $ codes)
                if not isinstance(value, dict) and not isinstance(value, list):
                    conditions = (
                        ((Q(q, key) == value) | (Q(q, key).any([value])))
                        if not conditions
                        else (
                            conditions
                            & ((Q(q, key) == value) | (Q(q, key).any([value])))
                        )
                    )
                    prev_key = key

            logger.debug(u"c: {}".format(conditions))
            if isinstance(value, dict):
                # yield from self.parse_condition(value, key)
                for parse_condition in self.parse_condition(value, key, prev_key):
                    yield parse_condition
            elif isinstance(value, list):
                if key == "$and":
                    grouped_conditions = None
                    for spec in value:
                        for parse_condition in self.parse_condition(spec):
                            grouped_conditions = (
                                parse_condition
                                if not grouped_conditions
                                else grouped_conditions & parse_condition
                            )
                    yield grouped_conditions
                elif key == "$or":
                    grouped_conditions = None
                    for spec in value:
                        for parse_condition in self.parse_condition(spec):
                            grouped_conditions = (
                                parse_condition
                                if not grouped_conditions
                                else grouped_conditions | parse_condition
                            )
                    yield grouped_conditions
                elif key == "$nor":
                    grouped_conditions = None
                    for spec in value:
                        for parse_condition in self.parse_condition(spec):
                            grouped_conditions = (
                                parse_condition
                                if not grouped_conditions
                                else grouped_conditions | parse_condition
                            )
                    yield ~grouped_conditions
                elif key == "$in":
                    # use `any` to find with list, before comparing to single string
                    grouped_conditions = Q(q, prev_key).any(value)
                    for val in value:
                        for parse_condition in self.parse_condition({prev_key: val}):
                            grouped_conditions = (
                                parse_condition
                                if not grouped_conditions
                                else grouped_conditions | parse_condition
                            )
                    yield grouped_conditions
                elif key == "$all":
                    yield Q(q, prev_key).all(value)
                elif isinstance(key, str) and key.startswith("$"):
                    if conditions is not None:
                        yield conditions
                    continue
                else:
                    yield Q(q, prev_key).any([value])
            else:
                yield conditions

    def update(self, query, doc, *args, **kwargs):
        """Backwards compatibility with update"""
        if isinstance(doc, list):
            return [self.update_one(query, item, *args, **kwargs) for item in doc]
        else:
            return self.update_many(query, doc, *args, **kwargs)

    def update_one(self, query, doc, *args, **kwargs):
        """
        Updates one element of the collection

        :param query: dictionary representing the mongo query
        :param doc: dictionary representing the item to be updated
        :return: UpdateResult
        """
        if self.parent.engine is not None:
            result = self.parent.engine.update_many(
                self.tablename, query, doc, multi=False
            )
            return UpdateResult(raw_result=result)

        if self.table is None:
            self.build_table()

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            self._refresh_table()
            allcond = self.parse_query(query)
            item = self.table.get(allcond)

            if item is None:
                return UpdateResult(raw_result=[])

            try:
                updated = _apply_update_document(item, doc)
                if _is_operator_update(doc):
                    result = self.table.update(updated, where(u"_id") == item[u"_id"])
                else:
                    self.table.remove(where(u"_id") == item[u"_id"])
                    self.table.insert(updated)
                    result = [item[u"_id"]]
                self._invalidate_indexes()
            except Exception:
                result = []

            return UpdateResult(raw_result=result)
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    def update_many(self, query, doc, *args, **kwargs):
        """
        Updates all elements matching the query

        :param query: dictionary representing the mongo query
        :param doc: dictionary or update document
        :return: UpdateResult
        """
        if self.parent.engine is not None:
            result = self.parent.engine.update_many(
                self.tablename, query, doc, multi=True
            )
            return UpdateResult(raw_result=result)

        if self.table is None:
            self.build_table()

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            self._refresh_table()
            allcond = self.parse_query(query)
            try:
                items = list(self.table.search(allcond))
                result = []
                for item in items:
                    updated = _apply_update_document(item, doc)
                    if _is_operator_update(doc):
                        result.extend(
                            self.table.update(updated, where(u"_id") == item[u"_id"])
                        )
                    else:
                        self.table.remove(where(u"_id") == item[u"_id"])
                        self.table.insert(updated)
                        result.append(item[u"_id"])
                self._invalidate_indexes()
            except Exception:
                result = []

            return UpdateResult(raw_result=result)
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    def replace_one(self, query, replacement, *args, **kwargs):
        """
        Replaces one document matching the query with the replacement document.
        """
        if self.parent.engine is not None:
            item = self.parent.engine.find_one(self.tablename, query)
            if item is None:
                return UpdateResult(raw_result=[])
            replacement[u"_id"] = item[u"_id"]
            self.parent.engine.replace_one(self.tablename, item[u"_id"], replacement)
            return UpdateResult(raw_result=[item[u"_id"]])

        if self.table is None:
            self.build_table()

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            self._refresh_table()
            allcond = self.parse_query(query)
            item = self.table.get(allcond)
            if item is None:
                return UpdateResult(raw_result=[])

            try:
                replacement[u"_id"] = item[u"_id"]
                self.table.remove(where(u"_id") == item[u"_id"])
                self.table.insert(replacement)
                self._invalidate_indexes()
                result = [item[u"_id"]]
            except Exception:
                result = []

            return UpdateResult(raw_result=result)
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    def find_one_and_update(self, query, update, *args, **kwargs):
        """
        Mimics MongoDB's findOneAndUpdate by returning the document before update.
        """
        if self.parent.engine is not None:
            item = self.parent.engine.find_one(self.tablename, query)
            if item is None:
                return None
            self.update_one(query, update, *args, **kwargs)
            return item

        if self.table is None:
            self.build_table()

        allcond = self.parse_query(query)
        item = self.table.get(allcond)
        if item is None:
            return None

        self.update_one(query, update, *args, **kwargs)
        return item

    def find(self, _filter=None, sort=None, skip=None, limit=None, *args, **kwargs):
        """
        Finds all matching results

        :param _filter: dictionary representing the mongo query
        :type _filter: Optional[dict]
        :return: cursor containing the search results
        """
        if self.parent.engine is not None:
            result = self.parent.engine.find(self.tablename, _filter)
            return TinyMongoCursor(result, sort=sort, skip=skip, limit=limit)

        if self.table is None:
            self.build_table()

        if _filter is None:
            result = self.table.all()
        else:
            simple = _simple_equality_filter(_filter)
            if simple is not None:
                key, value = simple
                index = self._get_index(key)
                if index is not None:
                    return TinyMongoCursor(
                        list(index.get(value, [])), sort=sort, skip=skip, limit=limit
                    )
            allcond = self.parse_query(_filter)

            try:
                result = self.table.search(allcond)
            except (AttributeError, TypeError):
                result = []

        result = TinyMongoCursor(result, sort=sort, skip=skip, limit=limit)

        return result

    def find_one(self, _filter=None):
        """
        Finds one matching query element

        :param query: dictionary representing the mongo query
        :return: the resulting document (if found)
        """
        if self.parent.engine is not None:
            return self.parent.engine.find_one(self.tablename, _filter)

        if self.table is None:
            self.build_table()

        allcond = self.parse_query(_filter)

        return self.table.get(allcond)

    def remove(self, spec_or_id, multi=True, *args, **kwargs):
        """Backwards compatibility with remove"""
        if multi:
            return self.delete_many(spec_or_id)
        return self.delete_one(spec_or_id)

    def delete_one(self, query):
        """
        Deletes one document from the collection

        :param query: dictionary representing the mongo query
        :return: DeleteResult
        """
        if self.parent.engine is not None:
            result = self.parent.engine.delete_many(
                self.tablename, query, multi=False
            )
            return DeleteResult(raw_result=result)

        item = self.find_one(query)
        self._set_storage_merge_writes(False)
        try:
            result = self.table.remove(where(u"_id") == item[u"_id"])
            self._invalidate_indexes()
        finally:
            self._set_storage_merge_writes(True)

        return DeleteResult(raw_result=result)

    def delete_many(self, query):
        """
        Removes all items matching the mongo query

        :param query: dictionary representing the mongo query
        :return: DeleteResult
        """
        if self.parent.engine is not None:
            result = self.parent.engine.delete_many(self.tablename, query, multi=True)
            return DeleteResult(raw_result=result)

        items = self.find(query)
        self._set_storage_merge_writes(False)
        try:
            result = [
                self.table.remove(where(u"_id") == item[u"_id"]) for item in items
            ]
            self._invalidate_indexes()

            if query == {}:
                # need to reset TinyDB's index for docs order consistency
                self.table._last_id = 0
        finally:
            self._set_storage_merge_writes(True)

        return DeleteResult(raw_result=result)


class TinyMongoCursor(object):
    """Mongo iterable cursor"""

    def __init__(self, cursordat, sort=None, skip=None, limit=None):
        """Initialize the mongo iterable cursor with data"""
        self.cursordat = cursordat
        self.cursorpos = -1

        if len(self.cursordat) == 0:
            self.currentrec = None
        else:
            self.currentrec = self.cursordat[self.cursorpos]

        if sort:
            self.sort(sort)

        self.paginate(skip, limit)

    def __getitem__(self, key):
        """Gets record by index or value by key"""
        if isinstance(key, int):
            return self.cursordat[key]
        return self.currentrec[key]

    def paginate(self, skip, limit):
        """Paginate list of records"""
        if not self.count() or not limit:
            return
        skip = skip or 0
        pages = int(ceil(self.count() / float(limit)))
        limits = {}
        last = 0
        for i in range(pages):
            current = limit * i
            limits[last] = current
            last = current
        # example with count == 62
        # {0: 20, 20: 40, 40: 60, 60: 62}
        if limit and limit < self.count():
            limit = limits.get(skip, self.count())
            self.cursordat = self.cursordat[skip:limit]

    def _order(self, value, is_reverse=None):
        """Parsing data to a sortable form
        By giving each data type an ID(int), and assemble with the value
        into a sortable tuple.
        """

        def _dict_parser(dict_doc):
            """ dict ordered by:
            valueType_N -> key_N -> value_N
            """
            result = list()
            for key in dict_doc:
                data = self._order(dict_doc[key])
                res = (data[0], key, data[1])
                result.append(res)
            return tuple(result)

        def _list_parser(list_doc):
            """list will iter members to compare
            """
            result = list()
            for member in list_doc:
                result.append(self._order(member))
            return result

        # (TODO) include more data type
        if value is None or not isinstance(
            # value, (dict, list, basestring, bool, float, int)
            value, (dict, list, str, bool, float, int)
        ):
            # not support/sortable value type
            value = (0, None)

        elif isinstance(value, bool):
            value = (5, value)

        elif isinstance(value, (int, float)):
            value = (1, value)

        # elif isinstance(value, basestring):
        elif isinstance(value, str):

            value = (2, value)

        elif isinstance(value, dict):
            value = (3, _dict_parser(value))

        elif isinstance(value, list):
            if len(value) == 0:
                # [] less then None
                value = [(-1, [])]
            else:
                value = _list_parser(value)

            if is_reverse is not None:
                # list will firstly compare with other doc by it's smallest
                # or largest member
                value = max(value) if is_reverse else min(value)
            else:
                # if the smallest or largest member is a list
                # then compaer with it's sub-member in list index order
                value = (4, tuple(value))

        return value

    def sort(self, key_or_list, direction=None):
        """
        Sorts a cursor object based on the input

        :param key_or_list: a list/tuple containing the sort specification,
        # i.e. ('user_number': -1), or a basestring
        i.e. ('user_number': -1), or a str
        :param direction: sorting direction, 1 or -1, needed if key_or_list
                          # is a basestring
                          is a str
        :return:
        """

        # checking input format

        sort_specifier = list()
        if isinstance(key_or_list, list):
            if direction is not None:
                raise ValueError(
                    "direction can not be set separately "
                    "if sorting by multiple fields."
                )
            for pair in key_or_list:
                if not (isinstance(pair, list) or isinstance(pair, tuple)):
                    raise TypeError("key pair should be a list or tuple.")
                if not len(pair) == 2:
                    raise ValueError("Need to be (key, direction) pair")
                # if not isinstance(pair[0], basestring):
                if not isinstance(pair[0], str):
                    raise TypeError("first item in each key pair must " "be a string")
                if not isinstance(pair[1], int) or not abs(pair[1]) == 1:
                    raise TypeError("bad sort specification.")

            sort_specifier = key_or_list

        # elif isinstance(key_or_list, basestring):
        elif isinstance(key_or_list, str):
            if direction is not None:
                if not isinstance(direction, int) or not abs(direction) == 1:
                    raise TypeError("bad sort specification.")
            else:
                # default ASCENDING
                direction = 1

            sort_specifier = [(key_or_list, direction)]

        else:
            raise ValueError(
                "Wrong input, pass a field name and a direction,"
                " or pass a list of (key, direction) pairs."
            )

        # sorting

        _cursordat = self.cursordat

        total = len(_cursordat)
        pre_sect_stack = list()
        for pair in sort_specifier:

            is_reverse = bool(1 - pair[1])
            value_stack = list()
            for index, data in enumerate(_cursordat):

                # get field value

                not_found = None
                for key in pair[0].split("."):
                    not_found = True

                    if isinstance(data, dict) and key in data:
                        data = copy.deepcopy(data[key])
                        not_found = False

                    elif isinstance(data, list):
                        if not is_reverse and len(data) == 1:
                            # MongoDB treat [{data}] as {data}
                            # when finding fields
                            if isinstance(data[0], dict) and key in data[0]:
                                data = copy.deepcopy(data[0][key])
                                not_found = False

                        elif is_reverse:
                            # MongoDB will keep finding field in reverse mode
                            for _d in data:
                                if isinstance(_d, dict) and key in _d:
                                    data = copy.deepcopy(_d[key])
                                    not_found = False
                                    break

                    if not_found:
                        break

                # parsing data for sorting

                if not_found:
                    # treat no match as None
                    data = None

                value = self._order(data, is_reverse)

                # read previous section
                pre_sect = pre_sect_stack[index] if pre_sect_stack else 0
                # inverse if in reverse mode
                # for keeping order as ASCENDING after sort
                pre_sect = (total - pre_sect) if is_reverse else pre_sect
                _ind = (total - index) if is_reverse else index

                value_stack.append((pre_sect, value, _ind))

            # sorting cursor data

            value_stack.sort(reverse=is_reverse)

            ordereddat = list()
            sect_stack = list()
            sect_id = -1
            last_dat = None
            for dat in value_stack:
                # restore if in reverse mode
                _ind = (total - dat[-1]) if is_reverse else dat[-1]
                ordereddat.append(_cursordat[_ind])

                # define section
                # maintain the sorting result in next level sorting
                if not dat[1] == last_dat:
                    sect_id += 1
                sect_stack.append(sect_id)
                last_dat = dat[1]

            # save result for next level sorting
            _cursordat = ordereddat
            pre_sect_stack = sect_stack

        # done

        self.cursordat = _cursordat

        return self

    def limit(self, n):
        self.cursordat = self.cursordat[:n]
        return self

    def has_next(self):
        """
        Returns True if the cursor has a next position, False if not
        :return:
        """
        cursor_pos = self.cursorpos + 1

        return cursor_pos + 1 < len(self.cursordat)

    def hasNext(self):
        """
        Returns True if the cursor has a next position, False if not
        :return:
        """
        cursor_pos = self.cursorpos + 1

        try:
            self.cursordat[cursor_pos]
            return True
        except IndexError:
            return False

    def next(self):
        """
        Returns the next record

        :return:
        """
        self.cursorpos += 1
        return self.cursordat[self.cursorpos]

    def count(self, with_limit_and_skip=False):
        """
        Returns the number of records in the current cursor

        :return: number of records
        """
        return len(self.cursordat)

    def __iter__(self):
        self.cursorpos = -1
        return self

    def __next__(self):
        """
        Returns the next record
        :return:
        """
        if not self.hasNext():
            raise StopIteration
        self.cursorpos += 1
        return self.cursordat[self.cursorpos]


class TinyGridFS(object):
    """GridFS for tinyDB"""

    def __init__(self, *args, **kwargs):
        self.database = None

    def grid_fs(self, tinydatabase):
        """TODO: Must implement yet"""
        self.database = tinydatabase
        return self

    def GridFS(self, tinydatabase):
        """TODO: Must implement yet"""
        self.database = tinydatabase
        return self

def generate_id():
    """Generate new UUID"""
    # TODO: Use six.string_type to Py3 compat
    return str(uuid4()).replace(u"-", u"")

# def generate_id():
#     """Generate new UUID"""
#     # TODO: Use six.string_type to Py3 compat
#     try:
#         return unicode(uuid1()).replace(u"-", u"")
#     except NameError:
#         return str(uuid1()).replace(u"-", u"")
