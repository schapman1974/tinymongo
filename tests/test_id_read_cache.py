"""Exact ID reads avoid resident scans without changing BSON equality."""

from uuid import uuid4

import pytest

from tinymongo import TinyMongoClient
from tinymongo.storage_backends import AtomicJSONStorage, clear_memory_namespace


@pytest.mark.parametrize("backend", ["json", "memory"])
def test_warm_id_reads_share_cache_and_isolate_results(tmp_path, monkeypatch, backend):
    with TinyMongoClient(str(tmp_path), backend=backend) as client:
        items = client.app.items
        items.insert_many([{"_id": i, "nested": [i]} for i in range(40)])
        assert items.find_one({"_id": 3})["nested"] == [3]

        def no_scan():
            pytest.fail("warm exact ID read scanned resident documents")

        monkeypatch.setattr(items.table, "all", no_scan)
        found = client.app.items.find_one({"_id": {"$eq": 3.0}})
        found["nested"].append("caller mutation")
        assert items.find_one({"_id": 3})["nested"] == [3]
        assert client.app.items.find_one({"_id": 4}, {"nested": 1, "_id": 0}) == {
            "nested": [4]
        }
        assert items.find_one({"_id": 99}) is None
        assert list(items.find({"_id": 3}, skip=1)) == []


@pytest.mark.parametrize("backend", ["json", "memory"])
def test_id_cache_tracks_local_and_peer_writes(tmp_path, backend):
    address = (
        "memory://id-cache-" + uuid4().hex if backend == "memory" else str(tmp_path)
    )
    try:
        with (
            TinyMongoClient(address, backend=backend) as first,
            TinyMongoClient(address, backend=backend) as peer,
        ):
            items = first.app.items
            items.insert_one({"_id": 1, "value": "before"})
            assert items.find_one({"_id": 1})["value"] == "before"
            peer.app.items.update_one({"_id": 1}, {"$set": {"value": "after"}})
            assert items.find_one({"_id": 1})["value"] == "after"
            items.insert_one({"_id": 2})
            assert first.app.items.find_one({"_id": 2}) == {"_id": 2}
            peer.app.items.delete_one({"_id": 1})
            assert items.find_one({"_id": 1}) is None
            peer.app.items.drop()
            peer.app.items.insert_one({"_id": 3})
            assert items.find_one({"_id": 2}) is None
            assert first.app.items.find_one({"_id": 3}) == {"_id": 3}
    finally:
        if backend == "memory":
            clear_memory_namespace(address)


@pytest.mark.parametrize("backend", ["json", "memory"])
def test_legacy_exact_ids_keep_types_arrays_field_order_and_duplicates(
    tmp_path, monkeypatch, backend
):
    from bson.binary import Binary

    with TinyMongoClient(str(tmp_path), backend=backend) as client:
        items = client.app.items
        items.insert_one({"_id": "seed"})
        ids = [
            True,
            1,
            [1, 2],
            {"a": 1, "b": 2},
            {"b": 2, "a": 1},
            Binary(b"x", 0),
            Binary(b"x", 1),
            None,
            1.0,
        ]
        documents = [{"_id": value, "position": i} for i, value in enumerate(ids)]
        # Model a legacy table, including duplicate numeric identities.
        monkeypatch.setattr(type(items.table), "all", lambda self: documents)
        for value, expected in [
            (True, [0]),
            (1, [1, 8]),
            ([1, 2], [2]),
            ({"a": 1, "b": 2}, [3]),
            ({"b": 2, "a": 1}, [4]),
            (b"x", [5]),
            (Binary(b"x", 1), [6]),
            (None, [7]),
        ]:
            assert [d["position"] for d in items.find({"_id": value})] == expected
        assert [
            d["position"]
            for d in items.find({"_id": 1}, sort=[("position", -1)], limit=1)
        ] == [8]


def test_id_cache_conservative_fallbacks(tmp_path, monkeypatch):
    legacy = object()

    with TinyMongoClient(str(tmp_path), backend="json") as client:
        items = client.app.items
        items.insert_one({"_id": "seed"})
        documents = [{"_id": legacy}, {"_id": 2}]
        monkeypatch.setattr(type(items.table), "all", lambda self: documents)
        assert items._id_read_candidates(1) == documents
        assert items._id_read_candidates(2) == documents
        assert items._id_read_candidates(legacy) == documents
        monkeypatch.setattr(AtomicJSONStorage, "revision", property(lambda self: None))
        assert items._id_read_candidates(2) == documents


def test_id_cache_rebuilds_on_revision_change(tmp_path, monkeypatch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        items = client.app.items
        items.insert_one({"_id": 1})
        assert items._id_read_candidates(1) == [{"_id": 1}]
        monkeypatch.setattr(items.parent, "_current_memory_revision", lambda: "changed")
        monkeypatch.setattr(items.table, "all", lambda: [{"_id": 2}])
        assert items._id_read_candidates(1) == []
        assert items._id_read_candidates(2) == [{"_id": 2}]
