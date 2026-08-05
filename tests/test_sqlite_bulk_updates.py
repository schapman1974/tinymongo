import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import tinymongo as tm
import tinymongo.table_backends as table_backends

from tinymongo.errors import DuplicateKeyError, WriteError
from tinymongo.indexes import parse_index_spec
from tinymongo.table_backends import SQLiteTableBackend


class _TrackedConnection:
    def __init__(self, connection):
        self.connection = connection
        self.execute_calls = []
        self.executemany_calls = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def execute(self, sql, params=()):
        self.execute_calls.append((sql, params))
        return self.connection.execute(sql, params)

    def executemany(self, sql, params):
        rows = list(params)
        self.executemany_calls.append((sql, rows))
        return self.connection.executemany(sql, rows)

    def commit(self):
        self.commits += 1
        return self.connection.commit()

    def rollback(self):
        self.rollbacks += 1
        return self.connection.rollback()

    def close(self):
        self.closes += 1
        return self.connection.close()


def test_sqlite_bulk_update_uses_one_transaction_and_executemany(tmp_path, monkeypatch):
    backend = SQLiteTableBackend(str(tmp_path / "bulk.sqlite"))
    backend.insert_many(
        "items",
        [
            {"_id": 1, "group": "selected", "count": 1},
            {"_id": 2, "group": "selected", "count": 2},
            {"_id": 3, "group": "other", "count": 3},
        ],
    )

    # Isolate the update transaction from the existing collection/catalog
    # preflights so the test can assert its connection and commit behavior.
    monkeypatch.setattr(backend, "create_collection", lambda _collection: None)
    monkeypatch.setattr(backend, "get_index_specs", lambda _collection: [])
    tracked = _TrackedConnection(backend._connect())
    monkeypatch.setattr(backend, "_connect", lambda: tracked)

    assert backend.update_many(
        "items", {"group": "selected"}, {"$inc": {"count": 10}}
    ) == [1, 2]

    assert [sql for sql, _params in tracked.execute_calls] == [
        "BEGIN IMMEDIATE",
        'SELECT rowid, _id, data FROM "items" ORDER BY rowid',
    ]
    assert len(tracked.executemany_calls) == 1
    assert tracked.executemany_calls[0][0] == (
        'UPDATE "items" SET data = ? WHERE _id = ?'
    )
    assert len(tracked.executemany_calls[0][1]) == 2
    assert tracked.commits == 1
    assert tracked.rollbacks == 0
    assert tracked.closes == 1

    reopened = SQLiteTableBackend(backend.path)
    assert [doc["count"] for doc in reopened.find("items", {})] == [11, 12, 3]


def test_sqlite_exact_id_update_decodes_only_target_and_miss(
    tmp_path,
    monkeypatch,
):
    backend = SQLiteTableBackend(str(tmp_path / "targeted-id.sqlite"))
    backend.insert_many(
        "items",
        [{"_id": index, "count": index} for index in range(100)],
    )
    decoded_ids = []
    original_loads = table_backends._json_loads

    def tracked_loads(value):
        document = original_loads(value)
        decoded_ids.append(document["_id"])
        return document

    monkeypatch.setattr(table_backends, "_json_loads", tracked_loads)

    assert backend.update_many(
        "items",
        {"_id": 99},
        {"$inc": {"count": 1}},
        multi=False,
    ) == [99]
    assert decoded_ids == [99]

    decoded_ids.clear()
    assert (
        backend.update_many(
            "items",
            {"_id": "missing"},
            {"$set": {"seen": True}},
            multi=False,
        )
        == []
    )
    assert decoded_ids == []


def test_sqlite_indexed_update_decodes_only_bson_matching_candidates(
    tmp_path,
    monkeypatch,
):
    backend = SQLiteTableBackend(str(tmp_path / "targeted-index.sqlite"))
    backend.insert_many(
        "items",
        [
            {"_id": "array-bool", "flag": [True]},
            {"_id": "scalar-bool", "flag": True},
            {"_id": "scalar-number", "flag": 1},
            {"_id": "array-number", "flag": [1]},
            {"_id": "other", "flag": False},
        ],
    )
    backend.create_index("items", parse_index_spec("flag"))
    decoded_ids = []
    original_loads = table_backends._json_loads

    def tracked_loads(value):
        document = original_loads(value)
        decoded_ids.append(document["_id"])
        return document

    monkeypatch.setattr(table_backends, "_json_loads", tracked_loads)

    assert backend.update_many(
        "items",
        {"flag": True},
        {"$set": {"matched": True}},
    ) == ["array-bool", "scalar-bool"]
    assert decoded_ids == ["scalar-bool", "array-bool"]


def test_sqlite_indexed_update_one_keeps_natural_first_match(tmp_path):
    backend = SQLiteTableBackend(str(tmp_path / "targeted-order.sqlite"))
    backend.insert_many(
        "items",
        [
            {"_id": "array-first", "group": ["selected"], "state": "done"},
            {"_id": "scalar-second", "group": "selected", "state": "pending"},
        ],
    )
    backend.create_index("items", parse_index_spec("group"))

    assert (
        backend.update_many(
            "items",
            {"group": "selected"},
            {"$set": {"state": "done"}},
            multi=False,
        )
        == []
    )
    assert backend.find_one("items", {"_id": "scalar-second"})["state"] == "pending"


def test_sqlite_targeted_update_falls_back_for_legacy_and_unsafe_candidates(
    tmp_path,
):
    backend = SQLiteTableBackend(str(tmp_path / "targeted-fallbacks.sqlite"))
    backend.insert_many(
        "items",
        [
            {"_id": "number", "value": 1},
            {"_id": "object", "value": {"nested": 1}},
        ],
    )
    backend.create_index("items", parse_index_spec("value"))

    assert (
        backend.update_many(
            "items",
            {"_id": {"missing": True}},
            {"$set": {"seen": True}},
            multi=False,
        )
        == []
    )
    assert (
        backend.update_many(
            "items",
            {"value": float("nan")},
            {"$set": {"seen": True}},
            multi=False,
        )
        == []
    )
    assert (
        backend.update_many(
            "items",
            {"value": 10**100},
            {"$set": {"seen": True}},
            multi=False,
        )
        == []
    )
    assert backend.update_many(
        "items",
        {"value": 1},
        {"$set": {"seen": True}},
    ) == ["number"]
    assert backend.find_one("items", {"_id": "object"}).get("seen") is None


def test_sqlite_bulk_update_validates_unique_post_image_atomically(tmp_path):
    backend = SQLiteTableBackend(str(tmp_path / "unique.sqlite"))
    backend.insert_many(
        "users",
        [
            {"_id": 1, "team": "compiler", "email": "one@example.com"},
            {"_id": 2, "team": "compiler", "email": "two@example.com"},
            {"_id": 3, "team": "docs", "email": "three@example.com"},
        ],
    )
    backend.create_index("users", parse_index_spec("email", unique=True))

    with pytest.raises(DuplicateKeyError):
        backend.update_many(
            "users",
            {"team": "compiler"},
            {"$set": {"email": "shared@example.com"}},
        )

    assert [doc["email"] for doc in backend.find("users", {})] == [
        "one@example.com",
        "two@example.com",
        "three@example.com",
    ]


def test_sqlite_bulk_update_one_stops_after_first_noop(tmp_path):
    backend = SQLiteTableBackend(str(tmp_path / "single.sqlite"))
    backend.insert_many(
        "items",
        [
            {"_id": 1, "group": "selected", "state": "done"},
            {"_id": 2, "group": "selected", "state": "pending"},
        ],
    )

    assert (
        backend.update_many(
            "items",
            {"group": "selected"},
            {"$set": {"state": "done"}},
            multi=False,
        )
        == []
    )
    assert backend.find_one("items", {"_id": 2})["state"] == "pending"


def test_sqlite_bulk_update_rolls_back_when_later_document_is_invalid(tmp_path):
    backend = SQLiteTableBackend(str(tmp_path / "rollback.sqlite"))
    backend.insert_many(
        "items",
        [
            {"_id": 1, "group": "selected", "count": 1},
            {"_id": 2, "group": "selected", "count": "not-a-number"},
        ],
    )

    with pytest.raises(WriteError):
        backend.update_many("items", {"group": "selected"}, {"$inc": {"count": 1}})

    assert backend.find_one("items", {"_id": 1})["count"] == 1


def test_sqlite_bulk_update_maps_native_integrity_errors(tmp_path, monkeypatch):
    backend = SQLiteTableBackend(str(tmp_path / "integrity.sqlite"))
    backend.insert_many("items", [{"_id": 1, "value": "before"}])

    original_connect = backend._connect

    class _FailingConnection(_TrackedConnection):
        def executemany(self, sql, params):
            raise sqlite3.IntegrityError("native unique conflict")

    monkeypatch.setattr(backend, "create_collection", lambda _collection: None)
    monkeypatch.setattr(backend, "get_index_specs", lambda _collection: [])
    failing = _FailingConnection(original_connect())
    monkeypatch.setattr(backend, "_connect", lambda: failing)

    with pytest.raises(DuplicateKeyError, match="native unique conflict"):
        backend.update_many("items", {}, {"$set": {"value": "after"}})

    assert failing.commits == 0
    assert failing.rollbacks == 1


def test_sqlite_public_updates_use_single_backend_result_hook(tmp_path, monkeypatch):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.items
    collection.insert_many(
        [
            {"_id": 1, "group": "selected", "state": "done"},
            {"_id": 2, "group": "selected", "state": "pending"},
            {"_id": 3, "group": "other", "state": "pending"},
        ]
    )
    backend = collection.parent.engine
    calls = []
    original_hook = backend.update_many_with_result

    def tracked_hook(*args, **kwargs):
        calls.append((args, kwargs))
        return original_hook(*args, **kwargs)

    monkeypatch.setattr(backend, "update_many_with_result", tracked_hook)
    monkeypatch.setattr(
        backend,
        "update_many",
        lambda *args, **kwargs: pytest.fail("the legacy backend update was called"),
    )

    many = collection.update_many({"group": "selected"}, {"$set": {"state": "done"}})
    one = collection.update_one({"group": "other"}, {"$set": {"state": "finished"}})

    assert (many.matched_count, many.modified_count) == (2, 1)
    assert (one.matched_count, one.modified_count) == (1, 1)
    assert [call[1]["multi"] for call in calls] == [True, False]
    assert all(call[1]["validate_document"] is not None for call in calls)


def test_sqlite_concurrent_clients_do_not_lose_increment_updates(tmp_path):
    location = str(tmp_path / "concurrent")
    setup = tm.TinyMongoClient(location, backend="sqlite")
    setup.app.items.insert_one({"_id": "counter", "count": 0})
    setup.close()

    workers = 6
    start = threading.Barrier(workers)

    def increment(_worker):
        client = tm.TinyMongoClient(location, backend="sqlite")
        try:
            start.wait(timeout=10)
            result = client.app.items.update_one(
                {"_id": "counter"},
                {"$inc": {"count": 1}},
            )
            return result.matched_count, result.modified_count
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(increment, range(workers)))

    assert results == [(1, 1)] * workers
    reader = tm.TinyMongoClient(location, backend="sqlite")
    try:
        assert reader.app.items.find_one({"_id": "counter"})["count"] == workers
    finally:
        reader.close()


def test_sqlite_public_result_hook_preserves_upsert(tmp_path):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.items

    result = collection.update_one(
        {"_id": "new", "group": "selected"},
        {"$set": {"state": "created"}},
        upsert=True,
    )

    assert result.matched_count == 0
    assert result.modified_count == 0
    assert result.upserted_id == "new"
    assert collection.find_one({"_id": "new"}) == {
        "_id": "new",
        "group": "selected",
        "state": "created",
    }


def test_sqlite_result_hook_preserves_decimal128_quiet_nan_count(tmp_path):
    bson = pytest.importorskip("bson")
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    collection = client.app.items
    collection.insert_one({"_id": "nan", "amount": bson.Decimal128("NaN")})

    result = collection.update_one(
        {"_id": "nan"},
        {"$inc": {"amount": bson.Decimal128("0")}},
    )

    assert result.matched_count == 1
    assert result.modified_count == 1
    assert (
        collection.find_one({"_id": "nan"})["amount"].bid == bson.Decimal128("NaN").bid
    )
