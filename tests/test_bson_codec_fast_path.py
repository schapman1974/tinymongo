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


def test_clone_builtin_tree_matches_storage_without_json_text(monkeypatch):
    document = {
        "body": "transcript 🦆" * 10000,
        "values": [None, True, 2**70, -0.0, 1.25, "\ud800"],
        "nested": ({1: ["original"], "1": ["last key wins"]},),
        "nonfinite": [float("nan"), float("inf"), float("-inf")],
    }
    expected = bson_codec.loads(bson_codec.dumps(document))

    def unexpected_json(*args, **kwargs):
        raise AssertionError("cloning builtins must not serialize JSON text")

    monkeypatch.setattr(bson_codec.json, "dumps", unexpected_json)
    monkeypatch.setattr(bson_codec.json, "loads", unexpected_json)
    cloned = bson_codec.clone(document)
    assert cloned["body"] == expected["body"]
    assert cloned["values"] == expected["values"]
    assert math.copysign(1, cloned["values"][3]) == -1
    assert cloned["nested"] == expected["nested"]
    assert math.isnan(cloned["nonfinite"][0])
    assert cloned["nonfinite"][1:] == expected["nonfinite"][1:]
    cloned["nested"][0]["1"].append("changed")
    cloned["values"].append("changed")
    assert document["nested"][0]["1"] == ["last key wins"]
    assert len(document["values"]) == 6
    document["nested"][0][1].append("source change")
    assert cloned["nested"][0]["1"] == ["last key wins", "changed"]


@pytest.mark.parametrize(
    "document",
    [
        {"__tinymongo_type_v1__": "float", "value": "infinity"},
        {"nested": {"__tinymongo_type_v1__": "unknown", "value": [1]}},
    ],
)
def test_clone_reserved_mapping_remains_user_data(document):
    cloned = bson_codec.clone(document)
    assert cloned == document
    assert cloned is not document


def test_clone_subclasses_use_storage_normalization():
    class Text(str):
        pass

    class Number(int):
        pass

    cloned = bson_codec.clone({"text": Text("value"), "number": Number(7)})
    assert type(cloned["text"]) is str
    assert type(cloned["number"]) is int


def test_clone_extended_bson_stays_isolated_and_normalized():
    bson = pytest.importorskip("bson")
    value = {
        "code": bson.Code("x", {"nested": [1]}),
        "binary": bson.Binary(b"abc", subtype=0),
        "integer": bson.Int64(7),
    }
    cloned = bson_codec.clone(value)
    expected = bson_codec.loads(bson_codec.dumps(value))
    assert type(cloned["code"]) is bson.Code
    assert type(cloned["binary"]) is bytes
    assert type(cloned["integer"]) is type(expected["integer"])
    assert cloned["integer"] == expected["integer"]
    cloned["code"].scope["nested"].append(2)
    assert value["code"].scope["nested"] == [1]


@pytest.mark.parametrize("document", [{"bad\x00key": 1}, {"nested": [object()]}])
def test_clone_invalid_document_preserves_root(document):
    with pytest.raises(InvalidDocument) as caught:
        bson_codec.clone(document)
    assert caught.value.document is document
