"""JSON-safe encoding for BSON-shaped values supported by TinyMongo.

Storage backends keep using ordinary JSON payloads. Values JSON cannot
represent are wrapped at the persistence boundary and restored on read, so
callers continue to work with the original Python objects.

``__tinymongo_type_v1__`` is TinyMongo's reserved persisted tag namespace and
must remain readable for backward compatibility. A user mapping with the exact
two-key tag shape is escaped before storage so new writes remain unambiguous.
"""

from __future__ import absolute_import

import base64
import binascii
from collections.abc import Mapping
from datetime import datetime
import json
import math

from . import bson_types
from .errors import InvalidDocument


# Compatibility aliases remain patchable for dependency-error tests and older
# integrations. Type recognition and encoding metadata come exclusively from
# ``bson_types``.
_ObjectId = bson_types.object_id_type()
_Binary = bson_types.binary_type()


_TYPE_MARKER = "__tinymongo_type_v1__"
_VALUE_MARKER = "value"
_ESCAPED_MAPPING = "mapping"
_BINARY_VALUE = "binary"
_BINARY_DATA = "base64"
_BINARY_SUBTYPE = "subtype"
_NONFINITE_FLOAT = "float"
_ROOT_UNSET = object()


def bson_available():
    """Return whether the optional BSON implementation can be used."""
    return object_id_available() and binary_available()


def object_id_available():
    """Return whether PyMongo ``ObjectId`` values can be restored."""

    return _ObjectId is not None


def binary_available():
    """Return whether non-generic PyMongo ``Binary`` values can be restored."""

    return _Binary is not None


def _nested_path(path, key):
    if isinstance(key, int):
        return "{0}[{1}]".format(path, key)
    return "{0}[{1!r}]".format(path, key)


def _bounded_repr(value, limit=160):
    rendered = repr(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def encode_value(value, _path="$", _root=_ROOT_UNSET, _context=None):
    """Recursively convert supported non-JSON values into tagged JSON data."""
    if _root is _ROOT_UNSET:
        _root = value
    spec = bson_types.bson_type_spec(value)
    storage_tag = spec.storage_tag if spec is not None else None
    if storage_tag == "objectid":
        return {_TYPE_MARKER: "objectid", _VALUE_MARKER: str(value)}
    if storage_tag == "datetime":
        return {_TYPE_MARKER: "datetime", _VALUE_MARKER: value.isoformat()}
    if storage_tag == _BINARY_VALUE:
        raw, subtype = bson_types.binary_components(value)
        return _encode_binary(raw, subtype)
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            payload = "nan"
        elif value < 0:
            payload = "-infinity"
        else:
            payload = "infinity"
        return {_TYPE_MARKER: _NONFINITE_FLOAT, _VALUE_MARKER: payload}
    if isinstance(value, Mapping):
        # A user mapping can legitimately have the same two keys as one of our
        # scalar tags. Wrap that exact shape so it cannot be silently decoded as
        # a datetime/ObjectId when it crosses a persistence boundary.
        if set(value) == {_TYPE_MARKER, _VALUE_MARKER}:
            return {
                _TYPE_MARKER: _ESCAPED_MAPPING,
                _VALUE_MARKER: [
                    [
                        str(key),
                        encode_value(
                            item,
                            _path=_nested_path(_path, key),
                            _root=_root,
                            _context=_context,
                        ),
                    ]
                    for key, item in value.items()
                ],
            }
        return {
            str(key): encode_value(
                item,
                _path=_nested_path(_path, key),
                _root=_root,
                _context=_context,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            encode_value(
                item,
                _path=_nested_path(_path, index),
                _root=_root,
                _context=_context,
            )
            for index, item in enumerate(value)
        ]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    context = " for {0}".format(_context) if _context else ""
    raise InvalidDocument(
        "Invalid document{0} at {1!r}: cannot encode object {2}, "
        "of type {3!r}".format(
            context,
            _path,
            _bounded_repr(value),
            type(value),
        ),
        document=_root,
    )


def _encode_binary(value, subtype):
    payload = {
        _BINARY_DATA: base64.b64encode(bytes(value)).decode("ascii"),
        _BINARY_SUBTYPE: int(subtype),
    }
    return {_TYPE_MARKER: _BINARY_VALUE, _VALUE_MARKER: payload}


def _decode_binary(payload):
    if not isinstance(payload, dict):
        raise ValueError("Binary payload must be a mapping")
    if set(payload) != {_BINARY_DATA, _BINARY_SUBTYPE}:
        raise ValueError("Binary payload has an unexpected shape")

    encoded = payload[_BINARY_DATA]
    subtype = payload[_BINARY_SUBTYPE]
    if not isinstance(encoded, str):
        raise ValueError("Binary data must be base64 text")
    if isinstance(subtype, bool) or not isinstance(subtype, int):
        raise ValueError("Binary subtype must be an integer")
    if not 0 <= subtype <= 255:
        raise ValueError("Binary subtype must be between 0 and 255")

    raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    if subtype == 0:
        # PyMongo also decodes BSON's generic binary subtype to plain bytes.
        return raw
    if _Binary is None:
        raise ImportError(
            "Reading BSON Binary values with a non-zero subtype requires the "
            "optional 'pymongo' package. Install it with: "
            "pip install 'tinymongo[bson]'"
        )
    return _Binary(raw, subtype=subtype)


def decode_value(value):
    """Recursively restore values previously produced by :func:`encode_value`."""
    if isinstance(value, dict):
        if set(value) == {_TYPE_MARKER, _VALUE_MARKER}:
            kind = value[_TYPE_MARKER]
            payload = value[_VALUE_MARKER]
            if kind == "datetime":
                if isinstance(payload, str):
                    try:
                        return datetime.fromisoformat(payload)
                    except ValueError:
                        # Preserve malformed or future-shaped tags below.
                        pass
            if kind == "objectid":
                if _valid_object_id_payload(payload):
                    if _ObjectId is None:
                        raise ImportError(
                            "Reading BSON ObjectId values requires the optional "
                            "'pymongo' package. Install it with: "
                            "pip install 'tinymongo[bson]'"
                        )
                    return _ObjectId(payload)
            if kind == _BINARY_VALUE:
                try:
                    return _decode_binary(payload)
                except (binascii.Error, UnicodeEncodeError, ValueError):
                    # Preserve malformed or future-shaped tags just like an
                    # unknown tag rather than turning user data into a value.
                    return value
            if kind == _NONFINITE_FLOAT:
                if payload == "nan":
                    return float("nan")
                if payload == "-infinity":
                    return float("-inf")
                if payload == "infinity":
                    return float("inf")
            if kind == _ESCAPED_MAPPING:
                if not isinstance(payload, list):
                    return value
                try:
                    return {str(key): decode_value(item) for key, item in payload}
                except (TypeError, ValueError):
                    return value
            # Unknown tags and malformed datetime/ObjectId tags are user data,
            # not partially decodable containers.
            return value
        return {key: decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    return value


def dumps(value, document_context=None, **kwargs):
    """Serialize a TinyMongo value as JSON with BSON-compatible tagging."""
    kwargs.setdefault("allow_nan", False)
    return json.dumps(encode_value(value, _context=document_context), **kwargs)


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
    if isinstance(value, float) and not math.isfinite(value):
        return True
    spec = bson_types.bson_type_spec(value)
    if spec is not None and spec.requires_python_comparison:
        return True
    if isinstance(value, dict):
        return any(contains_extended_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_extended_value(item) for item in value)
    return False


def _valid_object_id_payload(payload):
    """Return whether a persisted ObjectId payload has TinyMongo's v1 shape."""

    if not isinstance(payload, str) or len(payload) != 24:
        return False
    try:
        decoded = bytes.fromhex(payload)
    except ValueError:
        return False
    return len(decoded) == 12
