import os
import multiprocessing as mp
import shutil
import time

import tinymongo as tm


def writer_process(db_dir, proc_id, count, start_value=0):
    with tm.TinyMongoClient(db_dir) as client:
        coll = client.multiDB.multiCollection
        items = [{"count": start_value + proc_id * count + i} for i in range(count)]
        coll.insert_many(items)


def test_multi_writer_stress(tmp_path):
    db_dir = str(tmp_path / "multi")
    # ensure clean dir
    if os.path.exists(db_dir):
        try:
            shutil.rmtree(db_dir)
        except Exception:
            pass

    num_procs = 6
    per_proc = 50

    procs = []
    for pid in range(num_procs):
        p = mp.Process(target=writer_process, args=(db_dir, pid, per_proc, 0))
        p.start()
        procs.append(p)

    deadline = time.monotonic() + 60
    for process in procs:
        process.join(timeout=max(0, deadline - time.monotonic()))

    timed_out = [process for process in procs if process.is_alive()]
    for process in timed_out:
        process.terminate()
        process.join(timeout=5)

    assert timed_out == []
    assert [process.exitcode for process in procs] == [0] * num_procs

    # validate
    with tm.TinyMongoClient(db_dir) as client:
        all_docs = list(client.multiDB.multiCollection.find())
    assert len(all_docs) == num_procs * per_proc

    # check uniqueness
    counts = sorted(d["count"] for d in all_docs)
    assert counts == list(range(0, num_procs * per_proc))
