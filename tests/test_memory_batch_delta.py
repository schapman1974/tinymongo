from datetime import datetime, timezone
from uuid import uuid4

import pytest

from tinymongo import TinyMongoClient, storage_backends as sb


def test_memory_batch_clones_only_new_rows(monkeypatch):
    bson = pytest.importorskip("bson")
    client = TinyMongoClient("memory://" + uuid4().hex, backend="memory")
    collection = client.app.items
    old_id = bson.ObjectId()
    collection.insert_many(
        [{"_id": old_id, "nested": [1], "when": datetime(2020, 1, 1)}]
    )
    original = sb.clone_document
    cloned = []

    def track(value):
        if "items" in value:
            cloned.extend(row["_id"] for row in value["items"].values())
        return original(value)

    monkeypatch.setattr(sb, "clone_document", track)
    document = {
        "_id": "new",
        "nested": [2],
        "when": datetime(2020, 1, 1, tzinfo=timezone.utc),
    }
    collection.insert_many([document])
    assert cloned == ["new"]
    document["nested"].append(3)
    assert collection.find_one({"_id": "new"})["nested"] == [2]
    assert collection.find_one({"_id": old_id})["nested"] == [1]
    assert collection.find_one({"_id": "new"})["when"] == datetime(2020, 1, 1)
    client.close()


@pytest.mark.parametrize(
    "hook",
    [
        "proxy_write",
        "storage_write",
        "proxy_read",
        "storage_read",
        "table_write",
        "table_read",
    ],
)
def test_custom_hooks_receive_full_snapshot(monkeypatch, hook):
    db = sb.MemoryTinyDB(uuid4().hex, storage=sb.MemoryStorage)
    table = db.table("items")
    table.insert({"_id": "old"})
    owner, name = {
        "proxy_write": (table._storage, "write"),
        "storage_write": (db._storage, "write_table"),
        "proxy_read": (table._storage, "read"),
        "storage_read": (db._storage, "read_table"),
        "table_write": (table, "_write"),
        "table_read": (table, "_read"),
    }[hook]
    original = getattr(owner, name)
    seen = []

    def wrapped(*args):
        if hook.endswith("write"):
            seen.extend(row["_id"] for row in args[-1].values())
        result = original(*args)
        if hook.endswith("read"):
            for row in result.values():
                if row["_id"] == "old":
                    row["transformed"] = True
        return result

    monkeypatch.setattr(owner, name, wrapped)
    snapshot = table._read_insert_snapshot()
    table._insert_multiple_from_snapshot([{"_id": "new"}], snapshot)
    if hook.endswith("write"):
        assert seen == ["old", "new"]
    assert [row["_id"] for row in table.all()] == ["old", "new"]
    if hook.endswith("read"):
        assert db._storage._entry["data"]["items"]["1"]["transformed"] is True


@pytest.mark.parametrize(
    "rows", [[{"value": "legacy"}], [{"_id": "same", "v": 1}, {"_id": "same", "v": 2}]]
)
def test_legacy_rows_keep_full_replay(rows):
    db = sb.MemoryTinyDB(uuid4().hex, storage=sb.MemoryStorage)
    storage = db._storage
    storage.merge_writes = False
    storage.write_table("items", {str(i + 1): row for i, row in enumerate(rows)})
    storage.merge_writes = True
    table = db.table("items")
    snapshot = table._read_insert_snapshot()
    expected = storage._merge_data(
        {"items": storage.read_table("items")},
        {"items": dict(snapshot, **{str(len(rows) + 1): {"_id": "new"}})},
    )
    table._insert_multiple_from_snapshot([{"_id": "new"}], snapshot)
    assert storage.read_table("items") == expected["items"]


def test_replacement_mode_retains_resident_rows():
    db = sb.MemoryTinyDB(uuid4().hex, storage=sb.MemoryStorage)
    table = db.table("items")
    table.insert({"_id": "old"})
    db._storage.merge_writes = False
    table._insert_multiple_from_snapshot(
        [{"_id": "new"}], table._read_insert_snapshot()
    )
    assert [row["_id"] for row in table.all()] == ["old", "new"]


@pytest.mark.parametrize("failure", ["codec", "storage"])
def test_failed_delta_does_not_publish_and_can_retry(monkeypatch, failure):
    client = TinyMongoClient("memory://" + uuid4().hex, backend="memory")
    collection = client.app.items
    collection.insert_many([{"_id": "old", "nested": [1]}])
    storage = collection.parent.tinydb._storage
    revision = storage.revision

    def fail(*args):
        raise OSError("injected delta failure")

    with monkeypatch.context() as patch:
        if failure == "codec":
            patch.setattr(sb, "clone_document", fail)
        else:
            patch.setattr(sb.MemoryStorage, "write_table", fail)
        with pytest.raises(OSError, match="injected delta"):
            collection.insert_many([{"_id": "failed"}])
    assert storage.revision == revision
    assert list(collection.find({})) == [{"_id": "old", "nested": [1]}]
    collection.insert_many([{"_id": "retry"}])
    assert {row["_id"] for row in collection.find({})} == {"old", "retry"}
    client.close()


def test_colliding_legacy_internal_ids_keep_full_replay():
    db = sb.MemoryTinyDB(uuid4().hex, storage=sb.MemoryStorage)
    storage = db._storage
    storage.merge_writes = False
    storage.write_table(
        "items", {"01": {"_id": "same", "v": 1}, "1": {"_id": "same", "v": 2}}
    )
    storage.merge_writes = True
    table = db.table("items")
    snapshot = table._read_insert_snapshot()
    table._insert_multiple_from_snapshot([{"_id": "new"}], snapshot)
    rows = storage.read_table("items")
    assert rows["01"] == {"_id": "same", "v": 2}
    assert rows["1"] == {"_id": "same", "v": 2}
    assert rows["2"] == {"_id": "new"}


def test_memory_batch_does_not_copy_resident_payload(monkeypatch):
    client = TinyMongoClient("memory://" + uuid4().hex, backend="memory")
    collection = client.app.items
    collection.insert_many([{"_id": "old", "nested": [1, {"payload": "resident"}]}])
    original = sb.copy.deepcopy
    copied = []

    def track(value, *args, **kwargs):
        if isinstance(value, dict) and any(
            isinstance(row, dict) and "nested" in row for row in value.values()
        ):
            copied.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(sb.copy, "deepcopy", track)
    collection.insert_many([{"_id": "new"}])
    assert copied == []
    assert collection.find_one({"_id": "old"})["nested"] == [1, {"payload": "resident"}]
    client.close()


@pytest.mark.parametrize("mode", ["replacement", "hook"])
def test_id_snapshot_never_replaces_full_rows(monkeypatch, mode):
    db = sb.MemoryTinyDB(uuid4().hex, storage=sb.MemoryStorage)
    table = db.table("items")
    table.insert({"_id": {"nested": [1]}, "payload": [2]})
    snapshot = table._read_insert_snapshot(ids_only=True)
    assert isinstance(snapshot, sb._InsertIDSnapshot)
    snapshot[1]["_id"]["nested"].append(99)
    assert table.all()[0]["_id"] == {"nested": [1]}
    if mode == "replacement":
        db._storage.merge_writes = False
    else:
        original = table._write
        monkeypatch.setattr(table, "_write", lambda data: original(data))
    table._insert_multiple_from_snapshot([{"_id": "new"}], snapshot)
    assert table.all() == [{"_id": {"nested": [1]}, "payload": [2]}, {"_id": "new"}]


@pytest.mark.parametrize(
    "rows",
    [
        {"1": {"payload": [1]}},
        {"1": {"_id": 1}, "2": {"_id": 1.0}},
        {"01": {"_id": "a"}, "1": {"_id": "b"}},
        {"1": [("_id", "legacy"), ("payload", [1])]},
    ],
)
def test_id_planning_legacy_falls_back(rows):
    db = sb.MemoryTinyDB(uuid4().hex, storage=sb.MemoryStorage)
    db._storage._entry["data"] = {"items": rows}
    table = db.table("items")
    assert not isinstance(
        table._read_insert_snapshot(ids_only=True), sb._InsertIDSnapshot
    )


@pytest.mark.parametrize("ordered", [True, False])
def test_id_planning_bson_duplicates(ordered):
    from tinymongo.errors import BulkWriteError

    client = TinyMongoClient("memory://" + uuid4().hex, backend="memory")
    collection = client.app.items
    collection.insert_many([{"_id": 1, "payload": [1]}])
    with pytest.raises(BulkWriteError) as caught:
        collection.insert_many([{"_id": 1.0}, {"_id": True}], ordered=ordered)
    assert caught.value.details["nInserted"] == (0 if ordered else 1)
    assert collection.find_one({"_id": 1})["payload"] == [1]
    assert collection.count_documents({}) == (1 if ordered else 2)
    client.close()
