import os

import pytest

import tinymongo as tm


pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("backend", "env_name"),
    [
        ("postgres", "TINYMONGO_POSTGRES_DSN"),
        ("mariadb", "TINYMONGO_MYSQL_DSN"),
    ],
)
def test_remote_sql_backend_round_trip(backend, env_name):
    dsn = os.environ.get(env_name)
    if not dsn:
        pytest.skip("{0} is required".format(env_name))

    database = os.environ.get("TINYMONGO_REMOTE_SQL_DB", "tinymongoIntegration")
    collection = os.environ.get("TINYMONGO_REMOTE_SQL_COLLECTION", "roundTrip")
    client = tm.TinyMongoClient(backend=backend, dsn=dsn)
    docs = client[database][collection]
    docs.drop()

    docs.insert_many([
        {"_id": "one", "kind": backend, "score": 1},
        {"_id": "two", "kind": backend, "score": 2},
    ])
    docs.update_one({"_id": "one"}, {"$inc": {"score": 10}})

    assert docs.find_one({"_id": "one"})["score"] == 11
    assert docs.find({"kind": backend}).count() == 2
    assert collection in client[database].collection_names()
    assert database in client.list_database_names()

    docs.drop()
