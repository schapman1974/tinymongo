"""Acts like a Pymongo client to TinyDB"""

# coding: utf-8

from __future__ import absolute_import

import copy
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import replace
from functools import reduce, wraps
import logging
import operator as comparison_operator
import os
import shutil
import threading
from uuid import uuid4

from tinydb import Query, TinyDB, where  # type: ignore[attr-defined]
from .bson_codec import storage_values_equal
from .bson_types import (
    add_bson_numbers,
    bson_identity_key,
    bson_number_decimal,
    bson_scalar_sort_key,
    bson_value_identity_key,
    bson_value_sort_key,
    bson_values_equal,
    decimal128_type,
    is_bson_regex,
    is_bson_number,
    object_id_type,
    supported_bson_types,
)
from .aggregation import AggregationEngine, aggregation_capabilities
from .sorting import bson_document_sort_value_key, sort_documents
from .warning_context import emit_warning
from .storage_backends import (
    clear_memory_database,
    clear_memory_namespace,
    get_storage_class,
    get_table_backend,
    is_remote_sql_backend,
    is_table_backend,
    join_storage_uri,
    list_memory_databases,
    storage_extension,
)

# from .results import InsertOneResult, InsertManyResult, UpdateResult, DeleteResult
# from .errors import DuplicateKeyError
from .results import InsertOneResult, InsertManyResult, UpdateResult, DeleteResult
from .errors import (
    BulkWriteError,
    DuplicateKeyError,
    InvalidDocument as InvalidDocument,
    InvalidOperation,
    OperationFailure,
    TinyMongoNotSupportedError,
    WriteError,
)
from .indexes import (
    INDEX_CATALOG_TABLE,
    IndexBatchPlan,
    IndexSpec,
    TinyMongoUnsupportedWarning,
    degraded_index_reuse_warning,
    emit_index_plan_warnings,
    index_catalog_id,
    index_spec_signature,
    parse_index_spec,
    plan_index_models,
    validate_unique_documents,
)
from .projection import normalize_projection, project_document
from .table_backends import (
    _filter_references_id,
    _value_matches,
    matches_filter,
    query_operator_capabilities,
    requires_python_filter,
    validate_filter_operators,
)

basestring = str


logger = logging.getLogger(__name__)

ASCENDING = 1
DESCENDING = -1


class _CompatibilityConcern(object):
    def __init__(self):
        self.document = {}


def Q(query, key):
    return reduce(
        lambda partial_query, field: partial_query[field], key.split("."), query
    )


_MISSING = object()
_DECIMAL128 = decimal128_type()


def _generate_document_id():
    """Return a PyMongo-shaped automatic ID when optional BSON is available."""

    object_id_class = object_id_type()
    if object_id_class is not None:
        return object_id_class()
    return generate_id()


def _get_nested(doc, path, default=_MISSING):
    current = doc
    for key in path.split("."):
        if not isinstance(current, Mapping) or key not in current:
            return default
        current = current[key]
    return current


def _bulk_write_details(n_inserted, write_errors):
    """Return the result document exposed by PyMongo's ``BulkWriteError``."""
    return {
        "writeErrors": write_errors,
        "writeConcernErrors": [],
        "nInserted": n_inserted,
        "nUpserted": 0,
        "nMatched": 0,
        "nModified": 0,
        "nRemoved": 0,
        "upserted": [],
    }


def _duplicate_write_error(collection, index, document, spec=None, error=None):
    """Describe one duplicate insert using PyMongo's bulk error shape."""
    field = "_id" if spec is None else spec.field
    index_name = "_id_" if spec is None else spec.name
    value = _get_nested(document, field)
    if value is _MISSING:
        value = None
    message = (
        "E11000 duplicate key error collection: {0} index: {1} "
        "dup key: {{ {2}: {3!r} }}"
    ).format(collection.full_name, index_name, field, value)
    if error is not None and str(error):
        message = "{0}: {1}".format(message, error)
    return {
        "index": index,
        "code": 11000,
        "errmsg": message,
        "keyPattern": {field: 1 if spec is None else spec.direction},
        "keyValue": {field: value},
        "op": document,
    }


def _first_unique_conflict(existing_documents, document, specs):
    """Return the first unique index violated by ``document``, if any."""
    for spec in specs:
        if not spec.unique:
            continue
        try:
            validate_unique_documents(existing_documents + [document], [spec])
        except DuplicateKeyError as error:
            return spec, error
    return None, None


def _plan_insert_many(collection, documents, existing_documents, specs, ordered):
    """Split a batch into inserts and duplicate-key write errors."""
    existing_ids = [document["_id"] for document in existing_documents]
    accepted = []
    write_errors = []

    for index, document in enumerate(documents):
        duplicate_error = None
        if any(bson_values_equal(document["_id"], value) for value in existing_ids):
            duplicate_error = _duplicate_write_error(collection, index, document)
        else:
            spec, error = _first_unique_conflict(existing_documents, document, specs)
            if spec is not None:
                duplicate_error = _duplicate_write_error(
                    collection,
                    index,
                    document,
                    spec=spec,
                    error=error,
                )

        if duplicate_error is not None:
            write_errors.append(duplicate_error)
            if ordered:
                break
        else:
            accepted.append(document)
            existing_documents.append(document)
            existing_ids.append(document["_id"])

    return accepted, write_errors


def _execute_engine_insert_many(
    collection,
    documents,
    ordered,
    bypass_document_validation,
):
    """Plan and execute a table-backend batch, retrying native races."""

    engine = collection.parent.engine
    last_error = None
    accepted = []
    write_errors = []

    # Remote SQL constraints are the final cross-process authority. If another
    # writer commits after our preflight, the native insert fails atomically;
    # re-read and re-plan so the public error still identifies the original
    # operation and preserves ordered/unordered semantics.
    for _attempt in range(3):
        existing_documents = engine.find(collection.tablename, {})
        accepted, write_errors = _plan_insert_many(
            collection,
            documents,
            existing_documents,
            engine.get_index_specs(collection.tablename),
            ordered,
        )
        if not accepted:
            return [], accepted, write_errors
        try:
            results = engine.insert_many(
                collection.tablename,
                accepted,
                bypass_document_validation=bypass_document_validation is True,
            )
        except DuplicateKeyError as error:
            last_error = error
            continue
        return list(results), accepted, write_errors

    # A native constraint continued to reject the batch without exposing a
    # newly committed conflicting row. Preserve the batch exception contract
    # with the earliest operation that reached the backend.
    conflict_document = accepted[0]
    conflict_index = next(
        index
        for index, document in enumerate(documents)
        if document is conflict_document
    )
    write_errors.append(
        _duplicate_write_error(
            collection,
            conflict_index,
            conflict_document,
            error=last_error,
        )
    )
    write_errors.sort(key=lambda item: item["index"])
    if ordered:
        write_errors = write_errors[:1]
    return [], [], write_errors


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


_UPDATE_OPERATORS = frozenset(("$set", "$unset", "$inc", "$push", "$pull", "$addToSet"))
_PUSH_MODIFIERS = frozenset(("$each", "$position", "$sort", "$slice"))
_ADD_TO_SET_MODIFIERS = frozenset(("$each",))
_PULL_COMPARISON_OPERATORS = {
    "$gt": comparison_operator.gt,
    "$gte": comparison_operator.ge,
    "$lt": comparison_operator.lt,
    "$lte": comparison_operator.le,
}
_PULL_FIELD_OPERATORS = frozenset(("$eq",) + tuple(_PULL_COMPARISON_OPERATORS))
_PULL_LOGICAL_OPERATORS = frozenset(("$and", "$or", "$nor"))


def _raise_write_error(message, code=2):
    """Raise a Mongo-shaped write error that remains a TinyMongoError."""

    raise WriteError(message, code=code)


def _is_modifier_document(value):
    return isinstance(value, Mapping) and any(
        isinstance(key, str) and key.startswith("$") for key in value
    )


def _is_modifier_integer(value):
    if isinstance(value, bool) or not is_bson_number(value):
        return False
    numeric = bson_number_decimal(value)
    return numeric.is_finite() and numeric == numeric.to_integral_value()


def _is_sort_direction(value):
    return (
        not isinstance(value, bool)
        and is_bson_number(value)
        and bson_number_decimal(value).is_finite()
        and bson_number_decimal(value) in (-1, 1)
    )


def _validate_each_modifier(value, operator, allowed_modifiers):
    """Validate an array modifier document and return whether one was used."""

    if not _is_modifier_document(value):
        return False

    unsupported = [key for key in value if key not in allowed_modifiers]
    if unsupported:
        _raise_write_error(
            "{0} does not support modifier {1}".format(operator, repr(unsupported[0]))
        )
    if "$each" not in value:
        _raise_write_error("{0} modifiers require $each".format(operator))
    if not isinstance(value["$each"], (list, tuple)):
        _raise_write_error("{0} $each requires an array".format(operator))
    return True


def _validate_push_operand(value):
    if not _validate_each_modifier(value, "$push", _PUSH_MODIFIERS):
        return False

    if "$position" in value and not _is_modifier_integer(value["$position"]):
        _raise_write_error("$push $position requires an integer")
    if "$slice" in value and not _is_modifier_integer(value["$slice"]):
        _raise_write_error("$push $slice requires an integer")
    if "$sort" in value:
        sort_spec = value["$sort"]
        if _is_sort_direction(sort_spec):
            return True
        if not isinstance(sort_spec, Mapping) or not sort_spec:
            _raise_write_error("$push $sort requires 1, -1, or a non-empty document")
        if not all(
            isinstance(field, str) and _is_sort_direction(direction)
            for field, direction in sort_spec.items()
        ):
            _raise_write_error("$push $sort contains an invalid sort specification")
    return True


def _validate_add_to_set_operand(value):
    return _validate_each_modifier(value, "$addToSet", _ADD_TO_SET_MODIFIERS)


def _validate_pull_field_condition(condition):
    """Validate the operators applied to one array element or document field."""

    if not isinstance(condition, Mapping):
        return

    operator_keys = [
        key for key in condition if isinstance(key, str) and key.startswith("$")
    ]
    if not operator_keys:
        return
    if len(operator_keys) != len(condition):
        _raise_write_error("$pull cannot mix field and document query operators")

    for key, operand in condition.items():
        if key not in _PULL_FIELD_OPERATORS:
            _raise_write_error("$pull does not support query operator {0}".format(key))
        if key in _PULL_COMPARISON_OPERATORS and (
            operand is None or bson_scalar_sort_key(operand) is None
        ):
            _raise_write_error(
                "$pull {0} requires a supported scalar value".format(key)
            )


def _validate_pull_document_condition(condition):
    """Validate a query applied to each embedded document in the array."""

    for key, operand in condition.items():
        if key in _PULL_LOGICAL_OPERATORS:
            if (
                not isinstance(operand, (list, tuple))
                or not operand
                or not all(isinstance(clause, Mapping) for clause in operand)
            ):
                _raise_write_error(
                    "$pull {0} requires an array of documents".format(key)
                )
            for clause in operand:
                _validate_pull_document_condition(clause)
        elif isinstance(key, str) and key.startswith("$"):
            _raise_write_error(
                "$pull does not support document query operator {0}".format(key)
            )
        else:
            _validate_pull_field_condition(operand)


def _validate_pull_condition(condition):
    """Reject unsupported query operators instead of silently matching nothing."""

    if not isinstance(condition, Mapping):
        return

    operator_keys = [
        key for key in condition if isinstance(key, str) and key.startswith("$")
    ]
    if any(key in _PULL_LOGICAL_OPERATORS for key in operator_keys):
        _validate_pull_document_condition(condition)
    elif operator_keys:
        _validate_pull_field_condition(condition)
    else:
        _validate_pull_document_condition(condition)


def _validate_update_operators(update_doc):
    for operator, changes in update_doc.items():
        if operator not in _UPDATE_OPERATORS:
            raise TinyMongoNotSupportedError(
                "Unsupported update operator: {0}".format(operator)
            )
        if not isinstance(changes, dict):
            raise ValueError("{0} update requires a dict".format(operator))
        if operator == "$push":
            for value in changes.values():
                _validate_push_operand(value)
        elif operator == "$addToSet":
            for value in changes.values():
                _validate_add_to_set_operand(value)
        elif operator == "$pull":
            for value in changes.values():
                _validate_pull_condition(value)
        elif operator == "$inc":
            for value in changes.values():
                if not is_bson_number(value):
                    _raise_write_error("$inc requires numeric values", code=14)


def _sort_pushed_values(values, sort_spec):
    """Sort array values using whole-value BSON ordering."""

    order = TinyMongoCursor([])._order
    if _is_sort_direction(sort_spec):
        values.sort(key=order, reverse=bson_number_decimal(sort_spec) < 0)
        return

    # Stable passes from the least-significant field implement a compound
    # sort while retaining the BSON ordering already shared by cursors.
    for field, direction in reversed(list(sort_spec.items())):
        values.sort(
            key=lambda item, path=field: order(_get_nested(item, path, None)),
            reverse=bson_number_decimal(direction) < 0,
        )


def _apply_push(current, value):
    if not _validate_push_operand(value):
        current.append(copy.deepcopy(value))
        return current

    additions = copy.deepcopy(list(value["$each"]))
    if "$position" not in value:
        current.extend(additions)
    else:
        position = int(bson_number_decimal(value["$position"]))
        if position < 0:
            position = max(len(current) + position, 0)
        else:
            position = min(position, len(current))
        current[position:position] = additions

    if "$sort" in value:
        _sort_pushed_values(current, value["$sort"])

    if "$slice" in value:
        limit = int(bson_number_decimal(value["$slice"]))
        if limit > 0:
            current = current[:limit]
        elif limit < 0:
            current = current[limit:]
        else:
            current = []
    return current


def _apply_add_to_set(current, value):
    candidates = value["$each"] if _validate_add_to_set_operand(value) else [value]
    for candidate in candidates:
        if not any(bson_values_equal(candidate, existing) for existing in current):
            current.append(copy.deepcopy(candidate))
    return current


def _pull_comparison_matches(actual, operand, comparison):
    operand_identity = bson_identity_key(operand)
    operand_order = bson_scalar_sort_key(operand)
    values = actual if isinstance(actual, (list, tuple)) else [actual]
    for value in values:
        value_identity = bson_identity_key(value)
        value_order = bson_scalar_sort_key(value)
        if (
            value_identity is not None
            and value_identity[0] == "number"
            and is_bson_number(value)
            and is_bson_number(operand)
            and (
                bson_number_decimal(value).is_nan()
                != bson_number_decimal(operand).is_nan()
            )
        ):
            continue
        if (
            value_identity is not None
            and value_order is not None
            and value_identity[0] == operand_identity[0]
            and comparison(value_order[1], operand_order[1])
        ):
            return True
    return False


def _pull_field_matches(actual, condition):
    if actual is _MISSING:
        return False
    if not isinstance(condition, Mapping) or not any(
        isinstance(key, str) and key.startswith("$") for key in condition
    ):
        return _value_matches(actual, condition)

    for query_operator, operand in condition.items():
        if query_operator == "$eq":
            if not _value_matches(actual, operand):
                return False
        elif not _pull_comparison_matches(
            actual, operand, _PULL_COMPARISON_OPERATORS[query_operator]
        ):
            return False
    return True


def _pull_document_matches(document, condition):
    for key, expected in condition.items():
        if key == "$and":
            if not all(_pull_document_matches(document, item) for item in expected):
                return False
        elif key == "$or":
            if not any(_pull_document_matches(document, item) for item in expected):
                return False
        elif key == "$nor":
            if any(_pull_document_matches(document, item) for item in expected):
                return False
        elif not _pull_field_matches(_get_nested(document, key), expected):
            return False
    return True


def _pull_matches(item, condition):
    if not isinstance(condition, Mapping):
        return bson_values_equal(item, condition)

    operator_keys = [
        key for key in condition if isinstance(key, str) and key.startswith("$")
    ]
    if any(key in _PULL_LOGICAL_OPERATORS for key in operator_keys):
        return isinstance(item, Mapping) and _pull_document_matches(item, condition)
    if operator_keys:
        return _pull_field_matches(item, condition)
    return isinstance(item, Mapping) and _pull_document_matches(item, condition)


def _apply_update_document(item, update_doc):
    updated = copy.deepcopy(item)
    operator_keys = [key for key in update_doc if key.startswith("$")]

    if not operator_keys:
        replacement = copy.deepcopy(update_doc)
        if "_id" not in replacement:  # pragma: no branch
            replacement["_id"] = item["_id"]
        return replacement

    _validate_update_operators(update_doc)

    for operator, changes in update_doc.items():
        if operator == "$set":
            for path, value in changes.items():
                _set_nested(updated, path, value)
        elif operator == "$unset":
            for path in changes:
                _unset_nested(updated, path)
        elif operator == "$inc":
            for path, value in changes.items():
                current = _get_nested(updated, path, 0)
                if not is_bson_number(current) or not is_bson_number(value):
                    _raise_write_error("$inc requires numeric values", code=14)
                _set_nested(updated, path, add_bson_numbers(current, value))
        elif operator == "$push":
            for path, value in changes.items():
                current = _get_nested(updated, path, [])
                if not isinstance(current, list):
                    raise ValueError("$push target must be a list")
                current = list(current)
                current = _apply_push(current, value)
                _set_nested(updated, path, current)
        elif operator == "$pull":
            for path, value in changes.items():
                current = _get_nested(updated, path, [])
                if not isinstance(current, list):
                    raise ValueError("$pull target must be a list")
                _set_nested(
                    updated,
                    path,
                    [item for item in current if not _pull_matches(item, value)],
                )
        elif operator == "$addToSet":  # pragma: no branch - operators prevalidated
            for path, value in changes.items():
                current = _get_nested(updated, path, [])
                if not isinstance(current, list):
                    _raise_write_error("Cannot apply $addToSet to non-array field")
                current = list(current)
                current = _apply_add_to_set(current, value)
                _set_nested(updated, path, current)

    updated["_id"] = item["_id"]
    return updated


def _update_document_modified(original, updated, update_doc):
    """Report MongoDB's write result for one applied update document."""

    if not storage_values_equal(updated, original):
        return True
    if _DECIMAL128 is None or "$inc" not in update_doc:
        return False

    # MongoDB reports an executed arithmetic update on a quiet Decimal128 NaN
    # as modified even though the resulting BID is byte-for-byte unchanged.
    return any(
        isinstance(current, _DECIMAL128) and current.to_decimal().is_qnan()
        for path in update_doc["$inc"]
        for current in (_get_nested(original, path),)
    )


def _tinydb_replace_document(replacement):
    """Return a TinyDB transform that writes an exact document post-image."""

    def replace(document):
        document.clear()
        document.update(copy.deepcopy(replacement))

    return replace


def _tinydb_id_condition(value):
    """Return a TinyDB condition with BSON-aware ``_id`` equality."""

    return where("_id").test(bson_values_equal, value)


def _direct_id_equality(filter_doc):
    """Return the operand for a direct exact ``_id`` query, if present."""

    if not isinstance(filter_doc, Mapping) or set(filter_doc) != {"_id"}:
        return _MISSING
    expected = filter_doc["_id"]
    if is_bson_regex(expected):
        return _MISSING
    if isinstance(expected, Mapping) and any(
        str(key).startswith("$") for key in expected
    ):
        if set(expected) != {"$eq"}:
            return _MISSING
        expected = expected["$eq"]
        if is_bson_regex(expected):
            return _MISSING
        return expected
    return expected


def _validate_update_document(update_doc):
    if (
        not isinstance(update_doc, dict)
        or not update_doc
        or not all(key.startswith("$") for key in update_doc)
    ):
        raise ValueError("update only works with $ operators; use replace_one instead")
    _validate_update_operators(update_doc)


def _reject_session(kwargs):
    if kwargs.get("session") is not None:
        raise TinyMongoNotSupportedError(
            "Sessions and transactions are not supported by TinyMongo"
        )


def _document_for_upsert(query, update_doc):
    document = {}
    for key, value in (query or {}).items():
        if key.startswith("$"):
            continue
        if isinstance(value, Mapping):
            if set(value) == {"$eq"}:
                value = value["$eq"]
            else:
                continue  # pragma: no cover - continue has no trace event on Python 3.9
        _set_nested(document, key, copy.deepcopy(value))
    if "_id" not in document:
        document["_id"] = _generate_document_id()
    return _apply_update_document(document, update_doc)


def _simple_equality_filter(_filter):
    if not isinstance(_filter, Mapping) or len(_filter) != 1:
        return None
    field, value = next(iter(_filter.items()))
    if (
        field.startswith("$")
        or value is None
        or isinstance(value, (Mapping, list, tuple))
        or is_bson_regex(value)
    ):
        return None
    return field, value


def _cached_value_matches(actual, expected, _cache_identity):
    """Match a TinyDB value while keeping BSON-distinct query-cache keys."""

    return _value_matches(actual, expected)


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
        return "tinydb"
    return host


def _memory_namespace(foldername):
    """Return an isolated or explicitly shared process-memory namespace."""
    address = str(foldername or "").strip()
    if "://" not in address:
        return "memory://__anonymous__{0}".format(uuid4().hex), False
    if not address.lower().startswith("memory://"):
        raise ValueError("Memory backend addresses must start with memory://")

    name = address[len("memory://") :]
    if not name or not all(
        character.isalnum() or character in "._-" for character in name
    ):
        raise ValueError(
            "Memory addresses must use a simple name such as memory://test-suite"
        )
    return "memory://{0}".format(name), True


def _engine_write_locked(method):
    """Hold a table backend's write lock across collection-level preflights."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        engine = getattr(getattr(self, "parent", None), "engine", None)
        if engine is None:
            return method(self, *args, **kwargs)
        with engine._write_lock():
            return method(self, *args, **kwargs)

    return wrapped


class TinyMongoClient(object):
    """Represents the Tiny `db` client"""

    def __init__(self, foldername="tinydb", backend="tinydb", **kwargs):
        """Initialize container folder and choose a storage backend."""
        self._foldername = foldername
        self._backend = backend or "tinydb"
        # Reject unknown names at construction instead of waiting for the first
        # database access, when the configuration error is harder to diagnose.
        get_storage_class(self._backend)
        backend_name = str(self._backend).lower()
        self._memory_namespace = None
        self._shared_memory = False
        if backend_name == "memory":
            self._memory_namespace, self._shared_memory = _memory_namespace(foldername)
            self._foldername = self._memory_namespace
        self._threads = kwargs.get("threads")
        storage_uri = kwargs.get("storage_uri")
        if backend_name in ("parquet", "parquetv2"):
            if storage_uri is None:
                storage_uri = os.environ.get("TINYMONGO_STORAGE_URI")
            self._storage_uri = storage_uri or None
        else:
            self._storage_uri = None
        self._duckdb_config = kwargs.get("duckdb_config")
        dsn = kwargs.get("dsn")
        if is_remote_sql_backend(backend_name):
            if dsn is None:
                dsn = self._dsn_from_env(backend_name)
            self._dsn = dsn or None
        else:
            self._dsn = None
        self._databases = {}
        self._databases_lock = threading.RLock()
        self._closed = False
        storage_is_nonlocal = is_remote_sql_backend(backend_name) or bool(
            self._storage_uri and backend_name in ("parquet", "parquetv2")
        )
        if self._memory_namespace is None and not storage_is_nonlocal:
            try:
                os.makedirs(foldername, exist_ok=True)
            except OSError as x:
                logger.info("{}".format(x))

    def _dsn_from_env(self, backend):
        backend = str(backend or "").lower()
        if backend in ("postgres", "postgresql"):
            return (
                os.environ.get("TINYMONGO_POSTGRES_DSN")
                or os.environ.get("TINYMONGO_POSTGRESQL_DSN")
                or os.environ.get("DATABASE_URL")
            )
        if backend in ("mysql", "mariadb"):
            return (
                os.environ.get("TINYMONGO_MYSQL_DSN")
                or os.environ.get("TINYMONGO_MARIADB_DSN")
                or os.environ.get("MYSQL_URL")
                or os.environ.get("MARIADB_URL")
            )
        return None

    @property
    def _storage(self):
        """Return the TinyDB storage class for the configured backend."""
        return get_storage_class(self._backend)

    def __getitem__(self, key):
        """Gets a new or existing database based in key"""
        return self._get_db(key)

    def get_database(self, name, *args, **kwargs):
        """Return a database handle while accepting compatibility options."""
        return self[name]

    def _get_db_path(self, key):
        if self._memory_namespace is not None:
            return self._memory_namespace.rstrip("/") + "/" + str(key)
        if is_remote_sql_backend(self._backend):
            return key
        if self._storage_uri and str(self._backend).lower() in ("parquet", "parquetv2"):
            return join_storage_uri(
                self._storage_uri, key + storage_extension(self._backend)
            )
        return os.path.join(self._foldername, key + storage_extension(self._backend))

    def _get_db(self, key):
        with self._databases_lock:
            self._ensure_open()
            if key in self._databases:
                return self._databases[key]
            path = self._get_db_path(key)
            if is_table_backend(self._backend):
                engine_class = get_table_backend(self._backend)
                database = TinyMongoDatabase(
                    key,
                    path,
                    self._storage,
                    engine=engine_class(
                        path,
                        threads=self._threads,
                        duckdb_config=self._duckdb_config,
                        database=key,
                        dsn=self._dsn,
                    ),
                )
            else:
                database = TinyMongoDatabase(key, path, self._storage)
            self._databases[key] = database
            return database

    def _ensure_open(self):
        """Reject operations that would use a client after it was closed."""

        if self._closed:
            raise InvalidOperation("Cannot use a closed TinyMongoClient")

    def close(self):
        """Close databases opened by this client."""
        with self._databases_lock:
            if self._closed:
                return
            for database in self._databases.values():
                database.close()
            self._databases.clear()
            if self._memory_namespace is not None and not self._shared_memory:
                clear_memory_namespace(self._memory_namespace)
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def server_info(self):
        """Return local TinyMongo metadata in the shape of PyMongo's call."""
        self._ensure_open()
        return {
            "version": "tinymongo",
            "storage": self._backend,
            "localPath": self._foldername,
            "storageUri": self._storage_uri,
            "dsnConfigured": bool(self._dsn),
            "tinymongo": True,
        }

    def capabilities(self):
        """Describe behavior that the configured backend can honor."""
        self._ensure_open()
        backend = str(self._backend or "tinydb").lower()
        remote = is_remote_sql_backend(backend)
        object_storage = bool(self._storage_uri and backend in ("parquet", "parquetv2"))
        return {
            "backend": backend,
            "persistent": backend != "memory",
            "remote_storage": remote,
            "object_storage": object_storage,
            "table_native": is_table_backend(backend),
            "multiprocess_writes": backend != "memory" and not object_storage,
            "native_indexes": backend
            in ("sqlite", "postgres", "postgresql", "mysql", "mariadb"),
            "projections": True,
            "bulk_writes": False,
            "aggregation": aggregation_capabilities(),
            "query_operators": query_operator_capabilities(),
            "sessions": False,
            "transactions": False,
            "change_streams": False,
            "bson_types": supported_bson_types(),
        }

    def supports(self, feature):
        """Return whether a named capability is available."""
        capabilities = self.capabilities()
        if feature not in capabilities or feature == "backend":
            raise ValueError("Unknown TinyMongo capability: {0}".format(feature))
        return bool(capabilities[feature])

    def start_session(self, *args, **kwargs):
        raise TinyMongoNotSupportedError(
            "Sessions and transactions are not supported by TinyMongo"
        )

    def watch(self, *args, **kwargs):
        raise TinyMongoNotSupportedError(
            "Change streams are not supported by TinyMongo"
        )

    def list_database_names(self):
        """Return database names found in the configured local storage folder."""
        self._ensure_open()
        if self._memory_namespace is not None:
            return list_memory_databases(self._memory_namespace)
        extension = storage_extension(self._backend)
        if is_remote_sql_backend(self._backend):
            engine_class = get_table_backend(self._backend)
            engine = engine_class(
                "",
                threads=self._threads,
                duckdb_config=self._duckdb_config,
                dsn=self._dsn,
            )
            return engine.list_databases()
        if self._storage_uri and str(self._backend).lower() in ("parquet", "parquetv2"):
            engine_class = get_table_backend(self._backend)
            engine = engine_class(
                self._storage_uri,
                threads=self._threads,
                duckdb_config=self._duckdb_config,
                dsn=self._dsn,
            )
            return engine.list_collections()
        if not os.path.isdir(self._foldername):
            return []
        names = []
        for filename in os.listdir(self._foldername):
            if filename.endswith(extension):
                names.append(filename[: -len(extension)])
        return sorted(names)

    def list_databases(self, *args, **kwargs):
        """Return PyMongo-shaped metadata for each embedded database."""
        _reject_session(kwargs)
        databases = []
        for name in self.list_database_names():
            path = self._get_db_path(name)
            size = 0
            if isinstance(path, str) and os.path.isfile(path):
                size = os.path.getsize(path)
            elif isinstance(path, str) and os.path.isdir(path):
                size = sum(
                    os.path.getsize(os.path.join(root, filename))
                    for root, _, filenames in os.walk(path)
                    for filename in filenames
                )
            database = self[name]
            databases.append(
                {
                    "name": name,
                    "sizeOnDisk": size,
                    "empty": not bool(database.list_collection_names()),
                }
            )
        return TinyMongoCursor(databases)

    def drop_database(self, name_or_database, *args, **kwargs):
        """Drop all collections and storage belonging to one database."""
        _reject_session(kwargs)
        name = getattr(name_or_database, "database", name_or_database)
        if not isinstance(name, str):
            raise TypeError("database name must be a string or TinyMongoDatabase")
        with self._databases_lock:
            self._ensure_open()
            if name not in self.list_database_names() and name not in self._databases:
                return None

            database = self._databases.pop(name, None)
            if database is None:
                database = self[name]
                self._databases.pop(name, None)
            for collection_name in list(database.list_collection_names()):
                database.drop_collection(collection_name)
            database.close()

            path = self._get_db_path(name)
            if self._memory_namespace is not None:
                clear_memory_database(path)
            elif not is_remote_sql_backend(self._backend) and not self._storage_uri:
                if os.path.isfile(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path)
        return None

    def database_names(self):
        """Compatibility alias for older PyMongo versions."""
        return self.list_database_names()

    def __getattr__(self, name):
        """Gets a new or existing database based in attribute."""
        if name.startswith("_"):
            raise AttributeError(
                "{} object has no attribute {}".format(type(self).__name__, name)
            )
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
        **kwargs,
    ):
        backend = kwargs.pop("backend", "tinydb")
        storage_uri = kwargs.pop("storage_uri", None)
        threads = kwargs.pop("threads", None)
        duckdb_config = kwargs.pop("duckdb_config", None)
        dsn = kwargs.pop("dsn", None)
        explicit_folder = (
            kwargs.get("tinymongo_folder")
            or kwargs.get("tinymongo_path")
            or kwargs.get("foldername")
            or os.environ.get("TINYMONGO_HOME")
        )
        foldername = _folder_from_mongo_client_args(host, port, kwargs)
        if (
            str(backend).lower() == "memory"
            and explicit_folder is None
            and isinstance(host, str)
            and "://" in host
        ):
            foldername = host
        super(MongoClient, self).__init__(
            foldername=foldername,
            backend=backend,
            storage_uri=storage_uri,
            threads=threads,
            duckdb_config=duckdb_config,
            dsn=dsn,
        )


class TinyMongoDatabase(object):
    """Representation of a Pymongo database"""

    def __init__(self, database, path, storage, engine=None):
        """Initialize a TinyDB file named as the db name in the given folder."""
        self.database = database
        self._path = path
        self._foldername = (
            path.rsplit("/", 1)[0]
            if str(path).startswith("memory://")
            else os.path.dirname(path) or "."
        )
        self._storage = storage
        self.engine = engine
        self.tinydb = None if engine is not None else TinyDB(path, storage=storage)
        self._memory_revision = self._current_memory_revision()

    @property
    def name(self):
        """Return the PyMongo-style database name."""
        return self.database

    def _current_memory_revision(self):
        storage = getattr(self.tinydb, "_storage", None)
        return getattr(storage, "revision", None)

    def _refresh_table(self):
        """Reload the TinyDB database from disk to pick up external writes."""
        if self.engine is not None:
            return
        try:
            self.tinydb.close()
        except Exception:
            pass
        self.tinydb = TinyDB(self._path, storage=self._storage)
        self._memory_revision = self._current_memory_revision()

    def __getattr__(self, name):
        """Gets a new or existing collection"""
        if name.startswith("_"):
            raise AttributeError(
                "{} object has no attribute {}".format(type(self).__name__, name)
            )
        return TinyMongoCollection(name, self)

    def __getitem__(self, name):
        """Gets a new or existing collection"""
        return TinyMongoCollection(name, self)

    def get_collection(self, name, *args, **kwargs):
        """Return a collection while accepting PyMongo compatibility options."""
        return self[name]

    def drop_collection(self, name, *args, **kwargs):
        """Drop a collection by name or collection object."""
        if isinstance(name, TinyMongoCollection):
            name = name.tablename
        return self[name].drop()

    def command(self, *args, **kwargs):
        raise TinyMongoNotSupportedError(
            "Database commands are not supported by TinyMongo"
        )

    def watch(self, *args, **kwargs):
        raise TinyMongoNotSupportedError(
            "Change streams are not supported by TinyMongo"
        )

    def collection_names(self):
        """Get a list of all the collection names in this database"""
        if self.engine is not None:
            names = self.engine.list_collections()
        else:
            names = list(self.tinydb.tables())
        return [
            name
            for name in names
            if name != "_default" and not name.startswith("__tinymongo_")
        ]

    def list_collection_names(self):
        """Compatibility alias for modern PyMongo."""
        return self.collection_names()

    def close(self):
        """Close this database's storage resources."""
        if self.engine is not None:
            self.engine.close()
        elif self.tinydb is not None:  # pragma: no branch
            self.tinydb.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


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
        self._index_specs = {}
        self._index_cache = {}
        self._memory_revision = None

    def __repr__(self):
        """Return collection name"""
        return self.tablename

    @property
    def name(self):
        """Return the PyMongo-style collection name."""
        return self.tablename

    @property
    def database(self):
        """Return this collection's parent database."""
        return self.parent

    @property
    def full_name(self):
        """Return the dotted database and collection name."""
        return "{0}.{1}".format(self.parent.name, self.tablename)

    def _validate_storage_document(self, document, index=None):
        """Reject unencodable values before a backend observes the write."""

        from .bson_codec import dumps as encode_storage_payload

        context = "collection {0!r}".format(self.full_name)
        if index is not None:
            context += ", document index {0}".format(index)
        if "_id" in document:
            context += ", document _id={0!r}".format(document["_id"])
        encode_storage_payload(
            document,
            document_context=context,
            ensure_ascii=False,
        )

    @property
    def write_concern(self):
        return _CompatibilityConcern()

    @property
    def read_concern(self):
        return _CompatibilityConcern()

    def with_options(self, *args, **kwargs):
        """Accept PyMongo collection options that local storage does not use."""
        options = list(args) + [value for value in kwargs.values() if value is not None]
        for option in options:
            document = getattr(option, "document", {})
            if document:
                raise TinyMongoNotSupportedError(
                    "Non-default read and write concerns are not supported"
                )
        return self

    def aggregate(self, pipeline, session=None, **kwargs):
        """Run TinyMongo's supported aggregation subset over this collection."""

        _reject_session({"session": session})
        options = list(kwargs)
        if options:
            raise TinyMongoNotSupportedError(
                "Aggregation option {0} is not supported by TinyMongo".format(
                    options[0]
                )
            )
        engine = AggregationEngine()
        prepared = engine.prepare(pipeline)
        if prepared and prepared[0][0] == "$match":
            documents = self.find(prepared[0][2])
            prepared = prepared[1:]
        else:
            documents = self.find({})
        return TinyMongoCursor(engine.run_prepared(documents, prepared))

    def bulk_write(self, *args, **kwargs):
        raise TinyMongoNotSupportedError("Bulk writes are not supported by TinyMongo")

    def watch(self, *args, **kwargs):
        raise TinyMongoNotSupportedError(
            "Change streams are not supported by TinyMongo"
        )

    def __getattr__(self, name):
        """Return a dotted child collection selected by attribute."""
        if name.startswith("_"):
            full_name = "{0}.{1}".format(self.tablename, name)
            raise AttributeError(
                "{0} has no attribute {1!r}. To access the {2} collection, "
                "use database[{2!r}].".format(
                    type(self).__name__,
                    name,
                    full_name,
                )
            )
        return self[name]

    def __getitem__(self, name):
        """Return a dotted child collection selected by subscription."""
        return TinyMongoCollection(
            "{0}.{1}".format(self.tablename, name),
            self.parent,
        )

    def __call__(self, *args, **kwargs):
        """Explain method typos that resolved to a child collection."""
        if "." not in self.tablename:
            raise TypeError(
                "'{0}' object is not callable. If you meant to call the "
                "'{1}' method on a 'TinyMongoDatabase' object it is failing "
                "because no such method exists.".format(
                    type(self).__name__,
                    self.tablename,
                )
            )
        raise TypeError(
            "'{0}' object is not callable. If you meant to call the '{1}' "
            "method on a '{0}' object it is failing because no such method "
            "exists.".format(
                type(self).__name__,
                self.tablename.rsplit(".", 1)[-1],
            )
        )

    def build_table(self):
        """
        Builds a new tinydb table at the parent database
        :return:
        """
        if self.parent.engine is not None:
            self.parent.engine.create_collection(self.tablename)
            return
        self.table = self.parent.tinydb.table(self.tablename)
        self._load_durable_indexes()
        self._remember_memory_revision()

    def _refresh_table(self):
        """Reload the TinyDB database from disk and reset the table object."""
        self.parent._refresh_table()
        self.table = self.parent.tinydb.table(self.tablename)
        self._index_cache = {}
        self._load_durable_indexes()
        self._remember_memory_revision()

    def _remember_memory_revision(self):
        self._memory_revision = self.parent._memory_revision

    def _refresh_stale_memory_table(self):
        """Reset TinyDB query caches after another memory client writes."""
        storage = getattr(self.parent.tinydb, "_storage", None)
        revision = getattr(storage, "revision", None)
        if revision is not None and revision != self._memory_revision:
            self._refresh_table()

    def _load_durable_indexes(self):
        if INDEX_CATALOG_TABLE in self.parent.tinydb.tables():
            catalog = self.parent.tinydb.table(INDEX_CATALOG_TABLE)
            specs = []
            for document in catalog.all():
                if document.get("collection") == self.tablename:
                    specs.append(IndexSpec.from_metadata(document["spec"]))
        else:
            specs = []
        self._index_specs = {spec.name: spec for spec in specs}
        self._indexes = {spec.field for spec in specs}

    def _index_document(self, spec):
        return {
            "_id": index_catalog_id(self.tablename, spec.name),
            "collection": self.tablename,
            "spec": spec.to_metadata(),
        }

    def _find_index_spec(self, name_or_field):
        for spec in self._index_specs.values():
            if name_or_field in (spec.name, spec.field):
                return spec
        return None

    def _validate_index_compatibility(self, spec):
        by_name = self._index_specs.get(spec.name)
        if by_name is not None and by_name != spec:
            raise OperationFailure(
                "An index with the same name or key has different options"
            )
        if by_name is not None:
            # Preserve idempotent retries for legacy catalogs which may still
            # contain an equivalent index under another name.
            return by_name
        equivalent = next(
            (
                current
                for current in self._index_specs.values()
                if index_spec_signature(current) == index_spec_signature(spec)
            ),
            None,
        )
        if equivalent is not None and equivalent.name != spec.name:
            raise OperationFailure(
                "An index with the same key and options already exists under "
                "a different name",
                code=85,
            )
        return None

    def _current_index_specs(self):
        """Return refreshed user-created index specs for batch planning."""
        if self.parent.engine is not None:
            return tuple(self.parent.engine.get_index_specs(self.tablename))

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            if self.table is None:
                self.build_table()
            self._refresh_table()
            return tuple(self._index_specs.values())
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    def _validate_unique_post_image(self, documents, extra_specs=()):
        specs = list(self._index_specs.values()) + list(extra_specs)
        validate_unique_documents(documents, specs)

    @_engine_write_locked
    def create_index(self, key, *args, **kwargs):
        """Create a durable single-field ascending equality index."""
        if args:
            raise TinyMongoNotSupportedError(
                "Only single-field ascending equality indexes are supported"
            )
        spec = parse_index_spec(key, **kwargs)
        if spec.field == "_id":
            if "unique" in kwargs:
                raise OperationFailure(
                    "The unique option is not valid for the built-in _id index",
                    code=197,
                )
            return "_id_"
        if self.parent.engine is not None:
            return self.parent.engine.create_index(self.tablename, spec)

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            if self.table is None:
                self.build_table()
            self._refresh_table()
            existing = self._validate_index_compatibility(spec)
            if existing is not None:
                return existing.name
            if spec.unique:
                self._validate_unique_post_image(self.table.all(), [spec])
            self.parent.tinydb.table(INDEX_CATALOG_TABLE).insert(
                self._index_document(spec)
            )
            self._index_specs[spec.name] = spec
            self._indexes.add(spec.field)
            self._index_cache.pop(spec.field, None)
            return spec.name
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    @_engine_write_locked
    def create_indexes(self, indexes, *args, **kwargs):
        """Create a validated batch of PyMongo-style index models."""
        if args:
            raise TypeError("create_indexes accepts one iterable of index models")
        _reject_session(kwargs)
        batch = plan_index_models(indexes)
        if self.parent.engine is not None:
            return self._create_indexes_locked(batch)

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            return self._create_indexes_locked(batch)
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    def _create_indexes_locked(self, batch):
        """Plan and create a validated batch while holding its storage lock."""
        effective = {
            index_spec_signature(spec): spec for spec in self._current_index_specs()
        }
        resolved_entries = []
        names = []
        for entry in batch:
            if entry.spec is None:
                resolved_entries.append(entry)
                names.append(entry.name)
                continue
            signature = index_spec_signature(entry.spec)
            existing = effective.get(signature)
            if (
                existing is not None
                and existing.name != entry.spec.name
                and entry.degraded_features
            ):
                resolved_entries.append(
                    replace(
                        entry,
                        warning=degraded_index_reuse_warning(entry, existing),
                    )
                )
                names.append(existing.name)
                continue
            options = {"name": entry.spec.name}
            if entry.spec.unique:
                options["unique"] = True
            name = self.create_index([(entry.spec.field, ASCENDING)], **options)
            effective[signature] = replace(entry.spec, name=name)
            resolved_entries.append(entry)
            names.append(name)
        emit_index_plan_warnings(
            IndexBatchPlan(tuple(resolved_entries)),
            stacklevel=3,
        )
        return names

    @_engine_write_locked
    def drop_index(self, key):
        """Drop a durable index by its name or legacy field name."""
        if key in ("_id", "_id_"):
            raise OperationFailure("The _id index cannot be dropped")
        if self.parent.engine is not None:
            return self.parent.engine.drop_index(self.tablename, key)

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            if self.table is None:
                self.build_table()
            self._refresh_table()
            spec = self._find_index_spec(key)
            if spec is None:
                raise OperationFailure("Index not found: {0}".format(key))
            catalog = self.parent.tinydb.table(INDEX_CATALOG_TABLE)
            self._set_storage_merge_writes(False)
            try:
                catalog.remove(where("_id") == self._index_document(spec)["_id"])
            finally:
                self._set_storage_merge_writes(True)
            self._index_specs.pop(spec.name, None)
            if not any(
                current.field == spec.field for current in self._index_specs.values()
            ):
                self._indexes.discard(spec.field)
            self._index_cache.pop(spec.field, None)
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    def list_indexes(self):
        """Return durable index metadata for this collection."""
        if self.parent.engine is not None:
            return self.parent.engine.list_indexes(self.tablename)
        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            if self.table is None:
                self.build_table()
            self._refresh_table()
            indexes = [{"name": "_id_", "key": [("_id", 1)]}]
            for spec in sorted(self._index_specs.values(), key=lambda item: item.name):
                metadata = {"name": spec.name, "key": [(spec.field, spec.direction)]}
                if spec.unique:
                    metadata["unique"] = True
                indexes.append(metadata)
            return indexes
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    def index_information(self):
        """Return durable index metadata keyed by index name."""
        return {
            metadata["name"]: {
                key: copy.deepcopy(value)
                for key, value in metadata.items()
                if key != "name"
            }
            for metadata in self.list_indexes()
        }

    def _invalidate_indexes(self):
        self._index_cache = {}

    def _get_index(self, key):
        if key not in self._indexes:
            return None
        if key in self._index_cache:
            return self._index_cache[key]
        index = {}
        for doc in self.table.all():
            value = _get_nested(doc, key)
            if value is _MISSING:
                continue
            values = value if isinstance(value, list) else [value]
            seen = set()
            for item in values:
                try:
                    identity = bson_identity_key(item)
                    cache_key = (
                        ("__tinymongo_bson_identity__", identity)
                        if identity is not None
                        else item
                    )
                    if cache_key in seen:
                        continue
                    seen.add(cache_key)
                    index.setdefault(cache_key, []).append(doc)
                except TypeError:
                    continue
        self._index_cache[key] = index
        return index

    def _acquire_memory_collection_lock(self):
        storage = getattr(self.parent.tinydb, "_storage", None)
        memory_lock = getattr(storage, "collection_lock", None)
        if memory_lock is not None:
            memory_lock.acquire()
            return memory_lock, None
        return None, None

    def _acquire_collection_lock(self):
        memory_lock, portalocker_lock = self._acquire_memory_collection_lock()
        if memory_lock is not None:
            return memory_lock, portalocker_lock

        lock_path = os.path.join(self.parent._foldername, ".tinymongo.lock")
        from .parquet_storage import _local_rlocks, _acquire_rlock, portalocker
        import threading

        rlock = _local_rlocks.setdefault(lock_path, threading.RLock())
        first_acquire = _acquire_rlock(rlock)
        portalocker_lock = None
        try:
            if first_acquire and portalocker is not None:  # pragma: no branch
                portalocker_lock = portalocker.Lock(lock_path, timeout=30)
                portalocker_lock.acquire()
            return rlock, portalocker_lock
        except Exception:
            rlock.release()
            raise

    def _release_collection_lock(self, rlock, portalocker_lock):
        if portalocker_lock is not None:  # pragma: no branch
            try:
                portalocker_lock.release()
            except Exception:
                pass
        if rlock is not None:  # pragma: no branch
            try:
                rlock.release()
            except Exception:
                pass

    def _set_storage_merge_writes(self, enabled):
        storage = getattr(self.parent.tinydb, "_storage", None)
        if hasattr(storage, "merge_writes"):  # pragma: no branch
            storage.merge_writes = enabled

    def count(self):
        """
        Counts the documents in the collection.
        :return: Integer representing the number of documents in the collection.
        """
        return self.find().count()

    def count_documents(self, filter=None, *args, **kwargs):
        """
        Counts the documents in the collection.
        :return: Integer representing the number of documents in the collection.
        """
        _reject_session(kwargs)
        return self.find(filter).count()

    def estimated_document_count(self, *args, **kwargs):
        """Return the local collection size."""
        return self.count_documents({})

    @_engine_write_locked
    def drop(self, **kwargs):
        """
        Removes a collection from the database.
        **kwargs only because of the optional "writeConcern" field, but does nothing in the TinyDB database.
        :return: Returns True when successfully drops a collection. Returns False when collection to drop does not
        exist.
        """
        _reject_session(kwargs)
        if self.parent.engine is not None:
            return self.parent.engine.drop_collection(self.tablename)
        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            self.parent._refresh_table()
            if self.tablename not in self.parent.collection_names():
                return False
            self.table = self.parent.tinydb.table(self.tablename)
            self._load_durable_indexes()
            self._set_storage_merge_writes(False)
            try:
                drop_table = getattr(self.parent.tinydb, "drop_table", None)
                if drop_table is not None:
                    drop_table(self.tablename)
                else:
                    self.parent.tinydb.purge_table(self.tablename)
                if INDEX_CATALOG_TABLE in self.parent.tinydb.tables():
                    self.parent.tinydb.table(INDEX_CATALOG_TABLE).remove(
                        where("collection") == self.tablename
                    )
                self.table = None
                self._index_specs = {}
                self._indexes = set()
                self._index_cache = {}
                return True
            finally:
                self._set_storage_merge_writes(True)
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    def insert(self, docs, *args, **kwargs):
        """Backwards compatibility with insert"""
        if isinstance(docs, list):
            return self.insert_many(docs, *args, **kwargs)
        else:
            return self.insert_one(docs, *args, **kwargs)

    @_engine_write_locked
    def insert_one(self, doc, *args, **kwargs):
        """
        Inserts one document into the collection
        If contains '_id' key it is used, else it is generated.
        :param doc: the document
        :return: InsertOneResult
        """
        _reject_session(kwargs)
        if not isinstance(doc, dict):
            raise ValueError('"doc" must be a dict')

        # PyMongo generates an ID only when the field is absent. Explicit
        # values such as 0, False, and None remain user-controlled IDs.
        if "_id" in doc:
            _id = doc["_id"]
        else:
            _id = doc["_id"] = _generate_document_id()
        self._validate_storage_document(doc)

        if self.parent.engine is not None:
            self.parent.engine.validate_unique_post_image(
                self.tablename,
                self.parent.engine.find(self.tablename, {}) + [doc],
            )
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

            # A bare regex is a query predicate, but an ``_id`` collision is
            # always exact BSON identity. Looking it up through ``find_one``
            # could otherwise confuse a regex ID with a matching string ID.
            documents = self.table.all()
            existing = next(
                (item for item in documents if bson_values_equal(item.get("_id"), _id)),
                None,
            )
            if existing is None:
                self._validate_unique_post_image(documents + [doc])
                eid = self.table.insert(doc)
            else:
                raise DuplicateKeyError(
                    "_id:{0} already exists in collection:{1}".format(
                        _id, self.tablename
                    )
                )

            self._invalidate_indexes()
            return InsertOneResult(eid=eid, inserted_id=_id)
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    @_engine_write_locked
    def insert_many(
        self,
        docs,
        ordered=True,
        bypass_document_validation=None,
        session=None,
        comment=None,
    ):
        """
        Insert several documents with PyMongo-style partial failure semantics.

        Duplicate-key failures stop an ordered batch at the first failed
        operation. Unordered batches continue with every valid operation. Any
        client-side serialization failure is detected before storage is
        changed. This whole-list preflight is a stronger TinyMongo guarantee;
        PyMongo may split a very large input across multiple wire batches.

        :param docs: an iterable of documents
        :return: InsertManyResult
        """
        _reject_session({"session": session})
        if not isinstance(docs, Iterable) or isinstance(
            docs, (Mapping, str, bytes, bytearray)
        ):
            raise TypeError("documents must be a non-empty iterable")
        docs = list(docs)
        if not docs:
            raise TypeError("documents must be a non-empty list")
        if not isinstance(ordered, bool):
            raise TypeError("ordered must be True or False")

        _ids = []
        for doc in docs:
            if not isinstance(doc, MutableMapping):
                raise TypeError("each document must be a mutable mapping")
            if "_id" in doc:
                _id = doc["_id"]
            else:
                _id = doc["_id"] = _generate_document_id()
            _ids.append(_id)

        # Validate TinyMongo's one storage batch at the JSON boundary before
        # changing storage. PyMongo can split very large inputs across multiple
        # wire batches, so TinyMongo intentionally provides a stronger
        # whole-list guarantee here.
        for index, doc in enumerate(docs):
            self._validate_storage_document(doc, index=index)

        engine = self.parent.engine
        if engine is not None:
            results, accepted, write_errors = _execute_engine_insert_many(
                self,
                docs,
                ordered,
                bypass_document_validation,
            )
            if write_errors:
                raise BulkWriteError(_bulk_write_details(len(accepted), write_errors))
            return InsertManyResult(eids=results, inserted_ids=list(_ids))

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            if self.table is None:
                self.build_table()
            self._refresh_table()
            accepted, write_errors = _plan_insert_many(
                self,
                docs,
                self.table.all(),
                list(self._index_specs.values()),
                ordered,
            )

            if accepted:
                results = self.table.insert_multiple(accepted)
                self._invalidate_indexes()
            else:
                results = []

            if write_errors:
                raise BulkWriteError(_bulk_write_details(len(accepted), write_errors))

            return InsertManyResult(eids=list(results), inserted_ids=list(_ids))
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    def parse_query(self, query):
        """
        Creates a tinydb Query() object from the query dict

        :param query: object containing the dictionary representation of the
        query
        :return: composite Query()
        """
        logger.debug("query to parse2: {}".format(query))

        # this should find all records
        if query == {} or query is None:
            return Query()._id != "-1"  # noqa

        q = None
        # find the final result of the generator
        for c in self.parse_condition(query):
            if q is None:
                q = c
            else:
                q = q & c

        logger.debug("new query item2: {}".format(q))

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
        logger.debug("query: {} prev_query: {}".format(query, prev_key))

        q = Query()
        conditions = None

        # deal with the {'name': value} case by injecting a previous key
        if not prev_key:
            temp_query = copy.deepcopy(query)
            k, v = temp_query.popitem()
            prev_key = k

        # deal with the conditions
        for key, value in query.items():
            logger.debug("conditions: {} {}".format(key, value))

            if key == "$gte":
                conditions = (
                    (Q(q, prev_key) >= value)
                    if not conditions and prev_key != "$not"
                    else (
                        (conditions & (Q(q, prev_key) >= value))
                        if prev_key != "$not"
                        else (q[last_prev_key] < value)
                    )
                )
            elif key == "$gt":
                conditions = (
                    (Q(q, prev_key) > value)
                    if not conditions and prev_key != "$not"
                    else (
                        (conditions & (Q(q, prev_key) > value))
                        if prev_key != "$not"
                        else (q[last_prev_key] <= value)
                    )
                )
            elif key == "$lte":
                conditions = (
                    (Q(q, prev_key) <= value)
                    if not conditions and prev_key != "$not"
                    else (
                        (conditions & (Q(q, prev_key) <= value))
                        if prev_key != "$not"
                        else (q[last_prev_key] > value)
                    )
                )
            elif key == "$lt":
                conditions = (
                    (Q(q, prev_key) < value)
                    if not conditions and prev_key != "$not"
                    else (
                        (conditions & (Q(q, prev_key) < value))
                        if prev_key != "$not"
                        else (q[last_prev_key] >= value)
                    )
                )
            elif key == "$ne":
                conditions = (
                    (Q(q, prev_key) != value)
                    if not conditions and prev_key != "$not"
                    else (
                        (conditions & (Q(q, prev_key) != value))
                        if prev_key != "$not"
                        else (q[last_prev_key] == value)
                    )
                )
            elif key == "$not":
                if not isinstance(value, dict) and not isinstance(
                    value, list
                ):  # pragma: no branch - containers recurse below
                    conditions = (
                        (Q(q, prev_key) != value)
                        if not conditions and prev_key != "$not"
                        else (
                            (conditions & (Q(q, prev_key) != value))
                            if prev_key != "$not"
                            else (q[last_prev_key] >= value)
                        )
                    )
                else:
                    # let the value's condition be parsed below
                    pass
            elif key == "$regex":
                value = value.replace("\\\\\\", "|||")
                value = value.replace("\\\\", "|||")
                regex = value.replace("\\", "")
                regex = regex.replace("|||", "\\")
                currCond = where(prev_key).matches(regex)
                conditions = currCond if not conditions else (conditions & currCond)
            elif key == "$nin":
                # Build a conjunctive condition: field != each value
                vals = value if isinstance(value, list) else [value]
                nin_cond = None
                for v in vals:
                    term = Q(q, prev_key) != v
                    nin_cond = term if nin_cond is None else (nin_cond & term)
                conditions = nin_cond if not conditions else (conditions & nin_cond)
            elif key == "$exists":
                exists_cond = Q(q, prev_key).exists()
                if value:
                    conditions = (
                        exists_cond if not conditions else conditions & exists_cond
                    )
                else:
                    conditions = (
                        ~exists_cond if not conditions else conditions & ~exists_cond
                    )
            elif key in ["$and", "$or", "$nor", "$in", "$all"]:
                pass
            else:

                # don't want to use the previous key if this is a secondary key
                # (fixes multiple item query that includes $ codes)
                if not isinstance(value, dict) and not isinstance(value, list):
                    cache_identity = bson_identity_key(value)
                    equality = Q(q, key).test(
                        _cached_value_matches,
                        value,
                        ("__tinymongo_bson_identity__", cache_identity),
                    )
                    conditions = equality if not conditions else (conditions & equality)
                    prev_key = key

            logger.debug("c: {}".format(conditions))
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
                    if conditions is not None:  # pragma: no branch - parser guard
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

    @_engine_write_locked
    def update_one(self, query, doc, *args, **kwargs):
        """
        Updates one element of the collection

        :param query: dictionary representing the mongo query
        :param doc: dictionary representing the item to be updated
        :return: UpdateResult
        """
        _reject_session(kwargs)
        validate_filter_operators(query)
        _validate_update_document(doc)
        self._validate_storage_document(doc)
        upsert = kwargs.get("upsert") is True

        if self.parent.engine is not None:
            matches = self.parent.engine.find(self.tablename, query, limit=1)
            if not matches:
                if upsert:
                    inserted = _document_for_upsert(query, doc)
                    self._validate_storage_document(inserted)
                    self.parent.engine.validate_unique_post_image(
                        self.tablename,
                        self.parent.engine.find(self.tablename, {}) + [inserted],
                    )
                    self.parent.engine.insert_many(self.tablename, [inserted])
                    return UpdateResult(
                        raw_result=[],
                        matched_count=0,
                        modified_count=0,
                        upserted_id=inserted["_id"],
                    )
                return UpdateResult(raw_result=[], matched_count=0, modified_count=0)
            updated = self.parent.engine.apply_update(matches[0], doc)
            self._validate_storage_document(updated)
            modified = _update_document_modified(matches[0], updated, doc)
            self.parent.engine.validate_unique_post_image(
                self.tablename,
                [
                    (
                        updated
                        if bson_values_equal(item.get("_id"), matches[0].get("_id"))
                        else item
                    )
                    for item in self.parent.engine.find(self.tablename, {})
                ],
            )
            self.parent.engine.update_many(self.tablename, query, doc, multi=False)
            return UpdateResult(
                raw_result={
                    "n": 1,
                    "nModified": int(modified),
                    "updatedExisting": True,
                },
                matched_count=1,
                modified_count=int(modified),
            )

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            if self.table is None:
                self.build_table()
            self._refresh_table()
            direct_id = _direct_id_equality(query)
            if direct_id is not _MISSING:
                item = next(
                    (
                        candidate
                        for candidate in self.table.all()
                        if bson_values_equal(candidate.get("_id"), direct_id)
                    ),
                    None,
                )
            elif _filter_references_id(query) or requires_python_filter(query):
                item = next(
                    (
                        candidate
                        for candidate in self.table.all()
                        if matches_filter(candidate, query)
                    ),
                    None,
                )
            else:
                allcond = self.parse_query(query)
                item = self.table.get(allcond)

            if item is None:
                if upsert:
                    inserted = _document_for_upsert(query, doc)
                    self._validate_storage_document(inserted)
                    self._validate_unique_post_image(self.table.all() + [inserted])
                    self.table.insert(inserted)
                    self._invalidate_indexes()
                    return UpdateResult(
                        raw_result=[],
                        matched_count=0,
                        modified_count=0,
                        upserted_id=inserted["_id"],
                    )
                return UpdateResult(raw_result=[])

            updated = _apply_update_document(item, doc)
            self._validate_storage_document(updated)
            modified = _update_document_modified(item, updated, doc)
            if modified:
                post_image = [
                    (
                        updated
                        if bson_values_equal(current.get("_id"), item.get("_id"))
                        else current
                    )
                    for current in self.table.all()
                ]
                self._validate_unique_post_image(post_image)
                self.table.update(
                    _tinydb_replace_document(updated),
                    _tinydb_id_condition(item["_id"]),
                )
                self._invalidate_indexes()

            return UpdateResult(
                raw_result={
                    "n": 1,
                    "nModified": int(modified),
                    "updatedExisting": True,
                },
                matched_count=1,
                modified_count=int(modified),
            )
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    @_engine_write_locked
    def update_many(self, query, doc, *args, **kwargs):
        """
        Updates all elements matching the query

        :param query: dictionary representing the mongo query
        :param doc: dictionary or update document
        :return: UpdateResult
        """
        _reject_session(kwargs)
        validate_filter_operators(query)
        _validate_update_document(doc)
        self._validate_storage_document(doc)
        upsert = kwargs.get("upsert") is True

        if self.parent.engine is not None:
            matches = self.parent.engine.find(self.tablename, query)
            if not matches and upsert:
                inserted = _document_for_upsert(query, doc)
                self._validate_storage_document(inserted)
                self.parent.engine.validate_unique_post_image(
                    self.tablename,
                    self.parent.engine.find(self.tablename, {}) + [inserted],
                )
                self.parent.engine.insert_many(self.tablename, [inserted])
                return UpdateResult(
                    raw_result=[],
                    matched_count=0,
                    modified_count=0,
                    upserted_id=inserted["_id"],
                )
            updates = [
                (item, self.parent.engine.apply_update(item, doc)) for item in matches
            ]
            for _original, updated in updates:
                self._validate_storage_document(updated)
            modified_count = sum(
                _update_document_modified(item, updated, doc)
                for item, updated in updates
            )
            self.parent.engine.validate_unique_post_image(
                self.tablename,
                [
                    next(
                        (
                            updated
                            for original, updated in updates
                            if bson_values_equal(
                                original.get("_id"),
                                item.get("_id"),
                            )
                        ),
                        item,
                    )
                    for item in self.parent.engine.find(self.tablename, {})
                ],
            )
            result = self.parent.engine.update_many(
                self.tablename, query, doc, multi=True
            )
            return UpdateResult(
                raw_result=result,
                matched_count=len(matches),
                modified_count=modified_count,
            )

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            if self.table is None:
                self.build_table()
            self._refresh_table()
            direct_id = _direct_id_equality(query)
            if direct_id is not _MISSING:
                items = [
                    candidate
                    for candidate in self.table.all()
                    if bson_values_equal(candidate.get("_id"), direct_id)
                ]
            elif _filter_references_id(query) or requires_python_filter(query):
                items = [
                    candidate
                    for candidate in self.table.all()
                    if matches_filter(candidate, query)
                ]
            else:
                allcond = self.parse_query(query)
                items = list(self.table.search(allcond))
            if not items and upsert:
                inserted = _document_for_upsert(query, doc)
                self._validate_storage_document(inserted)
                self._validate_unique_post_image(self.table.all() + [inserted])
                self.table.insert(inserted)
                self._invalidate_indexes()
                return UpdateResult(
                    raw_result=[],
                    matched_count=0,
                    modified_count=0,
                    upserted_id=inserted["_id"],
                )
            updates = [(item, _apply_update_document(item, doc)) for item in items]
            for _original, updated in updates:
                self._validate_storage_document(updated)
            self._validate_unique_post_image(
                [
                    next(
                        (
                            updated
                            for original, updated in updates
                            if bson_values_equal(
                                original.get("_id"),
                                item.get("_id"),
                            )
                        ),
                        item,
                    )
                    for item in self.table.all()
                ]
            )
            result = []
            modified_count = 0
            for item, updated in updates:
                if _update_document_modified(item, updated, doc):
                    modified_count += 1
                    result.extend(
                        self.table.update(
                            _tinydb_replace_document(updated),
                            _tinydb_id_condition(item["_id"]),
                        )
                    )
            if modified_count:
                self._invalidate_indexes()

            return UpdateResult(
                raw_result=result,
                matched_count=len(items),
                modified_count=modified_count,
            )
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    @_engine_write_locked
    def replace_one(self, query, replacement, *args, **kwargs):
        """
        Replaces one document matching the query with the replacement document.
        """
        _reject_session(kwargs)
        validate_filter_operators(query)
        if not isinstance(replacement, dict):
            raise TypeError('"replacement" must be a dict')
        self._validate_storage_document(replacement)
        if self.parent.engine is not None:
            item = self.parent.engine.find_one(self.tablename, query)
            if item is None:
                if kwargs.get("upsert") is True:
                    inserted = copy.deepcopy(replacement)
                    if "_id" not in inserted:
                        inserted["_id"] = _generate_document_id()
                    self._validate_storage_document(inserted)
                    self.parent.engine.validate_unique_post_image(
                        self.tablename,
                        self.parent.engine.find(self.tablename, {}) + [inserted],
                    )
                    self.parent.engine.insert_many(self.tablename, [inserted])
                    return UpdateResult(
                        raw_result=[],
                        matched_count=0,
                        modified_count=0,
                        upserted_id=inserted["_id"],
                    )
                return UpdateResult(raw_result=[])
            # MongoDB stores ``_id`` first even when the replacement omitted it.
            # Keep that canonical order so a later equivalent replacement is
            # not misreported as a field-order-only modification.
            updated = {"_id": item["_id"]}
            updated.update(copy.deepcopy(replacement))
            updated["_id"] = item["_id"]
            self._validate_storage_document(updated)
            modified = not storage_values_equal(updated, item)
            if modified:
                self.parent.engine.validate_unique_post_image(
                    self.tablename,
                    [
                        (
                            updated
                            if bson_values_equal(current.get("_id"), item.get("_id"))
                            else current
                        )
                        for current in self.parent.engine.find(self.tablename, {})
                    ],
                )
                self.parent.engine.replace_one(self.tablename, item["_id"], updated)
            return UpdateResult(
                raw_result=[item["_id"]] if modified else [],
                matched_count=1,
                modified_count=int(modified),
            )

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            if self.table is None:
                self.build_table()
            self._refresh_table()
            direct_id = _direct_id_equality(query)
            if direct_id is not _MISSING:
                item = next(
                    (
                        candidate
                        for candidate in self.table.all()
                        if bson_values_equal(candidate.get("_id"), direct_id)
                    ),
                    None,
                )
            elif _filter_references_id(query) or requires_python_filter(query):
                item = next(
                    (
                        candidate
                        for candidate in self.table.all()
                        if matches_filter(candidate, query)
                    ),
                    None,
                )
            else:
                allcond = self.parse_query(query)
                item = self.table.get(allcond)
            if item is None:
                if kwargs.get("upsert") is True:
                    inserted = copy.deepcopy(replacement)
                    if "_id" not in inserted:
                        inserted["_id"] = _generate_document_id()
                    self._validate_storage_document(inserted)
                    self._validate_unique_post_image(self.table.all() + [inserted])
                    self.table.insert(inserted)
                    self._invalidate_indexes()
                    return UpdateResult(
                        raw_result=[],
                        matched_count=0,
                        modified_count=0,
                        upserted_id=inserted["_id"],
                    )
                return UpdateResult(raw_result=[])

            updated = {"_id": item["_id"]}
            updated.update(copy.deepcopy(replacement))
            updated["_id"] = item["_id"]
            self._validate_storage_document(updated)
            modified = not storage_values_equal(updated, item)
            if modified:
                post_image = [
                    (
                        updated
                        if bson_values_equal(current.get("_id"), item.get("_id"))
                        else current
                    )
                    for current in self.table.all()
                ]
                self._validate_unique_post_image(post_image)
                self.table.remove(_tinydb_id_condition(item["_id"]))
                self.table.insert(updated)
                self._invalidate_indexes()
                result = [item["_id"]]
            else:
                result = []

            return UpdateResult(
                raw_result=result, matched_count=1, modified_count=int(modified)
            )
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    @_engine_write_locked
    def find_one_and_update(self, query, update, *args, **kwargs):
        """
        Mimics MongoDB's findOneAndUpdate by returning the document before update.
        """
        _reject_session(kwargs)
        return_after = kwargs.get("return_document", False)
        if not isinstance(return_after, bool):
            raise ValueError(
                "return_document must be ReturnDocument.BEFORE or "
                "ReturnDocument.AFTER"
            )
        projection = normalize_projection(kwargs.get("projection"))
        sort = kwargs.get("sort")
        write_kwargs = dict(kwargs)
        for option in ("projection", "return_document", "sort"):
            write_kwargs.pop(option, None)

        rlock = portalocker_lock = None
        if self.parent.engine is None:
            rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            item = self.find_one(query, sort=sort)
            if item is None:
                result = self.update_one(query, update, *args, **write_kwargs)
                if result.upserted_id is None or not return_after:
                    return None
                returned = self.find_one({"_id": result.upserted_id})
            else:
                self.update_one({"_id": item["_id"]}, update, *args, **write_kwargs)
                returned = self.find_one({"_id": item["_id"]}) if return_after else item
            return project_document(returned, projection)
        finally:
            if self.parent.engine is None:
                self._release_collection_lock(rlock, portalocker_lock)

    @_engine_write_locked
    def find_one_and_replace(self, query, replacement, *args, **kwargs):
        """Replace one document and return its previous or resulting value."""
        _reject_session(kwargs)
        return_after = kwargs.get("return_document", False)
        if not isinstance(return_after, bool):
            raise ValueError(
                "return_document must be ReturnDocument.BEFORE or "
                "ReturnDocument.AFTER"
            )
        projection = normalize_projection(kwargs.get("projection"))
        sort = kwargs.get("sort")
        write_kwargs = dict(kwargs)
        for option in ("projection", "return_document", "sort"):
            write_kwargs.pop(option, None)

        rlock = portalocker_lock = None
        if self.parent.engine is None:
            rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            previous = self.find_one(query, sort=sort)
            target = query if previous is None else {"_id": previous["_id"]}
            result = self.replace_one(target, replacement, *args, **write_kwargs)
            if previous is None:
                if result.upserted_id is None or not return_after:
                    return None
                returned = self.find_one({"_id": result.upserted_id})
            else:
                returned = (
                    self.find_one({"_id": previous["_id"]})
                    if return_after
                    else previous
                )
            return project_document(returned, projection)
        finally:
            if self.parent.engine is None:
                self._release_collection_lock(rlock, portalocker_lock)

    @_engine_write_locked
    def find_one_and_delete(self, query, *args, **kwargs):
        """Delete one matching document and return its pre-delete value."""
        _reject_session(kwargs)
        projection = kwargs.get("projection")
        sort = kwargs.get("sort")
        rlock = portalocker_lock = None
        if self.parent.engine is None:
            rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            item = self.find_one(query, sort=sort)
            if item is None:
                return None
            self.delete_one({"_id": item["_id"]})
            return project_document(item, normalize_projection(projection))
        finally:
            if self.parent.engine is None:
                self._release_collection_lock(rlock, portalocker_lock)

    def find(
        self, _filter=None, projection=None, skip=None, limit=None, *args, **kwargs
    ):
        """
        Finds all matching results

        :param _filter: dictionary representing the mongo query
        :type _filter: Optional[dict]
        :return: cursor containing the search results
        """
        _reject_session(kwargs)
        if _filter is None and "filter" in kwargs:
            _filter = kwargs["filter"]
        validate_filter_operators({} if _filter is None else _filter)
        projection = normalize_projection(projection)
        sort = kwargs.get("sort")
        if self.parent.engine is not None:
            projected_find = getattr(self.parent.engine, "find_projected", None)
            projection_applied = (
                projection is not None and not sort and callable(projected_find)
            )
            if projection_applied:
                result = projected_find(self.tablename, _filter, projection)
            else:
                result = self.parent.engine.find(self.tablename, _filter)
            return TinyMongoCursor(
                result,
                sort=sort,
                skip=skip,
                limit=limit,
                collection=self,
                projection=projection,
                query=_filter,
                source_projected=projection_applied,
            )

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            if self.table is None:
                self.build_table()

            self._refresh_stale_memory_table()

            if _filter is None:
                result = self.table.all()
            else:
                direct_id = _direct_id_equality(_filter)
                if direct_id is not _MISSING:
                    result = [
                        document
                        for document in self.table.all()
                        if bson_values_equal(document.get("_id"), direct_id)
                    ]
                    return TinyMongoCursor(
                        result,
                        sort=sort,
                        skip=skip,
                        limit=limit,
                        collection=self,
                        projection=projection,
                        query=_filter,
                    )
                python_filter = requires_python_filter(_filter)
                simple = _simple_equality_filter(_filter)
                if simple is not None and "." in simple[0]:
                    simple = None
                if simple is not None:
                    key, value = simple
                    index = self._get_index(key)
                    if index is not None:
                        identity = bson_identity_key(value)
                        cache_key = (
                            ("__tinymongo_bson_identity__", identity)
                            if identity is not None
                            else value
                        )
                        return TinyMongoCursor(
                            list(index.get(cache_key, [])),
                            sort=sort,
                            skip=skip,
                            limit=limit,
                            collection=self,
                            projection=projection,
                            query=_filter,
                        )
                if _filter_references_id(_filter) or python_filter:
                    result = [
                        document
                        for document in self.table.all()
                        if matches_filter(document, _filter)
                    ]
                else:
                    allcond = self.parse_query(_filter)

                    try:
                        result = self.table.search(allcond)
                    except (AttributeError, TypeError):
                        result = []

            return TinyMongoCursor(
                result,
                sort=sort,
                skip=skip,
                limit=limit,
                collection=self,
                projection=projection,
                query=_filter,
            )
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    def find_one(self, _filter=None, projection=None, *args, **kwargs):
        """
        Finds one matching query element

        :param query: dictionary representing the mongo query
        :return: the resulting document (if found)
        """
        _reject_session(kwargs)
        if _filter is None and "filter" in kwargs:
            _filter = kwargs["filter"]
        sort = kwargs.get("sort")
        cursor = self.find(
            _filter,
            projection,
            limit=1,
            sort=sort,
        )
        try:
            return cursor.next()
        except (IndexError, StopIteration):
            return None

    def distinct(self, key, filter=None, *args, **kwargs):
        """Return unique values for a field among matching documents."""
        _reject_session(kwargs)
        values = []
        identities = set()
        unsupported_values = []
        for document in self.find({} if filter is None else filter):
            value = _get_nested(document, key)
            if value is _MISSING:
                continue
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                identity = bson_value_identity_key(candidate)
                if identity is not None:
                    if identity in identities:
                        continue
                    identities.add(identity)
                else:
                    if any(
                        type(candidate) is type(existing) and candidate == existing
                        for existing in unsupported_values
                    ):
                        continue
                    unsupported_values.append(candidate)
                values.append(copy.deepcopy(candidate))
        return values

    def remove(self, spec_or_id, multi=True, *args, **kwargs):
        """Backwards compatibility with remove"""
        if multi:
            return self.delete_many(spec_or_id)
        return self.delete_one(spec_or_id)

    @_engine_write_locked
    def delete_one(self, query, *args, **kwargs):
        """
        Deletes one document from the collection

        :param query: dictionary representing the mongo query
        :return: DeleteResult
        """
        _reject_session(kwargs)
        validate_filter_operators(query)
        if self.parent.engine is not None:
            result = self.parent.engine.delete_many(self.tablename, query, multi=False)
            return DeleteResult(raw_result=result)

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            item = self.find_one(query)
            if item is None:
                return DeleteResult(raw_result=[])
            self._set_storage_merge_writes(False)
            try:
                result = self.table.remove(_tinydb_id_condition(item["_id"]))
                self._invalidate_indexes()
            finally:
                self._set_storage_merge_writes(True)

            return DeleteResult(raw_result=result)
        finally:
            self._release_collection_lock(rlock, portalocker_lock)

    @_engine_write_locked
    def delete_many(self, query, *args, **kwargs):
        """
        Removes all items matching the mongo query

        :param query: dictionary representing the mongo query
        :return: DeleteResult
        """
        _reject_session(kwargs)
        validate_filter_operators(query)
        if self.parent.engine is not None:
            result = self.parent.engine.delete_many(self.tablename, query, multi=True)
            return DeleteResult(raw_result=result)

        rlock, portalocker_lock = self._acquire_collection_lock()
        try:
            items = self.find(query)
            self._set_storage_merge_writes(False)
            try:
                result = [
                    self.table.remove(_tinydb_id_condition(item["_id"]))
                    for item in items
                ]
                self._invalidate_indexes()

                if query == {}:
                    # need to reset TinyDB's index for docs order consistency
                    self.table._last_id = 0
            finally:
                self._set_storage_merge_writes(True)

            return DeleteResult(raw_result=result)
        finally:
            self._release_collection_lock(rlock, portalocker_lock)


class TinyMongoCursor(object):
    """Mongo iterable cursor"""

    def __init__(
        self,
        cursordat,
        sort=None,
        skip=None,
        limit=None,
        collection=None,
        projection=None,
        query=None,
        source_projected=False,
    ):
        """Initialize the mongo iterable cursor with data"""
        self._source_data = list(cursordat)
        self.cursordat = list(self._source_data)
        self.collection = collection
        self.projection = projection
        self.query = copy.deepcopy(query)
        self._source_projected = source_projected
        self._sort_spec = None
        self._skip = 0
        self._limit = 0
        self._closed = False
        self._unsupported_sort_warnings = set()
        self.cursorpos = -1
        self.currentrec = None

        if sort:
            self.sort(sort)

        self.paginate(skip, limit)

    def __getitem__(self, key):
        """Gets record by index or value by key"""
        if isinstance(key, int):
            return self._project(self.cursordat[key])
        return self._project(self.currentrec)[key]

    def _project(self, document):
        if self.projection is None:
            return copy.deepcopy(document)
        return project_document(document, self.projection)

    def paginate(self, skip, limit):
        """Paginate list of records"""
        if skip is not None:
            self._validate_skip(skip)
            self._skip = skip
        if limit is not None:
            self._validate_limit(limit)
            self._limit = abs(limit)
        self._refresh_view()
        return self

    @staticmethod
    def _validate_skip(value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("skip must be an integer")
        if value < 0:
            raise ValueError("skip must be non-negative")

    @staticmethod
    def _validate_limit(value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("limit must be an integer")

    def _refresh_view(self):
        end = None if self._limit == 0 else self._skip + self._limit
        self.cursordat = self._source_data[self._skip : end]
        self.cursorpos = -1
        self.currentrec = self.cursordat[-1] if self.cursordat else None

    def _warn_unsupported_sort_value(self, field, value):
        """Warn once when a sort field contains an unsupported value type."""
        warning_key = (field, type(value))
        if warning_key in self._unsupported_sort_warnings:
            return
        self._unsupported_sort_warnings.add(warning_key)
        emit_warning(
            "Sorting field '{0}' encountered unsupported value type '{1}'; "
            "values of this type compare as null.".format(
                field,
                type(value).__name__,
            ),
            TinyMongoUnsupportedWarning,
            stacklevel=4,
        )

    def _order(self, value, is_reverse=None, sort_field=None):
        """Parsing data to a sortable form
        By giving each data type an ID(int), and assemble with the value
        into a sortable tuple.
        """

        if is_reverse is None:
            value_key = bson_value_sort_key(value)
            if value_key is None:
                if sort_field is not None:
                    self._warn_unsupported_sort_value(sort_field, value)
                return 0, None
            return value_key
        return bson_document_sort_value_key(
            value,
            descending=is_reverse,
            sort_field=sort_field,
            unsupported_value_callback=self._warn_unsupported_sort_value,
        )

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

        self._sort_spec = (copy.deepcopy(key_or_list), direction)

        if self._source_projected:
            # A chained sort can name a field omitted by the projection.  Load
            # complete post-filter documents before ordering so projection
            # pushdown never changes cursor semantics.
            source = self.collection.find(copy.deepcopy(self.query))
            self._source_data = list(source._source_data)
            self._source_projected = False

        self._source_data = sort_documents(
            self._source_data,
            sort_specifier,
            unsupported_value_callback=self._warn_unsupported_sort_value,
        )
        self._refresh_view()

        return self

    def limit(self, n):
        self._validate_limit(n)
        self._limit = abs(n)
        self._refresh_view()
        return self

    def skip(self, n):
        """Skip the first ``n`` records without changing the result source."""
        self._validate_skip(n)
        self._skip = n
        self._refresh_view()
        return self

    def clone(self):
        """Return an independently consumable copy of this cursor."""
        if self.collection is None:
            clone = type(self)(
                copy.deepcopy(self._source_data),
                projection=copy.deepcopy(self.projection),
                query=copy.deepcopy(self.query),
            )
        else:
            clone = self.collection.find(copy.deepcopy(self.query))
            clone.projection = copy.deepcopy(self.projection)
            if self._sort_spec is not None:
                clone.sort(
                    copy.deepcopy(self._sort_spec[0]),
                    self._sort_spec[1],
                )
        clone._skip = self._skip
        clone._limit = self._limit
        clone._refresh_view()
        return clone

    def rewind(self):
        """Reset iteration to the beginning of the current cursor window."""
        if self._closed:
            raise InvalidOperation("Cannot rewind a closed cursor")
        self.cursorpos = -1
        return self

    @property
    def alive(self):
        return not self._closed and self.cursorpos + 1 < len(self.cursordat)

    def close(self):
        """Close this local cursor and discard no collection data."""
        self._closed = True

    def to_list(self, length=None):
        """Consume up to ``length`` remaining records into a list."""
        if length is not None:
            if isinstance(length, bool) or not isinstance(length, int):
                raise TypeError("length must be an integer or None")
            if length < 0:
                raise ValueError("length must be non-negative")
        if self._closed:
            return []
        start = self.cursorpos + 1
        end = len(self.cursordat) if length is None else start + length
        documents = [self._project(item) for item in self.cursordat[start:end]]
        self.cursorpos += len(documents)
        return documents

    def has_next(self):
        """
        Returns True if the cursor has a next position, False if not
        :return:
        """
        return self.alive

    def hasNext(self):
        """
        Returns True if the cursor has a next position, False if not
        :return:
        """
        if self._closed:
            return False
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
        if not self.hasNext():
            raise StopIteration
        self.cursorpos += 1
        return self._project(self.cursordat[self.cursorpos])

    def count(self, with_limit_and_skip=False):
        """
        Returns the number of records in the current cursor

        :return: number of records
        """
        return len(self.cursordat)

    def __iter__(self):
        if self._closed:
            return iter(())
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __next__(self):
        """
        Returns the next record
        :return:
        """
        if not self.hasNext():
            raise StopIteration
        self.cursorpos += 1
        return self._project(self.cursordat[self.cursorpos])


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
    """Generate a portable UUID string for callers that explicitly want one."""
    return uuid4().hex


# def generate_id():
#     """Generate new UUID"""
#     # TODO: Use six.string_type to Py3 compat
#     try:
#         return unicode(uuid1()).replace(u"-", u"")
#     except NameError:
#         return str(uuid1()).replace(u"-", u"")
