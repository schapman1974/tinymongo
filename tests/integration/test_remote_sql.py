import asyncio
from collections import UserDict
import os
import math
import re
from uuid import UUID, uuid4

import pytest

import tinymongo as tm
from tinymongo.asyncio import AsyncTinyMongoClient
from tinymongo.errors import (
    BulkWriteError,
    DuplicateKeyError,
    TinyMongoNotSupportedError,
    WriteError,
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
def test_remote_sql_query_operators_and_write_error_codes(backend, env_name):
    dsn, database, prefix = _remote_target(backend, env_name)
    client = tm.TinyMongoClient(backend=backend, dsn=dsn)
    docs = client[database][prefix + "_query_operators"]
    try:
        docs.insert_many(
            [
                {"_id": "one", "values": [2, 7], "results": [{"score": 8}]},
                {"_id": "two", "values": [1, 4, 9], "results": [{"score": 3}]},
                {"_id": "scalar", "values": "not-an-array"},
            ]
        )

        assert docs.find_one({"values": {"$size": 2}})["_id"] == "one"
        assert docs.find_one({"results": {"$elemMatch": {"score": 8}}})["_id"] == "one"
        assert {item["_id"] for item in docs.find({"values": {"$type": "int"}})} == {
            "one",
            "two",
        }
        assert {item["_id"] for item in docs.find({"values": {"$mod": [2, 0]}})} == {
            "one",
            "two",
        }

        with pytest.raises(TinyMongoNotSupportedError, match=r"\$exsits"):
            docs.find({"values": {"$exsits": True}})
        with pytest.raises(DuplicateKeyError) as duplicate:
            docs.insert_one({"_id": "one"})
        assert duplicate.value.code == 11000
        with pytest.raises(WriteError) as write_error:
            docs.update_one(
                {"_id": "scalar"},
                {"$addToSet": {"values": "new"}},
            )
        assert write_error.value.code == 2
    finally:
        docs.drop()
        client.close()


@pytest.mark.parametrize(("backend", "env_name"), REMOTE_BACKENDS)
def test_remote_sql_async_query_operators(backend, env_name):
    dsn, database, prefix = _remote_target(backend, env_name)

    async def scenario():
        client = AsyncTinyMongoClient(backend=backend, dsn=dsn)
        docs = client[database][prefix + "_async_query_operators"]
        try:
            await docs.insert_many(
                [
                    {"_id": "one", "values": [2, 7], "results": [{"score": 8}]},
                    {"_id": "two", "values": [1, 4, 9]},
                ]
            )
            size_rows = await docs.find({"values": {"$size": 2}}).to_list()
            elem_rows = await docs.find(
                {"results": {"$elemMatch": {"score": 8}}}
            ).to_list()
            type_rows = await docs.find({"values": {"$type": "int"}}).to_list()
            mod_rows = await docs.find({"values": {"$mod": [2, 0]}}).to_list()

            assert [item["_id"] for item in size_rows] == ["one"]
            assert [item["_id"] for item in elem_rows] == ["one"]
            assert {item["_id"] for item in type_rows} == {"one", "two"}
            assert {item["_id"] for item in mod_rows} == {"one", "two"}
        finally:
            await docs.drop()
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(("backend", "env_name"), REMOTE_BACKENDS)
def test_remote_sql_uuid_regex_roundtrip_query_and_unique_fail_closed(
    backend, env_name
):
    bson = pytest.importorskip("bson")
    dsn, database, prefix = _remote_target(backend, env_name)
    client = tm.TinyMongoClient(backend=backend, dsn=dsn)
    docs = client[database][prefix + "_bson_values"]
    protected = client[database][prefix + "_bson_unique"]
    value = UUID("00112233-4455-6677-8899-aabbccddeeff")
    native = re.compile("remote", re.IGNORECASE)
    try:
        docs.insert_many(
            [
                {"_id": "uuid", "value": value},
                {"_id": "native", "value": native},
                {"_id": "bson", "value": bson.Regex("remote", "i")},
                {"_id": "text", "value": "REMOTE"},
            ]
        )

        assert docs.find_one({"value": bson.Binary(value.bytes, 4)})["_id"] == "uuid"
        assert {item["_id"] for item in docs.find({"value": native})} == {
            "native",
            "text",
        }
        assert type(docs.find_one({"_id": "uuid"})["value"]) is UUID
        assert isinstance(docs.find_one({"_id": "native"})["value"], type(native))
        assert isinstance(docs.find_one({"_id": "bson"})["value"], bson.Regex)

        protected.create_index("value", unique=True)
        for extended in (value, native):
            with pytest.raises(TinyMongoNotSupportedError, match="Remote SQL"):
                protected.insert_one({"value": extended})
        assert protected.count_documents({}) == 0
    finally:
        docs.drop()
        protected.drop()
        client.close()


@pytest.mark.parametrize(("backend", "env_name"), REMOTE_BACKENDS)
def test_remote_sql_async_uuid_regex_roundtrip_and_query(backend, env_name):
    bson = pytest.importorskip("bson")
    dsn, database, prefix = _remote_target(backend, env_name)
    collection = prefix + "_async_bson_values"
    value = UUID("00112233-4455-6677-8899-aabbccddeeff")
    native = re.compile("remote", re.IGNORECASE)

    async def scenario():
        client = AsyncTinyMongoClient(backend=backend, dsn=dsn)
        docs = client[database][collection]
        try:
            await docs.insert_many(
                [
                    {"_id": "uuid", "value": value},
                    {"_id": "native", "value": native},
                    {"_id": "text", "value": "REMOTE"},
                ]
            )
            assert (await docs.find_one({"value": bson.Binary(value.bytes, 4)}))[
                "_id"
            ] == "uuid"
            rows = await docs.find({"value": native}).to_list()
            assert {item["_id"] for item in rows} == {"native", "text"}
            restored = await docs.find_one({"_id": "native"})
            assert isinstance(restored["value"], type(native))
        finally:
            await docs.drop()
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(("backend", "env_name"), REMOTE_BACKENDS)
def test_remote_sql_aggregation_core(backend, env_name):
    dsn, database, prefix = _remote_target(backend, env_name)
    collection = prefix + "_aggregation"
    client = tm.TinyMongoClient(backend=backend, dsn=dsn)
    docs = client[database][collection]
    try:
        docs.insert_many(
            [
                {"_id": 1, "team": "alpha", "score": 2, "items": [1, 2]},
                {"_id": 2, "team": "alpha", "score": 5, "items": None},
                {"_id": 3, "team": "beta", "score": 9},
            ]
        )

        rows = docs.aggregate(
            [
                {"$match": {"score": {"$lte": 5}}},
                {
                    "$set": {
                        "item_count": {"$size": {"$ifNull": ["$items", []]}},
                        "temporary": {"$literal": "$score"},
                    }
                },
                {"$unset": ["items", "temporary"]},
                {
                    "$project": {
                        "team": 1,
                        "score": 1,
                        "item_count": 1,
                    }
                },
                {"$sort": {"_id": 1}},
                {
                    "$group": {
                        "_id": "$team",
                        "average": {"$avg": "$score"},
                        "first": {"$first": "$score"},
                        "last": {"$last": "$score"},
                        "minimum": {"$min": "$score"},
                        "maximum": {"$max": "$score"},
                        "pushed": {"$push": "$score"},
                        "unique": {"$addToSet": "$score"},
                        "total": {"$sum": "$score"},
                        "items": {"$sum": "$item_count"},
                    }
                },
            ]
        ).to_list()

        unique = rows[0].pop("unique")
        assert set(unique) == {2, 5}
        assert rows == [
            {
                "_id": "alpha",
                "average": 3.5,
                "first": 2,
                "last": 5,
                "minimum": 2,
                "maximum": 5,
                "pushed": [2, 5],
                "total": 7,
                "items": 2,
            }
        ]

        assert docs.aggregate(
            [
                {"$sort": {"score": -1, "_id": 1}},
                {"$skip": 1},
                {"$limit": 1},
                {"$project": {"_id": 1, "score": 1}},
            ]
        ).to_list() == [{"_id": 2, "score": 5}]
        assert docs.aggregate(
            [
                {"$match": {"team": "alpha"}},
                {"$count": "matched"},
            ]
        ).to_list() == [{"matched": 2}]
    finally:
        docs.drop()
        client.close()


@pytest.mark.parametrize(("backend", "env_name"), REMOTE_BACKENDS)
def test_remote_sql_async_aggregation_projection(backend, env_name):
    dsn, database, prefix = _remote_target(backend, env_name)
    collection = prefix + "_async_aggregation"

    async def scenario():
        client = AsyncTinyMongoClient(backend=backend, dsn=dsn)
        docs = client[database][collection]
        try:
            await docs.insert_many(
                [
                    {"_id": 1, "course_id": "python", "lectures": [1, 2]},
                    {"_id": 2, "course_id": "python", "lectures": None},
                    {"_id": 3, "course_id": "python"},
                    {"_id": 4, "course_id": "excluded", "lectures": [1, 2, 3]},
                ]
            )
            cursor = await docs.aggregate(
                [
                    {"$match": {"course_id": {"$in": ["python"]}}},
                    {
                        "$addFields": {
                            "count": {"$size": {"$ifNull": ["$lectures", []]}},
                            "literal": {"$literal": "$course_id"},
                        }
                    },
                    {"$unset": ["lectures", "literal"]},
                    {
                        "$project": {
                            "course_id": 1,
                            "count": 1,
                        }
                    },
                    {"$sort": {"_id": 1}},
                    {
                        "$group": {
                            "_id": "$course_id",
                            "average": {"$avg": "$count"},
                            "first": {"$first": "$count"},
                            "last": {"$last": "$count"},
                            "pushed": {"$push": "$count"},
                            "unique": {"$addToSet": "$count"},
                            "total": {"$sum": "$count"},
                        }
                    },
                ]
            )
            rows = await cursor.to_list()
            unique = rows[0].pop("unique")
            assert set(unique) == {0, 2}
            assert rows == [
                {
                    "_id": "python",
                    "average": 2.0 / 3.0,
                    "first": 2,
                    "last": 0,
                    "pushed": [2, 0, 0],
                    "total": 2,
                }
            ]

            page = await docs.aggregate(
                [
                    {"$sort": {"_id": -1}},
                    {"$skip": 1},
                    {"$limit": 2},
                    {"$project": {"_id": 1}},
                ]
            )
            assert await page.to_list() == [{"_id": 3}, {"_id": 2}]

            count = await docs.aggregate(
                [
                    {"$limit": 3},
                    {"$count": "selected"},
                ]
            )
            assert await count.to_list() == [{"selected": 3}]
        finally:
            await docs.drop()
            await client.close()

    asyncio.run(scenario())


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


@pytest.mark.parametrize(("backend", "env_name"), REMOTE_BACKENDS)
def test_remote_sql_decimal128_unique_values_fail_closed(backend, env_name):
    bson = pytest.importorskip("bson")
    dsn, database, prefix = _remote_target(backend, env_name)
    client = tm.TinyMongoClient(backend=backend, dsn=dsn)
    protected = client[database][prefix + "_decimal_protected"]
    existing = client[database][prefix + "_decimal_existing"]

    try:
        protected.create_index("value", unique=True)
        protected.insert_one({"_id": 1, "value": 1})
        with pytest.raises(TinyMongoNotSupportedError, match="Decimal128 values"):
            protected.insert_one({"_id": 2, "value": bson.Decimal128("1.00")})

        existing.insert_one({"_id": 1, "value": bson.Decimal128("2.00")})
        with pytest.raises(TinyMongoNotSupportedError, match="Decimal128 values"):
            existing.create_index("value", unique=True)
    finally:
        existing.drop()
        protected.drop()
        client.close()
