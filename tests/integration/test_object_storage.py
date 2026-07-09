import os

import pytest

import tinymongo as tm


pytestmark = pytest.mark.integration


def test_parquet_object_storage_round_trip():
    storage_uri = os.environ.get("TINYMONGO_OBJECT_STORAGE_URI")
    if not storage_uri:
        pytest.skip("TINYMONGO_OBJECT_STORAGE_URI is required")

    database = os.environ.get("TINYMONGO_OBJECT_STORAGE_DB", "tinymongoIntegration")
    collection = os.environ.get("TINYMONGO_OBJECT_STORAGE_COLLECTION", "roundTrip")
    client = tm.TinyMongoClient(
        "/tmp/tinymongo-object-storage",
        backend="parquet",
        storage_uri=storage_uri,
    )
    docs = client[database][collection]
    docs.delete_many({})

    docs.insert_many([
        {"_id": "one", "kind": "object-storage", "score": 1},
        {"_id": "two", "kind": "object-storage", "score": 2},
    ])
    docs.update_one({"_id": "one"}, {"$inc": {"score": 10}})

    assert docs.find_one({"_id": "one"})["score"] == 11
    assert docs.find({"kind": "object-storage"}).count() == 2

    docs.delete_many({})
