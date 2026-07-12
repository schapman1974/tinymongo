import sys

import pytest
import tinymongo
from tinymongo.errors import TinyMongoNotSupportedError


def test_pymongo_shaped_application_code_can_run_with_tinymongo(tmp_path):
    original_pymongo = sys.modules.get("pymongo")
    sys.modules["pymongo"] = tinymongo
    try:
        namespace = {"storage_path": str(tmp_path / "compat-db")}
        exec(
            """
import pymongo

client = pymongo.MongoClient(
    "mongodb://localhost:27017",
    serverSelectionTimeoutMS=50,
    connect=False,
    tinymongo_folder=storage_path,
)
assert client.server_info()["tinymongo"] is True

db = client["contract"]
users = db["users"]
users.create_index("email")
users.insert_many([
    {"_id": 1, "email": "ada@example.com", "score": 7, "tags": ["math"]},
    {"_id": 2, "email": "grace@example.com", "score": 9, "tags": ["code"]},
])
users.update_one({"email": "ada@example.com"}, {"$inc": {"score": 2}})
users.update_many({}, {"$addToSet": {"tags": "pioneer"}})

rows = list(users.find({"score": {"$gte": 9}}).sort("email", pymongo.ASCENDING))
assert [row["email"] for row in rows] == ["ada@example.com", "grace@example.com"]
assert users.count_documents({"tags": {"$all": ["pioneer"]}}) == 2
assert users.delete_one({"_id": 1}).deleted_count == 1
assert "contract" in client.list_database_names()
assert "users" in db.list_collection_names()
client.close()
""",
            namespace,
        )
    finally:
        if original_pymongo is None:
            sys.modules.pop("pymongo", None)
        else:
            sys.modules["pymongo"] = original_pymongo


def test_client_database_listing_and_server_info(tmp_path):
    client = tinymongo.MongoClient(tinymongo_folder=str(tmp_path / "dbs"))

    assert client.server_info()["storage"] == "tinydb"
    assert client.list_database_names() == []

    client.alpha.users.insert_one({"_id": 1})
    client.beta.users.insert_one({"_id": 2})

    assert client.database_names() == ["alpha", "beta"]
    assert client.alpha.list_collection_names() == ["users"]


def test_list_database_names_returns_empty_for_missing_folder(tmp_path):
    client = tinymongo.TinyMongoClient(str(tmp_path / "missing"))
    client._foldername = str(tmp_path / "does-not-exist")

    assert client.list_database_names() == []


def test_delete_one_missing_document_returns_zero(tmp_path):
    collection = tinymongo.TinyMongoClient(str(tmp_path / "db")).app.users

    assert collection.delete_one({"_id": "missing"}).deleted_count == 0


def test_noop_update_distinguishes_matched_and_modified_counts(tmp_path):
    collection = tinymongo.TinyMongoClient(str(tmp_path / "db")).app.users
    collection.insert_one({"_id": 1, "active": True})

    result = collection.update_one({"_id": 1}, {"$set": {"active": True}})

    assert result.matched_count == 1
    assert result.modified_count == 0


@pytest.mark.parametrize("backend", ["tinydb", "sqlite"])
def test_update_and_replace_upserts(tmp_path, backend):
    collection = tinymongo.TinyMongoClient(
        str(tmp_path / backend), backend=backend
    ).app.users

    updated = collection.update_one(
        {"email": "ada@example.com"},
        {"$set": {"active": True}},
        upsert=True,
    )
    assert updated.matched_count == 0
    assert updated.modified_count == 0
    assert updated.upserted_id is not None
    assert collection.find_one({"email": "ada@example.com"})["active"] is True

    replaced = collection.replace_one(
        {"email": "grace@example.com"},
        {"email": "grace@example.com", "active": True},
        upsert=True,
    )
    assert replaced.upserted_id is not None


def test_update_many_upsert_creates_one_document(tmp_path):
    collection = tinymongo.TinyMongoClient(str(tmp_path / "db")).app.users

    result = collection.update_many(
        {"profile.email": {"$eq": "ada@example.com"}, "ignored": {"$gt": 1}},
        {"$set": {"active": True}},
        upsert=True,
    )

    assert result.upserted_id is not None
    assert collection.count_documents({}) == 1
    assert collection.find_one({})["profile"]["email"] == "ada@example.com"


@pytest.mark.parametrize("backend", ["tinydb", "sqlite", "duckdb", "parquet"])
def test_backend_capabilities_are_explicit(tmp_path, backend):
    client = tinymongo.TinyMongoClient(str(tmp_path / backend), backend=backend)
    capabilities = client.capabilities()

    assert capabilities["backend"] == backend
    assert capabilities["persistent"] is True
    assert capabilities["aggregation"] is False
    assert capabilities["sessions"] is False
    assert client.supports("multiprocess_writes") is True
    assert client.supports("aggregation") is False


def test_object_storage_capabilities_are_conservative(tmp_path):
    client = tinymongo.TinyMongoClient(
        str(tmp_path), backend="parquet", storage_uri="s3://bucket/path"
    )

    assert client.supports("object_storage") is True
    assert client.supports("multiprocess_writes") is False
    with pytest.raises(ValueError, match="Unknown TinyMongo capability"):
        client.supports("missing")
    with pytest.raises(ValueError, match="Unknown TinyMongo capability"):
        client.supports("backend")


def test_unsupported_features_fail_loudly(tmp_path):
    client = tinymongo.TinyMongoClient(str(tmp_path / "db"))
    database = client.app
    collection = database.items

    unsupported_calls = [
        client.start_session,
        client.watch,
        database.command,
        database.watch,
        collection.aggregate,
        collection.bulk_write,
        collection.watch,
    ]
    for call in unsupported_calls:
        with pytest.raises(TinyMongoNotSupportedError):
            call([])

    with pytest.raises(TinyMongoNotSupportedError, match="single-field"):
        collection.create_index([("email", 1)])
    with pytest.raises(TinyMongoNotSupportedError, match="concerns"):
        collection.with_options(type("Concern", (), {"document": {"w": 2}})())


@pytest.mark.parametrize(
    "operation",
    [
        lambda collection: collection.insert_one({"_id": 1}, session=object()),
        lambda collection: collection.insert_many([], session=object()),
        lambda collection: collection.find({}, session=object()),
        lambda collection: collection.find_one({}, session=object()),
        lambda collection: collection.count_documents({}, session=object()),
        lambda collection: collection.update_one(
            {}, {"$set": {"x": 1}}, session=object()
        ),
        lambda collection: collection.update_many(
            {}, {"$set": {"x": 1}}, session=object()
        ),
        lambda collection: collection.replace_one({}, {}, session=object()),
        lambda collection: collection.find_one_and_update(
            {}, {"$set": {"x": 1}}, session=object()
        ),
        lambda collection: collection.find_one_and_replace({}, {}, session=object()),
        lambda collection: collection.delete_one({}, session=object()),
        lambda collection: collection.delete_many({}, session=object()),
        lambda collection: collection.drop(session=object()),
    ],
)
def test_session_options_are_rejected(tmp_path, operation):
    collection = tinymongo.TinyMongoClient(str(tmp_path / "db")).app.items

    with pytest.raises(TinyMongoNotSupportedError, match="Sessions"):
        operation(collection)
