import multiprocessing as mp
import os
import time

import pytest

import tinymongo as tm


def _env_int(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _writer_process(db_dir, backend, proc_id, writes_per_proc, start, errors):
    try:
        start.wait(timeout=30)
        client = tm.TinyMongoClient(db_dir, backend=backend)
        collection = client.integrationDB.concurrentWrites
        docs = [
            {
                "_id": "{0}-{1}".format(proc_id, index),
                "proc": proc_id,
                "index": index,
                "value": proc_id * writes_per_proc + index,
            }
            for index in range(writes_per_proc)
        ]
        collection.insert_many(docs)
    except Exception as exc:
        errors.put("{0}: {1}".format(type(exc).__name__, exc))
        raise


@pytest.mark.integration
def test_concurrent_bulk_writes_scale(tmp_path):
    backend = os.environ.get("TINYMONGO_INTEGRATION_BACKEND", "tinydb")
    processes = _env_int("TINYMONGO_INTEGRATION_PROCS", 32)
    writes_per_proc = _env_int("TINYMONGO_INTEGRATION_WRITES_PER_PROC", 100)
    expected = processes * writes_per_proc

    db_dir = str(tmp_path / "tinymongo-stress")
    start = mp.Barrier(processes)
    errors = mp.Queue()
    workers = [
        mp.Process(
            target=_writer_process,
            args=(db_dir, backend, proc_id, writes_per_proc, start, errors),
        )
        for proc_id in range(processes)
    ]

    started = time.monotonic()
    for worker in workers:
        worker.start()

    for worker in workers:
        worker.join(timeout=120)

    failed = [worker.exitcode for worker in workers if worker.exitcode != 0]
    while not errors.empty():
        failed.append(errors.get())

    for worker in workers:
        if worker.is_alive():
            worker.terminate()
            failed.append("timeout")

    assert failed == []

    client = tm.TinyMongoClient(db_dir, backend=backend)
    collection = client.integrationDB.concurrentWrites
    docs = list(collection.find({}))
    values = sorted(doc["value"] for doc in docs)

    assert len(docs) == expected
    assert len({doc["_id"] for doc in docs}) == expected
    assert values == list(range(expected))
    assert time.monotonic() - started < 120


def _single_insert_writer(db_dir, backend, proc_id, writes_per_proc, start, errors):
    try:
        start.wait(timeout=30)
        client = tm.TinyMongoClient(db_dir, backend=backend)
        collection = client.integrationDB.singleInserts
        for index in range(writes_per_proc):
            collection.insert_one(
                {
                    "_id": "{0}-{1}".format(proc_id, index),
                    "proc": proc_id,
                    "index": index,
                }
            )
    except Exception as exc:
        errors.put("{0}: {1}".format(type(exc).__name__, exc))
        raise


@pytest.mark.integration
def test_concurrent_single_writes_smoke(tmp_path):
    backend = os.environ.get("TINYMONGO_INTEGRATION_BACKEND", "tinydb")
    processes = _env_int("TINYMONGO_INTEGRATION_SINGLE_PROCS", 8)
    writes_per_proc = _env_int("TINYMONGO_INTEGRATION_SINGLE_WRITES_PER_PROC", 25)
    expected = processes * writes_per_proc

    db_dir = str(tmp_path / "tinymongo-single-stress")
    start = mp.Barrier(processes)
    errors = mp.Queue()
    workers = [
        mp.Process(
            target=_single_insert_writer,
            args=(db_dir, backend, proc_id, writes_per_proc, start, errors),
        )
        for proc_id in range(processes)
    ]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)

    failed = [worker.exitcode for worker in workers if worker.exitcode != 0]
    while not errors.empty():
        failed.append(errors.get())

    for worker in workers:
        if worker.is_alive():
            worker.terminate()
            failed.append("timeout")

    assert failed == []

    docs = list(
        tm.TinyMongoClient(db_dir, backend=backend).integrationDB.singleInserts.find({})
    )
    assert len(docs) == expected
    assert len({doc["_id"] for doc in docs}) == expected
