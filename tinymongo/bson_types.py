"""Shared BSON scalar type information.

The JSON codec, query comparison, and cursor sorting all consume this registry
so type recognition and subclass precedence cannot drift between them. PyMongo
is optional: its bundled :mod:`bson` implementation is enabled atomically only
when both ``ObjectId`` and ``Binary`` can be imported.
"""

from __future__ import absolute_import

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Callable, Optional


try:
    import pymongo as _pymongo  # noqa: F401 - proves this is PyMongo's bson
    from bson import ObjectId as _ObjectId
    from bson.binary import Binary as _Binary
except ImportError:  # pragma: no cover - environment without optional bson
    # Treat the optional implementation as one capability. This avoids
    # accidentally accepting the unrelated third-party ``bson`` distribution
    # or reporting partial BSON support from a broken installation.
    _ObjectId = None  # type: ignore[misc,assignment]
    _Binary = None  # type: ignore[misc,assignment]


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
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    return value


def _number_sort_key(value):
    # MongoDB gives NaN a stable position below every other numeric value.
    # Python's raw NaN comparisons are unordered and can leave surrounding
    # ordinary numbers unsorted.
    if isinstance(value, float) and math.isnan(value):
        return 0, 0
    return 1, value


def binary_components(value):
    """Return a binary value as ``(bytes, subtype)``.

    Native ``bytes`` and ``bytearray`` use BSON's generic subtype 0. PyMongo's
    ``Binary`` retains its explicit subtype.
    """

    raw = bytes(value)
    subtype = (
        int(value.subtype) if _Binary is not None and isinstance(value, _Binary) else 0
    )
    return raw, subtype


def _binary_sort_key(value):
    raw, subtype = binary_components(value)
    # MongoDB compares BinData by length, then subtype, then byte content.
    return len(raw), subtype, raw


def _binary_identity_key(value):
    raw, subtype = binary_components(value)
    return subtype, raw


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


def _datetime_identity_key(value):
    """Return MongoDB's signed UTC millisecond identity for a datetime."""

    normalized = normalize_datetime(value)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = normalized - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


# The ranks reserve 3 and 4 for mappings and arrays, which cursors recursively
# order themselves. The remaining order follows MongoDB's documented BSON
# comparison order for the scalar families TinyMongo currently supports.
_NULL = BSONTypeSpec("null", 0, _null_key, _null_key)
_NUMBER = BSONTypeSpec("number", 1, _number_sort_key, _number_identity_key)
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
    normalize_datetime,
    _datetime_identity_key,
    storage_tag="datetime",
    requires_python_comparison=True,
)


def bson_capabilities():
    """Return the optional PyMongo BSON capabilities available at import time."""

    available = _ObjectId is not None and _Binary is not None
    return {"objectid": available, "binary": available}


def object_id_type():
    """Return PyMongo's ``ObjectId`` class, or ``None`` when unavailable."""

    return _ObjectId


def binary_type():
    """Return PyMongo's ``Binary`` class, or ``None`` when unavailable."""

    return _Binary


def bson_type_spec(value) -> Optional[BSONTypeSpec]:
    """Return sorting metadata for a supported scalar, or ``None``.

    Checks are deliberately ordered for subclass safety: ``Binary`` precedes
    native byte values, and ``bool`` precedes integers.
    """

    if value is None:
        return _NULL
    if _Binary is not None and isinstance(value, _Binary):
        return _BINARY
    if isinstance(value, (bytes, bytearray)):
        return _BINARY
    if _ObjectId is not None and isinstance(value, _ObjectId):
        return _OBJECT_ID
    if isinstance(value, datetime):
        return _DATETIME
    if isinstance(value, bool):
        return _BOOLEAN
    if isinstance(value, (int, float)):
        return _NUMBER
    if isinstance(value, str):
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


def bson_identity_key(value):
    """Return a hashable, BSON-type-aware scalar equality key.

    ``None`` means the value is outside the registered scalar families. Binary
    subtype is significant, except that native byte values and ``Binary`` with
    subtype 0 intentionally share one identity. Numeric Python types share
    MongoDB-style numeric equality while booleans remain a separate family.
    """

    spec = bson_type_spec(value)
    if spec is None:
        return None
    return spec.name, spec.identity_key(value)


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
