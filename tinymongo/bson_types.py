"""Shared BSON scalar type information.

The JSON codec, query comparison, and cursor sorting all consume this registry
so type recognition and subclass precedence cannot drift between them. PyMongo
is optional: its bundled :mod:`bson` implementation is enabled atomically only
when the supported optional BSON classes can all be imported. Native UUID and
compiled regular-expression values remain available without PyMongo.
"""

from __future__ import absolute_import

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import math
import re
from typing import Any, Callable, Optional
from uuid import UUID


try:
    import pymongo as _pymongo  # noqa: F401 - proves this is PyMongo's bson
    from bson import Code as _Code
    from bson import MaxKey as _MaxKey
    from bson import MinKey as _MinKey
    from bson import ObjectId as _ObjectId
    from bson import Timestamp as _Timestamp
    from bson.binary import Binary as _Binary
    from bson.decimal128 import (
        Decimal128 as _Decimal128,
        create_decimal128_context as _create_decimal128_context,
    )
    from bson.regex import Regex as _Regex
except ImportError:  # pragma: no cover - environment without optional bson
    # Treat the optional implementation as one capability. This avoids
    # accidentally accepting the unrelated third-party ``bson`` distribution
    # or reporting partial BSON support from a broken installation.
    _ObjectId = None  # type: ignore[misc,assignment]
    _Binary = None  # type: ignore[misc,assignment]
    _Code = None  # type: ignore[misc,assignment]
    _Decimal128 = None  # type: ignore[misc,assignment]
    _MaxKey = None  # type: ignore[misc,assignment]
    _MinKey = None  # type: ignore[misc,assignment]
    _Regex = None  # type: ignore[misc,assignment]
    _Timestamp = None  # type: ignore[misc,assignment]
    _create_decimal128_context = None  # type: ignore[misc,assignment]


_NATIVE_REGEX_TYPE = type(re.compile(""))


@dataclass(frozen=True)
class BSONTypeSpec:
    """Shared behavior for one supported BSON scalar family."""

    name: str
    sort_rank: int
    sort_key: Callable[[Any], Any]
    identity_key: Callable[[Any], Any]
    storage_tag: Optional[str] = None
    requires_python_comparison: bool = False


def _identity(value):
    return value


def _null_key(_value):
    return None


def _number_identity_key(value):
    # MongoDB considers numeric values across integer/double representations by
    # numeric value. Python already supplies that equality/hash behavior except
    # for NaN, which MongoDB treats as one comparable numeric value.
    decimal_value = bson_number_decimal(value)
    if decimal_value.is_nan():
        return "nan"
    if decimal_value.is_infinite():
        return "-infinity" if decimal_value.is_signed() else "infinity"
    # A reduced ratio gives int, float, and Decimal128 one exact, JSON-safe
    # identity without confusing a binary float such as 0.1 with Decimal128
    # ("0.1"). This also preserves the physical-ID encoding used by earlier
    # int/float releases.
    return decimal_value.as_integer_ratio()


def _number_sort_key(value):
    # MongoDB gives NaN a stable position below every other numeric value.
    # Python's raw NaN comparisons are unordered and can leave surrounding
    # ordinary numbers unsorted.
    decimal_value = bson_number_decimal(value)
    if decimal_value.is_nan():
        return 0, 0
    return 1, decimal_value


def bson_number_decimal(value):
    """Return the exact :class:`Decimal` value of a supported BSON number."""

    if isinstance(value, bool):
        raise TypeError("Boolean values are not BSON numbers")
    if _Decimal128 is not None and isinstance(value, _Decimal128):
        return value.to_decimal()
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal.from_float(value)
    raise TypeError("Unsupported BSON numeric type: {0}".format(type(value).__name__))


def is_bson_number(value):
    """Return whether ``value`` belongs to TinyMongo's BSON numeric family."""

    return not isinstance(value, bool) and (
        isinstance(value, (int, float))
        or (_Decimal128 is not None and isinstance(value, _Decimal128))
    )


def _decimal128_update_operand(value):
    """Convert one update operand using MongoDB's double-to-decimal rule."""

    if isinstance(value, float):
        if math.isfinite(value):
            # MongoDB promotes a double used with Decimal128 to 15 significant
            # decimal digits. Scientific notation retains the scale needed for
            # results such as Decimal128("2") + 0.1 -> 2.100000000000000.
            return Decimal(format(value, ".14e"))
        return Decimal(str(value))
    return bson_number_decimal(value)


def add_bson_numbers(left, right):
    """Add two update operands with MongoDB-style Decimal128 promotion."""

    if not is_bson_number(left) or not is_bson_number(right):
        raise TypeError("BSON numeric addition requires two numeric values")
    if _Decimal128 is not None and (
        isinstance(left, _Decimal128) or isinstance(right, _Decimal128)
    ):
        # PyMongo exposes the same IEEE-754 Decimal128 context MongoDB uses.
        # Its traps are disabled, so sNaN and opposite infinities become NaN
        # instead of leaking decimal.InvalidOperation into application code.
        with localcontext(_create_decimal128_context()):
            result = _decimal128_update_operand(left) + _decimal128_update_operand(
                right
            )
        if (
            isinstance(left, _Decimal128)
            and not result.is_nan()
            and result == left.to_decimal()
        ):
            # Preserve the existing BID, including scale and signed zero, when
            # Decimal128 rounding makes the increment a storage-level no-op.
            return left
        return _Decimal128(result)
    return left + right


def decimal128_context():
    """Return PyMongo's IEEE-754 Decimal128 arithmetic context."""

    if _create_decimal128_context is None:
        raise RuntimeError(
            "Decimal128 arithmetic requires the optional pymongo package"
        )
    return _create_decimal128_context()


def decimal128_from_decimal(value):
    """Wrap a :class:`Decimal` in the installed BSON ``Decimal128`` type."""

    if _Decimal128 is None:
        raise RuntimeError(
            "Decimal128 arithmetic requires the optional pymongo package"
        )
    return _Decimal128(value)


def bson_number_truth(value):
    """Return MongoDB's truth value for a numeric projection flag."""

    return not bson_number_decimal(value).is_zero()


def binary_components(value):
    """Return a binary value as ``(bytes, subtype)``.

    Native ``bytes`` and ``bytearray`` use BSON's generic subtype 0. PyMongo's
    ``Binary`` retains its explicit subtype. UUID values use MongoDB's standard
    UUID representation, binary subtype 4 with RFC-4122 byte order.
    """

    if isinstance(value, UUID):
        return value.bytes, 4
    raw = bytes(value)
    subtype = (
        int(value.subtype) if _Binary is not None and isinstance(value, _Binary) else 0
    )
    return raw, subtype


def _binary_sort_key(value):
    raw, subtype = binary_components(value)
    # MongoDB compares BinData by encoded length, then subtype, then byte
    # content. Legacy subtype 2 carries an additional four-byte inner length.
    encoded_length = len(raw) + (4 if subtype == 2 else 0)
    return encoded_length, subtype, raw


def _binary_identity_key(value):
    raw, subtype = binary_components(value)
    return subtype, raw


def _regex_pattern(value):
    """Return the UTF-8 pattern text represented by a BSON regex value."""

    pattern = value.pattern
    if isinstance(pattern, bytes):
        return pattern.decode("utf8")
    return pattern


def regex_flags_text(flags):
    """Return MongoDB's canonical, ordered BSON regex option string."""

    rendered = []
    for letter, flag in (
        ("i", re.IGNORECASE),
        ("l", re.LOCALE),
        ("m", re.MULTILINE),
        ("s", re.DOTALL),
        ("u", re.UNICODE),
        ("x", re.VERBOSE),
    ):
        if int(flags) & int(flag):
            rendered.append(letter)
    return "".join(rendered)


def regex_components(value):
    """Return a regex value as canonical BSON ``(pattern, options)`` text."""

    if not is_bson_regex(value):
        raise TypeError(
            "Unsupported BSON regular-expression type: {0}".format(type(value).__name__)
        )
    return _regex_pattern(value), regex_flags_text(value.flags)


def regex_compile_components(value):
    """Return the original pattern and BSON-supported flags for matching."""

    if not is_bson_regex(value):
        raise TypeError(
            "Unsupported BSON regular-expression type: {0}".format(type(value).__name__)
        )
    known_flags = sum(
        int(flag)
        for flag in (
            re.IGNORECASE,
            re.LOCALE,
            re.MULTILINE,
            re.DOTALL,
            re.UNICODE,
            re.VERBOSE,
        )
    )
    return _regex_pattern(value), int(value.flags) & known_flags


def _regex_key(value):
    return regex_components(value)


def is_native_regex(value):
    """Return whether ``value`` is a compiled :mod:`re` pattern."""

    return isinstance(value, _NATIVE_REGEX_TYPE)


def is_bson_regex(value):
    """Return whether ``value`` can be represented as a BSON regex."""

    return is_native_regex(value) or (_Regex is not None and isinstance(value, _Regex))


def is_bson_code(value):
    """Return whether ``value`` is BSON JavaScript code rather than text."""

    return _Code is not None and isinstance(value, _Code)


def is_bson_string(value):
    """Return whether ``value`` belongs to BSON's ordinary string family."""

    return isinstance(value, str) and not is_bson_code(value)


def _object_id_key(value):
    return value.binary


def normalize_datetime(value):
    """Return a datetime normalized to UTC for stable mixed-date sorting.

    MongoDB stores dates as UTC. TinyMongo treats a naive datetime as already
    being in UTC and converts aware datetimes to the same timezone.
    """

    if value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def datetime_milliseconds(value):
    """Return MongoDB's signed UTC millisecond value for ``value``.

    BSON dates are signed integers rather than floating-point timestamps.
    Calculating from a :class:`~datetime.timedelta` preserves exact behavior
    before the Unix epoch and avoids platform-dependent timestamp rounding.
    """

    normalized = normalize_datetime(value)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = normalized - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def canonicalize_datetime(value, tz_aware=False, tzinfo=None):
    """Return ``value`` with MongoDB's UTC millisecond decode semantics.

    The default mirrors PyMongo's naive-UTC result. With ``tz_aware=True``,
    the result is UTC-aware and is converted to ``tzinfo`` when supplied.
    Client-option validation remains the caller's responsibility.
    """

    milliseconds = datetime_milliseconds(value)
    days, day_milliseconds = divmod(milliseconds, 86_400_000)
    seconds, milliseconds = divmod(day_milliseconds, 1_000)
    canonical = datetime(1970, 1, 1) + timedelta(
        days=days,
        seconds=seconds,
        milliseconds=milliseconds,
    )
    if not tz_aware:
        return canonical
    canonical = canonical.replace(tzinfo=timezone.utc)
    return canonical if tzinfo is None else canonical.astimezone(tzinfo)


def _datetime_identity_key(value):
    """Return MongoDB's signed UTC millisecond identity for a datetime."""

    return datetime_milliseconds(value)


def _timestamp_key(value):
    """Return BSON timestamp order: seconds first, then increment."""

    return value.time, value.inc


def _code_identity_key(value):
    """Return the JavaScript source plus ordered BSON scope identity."""

    scope = value.scope
    if scope is None:
        return str(value)
    scope_key = bson_value_identity_key(scope)
    if scope_key is None:  # pragma: no cover - storage validation rejects this
        return str(value), repr(scope)
    return str(value), scope_key


def _code_sort_key(value):
    """Return the JavaScript source plus MongoDB-ordered scope value."""

    scope = value.scope
    if scope is None:
        return str(value)
    scope_key = bson_value_sort_key(scope)
    if scope_key is None:  # pragma: no cover - storage validation rejects this
        return str(value), repr(scope)
    return str(value), scope_key


# The ranks reserve 3 and 4 for mappings and arrays, which cursors recursively
# order themselves. The remaining order follows MongoDB's documented BSON
# comparison order for the scalar families TinyMongo supports.
_MIN_KEY = BSONTypeSpec(
    "minKey",
    -1,
    _null_key,
    _null_key,
    storage_tag="minkey",
    requires_python_comparison=True,
)
_NULL = BSONTypeSpec("null", 0, _null_key, _null_key)
_NUMBER = BSONTypeSpec("number", 1, _number_sort_key, _number_identity_key)
_DECIMAL_NUMBER = BSONTypeSpec(
    "number",
    1,
    _number_sort_key,
    _number_identity_key,
    storage_tag="decimal128",
    requires_python_comparison=True,
)
_STRING = BSONTypeSpec("string", 2, _identity, _identity)
_BINARY = BSONTypeSpec(
    "binary",
    5,
    _binary_sort_key,
    _binary_identity_key,
    storage_tag="binary",
    requires_python_comparison=True,
)
_OBJECT_ID = BSONTypeSpec(
    "objectid",
    6,
    _object_id_key,
    _object_id_key,
    storage_tag="objectid",
    requires_python_comparison=True,
)
_BOOLEAN = BSONTypeSpec("boolean", 7, _identity, _identity)
_DATETIME = BSONTypeSpec(
    "datetime",
    8,
    _datetime_identity_key,
    _datetime_identity_key,
    storage_tag="datetime",
    requires_python_comparison=True,
)
_TIMESTAMP = BSONTypeSpec(
    "timestamp",
    9,
    _timestamp_key,
    _timestamp_key,
    storage_tag="timestamp",
    requires_python_comparison=True,
)
_UUID = BSONTypeSpec(
    "binary",
    5,
    _binary_sort_key,
    _binary_identity_key,
    storage_tag="uuid",
    requires_python_comparison=True,
)
_REGEX = BSONTypeSpec(
    "regex",
    10,
    _regex_key,
    _regex_key,
    storage_tag="regex",
    requires_python_comparison=True,
)
_CODE_VALUE = BSONTypeSpec(
    "javascript",
    11,
    _code_sort_key,
    _code_identity_key,
    storage_tag="code",
    requires_python_comparison=True,
)
_SCOPED_CODE_VALUE = BSONTypeSpec(
    "javascriptWithScope",
    12,
    _code_sort_key,
    _code_identity_key,
    storage_tag="code",
    requires_python_comparison=True,
)
_MAX_KEY = BSONTypeSpec(
    "maxKey",
    13,
    _null_key,
    _null_key,
    storage_tag="maxkey",
    requires_python_comparison=True,
)


def bson_capabilities():
    """Return the optional PyMongo BSON capabilities available at import time."""

    available = (
        _ObjectId is not None
        and _Binary is not None
        and _Code is not None
        and _Decimal128 is not None
        and _MaxKey is not None
        and _MinKey is not None
        and _Regex is not None
        and _Timestamp is not None
    )
    return {
        "objectid": available,
        "binary": available,
        "code": available,
        "decimal128": available,
        "maxkey": available,
        "minkey": available,
        "regex": available,
        "timestamp": available,
    }


def supported_bson_types():
    """Enumerate native families and richer types enabled by PyMongo."""

    optional = bson_capabilities()
    return {
        "native": (
            "array",
            "binary",
            "boolean",
            "datetime",
            "double",
            "int",
            "long",
            "null",
            "object",
            "regex",
            "string",
            "uuid",
        ),
        "pymongo": tuple(
            name
            for name in (
                "binary",
                "code",
                "decimal128",
                "maxkey",
                "minkey",
                "objectid",
                "regex",
                "timestamp",
            )
            if optional[name]
        ),
    }


def object_id_type():
    """Return PyMongo's ``ObjectId`` class, or ``None`` when unavailable."""

    return _ObjectId


def binary_type():
    """Return PyMongo's ``Binary`` class, or ``None`` when unavailable."""

    return _Binary


def code_type():
    """Return PyMongo's ``Code`` class, or ``None`` when unavailable."""

    return _Code


def decimal128_type():
    """Return PyMongo's ``Decimal128`` class, or ``None`` when unavailable."""

    return _Decimal128


def max_key_type():
    """Return PyMongo's ``MaxKey`` class, or ``None`` when unavailable."""

    return _MaxKey


def min_key_type():
    """Return PyMongo's ``MinKey`` class, or ``None`` when unavailable."""

    return _MinKey


def regex_type():
    """Return PyMongo's ``Regex`` class, or ``None`` when unavailable."""

    return _Regex


def timestamp_type():
    """Return PyMongo's ``Timestamp`` class, or ``None`` when unavailable."""

    return _Timestamp


def bson_type_spec(value) -> Optional[BSONTypeSpec]:
    """Return sorting metadata for a supported scalar, or ``None``.

    Checks are deliberately ordered for subclass safety: ``Binary`` precedes
    native byte values, ``Code`` precedes strings, and ``bool`` precedes
    integers.
    """

    if _MinKey is not None and isinstance(value, _MinKey):
        return _MIN_KEY
    if _MaxKey is not None and isinstance(value, _MaxKey):
        return _MAX_KEY
    if value is None:
        return _NULL
    if _Binary is not None and isinstance(value, _Binary):
        return _BINARY
    if isinstance(value, (bytes, bytearray)):
        return _BINARY
    if isinstance(value, UUID):
        return _UUID
    if _ObjectId is not None and isinstance(value, _ObjectId):
        return _OBJECT_ID
    if _Decimal128 is not None and isinstance(value, _Decimal128):
        return _DECIMAL_NUMBER
    if _Timestamp is not None and isinstance(value, _Timestamp):
        return _TIMESTAMP
    if is_bson_regex(value):
        return _REGEX
    if is_bson_code(value):
        return _SCOPED_CODE_VALUE if value.scope is not None else _CODE_VALUE
    if isinstance(value, datetime):
        return _DATETIME
    if isinstance(value, bool):
        return _BOOLEAN
    if is_bson_number(value):
        return _NUMBER
    if is_bson_string(value):
        return _STRING
    return None


def bson_scalar_sort_key(value):
    """Return ``(rank, value)`` for a supported BSON scalar.

    ``None`` means the cursor must handle a container or unsupported value.
    """

    spec = bson_type_spec(value)
    if spec is None:
        return None
    return spec.sort_rank, spec.sort_key(value)


def bson_value_sort_key(value):
    """Return MongoDB-style ordering metadata for scalars and containers.

    Cursor sorting and aggregation accumulators both need the same recursive
    BSON comparison order.  ``None`` means the value is outside TinyMongo's
    supported BSON families and cannot be compared safely.
    """

    if isinstance(value, Mapping):
        parsed = []
        for key, item in value.items():
            item_key = bson_value_sort_key(item)
            if item_key is None:
                return None
            parsed.append((item_key[0], key, item_key[1]))
        return 3, tuple(parsed)

    if isinstance(value, (list, tuple)):
        if not value:
            # An empty tuple is lexicographically below every non-empty array
            # payload, including one whose first member is MinKey.
            return 4, ()
        parsed = []
        for item in value:
            item_key = bson_value_sort_key(item)
            if item_key is None:
                return None
            parsed.append(item_key)
        return 4, tuple(parsed)

    return bson_scalar_sort_key(value)


def bson_query_range_matches(actual, operand, comparison, exact=False):
    """Return whether one query range comparison follows BSON semantics.

    MongoDB brackets range predicates by the outer BSON type, with all numeric
    representations sharing one family. MinKey and MaxKey operands are the two
    exceptions: their comparisons span every supported BSON type. A
    non-positional array field exposes both its complete array value and each
    direct member to the predicate; ``exact`` endpoints such as ``$elemMatch``
    or a numeric path component do not fan out again.
    """

    operand_identity = bson_value_identity_key(operand)
    operand_order = bson_value_sort_key(operand)
    if operand_identity is None or operand_order is None:
        return False
    crosses_type_brackets = operand_identity[0] in ("minKey", "maxKey")

    values = [actual]
    if isinstance(actual, (list, tuple)) and not exact:
        values.extend(actual)

    for value in values:
        value_identity = bson_value_identity_key(value)
        value_order = bson_value_sort_key(value)
        if (
            value_identity is None
            or value_order is None
            or (not crosses_type_brackets and value_identity[0] != operand_identity[0])
        ):
            continue
        try:
            if value_identity[0] == operand_identity[0] == "number" and (
                bson_number_decimal(value).is_nan()
                != bson_number_decimal(operand).is_nan()
            ):
                continue
            if comparison(value_order, operand_order):
                return True
        except (ArithmeticError, TypeError, ValueError):
            continue
    return False


def bson_identity_key(value):
    """Return a hashable, BSON-type-aware scalar equality key.

    ``None`` means the value is outside the registered scalar families. Binary
    subtype is significant, except that native byte values and ``Binary`` with
    subtype 0 intentionally share one identity. Numeric Python types share
    MongoDB-style numeric equality while booleans remain a separate family.
    """

    value_type = type(value)
    if value is None:
        return "null", None
    if value_type is str:
        return "string", value
    if value_type is bool:
        return "boolean", value
    if value_type is int or value_type is float:
        return "number", _number_identity_key(value)
    if value_type is bytes or value_type is bytearray:
        return "binary", _binary_identity_key(value)

    spec = bson_type_spec(value)
    if spec is None:
        return None
    return spec.name, spec.identity_key(value)


def bson_value_identity_key(value):
    """Return a recursive, hashable key using MongoDB value equality rules.

    Mapping field order remains significant, arrays retain member order, and
    registered scalar families use :func:`bson_identity_key`. ``None`` means
    the value cannot be represented safely by the current BSON registry.
    """

    scalar_key = bson_identity_key(value)
    if scalar_key is not None:
        return scalar_key

    if isinstance(value, Mapping):
        items = []
        for key, item in value.items():
            item_key = bson_value_identity_key(item)
            if item_key is None:
                return None
            items.append((key, item_key))
        return "object", tuple(items)

    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            item_key = bson_value_identity_key(item)
            if item_key is None:
                return None
            items.append(item_key)
        return "array", tuple(items)

    return None


def bson_values_equal(left, right):
    """Compare values recursively with MongoDB-compatible type semantics."""

    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        left_items = list(left.items())
        right_items = list(right.items())
        return len(left_items) == len(right_items) and all(
            left_key == right_key and bson_values_equal(left_value, right_value)
            for (left_key, left_value), (right_key, right_value) in zip(
                left_items, right_items
            )
        )

    left_is_array = isinstance(left, (list, tuple))
    right_is_array = isinstance(right, (list, tuple))
    if left_is_array or right_is_array:
        if not left_is_array or not right_is_array or len(left) != len(right):
            return False
        return all(
            bson_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )

    left_key = bson_identity_key(left)
    right_key = bson_identity_key(right)
    if left_key is None or right_key is None:
        return left == right
    return left_key == right_key
