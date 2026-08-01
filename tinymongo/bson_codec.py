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
import re
from uuid import UUID

from . import bson_types
from .errors import InvalidDocument


# Compatibility aliases remain patchable for dependency-error tests and older
# integrations. Type recognition and encoding metadata come exclusively from
# ``bson_types``.
_ObjectId = bson_types.object_id_type()
_Binary = bson_types.binary_type()
_Decimal128 = bson_types.decimal128_type()
_Regex = bson_types.regex_type()


_TYPE_MARKER = "__tinymongo_type_v1__"
_VALUE_MARKER = "value"
_ESCAPED_MAPPING = "mapping"
_BINARY_VALUE = "binary"
_BINARY_DATA = "base64"
_BINARY_SUBTYPE = "subtype"
_NONFINITE_FLOAT = "float"
_DECIMAL128_VALUE = "decimal128"
_UUID_VALUE = "uuid"
_REGEX_VALUE = "regex"
_REGEX_PATTERN = "pattern"
_REGEX_FLAGS = "flags"
_REGEX_REPRESENTATION = "representation"
_REGEX_PATTERN_TYPE = "pattern_type"
_REGEX_NATIVE = "python"
_REGEX_BSON = "bson"
_REGEX_TEXT = "string"
_REGEX_BYTES = "bytes"
_ROOT_UNSET = object()


def bson_available():
    """Return whether the optional BSON implementation can be used."""
    return (
        object_id_available()
        and binary_available()
        and decimal128_available()
        and regex_available()
    )


def object_id_available():
    """Return whether PyMongo ``ObjectId`` values can be restored."""

    return _ObjectId is not None


def binary_available():
    """Return whether non-generic PyMongo ``Binary`` values can be restored."""

    return _Binary is not None


def decimal128_available():
    """Return whether PyMongo ``Decimal128`` values can be restored."""

    return _Decimal128 is not None


def regex_available():
    """Return whether PyMongo ``Regex`` values can be restored."""

    return _Regex is not None


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
    if storage_tag == _DECIMAL128_VALUE:
        # Store the raw IEEE-754 BID bytes. ``str(value)`` loses the distinction
        # between quiet and signaling NaN and would not be an exact round trip.
        return {_TYPE_MARKER: _DECIMAL128_VALUE, _VALUE_MARKER: value.bid.hex()}
    if storage_tag == _UUID_VALUE:
        return {_TYPE_MARKER: _UUID_VALUE, _VALUE_MARKER: str(value)}
    if storage_tag == _REGEX_VALUE:
        try:
            return {
                _TYPE_MARKER: _REGEX_VALUE,
                _VALUE_MARKER: _encode_regex(value),
            }
        except (UnicodeDecodeError, ValueError) as error:
            context = " for {0}".format(_context) if _context else ""
            raise InvalidDocument(
                "Invalid document{0} at {1!r}: cannot encode regular "
                "expression {2}: {3}".format(
                    context,
                    _path,
                    _bounded_repr(value),
                    error,
                ),
                document=_root,
            ) from error
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


def _encode_regex(value):
    pattern = value.pattern
    pattern_type = _REGEX_BYTES if isinstance(pattern, bytes) else _REGEX_TEXT
    if isinstance(pattern, bytes):
        pattern = pattern.decode("utf8")
    if "\x00" in pattern:
        raise ValueError("BSON regular-expression patterns cannot contain NUL")
    return {
        _REGEX_PATTERN: pattern,
        _REGEX_FLAGS: int(value.flags),
        _REGEX_REPRESENTATION: (
            _REGEX_NATIVE if bson_types.is_native_regex(value) else _REGEX_BSON
        ),
        _REGEX_PATTERN_TYPE: pattern_type,
    }


def _decode_regex(payload):
    if not isinstance(payload, dict):
        raise ValueError("Regex payload must be a mapping")
    expected = {
        _REGEX_PATTERN,
        _REGEX_FLAGS,
        _REGEX_REPRESENTATION,
        _REGEX_PATTERN_TYPE,
    }
    if set(payload) != expected:
        raise ValueError("Regex payload has an unexpected shape")

    pattern = payload[_REGEX_PATTERN]
    flags = payload[_REGEX_FLAGS]
    representation = payload[_REGEX_REPRESENTATION]
    pattern_type = payload[_REGEX_PATTERN_TYPE]
    if not isinstance(pattern, str) or "\x00" in pattern:
        raise ValueError("Regex pattern must be NUL-free text")
    if isinstance(flags, bool) or not isinstance(flags, int):
        raise ValueError("Regex flags must be an integer")
    if representation not in (_REGEX_NATIVE, _REGEX_BSON):
        raise ValueError("Regex representation is not supported")
    if pattern_type not in (_REGEX_TEXT, _REGEX_BYTES):
        raise ValueError("Regex pattern type is not supported")

    decoded_pattern = (
        pattern.encode("utf8") if pattern_type == _REGEX_BYTES else pattern
    )
    if representation == _REGEX_NATIVE:
        return re.compile(decoded_pattern, flags)
    if _Regex is None:
        raise ImportError(
            "Reading BSON Regex values requires the optional 'pymongo' "
            "package. Install it with: pip install 'tinymongo[bson]'"
        )
    return _Regex(decoded_pattern, flags)


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
            if kind == _DECIMAL128_VALUE:
                raw = _decimal128_payload(payload)
                if raw is not None:
                    if _Decimal128 is None:
                        raise ImportError(
                            "Reading BSON Decimal128 values requires the optional "
                            "'pymongo' package. Install it with: "
                            "pip install 'tinymongo[bson]'"
                        )
                    return _Decimal128.from_bid(raw)
            if kind == _UUID_VALUE:
                parsed = _uuid_payload(payload)
                if parsed is not None:
                    return parsed
            if kind == _REGEX_VALUE:
                try:
                    return _decode_regex(payload)
                except ImportError:
                    raise
                except (OverflowError, TypeError, ValueError, re.error):
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


def storage_values_equal(left, right):
    """Compare the exact representations TinyMongo would persist.

    Query equality intentionally treats numeric BSON types as one family. A
    write decision cannot: replacing integer ``1`` with double ``1.0`` or with
    ``Decimal128('1.00')`` changes the stored BSON representation and must not
    be optimized away.
    """

    options = {"ensure_ascii": False, "separators": (",", ":")}
    return dumps(left, **options) == dumps(right, **options)


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


def _decimal128_payload(payload):
    """Return a valid 16-byte Decimal128 BID payload, or ``None``."""

    if not isinstance(payload, str) or len(payload) != 32:
        return None
    try:
        decoded = bytes.fromhex(payload)
    except ValueError:
        return None
    return decoded if len(decoded) == 16 else None


def _uuid_payload(payload):
    """Return a canonical RFC-4122 UUID payload, or ``None``."""

    if not isinstance(payload, str) or len(payload) != 36:
        return None
    try:
        parsed = UUID(payload)
    except (AttributeError, ValueError):
        return None
    return parsed if str(parsed) == payload.lower() else None
