import sys

import tinymongo


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
