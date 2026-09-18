"""Atomic recovery when portable query-key setup or materialization fails."""

import sqlite3

import pytest
from bson import ObjectId

from tinymongo import TinyMongoClient, table_backends


def test_query_key_schema_setup_rolls_back_on_trigger_failure(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        col = client.app.docs
        value = ObjectId()
        col.insert_one({"_id": 1, "k": value})
        col.create_index("k")
        engine = col.parent.engine
        spec = engine.get_index_specs("docs")[0]
        conn = engine._connect()
        conn.set_authorizer(
            lambda action, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_CREATE_TRIGGER
                else sqlite3.SQLITE_OK
            )
        )
        try:
            with pytest.raises(sqlite3.DatabaseError, match="authorized"):
                engine._ensure_bson_query_index(conn, "docs", spec)
            assert not conn.in_transaction
            assert ("docs", spec.name) not in engine._ready_bson_query_indexes
            assert [row[1] for row in conn.execute('PRAGMA table_info("docs")')] == [
                "_id",
                "data",
            ]
        finally:
            conn.close()
        assert [doc["_id"] for doc in col.find({"k": value})] == [1]


def test_query_key_refresh_rolls_back_partial_updates(tmp_path, monkeypatch):
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        col = client.app.docs
        values = [ObjectId(), ObjectId()]
        col.insert_many([{"_id": i, "k": value} for i, value in enumerate(values)])
        col.create_index("k")
        original = table_backends._sqlite_bson_query_key_from_row
        calls = []

        def interrupted(data, field):
            calls.append(data)
            if len(calls) == 2:
                raise ValueError("interrupted key materialization")
            return original(data, field)

        monkeypatch.setattr(
            table_backends, "_sqlite_bson_query_key_from_row", interrupted
        )
        with pytest.raises(ValueError, match="interrupted key"):
            list(col.find({"k": values[0]}))
        engine = col.parent.engine
        spec = engine.get_index_specs("docs")[0]
        key = engine._sqlite_bson_query_expression("docs", spec)
        with sqlite3.connect(engine.path) as conn:
            assert conn.execute("SELECT " + key + " FROM docs").fetchall() == [
                (None,),
                (None,),
            ]
        monkeypatch.setattr(table_backends, "_sqlite_bson_query_key_from_row", original)
        assert [doc["_id"] for doc in col.find({"k": values[0]})] == [0]
        assert [doc["_id"] for doc in col.find({"k": values[1]})] == [1]


def test_legacy_cleanup_preserves_unowned_callback_index(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        col = client.app.docs
        col.insert_one({"_id": 1, "k": ObjectId()})
        engine = col.parent.engine
        conn = engine._connect()
        try:
            conn.create_function(
                "tinymongo_bson_query_key_v1",
                2,
                table_backends._sqlite_bson_query_key_from_row,
                deterministic=True,
            )
            conn.execute(
                "CREATE INDEX external_custom_bson_v1 ON docs "
                "(tinymongo_bson_query_key_v1(data, 'k'))"
            )
            engine._remove_legacy_bson_query_indexes(conn)
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE name='external_custom_bson_v1'"
            ).fetchone() == ("external_custom_bson_v1",)
        finally:
            conn.close()
