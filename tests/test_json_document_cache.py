"""Unchanged document text may be reused, but storage representation may not change."""

from datetime import datetime
import itertools
import math

import pytest
from bson import Binary, Code, Decimal128, Int64, ObjectId, Regex, Timestamp

from tinymongo import bson_codec as codec
from tinymongo import storage_backends as sb
from tinymongo.errors import InvalidDocument


def test_representation_comparison_matches_json_oracle():
    class MisleadingInt(int):
        def __int__(self):
            return 99

    values = [
        None,
        False,
        True,
        0,
        1,
        0.0,
        -0.0,
        1.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        "one",
        "",
        [],
        [1],
        [1.0],
        [1, 2],
        {"a": 1, "b": 2},
        {"b": 2, "a": 1},
        {"a": 1.0, "b": 2},
        {"a": [1, {"z": -0.0}]},
        (1, 2),
        Int64(1),
        MisleadingInt(1),
        Decimal128("1.0"),
        Decimal128("1.00"),
        Binary(b"abc", 0),
        b"abc",
        Binary(b"abc", 128),
        Code("one"),
        Code("one", {"x": 1}),
        ObjectId("0" * 24),
        datetime(2026, 1, 1),
        Timestamp(1, 2),
        Regex("x"),
        {"__tinymongo_type_v1__": "datetime", "value": "literal"},
    ]
    for left, right in itertools.product(values, repeat=2):
        expected = codec.dumps(left) == codec.dumps(right)
        assert codec.storage_values_equal(left, right) == expected, (left, right)


def test_same_collection_insert_reuses_resident_document_text(tmp_path, monkeypatch):
    storage = sb.AtomicJSONStorage(str(tmp_path / "app.json"))
    resident = {"_id": "resident", "body": "x" * 100_000, "oid": ObjectId()}
    storage.write_table("docs", {1: resident})
    saved = storage._serialized_documents["docs"]["1"]
    original = sb.json_dumps
    serialized = []

    def record(value, **kwargs):
        serialized.append(value)
        return original(value, **kwargs)

    monkeypatch.setattr(sb, "json_dumps", record)
    storage.write_table("docs", {1: resident, 2: {"_id": "new"}})
    assert storage._serialized_documents["docs"]["1"] is saved
    assert not any(
        isinstance(v, dict) and v.get("_id") == "resident" for v in serialized
    )
    assert len(storage.read()["docs"]) == 2
    # Returned and original caller containers cannot mutate the cached snapshot.
    resident["body"] = "changed"
    returned = storage.read_table("docs")
    returned["1"]["body"] = "also changed"
    assert storage.read_table("docs")["1"]["body"] == "x" * 100_000


def test_cached_writes_keep_exact_types_order_and_bson(tmp_path):
    path = tmp_path / "app.json"
    storage = sb.AtomicJSONStorage(str(path))
    storage.merge_writes = False
    values = [
        1,
        1.0,
        True,
        0.0,
        -0.0,
        Decimal128("1.0"),
        Decimal128("1.00"),
        Binary(b"a", 128),
        Binary(b"a", 129),
        Code("x", {"n": 1}),
        Code("x", {"n": 1.0}),
        {"a": 1, "b": 2},
        {"b": 2, "a": 1},
        [1],
        [1.0],
        float("nan"),
        float("inf"),
        {"__tinymongo_type_v1__": "float", "value": "nan"},
    ]
    for value in values:
        data = {"docs": {"1": {"_id": "fixed", "value": value}}}
        storage.write_table("docs", data["docs"])
        assert path.read_text() == codec.dumps(data, ensure_ascii=False)
        assert codec.dumps(storage.read()) == codec.dumps(data)
    storage.write_table("docs", {"2": {"_id": "replacement", "value": -0.0}})
    assert set(storage._serialized_documents["docs"]) == {"2"}
    assert math.copysign(1, storage.read()["docs"]["2"]["value"]) == -1
    storage.purge_table("docs")
    assert storage._serialized_documents == {}
    storage.close()
    assert storage._serialized_documents == {}


@pytest.mark.parametrize("operation", ["replace", "fsync"])
def test_failed_persistence_keeps_document_chunks(tmp_path, monkeypatch, operation):
    path = tmp_path / "app.json"
    storage = sb.AtomicJSONStorage(str(path))
    storage.write_table("docs", {1: {"_id": 1, "value": "before"}})
    cached = storage._serialized_documents
    original = path.read_bytes()

    def fail(*args):
        raise OSError("injected")

    with monkeypatch.context() as patch:
        patch.setattr(sb.os, operation, fail)
        with pytest.raises(OSError):
            storage.write_table("docs", {1: {"_id": 1, "value": "after"}})
    assert storage._serialized_documents is cached
    assert path.read_bytes() == original
    storage.write_table("docs", {1: {"_id": 1, "value": "before"}})
    assert path.read_bytes() == original
    # A direct external edit clears both table and document serialization caches.
    path.write_text(codec.dumps({"docs": {"1": {"_id": 1, "value": "external"}}}))
    storage.write_table("docs", {2: {"_id": 2}})
    assert storage.read()["docs"]["1"]["value"] == "external"


def test_legacy_non_mapping_and_reserved_table_shapes(tmp_path):
    path = tmp_path / "app.json"
    storage = sb.AtomicJSONStorage(str(path))
    legacy = {"legacy": [1, 2], "marker": {"__tinymongo_type_v1__": "a", "value": "b"}}
    path.write_text(codec.dumps(legacy))
    storage.write_table("docs", {1: {"_id": 1}})
    assert storage.read()["legacy"] == legacy["legacy"]
    assert storage.read()["marker"] == legacy["marker"]


def test_legacy_nul_table_key_still_fails_codec_validation(tmp_path):
    path = tmp_path / "app.json"
    path.write_text('{"legacy": {"bad\\u0000key": {"_id": 1}}}')
    storage = sb.AtomicJSONStorage(str(path))
    original = path.read_bytes()
    with pytest.raises(InvalidDocument):
        storage.write_table("new", {})
    assert path.read_bytes() == original
