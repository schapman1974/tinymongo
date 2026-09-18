"""TM-042: indexed BSON equality, standalone ranges, and complete OR plans."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from bson import Binary, Decimal128, Int64, ObjectId, Regex

from tinymongo import TinyMongoClient, table_backends


DATE = datetime(2026, 1, 1)


def oid(value):
    return ObjectId(value.to_bytes(12, "big"))


def _ids(col, query):
    return [row["_id"] for row in col.find(query)]


@pytest.mark.parametrize("backend", ["sqlite", "sqlite-sharded"])
@pytest.mark.parametrize(
    "kind, make_value, query",
    [
        ("objectid", oid, {"k": {"$eq": oid(71)}}),
        (
            "datetime",
            lambda i: DATE + timedelta(days=i),
            {"k": DATE + timedelta(days=71)},
        ),
        ("int64", Int64, {"$and": [{"k": Int64(71)}, {"n": {"$ne": 0}}]}),
        ("decimal", lambda i: Decimal128(str(i)), {"k": Decimal128("71.00")}),
        (
            "binary",
            lambda i: Binary(i.to_bytes(4, "big"), 128),
            {"k": Binary((71).to_bytes(4, "big"), 128)},
        ),
        ("uuid", lambda i: UUID(int=i), {"k": UUID(int=71)}),
        (
            "date-range",
            lambda i: DATE + timedelta(days=i),
            {
                "k": {
                    "$gte": DATE + timedelta(days=71),
                    "$lt": DATE + timedelta(days=74),
                }
            },
        ),
        ("numeric-range", int, {"k": {"$gte": 71, "$lt": 74}}),
        ("or", oid, {"$or": [{"k": oid(71)}, {"n": 73}, {"k": oid(71)}]}),
    ],
)
def test_tm042_selective_reads_decode_only_candidates(
    tmp_path, monkeypatch, backend, kind, make_value, query
):
    with TinyMongoClient(str(tmp_path), backend=backend) as client:
        col = client.app.docs
        documents = [
            {"_id": i, "k": make_value(i), "n": i, "body": "x" * 500}
            for i in range(200)
        ]
        col.insert_many(documents)
        expected = _ids(col, query)
        assert expected
        col.create_index("k")
        col.create_index("n")
        assert (
            _ids(col, query) == expected
        )  # Build derived indexes before measuring reads.
        assert (
            _ids(col, query) == expected
        )  # Reconcile the resulting schema generation.
        decoded = []
        original = table_backends._json_loads

        def load(value):
            decoded.append(value)
            return original(value)

        monkeypatch.setattr(table_backends, "_json_loads", load)
        assert _ids(col, query) == expected
        assert len(decoded) == len(expected)
        assert col.count_documents(query) == len(expected)
        assert [
            row["_id"] for row in col.find(query, {"_id": 1}).skip(1).limit(1)
        ] == expected[1:2]


def test_bson_candidates_use_native_index_and_survive_restart_and_updates(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        col = client.app.docs
        col.insert_many([{"_id": i, "k": oid(i)} for i in range(40)])
        col.create_index("k")
        assert _ids(col, {"k": oid(7)}) == [7]
        engine = col.parent.engine
        conn = engine._connect()
        try:
            sql, params = engine._sqlite_complex_candidate_query(
                conn, "docs", {"k": oid(7)}
            )
            plans = [
                row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
            ]
            assert any("SEARCH" in plan and "_bson_v1" in plan for plan in plans)
            assert not any("SCAN docs" in plan for plan in plans)
        finally:
            conn.close()
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        col = client.app.docs
        assert _ids(col, {"k": oid(7)}) == [7]
        col.update_one({"_id": 7}, {"$set": {"k": oid(8)}})
        assert _ids(col, {"k": oid(7)}) == []
        assert _ids(col, {"k": oid(8)}) == [7, 8]
        col.delete_one({"_id": 8})
        assert _ids(col, {"k": oid(8)}) == [7]
        col.drop_index("k_1")
        assert _ids(col, {"k": oid(8)}) == [7]
        conn = col.parent.engine._connect()
        try:
            assert not any(
                row[0].endswith("_bson_v1")
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            )
        finally:
            conn.close()


@pytest.mark.parametrize("dates", [False, True])
def test_range_candidates_use_native_index_searches(tmp_path, dates):
    value = (lambda i: DATE + timedelta(days=i)) if dates else int
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        col = client.app.docs
        col.insert_many([{"_id": i, "k": value(i)} for i in range(100)])
        col.create_index("k")
        query = {"k": {"$gte": value(71), "$lt": value(74)}}
        assert _ids(col, query) == [71, 72, 73]
        engine = col.parent.engine
        conn = engine._connect()
        try:
            sql, params = engine._sqlite_complex_candidate_query(conn, "docs", query)
            plans = [
                row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
            ]
            assert any(
                "SEARCH docs" in plan and "<expr>>? AND <expr><?" in plan
                for plan in plans
            )
            assert not any("SCAN docs" in plan for plan in plans)
        finally:
            conn.close()


def test_peer_index_drop_and_recreation_invalidates_bson_candidates(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        col = client.app.docs
        col.insert_many([{"_id": i, "k": oid(i)} for i in range(4)])
        col.create_index("k")
        assert _ids(col, {"k": oid(1)}) == [1]
        with TinyMongoClient(str(tmp_path), backend="sqlite") as peer:
            peer.app.docs.drop_index("k_1")
            assert _ids(col, {"k": oid(1)}) == [1]
            peer.app.docs.create_index("k")
            peer.app.docs.insert_one({"_id": 4, "k": oid(1)})
            assert _ids(col, {"k": oid(1)}) == [1, 4]


@pytest.mark.parametrize(
    "query",
    [
        {"k": {"$in": [Int64(1), Decimal128("1.0"), 1.0, True, Binary(b"a", 0)]}},
        {"k": Decimal128("0.1")},
        {"k": Binary(b"a", 0)},
        {"k": Binary(b"a", 128)},
        {"k": oid(1)},
        {"k": DATE.replace(tzinfo=timezone.utc)},
        {"k": {"$gte": DATE, "$lt": DATE + timedelta(days=1)}},
        {"k": {"$gt": 0, "$lt": 2}},
        {"k": {"$lt": 2}},
        {"k": {"$gt": 0}},
        {"k": {"$gt": 0, "$gte": 1, "$lt": 3, "$lte": 2}},
        {"k": {"$gt": -1, "$lt": 1, "$mod": [2, 0]}},
        {"k": {"$gte": 2**53 - 1}},
        {"$or": [{"k": oid(1)}, {"n": 1}, {"k": oid(1)}]},
        {"$and": [{"n": {"$ne": 4}}, {"$or": [{"k": oid(1)}, {"n": 1}]}]},
        {"$or": [{"k": oid(1)}, {"unindexed": "match"}]},
    ],
)
def test_indexed_matches_full_scan_for_adversarial_values(tmp_path, query):
    values = [
        None,
        True,
        1,
        1.0,
        Int64(1),
        Decimal128("1"),
        Decimal128("0.1"),
        0.1,
        Binary(b"a", 0),
        Binary(b"a", 128),
        oid(1),
        DATE,
        [oid(1), oid(2)],
        [DATE - timedelta(days=1), DATE + timedelta(days=2)],
        [-1, 3],
        [Decimal128("1"), 3],
        {"nested": 1},
        [],
        2**53,
        2**63 - 1,
        2**100,
        float("inf"),
        float("nan"),
    ]
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        col = client.app.docs
        col.insert_many(
            [
                {"_id": i, "k": value, "n": i % 3, "unindexed": "match"}
                for i, value in enumerate(values)
            ]
            + [{"_id": 99}]
        )
        expected = _ids(col, query)
        col.create_index("k")
        col.create_index("n")
        assert _ids(col, query) == expected
        assert col.count_documents(query) == len(expected)
        assert [
            row["_id"] for row in col.find(query, {"_id": 1}).skip(1).limit(2)
        ] == expected[1:3]


def test_date_keys_preserve_pre_epoch_and_timezone_millisecond_boundaries(tmp_path):
    before = datetime(1969, 12, 31, 23, 59, 59, 999999)
    epoch = datetime(1970, 1, 1)
    same = datetime(1970, 1, 1, 1, tzinfo=timezone(timedelta(hours=1)))
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        col = client.app.docs
        col.insert_many(
            [{"_id": 1, "k": before}, {"_id": 2, "k": epoch}, {"_id": 3, "k": same}]
        )
        col.create_index("k")
        assert _ids(col, {"k": {"$gte": epoch}}) == [2, 3]
        assert _ids(col, {"k": {"$lt": epoch}}) == [1]
        assert _ids(col, {"k": same}) == [2, 3]


def test_incomplete_or_and_unplannable_values_remain_safe(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        col = client.app.docs
        col.insert_many([{"_id": 1, "k": oid(1)}, {"_id": 2, "label": "outside"}])
        col.create_index("k")
        engine = col.parent.engine
        conn = engine._connect()
        try:
            deep_or = {"k": oid(1)}
            for _ in range(10):
                deep_or = {"$or": [deep_or]}
            for query in [
                deep_or,
                {"$or": [{"k": oid(1)}, {"label": "outside"}]},
                {"k": {"$in": [oid(1), Regex("outside")]}},
                {"k": None},
                {"$or": [{"k": oid(1)}] * 65},
            ]:
                assert (
                    engine._sqlite_complex_candidate_query(conn, "docs", query) is None
                )
        finally:
            conn.close()
        assert _ids(col, {"$or": [{"k": oid(1)}, {"label": "outside"}]}) == [1, 2]
