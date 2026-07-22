"""JSON-safe encoding for BSON-shaped values supported by TinyMongo.

Storage backends keep using ordinary JSON payloads. Values JSON cannot
represent are wrapped at the persistence boundary and restored on read, so
callers continue to work with the original Python objects.
"""

from __future__ import absolute_import

from datetime import datetime
import json


_TYPE_MARKER = "__tinymongo_type_v1__"
_VALUE_MARKER = "value"
_ESCAPED_MAPPING = "mapping"

try:
    from bson import ObjectId as _ObjectId
except ImportError:  # pragma: no cover - environment without optional bson
    _ObjectId = None  # type: ignore[misc,assignment]


def bson_available():
    """Return whether the optional BSON implementation can be used."""
    return _ObjectId is not None


def encode_value(value):
    """Recursively convert supported non-JSON values into tagged JSON data."""
    if _ObjectId is not None and isinstance(value, _ObjectId):
        return {_TYPE_MARKER: "objectid", _VALUE_MARKER: str(value)}
    if isinstance(value, datetime):
        return {_TYPE_MARKER: "datetime", _VALUE_MARKER: value.isoformat()}
    if isinstance(value, dict):
        # A user mapping can legitimately have the same two keys as one of our
        # scalar tags. Wrap that exact shape so it cannot be silently decoded as
        # a datetime/ObjectId when it crosses a persistence boundary.
        if set(value) == {_TYPE_MARKER, _VALUE_MARKER}:
            return {
                _TYPE_MARKER: _ESCAPED_MAPPING,
                _VALUE_MARKER: [
                    [str(key), encode_value(item)] for key, item in value.items()
                ],
            }
        return {str(key): encode_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]
    return value


def decode_value(value):
    """Recursively restore values previously produced by :func:`encode_value`."""
    if isinstance(value, dict):
        if set(value) == {_TYPE_MARKER, _VALUE_MARKER}:
            kind = value[_TYPE_MARKER]
            payload = value[_VALUE_MARKER]
            if kind == "datetime":
                return datetime.fromisoformat(payload)
            if kind == "objectid":
                if _ObjectId is None:
                    raise ImportError(
                        "Reading BSON ObjectId values requires the optional "
                        "'pymongo' package. Install it with: "
                        "pip install 'tinymongo[bson]'"
                    )
                return _ObjectId(payload)
            if kind == _ESCAPED_MAPPING:
                if not isinstance(payload, list):
                    return {key: decode_value(item) for key, item in value.items()}
                try:
                    return {str(key): decode_value(item) for key, item in payload}
                except (TypeError, ValueError):
                    return {key: decode_value(item) for key, item in value.items()}
        return {key: decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    return value


def dumps(value, **kwargs):
    """Serialize a TinyMongo value as JSON with BSON-compatible tagging."""
    return json.dumps(encode_value(value), **kwargs)


def loads(value):
    """Deserialize JSON data and restore tagged BSON-compatible values."""
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    return decode_value(value)


def clone(value):
    """Return an isolated copy using the same rules as persistent storage."""
    return loads(dumps(value, ensure_ascii=False))


def contains_extended_value(value):
    """Return whether a filter contains a value requiring Python comparison."""
    if isinstance(value, datetime):
        return True
    if _ObjectId is not None and isinstance(value, _ObjectId):
        return True
    if isinstance(value, dict):
        return any(contains_extended_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_extended_value(item) for item in value)
    return False
