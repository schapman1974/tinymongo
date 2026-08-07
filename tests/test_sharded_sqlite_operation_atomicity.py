"""Deterministic operation-lock contracts for sharded SQLite."""

import threading

import tinymongo


def _open_collection(tmp_path, name):
    client = tinymongo.TinyMongoClient(
        str(tmp_path / name),
        backend="sqlite-sharded",
        sqlite_shards=2,
    )
    return client, client.app.items


def _id_for_shard(engine, shard_index, label):
    for candidate in range(10_000):
        document_id = "{0}-{1}".format(label, candidate)
        if engine._shard_index(document_id) == shard_index:
            return document_id
    raise AssertionError("could not find an id for shard {0}".format(shard_index))


def _launch(name, callback):
    call = {"done": threading.Event()}

    def invoke():
        try:
            call["value"] = callback()
        except BaseException as exc:  # pragma: no cover - surfaced below
            call["error"] = exc
        finally:
            call["done"].set()

    call["thread"] = threading.Thread(target=invoke, name=name)
    call["thread"].start()
    return call


def _assert_call_finished(call, timeout=10):
    assert call["done"].wait(timeout), "worker did not finish"
    call["thread"].join(timeout=timeout)
    assert not call["thread"].is_alive()
    assert "error" not in call, repr(call.get("error"))
    return call.get("value")


def _assert_competitor_waits(attempted, call):
    assert attempted.wait(5), "competing operation did not start"
    assert not call["done"].wait(0.2), "competing operation escaped the shard lock"


def test_conditional_replace_cannot_overwrite_a_competing_version_change(
    tmp_path,
    monkeypatch,
):
    client, collection = _open_collection(tmp_path, "conditional-replace")
    engine = collection.parent.engine
    document_id = _id_for_shard(engine, 0, "versioned")
    collection.insert_one({"_id": document_id, "version": 1, "payload": "original"})
    replacement_selected = threading.Event()
    release_replace = threading.Event()
    competitor_attempted = threading.Event()
    original_validate = engine.validate_unique_post_image

    def pause_after_selection(*args, **kwargs):
        if threading.current_thread().name == "conditional-replace":
            replacement_selected.set()
            if not release_replace.wait(10):
                raise AssertionError("timed out releasing conditional replace")
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(engine, "validate_unique_post_image", pause_after_selection)
    replace_call = _launch(
        "conditional-replace",
        lambda: collection.replace_one(
            {"_id": document_id, "version": 1},
            {"version": 1, "payload": "replacement"},
        ),
    )
    competitor_call = None
    try:
        assert replacement_selected.wait(5), "replace did not finish its preflight"

        def change_version():
            competitor_attempted.set()
            return collection.update_one(
                {"_id": document_id},
                {"$set": {"version": 2, "competitor": True}},
            )

        competitor_call = _launch("version-change", change_version)
        _assert_competitor_waits(competitor_attempted, competitor_call)
    finally:
        release_replace.set()

    try:
        replaced = _assert_call_finished(replace_call)
        changed = _assert_call_finished(competitor_call)
        assert replaced.matched_count == 1
        assert changed.matched_count == 1
        assert collection.find_one({"_id": document_id}) == {
            "_id": document_id,
            "version": 2,
            "payload": "replacement",
            "competitor": True,
        }
    finally:
        client.close()


def test_find_one_and_update_keeps_mutation_and_after_read_atomic(
    tmp_path,
    monkeypatch,
):
    client, collection = _open_collection(tmp_path, "find-update")
    engine = collection.parent.engine
    document_id = _id_for_shard(engine, 0, "find-update")
    collection.insert_one({"_id": document_id, "phase": "original"})
    operation_mutated = threading.Event()
    release_operation = threading.Event()
    competitor_attempted = threading.Event()
    original_update_one = collection.update_one

    def pause_after_nested_update(*args, **kwargs):
        result = original_update_one(*args, **kwargs)
        if threading.current_thread().name == "find-one-update":
            operation_mutated.set()
            if not release_operation.wait(10):
                raise AssertionError("timed out releasing find_one_and_update")
        return result

    monkeypatch.setattr(collection, "update_one", pause_after_nested_update)
    operation_call = _launch(
        "find-one-update",
        lambda: collection.find_one_and_update(
            {"_id": document_id},
            {"$set": {"phase": "operation"}},
            return_document=True,
        ),
    )
    competitor_call = None
    try:
        assert operation_mutated.wait(5), "operation did not commit its update"

        def compete():
            competitor_attempted.set()
            return original_update_one(
                {"_id": document_id},
                {"$set": {"phase": "competitor"}},
            )

        competitor_call = _launch("find-update-competitor", compete)
        _assert_competitor_waits(competitor_attempted, competitor_call)
    finally:
        release_operation.set()

    try:
        returned = _assert_call_finished(operation_call)
        changed = _assert_call_finished(competitor_call)
        assert returned == {"_id": document_id, "phase": "operation"}
        assert changed.matched_count == 1
        assert collection.find_one({"_id": document_id})["phase"] == "competitor"
    finally:
        client.close()


def test_find_one_and_replace_keeps_selection_mutation_and_before_atomic(
    tmp_path,
    monkeypatch,
):
    client, collection = _open_collection(tmp_path, "find-replace")
    engine = collection.parent.engine
    document_id = _id_for_shard(engine, 0, "find-replace")
    collection.insert_one({"_id": document_id, "phase": "original"})
    selection_complete = threading.Event()
    release_operation = threading.Event()
    competitor_attempted = threading.Event()
    original_replace_one = collection.replace_one
    original_update_one = collection.update_one

    def pause_before_nested_replace(*args, **kwargs):
        if threading.current_thread().name == "find-one-replace":
            selection_complete.set()
            if not release_operation.wait(10):
                raise AssertionError("timed out releasing find_one_and_replace")
        return original_replace_one(*args, **kwargs)

    monkeypatch.setattr(collection, "replace_one", pause_before_nested_replace)
    operation_call = _launch(
        "find-one-replace",
        lambda: collection.find_one_and_replace(
            {"_id": document_id},
            {"phase": "replacement"},
        ),
    )
    competitor_call = None
    try:
        assert selection_complete.wait(5), "replace selection did not complete"

        def compete():
            competitor_attempted.set()
            return original_update_one(
                {"_id": document_id},
                {"$set": {"competitor": True}},
            )

        competitor_call = _launch("find-replace-competitor", compete)
        _assert_competitor_waits(competitor_attempted, competitor_call)
    finally:
        release_operation.set()

    try:
        returned = _assert_call_finished(operation_call)
        changed = _assert_call_finished(competitor_call)
        assert returned == {"_id": document_id, "phase": "original"}
        assert changed.matched_count == 1
        assert collection.find_one({"_id": document_id}) == {
            "_id": document_id,
            "phase": "replacement",
            "competitor": True,
        }
    finally:
        client.close()


def test_find_one_and_delete_keeps_selection_mutation_and_before_atomic(
    tmp_path,
    monkeypatch,
):
    client, collection = _open_collection(tmp_path, "find-delete")
    engine = collection.parent.engine
    document_id = _id_for_shard(engine, 0, "find-delete")
    collection.insert_one({"_id": document_id, "phase": "original"})
    selection_complete = threading.Event()
    release_operation = threading.Event()
    competitor_attempted = threading.Event()
    original_delete_one = collection.delete_one
    original_update_one = collection.update_one

    def pause_before_nested_delete(*args, **kwargs):
        if threading.current_thread().name == "find-one-delete":
            selection_complete.set()
            if not release_operation.wait(10):
                raise AssertionError("timed out releasing find_one_and_delete")
        return original_delete_one(*args, **kwargs)

    monkeypatch.setattr(collection, "delete_one", pause_before_nested_delete)
    operation_call = _launch(
        "find-one-delete",
        lambda: collection.find_one_and_delete({"_id": document_id}),
    )
    competitor_call = None
    try:
        assert selection_complete.wait(5), "delete selection did not complete"

        def compete():
            competitor_attempted.set()
            return original_update_one(
                {"_id": document_id},
                {"$set": {"competitor": True}},
            )

        competitor_call = _launch("find-delete-competitor", compete)
        _assert_competitor_waits(competitor_attempted, competitor_call)
    finally:
        release_operation.set()

    try:
        returned = _assert_call_finished(operation_call)
        changed = _assert_call_finished(competitor_call)
        assert returned == {"_id": document_id, "phase": "original"}
        assert changed.matched_count == 0
        assert collection.find_one({"_id": document_id}) is None
    finally:
        client.close()


def test_broad_and_sorted_find_and_modify_use_logical_cross_shard_order(tmp_path):
    client, collection = _open_collection(tmp_path, "logical-order")
    engine = collection.parent.engine
    natural_first = _id_for_shard(engine, 1, "natural-first")
    natural_later = _id_for_shard(engine, 0, "natural-later")
    sorted_lower = _id_for_shard(engine, 1, "sorted-lower")
    sorted_higher = _id_for_shard(engine, 0, "sorted-higher")
    try:
        collection.insert_many(
            [
                {"_id": natural_first, "group": "natural", "rank": 1},
                {"_id": natural_later, "group": "natural", "rank": 2},
                {"_id": sorted_lower, "group": "sorted", "rank": 1},
                {"_id": sorted_higher, "group": "sorted", "rank": 10},
            ]
        )

        previous = collection.find_one_and_replace(
            {"group": "natural"},
            {"group": "natural", "rank": 3, "replaced": True},
        )
        after = collection.find_one_and_update(
            {"group": "sorted"},
            {"$set": {"selected": True}},
            sort=[("rank", -1)],
            return_document=True,
        )

        assert previous["_id"] == natural_first
        assert collection.find_one({"_id": natural_later})["rank"] == 2
        assert after["_id"] == sorted_higher
        assert "selected" not in collection.find_one({"_id": sorted_lower})
    finally:
        client.close()


def test_exact_id_operations_on_different_shards_remain_independent(
    tmp_path,
    monkeypatch,
):
    client, collection = _open_collection(tmp_path, "independent-exact-ids")
    engine = collection.parent.engine
    blocked_id = _id_for_shard(engine, 0, "blocked")
    independent_id = _id_for_shard(engine, 1, "independent")
    collection.insert_many(
        [
            {"_id": blocked_id, "value": 0},
            {"_id": independent_id, "value": 0},
        ]
    )
    blocked_mutated = threading.Event()
    release_blocked = threading.Event()
    original_update = engine.update_many_with_result

    def pause_blocked_shard(*args, **kwargs):
        result = original_update(*args, **kwargs)
        if threading.current_thread().name == "blocked-shard":
            blocked_mutated.set()
            if not release_blocked.wait(10):
                raise AssertionError("timed out releasing blocked shard")
        return result

    monkeypatch.setattr(engine, "update_many_with_result", pause_blocked_shard)
    blocked_call = _launch(
        "blocked-shard",
        lambda: collection.update_one(
            {"_id": blocked_id},
            {"$inc": {"value": 1}},
        ),
    )
    independent_call = None
    try:
        assert blocked_mutated.wait(5), "first shard did not reach its pause"
        independent_call = _launch(
            "independent-shard",
            lambda: collection.update_one(
                {"_id": independent_id},
                {"$inc": {"value": 1}},
            ),
        )
        independent = _assert_call_finished(independent_call, timeout=5)
        assert independent.modified_count == 1
        assert not blocked_call["done"].is_set()
        assert collection.find_one({"_id": independent_id})["value"] == 1
    finally:
        release_blocked.set()

    try:
        blocked = _assert_call_finished(blocked_call)
        assert blocked.modified_count == 1
        assert collection.find_one({"_id": blocked_id})["value"] == 1
    finally:
        client.close()
