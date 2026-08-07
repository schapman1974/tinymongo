"""Focused mutation-path coverage for the experimental sharded backend."""

import sqlite3

import pytest

import tinymongo
from tinymongo.errors import DuplicateKeyError, StorageCorruptionError
from tinymongo.indexes import IndexSpec


@pytest.fixture
def sharded_engine(tmp_path):
    client = tinymongo.TinyMongoClient(
        str(tmp_path / "store"),
        backend="sqlite-sharded",
        sqlite_shards=2,
    )
    engine = client.app.engine
    engine.create_collection("items")
    try:
        yield engine
    finally:
        client.close()


def _id_for_shard(engine, shard_index, label):
    for candidate in range(10_000):
        document_id = "{0}-{1}".format(label, candidate)
        if engine._shard_index(document_id) == shard_index:
            return document_id
    raise AssertionError("could not find an id for shard {0}".format(shard_index))


def test_conflict_candidate_helpers_route_and_fail_closed(
    sharded_engine,
    monkeypatch,
):
    engine = sharded_engine
    documents = [
        {"_id": _id_for_shard(engine, 0, "candidate-zero")},
        {"_id": _id_for_shard(engine, 1, "candidate-one")},
    ]
    unique_specs = [IndexSpec("email", unique=True)]

    assert (
        engine.find_insert_conflict_candidates("items", documents, unique_specs) is None
    )

    candidate_calls = []
    for index, shard in enumerate(engine._shards):

        def find_candidates(collection, group, specs, shard_index=index):
            candidate_calls.append(
                (shard_index, collection, [item["_id"] for item in group], specs)
            )
            return [{"_id": "found-{0}".format(shard_index)}]

        monkeypatch.setattr(
            shard,
            "find_insert_conflict_candidates",
            find_candidates,
        )

    assert engine.find_insert_conflict_candidates("items", documents, []) == [
        {"_id": "found-0"},
        {"_id": "found-1"},
    ]
    assert [call[0] for call in candidate_calls] == [0, 1]
    assert all(call[1] == "items" and call[3] == [] for call in candidate_calls)

    monkeypatch.setattr(
        engine._shards[0],
        "find_insert_conflict_candidates",
        lambda *_args, **_kwargs: None,
    )
    assert engine.find_insert_conflict_candidates("items", documents, []) is None

    prepared = engine._prepare_insert_many(documents)
    assert (
        engine._find_prepared_insert_conflict_candidates(
            "items", prepared, unique_specs
        )
        is None
    )

    prepared_calls = []
    for index, shard in enumerate(engine._shards):

        def find_prepared(collection, group, specs, shard_index=index):
            prepared_calls.append(
                (
                    shard_index,
                    collection,
                    [item.document["_id"] for item in group],
                    specs,
                )
            )
            return [{"_id": "prepared-{0}".format(shard_index)}]

        monkeypatch.setattr(
            shard,
            "_find_prepared_insert_conflict_candidates",
            find_prepared,
        )

    assert engine._find_prepared_insert_conflict_candidates("items", prepared, []) == [
        {"_id": "prepared-0"},
        {"_id": "prepared-1"},
    ]
    assert [call[0] for call in prepared_calls] == [0, 1]

    monkeypatch.setattr(
        engine._shards[0],
        "_find_prepared_insert_conflict_candidates",
        lambda *_args, **_kwargs: None,
    )
    assert (
        engine._find_prepared_insert_conflict_candidates("items", prepared, []) is None
    )


def test_direct_prevalidated_insert_and_empty_prepared_batch(sharded_engine):
    engine = sharded_engine
    documents = [
        {"_id": _id_for_shard(engine, 0, "prevalidated-zero"), "value": 0},
        {"_id": _id_for_shard(engine, 1, "prevalidated-one"), "value": 1},
    ]

    assert engine.insert_many_prevalidated(
        "items",
        documents,
        bypass_document_validation=True,
    ) == [0, 1]
    assert {document["_id"] for document in engine.find("items")} == {
        document["_id"] for document in documents
    }
    assert (
        engine._insert_many_prepared(
            "items",
            [],
            bypass_document_validation=True,
        )
        == []
    )


def test_insert_integrity_failure_rolls_back_before_any_commit(sharded_engine):
    engine = sharded_engine
    document_id = _id_for_shard(engine, 0, "duplicate")
    prepared = engine._prepare_insert_many(
        [{"_id": document_id, "value": 1}, {"_id": document_id, "value": 2}]
    )

    with pytest.raises(DuplicateKeyError):
        engine._insert_many_prepared("items", prepared)

    assert engine.find("items") == []


def test_insert_runtime_failure_rolls_back_before_any_commit(
    sharded_engine,
    monkeypatch,
):
    engine = sharded_engine
    document_id = _id_for_shard(engine, 0, "runtime-failure")
    prepared = engine._prepare_insert_many([{"_id": document_id}])

    def fail_insert(*_args, **_kwargs):
        raise RuntimeError("injected insert failure")

    monkeypatch.setattr(
        engine._shards[0],
        "insert_ordered_rows_on_connection",
        fail_insert,
    )

    with pytest.raises(RuntimeError, match="injected insert failure"):
        engine._insert_many_prepared("items", prepared)

    assert engine.find("items") == []


class _CommitFailureConnection:
    """Proxy a real connection while injecting one commit failure."""

    def __init__(self, connection, failure=None):
        self._connection = connection
        self._failure = failure

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self):
        if self._failure is not None:
            failure, self._failure = self._failure, None
            raise failure
        return self._connection.commit()


@pytest.mark.parametrize(
    "failure",
    [sqlite3.IntegrityError("injected conflict"), RuntimeError("injected failure")],
    ids=["integrity", "runtime"],
)
def test_insert_commit_failure_after_another_shard_committed_is_loud(
    sharded_engine,
    monkeypatch,
    failure,
):
    engine = sharded_engine
    first_id = _id_for_shard(engine, 0, "committed")
    second_id = _id_for_shard(engine, 1, "rolled-back")
    prepared = engine._prepare_insert_many([{"_id": first_id}, {"_id": second_id}])
    first_connect = engine._shards[0]._connect
    second_connect = engine._shards[1]._connect
    monkeypatch.setattr(
        engine._shards[0],
        "_connect",
        lambda: _CommitFailureConnection(first_connect()),
    )
    monkeypatch.setattr(
        engine._shards[1],
        "_connect",
        lambda: _CommitFailureConnection(second_connect(), failure),
    )

    with pytest.raises(StorageCorruptionError, match="failed after another shard"):
        engine._insert_many_prepared("items", prepared)

    assert engine._shards[0].find("items", {"_id": first_id}) == [{"_id": first_id}]
    assert engine._shards[1].find("items", {"_id": second_id}) == []


def test_unique_update_paths_and_unique_replace_validation(sharded_engine):
    engine = sharded_engine
    first_id = _id_for_shard(engine, 0, "unique-first")
    second_id = _id_for_shard(engine, 1, "unique-second")
    engine.create_index(
        "items",
        IndexSpec("email", name="unique_email", unique=True),
    )
    engine.insert_many(
        "items",
        [
            {"_id": first_id, "email": "first@example.com", "kind": "user"},
            {"_id": second_id, "email": "second@example.com", "kind": "user"},
        ],
    )

    assert (
        engine.update_many(
            "items",
            {"kind": "missing"},
            {"$set": {"checked": True}},
        )
        == []
    )

    validated = []
    assert engine.update_many_with_result(
        "items",
        {"_id": first_id},
        {"$set": {"email": "first@example.com"}},
        validate_document=validated.append,
    ) == (1, 0)
    assert validated == [
        {"_id": first_id, "email": "first@example.com", "kind": "user"}
    ]

    assert engine.update_many(
        "items",
        {"kind": "user"},
        {"$set": {"checked": True}},
        multi=False,
    ) == [first_id]
    assert engine.find("items", {"_id": first_id})[0]["checked"] is True
    assert "checked" not in engine.find("items", {"_id": second_id})[0]

    engine.replace_one(
        "items",
        first_id,
        {
            "_id": first_id,
            "email": "replacement@example.com",
            "kind": "replacement",
        },
    )
    assert engine.find("items", {"_id": first_id}) == [
        {
            "_id": first_id,
            "email": "replacement@example.com",
            "kind": "replacement",
        }
    ]

    with pytest.raises(DuplicateKeyError):
        engine.replace_one(
            "items",
            first_id,
            {
                "_id": first_id,
                "email": "second@example.com",
                "kind": "duplicate",
            },
        )
    assert engine.find("items", {"_id": first_id})[0]["email"] == (
        "replacement@example.com"
    )


def test_nonunique_single_update_methods_stop_after_first_matching_shard(
    sharded_engine,
):
    engine = sharded_engine
    first_id = _id_for_shard(engine, 0, "nonunique-first")
    second_id = _id_for_shard(engine, 1, "nonunique-second")
    engine.insert_many(
        "items",
        [{"_id": first_id, "value": 0}, {"_id": second_id, "value": 0}],
    )

    assert engine.update_many(
        "items",
        {},
        {"$set": {"first_update": True}},
        multi=False,
    ) == [first_id]
    assert "first_update" not in engine.find("items", {"_id": second_id})[0]

    assert engine.update_many(
        "items",
        {"first_update": {"$exists": False}},
        {"$set": {"routed_update": True}},
        multi=False,
    ) == [second_id]

    assert engine.update_many_with_result(
        "items",
        {"first_update": {"$exists": False}},
        {"$set": {"second_update": True}},
        multi=False,
    ) == (1, 1)
    assert "second_update" not in engine.find("items", {"_id": first_id})[0]
    assert engine.find("items", {"_id": second_id})[0]["second_update"] is True

    assert set(
        engine.update_many(
            "items",
            {"value": 0},
            {"$set": {"all_updated": True}},
            multi=True,
        )
    ) == {first_id, second_id}
    assert engine.update_many_with_result(
        "items",
        {"all_updated": True},
        {"$inc": {"value": 1}},
        multi=True,
    ) == (2, 2)

    engine.replace_one("items", first_id, {"_id": first_id, "value": 9})
    assert engine.find("items", {"_id": first_id}) == [{"_id": first_id, "value": 9}]


def test_nonunique_single_updates_use_global_merged_natural_order(tmp_path):
    client = tinymongo.TinyMongoClient(
        str(tmp_path / "natural-order-store"),
        backend="sqlite-sharded",
        sqlite_shards=2,
    )
    collection = client.app.items
    engine = client.app.engine
    earlier_id = _id_for_shard(engine, 1, "earlier-higher-shard")
    later_id = _id_for_shard(engine, 0, "later-lower-shard")
    try:
        collection.insert_many(
            [
                {
                    "_id": earlier_id,
                    "group": "selected",
                    "state": "done",
                },
                {
                    "_id": later_id,
                    "group": "selected",
                    "state": "pending",
                },
            ]
        )

        result = collection.update_one(
            {"group": "selected"},
            {"$set": {"state": "done"}},
        )

        assert result.matched_count == 1
        assert result.modified_count == 0
        assert collection.find_one({"_id": later_id})["state"] == "pending"

        assert engine.update_many(
            "items",
            {"group": "selected"},
            {"$set": {"direct_backend_update": True}},
            multi=False,
        ) == [earlier_id]
        assert collection.find_one({"_id": earlier_id})["direct_backend_update"] is True
        assert "direct_backend_update" not in collection.find_one({"_id": later_id})
    finally:
        client.close()


def test_nonunique_direct_id_single_updates_keep_targeted_route(
    sharded_engine,
    monkeypatch,
):
    engine = sharded_engine
    document_id = _id_for_shard(engine, 1, "direct-id")
    engine.insert_many("items", [{"_id": document_id, "value": 0}])

    def unexpected_scatter(*_args, **_kwargs):
        raise AssertionError("an exact _id update must not scan every shard")

    monkeypatch.setattr(engine, "_find_existing", unexpected_scatter)

    assert engine.update_many(
        "items",
        {"_id": document_id},
        {"$inc": {"value": 1}},
        multi=False,
    ) == [document_id]
    assert engine.update_many_with_result(
        "items",
        {"_id": {"$eq": document_id}},
        {"$inc": {"value": 1}},
        multi=False,
    ) == (1, 1)
    assert engine._shards[1].find("items", {"_id": document_id})[0]["value"] == 2


def test_nonunique_updates_handle_an_empty_internal_route(
    sharded_engine,
    monkeypatch,
):
    engine = sharded_engine
    monkeypatch.setattr(engine, "_target_shards", lambda _filter: ())

    assert (
        engine.update_many(
            "items",
            {},
            {"$set": {"value": 1}},
            multi=False,
        )
        == []
    )
    assert engine.update_many_with_result(
        "items",
        {},
        {"$set": {"value": 1}},
        multi=False,
    ) == (0, 0)
    assert engine.update_many("items", {}, {"$set": {"value": 1}}) == []
    assert engine.update_many_with_result(
        "items",
        {},
        {"$set": {"value": 1}},
    ) == (0, 0)


def test_delete_ids_empty_and_grouped_across_shards(sharded_engine):
    engine = sharded_engine
    first_id = _id_for_shard(engine, 0, "delete-first")
    second_id = _id_for_shard(engine, 1, "delete-second")
    engine.insert_many("items", [{"_id": first_id}, {"_id": second_id}])

    assert engine.delete_ids("items", []) is None
    assert engine.delete_many("items", {"missing": True}, multi=True) == []
    engine.delete_ids("items", [first_id, second_id])

    assert engine.find("items") == []
