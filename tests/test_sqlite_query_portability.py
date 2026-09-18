"""TM-053: BSON read acceleration must preserve ordinary SQLite access."""

import sqlite3
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from tinymongo import TinyMongoClient, table_backends
from tinymongo.indexes import parse_index_spec


DATE = datetime(2026, 1, 1)
OID = ObjectId("000000000000000000000001")


def _ids(collection, query):
    return [document["_id"] for document in collection.find(query)]


def _raw_documents(connection):
    return [
        table_backends._json_loads(row[0])
        for row in connection.execute('SELECT data FROM "docs" ORDER BY rowid')
    ]


def _raw_update(connection, document):
    connection.execute(
        "UPDATE \"docs\" SET data = ? WHERE json_extract(data, '$._id') = ?",
        (table_backends._json_dumps(document), document["_id"]),
    )


def _assert_matches(collection, query, expected):
    assert _ids(collection, query) == expected
    assert collection.count_documents(query) == len(expected)
    assert [
        document["_id"]
        for document in collection.find(query, {"_id": 1}).skip(1).limit(1)
    ] == expected[1:2]


@pytest.mark.parametrize("value", [DATE, OID], ids=["datetime", "objectid"])
def test_bson_read_allows_plain_sqlite_maintenance_and_backup(tmp_path, value):
    documents = [{"_id": 1, "k": value}, {"_id": 2, "k": [value]}, {"_id": 3}]
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        collection = client.app.docs
        collection.insert_many(documents)
        collection.create_index("k")
        _assert_matches(collection, {"k": value}, [1, 2])
        with sqlite3.connect(collection.parent.engine.path) as connection:
            connection.execute("PRAGMA recursive_triggers = ON")
            connection.execute("VACUUM")
            connection.execute("REINDEX")
            connection.execute('UPDATE "docs" SET data = data')
            connection.commit()
            assert _raw_documents(connection) == documents
            schema = "\n".join(
                sql or ""
                for (sql,) in connection.execute("SELECT sql FROM sqlite_master")
            )
            assert "tinymongo_bson_query_key_v1" not in schema

            with sqlite3.connect(str(tmp_path / "backup.sqlite")) as backup:
                connection.backup(backup)
                assert _raw_documents(backup) == documents
                backup.execute("REINDEX")
                backup.execute('UPDATE "docs" SET data = data')

            dump = "\n".join(connection.iterdump())
            with sqlite3.connect(":memory:") as restored:
                restored.executescript(dump)
                assert _raw_documents(restored) == documents
                restored.execute("REINDEX")
                restored.execute('UPDATE "docs" SET data = data')
                restored.commit()
                restored.execute("VACUUM")
        _assert_matches(collection, {"k": value}, [1, 2])


@pytest.mark.parametrize(
    "value, other",
    [(DATE, DATE + timedelta(days=5)), (OID, ObjectId("000000000000000000000002"))],
    ids=["datetime", "objectid"],
)
@pytest.mark.parametrize("recursive_triggers", [False, True])
def test_plain_writers_invalidate_bson_candidates_without_losing_or_repeating_rows(
    tmp_path, value, other, recursive_triggers
):
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        collection = client.app.docs
        collection.insert_many(
            [
                {"_id": 1, "k": value},
                {"_id": 2, "k": [value, value]},
                {"_id": 3},
                {"_id": 4, "k": other},
            ]
        )
        collection.create_index("k")
        queries = [{"k": value}, {"k": {"$in": [value, value]}}]
        if isinstance(value, datetime):
            queries.append({"k": {"$gte": value, "$lt": value + timedelta(days=1)}})
        for query in queries:
            _assert_matches(collection, query, [1, 2])

        with sqlite3.connect(collection.parent.engine.path) as connection:
            connection.execute(
                "PRAGMA recursive_triggers = " + ("ON" if recursive_triggers else "OFF")
            )
            _raw_update(connection, {"_id": 1, "k": other})
            _raw_update(connection, {"_id": 2})
            _raw_update(connection, {"_id": 3, "k": value})
            connection.execute(
                'INSERT OR REPLACE INTO "docs" (_id, data) VALUES (?, ?)',
                (
                    table_backends._physical_id_key(4),
                    table_backends._json_dumps({"_id": 4, "k": [value, value]}),
                ),
            )
        for query in queries:
            _assert_matches(collection, query, [3, 4])

        with sqlite3.connect(collection.parent.engine.path) as connection:
            _raw_update(connection, {"_id": 1, "k": [value, value]})
            _raw_update(connection, {"_id": 3, "k": []})
            _raw_update(connection, {"_id": 4, "k": value})
        for query in queries:
            _assert_matches(collection, query, [1, 4])


def test_recreated_index_name_cannot_reuse_keys_from_another_field(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        collection = client.app.docs
        collection.insert_many(
            [
                {"_id": 1, "first": OID, "second": DATE},
                {"_id": 2, "first": DATE, "second": OID},
            ]
        )
        collection.create_index("first", name="lookup")
        _assert_matches(collection, {"first": OID}, [1])
        collection.drop_index("lookup")
        collection.create_index("second", name="lookup")
        _assert_matches(collection, {"second": OID}, [2])
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        _assert_matches(client.app.docs, {"second": OID}, [2])
        with sqlite3.connect(client.app.docs.parent.engine.path) as connection:
            _raw_update(connection, {"_id": 1, "second": OID})
            _raw_update(connection, {"_id": 2})
        _assert_matches(client.app.docs, {"second": OID}, [1])


@pytest.mark.parametrize("use_range", [False, True])
def test_compiled_query_survives_peer_reusing_index_name(
    tmp_path, monkeypatch, use_range
):
    other = DATE + timedelta(days=5)
    query = (
        {"first": {"$gte": DATE, "$lt": DATE + timedelta(days=1)}}
        if use_range
        else {"first": DATE}
    )
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        collection = client.app.docs
        collection.insert_many(
            [
                {"_id": 1, "first": DATE, "second": other},
                {"_id": 2, "first": other, "second": DATE},
                {"_id": 3, "first": [DATE], "second": other},
            ]
        )
        collection.create_index("first", name="lookup")
        _assert_matches(collection, query, [1, 3])
        backend = collection.parent.engine
        original_query = backend._sqlite_complex_candidate_query
        injected = []

        def compile_then_replace_index(connection, collection_name, filter_doc):
            candidate = original_query(connection, collection_name, filter_doc)
            if not injected:
                assert candidate is not None
                with TinyMongoClient(str(tmp_path), backend="sqlite") as peer:
                    peer.app.docs.drop_index("lookup")
                    peer.app.docs.create_index("second", name="lookup")
                    _assert_matches(peer.app.docs, {"second": DATE}, [2])
                injected.append(True)
            return candidate

        monkeypatch.setattr(
            backend, "_sqlite_complex_candidate_query", compile_then_replace_index
        )
        _assert_matches(collection, query, [1, 3])
        assert injected == [True]


@pytest.mark.parametrize("use_range", [False, True])
def test_retired_key_storage_is_not_refreshed_without_invalidation_trigger(
    tmp_path, monkeypatch, use_range
):
    other = DATE + timedelta(days=5)
    query = (
        {"first": {"$gte": DATE, "$lt": DATE + timedelta(days=1)}}
        if use_range
        else {"first": DATE}
    )
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        collection = client.app.docs
        collection.insert_many(
            [
                {"_id": 1, "first": DATE, "second": other},
                {"_id": 2, "first": other, "second": DATE},
                {"_id": 3, "first": [DATE], "second": other},
            ]
        )
        collection.create_index("first", name="lookup")
        _assert_matches(collection, query, [1, 3])
        backend = collection.parent.engine
        original_refresh = backend._refresh_bson_query_keys
        injected = []

        def replace_index_then_refresh(connection, collection_name, spec):
            if injected:
                return original_refresh(connection, collection_name, spec)
            with TinyMongoClient(str(tmp_path), backend="sqlite") as peer:
                peer.app.docs.drop_index("lookup")
                peer.app.docs.create_index("second", name="lookup")
                _assert_matches(peer.app.docs, {"second": DATE}, [2])
            original_refresh(connection, collection_name, spec)
            # A stale reader must not fill retired keys: this plain writer no
            # longer has an invalidation trigger for the first field's cache.
            with sqlite3.connect(backend.path) as writer:
                _raw_update(writer, {"_id": 1, "first": other, "second": other})
                _raw_update(writer, {"_id": 2, "first": DATE, "second": DATE})
                _raw_update(writer, {"_id": 3, "first": [], "second": other})
            injected.append(True)

        monkeypatch.setattr(
            backend, "_refresh_bson_query_keys", replace_index_then_refresh
        )
        _assert_matches(collection, query, [2])
        assert injected == [True]


@pytest.mark.parametrize("use_range", [False, True])
def test_writer_after_key_refresh_is_included_once(tmp_path, monkeypatch, use_range):
    other = DATE + timedelta(days=5)
    query = (
        {"k": {"$gte": DATE, "$lt": DATE + timedelta(days=1)}}
        if use_range
        else {"k": DATE}
    )
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        collection = client.app.docs
        collection.insert_many(
            [
                {"_id": 1, "k": DATE},
                {"_id": 2, "k": other},
                {"_id": 3},
                {"_id": 4, "k": [DATE]},
                {"_id": 5, "k": other},
            ]
        )
        collection.create_index("k")
        _assert_matches(collection, query, [1, 4])
        backend = collection.parent.engine
        original_refresh = backend._refresh_bson_query_keys
        injected = []

        def refresh_then_write(connection, collection_name, spec):
            original_refresh(connection, collection_name, spec)
            if injected:
                return
            with sqlite3.connect(backend.path) as writer:
                _raw_update(writer, {"_id": 1, "k": other})
                _raw_update(writer, {"_id": 2, "k": DATE})
                _raw_update(writer, {"_id": 3, "k": [DATE, DATE]})
                _raw_update(writer, {"_id": 4})
                _raw_update(writer, {"_id": 5, "k": [DATE]})
                writer.execute(
                    'INSERT INTO "docs" (_id, data) VALUES (?, ?)',
                    (
                        table_backends._physical_id_key(6),
                        table_backends._json_dumps({"_id": 6, "k": DATE}),
                    ),
                )
            injected.append(True)

        monkeypatch.setattr(backend, "_refresh_bson_query_keys", refresh_then_write)
        _assert_matches(collection, query, [2, 3, 5, 6])
        assert injected == [True]


@pytest.mark.parametrize("reopen", [False, True])
def test_missing_invalidation_triggers_rebuild_stale_keys(tmp_path, reopen):
    client = TinyMongoClient(str(tmp_path), backend="sqlite")
    try:
        collection = client.app.docs
        collection.insert_many(
            [{"_id": 1, "k": DATE}, {"_id": 2, "k": DATE + timedelta(days=5)}]
        )
        collection.create_index("k")
        _assert_matches(collection, {"k": DATE}, [1])
        with sqlite3.connect(collection.parent.engine.path) as connection:
            triggers = [
                name
                for (name,) in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND tbl_name = 'docs'"
                )
            ]
            assert triggers
            for name in triggers:
                connection.execute(
                    "DROP TRIGGER " + table_backends._quote_identifier(name)
                )
            _raw_update(connection, {"_id": 1, "k": DATE + timedelta(days=5)})
            _raw_update(connection, {"_id": 2, "k": DATE})
        if reopen:
            client.close()
            client = TinyMongoClient(str(tmp_path), backend="sqlite")
            collection = client.app.docs
        _assert_matches(collection, {"k": DATE}, [2])
        _assert_matches(
            collection, {"k": {"$gte": DATE, "$lt": DATE + timedelta(days=1)}}, [2]
        )
    finally:
        client.close()


def test_opening_legacy_bson_index_restores_sqlite_portability(tmp_path):
    documents = [{"_id": 1, "k": OID}, {"_id": 2, "k": [OID]}, {"_id": 3}]
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        collection = client.app.docs
        collection.insert_many(documents)
        collection.create_index("k")
        backend = collection.parent.engine
        path = backend.path
        physical_name = backend._physical_index_name("docs", parse_index_spec("k"))
    with sqlite3.connect(path) as legacy:
        legacy.create_function(
            "tinymongo_bson_query_key_v1",
            2,
            table_backends._sqlite_bson_query_key_from_row,
            deterministic=True,
        )
        legacy.execute(
            "CREATE INDEX {0} ON docs (tinymongo_bson_query_key_v1(data, 'k'))".format(
                table_backends._quote_identifier(physical_name + "_bson_v1")
            )
        )
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE docs SET data = data")

    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        assert list(client.app.docs.find({})) == documents
        with sqlite3.connect(path) as connection:
            assert (
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = ?",
                    (physical_name + "_bson_v1",),
                ).fetchall()
                == []
            )
            connection.execute("VACUUM")
            connection.execute("REINDEX")
            connection.execute("UPDATE docs SET data = data")
            assert _raw_documents(connection) == documents
        _assert_matches(client.app.docs, {"k": OID}, [1, 2])
