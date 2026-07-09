import tinymongo as pymongo
from tinymongo import tinymongo as core


def test_import_tinymongo_as_pymongo_dropin_subset(tmp_path):
    client = pymongo.MongoClient(str(tmp_path / "db"))
    collection = client.app.users

    collection.create_index("email")
    result = collection.insert_many(
        [
            {"_id": 1, "email": "ada@example.com", "name": "Ada", "score": 7},
            {"_id": 2, "email": "grace@example.com", "name": "Grace", "score": 9},
        ]
    )

    assert result.inserted_ids == [1, 2]
    assert collection.find_one({"email": "ada@example.com"})["name"] == "Ada"
    assert collection.count_documents({"score": {"$gte": 7}}) == 2

    update = collection.update_one({"_id": 1}, {"$inc": {"score": 1}})
    assert update.modified_count == 1

    rows = list(
        collection.find({"score": {"$gte": 8}}).sort("score", pymongo.DESCENDING)
    )
    assert [row["_id"] for row in rows] == [2, 1]

    delete = collection.delete_one({"_id": 1})
    assert delete.deleted_count == 1
    assert collection.count_documents({}) == 1


def test_pymongo_sort_constants_are_exposed():
    assert pymongo.ASCENDING == 1
    assert pymongo.DESCENDING == -1


def test_mongo_client_ignores_uri_and_uses_configured_local_folder(tmp_path):
    local_folder = tmp_path / "local"
    client = pymongo.MongoClient(
        "mongodb://localhost:27017",
        serverSelectionTimeoutMS=10,
        connect=False,
        tinymongo_folder=str(local_folder),
    )

    client.app.users.insert_one({"_id": "network-shaped", "ok": True})

    assert client._foldername == str(local_folder)
    assert (local_folder / "app.json").exists()
    assert client.app.users.find_one({"_id": "network-shaped"})["ok"] is True


def test_mongo_client_uses_env_folder_for_network_target(tmp_path, monkeypatch):
    local_folder = tmp_path / "env-local"
    monkeypatch.setenv("TINYMONGO_HOME", str(local_folder))

    client = pymongo.MongoClient(host="localhost", port=27017)
    client.app.users.insert_one({"_id": "host-port"})

    assert client._foldername == str(local_folder)
    assert client.app.users.count_documents({}) == 1


def test_mongo_client_keeps_plain_path_as_storage_folder(tmp_path):
    local_folder = tmp_path / "plain-path"
    client = pymongo.MongoClient(str(local_folder))

    client.app.users.insert_one({"_id": "plain"})

    assert client._foldername == str(local_folder)
    assert (local_folder / "app.json").exists()


def test_network_target_detection_branches():
    assert core._looks_like_network_target(["localhost:27017"]) is True
    assert core._looks_like_network_target(object()) is False
    assert core._looks_like_network_target("mongodb://localhost:27017") is True
    assert core._looks_like_network_target("127.0.0.1") is True
    assert core._looks_like_network_target("[::1]:27017") is True
    assert core._folder_from_mongo_client_args(None, None, {}) == "tinydb"
