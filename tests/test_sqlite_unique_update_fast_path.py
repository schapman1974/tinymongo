"""TM-043 contracts for bounded SQLite updates with unique indexes."""

import pytest

import tinymongo as tm
import tinymongo.table_backends as table_backends
from tinymongo.errors import DuplicateKeyError


def _collection(tmp_path, name="items"):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    return client, client.app[name]


def _track_decoded_ids(monkeypatch):
    decoded_ids = []
    original_loads = table_backends._json_loads

    def tracked_loads(value):
        document = original_loads(value)
        decoded_ids.append(document.get("_id"))
        return document

    monkeypatch.setattr(table_backends, "_json_loads", tracked_loads)
    return decoded_ids


@pytest.mark.parametrize("document_count", [10, 1_000])
def test_tm043_unrelated_id_update_with_unique_index_is_collection_size_bounded(
    tmp_path,
    monkeypatch,
    document_count,
):
    client, collection = _collection(tmp_path)
    collection.insert_many(
        [
            {
                "_id": index,
                "email": "user-{0}@example.com".format(index),
                "visits": 0,
            }
            for index in range(document_count)
        ]
    )
    collection.create_index("email", unique=True)
    decoded_ids = _track_decoded_ids(monkeypatch)

    try:
        target = document_count - 1
        result = collection.update_one(
            {"_id": target},
            {"$inc": {"visits": 1}},
        )

        assert (result.matched_count, result.modified_count) == (1, 1)
        # SQLite may decode the target again while maintaining its expression
        # index, but no other collection payload should cross into Python.
        assert decoded_ids
        assert set(decoded_ids) == {target}
        assert len(decoded_ids) <= 4
    finally:
        client.close()


def test_tm043_same_unique_value_noop_stays_targeted(tmp_path, monkeypatch):
    client, collection = _collection(tmp_path)
    collection.insert_many(
        [
            {"_id": index, "email": "user-{0}@example.com".format(index)}
            for index in range(100)
        ]
    )
    collection.create_index("email", unique=True)
    decoded_ids = _track_decoded_ids(monkeypatch)

    try:
        result = collection.update_one(
            {"_id": 73},
            {"$set": {"email": "user-73@example.com"}},
        )

        assert (result.matched_count, result.modified_count) == (1, 0)
        assert decoded_ids == [73]
    finally:
        client.close()


def test_tm043_reordered_unique_multikey_entries_stay_targeted(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path)
    collection.insert_many(
        [
            {
                "_id": index,
                "tags": ["tag-{0}".format(index), "shared-{0}".format(index)],
            }
            for index in range(100)
        ]
    )
    collection.create_index("tags", unique=True)
    decoded_ids = _track_decoded_ids(monkeypatch)

    try:
        result = collection.update_one(
            {"_id": 73},
            {"$set": {"tags": ["shared-73", "tag-73"]}},
        )

        assert (result.matched_count, result.modified_count) == (1, 1)
        assert decoded_ids and set(decoded_ids) == {73}
    finally:
        client.close()


def test_tm043_unique_index_id_miss_decodes_no_payloads(tmp_path, monkeypatch):
    client, collection = _collection(tmp_path)
    collection.insert_many(
        [
            {"_id": index, "email": "user-{0}@example.com".format(index)}
            for index in range(100)
        ]
    )
    collection.create_index("email", unique=True)
    decoded_ids = _track_decoded_ids(monkeypatch)

    try:
        result = collection.update_one(
            {"_id": "missing"},
            {"$set": {"seen": True}},
        )

        assert (result.matched_count, result.modified_count) == (0, 0)
        assert decoded_ids == []
    finally:
        client.close()


def test_tm043_changed_unique_scalar_preserves_atomic_conflict_handling(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path)
    collection.insert_many(
        [
            {"_id": index, "email": "user-{0}@example.com".format(index)}
            for index in range(100)
        ]
    )
    collection.create_index("email", unique=True)
    decoded_ids = _track_decoded_ids(monkeypatch)

    try:
        result = collection.update_one(
            {"_id": 73},
            {"$set": {"email": "available@example.com"}},
        )
        assert (result.matched_count, result.modified_count) == (1, 1)

        decoded_ids.clear()
        with pytest.raises(DuplicateKeyError):
            collection.update_one(
                {"_id": 73},
                {"$set": {"email": "user-0@example.com"}},
            )

        decoded_ids.clear()
        assert collection.find_one({"_id": 73})["email"] == "available@example.com"
    finally:
        client.close()


def test_tm043_sparse_unique_membership_transitions_remain_atomic(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path)
    collection.create_index("email", unique=True, sparse=True)
    collection.insert_many(
        [
            {"_id": 1, "name": "outside"},
            {"_id": 2, "email": "taken@example.com"},
            {"_id": 3},
        ]
    )
    decoded_ids = _track_decoded_ids(monkeypatch)

    try:
        result = collection.update_one({"_id": 1}, {"$set": {"name": "renamed"}})
        assert (result.matched_count, result.modified_count) == (1, 1)
        assert decoded_ids and set(decoded_ids) == {1}

        decoded_ids.clear()
        collection.update_one(
            {"_id": 1},
            {"$set": {"email": "available@example.com"}},
        )

        decoded_ids.clear()
        collection.update_one({"_id": 1}, {"$unset": {"email": ""}})

        decoded_ids.clear()
        with pytest.raises(DuplicateKeyError):
            collection.update_one(
                {"_id": 1},
                {"$set": {"email": "taken@example.com"}},
            )
        decoded_ids.clear()
        assert "email" not in collection.find_one({"_id": 1})
    finally:
        client.close()


def test_tm043_partial_unique_membership_transitions_remain_atomic(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path)
    collection.create_index(
        "email",
        name="active_email",
        unique=True,
        partialFilterExpression={"active": True},
    )
    collection.insert_many(
        [
            {"_id": 1, "email": "same@example.com", "active": True},
            {"_id": 2, "email": "same@example.com", "active": False},
            {"_id": 3, "email": "other@example.com", "active": False},
        ]
    )
    decoded_ids = _track_decoded_ids(monkeypatch)

    try:
        collection.update_one({"_id": 2}, {"$set": {"note": "still outside"}})
        assert decoded_ids and set(decoded_ids) == {2}

        decoded_ids.clear()
        with pytest.raises(DuplicateKeyError):
            collection.update_one({"_id": 2}, {"$set": {"active": True}})

        decoded_ids.clear()
        collection.update_one({"_id": 1}, {"$set": {"active": False}})

        decoded_ids.clear()
        result = collection.update_one({"_id": 2}, {"$set": {"active": True}})
        assert (result.matched_count, result.modified_count) == (1, 1)

        decoded_ids.clear()
        assert collection.find_one({"_id": 1})["active"] is False
        assert collection.find_one({"_id": 2})["active"] is True
    finally:
        client.close()


def test_tm043_compound_multikey_overlap_is_detected_for_parent_path_update(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path)
    collection.create_index(
        [("owner.id", 1), ("labels", 1)],
        name="owner_labels",
        unique=True,
    )
    collection.insert_many(
        [
            {
                "_id": 1,
                "owner": {"id": "north", "name": "Ada"},
                "labels": ["red", "blue"],
            },
            {
                "_id": 2,
                "owner": {"id": "south", "name": "Grace"},
                "labels": ["blue", "green"],
            },
            {
                "_id": 3,
                "owner": {"id": "west", "name": "Lin"},
                "labels": ["yellow"],
            },
        ]
    )
    decoded_ids = _track_decoded_ids(monkeypatch)

    try:
        collection.update_one({"_id": 2}, {"$set": {"note": "unrelated"}})
        assert decoded_ids and set(decoded_ids) == {2}

        decoded_ids.clear()
        with pytest.raises(DuplicateKeyError):
            collection.update_one(
                {"_id": 2},
                {"$set": {"owner": {"id": "north", "name": "Grace"}}},
            )

        decoded_ids.clear()
        assert collection.find_one({"_id": 2})["owner"]["id"] == "south"
        assert collection.find_one({"_id": 2})["labels"] == ["blue", "green"]
    finally:
        client.close()


def test_tm043_update_many_uses_indexed_candidates_with_unrelated_unique_index(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path)
    collection.insert_many(
        [
            {
                "_id": index,
                "email": "user-{0}@example.com".format(index),
                "group": "selected" if index % 20 == 0 else "other",
                "visits": 0,
            }
            for index in range(200)
        ]
    )
    collection.create_index("email", unique=True)
    collection.create_index("group")
    decoded_ids = _track_decoded_ids(monkeypatch)

    try:
        result = collection.update_many(
            {"group": "selected"},
            {"$inc": {"visits": 1}},
        )
        selected = set(range(0, 200, 20))

        assert (result.matched_count, result.modified_count) == (10, 10)
        assert decoded_ids
        assert set(decoded_ids) == selected
        # One candidate decode plus a small, constant expression-index cost
        # per replacement is acceptable; collection-wide validation is not.
        assert len(decoded_ids) <= 4 * len(selected)

        decoded_ids.clear()
        assert collection.count_documents({"visits": 1}) == 10
        assert collection.count_documents({"visits": 0}) == 190
    finally:
        client.close()


def test_tm043_no_index_id_update_keeps_existing_targeted_behavior(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path)
    collection.insert_many(
        [{"_id": index, "value": index, "visits": 0} for index in range(100)]
    )
    decoded_ids = _track_decoded_ids(monkeypatch)

    try:
        result = collection.update_one({"_id": 73}, {"$inc": {"visits": 1}})

        assert (result.matched_count, result.modified_count) == (1, 1)
        assert decoded_ids == [73]
    finally:
        client.close()
