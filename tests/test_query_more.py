import os
import shutil

import pytest

import tinymongo as tm

DB_DIR = os.path.abspath("./test_db_query_more")


@pytest.fixture(autouse=True)
def isolated_db_dir(tmp_path):
    global DB_DIR
    DB_DIR = str(tmp_path / "query-more")


def setup_db():
    if os.path.exists(DB_DIR):
        shutil.rmtree(DB_DIR)
    os.makedirs(DB_DIR, exist_ok=True)


def test_nin_query_operator():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many(
        [
            {"_id": 1, "tag": "alpha"},
            {"_id": 2, "tag": "beta"},
            {"_id": 3, "tag": "gamma"},
        ]
    )

    results = c.find({"tag": {"$nin": ["alpha", "beta"]}})
    assert results.count() == 1
    assert results[0]["tag"] == "gamma"


def test_update_many_applies_to_all_matches():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many(
        [
            {"_id": 1, "group": "a", "value": 1},
            {"_id": 2, "group": "a", "value": 2},
            {"_id": 3, "group": "b", "value": 3},
        ]
    )

    result = c.update_many({"group": "a"}, {"$set": {"active": True}})
    assert result.matched_count == 2
    assert result.modified_count == 2
    assert c.find({"active": True}).count() == 2


def test_write_filters_share_regex_options_semantics():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many(
        [
            {"_id": 1, "name": "Alpha"},
            {"_id": 2, "name": "amber"},
            {"_id": 3, "name": "Beta"},
        ]
    )
    starts_with_a = {"name": {"$regex": "^a", "$options": "i"}}

    one = c.update_one(starts_with_a, {"$set": {"first": True}})
    many = c.update_many(starts_with_a, {"$set": {"matched": True}})
    replacement = c.replace_one(
        {"name": {"$regex": "^b", "$options": "i"}},
        {"name": "Replaced"},
    )

    assert one.matched_count == 1
    assert many.matched_count == 2
    assert {doc["_id"] for doc in c.find({"matched": True})} == {1, 2}
    assert replacement.matched_count == 1
    assert c.find_one({"_id": 3}) == {"_id": 3, "name": "Replaced"}


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
    c.insert_many(
        [
            {"_id": 1, "tags": ["a", "b", "c"]},
            {"_id": 2, "tags": ["a", "c"]},
            {"_id": 3, "tags": ["b", "a", "c"]},
        ]
    )

    matches = c.find({"tags": {"$all": ["a", "c"]}})
    assert matches.count() == 3


def test_nor_operator_excludes_matching_documents():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many(
        [
            {"_id": 1, "status": "draft", "score": 2},
            {"_id": 2, "status": "published", "score": 5},
            {"_id": 3, "status": "archived", "score": 9},
        ]
    )

    matches = c.find({"$nor": [{"status": "draft"}, {"score": {"$gt": 8}}]})

    assert matches.count() == 1
    assert matches[0]["_id"] == 2


def test_count_documents_uses_filter():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many(
        [
            {"_id": 1, "even": True},
            {"_id": 2, "even": False},
            {"_id": 3, "even": True},
        ]
    )

    assert c.count_documents({"even": True}) == 2


def test_exists_query_operator():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many(
        [
            {"_id": 1, "name": "alpha", "meta": {"active": True}},
            {"_id": 2, "name": "beta"},
        ]
    )

    assert c.find({"meta": {"$exists": True}}).count() == 1
    assert c.find({"meta": {"$exists": False}}).count() == 1


def test_update_operator_support():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_one({"_id": 1, "count": 1, "tags": ["a"], "meta": {"old": True}})

    c.update_one(
        {"_id": 1},
        {
            "$inc": {"count": 2},
            "$set": {"meta.active": True},
            "$unset": {"meta.old": ""},
            "$push": {"tags": "b"},
        },
    )
    c.update_one({"_id": 1}, {"$addToSet": {"tags": "b"}})
    c.update_one({"_id": 1}, {"$pull": {"tags": "a"}})

    doc = c.find_one({"_id": 1})
    assert doc["count"] == 3
    assert doc["meta"] == {"active": True}
    assert doc["tags"] == ["b"]


def test_update_many_applies_operators_to_each_match():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many(
        [
            {"_id": 1, "group": "a", "count": 1, "tags": []},
            {"_id": 2, "group": "a", "count": 2, "tags": []},
            {"_id": 3, "group": "b", "count": 3, "tags": []},
        ]
    )

    result = c.update_many(
        {"group": "a"},
        {"$inc": {"count": 10}, "$push": {"tags": "updated"}},
    )

    assert result.modified_count == 2
    assert c.find_one({"_id": 1})["count"] == 11
    assert c.find_one({"_id": 2})["count"] == 12
    assert c.find_one({"_id": 3})["count"] == 3
    assert c.find({"tags": {"$all": ["updated"]}}).count() == 2


def test_nested_update_creates_missing_subdocuments():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_one({"_id": 1})

    c.update_one(
        {"_id": 1}, {"$set": {"profile.name": "Ada"}, "$inc": {"stats.views": 1}}
    )

    assert c.find_one({"_id": 1}) == {
        "_id": 1,
        "profile": {"name": "Ada"},
        "stats": {"views": 1},
    }


def test_update_rejects_list_operator_target():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_one({"_id": 1, "tags": "not-a-list"})

    with pytest.raises(ValueError, match="must be a list"):
        c.update_one({"_id": 1}, {"$push": {"tags": "new"}})
    assert c.find_one({"_id": 1})["tags"] == "not-a-list"


def test_update_many_requires_operators():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many(
        [
            {"_id": 1, "group": "a", "old": True},
            {"_id": 2, "group": "a", "old": True},
        ]
    )

    with pytest.raises(ValueError, match="update only works with \\$ operators"):
        c.update_many({"group": "a"}, {"group": "a", "new": True})


def test_collection_indexes_accelerate_equality_queries():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many(
        [
            {"_id": 1, "email": "a@example.com"},
            {"_id": 2, "email": "b@example.com"},
        ]
    )

    assert c.create_index("email") == "email_1"
    assert c.create_index("secondary") == "secondary_1"
    assert {"name": "email_1", "key": [("email", 1)]} in c.list_indexes()
    assert c.find({"email": "b@example.com"})[0]["_id"] == 2

    c.update_one({"_id": 2}, {"$set": {"email": "c@example.com"}})
    assert c.find({"email": "c@example.com"})[0]["_id"] == 2

    # A fresh collection handle exercises durable index loading before a drop;
    # choosing the second index also verifies name/field lookup traversal.
    client.db.collection.drop_index("secondary")
    c.drop_index("email")
    assert c.list_indexes() == [{"name": "_id_", "key": [("_id", 1)]}]


def test_equality_index_deduplicates_repeated_array_values():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    collection = client.db.collection
    collection.insert_one({"_id": 1, "tags": ["same", "same"]})

    assert len(list(collection.find({"tags": "same"}))) == 1
    collection.create_index("tags")
    assert list(collection.find({"tags": "same"})) == [
        {"_id": 1, "tags": ["same", "same"]}
    ]


def test_index_cache_is_invalidated_after_insert_and_delete():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_one({"_id": 1, "email": "a@example.com"})
    c.create_index("email")
    assert c.find({"email": "a@example.com"}).count() == 1

    c.insert_one({"_id": 2, "email": "b@example.com"})
    assert c.find({"email": "b@example.com"}).count() == 1

    c.delete_one({"_id": 2})
    assert c.find({"email": "b@example.com"}).count() == 0


def test_index_matches_array_membership_semantics():
    setup_db()
    client = tm.TinyMongoClient(DB_DIR)
    c = client.db.collection
    c.insert_many(
        [
            {"_id": 1, "tags": ["a", "b"]},
            {"_id": 2, "tags": "a"},
        ]
    )

    c.create_index("tags")
    assert c.find({"tags": "a"}).count() == 2
    c.drop_index("tags")
    assert c.find({"tags": "a"}).count() == 2
