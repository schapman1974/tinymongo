import hashlib
import importlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from functools import wraps
from urllib.parse import parse_qs, unquote, urlparse
from typing import Optional

from .bson_codec import contains_extended_value
from .bson_codec import dumps as bson_json_dumps
from .bson_codec import loads as bson_json_loads
from .errors import (
    DuplicateKeyError,
    OperationFailure,
    StorageCorruptionError,
    TinyMongoNotSupportedError,
)
from .indexes import (
    INDEX_CATALOG_TABLE,
    IndexSpec,
    index_catalog_id,
    index_tokens,
    parse_index_spec,
    validate_unique_documents,
)
from .parquet_storage import _acquire_rlock, _local_rlocks, portalocker


_MISSING = object()
_OBJECT_STORE_SCHEMES = {"s3", "gs", "gcs", "az", "azure", "abfs", "abfss"}


def _write_locked(method):
    """Serialize a backend read-check-write operation."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._write_lock():
            return method(self, *args, **kwargs)

    return wrapped


def _import_optional_driver(module_name, backend_name, install_hint):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # pragma: no cover - covered through callers
        raise ImportError(
            "{0} backend requires the optional Python driver '{1}'. "
            "Install it with: {2}".format(backend_name, module_name, install_hint)
        ) from exc


def _is_object_store_uri(path):
    return urlparse(str(path)).scheme.lower() in _OBJECT_STORE_SCHEMES


def _join_uri(base, *parts):
    if _is_object_store_uri(base):
        return "/".join(
            [str(base).rstrip("/")] + [str(part).strip("/") for part in parts]
        )
    return os.path.join(base, *parts)


def _sql_literal(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return "'" + str(value).replace("'", "''") + "'"


def _json_path(field):
    return "$" + "".join(
        "." + json.dumps(part, ensure_ascii=False) for part in field.split(".")
    )


def _env_first(*names):
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    return None


def _env_bool(name):
    value = os.environ.get(name)
    if value in (None, ""):
        return None
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _duckdb_object_store_settings():
    settings = {
        "s3_region": _env_first(
            "TINYMONGO_S3_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"
        ),
        "s3_access_key_id": _env_first(
            "TINYMONGO_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"
        ),
        "s3_secret_access_key": _env_first(
            "TINYMONGO_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"
        ),
        "s3_session_token": _env_first(
            "TINYMONGO_S3_SESSION_TOKEN", "AWS_SESSION_TOKEN"
        ),
        "s3_endpoint": _env_first("TINYMONGO_S3_ENDPOINT", "AWS_ENDPOINT_URL"),
        "s3_url_style": _env_first("TINYMONGO_S3_URL_STYLE"),
        "azure_storage_connection_string": _env_first(
            "TINYMONGO_AZURE_CONNECTION_STRING",
            "AZURE_STORAGE_CONNECTION_STRING",
        ),
    }
    use_ssl = _env_bool("TINYMONGO_S3_USE_SSL")
    if use_ssl is not None:
        settings["s3_use_ssl"] = use_ssl
    return {key: value for key, value in settings.items() if value not in (None, "")}


def _duckdb_secret_sql_from_env():
    statements = []
    gcs_key = _env_first("TINYMONGO_GCS_KEY_ID", "GOOGLE_HMAC_KEY_ID")
    gcs_secret = _env_first("TINYMONGO_GCS_SECRET", "GOOGLE_HMAC_SECRET")
    if gcs_key and gcs_secret:
        statements.append(
            "CREATE OR REPLACE SECRET tinymongo_gcs "
            "(TYPE gcs, KEY_ID {0}, SECRET {1})".format(
                _sql_literal(gcs_key), _sql_literal(gcs_secret)
            )
        )

    azure_connection = _env_first(
        "TINYMONGO_AZURE_CONNECTION_STRING",
        "AZURE_STORAGE_CONNECTION_STRING",
    )
    if azure_connection:
        statements.append(
            "CREATE OR REPLACE SECRET tinymongo_azure "
            "(TYPE azure, CONNECTION_STRING {0})".format(_sql_literal(azure_connection))
        )
    return statements


def _duckdb_setup_sql_from_env():
    sql = os.environ.get("TINYMONGO_DUCKDB_SETUP_SQL", "")
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


def _json_dumps(doc):
    return bson_json_dumps(doc, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value):
    decoded = bson_json_loads(value)
    if not isinstance(value, (str, bytes, bytearray)) and decoded == value:
        # Remote JSON drivers may already return an ordinary decoded mapping.
        return value
    return decoded


def _quote_identifier(name):
    return '"' + str(name).replace('"', '""') + '"'


def _get_nested(doc, path, default=_MISSING):
    current = doc
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _value_matches(actual, expected):
    if isinstance(actual, list):
        return actual == expected or expected in actual
    return actual == expected


def _sqlite_unique_token(data, field):
    """Return the same lossless unique token used by Python validation."""
    document = _json_loads(data)
    return json.dumps(
        index_tokens(document, field),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _simple_scalar_equality(filter_doc):
    """Return one SQL-safe equality pair, or ``None`` for richer filters."""
    if not isinstance(filter_doc, dict) or len(filter_doc) != 1:
        return None
    field, expected = next(iter(filter_doc.items()))
    if (
        not isinstance(field, str)
        or field.startswith("$")
        or field == "_id"
        or expected is None
        or isinstance(expected, (dict, list, tuple))
        or not isinstance(expected, (bool, int, float, str))
    ):
        return None
    return field, expected


def _reject_remote_unique_arrays(documents, specs):
    """Fail closed where remote native constraints cannot protect multikey races."""
    for spec in specs:
        if not spec.unique:
            continue
        for document in documents:
            value = _get_nested(document, spec.field)
            if isinstance(value, (list, tuple)):
                raise TinyMongoNotSupportedError(
                    "Remote SQL unique index {0!r} does not support array values; "
                    "cross-process multikey uniqueness cannot be guaranteed".format(
                        spec.name
                    )
                )


def _comparison_matches(actual, operand, comparison):
    values = actual if isinstance(actual, list) else [actual]
    for value in values:
        try:
            if comparison(value, operand):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _regex_matches(actual, pattern, options=""):
    flags = 0
    for option, flag in (
        ("i", re.IGNORECASE),
        ("m", re.MULTILINE),
        ("s", re.DOTALL),
        ("x", re.VERBOSE),
    ):
        if option in str(options):
            flags |= flag
    try:
        expression = re.compile(pattern, flags)
    except (TypeError, ValueError, re.error):
        return False
    values = actual if isinstance(actual, list) else [actual]
    return any(
        isinstance(value, str) and expression.search(value) is not None
        for value in values
    )


def _field_matches(actual, expected):
    exists = actual is not _MISSING
    if not isinstance(expected, dict) or not any(
        str(key).startswith("$") for key in expected
    ):
        return exists and _value_matches(actual, expected)

    options = expected.get("$options", "")
    for operator, operand in expected.items():
        if operator == "$options":
            if "$regex" not in expected:
                return False
        elif operator == "$exists":
            if bool(operand) != exists:
                return False
        elif operator == "$gt":
            if not exists or not _comparison_matches(
                actual, operand, lambda a, b: a > b
            ):
                return False
        elif operator == "$gte":
            if not exists or not _comparison_matches(
                actual, operand, lambda a, b: a >= b
            ):
                return False
        elif operator == "$lt":
            if not exists or not _comparison_matches(
                actual, operand, lambda a, b: a < b
            ):
                return False
        elif operator == "$lte":
            if not exists or not _comparison_matches(
                actual, operand, lambda a, b: a <= b
            ):
                return False
        elif operator == "$ne":
            if exists and _value_matches(actual, operand):
                return False
        elif operator == "$in":
            values = operand if isinstance(operand, list) else [operand]
            if not exists or not any(_value_matches(actual, item) for item in values):
                return False
        elif operator == "$nin":
            values = operand if isinstance(operand, list) else [operand]
            if exists and any(_value_matches(actual, item) for item in values):
                return False
        elif operator == "$all":
            if not isinstance(actual, list) or not all(
                item in actual for item in operand
            ):
                return False
        elif operator == "$regex":
            if not exists or not _regex_matches(actual, operand, options):
                return False
        elif operator == "$not":
            nested = operand if isinstance(operand, dict) else {"$eq": operand}
            if exists and _field_matches(actual, nested):
                return False
        elif operator == "$eq":
            if not exists or not _value_matches(actual, operand):
                return False
        else:
            return False
    return True


def matches_filter(doc, filter_doc):
    if not filter_doc:
        return True
    if not isinstance(filter_doc, dict):
        return False

    for key, expected in filter_doc.items():
        if key == "$and":
            if not all(matches_filter(doc, spec) for spec in expected):
                return False
        elif key == "$or":
            if not any(matches_filter(doc, spec) for spec in expected):
                return False
        elif key == "$nor":
            if any(matches_filter(doc, spec) for spec in expected):
                return False
        else:
            actual = _get_nested(doc, key)
            if not _field_matches(actual, expected):
                return False
    return True


def requires_python_filter(filter_doc):
    """Return whether SQL JSON scalar comparison could change query meaning."""
    if not filter_doc or not isinstance(filter_doc, dict):
        return False
    if contains_extended_value(filter_doc):
        return True
    for field, expected in filter_doc.items():
        if field in ("$and", "$or", "$nor"):
            if any(requires_python_filter(item) for item in expected):
                return True
        else:
            if field.startswith("$"):
                return True
            if field != "_id" and not isinstance(expected, dict):
                # A JSON scalar comparison cannot also see members of array fields.
                return True
            if isinstance(expected, dict):
                if not any(str(operator).startswith("$") for operator in expected):
                    # Literal embedded-document equality is not an operator
                    # expression and TinyDB/SQL parsers otherwise treat its keys
                    # as field or operator names.
                    return True
                if any(
                    operator in expected
                    for operator in (
                        "$eq",
                        "$ne",
                        "$in",
                        "$nin",
                        "$all",
                        "$regex",
                        "$not",
                        "$options",
                    )
                ):
                    return True
    return False


class SQLCompiler(object):
    def __init__(self, dialect):
        self.dialect = dialect

    def json_value(self, field, value=None, numeric=False):
        path = _json_path(field)
        if self.dialect == "sqlite":
            expression = "json_extract(data, {0})".format(_sql_literal(path))
            return expression, []
        if numeric:
            return "CAST(json_extract_string(data, ?) AS DOUBLE)", [path]
        return "json_extract_string(data, ?)", [path]

    def json_exists(self, field):
        path = _json_path(field)
        if self.dialect == "sqlite":
            return "json_type(data, {0}) IS NOT NULL".format(_sql_literal(path)), []
        return "json_exists(data, ?)", [path]

    def compile(self, filter_doc):
        if not filter_doc:
            return "", []
        where, params = self._compile_spec(filter_doc)
        return " WHERE " + where, params

    def _compile_spec(self, spec):
        clauses = []
        params = []
        for key, value in spec.items():
            if key == "$and":
                grouped = [self._compile_spec(item) for item in value]
                clauses.append("(" + " AND ".join(item[0] for item in grouped) + ")")
                for item in grouped:
                    params.extend(item[1])
                continue
            if key == "$or":
                grouped = [self._compile_spec(item) for item in value]
                clauses.append("(" + " OR ".join(item[0] for item in grouped) + ")")
                for item in grouped:
                    params.extend(item[1])
                continue
            if key == "$nor":
                grouped = [self._compile_spec(item) for item in value]
                clauses.append("NOT (" + " OR ".join(item[0] for item in grouped) + ")")
                for item in grouped:
                    params.extend(item[1])
                continue

            clause, clause_params = self._compile_field(key, value)
            clauses.append(clause)
            params.extend(clause_params)
        return " AND ".join(clauses), params

    def _compile_field(self, field, value):
        if field == "_id":
            if isinstance(value, dict):
                clauses = []
                params = []
                for operator, operand in value.items():
                    if operator in ("$eq", "$ne"):
                        clauses.append(
                            "_id {0} ?".format("!=" if operator == "$ne" else "=")
                        )
                        params.append(str(operand))
                    elif operator == "$in":
                        values = operand if isinstance(operand, list) else [operand]
                        clauses.append(
                            "_id IN (" + ", ".join("?" for _ in values) + ")"
                        )
                        params.extend(str(item) for item in values)
                    else:
                        raise ValueError(
                            "Unsupported _id SQL operator: {0}".format(operator)
                        )
                return "(" + " AND ".join(clauses) + ")", params
            return "_id = ?", [str(value)]

        if isinstance(value, dict):
            clauses = []
            params = []
            for operator, operand in value.items():
                if operator == "$exists":
                    clause, clause_params = self.json_exists(field)
                    if not operand:
                        clause = "NOT (" + clause + ")"
                    clauses.append(clause)
                    params.extend(clause_params)
                elif operator in ("$gt", "$gte", "$lt", "$lte"):
                    expression, expression_params = self.json_value(
                        field, operand, numeric=isinstance(operand, (int, float))
                    )
                    op = {"$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}[operator]
                    clauses.append(expression + " " + op + " ?")
                    params.extend(expression_params)
                    params.append(operand)
                elif operator in ("$eq", "$ne"):
                    expression, expression_params = self.json_value(field, operand)
                    clauses.append(
                        expression + (" != ?" if operator == "$ne" else " = ?")
                    )
                    params.extend(expression_params)
                    params.append(self._sql_value(operand))
                elif operator == "$in":
                    values = operand if isinstance(operand, list) else [operand]
                    expression, expression_params = self.json_value(field, value)
                    placeholders = ", ".join("?" for _ in values)
                    clauses.append(expression + " IN (" + placeholders + ")")
                    params.extend(expression_params)
                    params.extend(self._sql_value(item) for item in values)
                else:
                    raise ValueError(
                        "Unsupported SQL filter operator: {0}".format(operator)
                    )
            return "(" + " AND ".join(clauses) + ")", params

        expression, expression_params = self.json_value(field, value)
        return expression + " = ?", expression_params + [self._sql_value(value)]

    def _sql_value(self, value):
        if self.dialect == "duckdb" and isinstance(value, bool):
            return "true" if value else "false"
        return value


class TableBackend(object):
    dialect: Optional[str] = None
    extension: Optional[str] = None

    def __init__(
        self,
        path,
        threads=None,
        duckdb_config=None,
        database=None,
        dsn=None,
    ):
        self.path = path
        self.threads = threads
        self.duckdb_config = duckdb_config or {}
        self.database = database
        self.dsn = dsn
        self.compiler = SQLCompiler(self.dialect)
        self._ephemeral_indexes = {}
        if not _is_object_store_uri(path):
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def close(self):
        pass

    @contextmanager
    def _write_lock(self):
        """Serialize local backend writes across threads and processes."""
        if _is_object_store_uri(self.path):
            yield
            return

        directory = os.path.dirname(self.path) or "."
        lock_path = os.path.join(directory, ".tinymongo.lock")
        rlock = _local_rlocks.setdefault(lock_path, threading.RLock())
        first_acquire = _acquire_rlock(rlock)
        file_lock = None
        try:
            if first_acquire and portalocker is not None:  # pragma: no branch
                file_lock = portalocker.Lock(lock_path, timeout=30)
                file_lock.acquire()
            yield
        finally:
            if file_lock is not None:  # pragma: no branch
                file_lock.release()
            rlock.release()

    def list_collections(self):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def create_collection(
        self, collection
    ):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def drop_collection(
        self, collection
    ):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def insert_many(
        self, collection, docs, bypass_document_validation=False
    ):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def all_docs(self, collection):
        return self.find(collection, {})

    def find(
        self, collection, filter_doc=None, sort=None, skip=None, limit=None
    ):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def find_one(self, collection, filter_doc=None):
        docs = self.find(collection, filter_doc, limit=1)
        return docs[0] if docs else None

    @_write_locked
    def update_many(self, collection, filter_doc, update_doc, multi=True):
        matches = self.find(collection, filter_doc)
        if not multi:
            matches = matches[:1]
        updated_ids = []
        for doc in matches:
            updated = self.apply_update(doc, update_doc)
            if updated != doc:
                self.replace_one(collection, doc["_id"], updated)
                updated_ids.append(doc["_id"])
        return updated_ids

    def replace_one(
        self, collection, doc_id, replacement
    ):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    @_write_locked
    def delete_many(self, collection, filter_doc, multi=True):
        matches = self.find(collection, filter_doc)
        if not multi:
            matches = matches[:1]
        ids = [doc["_id"] for doc in matches]
        self.delete_ids(collection, ids)
        return ids

    def delete_ids(
        self, collection, ids
    ):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def get_index_specs(self, collection):
        return list(self._ephemeral_indexes.get(collection, {}).values())

    def _coerce_index_spec(self, spec):
        return spec if isinstance(spec, IndexSpec) else parse_index_spec(spec)

    def _check_index_compatibility(self, collection, spec):
        specs = self.get_index_specs(collection)
        existing = next(
            (current for current in specs if current.name == spec.name),
            None,
        )
        if existing is not None and existing != spec:
            raise OperationFailure(
                "An index with the same name or key has different options"
            )
        return existing

    def create_index(self, collection, spec):
        spec = self._coerce_index_spec(spec)
        existing = self._check_index_compatibility(collection, spec)
        if existing is not None:
            return existing.name
        if spec.unique:
            validate_unique_documents(self.find(collection, {}), [spec])
        self._ephemeral_indexes.setdefault(collection, {})[spec.name] = spec
        return spec.name

    def drop_index(self, collection, name_or_field):
        indexes = self._ephemeral_indexes.get(collection, {})
        for name, spec in list(indexes.items()):
            if name_or_field in (name, spec.field):
                indexes.pop(name, None)
                return None
        raise OperationFailure("Index not found: {0}".format(name_or_field))

    def list_indexes(self, collection):
        indexes = [{"name": "_id_", "key": [("_id", 1)]}]
        for spec in sorted(
            self.get_index_specs(collection), key=lambda item: item.name
        ):
            metadata = {"name": spec.name, "key": [(spec.field, spec.direction)]}
            if spec.unique:
                metadata["unique"] = True
            indexes.append(metadata)
        return indexes

    def validate_unique_post_image(self, collection, documents):
        validate_unique_documents(documents, self.get_index_specs(collection))

    def apply_update(self, doc, update_doc):
        from .tinymongo import _apply_update_document

        return _apply_update_document(doc, update_doc)


class SQLiteTableBackend(TableBackend):
    dialect = "sqlite"
    extension = ".sqlite"
    index_catalog_table = "__tinymongo_indexes"

    def _physical_index_name(self, collection, spec):
        identity = "{0}\x00{1}\x00{2}".format(
            self.database or "default", collection, spec.name
        )
        digest = hashlib.sha256(identity.encode("utf8")).hexdigest()[:32]
        return "__tm_idx_{0}".format(digest)

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.create_function(
            "tinymongo_unique_token",
            2,
            _sqlite_unique_token,
            deterministic=True,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_index_catalog(self, conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS {0} ("
            "collection_name TEXT NOT NULL, index_name TEXT NOT NULL, "
            "field_name TEXT NOT NULL, unique_flag INTEGER NOT NULL, "
            "PRIMARY KEY (collection_name, index_name))".format(
                _quote_identifier(self.index_catalog_table)
            )
        )

    def get_index_specs(self, collection):
        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            rows = conn.execute(
                "SELECT index_name, field_name, unique_flag FROM {0} "
                "WHERE collection_name = ? ORDER BY index_name".format(
                    _quote_identifier(self.index_catalog_table)
                ),
                (collection,),
            ).fetchall()
            return [
                IndexSpec(field=row[1], name=row[0], unique=bool(row[2]))
                for row in rows
            ]
        finally:
            conn.close()

    def _migrate_legacy_blob(self):
        conn = self._connect()
        try:
            has_legacy = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tinydb'"
            ).fetchone()
            if not has_legacy:
                return
            row = conn.execute("SELECT data FROM tinydb WHERE id = 1").fetchone()
            if not row or not row[0]:  # pragma: no cover - corrupt legacy fallback
                conn.execute("DROP TABLE tinydb")
                conn.commit()
                return
            data = json.loads(row[0])
            for collection, docs in data.items():
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS {0} (_id TEXT PRIMARY KEY, data TEXT NOT NULL)".format(
                        _quote_identifier(collection)
                    )
                )
                rows = [
                    (str(doc.get("_id", eid)), _json_dumps(doc))
                    for eid, doc in (docs or {}).items()
                    if isinstance(doc, dict)
                ]
                if rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO {0} (_id, data) VALUES (?, ?)".format(
                            _quote_identifier(collection)
                        ),
                        rows,
                    )
            conn.execute("DROP TABLE tinydb")
            conn.commit()
        finally:
            conn.close()

    def list_collections(self):
        self._migrate_legacy_blob()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '__tinymongo_%'"
            ).fetchall()
            return sorted(row[0] for row in rows)
        finally:
            conn.close()

    def create_collection(self, collection):
        self._migrate_legacy_blob()
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS {0} (_id TEXT PRIMARY KEY, data TEXT NOT NULL)".format(
                    _quote_identifier(collection)
                )
            )
            conn.commit()
        finally:
            conn.close()

    @_write_locked
    def drop_collection(self, collection):
        existed = collection in self.list_collections()
        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            conn.execute(
                "DROP TABLE IF EXISTS {0}".format(_quote_identifier(collection))
            )
            conn.execute(
                "DELETE FROM {0} WHERE collection_name = ?".format(
                    _quote_identifier(self.index_catalog_table)
                ),
                (collection,),
            )
            conn.commit()
            return existed
        finally:
            conn.close()

    @_write_locked
    def insert_many(self, collection, docs, bypass_document_validation=False):
        self.create_collection(collection)
        existing_docs = self.find(collection, {})
        self.validate_unique_post_image(collection, existing_docs + docs)
        rows = [(str(doc["_id"]), _json_dumps(doc)) for doc in docs]
        conn = self._connect()
        try:
            sql = "INSERT INTO {0} (_id, data) VALUES (?, ?)".format(
                _quote_identifier(collection)
            )
            conn.executemany(sql, rows)
            conn.commit()
            return list(range(len(rows)))
        except sqlite3.IntegrityError as exc:
            raise DuplicateKeyError(str(exc))
        finally:
            conn.close()

    def find(self, collection, filter_doc=None, sort=None, skip=None, limit=None):
        self.create_collection(collection)
        indexed = self._find_indexed_scalar_with_array_union(collection, filter_doc)
        if indexed is not None:
            return indexed
        if requires_python_filter(filter_doc):
            return [
                doc
                for doc in self._all_docs_unfiltered(collection)
                if matches_filter(doc, filter_doc)
            ]
        try:
            where, params = self.compiler.compile(filter_doc)
            sql = "SELECT data FROM {0}{1}".format(_quote_identifier(collection), where)
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
            return [_json_loads(row[0]) for row in rows]
        except Exception:
            return [
                doc
                for doc in self._all_docs_unfiltered(collection)
                if matches_filter(doc, filter_doc)
            ]

    def _find_indexed_scalar_with_array_union(self, collection, filter_doc):
        equality = _simple_scalar_equality(filter_doc)
        if equality is None:
            return None
        field, _ = equality
        if not any(spec.field == field for spec in self.get_index_specs(collection)):
            return None

        where, params = self.compiler.compile(filter_doc)
        path = _sql_literal(_json_path(field))
        scalar_sql = (
            "SELECT data FROM {table}{where} "
            "AND json_type(data, {path}) IN "
            "('text', 'integer', 'real', 'true', 'false')"
        ).format(
            table=_quote_identifier(collection),
            where=where,
            path=path,
        )
        array_sql = (
            "SELECT data FROM {table} WHERE json_type(data, {path}) = 'array'"
        ).format(table=_quote_identifier(collection), path=path)
        conn = self._connect()
        try:
            scalar_rows = conn.execute(scalar_sql, params).fetchall()
            array_rows = conn.execute(array_sql).fetchall()
        finally:
            conn.close()

        candidates = [_json_loads(row[0]) for row in scalar_rows + array_rows]
        return [doc for doc in candidates if matches_filter(doc, filter_doc)]

    def _all_docs_unfiltered(self, collection):
        self.create_collection(collection)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT data FROM {0}".format(_quote_identifier(collection))
            ).fetchall()
            return [_json_loads(row[0]) for row in rows]
        finally:
            conn.close()

    @_write_locked
    def replace_one(self, collection, doc_id, replacement):
        self.create_collection(collection)
        self.validate_unique_post_image(
            collection,
            [
                replacement if doc.get("_id") == doc_id else doc
                for doc in self.find(collection, {})
            ],
        )
        conn = self._connect()
        try:
            try:
                conn.execute(
                    "UPDATE {0} SET data = ? WHERE _id = ?".format(
                        _quote_identifier(collection)
                    ),
                    (_json_dumps(replacement), str(doc_id)),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise DuplicateKeyError(str(exc))
        finally:
            conn.close()

    @_write_locked
    def delete_ids(self, collection, ids):
        if not ids:
            return
        self.create_collection(collection)
        conn = self._connect()
        try:
            conn.executemany(
                "DELETE FROM {0} WHERE _id = ?".format(_quote_identifier(collection)),
                [(str(doc_id),) for doc_id in ids],
            )
            conn.commit()
        finally:
            conn.close()

    @_write_locked
    def create_index(self, collection, spec):
        spec = self._coerce_index_spec(spec)
        self.create_collection(collection)
        existing = self._check_index_compatibility(collection, spec)
        if existing is not None:
            return existing.name
        if spec.unique:
            validate_unique_documents(self.find(collection, {}), [spec])
        name = self._physical_index_name(collection, spec)
        path = _sql_literal(_json_path(spec.field))
        expression = "json_extract(data, {0})".format(path)
        if spec.unique:
            expression = "tinymongo_unique_token(data, {0})".format(
                _sql_literal(spec.field)
            )
        conn = self._connect()
        try:
            try:
                self._ensure_index_catalog(conn)
                conn.execute(
                    "CREATE {0}INDEX IF NOT EXISTS {1} ON {2} "
                    "({3})".format(
                        "UNIQUE " if spec.unique else "",
                        _quote_identifier(name),
                        _quote_identifier(collection),
                        expression,
                    )
                )
                if spec.unique:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS {0} ON {1} "
                        "(json_extract(data, {2}))".format(
                            _quote_identifier(name + "_lookup"),
                            _quote_identifier(collection),
                            path,
                        )
                    )
                conn.execute(
                    "INSERT INTO {0} (collection_name, index_name, field_name, unique_flag) "
                    "VALUES (?, ?, ?, ?)".format(
                        _quote_identifier(self.index_catalog_table)
                    ),
                    (collection, spec.name, spec.field, int(spec.unique)),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise DuplicateKeyError(str(exc))
        finally:
            conn.close()
        return spec.name

    @_write_locked
    def drop_index(self, collection, name_or_field):
        spec = next(
            (
                item
                for item in self.get_index_specs(collection)
                if name_or_field in (item.name, item.field)
            ),
            None,
        )
        if spec is None:
            raise OperationFailure("Index not found: {0}".format(name_or_field))
        physical_name = self._physical_index_name(collection, spec)
        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            conn.execute(
                "DROP INDEX IF EXISTS {0}".format(_quote_identifier(physical_name))
            )
            conn.execute(
                "DROP INDEX IF EXISTS {0}".format(
                    _quote_identifier(physical_name + "_lookup")
                )
            )
            conn.execute(
                "DELETE FROM {0} WHERE collection_name = ? AND index_name = ?".format(
                    _quote_identifier(self.index_catalog_table)
                ),
                (collection, spec.name),
            )
            conn.commit()
        finally:
            conn.close()


class DuckDBTableBackend(TableBackend):
    dialect = "duckdb"
    extension = ".duckdb"
    index_catalog_table = "__tinymongo_indexes"

    def __init__(
        self,
        path,
        threads=None,
        duckdb_config=None,
        database=None,
        dsn=None,
    ):
        duckdb = _import_optional_driver(
            "duckdb",
            "duckdb/parquet",
            'pip install "tinymongo[duckdb]"',
        )
        self.duckdb = duckdb
        super(DuckDBTableBackend, self).__init__(
            path,
            threads=threads,
            duckdb_config=duckdb_config,
            database=database,
            dsn=dsn,
        )

    def _connect(self):
        conn = self.duckdb.connect(self.path)
        if self.threads:
            conn.execute("PRAGMA threads={0}".format(int(self.threads)))
        self._configure_duckdb_connection(conn)
        return conn

    def _ensure_index_catalog(self, conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS {0} ("
            "collection_name VARCHAR NOT NULL, index_name VARCHAR NOT NULL, "
            "field_name VARCHAR NOT NULL, unique_flag BOOLEAN NOT NULL, "
            "PRIMARY KEY (collection_name, index_name))".format(
                _quote_identifier(self.index_catalog_table)
            )
        )

    def get_index_specs(self, collection):
        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            rows = conn.execute(
                "SELECT index_name, field_name, unique_flag FROM {0} "
                "WHERE collection_name = ? ORDER BY index_name".format(
                    _quote_identifier(self.index_catalog_table)
                ),
                (collection,),
            ).fetchall()
            return [
                IndexSpec(field=row[1], name=row[0], unique=bool(row[2]))
                for row in rows
            ]
        finally:
            conn.close()

    def _configure_duckdb_connection(self, conn):
        for key, value in (self.duckdb_config or {}).items():
            try:
                conn.execute("SET {0}={1}".format(key, _sql_literal(value)))
            except Exception:
                pass

        for stmt in _duckdb_secret_sql_from_env() + _duckdb_setup_sql_from_env():
            try:
                conn.execute(stmt)
            except Exception:
                pass

    def _migrate_legacy_blob(self):
        conn = self._connect()
        try:
            tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
            if "tinydb" not in tables:
                return
            try:
                row = conn.execute("SELECT data FROM tinydb WHERE id = 1").fetchone()
            except Exception:  # pragma: no cover - corrupt legacy fallback
                row = None
            if row and row[0]:  # pragma: no branch
                data = json.loads(row[0])
                for collection, docs in data.items():
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS {0} (_id VARCHAR PRIMARY KEY, data VARCHAR NOT NULL)".format(
                            _quote_identifier(collection)
                        )
                    )
                    rows = [
                        (str(doc.get("_id", eid)), _json_dumps(doc))
                        for eid, doc in (docs or {}).items()
                        if isinstance(doc, dict)
                    ]
                    if rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO {0} (_id, data) VALUES (?, ?)".format(
                                _quote_identifier(collection)
                            ),
                            rows,
                        )
            conn.execute("DROP TABLE IF EXISTS tinydb")
        finally:
            conn.close()

    def list_collections(self):
        self._migrate_legacy_blob()
        conn = self._connect()
        try:
            rows = conn.execute("SHOW TABLES").fetchall()
            return sorted(
                row[0] for row in rows if not row[0].startswith("__tinymongo_")
            )
        finally:
            conn.close()

    def create_collection(self, collection):
        self._migrate_legacy_blob()
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS {0} (_id VARCHAR PRIMARY KEY, data VARCHAR NOT NULL)".format(
                    _quote_identifier(collection)
                )
            )
        finally:
            conn.close()

    @_write_locked
    def drop_collection(self, collection):
        existed = collection in self.list_collections()
        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            conn.execute(
                "DROP TABLE IF EXISTS {0}".format(_quote_identifier(collection))
            )
            conn.execute(
                "DELETE FROM {0} WHERE collection_name = ?".format(
                    _quote_identifier(self.index_catalog_table)
                ),
                (collection,),
            )
            return existed
        finally:
            conn.close()

    @_write_locked
    def insert_many(self, collection, docs, bypass_document_validation=False):
        self.create_collection(collection)
        existing_docs = self.find(collection, {})
        self.validate_unique_post_image(collection, existing_docs + docs)
        rows = [(str(doc["_id"]), _json_dumps(doc)) for doc in docs]
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO {0} (_id, data) VALUES (?, ?)".format(
                    _quote_identifier(collection)
                ),
                rows,
            )
            return list(range(len(rows)))
        except Exception as exc:
            constraint_error = getattr(self.duckdb, "ConstraintException", ())
            if constraint_error and isinstance(exc, constraint_error):
                raise DuplicateKeyError(str(exc))
            raise
        finally:
            conn.close()

    def find(self, collection, filter_doc=None, sort=None, skip=None, limit=None):
        self.create_collection(collection)
        if requires_python_filter(filter_doc):
            return [
                doc
                for doc in self._all_docs_unfiltered(collection)
                if matches_filter(doc, filter_doc)
            ]
        try:
            where, params = self.compiler.compile(filter_doc)
            sql = "SELECT data FROM {0}{1}".format(_quote_identifier(collection), where)
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
            return [_json_loads(row[0]) for row in rows]
        except Exception:
            return [
                doc
                for doc in self._all_docs_unfiltered(collection)
                if matches_filter(doc, filter_doc)
            ]

    def _all_docs_unfiltered(self, collection):
        self.create_collection(collection)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT data FROM {0}".format(_quote_identifier(collection))
            ).fetchall()
            return [_json_loads(row[0]) for row in rows]
        finally:
            conn.close()

    @_write_locked
    def replace_one(self, collection, doc_id, replacement):
        self.create_collection(collection)
        self.validate_unique_post_image(
            collection,
            [
                replacement if doc.get("_id") == doc_id else doc
                for doc in self.find(collection, {})
            ],
        )
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE {0} SET data = ? WHERE _id = ?".format(
                    _quote_identifier(collection)
                ),
                (_json_dumps(replacement), str(doc_id)),
            )
        finally:
            conn.close()

    @_write_locked
    def delete_ids(self, collection, ids):
        if not ids:
            return
        self.create_collection(collection)
        conn = self._connect()
        try:
            conn.executemany(
                "DELETE FROM {0} WHERE _id = ?".format(_quote_identifier(collection)),
                [(str(doc_id),) for doc_id in ids],
            )
        finally:
            conn.close()

    @_write_locked
    def create_index(self, collection, spec):
        spec = self._coerce_index_spec(spec)
        self.create_collection(collection)
        existing = self._check_index_compatibility(collection, spec)
        if existing is not None:
            return existing.name
        if spec.unique:
            validate_unique_documents(self.find(collection, {}), [spec])
        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            conn.execute(
                "INSERT INTO {0} VALUES (?, ?, ?, ?)".format(
                    _quote_identifier(self.index_catalog_table)
                ),
                (collection, spec.name, spec.field, spec.unique),
            )
        finally:
            conn.close()
        return spec.name

    @_write_locked
    def drop_index(self, collection, name_or_field):
        spec = next(
            (
                item
                for item in self.get_index_specs(collection)
                if name_or_field in (item.name, item.field)
            ),
            None,
        )
        if spec is None:
            raise OperationFailure("Index not found: {0}".format(name_or_field))
        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            conn.execute(
                "DELETE FROM {0} WHERE collection_name = ? AND index_name = ?".format(
                    _quote_identifier(self.index_catalog_table)
                ),
                (collection, spec.name),
            )
        finally:
            conn.close()


class ParquetDuckDBBackend(DuckDBTableBackend):
    dialect = "duckdb"
    extension = ".parquet"

    def __init__(
        self,
        path,
        threads=None,
        duckdb_config=None,
        database=None,
        dsn=None,
    ):
        self.directory = path
        if not _is_object_store_uri(self.directory):
            os.makedirs(self.directory, exist_ok=True)
        self._is_object_store = _is_object_store_uri(self.directory)
        super(ParquetDuckDBBackend, self).__init__(
            ":memory:",
            threads=threads,
            duckdb_config=duckdb_config or _duckdb_object_store_settings(),
            database=database,
            dsn=dsn,
        )

    def _collection_path(self, collection):
        return _join_uri(self.directory, collection + ".parquet")

    @contextmanager
    def _write_lock(self):
        """Serialize local Parquet read-modify-write operations."""
        if self._is_object_store:
            yield
            return

        lock_path = os.path.join(self.directory, ".tinymongo.lock")
        rlock = _local_rlocks.setdefault(lock_path, threading.RLock())
        first_acquire = _acquire_rlock(rlock)
        file_lock = None
        try:
            if first_acquire and portalocker is not None:  # pragma: no branch
                file_lock = portalocker.Lock(lock_path, timeout=30)
                file_lock.acquire()
            yield
        finally:
            if file_lock is not None:  # pragma: no branch
                file_lock.release()
            rlock.release()

    def _connect(self):
        conn = super(ParquetDuckDBBackend, self)._connect()
        if self._is_object_store:
            self._load_object_store_extensions(conn)
        return conn

    def _load_object_store_extensions(self, conn):
        scheme = urlparse(self.directory).scheme.lower()
        extensions = (
            ["azure"] if scheme in {"az", "azure", "abfs", "abfss"} else ["httpfs"]
        )
        for extension in extensions:
            for command in ("INSTALL", "LOAD"):
                try:
                    conn.execute("{0} {1}".format(command, extension))
                except Exception:
                    pass

    def list_collections(self):
        if self._is_object_store:
            pattern = _join_uri(self.directory, "*.parquet")
            conn = self._connect()
            try:
                rows = conn.execute("SELECT file FROM glob(?)", (pattern,)).fetchall()
            except Exception:
                return []
            finally:
                conn.close()
            return sorted(
                os.path.basename(row[0])[: -len(".parquet")]
                for row in rows
                if str(row[0]).endswith(".parquet")
                and not os.path.basename(row[0]).startswith("__tinymongo_")
            )

        if not os.path.isdir(self.directory):
            return []
        return sorted(
            name[: -len(".parquet")]
            for name in os.listdir(self.directory)
            if name.endswith(".parquet") and not name.startswith("__tinymongo_")
        )

    def create_collection(self, collection):
        if not self._is_object_store:  # pragma: no branch
            os.makedirs(self.directory, exist_ok=True)

    def drop_collection(self, collection):
        path = self._collection_path(collection)
        with self._write_lock():
            data_exists = (
                collection in self.list_collections()
                if self._is_object_store
                else os.path.exists(path)
            )
            metadata_exists = bool(self.get_index_specs(collection))
            if not data_exists and not metadata_exists:
                return False
            if data_exists:
                if self._is_object_store:
                    self._write_rows(collection, [])
                else:
                    os.remove(path)
            metadata_rows = [
                row
                for row in self._read_all_rows(INDEX_CATALOG_TABLE)
                if _json_loads(row[1]).get("collection") != collection
            ]
            self._write_rows(INDEX_CATALOG_TABLE, metadata_rows)
            return True

    def get_index_specs(self, collection):
        specs = []
        for _, data in self._read_all_rows(INDEX_CATALOG_TABLE):
            document = _json_loads(data)
            if document.get("collection") == collection:
                specs.append(IndexSpec.from_metadata(document["spec"]))
        return specs

    def _read_all_rows(self, collection):
        path = self._collection_path(collection)
        if not self._is_object_store and not os.path.exists(path):
            return []
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT _id, data FROM read_parquet(?)", (path,)
            ).fetchall()
        except Exception as exc:
            if not self._is_object_store and os.path.exists(path):
                raise StorageCorruptionError(
                    "Cannot read Parquet collection {0}: {1}".format(collection, exc)
                ) from exc
            return []
        finally:
            conn.close()

    def _write_rows(self, collection, rows):
        path = self._collection_path(collection)
        output_path = path
        tmp = None
        if not self._is_object_store:  # pragma: no branch
            fd, tmp = tempfile.mkstemp(
                prefix="tmp_{0}_".format(collection),
                suffix=".parquet",
                dir=self.directory,
            )
            os.close(fd)
            os.remove(tmp)
            output_path = tmp
        conn = self._connect()
        try:
            conn.execute("CREATE TABLE docs(_id VARCHAR, data VARCHAR)")
            if rows:  # pragma: no branch
                conn.executemany("INSERT INTO docs VALUES (?, ?)", rows)
            conn.execute("COPY docs TO ? (FORMAT PARQUET)", (output_path,))
        finally:
            conn.close()
        if tmp is not None:  # pragma: no branch
            try:
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

    def insert_many(self, collection, docs, bypass_document_validation=False):
        with self._write_lock():
            rows = self._read_all_rows(collection)
            existing_docs = [_json_loads(row[1]) for row in rows]
            self.validate_unique_post_image(collection, existing_docs + docs)
            existing = {row[0] for row in rows}
            new_rows = []
            for doc in docs:
                doc_id = str(doc["_id"])
                if doc_id in existing:
                    raise DuplicateKeyError("_id:{0} already exists".format(doc["_id"]))
                new_rows.append((doc_id, _json_dumps(doc)))
                existing.add(doc_id)
            self._write_rows(collection, rows + new_rows)
        return list(range(len(new_rows)))

    def find(self, collection, filter_doc=None, sort=None, skip=None, limit=None):
        path = self._collection_path(collection)
        if not self._is_object_store and not os.path.exists(path):
            return []
        if requires_python_filter(filter_doc):
            return [
                _json_loads(row[1])
                for row in self._read_all_rows(collection)
                if matches_filter(_json_loads(row[1]), filter_doc)
            ]
        try:
            where, params = self.compiler.compile(filter_doc)
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT data FROM read_parquet(?)" + where,
                    [path] + params,
                ).fetchall()
            finally:
                conn.close()
            return [_json_loads(row[0]) for row in rows]
        except Exception:
            return [
                _json_loads(row[1])
                for row in self._read_all_rows(collection)
                if matches_filter(_json_loads(row[1]), filter_doc)
            ]

    def replace_one(self, collection, doc_id, replacement):
        with self._write_lock():
            current_rows = self._read_all_rows(collection)
            self.validate_unique_post_image(
                collection,
                [
                    replacement if row_id == str(doc_id) else _json_loads(data)
                    for row_id, data in current_rows
                ],
            )
            rows = [
                (row_id, _json_dumps(replacement) if row_id == str(doc_id) else data)
                for row_id, data in current_rows
            ]
            self._write_rows(collection, rows)

    def delete_ids(self, collection, ids):
        with self._write_lock():
            id_set = {str(doc_id) for doc_id in ids}
            rows = [
                row for row in self._read_all_rows(collection) if row[0] not in id_set
            ]
            self._write_rows(collection, rows)

    def create_index(self, collection, spec):
        spec = self._coerce_index_spec(spec)
        with self._write_lock():
            path = self._collection_path(collection)
            if (
                collection not in self.list_collections()
                if self._is_object_store
                else not os.path.exists(path)
            ):
                self._write_rows(collection, [])
            existing = self._check_index_compatibility(collection, spec)
            if existing is not None:
                return existing.name
            if spec.unique:
                validate_unique_documents(self.find(collection, {}), [spec])
            rows = self._read_all_rows(INDEX_CATALOG_TABLE)
            document = {
                "_id": index_catalog_id(collection, spec.name),
                "collection": collection,
                "spec": spec.to_metadata(),
            }
            rows.append((document["_id"], _json_dumps(document)))
            self._write_rows(INDEX_CATALOG_TABLE, rows)
        return spec.name

    def drop_index(self, collection, name_or_field):
        with self._write_lock():
            spec = next(
                (
                    item
                    for item in self.get_index_specs(collection)
                    if name_or_field in (item.name, item.field)
                ),
                None,
            )
            if spec is None:
                raise OperationFailure("Index not found: {0}".format(name_or_field))
            rows = [
                row
                for row in self._read_all_rows(INDEX_CATALOG_TABLE)
                if row[0] != index_catalog_id(collection, spec.name)
            ]
            self._write_rows(INDEX_CATALOG_TABLE, rows)


class RemoteSQLTableBackend(TableBackend):
    """Shared table backend for remote transactional SQL databases."""

    placeholder = "%s"
    json_type = "TEXT"
    metadata_table = "__tinymongo_collections"
    index_catalog_table = "__tinymongo_indexes"

    def __init__(
        self,
        path,
        threads=None,
        duckdb_config=None,
        database=None,
        dsn=None,
    ):
        if not dsn:
            raise ValueError("{0} backend requires a DSN".format(self.dialect))
        super(RemoteSQLTableBackend, self).__init__(
            path,
            threads=threads,
            duckdb_config=duckdb_config,
            database=database,
            dsn=dsn,
        )

    def _connect(self):  # pragma: no cover - implemented by concrete drivers
        raise NotImplementedError

    @contextmanager
    def _write_lock(self):
        """Let the remote transaction and native unique index serialize writes."""
        yield

    def _quote(self, name):
        return _quote_identifier(name)

    def _table_name(self, collection):
        return "{0}__{1}".format(self.database or "default", collection)

    def _execute(self, conn, sql, params=None):
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params or ())
            return cursor
        except Exception:
            try:
                cursor.close()
            except Exception:
                pass
            raise

    def _executemany(self, conn, sql, params):
        cursor = conn.cursor()
        try:
            cursor.executemany(sql, params)
            return cursor
        except Exception:
            try:
                cursor.close()
            except Exception:
                pass
            raise

    def _commit(self, conn):
        try:
            conn.commit()
        except Exception:
            pass

    def _close_cursor(self, cursor):
        try:
            cursor.close()
        except Exception:
            pass

    def _is_duplicate_error(self, error):
        sqlstate = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
        code = error.args[0] if getattr(error, "args", ()) else None
        message = str(error).lower()
        return (
            sqlstate == "23505"
            or code == 1062
            or "duplicate key" in message
            or "unique constraint" in message
        )

    def _ensure_metadata(self, conn):
        self._execute(
            conn,
            "CREATE TABLE IF NOT EXISTS {0} "
            "(database_name VARCHAR(255) NOT NULL, "
            "collection_name VARCHAR(255) NOT NULL, "
            "PRIMARY KEY (database_name, collection_name))".format(
                self._quote(self.metadata_table)
            ),
        )
        self._commit(conn)

    def _ensure_index_catalog(self, conn):
        self._execute(
            conn,
            "CREATE TABLE IF NOT EXISTS {0} "
            "(database_name VARCHAR(255) NOT NULL, "
            "collection_name VARCHAR(255) NOT NULL, "
            "index_name VARCHAR(255) NOT NULL, "
            "field_name VARCHAR(512) NOT NULL, "
            "unique_flag BOOLEAN NOT NULL, "
            "PRIMARY KEY (database_name, collection_name, index_name))".format(
                self._quote(self.index_catalog_table)
            ),
        )
        self._commit(conn)

    def get_index_specs(self, collection):
        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            cursor = self._execute(
                conn,
                "SELECT index_name, field_name, unique_flag FROM {0} "
                "WHERE database_name = {1} AND collection_name = {1} "
                "ORDER BY index_name".format(
                    self._quote(self.index_catalog_table), self.placeholder
                ),
                (self.database, collection),
            )
            try:
                return [
                    IndexSpec(field=row[1], name=row[0], unique=bool(row[2]))
                    for row in cursor.fetchall()
                ]
            finally:
                self._close_cursor(cursor)
        finally:
            conn.close()

    def _physical_index_name(self, collection, spec):
        identity = "{0}\x00{1}\x00{2}".format(
            self.database or "default", collection, spec.name
        )
        digest = hashlib.sha256(identity.encode("utf8")).hexdigest()[:32]
        return "__tm_idx_{0}".format(digest)

    def _create_native_index(self, conn, collection, spec):
        raise NotImplementedError  # pragma: no cover - implemented by drivers

    def validate_unique_post_image(self, collection, documents):
        documents = list(documents)
        specs = self.get_index_specs(collection)
        _reject_remote_unique_arrays(documents, specs)
        validate_unique_documents(documents, specs)

    def _drop_native_index(self, conn, collection, spec):
        raise NotImplementedError  # pragma: no cover - implemented by drivers

    def _record_collection(self, conn, collection):
        self._ensure_metadata(conn)
        self._insert_metadata(conn, collection)
        self._commit(conn)

    def _insert_metadata(self, conn, collection):
        raise NotImplementedError  # pragma: no cover - implemented by drivers

    def list_databases(self):
        conn = self._connect()
        try:
            self._ensure_metadata(conn)
            cursor = self._execute(
                conn,
                "SELECT DISTINCT database_name FROM {0} ORDER BY database_name".format(
                    self._quote(self.metadata_table)
                ),
            )
            try:
                return [row[0] for row in cursor.fetchall()]
            finally:
                self._close_cursor(cursor)
        finally:
            conn.close()

    def list_collections(self):
        conn = self._connect()
        try:
            self._ensure_metadata(conn)
            cursor = self._execute(
                conn,
                "SELECT collection_name FROM {0} WHERE database_name = {1} "
                "ORDER BY collection_name".format(
                    self._quote(self.metadata_table), self.placeholder
                ),
                (self.database,),
            )
            try:
                return [row[0] for row in cursor.fetchall()]
            finally:
                self._close_cursor(cursor)
        finally:
            conn.close()

    def create_collection(self, collection):
        conn = self._connect()
        try:
            self._execute(
                conn,
                "CREATE TABLE IF NOT EXISTS {0} "
                "(_id VARCHAR(255) PRIMARY KEY, data {1} NOT NULL)".format(
                    self._quote(self._table_name(collection)), self.json_type
                ),
            )
            self._record_collection(conn, collection)
            self._commit(conn)
        finally:
            conn.close()

    def drop_collection(self, collection):
        existed = collection in self.list_collections()
        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            self._execute(
                conn,
                "DROP TABLE IF EXISTS {0}".format(
                    self._quote(self._table_name(collection))
                ),
            )
            self._execute(
                conn,
                "DELETE FROM {0} WHERE database_name = {1} AND collection_name = {1}".format(
                    self._quote(self.metadata_table), self.placeholder
                ),
                (self.database, collection),
            )
            self._execute(
                conn,
                "DELETE FROM {0} WHERE database_name = {1} AND collection_name = {1}".format(
                    self._quote(self.index_catalog_table), self.placeholder
                ),
                (self.database, collection),
            )
            self._commit(conn)
            return existed
        finally:
            conn.close()

    def insert_many(self, collection, docs, bypass_document_validation=False):
        self.create_collection(collection)
        current = self._all_docs_unfiltered(collection)
        self.validate_unique_post_image(collection, current + docs)
        rows = [(str(doc["_id"]), _json_dumps(doc)) for doc in docs]
        conn = self._connect()
        try:
            self._insert_rows(conn, collection, rows, bypass_document_validation)
            self._commit(conn)
            return list(range(len(rows)))
        except Exception as exc:
            if self._is_duplicate_error(exc):
                raise DuplicateKeyError(str(exc))
            raise
        finally:
            conn.close()

    def _insert_rows(self, conn, collection, rows, bypass_document_validation):
        raise NotImplementedError  # pragma: no cover - implemented by drivers

    def _data_placeholder(self):
        return self.placeholder

    def find(self, collection, filter_doc=None, sort=None, skip=None, limit=None):
        self.create_collection(collection)
        if not filter_doc:
            return self._all_docs_unfiltered(collection)
        if isinstance(filter_doc, dict) and set(filter_doc.keys()) == {"_id"}:
            doc = self._find_by_id(collection, filter_doc["_id"])
            return [doc] if doc else []
        return [
            doc
            for doc in self._all_docs_unfiltered(collection)
            if matches_filter(doc, filter_doc)
        ]

    def _find_by_id(self, collection, doc_id):
        conn = self._connect()
        try:
            cursor = self._execute(
                conn,
                "SELECT data FROM {0} WHERE _id = {1}".format(
                    self._quote(self._table_name(collection)), self.placeholder
                ),
                (str(doc_id),),
            )
            try:
                row = cursor.fetchone()
                return _json_loads(row[0]) if row else None
            finally:
                self._close_cursor(cursor)
        finally:
            conn.close()

    def _all_docs_unfiltered(self, collection):
        conn = self._connect()
        try:
            cursor = self._execute(
                conn,
                "SELECT data FROM {0}".format(
                    self._quote(self._table_name(collection))
                ),
            )
            try:
                return [_json_loads(row[0]) for row in cursor.fetchall()]
            finally:
                self._close_cursor(cursor)
        finally:
            conn.close()

    def replace_one(self, collection, doc_id, replacement):
        self.create_collection(collection)
        self.validate_unique_post_image(
            collection,
            [
                replacement if str(doc.get("_id")) == str(doc_id) else doc
                for doc in self._all_docs_unfiltered(collection)
            ],
        )
        conn = self._connect()
        try:
            try:
                self._execute(
                    conn,
                    "UPDATE {0} SET data = {1} WHERE _id = {2}".format(
                        self._quote(self._table_name(collection)),
                        self._data_placeholder(),
                        self.placeholder,
                    ),
                    (_json_dumps(replacement), str(doc_id)),
                )
                self._commit(conn)
            except Exception as exc:
                if self._is_duplicate_error(exc):
                    raise DuplicateKeyError(str(exc))
                raise
        finally:
            conn.close()

    def delete_ids(self, collection, ids):
        if not ids:
            return
        self.create_collection(collection)
        conn = self._connect()
        try:
            self._executemany(
                conn,
                "DELETE FROM {0} WHERE _id = {1}".format(
                    self._quote(self._table_name(collection)), self.placeholder
                ),
                [(str(doc_id),) for doc_id in ids],
            )
            self._commit(conn)
        finally:
            conn.close()

    def create_index(self, collection, spec):
        spec = self._coerce_index_spec(spec)
        self.create_collection(collection)
        existing = self._check_index_compatibility(collection, spec)
        if existing is not None:
            return existing.name
        if spec.unique:
            documents = self.find(collection, {})
            _reject_remote_unique_arrays(documents, [spec])
            validate_unique_documents(documents, [spec])

        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            self._create_native_index(conn, collection, spec)
            self._execute(
                conn,
                "INSERT INTO {0} "
                "(database_name, collection_name, index_name, field_name, unique_flag) "
                "VALUES ({1}, {1}, {1}, {1}, {1})".format(
                    self._quote(self.index_catalog_table), self.placeholder
                ),
                (self.database, collection, spec.name, spec.field, spec.unique),
            )
            self._commit(conn)
        except Exception as exc:
            if spec.unique and self._is_duplicate_error(exc):
                raise DuplicateKeyError(str(exc))
            raise
        finally:
            conn.close()
        return spec.name

    def drop_index(self, collection, name_or_field):
        spec = next(
            (
                item
                for item in self.get_index_specs(collection)
                if name_or_field in (item.name, item.field)
            ),
            None,
        )
        if spec is None:
            raise OperationFailure("Index not found: {0}".format(name_or_field))

        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            self._drop_native_index(conn, collection, spec)
            self._execute(
                conn,
                "DELETE FROM {0} WHERE database_name = {1} "
                "AND collection_name = {1} AND index_name = {1}".format(
                    self._quote(self.index_catalog_table), self.placeholder
                ),
                (self.database, collection, spec.name),
            )
            self._commit(conn)
        finally:
            conn.close()


class PostgresTableBackend(RemoteSQLTableBackend):
    dialect = "postgres"
    json_type = "JSONB"

    def __init__(self, path, threads=None, duckdb_config=None, database=None, dsn=None):
        psycopg = _import_optional_driver(
            "psycopg",
            "postgres",
            'pip install "tinymongo[postgres]" or pip install "psycopg[binary]>=3.1"',
        )
        self.psycopg = psycopg
        super(PostgresTableBackend, self).__init__(
            path,
            threads=threads,
            duckdb_config=duckdb_config,
            database=database,
            dsn=dsn,
        )

    def _connect(self):
        return self.psycopg.connect(self.dsn)

    def _data_placeholder(self):
        return self.placeholder + "::jsonb"

    def _create_native_index(self, conn, collection, spec):
        path = ", ".join(_sql_literal(part) for part in spec.field.split("."))
        expression = "COALESCE(jsonb_extract_path(data, {0}), 'null'::jsonb)".format(
            path
        )
        # JSONB equality is type-aware and normalizes equivalent numbers. Arrays
        # are indexed whole; Python validation supplies Mongo-like multikey fan-out.
        self._execute(
            conn,
            "CREATE {0}INDEX {1} ON {2} (({3}))".format(
                "UNIQUE " if spec.unique else "",
                self._quote(self._physical_index_name(collection, spec)),
                self._quote(self._table_name(collection)),
                expression,
            ),
        )

    def _drop_native_index(self, conn, collection, spec):
        self._execute(
            conn,
            "DROP INDEX IF EXISTS {0}".format(
                self._quote(self._physical_index_name(collection, spec))
            ),
        )

    def _insert_metadata(self, conn, collection):
        self._execute(
            conn,
            "INSERT INTO {0} (database_name, collection_name) VALUES ({1}, {1}) "
            "ON CONFLICT (database_name, collection_name) DO NOTHING".format(
                self._quote(self.metadata_table), self.placeholder
            ),
            (self.database, collection),
        )

    def _insert_rows(self, conn, collection, rows, bypass_document_validation):
        sql = "INSERT INTO {0} (_id, data) VALUES ({1}, {2})".format(
            self._quote(self._table_name(collection)),
            self.placeholder,
            self._data_placeholder(),
        )
        self._executemany(conn, sql, rows)


class MySQLTableBackend(RemoteSQLTableBackend):
    dialect = "mysql"
    json_type = "JSON"

    def __init__(self, path, threads=None, duckdb_config=None, database=None, dsn=None):
        pymysql = _import_optional_driver(
            "pymysql",
            "mariadb/mysql",
            'pip install "tinymongo[mysql]" or pip install "PyMySQL>=1.1"',
        )
        self.pymysql = pymysql
        super(MySQLTableBackend, self).__init__(
            path,
            threads=threads,
            duckdb_config=duckdb_config,
            database=database,
            dsn=dsn,
        )

    def _quote(self, name):
        return "`" + str(name).replace("`", "``") + "`"

    def _connect(self):
        parsed = urlparse(self.dsn)
        if parsed.scheme:
            query = parse_qs(parsed.query)
            kwargs = {
                "host": parsed.hostname or "localhost",
                "port": parsed.port or 3306,
                "user": unquote(parsed.username or ""),
                "password": unquote(parsed.password or ""),
                "database": parsed.path.lstrip("/") or None,
                "charset": query.get("charset", ["utf8mb4"])[0],
            }
            return self.pymysql.connect(**kwargs)
        return self.pymysql.connect(host=self.dsn)

    def _generated_index_column(self, collection, spec):
        return self._physical_index_name(collection, spec).replace("_idx_", "_key_")

    def _create_native_index(self, conn, collection, spec):
        json_path = "$" + "".join(
            "." + json.dumps(part, ensure_ascii=False) for part in spec.field.split(".")
        )
        value = "JSON_EXTRACT(data, {0})".format(_sql_literal(json_path))
        value_type = "JSON_TYPE({0})".format(value)
        token_expression = (
            "CASE "
            "WHEN {value} IS NULL OR {value_type} = 'NULL' THEN 'null:' "
            "WHEN {value_type} = 'BOOLEAN' "
            "THEN CONCAT('bool:', JSON_UNQUOTE({value})) "
            "WHEN {value_type} IN ('INTEGER', 'DOUBLE', 'DECIMAL') "
            "THEN CONCAT('number:', CAST(CAST(JSON_UNQUOTE({value}) "
            "AS DECIMAL(65, 30)) AS CHAR)) "
            "WHEN {value_type} = 'STRING' "
            "THEN CONCAT('string:', CAST({value} AS CHAR)) "
            "ELSE CONCAT('json:', CAST({value} AS CHAR)) END"
        ).format(value=value, value_type=value_type)
        expression = "SHA2({0}, 256)".format(token_expression)
        # The typed scalar token mirrors Python uniqueness checks. Arrays remain
        # whole JSON values here, so native race protection is scalar-only.
        table = self._quote(self._table_name(collection))
        column = self._quote(self._generated_index_column(collection, spec))
        self._execute(
            conn,
            "ALTER TABLE {0} ADD COLUMN {1} CHAR(64) "
            "CHARACTER SET ascii COLLATE ascii_bin "
            "GENERATED ALWAYS AS ({2}) STORED".format(table, column, expression),
        )
        self._execute(
            conn,
            "CREATE {0}INDEX {1} ON {2} ({3})".format(
                "UNIQUE " if spec.unique else "",
                self._quote(self._physical_index_name(collection, spec)),
                table,
                column,
            ),
        )

    def _drop_native_index(self, conn, collection, spec):
        table = self._quote(self._table_name(collection))
        self._execute(
            conn,
            "DROP INDEX {0} ON {1}".format(
                self._quote(self._physical_index_name(collection, spec)), table
            ),
        )
        self._execute(
            conn,
            "ALTER TABLE {0} DROP COLUMN {1}".format(
                table, self._quote(self._generated_index_column(collection, spec))
            ),
        )

    def _insert_metadata(self, conn, collection):
        self._execute(
            conn,
            "INSERT IGNORE INTO {0} (database_name, collection_name) "
            "VALUES ({1}, {1})".format(
                self._quote(self.metadata_table), self.placeholder
            ),
            (self.database, collection),
        )

    def _insert_rows(self, conn, collection, rows, bypass_document_validation):
        sql = "INSERT INTO {0} (_id, data) VALUES ({1}, {1})".format(
            self._quote(self._table_name(collection)), self.placeholder
        )
        self._executemany(conn, sql, rows)
