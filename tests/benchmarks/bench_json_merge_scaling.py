"""TM-049: cost of one insert as its collection or an untouched neighbour grows.

Run from the repository: python -m tests.benchmarks.bench_json_merge_scaling
Each row uses an isolated temporary database; no external services are needed.
"""

import argparse
import statistics
import tempfile
import time
from pathlib import Path

from tinymongo import TinyMongoClient
from tinymongo.storage_backends import clear_memory_namespace


def measure(backend, size, neighbour, repeats):
    with tempfile.TemporaryDirectory(prefix="tm049-") as directory:
        address = str(Path(directory) / "data")
        with TinyMongoClient(address, backend=backend) as client:
            db = client.app
            growing = db.archive if neighbour else db.docs
            growing.insert_many([{"_id": i, "body": "x" * 400} for i in range(size)])
            if neighbour:
                db.docs.insert_many([{"_id": i} for i in range(200)])
            times = []
            for i in range(repeats):
                started = time.perf_counter()
                db.docs.insert_one({"_id": size + 200 + i, "body": "new"})
                times.append((time.perf_counter() - started) * 1000)
        if backend == "memory":
            clear_memory_namespace(address)
        return statistics.median(times)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[500, 1000, 2000, 4000])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    print("backend case documents median_insert_ms")
    for backend in ("json", "memory"):
        for neighbour in (False, True):
            for size in args.sizes:
                print(
                    backend,
                    "neighbour" if neighbour else "target",
                    size,
                    round(measure(backend, size, neighbour, args.repeats), 3),
                    flush=True,
                )


if __name__ == "__main__":
    main()
