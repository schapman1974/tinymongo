"""Retain plain JSON table text without bypassing BSON write validation."""

import json
import math
import re

from .bson_codec import loads

_WHITESPACE = re.compile(r"[ \t\n\r]*")
_MARKER_KEYS = {"__tinymongo_type_v1__", "value"}


def _valid_unicode(value):
    try:
        value.encode("utf8")
        return True
    except UnicodeEncodeError:
        return False


def _reusable(value):
    # Tagged values must pass through the codec when written. Legacy invalid
    # keys, surrogate strings and nonfinite numbers also need its normal path.
    if isinstance(value, dict):
        return set(value) != _MARKER_KEYS and all(
            "\x00" not in key and _valid_unicode(key) and _reusable(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_reusable(item) for item in value)
    if isinstance(value, str):
        return _valid_unicode(value)
    return not isinstance(value, float) or math.isfinite(value)


def load_table_chunks(text):
    """Decode a complete JSON value and retain eligible top-level table text.

    The C decoder validates each key/value and supplies offsets, avoiding a
    Python character scan of large strings. All object delimiters and trailing
    input are checked before any chunks can be published. Duplicate keys retain
    JSON's last-value-wins behavior, including invalidating an earlier chunk.
    """
    decoder = json.JSONDecoder()
    position = _WHITESPACE.match(text, 0).end()
    if text[position : position + 1] != "{":
        return loads(text), {}
    position = _WHITESPACE.match(text, position + 1).end()
    raw, chunks = {}, {}
    if text[position : position + 1] == "}":
        position += 1
    else:
        while True:
            key, position = decoder.raw_decode(text, position)
            if not isinstance(key, str):
                raise ValueError("JSON object key must be a string")
            position = _WHITESPACE.match(text, position).end()
            if text[position : position + 1] != ":":
                raise ValueError("Expected JSON object colon")
            start = _WHITESPACE.match(text, position + 1).end()
            value, position = decoder.raw_decode(text, start)
            raw[key] = value
            chunks.pop(key, None)
            if isinstance(value, dict) and _reusable(value):
                chunks[key] = (text[start:position],)
            position = _WHITESPACE.match(text, position).end()
            delimiter = text[position : position + 1]
            if delimiter == "}":
                position += 1
                break
            if delimiter != ",":
                raise ValueError("Expected JSON object comma")
            position = _WHITESPACE.match(text, position + 1).end()
    if _WHITESPACE.match(text, position).end() != len(text):
        raise ValueError("Trailing JSON data")
    # A root marker can change the entire shape during BSON decoding.
    if set(raw) == _MARKER_KEYS:
        chunks = {}
    return loads(raw), chunks
