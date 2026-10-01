"""Cold table caching preserves the codec and atomic storage contract."""

import json
from datetime import datetime

import pytest

from tinymongo import storage_backends as sb
from tinymongo.bson_codec import dumps, loads
from tinymongo.errors import InvalidDocument, StorageCorruptionError
from tinymongo.json_chunks import load_table_chunks


@pytest.mark.parametrize("kind", ["plain", "datetime", "objectid"])
def test_first_write_does_not_serialize_untouched_plain_table(
    tmp_path, monkeypatch, kind
):
    path = tmp_path / "db.json"
    archive = {"1": {"_id": "large", "body": "é😀" * 50_000}}
    if kind == "datetime":
        archive["1"]["tagged"] = datetime(2026, 1, 1)
    elif kind == "objectid":
        archive["1"]["tagged"] = pytest.importorskip("bson").ObjectId()
    path.write_text(dumps({"archive": archive}))
    storage = sb.AtomicJSONStorage(str(path))
    original = sb.json_dumps

    def checked(value, **kwargs):
        result = original(value, **kwargs)
        assert len(result) < 1000, "untouched resident text was serialized"
        return result

    monkeypatch.setattr(sb, "json_dumps", checked)
    storage.write_table("target", {1: {"_id": "new"}})
    assert loads(path.read_text())["archive"] == archive
    # On its first dirty write the table switches to document-level caching.
    monkeypatch.setattr(sb, "json_dumps", original)
    storage.write_table("archive", {2: {"_id": "second"}})
    assert len(storage.read_table("archive")) == 2
    storage.close()
    assert storage._serialized_tables == {}


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        ' \n {"a" : {"1": {"x":1e2,"z":-0.0,"v":[null,true,"é😀"]}}} \t',
        '{"a":{"1":{"x":1,"x":2}},"a":{"2":{"x":3}}}',
        '{"a":null}',
        '{"a":[]}',
        "[]",
        "null",
        '{"a":{"1":{"x":NaN,"y":Infinity,"z":1e999}}}',
        '{"a":{"1":{"x":"\\ud800"}}}',
        '{"a":{"1":{"\\ud800":1}}}',
        '{"a":{"1":{"bad\\u0000key":0}}}',
        '{"a":{"1":{"__tinymongo_type_v1__":"future","value":1}}}',
        '{"__tinymongo_type_v1__":"future","value":{}}',
        '{"a":{},"a":{"1":{"__tinymongo_type_v1__":"future","value":1}}}',
    ],
)
def test_parser_matches_codec_and_retains_only_safe_chunks(text):
    data, chunks = load_table_chunks(text)
    assert repr(data) == repr(loads(text))
    for name, chunk in chunks.items():
        assert loads(chunk[0]) == data[name]
        assert dumps(data[name])  # Retention must never bypass write rejection.


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        '{"a":{},}',
        '{"a" {}}',
        '{"a":}',
        '{"a":{}',
        "{1:2}",
        '{"a":{}} trailing',
        "{} {}",
        '{"a":{} "b":{}}',
        '{"a":{"x":"bad\ntext"}}',
        '{"a":01}',
    ],
)
def test_parser_rejects_complete_malformed_input(text):
    with pytest.raises(ValueError):
        load_table_chunks(text)


def test_tagged_tables_keep_codec_normalization(tmp_path):
    path = tmp_path / "db.json"
    archive = {"1": {"when": datetime(2026, 1, 1)}}
    path.write_text(dumps({"archive": archive}))
    storage = sb.AtomicJSONStorage(str(path))
    assert storage.table_names() == {"archive"}
    assert "archive" in storage._serialized_tables
    storage.write_table("target", {1: {"_id": 1}})
    assert storage.read()["archive"] == archive


@pytest.mark.parametrize(
    "marker,payload",
    [
        ("datetime", "2026-01-01T00:00:00.123456"),
        ("datetime", "2026-01-01T00:00:00+04:00"),
        ("datetime", "2026-01-01"),
        ("datetime", "invalid"),
        ("datetime", 1),
        ("datetime", "\ud800"),
        ("objectid", "0123456789ABCDEF01234567"),
        ("objectid", "invalid"),
        ("future", "value"),
        ([], 1),
        ("mapping", [["a", 1], ["a", 2]]),
        ("float", "nan"),
    ],
)
def test_noncanonical_tags_follow_existing_write_normalization(
    tmp_path, marker, payload
):
    if marker == "objectid":
        pytest.importorskip("bson")
    path = tmp_path / "db.json"
    text = json.dumps(
        {"archive": {"1": {"tag": {"__tinymongo_type_v1__": marker, "value": payload}}}}
    )
    path.write_text(text)
    data, chunks = load_table_chunks(text)
    assert chunks == {}
    expected = loads(dumps(data))
    storage = sb.AtomicJSONStorage(str(path))
    # A lone surrogate still fails in the existing UTF-8 persistence path.
    if payload == "\ud800":
        with pytest.raises(UnicodeEncodeError):
            storage.write_table("target", {1: {"_id": "new"}})
        assert path.read_text() == text
    else:
        storage.write_table("target", {1: {"_id": "new"}})
        actual = storage.read()
        del actual["target"]
        assert dumps(actual) == dumps(expected)


@pytest.mark.parametrize("bad", ['{"bad\\u0000key":0}', '{"x":"\\ud800"}'])
def test_legacy_invalid_untouched_values_still_reject_write(tmp_path, bad):
    path = tmp_path / "db.json"
    text = '{"archive":{"1":' + bad + "}}"
    path.write_text(text)
    storage = sb.AtomicJSONStorage(str(path))
    with pytest.raises((InvalidDocument, UnicodeEncodeError)):
        storage.write_table("target", {1: {"_id": 1}})
    assert path.read_text() == text
    assert "target" not in storage._cached_data


def test_external_changes_corruption_and_failed_replace(tmp_path, monkeypatch):
    path = tmp_path / "db.json"
    path.write_text('{"archive":{"1":{"x":"old"}}}')
    storage = sb.AtomicJSONStorage(str(path))
    storage.table_names()
    path.write_text('{"archive":{"1":{"x":"new"}}}')
    original = sb.os.replace
    with monkeypatch.context() as patch:
        patch.setattr(
            sb.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("fail"))
        )
        with pytest.raises(OSError, match="fail"):
            storage.write_table("target", {1: {"_id": 1}})
    assert "target" not in storage._cached_data
    storage.write_table("target", {1: {"_id": 1}})
    assert storage.read()["archive"]["1"]["x"] == "new"
    replacement = tmp_path / "replacement"
    replacement.write_text('{"other":{}}')
    original(replacement, path)
    assert storage.table_names() == {"other"}
    path.write_text('{"partial":{}} trailing')
    with pytest.raises(StorageCorruptionError):
        storage.table_names()
    assert not storage._retain_read_chunks
    assert storage._read_chunks == {}
    path.unlink()
    assert storage.table_names() == set()
    path.touch()
    assert storage.table_names() == set()
    storage.write_table("new", {1: {"_id": 1}})
    assert set(json.loads(path.read_text())) == {"new"}


def test_read_overrides_and_public_reads_remain_isolated(tmp_path, monkeypatch):
    path = tmp_path / "db.json"
    path.write_text('{"archive":{"1":{"x":"old"}}}')

    class ModifiedRead(sb.AtomicJSONStorage):
        def read(self):
            data = super().read()
            data["archive"]["1"]["x"] = "override"
            return data

    storage = ModifiedRead(str(path))
    storage.write_table("target", {1: {"_id": 1}})
    assert loads(path.read_text())["archive"]["1"]["x"] == "override"
    storage = sb.AtomicJSONStorage(str(path))
    storage.read()["archive"]["1"]["x"] = "caller"
    assert storage._read_chunks == storage._serialized_tables == {}
    with monkeypatch.context() as patch:
        patch.setattr(storage, "read", lambda: {"mock": {}})
        assert storage.table_names() == {"mock"}
        assert storage._serialized_tables == {}
    storage.close()
    assert storage.read()["archive"]["1"]["x"] == "override"
