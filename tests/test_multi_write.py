import os
import multiprocessing as mp
import time
import shutil
import random

import tinymongo as tm


DB_DIR = os.path.abspath("./test_db_multi")


def writer_process(proc_id, count, start_value=0):
    client = tm.TinyMongoClient(DB_DIR)
    db = client.multiDB
    coll = db.multiCollection

    items = []
    for i in range(count):
        items.append({"count": start_value + proc_id * count + i})

    # insert many
    coll.insert_many(items)


def test_multi_writer_stress():
    # ensure clean dir
    if os.path.exists(DB_DIR):
        try:
            shutil.rmtree(DB_DIR)
        except Exception:
            pass

    num_procs = 6
    per_proc = 50

    procs = []
    for pid in range(num_procs):
        p = mp.Process(target=writer_process, args=(pid, per_proc, 0))
        p.start()
        procs.append(p)

    for p in procs:
        p.join()

    # validate
    client = tm.TinyMongoClient(DB_DIR)
    coll = client.multiDB.multiCollection
    all_docs = list(coll.find())
    assert len(all_docs) == num_procs * per_proc

    # check uniqueness
    counts = sorted(d["count"] for d in all_docs)
    assert counts == list(range(0, num_procs * per_proc))
