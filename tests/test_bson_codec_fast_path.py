"""Regression coverage for the exact-built-in BSON codec fast path."""

import math

import pytest

from tinymongo import bson_codec, bson_types
from tinymongo.errors import InvalidDocument


def test_exact_json_builtins_do_not_consult_bson_registry(monkeypatch):
    def unexpected_registry_lookup(value):
        raise AssertionError(
            "exact built-in {0!r} reached the BSON registry".format(type(value))
        )

    monkeypatch.setattr(bson_types, "bson_type_spec", unexpected_registry_lookup)
    document = {
        "null": None,
        "boolean": True,
        "integer": 42,
        "double": 3.5,
        "text": "ordinary",
        "array": [1, "two", False],
        "tuple": (3, None),
        "nested": {"value": 4},
        "nonfinite": float("inf"),
    }

    encoded = bson_codec.encode_value(document)

    assert encoded == {
        "null": None,
        "boolean": True,
        "integer": 42,
        "double": 3.5,
        "text": "ordinary",
        "array": [1, "two", False],
        "tuple": [3, None],
        "nested": {"value": 4},
        "nonfinite": {
            "__tinymongo_type_v1__": "float",
            "value": "infinity",
        },
    }


def test_builtin_subclasses_still_consult_bson_registry(monkeypatch):
    class Text(str):
        pass

    class Number(int):
        pass

    class Double(float):
        pass

    class Document(dict):
        pass

    class Array(list):
        pass

    original = bson_types.bson_type_spec
    seen = []

    def recording_registry_lookup(value):
        seen.append(type(value))
        return original(value)

    monkeypatch.setattr(bson_types, "bson_type_spec", recording_registry_lookup)
    value = Document(
        {
            "text": Text("subclass"),
            "number": Number(7),
            "double": Double(2.5),
            "array": Array([Text("nested")]),
        }
    )

    encoded = bson_codec.encode_value(value)

    assert encoded == {
        "text": "subclass",
        "number": 7,
        "double": 2.5,
        "array": ["nested"],
    }
    assert {Document, Text, Number, Double, Array}.issubset(set(seen))


def test_pymongo_native_subclasses_keep_their_bson_tags():
    bson = pytest.importorskip("bson")
    script = bson.Code("return answer;", {"answer": 42})
    binary = bson.Binary(bytes(range(16)), subtype=4)

    encoded = bson_codec.encode_value({"script": script, "binary": binary})
    restored = bson_codec.decode_value(encoded)

    assert encoded["script"]["__tinymongo_type_v1__"] == "code"
    assert encoded["binary"]["__tinymongo_type_v1__"] == "binary"
    assert type(restored["script"]) is bson.Code
    assert restored["script"] == script
    assert type(restored["binary"]) is bson.Binary
    assert restored["binary"] == binary
    assert restored["binary"].subtype == 4


def test_fast_path_preserves_invalid_document_context_and_root():
    document = {"outer": [{"unsupported": {1, 2}}]}

    with pytest.raises(InvalidDocument) as caught:
        bson_codec.dumps(document, document_context="batch document 8")

    assert caught.value.document is document
    assert "batch document 8" in str(caught.value)
    assert "$['outer'][0]['unsupported']" in str(caught.value)


def test_document_context_can_be_built_only_when_validation_fails():
    context_calls = []

    def context():
        context_calls.append(True)
        return "lazy batch document"

    assert bson_codec.loads(
        bson_codec.dumps({"value": 1}, document_context=context)
    ) == {"value": 1}
    assert context_calls == []

    with pytest.raises(InvalidDocument) as caught:
        bson_codec.dumps({"value": object()}, document_context=context)

    assert context_calls == [True]
    assert "lazy batch document" in str(caught.value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_fast_path_keeps_nonfinite_float_round_trips(value):
    restored = bson_codec.loads(bson_codec.dumps({"value": value}))["value"]

    if math.isnan(value):
        assert math.isnan(restored)
    else:
        assert restored == value


def test_nonfinite_float_subclass_uses_the_bson_aware_fallback():
    class Double(float):
        pass

    restored = bson_codec.loads(bson_codec.dumps({"value": Double("inf")}))["value"]

    assert restored == float("inf")
