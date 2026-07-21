import os
from uuid import uuid4

import pytest

import tinymongo as tm
from tinymongo.errors import DuplicateKeyError, TinyMongoNotSupportedError


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


@pytest.mark.parametrize(
    ("backend", "env_name"),
    REMOTE_BACKENDS,
)
def test_remote_sql_backend_round_trip(backend, env_name):
    dsn, database, prefix = _remote_target(backend, env_name)
    collection = prefix + "_round_trip"
    client = tm.TinyMongoClient(backend=backend, dsn=dsn)
    docs = client[database][collection]
    try:
        docs.insert_many(
            [
                {"_id": "one", "kind": backend, "score": 1},
                {"_id": "two", "kind": backend, "score": 2},
            ]
        )
        docs.update_one({"_id": "one"}, {"$inc": {"score": 10}})

        assert docs.find_one({"_id": "one"})["score"] == 11
        assert docs.find({"kind": backend}).count() == 2
        assert collection in client[database].collection_names()
        assert database in client.list_database_names()
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
