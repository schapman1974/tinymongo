import os
import shutil

import tinymongo as tm

DB_DIR = os.path.abspath("./test_db_query_more")


def setup_db():
    if os.path.exists(DB_DIR):
        shutil.rmtree(DB_DIR)
    os.makedirs(DB_DIR, exist_ok=True)


def test_nin_query_operator():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many([
        {"_id": 1, "tag": "alpha"},
        {"_id": 2, "tag": "beta"},
        {"_id": 3, "tag": "gamma"},
    ])

    results = c.find({"tag": {"$nin": ["alpha", "beta"]}})
    assert results.count() == 1
    assert results[0]["tag"] == "gamma"


def test_update_many_applies_to_all_matches():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many([
        {"_id": 1, "group": "a", "value": 1},
        {"_id": 2, "group": "a", "value": 2},
        {"_id": 3, "group": "b", "value": 3},
    ])

    result = c.update_many({"group": "a"}, {"$set": {"active": True}})
    assert result.matched_count == 2
    assert result.modified_count == 2
    assert c.find({"active": True}).count() == 2


def test_replace_one_replaces_single_document():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_one({"_id": "one", "count": 1, "tag": "keep"})
    c.insert_one({"_id": "two", "count": 2, "tag": "keep"})

    result = c.replace_one({"_id": "one"}, {"count": 42})
    assert result.matched_count == 1
    doc = c.find_one({"_id": "one"})
    assert doc["count"] == 42
    assert doc["_id"] == "one"
    assert "tag" not in doc


def test_find_one_and_update_returns_old_document():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_one({"_id": "item", "score": 10})

    old_doc = c.find_one_and_update({"_id": "item"}, {"$set": {"score": 20}})
    assert old_doc["score"] == 10
    assert c.find_one({"_id": "item"})["score"] == 20


def test_skip_limit_pagination_with_find():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many([{"_id": i, "value": i} for i in range(20)])

    page = c.find(sort=[("value", 1)], skip=5, limit=5)
    assert page.count() == 5
    assert page[0]["value"] == 5
    assert page[-1]["value"] == 9


def test_all_operator_matches_arrays():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many([
        {"_id": 1, "tags": ["a", "b", "c"]},
        {"_id": 2, "tags": ["a", "c"]},
        {"_id": 3, "tags": ["b", "a", "c"]},
    ])

    matches = c.find({"tags": {"$all": ["a", "c"]}})
    assert matches.count() == 3


def test_count_documents_uses_filter():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many([
        {"_id": 1, "even": True},
        {"_id": 2, "even": False},
        {"_id": 3, "even": True},
    ])

    assert c.count_documents({"even": True}) == 2
