"""Deterministic concurrency contracts for experimental Sharded SQLite."""

import multiprocessing as mp
import queue
import sqlite3
import threading
import time
import traceback

import portalocker
import tinymongo

from tinymongo.bson_codec import dumps as bson_json_dumps


BACKEND = "sqlite-sharded"


def _open_client(root):
    return tinymongo.TinyMongoClient(
        str(root),
        backend=BACKEND,
        sqlite_shards=2,
    )


def _database_directory(root):
    return root / "app.sqlite-sharded"


def _shard_file(root, index):
    return (
        _database_directory(root) / "shards" / "{0:03d}".format(index) / "data.sqlite"
    )


def _shard_lock(root, index):
    return _shard_file(root, index).parent / ".tinymongo.lock"


def _id_for_shard(engine, shard_index, label):
    for candidate in range(10_000):
        document_id = "{0}-{1}".format(label, candidate)
        if engine._shard_index(document_id) == shard_index:
            return document_id
    raise AssertionError("could not find an id for shard {0}".format(shard_index))


def _insert_after_signal(root, document, ready, start, attempted, completed, result):
    client = None
    try:
        client = _open_client(root)
        collection = client.app.items
        # Complete all lazy initialization before the test holds a shard lock.
        collection.count_documents({})
        ready.set()
        if not start.wait(20):
            raise RuntimeError("timed out waiting for insert start signal")
        attempted.set()
        collection.insert_one(document)
        completed.set()
        result.put(("ok", document["_id"]))
    except BaseException:
        result.put(("error", traceback.format_exc()))
    finally:
        if client is not None:
            client.close()


def _hold_file_lock(lock_path, held, release, result):
    try:
        with portalocker.Lock(lock_path, mode="a", timeout=20):
            held.set()
            if not release.wait(20):
                raise RuntimeError("timed out waiting for shard-lock release")
        result.put(("ok", lock_path))
    except BaseException:
        result.put(("error", traceback.format_exc()))


def _wait_for_event(event, process, label, timeout=20):
    deadline = time.monotonic() + timeout
    while not event.wait(0.05):
        if process.exitcode is not None:
            raise AssertionError(
                "{0} process exited early with code {1}".format(
                    label,
                    process.exitcode,
                )
            )
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for {0}".format(label))


def _successful_result(result, label, timeout=20):
    try:
        status, payload = result.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError("timed out waiting for {0} result".format(label))
    assert status == "ok", payload
    return payload


def _stop_processes(processes):
    for process in processes:
        if process is not None and process.is_alive():
            process.terminate()
    for process in processes:
        if process is not None:
            process.join(timeout=5)


def test_different_shards_accept_independent_subprocess_writes(tmp_path):
    root = tmp_path / "striped-writes"
    setup = _open_client(root)
    engine = setup.app.engine
    warm_zero = _id_for_shard(engine, 0, "warm-zero")
    warm_one = _id_for_shard(engine, 1, "warm-one")
    blocked_id = _id_for_shard(engine, 0, "blocked")
    independent_id = _id_for_shard(engine, 1, "independent")
    setup.app.items.insert_many(
        [
            {"_id": warm_zero, "state": "warm"},
            {"_id": warm_one, "state": "warm"},
        ]
    )
    setup.close()
    assert _shard_lock(root, 0).is_file()
    assert _shard_lock(root, 1).is_file()

    context = mp.get_context("spawn")
    ready_zero = context.Event()
    ready_one = context.Event()
    start_zero = context.Event()
    start_one = context.Event()
    attempted_zero = context.Event()
    attempted_one = context.Event()
    completed_zero = context.Event()
    completed_one = context.Event()
    result_zero = context.Queue()
    result_one = context.Queue()
    lock_held = context.Event()
    release_lock = context.Event()
    holder_result = context.Queue()
    writer_zero = context.Process(
        target=_insert_after_signal,
        args=(
            root,
            {"_id": blocked_id, "state": "inserted"},
            ready_zero,
            start_zero,
            attempted_zero,
            completed_zero,
            result_zero,
        ),
    )
    writer_one = context.Process(
        target=_insert_after_signal,
        args=(
            root,
            {"_id": independent_id, "state": "inserted"},
            ready_one,
            start_one,
            attempted_one,
            completed_one,
            result_one,
        ),
    )
    holder = context.Process(
        target=_hold_file_lock,
        args=(str(_shard_lock(root, 0)), lock_held, release_lock, holder_result),
    )
    processes = [writer_zero, writer_one, holder]

    try:
        writer_zero.start()
        writer_one.start()
        _wait_for_event(ready_zero, writer_zero, "shard 0 writer readiness")
        _wait_for_event(ready_one, writer_one, "shard 1 writer readiness")

        holder.start()
        _wait_for_event(lock_held, holder, "external shard 0 lock")
        start_zero.set()
        start_one.set()
        _wait_for_event(attempted_zero, writer_zero, "shard 0 insert attempt")
        _wait_for_event(attempted_one, writer_one, "shard 1 insert attempt")

        assert _successful_result(result_one, "shard 1 insert") == independent_id
        assert completed_one.is_set()
        assert not completed_zero.is_set()
        assert writer_zero.is_alive()

        release_lock.set()
        _wait_for_event(completed_zero, writer_zero, "shard 0 insert completion")
        assert _successful_result(result_zero, "shard 0 insert") == blocked_id
        assert _successful_result(holder_result, "lock holder") == str(
            _shard_lock(root, 0)
        )
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
    finally:
        release_lock.set()
        _stop_processes(processes)
        for result in (result_zero, result_one, holder_result):
            result.close()
            result.join_thread()

    verify = _open_client(root)
    try:
        assert verify.app.items.find_one({"_id": independent_id})["state"] == "inserted"
        assert verify.app.items.find_one({"_id": blocked_id})["state"] == "inserted"
    finally:
        verify.close()


def test_wal_reader_sees_last_commit_during_raw_write_transaction(tmp_path):
    root = tmp_path / "wal-reader"
    client = _open_client(root)
    database = client.app
    document_id = _id_for_shard(database.engine, 0, "wal-document")
    database.items.insert_one({"_id": document_id, "value": "committed"})
    shard_path = _shard_file(root, 0)

    writer = sqlite3.connect(str(shard_path), timeout=1, isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        writer.execute("BEGIN IMMEDIATE")
        payload = bson_json_dumps(
            {"_id": document_id, "value": "not committed"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        assert (
            writer.execute(
                'UPDATE "items" SET data = ?',
                (payload,),
            ).rowcount
            == 1
        )
        assert writer.in_transaction is True

        assert database.items.find_one({"_id": document_id}) == {
            "_id": document_id,
            "value": "committed",
        }
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()
        client.close()


def test_client_use_starts_no_background_threads_or_processes(tmp_path):
    threads_before = {
        (thread.ident, thread.name)
        for thread in threading.enumerate()
        if thread.is_alive()
    }
    children_before = {
        process.pid for process in mp.active_children() if process.is_alive()
    }

    client = _open_client(tmp_path / "no-workers")
    try:
        client.app.items.insert_one({"_id": "foreground", "value": 1})
        assert client.app.items.find_one({"_id": "foreground"})["value"] == 1

        assert {
            (thread.ident, thread.name)
            for thread in threading.enumerate()
            if thread.is_alive()
        } == threads_before
        assert {
            process.pid for process in mp.active_children() if process.is_alive()
        } == children_before
    finally:
        client.close()
