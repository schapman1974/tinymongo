import json
import os
import re
import sqlite3
from urllib.parse import parse_qs, unquote, urlparse

from .errors import DuplicateKeyError


_MISSING = object()
_OBJECT_STORE_SCHEMES = {"s3", "gs", "gcs", "az", "azure", "abfs", "abfss"}


def _is_object_store_uri(path):
    return urlparse(str(path)).scheme.lower() in _OBJECT_STORE_SCHEMES


def _join_uri(base, *parts):
    if _is_object_store_uri(base):
        return "/".join([str(base).rstrip("/")] + [str(part).strip("/") for part in parts])
    return os.path.join(base, *parts)


def _sql_literal(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return "'" + str(value).replace("'", "''") + "'"


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
        "s3_region": _env_first("TINYMONGO_S3_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"),
        "s3_access_key_id": _env_first("TINYMONGO_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
        "s3_secret_access_key": _env_first("TINYMONGO_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"),
        "s3_session_token": _env_first("TINYMONGO_S3_SESSION_TOKEN", "AWS_SESSION_TOKEN"),
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
            "(TYPE azure, CONNECTION_STRING {0})".format(
                _sql_literal(azure_connection)
            )
        )
    return statements


def _duckdb_setup_sql_from_env():
    sql = os.environ.get("TINYMONGO_DUCKDB_SETUP_SQL", "")
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


def _json_dumps(doc):
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value):
    return json.loads(value)


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
        return expected in actual
    return actual == expected


def matches_filter(doc, filter_doc):
    if not filter_doc:
        return True
    if not isinstance(filter_doc, dict):
        return False

    for key, expected in filter_doc.items():
        if key == "$and":
            return all(matches_filter(doc, spec) for spec in expected)
        if key == "$or":
            return any(matches_filter(doc, spec) for spec in expected)
        if key == "$nor":
            return not any(matches_filter(doc, spec) for spec in expected)

        actual = _get_nested(doc, key)
        if isinstance(expected, dict):
            for operator, operand in expected.items():
                exists = actual is not _MISSING
                if operator == "$exists":
                    if bool(operand) != exists:
                        return False
                elif operator == "$gt":
                    if not exists or not actual > operand:
                        return False
                elif operator == "$gte":
                    if not exists or not actual >= operand:
                        return False
                elif operator == "$lt":
                    if not exists or not actual < operand:
                        return False
                elif operator == "$lte":
                    if not exists or not actual <= operand:
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
                    if not isinstance(actual, list) or not all(item in actual for item in operand):
                        return False
                elif operator == "$regex":
                    if not exists or re.search(operand, str(actual)) is None:
                        return False
                elif operator == "$not":
                    if matches_filter({key: actual}, {key: operand if isinstance(operand, dict) else {"$eq": operand}}):
                        return False
                elif operator == "$eq":
                    if not exists or not _value_matches(actual, operand):
                        return False
                else:
                    return False
        elif not _value_matches(actual, expected):
            return False
    return True


class SQLCompiler(object):
    def __init__(self, dialect):
        self.dialect = dialect

    def json_value(self, field, value=None, numeric=False):
        path = "$." + field
        if self.dialect == "sqlite":
            expression = "json_extract(data, ?)"
            return expression, [path]
        if numeric:
            return "CAST(json_extract_string(data, ?) AS DOUBLE)", [path]
        return "json_extract_string(data, ?)", [path]

    def json_exists(self, field):
        path = "$." + field
        if self.dialect == "sqlite":
            return "json_type(data, ?) IS NOT NULL", [path]
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
                        clauses.append("_id {0} ?".format("!=" if operator == "$ne" else "="))
                        params.append(str(operand))
                    elif operator == "$in":
                        values = operand if isinstance(operand, list) else [operand]
                        clauses.append(
                            "_id IN (" + ", ".join("?" for _ in values) + ")"
                        )
                        params.extend(str(item) for item in values)
                    else:
                        raise ValueError("Unsupported _id SQL operator: {0}".format(operator))
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
                    clauses.append(expression + (" != ?" if operator == "$ne" else " = ?"))
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
                    raise ValueError("Unsupported SQL filter operator: {0}".format(operator))
            return "(" + " AND ".join(clauses) + ")", params

        expression, expression_params = self.json_value(field, value)
        return expression + " = ?", expression_params + [self._sql_value(value)]

    def _sql_value(self, value):
        if self.dialect == "duckdb" and isinstance(value, bool):
            return "true" if value else "false"
        return value


class TableBackend(object):
    dialect = None
    extension = None

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
        if not _is_object_store_uri(path):
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def close(self):
        pass

    def list_collections(self):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def create_collection(self, collection):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def drop_collection(self, collection):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def insert_many(self, collection, docs, bypass_document_validation=False):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def all_docs(self, collection):
        return self.find(collection, {})

    def find(self, collection, filter_doc=None, sort=None, skip=None, limit=None):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def find_one(self, collection, filter_doc=None):
        docs = self.find(collection, filter_doc, limit=1)
        return docs[0] if docs else None

    def update_many(self, collection, filter_doc, update_doc, multi=True):
        matches = self.find(collection, filter_doc)
        if not multi:
            matches = matches[:1]
        updated_ids = []
        for doc in matches:
            updated = self.apply_update(doc, update_doc)
            self.replace_one(collection, doc["_id"], updated)
            updated_ids.append(doc["_id"])
        return updated_ids

    def replace_one(self, collection, doc_id, replacement):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def delete_many(self, collection, filter_doc, multi=True):
        matches = self.find(collection, filter_doc)
        if not multi:
            matches = matches[:1]
        ids = [doc["_id"] for doc in matches]
        self.delete_ids(collection, ids)
        return ids

    def delete_ids(self, collection, ids):  # pragma: no cover - abstract backend contract
        raise NotImplementedError

    def create_index(self, collection, field):
        return field

    def drop_index(self, collection, field):
        return None

    def list_indexes(self, collection):
        return [{"name": "_id_", "key": [("_id", 1)]}]

    def apply_update(self, doc, update_doc):
        from .tinymongo import _apply_update_document

        return _apply_update_document(doc, update_doc)


class SQLiteTableBackend(TableBackend):
    dialect = "sqlite"
    extension = ".sqlite"

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

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

    def drop_collection(self, collection):
        existed = collection in self.list_collections()
        conn = self._connect()
        try:
            conn.execute("DROP TABLE IF EXISTS {0}".format(_quote_identifier(collection)))
            conn.commit()
            return existed
        finally:
            conn.close()

    def insert_many(self, collection, docs, bypass_document_validation=False):
        self.create_collection(collection)
        rows = [(str(doc["_id"]), _json_dumps(doc)) for doc in docs]
        conn = self._connect()
        try:
            sql = "INSERT {0} INTO {1} (_id, data) VALUES (?, ?)".format(
                "OR REPLACE" if bypass_document_validation else "",
                _quote_identifier(collection),
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
                doc for doc in self._all_docs_unfiltered(collection)
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

    def replace_one(self, collection, doc_id, replacement):
        self.create_collection(collection)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE {0} SET data = ? WHERE _id = ?".format(
                    _quote_identifier(collection)
                ),
                (_json_dumps(replacement), str(doc_id)),
            )
            conn.commit()
        finally:
            conn.close()

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

    def create_index(self, collection, field):
        self.create_collection(collection)
        name = "{0}_{1}_idx".format(collection, field.replace(".", "_"))
        path = "'$." + field.replace("'", "''") + "'"
        conn = self._connect()
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS {0} ON {1} (json_extract(data, {2}))".format(
                    _quote_identifier(name), _quote_identifier(collection), path
                )
            )
            conn.commit()
        finally:
            conn.close()
        return field


class DuckDBTableBackend(TableBackend):
    dialect = "duckdb"
    extension = ".duckdb"

    def __init__(
        self,
        path,
        threads=None,
        duckdb_config=None,
        database=None,
        dsn=None,
    ):
        try:
            import duckdb
        except Exception as exc:  # pragma: no cover - optional dependency fallback
            raise ImportError("duckdb backend requires the duckdb package") from exc
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
            if row and row[0]:
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
            return sorted(row[0] for row in rows if not row[0].startswith("__tinymongo_"))
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

    def drop_collection(self, collection):
        existed = collection in self.list_collections()
        conn = self._connect()
        try:
            conn.execute("DROP TABLE IF EXISTS {0}".format(_quote_identifier(collection)))
            return existed
        finally:
            conn.close()

    def insert_many(self, collection, docs, bypass_document_validation=False):
        self.create_collection(collection)
        rows = [(str(doc["_id"]), _json_dumps(doc)) for doc in docs]
        conn = self._connect()
        try:
            if bypass_document_validation:
                conn.executemany(
                    "INSERT OR REPLACE INTO {0} (_id, data) VALUES (?, ?)".format(
                        _quote_identifier(collection)
                    ),
                    rows,
                )
            else:
                conn.executemany(
                    "INSERT INTO {0} (_id, data) VALUES (?, ?)".format(
                        _quote_identifier(collection)
                    ),
                    rows,
                )
            return list(range(len(rows)))
        except Exception as exc:
            raise DuplicateKeyError(str(exc))
        finally:
            conn.close()

    def find(self, collection, filter_doc=None, sort=None, skip=None, limit=None):
        self.create_collection(collection)
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
                doc for doc in self._all_docs_unfiltered(collection)
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

    def replace_one(self, collection, doc_id, replacement):
        self.create_collection(collection)
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

    def _connect(self):
        conn = super(ParquetDuckDBBackend, self)._connect()
        if self._is_object_store:
            self._load_object_store_extensions(conn)
        return conn

    def _load_object_store_extensions(self, conn):
        scheme = urlparse(self.directory).scheme.lower()
        extensions = ["azure"] if scheme in {"az", "azure", "abfs", "abfss"} else ["httpfs"]
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
                os.path.basename(row[0])[:-len(".parquet")]
                for row in rows
                if str(row[0]).endswith(".parquet")
            )

        if not os.path.isdir(self.directory):
            return []
        return sorted(
            name[:-len(".parquet")]
            for name in os.listdir(self.directory)
            if name.endswith(".parquet")
        )

    def create_collection(self, collection):
        if not self._is_object_store:
            os.makedirs(self.directory, exist_ok=True)

    def drop_collection(self, collection):
        path = self._collection_path(collection)
        existed = (
            collection in self.list_collections()
            if self._is_object_store
            else os.path.exists(path)
        )
        if existed:
            if self._is_object_store:
                self._write_rows(collection, [])
            else:
                os.remove(path)
        return existed

    def _read_all_rows(self, collection):
        path = self._collection_path(collection)
        if not self._is_object_store and not os.path.exists(path):
            return []
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT _id, data FROM read_parquet(?)", (path,)
            ).fetchall()
        except Exception:
            return []
        finally:
            conn.close()

    def _write_rows(self, collection, rows):
        path = self._collection_path(collection)
        conn = self._connect()
        try:
            conn.execute("CREATE TABLE docs(_id VARCHAR, data VARCHAR)")
            if rows:
                conn.executemany("INSERT INTO docs VALUES (?, ?)", rows)
            conn.execute("COPY docs TO ? (FORMAT PARQUET)", (path,))
        finally:
            conn.close()

    def insert_many(self, collection, docs, bypass_document_validation=False):
        rows = self._read_all_rows(collection)
        existing = {row[0] for row in rows}
        new_rows = []
        for doc in docs:
            doc_id = str(doc["_id"])
            if not bypass_document_validation and doc_id in existing:
                raise DuplicateKeyError("_id:{0} already exists".format(doc["_id"]))
            if bypass_document_validation and doc_id in existing:
                rows = [row for row in rows if row[0] != doc_id]
            new_rows.append((doc_id, _json_dumps(doc)))
            existing.add(doc_id)
        self._write_rows(collection, rows + new_rows)
        return list(range(len(new_rows)))

    def find(self, collection, filter_doc=None, sort=None, skip=None, limit=None):
        path = self._collection_path(collection)
        if not self._is_object_store and not os.path.exists(path):
            return []
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
        rows = [
            (row_id, _json_dumps(replacement) if row_id == str(doc_id) else data)
            for row_id, data in self._read_all_rows(collection)
        ]
        self._write_rows(collection, rows)

    def delete_ids(self, collection, ids):
        id_set = {str(doc_id) for doc_id in ids}
        rows = [row for row in self._read_all_rows(collection) if row[0] not in id_set]
        self._write_rows(collection, rows)


class RemoteSQLTableBackend(TableBackend):
    """Shared table backend for remote transactional SQL databases."""

    placeholder = "%s"
    json_type = "TEXT"
    metadata_table = "__tinymongo_collections"

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
            self._commit(conn)
            return existed
        finally:
            conn.close()

    def insert_many(self, collection, docs, bypass_document_validation=False):
        self.create_collection(collection)
        rows = [(str(doc["_id"]), _json_dumps(doc)) for doc in docs]
        conn = self._connect()
        try:
            self._insert_rows(conn, collection, rows, bypass_document_validation)
            self._commit(conn)
            return list(range(len(rows)))
        except Exception as exc:
            raise DuplicateKeyError(str(exc))
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
            doc for doc in self._all_docs_unfiltered(collection)
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
        conn = self._connect()
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

    def create_index(self, collection, field):
        self.create_collection(collection)
        return field


class PostgresTableBackend(RemoteSQLTableBackend):
    dialect = "postgres"
    json_type = "JSONB"

    def __init__(self, path, threads=None, duckdb_config=None, database=None, dsn=None):
        try:
            import psycopg
        except Exception as exc:  # pragma: no cover - optional dependency fallback
            raise ImportError("postgres backend requires psycopg") from exc
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
        if bypass_document_validation:
            sql = (
                "INSERT INTO {0} (_id, data) VALUES ({1}, {2}) "
                "ON CONFLICT (_id) DO UPDATE SET data = EXCLUDED.data"
            ).format(
                self._quote(self._table_name(collection)),
                self.placeholder,
                self._data_placeholder(),
            )
        else:
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
        try:
            import pymysql
        except Exception as exc:  # pragma: no cover - optional dependency fallback
            raise ImportError("mariadb/mysql backend requires PyMySQL") from exc
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
        if bypass_document_validation:
            sql = "REPLACE INTO {0} (_id, data) VALUES ({1}, {1})".format(
                self._quote(self._table_name(collection)), self.placeholder
            )
        else:
            sql = "INSERT INTO {0} (_id, data) VALUES ({1}, {1})".format(
                self._quote(self._table_name(collection)), self.placeholder
            )
        self._executemany(conn, sql, rows)
