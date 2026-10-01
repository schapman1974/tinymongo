"""Warm JSON writes reuse exact string keys without retaining deleted keys."""

import pytest

from tinymongo import TinyMongoClient, bson_codec as codec, storage_backends as sb
from tinymongo.errors import InvalidDocument


@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_warm_inserts_do_not_encode_resident_keys(tmp_path, monkeypatch, unique, batch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.items
        if unique:
            col.create_index("n", unique=True)
        col.insert_many([{"_id": i, "n": i} for i in range(100)])
        encoded = []
        original = sb.json_dumps

        def record(value, **kwargs):
            if type(value) is str and value in {str(i) for i in range(1, 101)}:
                encoded.append(value)
            return original(value, **kwargs)

        monkeypatch.setattr(sb, "json_dumps", record)
        for i in range(3):
            rows = [
                {"_id": 100 + i * 2 + j, "n": 100 + i * 2 + j}
                for j in range(2 if batch else 1)
            ]
            if batch:
                col.insert_many(rows)
            else:
                col.insert_one(rows[0])
        assert encoded == []
        assert col.count_documents({}) == 100 + 3 * (2 if batch else 1)
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        assert client.app.items.find_one({"_id": 0})["n"] == 0


def test_key_cache_tracks_current_keys_and_table_lifecycle(tmp_path, monkeypatch):
    path = tmp_path / "app.json"
    storage = sb.AtomicJSONStorage(str(path))
    storage.merge_writes = False
    table = {'quote"': {"_id": 1}, "snow☃": {"_id": 2}, "back\\": {"_id": 3}}
    storage.write_table("docs", table)
    saved = storage._serialized_keys["docs"].copy()
    storage.write_table("other", {"1": {"_id": "neighbor"}})
    assert storage._serialized_keys["docs"] == saved
    table = {"back\\": {"_id": 3, "new": True}, 'quote"': {"_id": 1}}
    storage.write_table("docs", table)
    assert set(storage._serialized_keys["docs"]) == set(table)
    assert list(storage.read()["docs"]) == list(table)
    assert path.read_text() == codec.dumps(
        {"docs": table, "other": {"1": {"_id": "neighbor"}}}, ensure_ascii=False
    )
    table["snow☃"] = {"_id": 4}
    storage.write_table("docs", table)
    assert storage.read()["docs"] == table
    storage.purge_table("docs")
    assert "docs" not in storage._serialized_keys
    storage.close()
    assert storage._serialized_keys == {}


@pytest.mark.parametrize("operation", ["replace", "delete", "full-write"])
def test_revision_changes_discard_key_cache(tmp_path, operation):
    path = tmp_path / "app.json"
    storage = sb.AtomicJSONStorage(str(path))
    storage.write_table("docs", {"1": {"_id": "old"}})
    if operation == "delete":
        path.unlink()
    else:
        peer = sb.AtomicJSONStorage(str(path)) if operation == "replace" else storage
        peer.merge_writes = False
        peer.write({"docs": {"7": {"_id": "peer"}}})
    storage.merge_writes = True
    storage.table_names()
    assert storage._serialized_keys == {}
    storage.write_table("docs", {"8": {"_id": "new"}})
    assert set(storage._serialized_keys["docs"]) == (
        {"1"} if operation == "delete" else {"7", "8"}
    )
    assert all(row["_id"] != "old" for row in storage.read()["docs"].values())


@pytest.mark.parametrize("failure", ["encode", "fsync", "replace"])
def test_failed_write_does_not_publish_key_cache(tmp_path, monkeypatch, failure):
    path = tmp_path / "app.json"
    storage = sb.AtomicJSONStorage(str(path))
    storage.write_table("docs", {"1": {"_id": 1}})
    before = path.read_bytes()
    keys = storage._serialized_keys

    def fail(*args, **kwargs):
        raise OSError("injected")

    with monkeypatch.context() as patch:
        if failure == "encode":
            patch.setattr(sb, "json_dumps", fail)
        else:
            patch.setattr(sb.os, failure, fail)
        with pytest.raises(OSError, match="injected"):
            storage.write_table("docs", {"2": {"_id": 2}})
    assert storage._serialized_keys is keys
    assert set(keys["docs"]) == {"1"}
    assert path.read_bytes() == before
    storage.write_table("docs", {"2": {"_id": 2}})
    assert set(storage._serialized_keys["docs"]) == {"1", "2"}


def test_cold_open_warms_keys_on_first_changed_table(tmp_path):
    path = tmp_path / "app.json"
    path.write_text(codec.dumps({"docs": {"1": {"_id": 1}}}))
    storage = sb.AtomicJSONStorage(str(path))
    assert storage.read_table("docs") == {"1": {"_id": 1}}
    assert storage._serialized_keys == {}
    storage.write_table("other", {})
    assert storage._serialized_keys["docs"] == {}
    storage.write_table("docs", {"2": {"_id": 2}})
    assert set(storage._serialized_keys["docs"]) == {"1", "2"}


def test_custom_serializer_hook_is_called_without_assuming_key_cache(tmp_path):
    calls = []

    class CustomStorage(sb.AtomicJSONStorage):
        def _serialize_table(self, name, table):
            calls.append(name)
            return super()._serialize_table(name, table)

    storage = CustomStorage(str(tmp_path / "app.json"))
    storage.write_table("docs", {"1": {"_id": 1}})
    storage.write_table("other", {})
    storage.write_table("docs", {"2": {"_id": 2}})
    assert calls == ["docs", "other", "docs"]
    assert storage.read()["docs"] == {"1": {"_id": 1}, "2": {"_id": 2}}
    assert storage._serialized_keys == {"other": {}}


@pytest.mark.parametrize(
    "table", [[1, 2], {"__tinymongo_type_v1__": "a", "value": "b"}, {}]
)
def test_fallback_and_empty_tables_do_not_retain_stale_keys(tmp_path, table):
    storage = sb.AtomicJSONStorage(str(tmp_path / "app.json"))
    storage.write_table("docs", {"1": {"_id": 1}})
    storage._write_cached_tables({"docs": table}, {"docs"})
    assert storage._serialized_keys["docs"] == {}
    assert storage.read()["docs"] == table


def test_non_native_keys_keep_conversion_and_never_enter_cache(tmp_path):
    class Key(str):
        text = "old"

        def __str__(self):
            return self.text

    storage = sb.AtomicJSONStorage(str(tmp_path / "app.json"))
    key = Key("key")
    for text in ("old", "new"):
        key.text = text
        table = {key: {"_id": 1}, 2: {"_id": 2}}
        storage._write_cached_tables({"docs": table}, {"docs"})
        assert storage._serialized_keys["docs"] == {}
        assert storage.read()["docs"] == {text: {"_id": 1}, "2": {"_id": 2}}


def test_nul_key_still_rejected_without_cache_publication(tmp_path):
    storage = sb.AtomicJSONStorage(str(tmp_path / "app.json"))
    storage.write_table("docs", {"1": {"_id": 1}})
    keys = storage._serialized_keys
    with pytest.raises(InvalidDocument):
        storage._write_cached_tables({"docs": {"nul\x00": {}}}, {"docs"})
    assert storage._serialized_keys is keys
