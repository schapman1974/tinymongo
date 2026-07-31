from collections import UserDict
import os
import math
from uuid import uuid4

import pytest

import tinymongo as tm
from tinymongo.errors import (
    BulkWriteError,
    DuplicateKeyError,
    TinyMongoNotSupportedError,
)
from tinymongo.table_backends import _json_dumps


pytestmark = pytest.mark.integration


REMOTE_BACKENDS = [
    pytest.param("postgres", "TINYMONGO_POSTGRES_DSN", id="postgres"),
    pytest.param("mariadb", "TINYMONGO_MYSQL_DSN", id="mariadb"),
]


def _remote_target(backend, env_name):
    dsn = os.environ.get(env_name)
    if not dsn:
        pytest.skip("{0} is required".format(env_name))
    database = os.environ.get("TINYMONGO_REMOTE_SQL_DB", "tinymongoIntegration")
    prefix = "ci_{0}".format(uuid4().hex[:12])
    return dsn, database, prefix


def _create_legacy_remote_table(engine, collection):
    conn = engine._connect()
    try:
        cursor = engine._execute(
            conn,
            "CREATE TABLE {0} (_id VARCHAR(255) PRIMARY KEY, "
            "data {1} NOT NULL)".format(
                engine._quote(engine._table_name(collection)),
                engine.json_type,
            ),
        )
        engine._close_cursor(cursor)
        engine._commit(conn)
    finally:
        conn.close()


def _insert_legacy_remote_row(engine, collection, row_id, document):
    conn = engine._connect()
    try:
        cursor = engine._execute(
            conn,
            "INSERT INTO {0} (_id, data) VALUES ({1}, {2})".format(
                engine._quote(engine._table_name(collection)),
                engine.placeholder,
                engine._data_placeholder(),
            ),
            (row_id, _json_dumps(document)),
        )
        engine._close_cursor(cursor)
        engine._commit(conn)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("backend", "env_name"),
    REMOTE_BACKENDS,
)
def test_remote_sql_backend_round_trip(backend, env_name):
    dsn, database, prefix = _remote_target(backend, env_name)
    collection = prefix + "_round_trip"
    client = tm.TinyMongoClient(backend=backend, dsn=dsn)
    docs = client[database][collection]
    large_payload = bytes(range(256)) * 400
    try:
        docs.insert_many(
            [
                {
                    "_id": "one",
                    "kind": backend,
                    "score": 1,
                    "payload": large_payload,
                },
                {"_id": "two", "kind": backend, "score": 2},
            ]
        )
        docs.update_one({"_id": "one"}, {"$inc": {"score": 10}})

        assert docs.find_one({"_id": "one"})["score"] == 11
        assert docs.find_one({"payload": large_payload})["_id"] == "one"
        assert docs.find({"kind": backend}).count() == 2
        assert collection in client[database].collection_names()
        assert database in client.list_database_names()
    finally:
        docs.drop()
        client.close()


@pytest.mark.parametrize(("backend", "env_name"), REMOTE_BACKENDS)
def test_remote_sql_aggregation_core(backend, env_name):
    dsn, database, prefix = _remote_target(backend, env_name)
    collection = prefix + "_aggregation"
    client = tm.TinyMongoClient(backend=backend, dsn=dsn)
    docs = client[database][collection]
    try:
        docs.insert_many(
            [
                {"_id": 1, "team": "alpha", "score": 2},
                {"_id": 2, "team": "alpha", "score": 5},
                {"_id": 3, "team": "beta", "score": 9},
            ]
        )

        rows = docs.aggregate(
            [
                {"$match": {"score": {"$lte": 5}}},
                {
                    "$group": {
                        "_id": "$team",
                        "minimum": {"$min": "$score"},
                        "maximum": {"$max": "$score"},
                        "total": {"$sum": "$score"},
                    }
                },
            ]
        ).to_list()

        assert rows == [{"_id": "alpha", "minimum": 2, "maximum": 5, "total": 7}]
    finally:
        docs.drop()
        client.close()


@pytest.mark.parametrize(("backend", "env_name"), REMOTE_BACKENDS)
def test_remote_sql_insert_many_partial_failures(backend, env_name):
    dsn, database, prefix = _remote_target(backend, env_name)
    collection = prefix + "_insert_many"
    client = tm.TinyMongoClient(backend=backend, dsn=dsn)
    docs = client[database][collection]
    batch = [{"_id": 1}, {"_id": 1}, {"_id": 2}]
    try:
        with pytest.raises(BulkWriteError) as ordered:
            docs.insert_many([dict(document) for document in batch])
        assert ordered.value.details["nInserted"] == 1
        assert sorted(document["_id"] for document in docs.find({})) == [1]

        docs.delete_many({})

        with pytest.raises(BulkWriteError) as unordered:
            docs.insert_many(
                [dict(document) for document in batch],
                ordered=False,
            )
        assert unordered.value.details["nInserted"] == 2
        assert [
            (error["index"], error["code"])
            for error in unordered.value.details["writeErrors"]
        ] == [(1, 11000)]
        assert sorted(document["_id"] for document in docs.find({})) == [1, 2]
    finally:
        docs.drop()
        client.close()


@pytest.mark.parametrize(("backend", "env_name"), REMOTE_BACKENDS)
def test_remote_sql_typed_and_legacy_id_migration(backend, env_name):
    bson = pytest.importorskip("bson")
    dsn, database, prefix = _remote_target(backend, env_name)
    collection = prefix + "_typed_ids"
    client = tm.TinyMongoClient(backend=backend, dsn=dsn)
    docs = client[database][collection]
    generic_binary = bytes(range(16))
    custom_binary = bson.Binary(generic_binary, subtype=4)
    ordered_document_id = {"longer": 1, "a": 2}
    reordered_document_id = {"a": 2, "longer": 1}
    try:
        docs.insert_many(
            [
                {"_id": 1, "label": "number"},
                {"_id": True, "label": "boolean"},
                {"_id": generic_binary, "label": "generic-binary"},
                {"_id": custom_binary, "label": "custom-binary"},
                {"_id": ordered_document_id, "label": "ordered-document"},
                {"_id": reordered_document_id, "label": "reordered-document"},
                {"_id": float("nan"), "label": "not-a-number"},
                {"_id": float("inf"), "label": "positive-infinity"},
                {"_id": float("-inf"), "label": "negative-infinity"},
            ]
        )

        assert docs.find_one({"_id": 1})["label"] == "number"
        assert docs.find_one({"_id": True})["label"] == "boolean"
        assert (
            docs.find_one({"_id": bson.Binary(generic_binary, 0)})["label"]
            == "generic-binary"
        )
        assert docs.find_one({"_id": custom_binary})["label"] == "custom-binary"
        assert docs.find_one({"_id": ordered_document_id})["label"] == (
            "ordered-document"
        )
        assert docs.find_one({"_id": reordered_document_id})["label"] == (
            "reordered-document"
        )
        assert docs.find_one({"_id": float("nan")})["label"] == "not-a-number"
        assert docs.find_one({"_id": float("inf")})["label"] == "positive-infinity"
        assert docs.find_one({"_id": float("-inf")})["label"] == "negative-infinity"
        with pytest.raises(DuplicateKeyError):
            docs.insert_one({"_id": 1.0, "label": "equivalent-number"})
        with pytest.raises(DuplicateKeyError):
            docs.insert_one(
                {
                    "_id": UserDict([("longer", 1.0), ("a", 2.0)]),
                    "label": "equivalent-document",
                }
            )

        docs.replace_one({"_id": True}, {"label": "updated-boolean"})
        docs.delete_one({"_id": generic_binary})
        assert docs.find_one({"_id": True})["label"] == "updated-boolean"
        assert docs.find_one({"_id": 1})["label"] == "number"
        assert docs.find_one({"_id": generic_binary}) is None
        assert docs.find_one({"_id": custom_binary})["label"] == "custom-binary"
        nonfinite_ids = [
            document["_id"]
            for document in docs.find(
                {
                    "$or": [
                        {"label": "not-a-number"},
                        {"label": "positive-infinity"},
                        {"label": "negative-infinity"},
                    ]
                }
            )
        ]
        assert any(math.isnan(value) for value in nonfinite_ids)
        assert float("inf") in nonfinite_ids
        assert float("-inf") in nonfinite_ids

        # Seed one pre-v2 row to prove a real remote database can still resolve,
        # replace, and delete the old stringified physical key.
        engine = docs.parent.engine
        _insert_legacy_remote_row(
            engine,
            collection,
            "7",
            {"_id": 7, "label": "legacy"},
        )

        assert docs.find_one({"_id": 7.0})["label"] == "legacy"
        docs.replace_one({"_id": 7.0}, {"label": "legacy-updated"})
        assert docs.find_one({"_id": 7})["label"] == "legacy-updated"
        docs.delete_one({"_id": 7})
        assert docs.find_one({"_id": 7.0}) is None
    finally:
        docs.drop()
        client.close()


@pytest.mark.parametrize(("backend", "env_name"), REMOTE_BACKENDS)
def test_remote_sql_upgrades_old_schema_and_recovers_ordered_legacy_id(
    backend,
    env_name,
):
    dsn, database, prefix = _remote_target(backend, env_name)
    collection = prefix + "_old_schema"
    client = tm.TinyMongoClient(backend=backend, dsn=dsn)
    docs = client[database][collection]
    engine = docs.parent.engine
    ordered_id = {"longer": 1, "a": 2}
    database_reordered_id = {"a": 2, "longer": 1}

    try:
        _create_legacy_remote_table(engine, collection)
        _insert_legacy_remote_row(
            engine,
            collection,
            str(ordered_id),
            {"_id": database_reordered_id, "label": "legacy"},
        )

        assert docs.find_one({"_id": ordered_id}) == {
            "_id": ordered_id,
            "label": "legacy",
        }
        assert docs.find_one({"label": "legacy"})["_id"] == ordered_id
        assert docs.find_one({"_id": database_reordered_id}) is None

        conn = engine._connect()
        try:
            cursor = engine._execute(
                conn,
                "SELECT data_ordered FROM {0}".format(
                    engine._quote(engine._table_name(collection))
                ),
            )
            try:
                assert list(cursor.fetchall()) == [(None,)]
            finally:
                engine._close_cursor(cursor)
        finally:
            conn.close()

        docs.replace_one({"_id": ordered_id}, {"label": "updated"})
        docs.insert_one({"_id": "fresh", "label": "new-schema"})
        assert docs.find_one({"_id": ordered_id})["label"] == "updated"
        assert docs.find_one({"_id": "fresh"})["label"] == "new-schema"

        docs.delete_one({"_id": ordered_id})
        assert docs.find_one({"_id": ordered_id}) is None
    finally:
        docs.drop()
        client.close()


@pytest.mark.parametrize(("backend", "env_name"), REMOTE_BACKENDS)
def test_remote_sql_native_index_catalog_and_unique_enforcement(backend, env_name):
    dsn, database, prefix = _remote_target(backend, env_name)
    scalar_name = prefix + "_scalar"
    multikey_name = prefix + "_multikey"
    client = tm.TinyMongoClient(backend=backend, dsn=dsn)
    scalar = client[database][scalar_name]
    multikey = client[database][multikey_name]

    try:
        assert (
            scalar.create_index("email", name="email_unique", unique=True)
            == "email_unique"
        )
        assert scalar.index_information() == {
            "_id_": {"key": [("_id", 1)]},
            "email_unique": {"key": [("email", 1)], "unique": True},
        }

        # Simulate a 1.2.0 table after its native index already exists. The
        # 1.2.1 sidecar upgrade must leave that index usable by later writes.
        engine = scalar.parent.engine
        conn = engine._connect()
        try:
            cursor = engine._execute(
                conn,
                "ALTER TABLE {0} DROP COLUMN data_ordered".format(
                    engine._quote(engine._table_name(scalar_name))
                ),
            )
            engine._close_cursor(cursor)
            engine._commit(conn)
            engine._ordered_data_collections.discard(scalar_name)
        finally:
            conn.close()

        scalar.insert_many(
            [
                {"_id": 1, "email": "ada@example.com"},
                {"_id": 2, "email": "grace@example.com"},
            ]
        )
        with pytest.raises(DuplicateKeyError):
            scalar.insert_one({"_id": 3, "email": "ada@example.com"})
        assert scalar.count_documents({}) == 2

        # A separate client proves the catalog is persisted remotely rather than
        # retained only on the collection handle that created the index.
        reader = tm.TinyMongoClient(backend=backend, dsn=dsn)
        try:
            assert reader[database][scalar_name].index_information()[
                "email_unique"
            ] == {"key": [("email", 1)], "unique": True}
        finally:
            reader.close()

        scalar.drop_index("email_unique")
        assert scalar.index_information() == {"_id_": {"key": [("_id", 1)]}}
        scalar.insert_one({"_id": 3, "email": "ada@example.com"})

        multikey.create_index("tags", name="tags_unique", unique=True)
        multikey.insert_one({"_id": 1, "tags": "beta"})
        with pytest.raises(TinyMongoNotSupportedError, match="multikey uniqueness"):
            multikey.insert_one({"_id": 2, "tags": ["beta", "gamma"]})
        assert multikey.count_documents({}) == 1
    finally:
        multikey.drop()
        scalar.drop()
        client.close()
