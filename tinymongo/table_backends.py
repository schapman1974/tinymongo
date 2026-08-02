import ast
import copy
import hashlib
import importlib
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from decimal import Decimal
from functools import wraps
from urllib.parse import parse_qs, unquote, urlparse
from typing import Optional

from .bson_codec import contains_extended_value
from .bson_codec import dumps as bson_json_dumps
from .bson_codec import loads as bson_json_loads
from .bson_codec import storage_values_equal
from .bson_types import (
    bson_identity_key,
    bson_number_decimal,
    bson_query_range_matches,
    bson_values_equal,
    decimal128_type,
    is_bson_regex,
    is_bson_number,
    is_native_regex,
    regex_compile_components,
    regex_components,
    regex_flags_text,
)
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
    index_spec_signature,
    index_tokens,
    parse_index_spec,
    validate_unique_documents,
)
from .parquet_storage import _acquire_rlock, _local_rlocks, portalocker
from .projection import project_document


_MISSING = object()
_DECIMAL128 = decimal128_type()
_ID_RATIO_HEX_TAG = "exact-ratio-hex-v1"
_SAFE_JSON_INTEGER_BITS = 1800
_SQLITE_UNIQUE_TOKEN_VERSION = 3
_REMOTE_UNIQUE_TOKEN_VERSION = 1
_OBJECT_STORE_SCHEMES = {"s3", "gs", "gcs", "az", "azure", "abfs", "abfss"}
_PHYSICAL_ID_PREFIX = "__tinymongo_id_v2__:"
_LOGICAL_FILTER_OPERATOR_NAMES = ("$and", "$or", "$nor")
_IGNORED_FILTER_OPERATOR_NAMES = ("$comment",)
_FIELD_FILTER_OPERATOR_NAMES = (
    "$all",
    "$elemMatch",
    "$eq",
    "$exists",
    "$gt",
    "$gte",
    "$in",
    "$lt",
    "$lte",
    "$ne",
    "$nin",
    "$not",
    "$mod",
    "$options",
    "$regex",
    "$size",
    "$type",
)
_LOGICAL_FILTER_OPERATORS = frozenset(_LOGICAL_FILTER_OPERATOR_NAMES)
_IGNORED_FILTER_OPERATORS = frozenset(_IGNORED_FILTER_OPERATOR_NAMES)
_FIELD_FILTER_OPERATORS = frozenset(_FIELD_FILTER_OPERATOR_NAMES)
_KNOWN_UNSUPPORTED_FILTER_OPERATORS = frozenset(
    (
        "$bitsAllClear",
        "$bitsAllSet",
        "$bitsAnyClear",
        "$bitsAnySet",
        "$expr",
        "$geoIntersects",
        "$geoWithin",
        "$jsonSchema",
        "$near",
        "$nearSphere",
        "$text",
        "$where",
    )
)
_KNOWN_MONGODB_FILTER_OPERATORS = (
    _LOGICAL_FILTER_OPERATORS
    | _IGNORED_FILTER_OPERATORS
    | _FIELD_FILTER_OPERATORS
    | _KNOWN_UNSUPPORTED_FILTER_OPERATORS
)
_BSON_QUERY_TYPE_CODES = {
    -1: "minKey",
    1: "double",
    2: "string",
    3: "object",
    4: "array",
    5: "binData",
    6: "undefined",
    7: "objectId",
    8: "bool",
    9: "date",
    10: "null",
    11: "regex",
    12: "dbPointer",
    13: "javascript",
    14: "symbol",
    15: "javascriptWithScope",
    16: "int",
    17: "timestamp",
    18: "long",
    19: "decimal",
    127: "maxKey",
}
_BSON_QUERY_TYPE_ALIASES = frozenset(
    tuple(_BSON_QUERY_TYPE_CODES.values()) + ("number",)
)
_SIGNED_INT64_MIN = -(2**63)
_SIGNED_INT64_MAX = 2**63 - 1
_SIGNED_INT32_MAX = 2**31 - 1


def _large_numeric_ratio(key):
    """Return an exact ratio that is unsafe for bounded int-to-text runtimes."""

    if (
        isinstance(key, tuple)
        and len(key) == 2
        and all(isinstance(part, int) for part in key)
        and any(abs(part).bit_length() > _SAFE_JSON_INTEGER_BITS for part in key)
    ):
        return key
    return None


def _signed_hex(value):
    sign = "-" if value < 0 else ""
    return sign + format(abs(value), "x")


def _safe_registered_key(family, key):
    ratio = _large_numeric_ratio(key) if family == "number" else None
    if ratio is None:
        return key
    return [_ID_RATIO_HEX_TAG, _signed_hex(ratio[0]), _signed_hex(ratio[1])]


def _legacy_large_ratio_physical_id_key(value):
    """Recreate the pre-safe typed key without Python's decimal digit limit."""

    identity = bson_identity_key(value)
    if identity is None or identity[0] != "number":
        return None
    ratio = _large_numeric_ratio(identity[1])
    if ratio is None:
        return None
    numerator = format(Decimal(ratio[0]), "f")
    denominator = format(Decimal(ratio[1]), "f")
    canonical = (
        '["registered-scalar",["number",[' + numerator + "," + denominator + "]]]"
    ).encode("ascii")
    return _PHYSICAL_ID_PREFIX + hashlib.sha256(canonical).hexdigest()


def _legacy_stringified_id(value):
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and abs(value).bit_length() > _SAFE_JSON_INTEGER_BITS
    ):
        return format(Decimal(value), "f")
    return str(value)


def _canonical_id_value(value):
    """Build a stable serialization of the registry's BSON identity key."""
    identity = bson_identity_key(value)
    if identity is not None:
        family, key = identity
        key = _safe_registered_key(family, key)
        return [
            "registered-scalar",
            json.loads(
                bson_json_dumps(
                    [family, key],
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
            ),
        ]

    # MongoDB does not permit arrays as `_id` values, and TinyMongo only
    # historically accepted containers as a local extension. Preserve that
    # behavior with recursive typed keys while keeping registered scalars
    # governed exclusively by the BSON registry above.
    if isinstance(value, Mapping):
        return [
            "object",
            [[str(key), _canonical_id_value(item)] for key, item in value.items()],
        ]
    if isinstance(value, (list, tuple)):
        return ["array", [_canonical_id_value(item) for item in value]]

    # Preserve deterministic identity for any codec-supported scalar outside
    # the current BSON registry.
    encoded = json.loads(
        bson_json_dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    return ["encoded-scalar", encoded]


def _physical_id_key(value):
    """Return the compact typed key stored in SQL and Parquet `_id` columns."""
    canonical = json.dumps(
        _canonical_id_value(value),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return _PHYSICAL_ID_PREFIX + hashlib.sha256(canonical).hexdigest()


def _physical_id_candidates(value):
    """Return the v2 key followed by compatible legacy stringified keys."""

    current = _physical_id_key(value)
    legacy = [_legacy_stringified_id(value)]
    old_typed = _legacy_large_ratio_physical_id_key(value)
    if old_typed is not None:
        legacy.insert(0, old_typed)
    if not isinstance(value, bool) and isinstance(value, int):
        try:
            float_value = float(value)
        except OverflowError:
            pass
        else:
            if float_value == value:
                legacy.append(str(float_value))
                if value == 0:
                    legacy.append(str(-0.0))
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        legacy.append(str(int(value)))

    return tuple(dict.fromkeys([current] + legacy))


def _requires_legacy_id_scan(value):
    """Return whether equivalent legacy IDs can have unpredictable strings."""

    if isinstance(value, (Mapping, list, tuple)):
        return True
    identity = bson_identity_key(value)
    return identity is not None and identity[0] == "datetime"


def _validate_physical_ids(existing_documents, new_documents):
    """Reject BSON-equivalent `_id` values before reaching native storage."""
    seen = {
        _physical_id_key(document["_id"])
        for document in existing_documents
        if "_id" in document
    }
    for document in new_documents:
        key = _physical_id_key(document["_id"])
        if key in seen:
            raise DuplicateKeyError("_id:{0} already exists".format(document["_id"]))
        seen.add(key)


def _legacy_id_values_equal(left, right):
    """Compare legacy container IDs without relying on mapping field order."""

    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return len(left) == len(right) and all(
            key in right and _legacy_id_values_equal(value, right[key])
            for key, value in left.items()
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _legacy_id_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return bson_values_equal(left, right)


def _restore_legacy_document_id(row_id, document, requested_id=_MISSING):
    """Recover the original order of a legacy container ID from its row key."""

    if (
        not isinstance(row_id, str)
        or row_id.startswith(_PHYSICAL_ID_PREFIX)
        or not isinstance(document, dict)
        or "_id" not in document
    ):
        return document

    if requested_id is not _MISSING and str(requested_id) == row_id:
        candidate = requested_id
    else:
        try:
            candidate = ast.literal_eval(row_id)
        except (SyntaxError, ValueError):
            return document

    if (
        not isinstance(candidate, (Mapping, list, tuple))
        or str(candidate) != row_id
        or not _legacy_id_values_equal(candidate, document["_id"])
    ):
        return document

    restored = dict(document)
    restored["_id"] = copy.deepcopy(candidate)
    return restored


def _matching_physical_row_id(rows, value, decode_row=None):
    """Resolve a current or legacy physical row key by its decoded BSON ID."""
    expected = _physical_id_key(value)
    for row in rows:
        row_id = row[0]
        try:
            document = (
                decode_row(row) if decode_row is not None else _json_loads(row[1])
            )
            if _physical_id_key(document["_id"]) == expected:
                return row_id
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _local_matching_physical_row_id(conn, quoted_table, value):
    candidates = _physical_id_candidates(value)
    rows = conn.execute(
        "SELECT _id, data FROM {0} WHERE _id IN ({1})".format(
            quoted_table,
            ", ".join("?" for _ in candidates),
        ),
        candidates,
    ).fetchall()
    stored_id = _matching_physical_row_id(rows, value)
    if stored_id is not None:
        return stored_id

    if not _requires_legacy_id_scan(value):
        return None

    # Container IDs and equivalent datetime representations can have legacy
    # strings that cannot be enumerated. Restrict the compatibility scan to
    # those uncommon values so ordinary missing-ID lookups remain indexed.
    rows = conn.execute("SELECT _id, data FROM {0}".format(quoted_table)).fetchall()
    return _matching_physical_row_id(rows, value)


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


def _is_query_array_index(part):
    return part == "0" or (
        part
        and part[0] in "123456789"
        and all("0" <= character <= "9" for character in part)
    )


def _query_path_match_candidates(value, parts):
    """Resolve values plus whether a numeric index selected the endpoint."""

    if not parts:
        return [(value, False)]

    part = parts[0]
    if isinstance(value, Mapping):
        if part not in value:
            return [(_MISSING, False)]
        if len(parts) == 1:
            return [(value[part], False)]
        return _query_path_match_candidates(value[part], parts[1:])

    if isinstance(value, (list, tuple)):
        candidates = []
        if _is_query_array_index(part):
            index = int(part)
            if index < len(value):
                if len(parts) == 1:
                    candidates.append((value[index], True))
                elif isinstance(value[index], (Mapping, list, tuple)):
                    candidates.extend(
                        _query_path_match_candidates(value[index], parts[1:])
                    )

        for member in value:
            if isinstance(member, Mapping):
                candidates.extend(_query_path_match_candidates(member, parts))
        return candidates

    return [(_MISSING, False)]


def _query_path_candidates(value, parts):
    """Resolve query paths through documents and arrays without flattening endpoints."""

    return [
        candidate
        for candidate, _indexed_endpoint in _query_path_match_candidates(value, parts)
    ]


def _values_equal(actual, expected):
    """Compare values recursively with BSON scalar equality semantics."""

    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(
            _values_equal(left, right) for left, right in zip(actual, expected)
        )
    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        actual_items = list(actual.items())
        expected_items = list(expected.items())
        return len(actual_items) == len(expected_items) and all(
            actual_key == expected_key and _values_equal(actual_value, expected_value)
            for (actual_key, actual_value), (expected_key, expected_value) in zip(
                actual_items, expected_items
            )
        )
    return bson_values_equal(actual, expected)


def _value_matches(actual, expected):
    if isinstance(actual, list):
        return _values_equal(actual, expected) or any(
            _values_equal(item, expected) for item in actual
        )
    return _values_equal(actual, expected)


def _query_values_equal(actual, expected, exact=False):
    """Apply MongoDB's null-equals-missing rule at query boundaries."""

    if actual is _MISSING:
        return expected is None
    return (_values_equal if exact else _value_matches)(actual, expected)


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
    if not isinstance(filter_doc, Mapping) or len(filter_doc) != 1:
        return None
    field, expected = next(iter(filter_doc.items()))
    if (
        not isinstance(field, str)
        or field.startswith("$")
        or field == "_id"
        or expected is None
        or isinstance(expected, (Mapping, list, tuple))
        or not isinstance(expected, (bool, int, float, str))
    ):
        return None
    return field, expected


def _reject_remote_unique_values(documents, specs):
    """Fail closed where remote constraints cannot protect Mongo uniqueness."""
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
            if _DECIMAL128 is not None and isinstance(value, _DECIMAL128):
                raise TinyMongoNotSupportedError(
                    "Remote SQL unique index {0!r} does not support Decimal128 "
                    "values; TinyMongo cannot yet derive the same safe native "
                    "token from Decimal128 BID data".format(spec.name)
                )
            identity = bson_identity_key(value)
            if identity is not None and identity[0] in ("binary", "regex"):
                raise TinyMongoNotSupportedError(
                    "Remote SQL unique index {0!r} does not support UUID, "
                    "Binary, or regular-expression values; its native token "
                    "constraint cannot guarantee cross-process BSON identity".format(
                        spec.name
                    )
                )


def _remote_unique_token(document, spec):
    """Return one fixed-width exact BSON token for a remote unique index."""

    _reject_remote_unique_values([document], [spec])
    tokens = index_tokens(document, spec.field)
    if len(tokens) != 1:
        # Arrays are rejected above. Keep this guard explicit so a future
        # multikey expansion cannot silently weaken the one-column native
        # constraint used by the remote backends.
        raise TinyMongoNotSupportedError(
            "Remote SQL unique index {0!r} requires exactly one scalar token "
            "per document".format(spec.name)
        )
    canonical = "remote-unique-v{0}\x00{1}".format(
        _REMOTE_UNIQUE_TOKEN_VERSION,
        tokens[0],
    )
    return hashlib.sha256(canonical.encode("utf8")).hexdigest()


def _comparison_matches(actual, operand, comparison, exact=False):
    value = None if actual is _MISSING else actual
    return bson_query_range_matches(value, operand, comparison, exact=exact)


def _validate_regex_options(options):
    if not isinstance(options, str):
        raise OperationFailure(
            "$options supports only i, m, s, u, and x",
            code=2,
        )
    if any(option not in "imsxu" for option in options):
        raise OperationFailure(
            "$options supports only i, m, s, u, and x",
            code=51108,
        )


def _regex_matches(actual, pattern, options="", exact=False):
    _validate_regex_options(options)
    flags = 0
    if is_bson_regex(pattern):
        if options and regex_flags_text(pattern.flags):
            raise OperationFailure(
                "$options cannot be combined with flags embedded in $regex",
                code=51075,
            )
        pattern, flags = regex_compile_components(pattern)
    for option, flag in (
        ("i", re.IGNORECASE),
        ("m", re.MULTILINE),
        ("s", re.DOTALL),
        ("u", re.UNICODE),
        ("x", re.VERBOSE),
    ):
        if option in str(options):
            flags |= flag
    regex_identity = ("regex", (pattern, regex_flags_text(flags)))
    values = actual if isinstance(actual, list) and not exact else [actual]
    if any(
        is_bson_regex(value) and bson_identity_key(value) == regex_identity
        for value in values
    ):
        return True
    try:
        expression = re.compile(pattern, flags)
    except (TypeError, ValueError, re.error):
        return False
    return any(
        isinstance(value, str) and expression.search(value) is not None
        for value in values
    )


def _query_integer(value, operator, minimum=None, maximum=None, truncate=False):
    """Return one finite integral BSON number for a query operator."""

    if not is_bson_number(value):
        raise OperationFailure("{0} requires numeric values".format(operator), code=2)
    try:
        decimal_value = bson_number_decimal(value)
        if not decimal_value.is_finite():
            raise ValueError
        integer = int(decimal_value)
    except (ArithmeticError, TypeError, ValueError, OverflowError) as error:
        raise OperationFailure(
            "{0} requires finite integral values".format(operator), code=2
        ) from error
    if not truncate and decimal_value != integer:
        raise OperationFailure("{0} requires integral values".format(operator), code=2)
    if minimum is not None and integer < minimum:
        raise OperationFailure(
            "{0} value is below the supported range".format(operator), code=2
        )
    if maximum is not None and integer > maximum:
        raise OperationFailure(
            "{0} value is above the supported range".format(operator), code=2
        )
    return integer


def _normalize_size_operand(operand):
    return _query_integer(
        operand,
        "$size",
        minimum=0,
        maximum=_SIGNED_INT32_MAX,
    )


def _normalize_mod_operand(operand):
    if not isinstance(operand, (list, tuple)) or len(operand) != 2:
        raise OperationFailure("$mod requires an array of two numbers", code=2)
    divisor = _query_integer(
        operand[0],
        "$mod",
        minimum=_SIGNED_INT64_MIN,
        maximum=_SIGNED_INT64_MAX,
        truncate=True,
    )
    remainder = _query_integer(
        operand[1],
        "$mod",
        minimum=_SIGNED_INT64_MIN,
        maximum=_SIGNED_INT64_MAX,
        truncate=True,
    )
    if divisor == 0:
        raise OperationFailure("$mod divisor cannot be zero", code=2)
    return divisor, remainder


def _truncating_remainder(dividend, divisor):
    quotient = abs(dividend) // abs(divisor)
    if (dividend < 0) != (divisor < 0):
        quotient = -quotient
    return dividend - quotient * divisor


def _mod_matches(actual, operand, exact=False):
    divisor, expected_remainder = _normalize_mod_operand(operand)
    values = actual if isinstance(actual, list) and not exact else [actual]
    for value in values:
        if not is_bson_number(value):
            continue
        try:
            decimal_value = bson_number_decimal(value)
            if not decimal_value.is_finite():
                continue
            dividend = int(decimal_value)
        except (ArithmeticError, TypeError, ValueError, OverflowError):
            continue
        if not _SIGNED_INT64_MIN <= dividend <= _SIGNED_INT64_MAX:
            continue
        if _truncating_remainder(dividend, divisor) == expected_remainder:
            return True
    return False


def _normalize_type_operand(operand):
    values = operand if isinstance(operand, (list, tuple)) else [operand]
    if not values:
        raise OperationFailure("$type must match at least one type", code=9)

    aliases = []
    for value in values:
        if isinstance(value, str):
            if value not in _BSON_QUERY_TYPE_ALIASES:
                raise OperationFailure(
                    "Unknown BSON type alias: {0}".format(value), code=2
                )
            alias = value
        elif is_bson_number(value):
            code = _query_integer(value, "$type")
            if code not in _BSON_QUERY_TYPE_CODES:
                raise OperationFailure(
                    "Invalid numerical BSON type code: {0}".format(code), code=2
                )
            alias = _BSON_QUERY_TYPE_CODES[code]
        else:
            raise OperationFailure(
                "$type must be represented as a number or a string", code=14
            )
        if alias not in aliases:
            aliases.append(alias)
    return frozenset(aliases)


def _bson_query_type_names(value):
    if isinstance(value, Mapping):
        return frozenset(("object",))
    if isinstance(value, (list, tuple)):
        return frozenset(("array",))

    identity = bson_identity_key(value)
    if identity is None:
        return frozenset()
    family = identity[0]
    if family == "number":
        if _DECIMAL128 is not None and isinstance(value, _DECIMAL128):
            exact = "decimal"
        elif isinstance(value, float):
            exact = "double"
        elif _SIGNED_INT32_MAX >= value >= -(2**31):
            exact = "int"
        else:
            exact = "long"
        return frozenset(("number", exact))
    return frozenset(
        (
            {
                "binary": "binData",
                "boolean": "bool",
                "datetime": "date",
                "objectid": "objectId",
            }.get(family, family),
        )
    )


def _type_matches(actual, operand, exact=False):
    requested = _normalize_type_operand(operand)
    if requested.intersection(_bson_query_type_names(actual)):
        return True
    return (
        not exact
        and isinstance(actual, list)
        and any(requested.intersection(_bson_query_type_names(item)) for item in actual)
    )


def _elem_match_matches(actual, operand):
    if not isinstance(actual, list):
        return False
    if not operand:
        return any(isinstance(item, (Mapping, list, tuple)) for item in actual)
    operator_expression = any(key in _FIELD_FILTER_OPERATORS for key in operand)
    if operator_expression:
        return any(_field_matches(item, operand, exact=True) for item in actual)
    return any(
        isinstance(item, Mapping)
        and matches_filter(item, operand, _exact_id=False)
        or isinstance(item, (list, tuple))
        and matches_filter(
            item,
            operand,
            _array_document=True,
            _exact_id=False,
        )
        for item in actual
    )


def _field_path_matches(candidates, expected, exact=False, indexed_endpoints=None):
    if indexed_endpoints is None:
        indexed_endpoints = [False] * len(candidates)
    if len(candidates) == 1:
        return _field_matches(
            candidates[0],
            expected,
            exact=exact or indexed_endpoints[0],
        )
    if not isinstance(expected, Mapping) or not any(
        str(key).startswith("$") for key in expected
    ):
        return any(
            _field_matches(
                candidate,
                expected,
                exact=exact or indexed_endpoint,
            )
            for candidate, indexed_endpoint in zip(candidates, indexed_endpoints)
        )

    options = expected.get("$options")
    for operator, operand in expected.items():
        if operator == "$options":
            continue
        clause = {operator: operand}
        if operator == "$regex" and options is not None:
            clause["$options"] = options
        matches = (
            _field_matches(
                candidate,
                clause,
                exact=exact or indexed_endpoint,
            )
            for candidate, indexed_endpoint in zip(candidates, indexed_endpoints)
        )
        negative = operator in ("$ne", "$nin", "$not") or (
            operator == "$exists" and not bool(operand)
        )
        if not (all(matches) if negative else any(matches)):
            return False
    return True


def _normalize_not_operand(operand):
    """Return a matcher expression for one MongoDB-valid ``$not`` operand."""

    if is_bson_regex(operand):
        return {"$regex": operand}
    if not isinstance(operand, Mapping):
        raise OperationFailure(
            "$not argument must be a regex or an object",
            code=2,
        )
    if not operand:
        raise OperationFailure(
            "$not argument must be a non-empty object",
            code=2,
        )
    return operand


def _field_matches(actual, expected, exact=False):
    exists = actual is not _MISSING

    def equality(left, right):
        return _query_values_equal(left, right, exact=exact)

    if not isinstance(expected, Mapping) or not any(
        str(key).startswith("$") for key in expected
    ):
        if is_bson_regex(expected):
            return exists and _regex_matches(actual, expected, exact=exact)
        return equality(actual, expected)

    options = expected.get("$options", "")
    for operator, operand in expected.items():
        if operator == "$options":
            if "$regex" not in expected:
                return False
        elif operator == "$exists":
            if bool(operand) != exists:
                return False
        elif operator == "$gt":
            if not _comparison_matches(
                actual, operand, lambda a, b: a > b, exact=exact
            ):
                return False
        elif operator == "$gte":
            if not _comparison_matches(
                actual, operand, lambda a, b: a >= b, exact=exact
            ):
                return False
        elif operator == "$lt":
            if not _comparison_matches(
                actual, operand, lambda a, b: a < b, exact=exact
            ):
                return False
        elif operator == "$lte":
            if not _comparison_matches(
                actual, operand, lambda a, b: a <= b, exact=exact
            ):
                return False
        elif operator == "$ne":
            if equality(actual, operand):
                return False
        elif operator == "$in":
            values = operand if isinstance(operand, (list, tuple)) else [operand]
            if not any(
                (
                    _regex_matches(actual, item, exact=exact)
                    if is_bson_regex(item)
                    else equality(actual, item)
                )
                for item in values
            ):
                return False
        elif operator == "$nin":
            values = operand if isinstance(operand, (list, tuple)) else [operand]
            if any(
                (
                    _regex_matches(actual, item, exact=exact)
                    if is_bson_regex(item)
                    else equality(actual, item)
                )
                for item in values
            ):
                return False
        elif operator == "$all":
            actual_values = (
                actual if isinstance(actual, list) and not exact else [actual]
            )
            if not operand or not all(
                any(
                    (
                        _elem_match_matches(actual, value["$elemMatch"])
                        if isinstance(value, Mapping) and set(value) == {"$elemMatch"}
                        else (
                            _regex_matches(item, value, exact=exact)
                            if is_bson_regex(value)
                            else _query_values_equal(item, value, exact=True)
                        )
                    )
                    for item in actual_values
                )
                for value in operand
            ):
                return False
        elif operator == "$elemMatch":
            if not exists or not _elem_match_matches(actual, operand):
                return False
        elif operator == "$regex":
            if not exists or not _regex_matches(actual, operand, options, exact=exact):
                return False
        elif operator == "$not":
            nested = _normalize_not_operand(operand)
            if _field_matches(actual, nested, exact=exact):
                return False
        elif operator == "$eq":
            if not equality(actual, operand):
                return False
        elif operator == "$mod":
            if not exists or not _mod_matches(actual, operand, exact=exact):
                return False
        elif operator == "$size":
            if (
                not exists
                or not isinstance(actual, list)
                or len(actual) != _normalize_size_operand(operand)
            ):
                return False
        elif operator == "$type":
            if not exists or not _type_matches(actual, operand, exact=exact):
                return False
        else:
            return False
    return True


def matches_filter(doc, filter_doc, _array_document=False, _exact_id=True):
    if not filter_doc:
        return True
    if not isinstance(filter_doc, Mapping):
        return False

    for key, expected in filter_doc.items():
        if key == "$and":
            if not all(
                matches_filter(doc, spec, _array_document, _exact_id)
                for spec in expected
            ):
                return False
        elif key == "$or":
            if not any(
                matches_filter(doc, spec, _array_document, _exact_id)
                for spec in expected
            ):
                return False
        elif key == "$nor":
            if any(
                matches_filter(doc, spec, _array_document, _exact_id)
                for spec in expected
            ):
                return False
        elif key in _IGNORED_FILTER_OPERATORS:
            continue
        else:
            parts = key.split(".")
            resolved = (
                [(_MISSING, False)]
                if _array_document
                and isinstance(doc, (list, tuple))
                and not _is_query_array_index(parts[0])
                else _query_path_match_candidates(doc, parts)
            )
            candidates = [candidate for candidate, _indexed in resolved]
            indexed_endpoints = [indexed for _candidate, indexed in resolved]
            if not _field_path_matches(
                candidates,
                expected,
                exact=_exact_id and key == "_id",
                indexed_endpoints=indexed_endpoints,
            ):
                return False
    return True


def _validate_regex_transport_literal(value):
    """Reject regex values that cannot cross a BSON persistence boundary."""

    try:
        pattern, options = regex_components(value)
    except (TypeError, UnicodeDecodeError) as error:
        raise OperationFailure(
            "Regular-expression patterns must be valid UTF-8 BSON text",
            code=2,
        ) from error
    if "\x00" in pattern:
        raise OperationFailure(
            "Regular-expression patterns cannot contain NUL",
            code=2,
        )
    return pattern, options


def _validate_regex_literal(value):
    """Reject regex values that cannot be executed by the query matcher."""

    _pattern, options = _validate_regex_transport_literal(value)
    if is_native_regex(value) or "l" in options:
        return
    compile_pattern, flags = regex_compile_components(value)
    try:
        re.compile(compile_pattern, flags)
    except (TypeError, ValueError, re.error) as error:
        raise OperationFailure(
            "Regular expression is not valid for TinyMongo's Python regex engine",
            code=51091,
        ) from error


def _validate_regex_literals(value, compile_patterns=True):
    if is_bson_regex(value):
        if compile_patterns:
            _validate_regex_literal(value)
        else:
            _validate_regex_transport_literal(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            _validate_regex_literals(item, compile_patterns=compile_patterns)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_regex_literals(item, compile_patterns=compile_patterns)


def _regex_query_flags(options):
    _validate_regex_options(options)
    flags = 0
    for option, flag in (
        ("i", re.IGNORECASE),
        ("m", re.MULTILINE),
        ("s", re.DOTALL),
        ("u", re.UNICODE),
        ("x", re.VERBOSE),
    ):
        if option in options:
            flags |= flag
    return flags


def _validate_regex_query_operand(operand, options=""):
    flags = _regex_query_flags(options)
    if is_bson_regex(operand):
        _validate_regex_literal(operand)
        if options and regex_flags_text(operand.flags):
            raise OperationFailure(
                "$options cannot be combined with flags embedded in $regex",
                code=51075,
            )
        return
    if not isinstance(operand, str):
        raise OperationFailure(
            "$regex requires a string or compiled regex value",
            code=2,
        )
    if "\x00" in operand:
        raise OperationFailure(
            "Regular-expression patterns cannot contain NUL",
            code=2,
        )
    try:
        re.compile(operand, flags)
    except (TypeError, ValueError, re.error) as error:
        raise OperationFailure(
            "$regex pattern is not valid for TinyMongo's Python regex engine",
            code=51091,
        ) from error


def _validate_field_filter_operators(expression, *, _inside_elem_match=False):
    """Validate one field's operator document, including nested ``$not``."""

    for operator, operand in expression.items():
        if operator in _IGNORED_FILTER_OPERATORS:
            raise OperationFailure("unknown operator: {0}".format(operator), code=2)
        if operator not in _FIELD_FILTER_OPERATORS:
            if isinstance(operator, str) and operator.startswith("$"):
                if (
                    _inside_elem_match
                    and operator not in _KNOWN_MONGODB_FILTER_OPERATORS
                ):
                    raise OperationFailure(
                        "unknown operator: {0}".format(operator), code=2
                    )
                raise TinyMongoNotSupportedError(
                    "Query operator {0} is not supported by TinyMongo".format(operator)
                )
            raise OperationFailure(
                "Field query documents cannot mix operators and literal fields",
                code=2,
            )
        if operator in ("$gt", "$gte", "$lt", "$lte"):
            if is_bson_regex(operand):
                raise OperationFailure(
                    "Can't have RegEx as arg to non-equality predicate",
                    code=2,
                )
            _validate_regex_literals(operand, compile_patterns=False)
        elif operator == "$eq":
            _validate_regex_literals(operand, compile_patterns=False)
        elif operator == "$ne":
            if is_bson_regex(operand):
                raise OperationFailure("Can't have regex as arg to $ne", code=2)
            _validate_regex_literals(operand, compile_patterns=False)
        elif operator not in (
            "$all",
            "$elemMatch",
            "$in",
            "$nin",
            "$not",
            "$options",
            "$regex",
        ):
            _validate_regex_literals(operand)
        if operator in ("$all", "$in", "$nin") and not isinstance(
            operand, (list, tuple)
        ):
            raise OperationFailure(
                "{0} requires an array".format(operator),
                code=2,
            )
        if operator in ("$in", "$nin"):
            nested_operator_documents = [
                item
                for item in operand
                if isinstance(item, Mapping)
                and any(str(key).startswith("$") for key in item)
            ]
            if any("$regex" in item for item in nested_operator_documents):
                raise OperationFailure(
                    "{0} accepts regex values, not $regex operator documents".format(
                        operator
                    ),
                    code=2,
                )
            if nested_operator_documents:
                raise OperationFailure(
                    "{0} cannot contain nested query operator documents".format(
                        operator
                    ),
                    code=2,
                )
            for item in operand:
                _validate_regex_literals(
                    item,
                    compile_patterns=is_bson_regex(item),
                )
        if operator == "$all":
            for item in operand:
                nested_operators = (
                    [key for key in item if str(key).startswith("$")]
                    if isinstance(item, Mapping)
                    else []
                )
                if not nested_operators:
                    _validate_regex_literals(
                        item,
                        compile_patterns=is_bson_regex(item),
                    )
                    continue
                if set(item) != {"$elemMatch"}:
                    if "$regex" in item:
                        raise OperationFailure(
                            "$all accepts regex values, not $regex operator documents",
                            code=2,
                        )
                    raise OperationFailure(
                        "$all cannot contain nested query operator documents",
                        code=2,
                    )
                _validate_elem_match_operand(item["$elemMatch"])
        if operator == "$regex":
            _validate_regex_query_operand(operand, expression.get("$options", ""))
        if operator == "$options" and "$regex" not in expression:
            raise OperationFailure(
                "$options requires $regex in the same query",
                code=2,
            )
        if operator == "$options":
            _validate_regex_query_operand(expression["$regex"], operand)
        if operator == "$not":
            nested = _normalize_not_operand(operand)
            _validate_field_filter_operators(
                nested,
                _inside_elem_match=_inside_elem_match,
            )
        if operator == "$elemMatch":
            _validate_elem_match_operand(operand)
        if operator == "$mod":
            _normalize_mod_operand(operand)
        if operator == "$size":
            _normalize_size_operand(operand)
        if operator == "$type":
            _normalize_type_operand(operand)


def _validate_elem_match_operand(operand):
    if not isinstance(operand, Mapping):
        raise OperationFailure("$elemMatch requires a query document", code=2)
    if any(key in _LOGICAL_FILTER_OPERATORS for key in operand):
        validate_filter_operators(operand, _inside_elem_match=True)
    elif any(key in _FIELD_FILTER_OPERATORS for key in operand):
        _validate_field_filter_operators(operand, _inside_elem_match=True)
    else:
        validate_filter_operators(operand, _inside_elem_match=True)


def validate_filter_operators(filter_doc, *, _inside_elem_match=False):
    """Reject operators the shared Python matcher cannot evaluate safely."""

    if not isinstance(filter_doc, Mapping):
        raise OperationFailure("Filter must be a query document", code=14)

    for key, expected in filter_doc.items():
        if not isinstance(key, str):
            raise OperationFailure("Filter field names must be strings", code=2)
        if key in _LOGICAL_FILTER_OPERATORS:
            if not isinstance(expected, (list, tuple)) or not expected:
                raise OperationFailure(
                    "{0} requires a non-empty array of query documents".format(key),
                    code=2,
                )
            for specification in expected:
                validate_filter_operators(
                    specification,
                    _inside_elem_match=_inside_elem_match,
                )
            continue
        if key in _IGNORED_FILTER_OPERATORS:
            continue
        if key.startswith("$"):
            if _inside_elem_match and (
                key in _FIELD_FILTER_OPERATORS
                or key not in _KNOWN_MONGODB_FILTER_OPERATORS
            ):
                raise OperationFailure("unknown operator: {0}".format(key), code=2)
            raise TinyMongoNotSupportedError(
                "Query operator {0} is not supported by TinyMongo".format(key)
            )

        if isinstance(expected, Mapping) and any(
            str(operator).startswith("$") for operator in expected
        ):
            _validate_field_filter_operators(
                expected,
                _inside_elem_match=_inside_elem_match,
            )
        else:
            _validate_regex_literals(
                expected,
                compile_patterns=is_bson_regex(expected),
            )


def query_operator_capabilities():
    """Return the exact query operators handled by the shared matcher."""

    return {
        "logical": _LOGICAL_FILTER_OPERATOR_NAMES,
        "ignored": _IGNORED_FILTER_OPERATOR_NAMES,
        "field": _FIELD_FILTER_OPERATOR_NAMES,
    }


def _validate_regex_field_expression(expression):
    """Validate only regex-specific shapes without widening query policy."""

    for operator, operand in expression.items():
        if operator in ("$gt", "$gte", "$lt", "$lte"):
            if is_bson_regex(operand):
                raise OperationFailure(
                    "Can't have RegEx as arg to non-equality predicate",
                    code=2,
                )
            _validate_regex_literals(operand, compile_patterns=False)
        elif operator == "$eq":
            _validate_regex_literals(operand, compile_patterns=False)
        elif operator == "$ne":
            if is_bson_regex(operand):
                raise OperationFailure("Can't have regex as arg to $ne", code=2)
            _validate_regex_literals(operand, compile_patterns=False)
        elif operator in ("$all", "$in", "$nin"):
            if not isinstance(operand, (list, tuple)):
                continue
            if any(isinstance(item, Mapping) and "$regex" in item for item in operand):
                raise OperationFailure(
                    "{0} accepts regex values, not $regex operator documents".format(
                        operator
                    ),
                    code=2,
                )
            for item in operand:
                if (
                    operator == "$all"
                    and isinstance(item, Mapping)
                    and set(item) == {"$elemMatch"}
                ):
                    _validate_regex_elem_match_operand(item["$elemMatch"])
                else:
                    _validate_regex_literals(
                        item,
                        compile_patterns=is_bson_regex(item),
                    )
        elif operator == "$regex":
            _validate_regex_query_operand(operand, expression.get("$options", ""))
        elif operator == "$not":
            if isinstance(operand, Mapping) and any(
                str(key).startswith("$") for key in operand
            ):
                _validate_regex_field_expression(operand)
            else:
                _validate_regex_literals(operand)
        elif operator == "$elemMatch":
            _validate_regex_elem_match_operand(operand)


def _validate_regex_elem_match_operand(operand):
    """Walk regex contexts inside ``$elemMatch`` without full validation."""

    if not isinstance(operand, Mapping):
        return
    if any(key in _LOGICAL_FILTER_OPERATORS for key in operand):
        validate_regex_filter(operand)
    elif any(key in _FIELD_FILTER_OPERATORS for key in operand):
        _validate_regex_field_expression(operand)
    else:
        validate_regex_filter(operand)


def validate_regex_filter(filter_doc):
    """Preflight regex operands without changing other legacy query behavior."""

    if not isinstance(filter_doc, Mapping):
        return
    for key, expected in filter_doc.items():
        if key in _LOGICAL_FILTER_OPERATORS and isinstance(expected, (list, tuple)):
            for specification in expected:
                validate_regex_filter(specification)
        elif isinstance(expected, Mapping) and any(
            str(operator).startswith("$") for operator in expected
        ):
            _validate_regex_field_expression(expected)
        else:
            _validate_regex_literals(
                expected,
                compile_patterns=is_bson_regex(expected),
            )


def _filter_references_id(filter_doc):
    if not isinstance(filter_doc, Mapping):
        return False
    if "_id" in filter_doc:
        return True
    return any(
        _filter_references_id(item)
        for operator in ("$and", "$or", "$nor")
        for item in (
            filter_doc.get(operator, [])
            if isinstance(filter_doc.get(operator, []), (list, tuple))
            else []
        )
    )


def _id_condition_requires_legacy_scan(condition):
    if not isinstance(condition, Mapping):
        return _requires_legacy_id_scan(condition)
    if not any(str(key).startswith("$") for key in condition):
        return _requires_legacy_id_scan(condition)
    for operator, operand in condition.items():
        if operator == "$eq" and _requires_legacy_id_scan(operand):
            return True
        if operator == "$in":
            values = operand if isinstance(operand, list) else [operand]
            if any(_requires_legacy_id_scan(value) for value in values):
                return True
    return False


def _filter_requires_legacy_id_scan(filter_doc):
    if not isinstance(filter_doc, Mapping):
        return False
    if "_id" in filter_doc and _id_condition_requires_legacy_scan(filter_doc["_id"]):
        return True
    return any(
        _filter_requires_legacy_id_scan(item)
        for operator in ("$and", "$or", "$nor")
        for item in (
            filter_doc.get(operator, [])
            if isinstance(filter_doc.get(operator, []), (list, tuple))
            else []
        )
    )


def _postfilter_id_candidates(documents, filter_doc, fallback):
    """Verify typed/legacy ID candidates and recover legacy numeric matches."""

    if not _filter_references_id(filter_doc):
        return documents

    matches = [
        document for document in documents if matches_filter(document, filter_doc)
    ]
    direct_equality = (
        isinstance(filter_doc, Mapping)
        and set(filter_doc) == {"_id"}
        and not isinstance(filter_doc["_id"], Mapping)
    )
    if direct_equality and matches:
        return matches
    if _filter_requires_legacy_id_scan(filter_doc):
        return [
            document for document in fallback() if matches_filter(document, filter_doc)
        ]
    return matches


def requires_python_filter(filter_doc):
    """Return whether SQL JSON scalar comparison could change query meaning."""
    if not filter_doc or not isinstance(filter_doc, Mapping):
        return False
    if not isinstance(filter_doc, dict):
        return True
    if contains_extended_value(filter_doc):
        return True
    for field, expected in filter_doc.items():
        if field in ("$and", "$or", "$nor"):
            # Physical `_id` candidates intentionally include legacy string
            # aliases. They are a safe superset for positive predicates, but
            # negating that SQL superset can discard BSON-distinct legacy rows
            # before the exact Python postfilter gets a chance to recover them.
            if field == "$nor" and _filter_references_id({field: expected}):
                return True
            if any(requires_python_filter(item) for item in expected):
                return True
        else:
            if field.startswith("$"):
                return True
            if "." in field:
                return True
            if field != "_id" and not isinstance(expected, Mapping):
                # A JSON scalar comparison cannot also see members of array fields.
                return True
            if isinstance(expected, Mapping):
                if not isinstance(expected, dict):
                    return True
                if not any(str(operator).startswith("$") for operator in expected):
                    # Literal embedded-document equality is not an operator
                    # expression and TinyDB/SQL parsers otherwise treat its keys
                    # as field or operator names.
                    return True
                if any(
                    operator in expected
                    for operator in (
                        "$eq",
                        "$elemMatch",
                        "$ne",
                        "$in",
                        "$nin",
                        "$all",
                        "$mod",
                        "$regex",
                        "$not",
                        "$options",
                        "$size",
                        "$type",
                    )
                ):
                    return True
                if any(
                    operator in expected for operator in ("$gt", "$gte", "$lt", "$lte")
                ):
                    # Native JSON/SQL predicates do not implement BSON type
                    # bracketing, array-member matching, recursive object/array
                    # comparison, or Decimal128's numeric family. Keep every
                    # range predicate on the shared BSON matcher until a
                    # backend can provide a complete candidate superset.
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
                    if operator == "$eq":
                        candidates = _physical_id_candidates(operand)
                        clauses.append(
                            "(" + " OR ".join("_id = ?" for _ in candidates) + ")"
                        )
                        params.extend(candidates)
                    elif operator == "$ne":
                        # The current typed key is sufficient for a superset.
                        # A legacy string key may also belong to a BSON-distinct
                        # value, so filtering it here could create false negatives.
                        clauses.append("_id != ?")
                        params.append(_physical_id_key(operand))
                    elif operator == "$in":
                        values = operand if isinstance(operand, list) else [operand]
                        candidates = [
                            candidate
                            for item in values
                            for candidate in _physical_id_candidates(item)
                        ]
                        clauses.append(
                            "_id IN (" + ", ".join("?" for _ in candidates) + ")"
                        )
                        params.extend(candidates)
                    else:
                        raise ValueError(
                            "Unsupported _id SQL operator: {0}".format(operator)
                        )
                return "(" + " AND ".join(clauses) + ")", params
            candidates = _physical_id_candidates(value)
            return (
                "(" + " OR ".join("_id = ?" for _ in candidates) + ")",
                list(candidates),
            )

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
            if not storage_values_equal(updated, doc):
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

    def _check_index_compatibility(self, collection, spec, specs=None):
        if specs is None:
            specs = self.get_index_specs(collection)
        by_name = next(
            (current for current in specs if current.name == spec.name),
            None,
        )
        if by_name is not None and by_name != spec:
            raise OperationFailure(
                "An index with the same name or key has different options",
                code=86,
            )
        if by_name is not None:
            # Older releases allowed equivalent specs under different names.
            # An exact name/spec retry remains idempotent even when that legacy
            # duplicate is still present elsewhere in the catalog.
            return by_name
        equivalent = next(
            (
                current
                for current in specs
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
        raise OperationFailure(
            "Index not found: {0}".format(name_or_field),
            code=27,
        )

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

    def _read_connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.create_function(
            "tinymongo_unique_token",
            2,
            _sqlite_unique_token,
            deterministic=True,
        )
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _connect(self):
        conn = self._read_connect()
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_index_catalog(self, conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS {0} ("
            "collection_name TEXT NOT NULL, index_name TEXT NOT NULL, "
            "field_name TEXT NOT NULL, unique_flag INTEGER NOT NULL, "
            "token_version INTEGER NOT NULL DEFAULT {1}, "
            "PRIMARY KEY (collection_name, index_name))".format(
                _quote_identifier(self.index_catalog_table),
                _SQLITE_UNIQUE_TOKEN_VERSION,
            )
        )
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info({0})".format(
                    _quote_identifier(self.index_catalog_table)
                )
            ).fetchall()
        }
        migrated = False
        if "token_version" not in columns:
            conn.execute(
                "ALTER TABLE {0} ADD COLUMN token_version INTEGER NOT NULL "
                "DEFAULT 1".format(_quote_identifier(self.index_catalog_table))
            )
            migrated = True

        stale = conn.execute(
            "SELECT collection_name, index_name, field_name FROM {0} "
            "WHERE unique_flag = 1 AND token_version < ?".format(
                _quote_identifier(self.index_catalog_table)
            ),
            (_SQLITE_UNIQUE_TOKEN_VERSION,),
        ).fetchall()
        for collection, index_name, field in stale:
            spec = IndexSpec(field=field, name=index_name, unique=True)
            physical_name = self._physical_index_name(collection, spec)
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                (physical_name,),
            ).fetchone()
            if exists is not None:
                try:
                    conn.execute("REINDEX {0}".format(_quote_identifier(physical_name)))
                except sqlite3.IntegrityError as exc:
                    # An older token format could admit values which MongoDB
                    # considers equal (for example int/float ``2**60``). The
                    # old expression index is unsafe once this connection has
                    # registered the exact-token function, so remove only its
                    # native constraint and retain the unique catalog entry.
                    # Store-wide write locking plus Python post-image
                    # validation keeps the catalog fail-closed while the user
                    # removes the conflicting row and recreates the index.
                    conn.execute(
                        "DROP INDEX IF EXISTS {0}".format(
                            _quote_identifier(physical_name)
                        )
                    )
                    conn.execute(
                        "UPDATE {0} SET token_version = ? "
                        "WHERE collection_name = ? AND index_name = ?".format(
                            _quote_identifier(self.index_catalog_table)
                        ),
                        (_SQLITE_UNIQUE_TOKEN_VERSION, collection, index_name),
                    )
                    conn.commit()
                    raise DuplicateKeyError(
                        "Cannot upgrade unique index {0!r} on collection {1!r}: "
                        "existing values conflict under exact BSON numeric "
                        "identity. Its native constraint was disabled, but "
                        "TinyMongo will continue enforcing the catalog entry; "
                        "remove the conflict, then drop and recreate the "
                        "index".format(index_name, collection)
                    ) from exc
            conn.execute(
                "UPDATE {0} SET token_version = ? WHERE collection_name = ? "
                "AND index_name = ?".format(
                    _quote_identifier(self.index_catalog_table)
                ),
                (_SQLITE_UNIQUE_TOKEN_VERSION, collection, index_name),
            )
            migrated = True
        if migrated:
            conn.commit()

    def _index_catalog_state(self, conn):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (self.index_catalog_table,),
        ).fetchone()
        if exists is None:
            return "absent"

        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info({0})".format(
                    _quote_identifier(self.index_catalog_table)
                )
            ).fetchall()
        }
        if "token_version" not in columns:
            return "stale"

        stale = conn.execute(
            "SELECT 1 FROM {0} WHERE unique_flag = 1 AND token_version < ? "
            "LIMIT 1".format(_quote_identifier(self.index_catalog_table)),
            (_SQLITE_UNIQUE_TOKEN_VERSION,),
        ).fetchone()
        return "stale" if stale is not None else "ready"

    def get_index_specs(self, collection):
        conn = self._read_connect()
        try:
            state = self._index_catalog_state(conn)
            if state == "absent":
                return []
            if state == "stale":
                # Index-token migrations change persisted expression-index
                # keys. Serialize only this one-time upgrade, then leave normal
                # catalog reads lock-free so SQLite WAL readers stay concurrent.
                conn.close()
                with self._write_lock():
                    migration_conn = self._connect()
                    try:
                        self._ensure_index_catalog(migration_conn)
                    finally:
                        migration_conn.close()
                conn = self._read_connect()
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
            data = _json_loads(row[0])
            for collection, docs in data.items():
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS {0} (_id TEXT PRIMARY KEY, data TEXT NOT NULL)".format(
                        _quote_identifier(collection)
                    )
                )
                rows = [
                    (_physical_id_key(doc.get("_id", eid)), _json_dumps(doc))
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
        _validate_physical_ids(existing_docs, docs)
        rows = [(_physical_id_key(doc["_id"]), _json_dumps(doc)) for doc in docs]
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
            documents = [_json_loads(row[0]) for row in rows]
            return _postfilter_id_candidates(
                documents,
                filter_doc,
                lambda: self._all_docs_unfiltered(collection),
            )
        except Exception:
            return [
                doc
                for doc in self._all_docs_unfiltered(collection)
                if matches_filter(doc, filter_doc)
            ]

    @staticmethod
    def _bounded_slice(documents, skip=0, limit=0):
        """Apply Mongo-style cursor bounds to an already materialized result."""

        end = None if not limit else skip + limit
        return list(documents)[skip:end]

    @staticmethod
    def _sqlite_bounds(skip=0, limit=0):
        """Return a SQLite LIMIT/OFFSET suffix and its bound parameters."""

        if limit:
            return " LIMIT ? OFFSET ?", [limit, skip]
        if skip:
            return " LIMIT -1 OFFSET ?", [skip]
        return "", []

    def _scan_bounded(self, collection, filter_doc, skip=0, limit=0):
        """Decode rows until the requested number of Python matches is found."""

        sql = "SELECT data FROM {0}".format(_quote_identifier(collection))
        conn = self._connect()
        try:
            documents = []
            matched = 0
            for row in conn.execute(sql):
                document = _json_loads(row[0])
                if not matches_filter(document, filter_doc):
                    continue
                if matched < skip:
                    matched += 1
                    continue
                documents.append(document)
                if limit and len(documents) >= limit:
                    break
            return documents
        finally:
            conn.close()

    def find_bounded(self, collection, filter_doc=None, skip=0, limit=0):
        """Return an unsorted cursor window without decoding later rows.

        This optional backend hook lets the public cursor stay lazy until its
        final ``skip`` and ``limit`` are known.  Sorting is intentionally kept
        out of this path because TinyMongo currently applies BSON ordering in
        Python and therefore needs every candidate document.
        """

        self.create_collection(collection)
        indexed = self._find_indexed_scalar_with_array_union(collection, filter_doc)
        if indexed is not None:
            return self._bounded_slice(indexed, skip, limit)
        if requires_python_filter(filter_doc):
            return self._scan_bounded(collection, filter_doc, skip, limit)
        try:
            if _filter_references_id(filter_doc):
                return self._bounded_slice(
                    self.find(collection, filter_doc),
                    skip,
                    limit,
                )
            where, params = self.compiler.compile(filter_doc)
            bounds, bound_params = self._sqlite_bounds(skip, limit)
            sql = "SELECT data FROM {0}{1}{2}".format(
                _quote_identifier(collection),
                where,
                bounds,
            )
            conn = self._connect()
            try:
                rows = conn.execute(sql, params + bound_params).fetchall()
            finally:
                conn.close()
            return [_json_loads(row[0]) for row in rows]
        except Exception:
            return self._scan_bounded(collection, filter_doc, skip, limit)

    def count_documents(self, collection, filter_doc=None):
        """Count matching SQLite rows without retaining document payloads."""

        self.create_collection(collection)
        if requires_python_filter(filter_doc):
            return self._count_filtered_scan(collection, filter_doc)
        try:
            if _filter_references_id(filter_doc):
                return len(self.find(collection, filter_doc))
            where, params = self.compiler.compile(filter_doc)
            sql = "SELECT COUNT(*) FROM {0}{1}".format(
                _quote_identifier(collection),
                where,
            )
            conn = self._connect()
            try:
                row = conn.execute(sql, params).fetchone()
            finally:
                conn.close()
            # SQLite always returns one row for ``COUNT(*)``.
            return int(row[0])
        except Exception:
            return self._count_filtered_scan(collection, filter_doc)

    def _count_filtered_scan(self, collection, filter_doc):
        """Count exact Python-filter matches while releasing each row promptly."""

        sql = "SELECT data FROM {0}".format(_quote_identifier(collection))
        conn = self._connect()
        try:
            return sum(
                1
                for row in conn.execute(sql)
                if matches_filter(_json_loads(row[0]), filter_doc)
            )
        finally:
            conn.close()

    def find_projected(self, collection, filter_doc, projection):
        """Return projected rows through the established backend hook."""

        return self._find_projected_bounded(
            collection,
            filter_doc,
            projection,
        )

    def find_projected_bounded(
        self,
        collection,
        filter_doc,
        projection,
        skip=0,
        limit=0,
    ):
        """Return a projected cursor window without breaking legacy overrides."""

        if type(self).find_projected is not SQLiteTableBackend.find_projected:
            return self._bounded_slice(
                self.find_projected(collection, filter_doc, projection),
                skip,
                limit,
            )
        return self._find_projected_bounded(
            collection,
            filter_doc,
            projection,
            skip=skip,
            limit=limit,
        )

    def _find_projected_bounded(
        self,
        collection,
        filter_doc,
        projection,
        skip=0,
        limit=0,
    ):
        """Scan SQLite rows while retaining only each projected post-image.

        ``TinyMongoCollection`` discovers this optional backend hook at runtime,
        which leaves the established ``find()`` signature intact for third-party
        table backends.  Sorting deliberately does not use this path because its
        keys must be read from the complete source documents first.
        """

        self.create_collection(collection)
        indexed = self._find_indexed_scalar_with_array_union(
            collection,
            filter_doc,
            projection=projection,
        )
        if indexed is not None:
            return self._bounded_slice(indexed, skip, limit)

        if requires_python_filter(filter_doc):
            return self._scan_projected(
                collection,
                projection,
                predicate=lambda document: matches_filter(document, filter_doc),
                skip=skip,
                limit=limit,
            )

        try:
            where, params = self.compiler.compile(filter_doc)
            if self._is_id_only_projection(projection) and not _filter_references_id(
                filter_doc
            ):
                return self._scan_projected_ids(
                    collection,
                    where,
                    params,
                    skip=skip,
                    limit=limit,
                )

            documents = self._scan_projected(
                collection,
                projection,
                where=where,
                params=params,
                predicate=(
                    (lambda document: matches_filter(document, filter_doc))
                    if _filter_references_id(filter_doc)
                    else None
                ),
                skip=skip,
                limit=limit,
            )
            direct_id_equality = (
                isinstance(filter_doc, Mapping)
                and set(filter_doc) == {"_id"}
                and not isinstance(filter_doc["_id"], Mapping)
            )
            if direct_id_equality and documents:
                return documents
            if _filter_requires_legacy_id_scan(filter_doc):
                return self._scan_projected(
                    collection,
                    projection,
                    predicate=lambda document: matches_filter(document, filter_doc),
                    skip=skip,
                    limit=limit,
                )
            return documents
        except Exception:
            return self._scan_projected(
                collection,
                projection,
                predicate=lambda document: matches_filter(document, filter_doc),
                skip=skip,
                limit=limit,
            )

    @staticmethod
    def _is_id_only_projection(projection):
        return (
            projection.mode == "include"
            and projection.include_id
            and not projection.tree
        )

    @staticmethod
    def _project_sqlite_id(kind, value):
        """Decode one SQLite JSON scalar without reading its document payload."""

        if kind is None:
            return {}
        if kind == "null":
            return {"_id": None}
        if kind in ("true", "false"):
            return {"_id": kind == "true"}
        if kind == "integer":
            # SQLite promotes JSON integers outside its signed 64-bit range to
            # a REAL.  The caller must read just that one complete row instead
            # of accepting a lossy identifier.
            if isinstance(value, float):
                return None
            return {"_id": value}
        if kind in ("object", "array"):
            return _json_loads(
                json.dumps(
                    {"_id": json.loads(value)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        return {"_id": value}

    def _scan_projected_ids(
        self,
        collection,
        where,
        params,
        skip=0,
        limit=0,
    ):
        """Return ``_id`` documents without transferring large JSON payloads."""

        table = _quote_identifier(collection)
        path = _sql_literal("$._id")
        bounds, bound_params = self._sqlite_bounds(skip, limit)
        sql = (
            "SELECT _id, json_type(data, {path}), "
            "json_extract(data, {path}) FROM {table}{where}{bounds}"
        ).format(path=path, table=table, where=where, bounds=bounds)
        conn = self._connect()
        try:
            documents = []
            cursor = conn.execute(sql, params + bound_params)
            for physical_id, kind, value in cursor:
                document = self._project_sqlite_id(kind, value)
                if document is None:
                    row = conn.execute(
                        "SELECT data FROM {0} WHERE _id = ?".format(table),
                        (physical_id,),
                    ).fetchone()
                    source_document = _json_loads(row[0])
                    document = (
                        {"_id": source_document["_id"]}
                        if "_id" in source_document
                        else {}
                    )
                documents.append(document)
            return documents
        finally:
            conn.close()

    def _scan_projected(
        self,
        collection,
        projection,
        where="",
        params=None,
        predicate=None,
        skip=0,
        limit=0,
    ):
        """Decode, filter, and release one source row at a time."""

        bounds = ""
        bound_params = []
        if predicate is None:
            bounds, bound_params = self._sqlite_bounds(skip, limit)
        sql = "SELECT data FROM {0}{1}{2}".format(
            _quote_identifier(collection),
            where,
            bounds,
        )
        conn = self._connect()
        try:
            documents = []
            matched = 0
            cursor = conn.execute(sql, (params or []) + bound_params)
            for row in cursor:
                document = _json_loads(row[0])
                if predicate is not None and not predicate(document):
                    continue
                if predicate is not None and matched < skip:
                    matched += 1
                    continue
                documents.append(project_document(document, projection))
                if predicate is not None and limit and len(documents) >= limit:
                    break
            return documents
        finally:
            conn.close()

    def _find_indexed_scalar_with_array_union(
        self,
        collection,
        filter_doc,
        projection=None,
    ):
        equality = _simple_scalar_equality(filter_doc)
        if equality is None:
            return None
        field, _ = equality
        if "." in field:
            return None
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
            "SELECT data FROM {table} WHERE json_type(data, {path}) "
            "IN ('array', 'object')"
        ).format(table=_quote_identifier(collection), path=path)
        conn = self._connect()
        try:
            documents = []
            for sql, sql_params in ((scalar_sql, params), (array_sql, [])):
                for row in conn.execute(sql, sql_params):
                    document = _json_loads(row[0])
                    if matches_filter(document, filter_doc):
                        documents.append(
                            project_document(document, projection)
                            if projection is not None
                            else document
                        )
            return documents
        finally:
            conn.close()

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
        target_id = _physical_id_key(doc_id)
        self.validate_unique_post_image(
            collection,
            [
                replacement if _physical_id_key(doc.get("_id")) == target_id else doc
                for doc in self.find(collection, {})
            ],
        )
        conn = self._connect()
        try:
            try:
                stored_id = _local_matching_physical_row_id(
                    conn,
                    _quote_identifier(collection),
                    doc_id,
                )
                if stored_id is None:
                    return
                conn.execute(
                    "UPDATE {0} SET data = ? WHERE _id = ?".format(
                        _quote_identifier(collection)
                    ),
                    (_json_dumps(replacement), stored_id),
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
            stored_ids = [
                _local_matching_physical_row_id(
                    conn,
                    _quote_identifier(collection),
                    doc_id,
                )
                for doc_id in ids
            ]
            conn.executemany(
                "DELETE FROM {0} WHERE _id = ?".format(_quote_identifier(collection)),
                [(stored_id,) for stored_id in stored_ids if stored_id is not None],
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
                    "INSERT INTO {0} (collection_name, index_name, field_name, "
                    "unique_flag, token_version) VALUES (?, ?, ?, ?, ?)".format(
                        _quote_identifier(self.index_catalog_table)
                    ),
                    (
                        collection,
                        spec.name,
                        spec.field,
                        int(spec.unique),
                        _SQLITE_UNIQUE_TOKEN_VERSION,
                    ),
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
            raise OperationFailure(
                "Index not found: {0}".format(name_or_field),
                code=27,
            )
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
                data = _json_loads(row[0])
                for collection, docs in data.items():
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS {0} (_id VARCHAR PRIMARY KEY, data VARCHAR NOT NULL)".format(
                            _quote_identifier(collection)
                        )
                    )
                    rows = [
                        (_physical_id_key(doc.get("_id", eid)), _json_dumps(doc))
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
        _validate_physical_ids(existing_docs, docs)
        rows = [(_physical_id_key(doc["_id"]), _json_dumps(doc)) for doc in docs]
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
            documents = [_json_loads(row[0]) for row in rows]
            return _postfilter_id_candidates(
                documents,
                filter_doc,
                lambda: self._all_docs_unfiltered(collection),
            )
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
        target_id = _physical_id_key(doc_id)
        self.validate_unique_post_image(
            collection,
            [
                replacement if _physical_id_key(doc.get("_id")) == target_id else doc
                for doc in self.find(collection, {})
            ],
        )
        conn = self._connect()
        try:
            stored_id = _local_matching_physical_row_id(
                conn,
                _quote_identifier(collection),
                doc_id,
            )
            if stored_id is None:
                return
            conn.execute(
                "UPDATE {0} SET data = ? WHERE _id = ?".format(
                    _quote_identifier(collection)
                ),
                (_json_dumps(replacement), stored_id),
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
            stored_ids = [
                _local_matching_physical_row_id(
                    conn,
                    _quote_identifier(collection),
                    doc_id,
                )
                for doc_id in ids
            ]
            conn.executemany(
                "DELETE FROM {0} WHERE _id = ?".format(_quote_identifier(collection)),
                [(stored_id,) for stored_id in stored_ids if stored_id is not None],
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
            raise OperationFailure(
                "Index not found: {0}".format(name_or_field),
                code=27,
            )
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
            _validate_physical_ids(existing_docs, docs)
            existing = {row[0] for row in rows}
            new_rows = []
            for doc in docs:
                doc_id = _physical_id_key(doc["_id"])
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
            documents = [_json_loads(row[0]) for row in rows]
            return _postfilter_id_candidates(
                documents,
                filter_doc,
                lambda: [
                    _json_loads(row[1]) for row in self._read_all_rows(collection)
                ],
            )
        except Exception:
            return [
                _json_loads(row[1])
                for row in self._read_all_rows(collection)
                if matches_filter(_json_loads(row[1]), filter_doc)
            ]

    def replace_one(self, collection, doc_id, replacement):
        with self._write_lock():
            current_rows = self._read_all_rows(collection)
            stored_id = _matching_physical_row_id(current_rows, doc_id)
            self.validate_unique_post_image(
                collection,
                [
                    replacement if row_id == stored_id else _json_loads(data)
                    for row_id, data in current_rows
                ],
            )
            rows = [
                (row_id, _json_dumps(replacement) if row_id == stored_id else data)
                for row_id, data in current_rows
            ]
            self._write_rows(collection, rows)

    def delete_ids(self, collection, ids):
        with self._write_lock():
            id_set = {_physical_id_key(doc_id) for doc_id in ids}
            rows = [
                row
                for row in self._read_all_rows(collection)
                if _physical_id_key(_json_loads(row[1])["_id"]) not in id_set
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
                raise OperationFailure(
                    "Index not found: {0}".format(name_or_field),
                    code=27,
                )
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
    ordered_data_type = "TEXT"
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
        self._ordered_data_collections = set()
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
        conn.commit()

    def _rollback(self, conn):
        try:
            conn.rollback()
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
            "token_version INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (database_name, collection_name, index_name))".format(
                self._quote(self.index_catalog_table)
            ),
        )
        self._ensure_index_catalog_token_version(conn)
        self._commit(conn)

    def _ensure_index_catalog_token_version(self, conn):
        """Upgrade the remote index catalog in a dialect-safe way."""

        raise NotImplementedError  # pragma: no cover - implemented by drivers

    @contextmanager
    def _collection_schema_lock(self, conn, collection):
        """Serialize per-collection index DDL in the concrete database."""

        yield

    @contextmanager
    def _collection_write_lock(self, conn, collection):
        """Coordinate writes with non-transactional index DDL when required."""

        yield

    def _index_specs_on_connection(self, conn, collection):
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

    def get_index_specs(self, collection):
        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            self._migrate_legacy_unique_indexes(conn, collection)
            return self._index_specs_on_connection(conn, collection)
        finally:
            conn.close()

    def _physical_index_name(self, collection, spec):
        identity = "{0}\x00{1}\x00{2}".format(
            self.database or "default", collection, spec.name
        )
        digest = hashlib.sha256(identity.encode("utf8")).hexdigest()[:32]
        return "__tm_idx_{0}".format(digest)

    def _unique_token_column(self, collection, spec):
        identity = "{0}\x00{1}\x00{2}".format(
            self.database or "default", collection, spec.name
        )
        digest = hashlib.sha256(identity.encode("utf8")).hexdigest()[:32]
        return "__tm_utk_{0}".format(digest)

    def _add_unique_token_column(self, conn, collection, spec):
        raise NotImplementedError  # pragma: no cover - implemented by drivers

    def _set_unique_token_not_null(self, conn, collection, spec):
        raise NotImplementedError  # pragma: no cover - implemented by drivers

    def _drop_unique_token_column(self, conn, collection, spec):
        raise NotImplementedError  # pragma: no cover - implemented by drivers

    def _drop_legacy_native_index(self, conn, collection, spec):
        """Remove the pre-token native index during a catalog migration."""

        self._drop_native_index(conn, collection, spec)

    def _remove_orphan_native_index(self, conn, collection, spec):
        """Clean an incomplete non-transactional build before retrying."""

    def _cleanup_failed_native_index_creation(self, conn, collection, spec):
        """Undo non-transactional DDL while the collection lock is held."""

    def _legacy_unique_specs(self, conn, collection):
        cursor = self._execute(
            conn,
            "SELECT index_name, field_name FROM {0} "
            "WHERE database_name = {1} AND collection_name = {1} "
            "AND unique_flag = {1} AND token_version < {1} "
            "ORDER BY index_name".format(
                self._quote(self.index_catalog_table), self.placeholder
            ),
            (
                self.database,
                collection,
                True,
                _REMOTE_UNIQUE_TOKEN_VERSION,
            ),
        )
        try:
            return [
                IndexSpec(field=row[1], name=row[0], unique=True)
                for row in cursor.fetchall()
            ]
        finally:
            self._close_cursor(cursor)

    def _stored_documents_on_connection(self, conn, collection):
        cursor = self._execute(
            conn,
            "SELECT _id, data_ordered, data FROM {0}".format(
                self._quote(self._table_name(collection))
            ),
        )
        try:
            rows = cursor.fetchall()
        finally:
            self._close_cursor(cursor)
        return [
            (
                row[0],
                _restore_legacy_document_id(
                    row[0],
                    self._decode_data_value(
                        row[-1],
                        ordered_data=row[1] if len(row) > 2 else None,
                    ),
                ),
            )
            for row in rows
        ]

    def _prepare_unique_token_column(self, conn, collection, spec):
        # PostgreSQL's ADD COLUMN takes an ACCESS EXCLUSIVE lock that remains
        # held through this transaction. Adding before the scan prevents a
        # concurrent insert from appearing after backfill with a NULL token.
        # MariaDB uses the named collection lock implemented below.
        self._add_unique_token_column(conn, collection, spec)
        stored_documents = self._stored_documents_on_connection(conn, collection)
        documents = [document for _stored_id, document in stored_documents]
        _reject_remote_unique_values(documents, [spec])
        validate_unique_documents(documents, [spec])
        if stored_documents:
            self._executemany(
                conn,
                "UPDATE {0} SET {1} = {2} WHERE _id = {2}".format(
                    self._quote(self._table_name(collection)),
                    self._quote(self._unique_token_column(collection, spec)),
                    self.placeholder,
                ),
                [
                    (_remote_unique_token(document, spec), stored_id)
                    for stored_id, document in stored_documents
                ],
            )
        self._set_unique_token_not_null(conn, collection, spec)

    def _migrate_legacy_unique_indexes(self, conn, collection):
        # The overwhelmingly common path must not take a table/advisory lock.
        # If a stale row is observed, acquire the database lock and then query
        # again because another client may complete the upgrade while we wait.
        if not self._legacy_unique_specs(conn, collection):
            return
        # End the optimistic read transaction before waiting. In particular,
        # MariaDB's default repeatable-read isolation must not carry the stale
        # catalog snapshot into the locked recheck.
        self._commit(conn)
        try:
            with self._collection_schema_lock(conn, collection):
                # Re-read after taking the database lock. Another client may
                # have completed the migration while this one was waiting.
                stale = self._legacy_unique_specs(conn, collection)
                for spec in stale:
                    self._prepare_unique_token_column(conn, collection, spec)
                    self._drop_legacy_native_index(conn, collection, spec)
                    self._create_native_index(conn, collection, spec)
                    self._execute(
                        conn,
                        "UPDATE {0} SET token_version = {1} "
                        "WHERE database_name = {1} AND collection_name = {1} "
                        "AND index_name = {1}".format(
                            self._quote(self.index_catalog_table), self.placeholder
                        ),
                        (
                            _REMOTE_UNIQUE_TOKEN_VERSION,
                            self.database,
                            collection,
                            spec.name,
                        ),
                    )
                self._commit(conn)
        except Exception as exc:
            self._rollback(conn)
            if self._is_duplicate_error(exc) or isinstance(exc, DuplicateKeyError):
                raise DuplicateKeyError(
                    "Cannot upgrade remote unique indexes on collection {0!r}: "
                    "existing values conflict under exact BSON numeric "
                    "identity".format(collection)
                ) from exc
            raise

    def _create_native_index(self, conn, collection, spec):
        raise NotImplementedError  # pragma: no cover - implemented by drivers

    def validate_unique_post_image(self, collection, documents):
        documents = list(documents)
        specs = self.get_index_specs(collection)
        _reject_remote_unique_values(documents, specs)
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
            table = self._quote(self._table_name(collection))
            self._execute(
                conn,
                "CREATE TABLE IF NOT EXISTS {0} "
                "(_id VARCHAR(255) PRIMARY KEY, data {1} NOT NULL, "
                "data_ordered {2} NULL)".format(
                    table,
                    self.json_type,
                    self.ordered_data_type,
                ),
            )
            needs_ordered_data = collection not in self._ordered_data_collections
            if needs_ordered_data:
                self._ensure_ordered_data_column(conn, collection, table)
            self._ensure_index_catalog(conn)
            self._migrate_legacy_unique_indexes(conn, collection)
            self._record_collection(conn, collection)
            self._commit(conn)
            if needs_ordered_data:
                self._ordered_data_collections.add(collection)
        finally:
            conn.close()

    def _ensure_ordered_data_column(self, conn, collection, table):
        """Upgrade a legacy remote table without disturbing its JSON indexes."""

        self._execute(
            conn,
            "ALTER TABLE {0} ADD COLUMN IF NOT EXISTS "
            "data_ordered {1} NULL".format(table, self.ordered_data_type),
        )

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
            self._ordered_data_collections.discard(collection)
            return existed
        finally:
            conn.close()

    def insert_many(self, collection, docs, bypass_document_validation=False):
        self.create_collection(collection)
        current = self._all_docs_unfiltered(collection)
        self.validate_unique_post_image(collection, current + docs)
        _validate_physical_ids(current, docs)
        conn = self._connect()
        try:
            with self._collection_write_lock(conn, collection):
                unique_specs = tuple(
                    spec
                    for spec in self._index_specs_on_connection(conn, collection)
                    if spec.unique
                )
                rows = [
                    (
                        _physical_id_key(doc["_id"]),
                        _json_dumps(doc),
                        tuple(_remote_unique_token(doc, spec) for spec in unique_specs),
                    )
                    for doc in docs
                ]
                self._insert_rows(
                    conn,
                    collection,
                    rows,
                    unique_specs,
                    bypass_document_validation,
                )
                self._commit(conn)
            return list(range(len(rows)))
        except Exception as exc:
            self._rollback(conn)
            if self._is_duplicate_error(exc):
                raise DuplicateKeyError(str(exc))
            raise
        finally:
            conn.close()

    def _insert_rows(
        self,
        conn,
        collection,
        rows,
        unique_specs,
        bypass_document_validation,
    ):
        raise NotImplementedError  # pragma: no cover - implemented by drivers

    def _data_placeholder(self):
        return self.placeholder

    def _decode_data_value(self, data, ordered_data=None):
        """Read ordered text when present and legacy JSON otherwise."""

        document = _json_loads(ordered_data if ordered_data is not None else data)
        # Tolerate the short-lived wrapped representation used by pre-release
        # development builds as well as released legacy object rows.
        if isinstance(document, str):
            document = _json_loads(document)
        return document

    def find(self, collection, filter_doc=None, sort=None, skip=None, limit=None):
        self.create_collection(collection)
        if not filter_doc:
            return self._all_docs_unfiltered(collection)
        if (
            isinstance(filter_doc, Mapping)
            and set(filter_doc.keys()) == {"_id"}
            and not isinstance(filter_doc["_id"], Mapping)
            and not is_bson_regex(filter_doc["_id"])
        ):
            doc = self._find_by_id(collection, filter_doc["_id"])
            return [doc] if doc else []
        return [
            doc
            for doc in self._all_docs_unfiltered(collection)
            if matches_filter(doc, filter_doc)
        ]

    def _find_by_id(self, collection, doc_id):
        _stored_id, document = self._stored_row_by_id(collection, doc_id)
        return document

    def _stored_row_by_id(self, collection, doc_id):
        conn = self._connect()
        try:
            expected = _physical_id_key(doc_id)
            for candidate in _physical_id_candidates(doc_id):
                cursor = self._execute(
                    conn,
                    "SELECT data_ordered, data FROM {0} WHERE _id = {1}".format(
                        self._quote(self._table_name(collection)), self.placeholder
                    ),
                    (candidate,),
                )
                try:
                    row = cursor.fetchone()
                finally:
                    self._close_cursor(cursor)
                if row is None:
                    continue
                document = self._decode_data_value(
                    row[-1],
                    ordered_data=row[0] if len(row) > 1 else None,
                )
                document = _restore_legacy_document_id(
                    candidate,
                    document,
                    requested_id=doc_id,
                )
                if _physical_id_key(document["_id"]) == expected:
                    return candidate, document

            if not _requires_legacy_id_scan(doc_id):
                return None, None

            # See the local fallback: container IDs and equivalent datetime
            # representations can have legacy strings that cannot be enumerated.
            cursor = self._execute(
                conn,
                "SELECT _id, data_ordered, data FROM {0}".format(
                    self._quote(self._table_name(collection))
                ),
            )
            try:
                rows = cursor.fetchall()
            finally:
                self._close_cursor(cursor)
            for row in rows:
                document = self._decode_data_value(
                    row[-1],
                    ordered_data=row[1] if len(row) > 2 else None,
                )
                document = _restore_legacy_document_id(
                    row[0],
                    document,
                    requested_id=doc_id,
                )
                try:
                    if _physical_id_key(document["_id"]) == expected:
                        return row[0], document
                except (KeyError, TypeError, ValueError):
                    continue
            return None, None
        finally:
            conn.close()

    def _all_docs_unfiltered(self, collection):
        conn = self._connect()
        try:
            cursor = self._execute(
                conn,
                "SELECT _id, data_ordered, data FROM {0}".format(
                    self._quote(self._table_name(collection))
                ),
            )
            try:
                return [
                    _restore_legacy_document_id(
                        row[0],
                        self._decode_data_value(
                            row[-1],
                            ordered_data=row[1] if len(row) > 2 else None,
                        ),
                    )
                    for row in cursor.fetchall()
                ]
            finally:
                self._close_cursor(cursor)
        finally:
            conn.close()

    def replace_one(self, collection, doc_id, replacement):
        self.create_collection(collection)
        target_id = _physical_id_key(doc_id)
        self.validate_unique_post_image(
            collection,
            [
                replacement if _physical_id_key(doc.get("_id")) == target_id else doc
                for doc in self._all_docs_unfiltered(collection)
            ],
        )
        stored_id, _document = self._stored_row_by_id(collection, doc_id)
        if stored_id is None:
            return
        conn = self._connect()
        try:
            try:
                with self._collection_write_lock(conn, collection):
                    unique_specs = tuple(
                        spec
                        for spec in self._index_specs_on_connection(conn, collection)
                        if spec.unique
                    )
                    token_assignments = "".join(
                        ", {0} = {1}".format(
                            self._quote(self._unique_token_column(collection, spec)),
                            self.placeholder,
                        )
                        for spec in unique_specs
                    )
                    self._execute(
                        conn,
                        "UPDATE {0} SET data = {1}, data_ordered = {2}{3} "
                        "WHERE _id = {2}".format(
                            self._quote(self._table_name(collection)),
                            self._data_placeholder(),
                            self.placeholder,
                            token_assignments,
                        ),
                        tuple(
                            [_json_dumps(replacement), _json_dumps(replacement)]
                            + [
                                _remote_unique_token(replacement, spec)
                                for spec in unique_specs
                            ]
                            + [stored_id]
                        ),
                    )
                    self._commit(conn)
            except Exception as exc:
                self._rollback(conn)
                if self._is_duplicate_error(exc):
                    raise DuplicateKeyError(str(exc))
                raise
        finally:
            conn.close()

    def delete_ids(self, collection, ids):
        if not ids:
            return
        self.create_collection(collection)
        stored_ids = [self._stored_row_by_id(collection, doc_id)[0] for doc_id in ids]
        conn = self._connect()
        try:
            with self._collection_write_lock(conn, collection):
                self._executemany(
                    conn,
                    "DELETE FROM {0} WHERE _id = {1}".format(
                        self._quote(self._table_name(collection)), self.placeholder
                    ),
                    [(stored_id,) for stored_id in stored_ids if stored_id is not None],
                )
                self._commit(conn)
        except Exception:
            self._rollback(conn)
            raise
        finally:
            conn.close()

    def create_index(self, collection, spec):
        spec = self._coerce_index_spec(spec)
        self.create_collection(collection)
        existing = self._check_index_compatibility(collection, spec)
        if existing is not None:
            return existing.name

        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            with self._collection_schema_lock(conn, collection):
                # The optimistic check above avoids DDL for ordinary retries;
                # this locked recheck closes the cross-client creation race.
                existing = self._check_index_compatibility(
                    collection,
                    spec,
                    specs=self._index_specs_on_connection(conn, collection),
                )
                if existing is not None:
                    return existing.name
                self._remove_orphan_native_index(conn, collection, spec)
                try:
                    if spec.unique:
                        self._prepare_unique_token_column(conn, collection, spec)
                    self._create_native_index(conn, collection, spec)
                    self._execute(
                        conn,
                        "INSERT INTO {0} "
                        "(database_name, collection_name, index_name, field_name, "
                        "unique_flag, token_version) "
                        "VALUES ({1}, {1}, {1}, {1}, {1}, {1})".format(
                            self._quote(self.index_catalog_table), self.placeholder
                        ),
                        (
                            self.database,
                            collection,
                            spec.name,
                            spec.field,
                            spec.unique,
                            _REMOTE_UNIQUE_TOKEN_VERSION if spec.unique else 0,
                        ),
                    )
                    self._commit(conn)
                except Exception:
                    self._rollback(conn)
                    try:
                        self._cleanup_failed_native_index_creation(
                            conn, collection, spec
                        )
                        self._commit(conn)
                    except Exception:
                        self._rollback(conn)
                    raise
        except Exception as exc:
            self._rollback(conn)
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
            raise OperationFailure(
                "Index not found: {0}".format(name_or_field),
                code=27,
            )

        conn = self._connect()
        try:
            self._ensure_index_catalog(conn)
            with self._collection_schema_lock(conn, collection):
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
        except Exception:
            self._rollback(conn)
            raise
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

    @contextmanager
    def _collection_schema_lock(self, conn, collection):
        cursor = self._execute(
            conn,
            "LOCK TABLE {0} IN ACCESS EXCLUSIVE MODE".format(
                self._quote(self._table_name(collection))
            ),
        )
        self._close_cursor(cursor)
        yield

    @contextmanager
    def _collection_write_lock(self, conn, collection):
        # ROW EXCLUSIVE locks are compatible with one another, so writers stay
        # concurrent while schema changes wait for (or precede) the write.
        cursor = self._execute(
            conn,
            "LOCK TABLE {0} IN ROW EXCLUSIVE MODE".format(
                self._quote(self._table_name(collection))
            ),
        )
        self._close_cursor(cursor)
        yield

    def _ensure_index_catalog_token_version(self, conn):
        cursor = self._execute(
            conn,
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = {0} "
            "AND column_name = 'token_version' LIMIT 1".format(self.placeholder),
            (self.index_catalog_table,),
        )
        try:
            exists = cursor.fetchone() is not None
        finally:
            self._close_cursor(cursor)
        if exists:
            return
        self._execute(
            conn,
            "ALTER TABLE {0} ADD COLUMN IF NOT EXISTS "
            "token_version INTEGER NOT NULL DEFAULT 0".format(
                self._quote(self.index_catalog_table)
            ),
        )

    def _add_unique_token_column(self, conn, collection, spec):
        self._execute(
            conn,
            "ALTER TABLE {0} ADD COLUMN IF NOT EXISTS {1} VARCHAR(64)".format(
                self._quote(self._table_name(collection)),
                self._quote(self._unique_token_column(collection, spec)),
            ),
        )

    def _set_unique_token_not_null(self, conn, collection, spec):
        self._execute(
            conn,
            "ALTER TABLE {0} ALTER COLUMN {1} SET NOT NULL".format(
                self._quote(self._table_name(collection)),
                self._quote(self._unique_token_column(collection, spec)),
            ),
        )

    def _drop_unique_token_column(self, conn, collection, spec):
        self._execute(
            conn,
            "ALTER TABLE {0} DROP COLUMN IF EXISTS {1}".format(
                self._quote(self._table_name(collection)),
                self._quote(self._unique_token_column(collection, spec)),
            ),
        )

    def _drop_legacy_native_index(self, conn, collection, spec):
        self._execute(
            conn,
            "DROP INDEX IF EXISTS {0}".format(
                self._quote(self._physical_index_name(collection, spec))
            ),
        )

    def _create_native_index(self, conn, collection, spec):
        if spec.unique:
            expression = self._quote(self._unique_token_column(collection, spec))
        else:
            path = ", ".join(_sql_literal(part) for part in spec.field.split("."))
            expression = (
                "(COALESCE(jsonb_extract_path(data, {0}), "
                "'null'::jsonb))".format(path)
            )
        self._execute(
            conn,
            "CREATE {0}INDEX {1} ON {2} ({3})".format(
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
        if spec.unique:
            self._drop_unique_token_column(conn, collection, spec)

    def _insert_metadata(self, conn, collection):
        self._execute(
            conn,
            "INSERT INTO {0} (database_name, collection_name) VALUES ({1}, {1}) "
            "ON CONFLICT (database_name, collection_name) DO NOTHING".format(
                self._quote(self.metadata_table), self.placeholder
            ),
            (self.database, collection),
        )

    def _insert_rows(
        self,
        conn,
        collection,
        rows,
        unique_specs,
        bypass_document_validation,
    ):
        token_columns = "".join(
            ", {0}".format(self._quote(self._unique_token_column(collection, spec)))
            for spec in unique_specs
        )
        token_placeholders = "".join(
            ", {0}".format(self.placeholder) for _ in unique_specs
        )
        sql = (
            "INSERT INTO {0} (_id, data, data_ordered{1}) " "VALUES ({2}, {3}, {2}{4})"
        ).format(
            self._quote(self._table_name(collection)),
            token_columns,
            self.placeholder,
            self._data_placeholder(),
            token_placeholders,
        )
        self._executemany(
            conn,
            sql,
            [
                tuple([doc_id, data, data] + list(tokens))
                for doc_id, data, tokens in rows
            ],
        )


class MySQLTableBackend(RemoteSQLTableBackend):
    dialect = "mysql"
    json_type = "JSON"
    ordered_data_type = "LONGTEXT"

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

    def _collection_lock_name(self, collection):
        identity = "{0}\x00{1}".format(self.database or "default", collection)
        return "tinymongo:{0}".format(
            hashlib.sha256(identity.encode("utf8")).hexdigest()[:48]
        )

    @contextmanager
    def _mysql_collection_lock(self, conn, collection):
        name = self._collection_lock_name(collection)
        cursor = self._execute(
            conn,
            "SELECT GET_LOCK({0}, {0})".format(self.placeholder),
            (name, 60),
        )
        try:
            row = cursor.fetchone()
        finally:
            self._close_cursor(cursor)
        if row is None or row[0] != 1:
            raise OperationFailure(
                "Timed out waiting for the remote SQL collection lock"
            )
        try:
            yield
        finally:
            try:
                cursor = self._execute(
                    conn,
                    "SELECT RELEASE_LOCK({0})".format(self.placeholder),
                    (name,),
                )
                self._close_cursor(cursor)
            except Exception:
                # Closing the connection also releases named locks. Preserve
                # the operation's original exception if the connection broke.
                pass

    def _collection_schema_lock(self, conn, collection):
        return self._mysql_collection_lock(conn, collection)

    def _collection_write_lock(self, conn, collection):
        return self._mysql_collection_lock(conn, collection)

    def _mysql_column_exists(self, conn, table, column):
        cursor = self._execute(
            conn,
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = {0} "
            "AND column_name = {0} LIMIT 1".format(self.placeholder),
            (table, column),
        )
        try:
            return cursor.fetchone() is not None
        finally:
            self._close_cursor(cursor)

    def _mysql_index_exists(self, conn, table, index):
        cursor = self._execute(
            conn,
            "SELECT 1 FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = {0} "
            "AND index_name = {0} LIMIT 1".format(self.placeholder),
            (table, index),
        )
        try:
            return cursor.fetchone() is not None
        finally:
            self._close_cursor(cursor)

    def _ensure_index_catalog_token_version(self, conn):
        if self._mysql_column_exists(conn, self.index_catalog_table, "token_version"):
            return
        try:
            self._execute(
                conn,
                "ALTER TABLE {0} ADD COLUMN token_version INTEGER "
                "NOT NULL DEFAULT 0".format(self._quote(self.index_catalog_table)),
            )
        except Exception as exc:
            code = exc.args[0] if getattr(exc, "args", ()) else None
            if code != 1060 and "duplicate column" not in str(exc).lower():
                raise

    def _generated_index_column(self, collection, spec):
        return self._physical_index_name(collection, spec).replace("_idx_", "_key_")

    def _add_unique_token_column(self, conn, collection, spec):
        table_name = self._table_name(collection)
        column_name = self._unique_token_column(collection, spec)
        if self._mysql_column_exists(conn, table_name, column_name):
            return
        try:
            self._execute(
                conn,
                "ALTER TABLE {0} ADD COLUMN {1} CHAR(64) "
                "CHARACTER SET ascii COLLATE ascii_bin NULL".format(
                    self._quote(table_name), self._quote(column_name)
                ),
            )
        except Exception as exc:
            code = exc.args[0] if getattr(exc, "args", ()) else None
            if code != 1060 and "duplicate column" not in str(exc).lower():
                raise

    def _set_unique_token_not_null(self, conn, collection, spec):
        self._execute(
            conn,
            "ALTER TABLE {0} MODIFY COLUMN {1} CHAR(64) "
            "CHARACTER SET ascii COLLATE ascii_bin NOT NULL".format(
                self._quote(self._table_name(collection)),
                self._quote(self._unique_token_column(collection, spec)),
            ),
        )

    def _drop_unique_token_column(self, conn, collection, spec):
        table_name = self._table_name(collection)
        column_name = self._unique_token_column(collection, spec)
        if not self._mysql_column_exists(conn, table_name, column_name):
            return
        self._execute(
            conn,
            "ALTER TABLE {0} DROP COLUMN {1}".format(
                self._quote(table_name), self._quote(column_name)
            ),
        )

    def _ensure_ordered_data_column(self, conn, collection, table):
        # MySQL 8 does not accept ADD COLUMN IF NOT EXISTS. Querying the active
        # schema also avoids needless ALTER TABLE operations for new tables.
        cursor = self._execute(
            conn,
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = {0} "
            "AND column_name = 'data_ordered' LIMIT 1".format(self.placeholder),
            (self._table_name(collection),),
        )
        try:
            exists = cursor.fetchone() is not None
        finally:
            self._close_cursor(cursor)
        if exists:
            return

        try:
            self._execute(
                conn,
                "ALTER TABLE {0} ADD COLUMN data_ordered {1} NULL".format(
                    table, self.ordered_data_type
                ),
            )
        except Exception as exc:
            code = exc.args[0] if getattr(exc, "args", ()) else None
            if code != 1060 and "duplicate column" not in str(exc).lower():
                raise

    def _create_native_index(self, conn, collection, spec):
        if spec.unique:
            self._execute(
                conn,
                "CREATE UNIQUE INDEX {0} ON {1} ({2})".format(
                    self._quote(self._physical_index_name(collection, spec)),
                    self._quote(self._table_name(collection)),
                    self._quote(self._unique_token_column(collection, spec)),
                ),
            )
            return
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
        table_name = self._table_name(collection)
        physical_name = self._physical_index_name(collection, spec)
        table = self._quote(table_name)
        if self._mysql_index_exists(conn, table_name, physical_name):
            self._execute(
                conn,
                "DROP INDEX {0} ON {1}".format(self._quote(physical_name), table),
            )
        if spec.unique:
            self._drop_unique_token_column(conn, collection, spec)
            return
        generated = self._generated_index_column(collection, spec)
        if self._mysql_column_exists(conn, table_name, generated):
            self._execute(
                conn,
                "ALTER TABLE {0} DROP COLUMN {1}".format(table, self._quote(generated)),
            )

    def _drop_legacy_native_index(self, conn, collection, spec):
        table_name = self._table_name(collection)
        physical_name = self._physical_index_name(collection, spec)
        table = self._quote(table_name)
        generated = self._generated_index_column(collection, spec)
        clauses = []
        if self._mysql_index_exists(conn, table_name, physical_name):
            clauses.append("DROP INDEX {0}".format(self._quote(physical_name)))
        if self._mysql_column_exists(conn, table_name, generated):
            clauses.append("DROP COLUMN {0}".format(self._quote(generated)))
        if clauses:
            self._execute(conn, "ALTER TABLE {0} {1}".format(table, ", ".join(clauses)))

    def _remove_orphan_native_index(self, conn, collection, spec):
        self._drop_native_index(conn, collection, spec)

    def _cleanup_failed_native_index_creation(self, conn, collection, spec):
        self._drop_native_index(conn, collection, spec)

    def _insert_metadata(self, conn, collection):
        self._execute(
            conn,
            "INSERT IGNORE INTO {0} (database_name, collection_name) "
            "VALUES ({1}, {1})".format(
                self._quote(self.metadata_table), self.placeholder
            ),
            (self.database, collection),
        )

    def _insert_rows(
        self,
        conn,
        collection,
        rows,
        unique_specs,
        bypass_document_validation,
    ):
        token_columns = "".join(
            ", {0}".format(self._quote(self._unique_token_column(collection, spec)))
            for spec in unique_specs
        )
        token_placeholders = "".join(
            ", {0}".format(self.placeholder) for _ in unique_specs
        )
        sql = (
            "INSERT INTO {0} (_id, data, data_ordered{1}) " "VALUES ({2}, {2}, {2}{3})"
        ).format(
            self._quote(self._table_name(collection)),
            token_columns,
            self.placeholder,
            token_placeholders,
        )
        self._executemany(
            conn,
            sql,
            [
                tuple([doc_id, data, data] + list(tokens))
                for doc_id, data, tokens in rows
            ],
        )
