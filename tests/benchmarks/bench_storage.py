import time
import shutil
import os
from tinydb import TinyDB
from tinydb.storages import JSONStorage

import tinymongo as tm


def _time_parquet(n=1000):
    d = os.path.abspath("./bench_parquet_db")
    if os.path.exists(d):
        shutil.rmtree(d)
    os.makedirs(d, exist_ok=True)

    client = tm.TinyMongoClient(d)
    c = client.bench.collection

    docs = [{"i": i, "v": str(i)} for i in range(n)]
    t0 = time.time()
    c.insert_many(docs)
    t1 = time.time()
    return t1 - t0


def _time_json(n=1000):
    d = os.path.abspath("./bench_json_db")
    if os.path.exists(d):
        shutil.rmtree(d)
    os.makedirs(d, exist_ok=True)

    path = os.path.join(d, "bench.json")
    db = TinyDB(path, storage=JSONStorage)
    table = db.table("collection")
    docs = [{"i": i, "v": str(i)} for i in range(n)]
    t0 = time.time()
    table.insert_multiple(docs)
    t1 = time.time()
    return t1 - t0


def run_benchmarks():
    for n in (100, 1000):
        p = _time_parquet(n)
        j = _time_json(n)
        print(f"n={n} parquet={p:.3f}s json={j:.3f}s")


if __name__ == "__main__":
    run_benchmarks()
