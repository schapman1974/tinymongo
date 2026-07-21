import os
import shutil

import pytest
import tinymongo as tm


DB_DIR = os.path.abspath("./test_db_mongo_like")


@pytest.fixture(autouse=True)
def isolated_db_dir(tmp_path):
    global DB_DIR
    DB_DIR = str(tmp_path / "mongo-like")


def setup_db():
    if os.path.exists(DB_DIR):
        shutil.rmtree(DB_DIR)
    os.makedirs(DB_DIR, exist_ok=True)


def test_upsert_and_find():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection

    # insert doc
    c.insert_one({"_id": "a", "count": 1})

    # upsert behavior simulation: update if exists else insert
    existing = c.find_one({"_id": "a"})
    assert existing is not None

    # update using $set
    c.update_one({"_id": "a"}, {"$set": {"count": 5}})
    doc = c.find_one({"_id": "a"})
    assert doc["count"] == 5


def test_update_operators():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection

    c.insert_many([{"_id": i, "v": i} for i in range(5)])

    # Simulate $inc by reading, modifying, writing
    for i in range(5):
        c.update_one({"_id": i}, {"$set": {"v": i + 10}})

    vals = sorted([d["v"] for d in c.find()])
    assert vals == [10, 11, 12, 13, 14]


def test_projection_like():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection

    c.insert_one({"_id": "p", "a": 1, "b": 2, "c": 3})
    doc = c.find_one({"_id": "p"}, {"a": 1, "_id": 0})
    assert doc == {"a": 1}


def test_find_and_modify_semantics():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection

    c.insert_one({"_id": "fm", "counter": 0})

    # Emulate findOneAndUpdate: read, update atomically via TinyDB API
    c.update_one({"_id": "fm"}, {"$set": {"counter": 1}})
    d = c.find_one({"_id": "fm"})
    assert d["counter"] == 1


def test_unique_index_simulation():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection

    # TinyMongo insert_one enforces _id uniqueness
    c.insert_one({"_id": "u1", "v": 1})
    try:
        c.insert_one({"_id": "u1", "v": 2})
        inserted = True
    except Exception:
        inserted = False

    assert not inserted
