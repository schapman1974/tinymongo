"""TM-042 cold/warm indexed reads; isolated synthetic data, no external services.

PYTHONPATH=. python tests/benchmarks/bench_sqlite_tm042.py --sizes 2000 20000
Run the same script with PYTHONPATH pointing at an older checkout for an A/B.
"""

import argparse
import json
import statistics
import tempfile
import time
from datetime import datetime, timedelta

from bson import Binary, Decimal128, Int64, ObjectId
from tinymongo import TinyMongoClient


DATE = datetime(2026, 1, 1)


def oid(number):
    return ObjectId(number.to_bytes(12, "big"))


def timed(call):
    started = time.perf_counter()
    result = call()
    return (time.perf_counter() - started) * 1000, result


def measure(size, repeats):
    with tempfile.TemporaryDirectory(prefix="tm042-") as directory:
        with TinyMongoClient(directory, backend="sqlite") as client:
            col = client.app.docs
            col.insert_many(
                [
                    {
                        "_id": i,
                        "oid": oid(i),
                        "date": DATE + timedelta(seconds=i),
                        "number": i,
                        "decimal": Decimal128(str(i)),
                        "binary": Binary(i.to_bytes(4, "big"), 128),
                        "body": "x" * 200,
                    }
                    for i in range(size)
                ]
            )
            target = size // 2
            queries = {
                "objectid_eq": {"oid": oid(target)},
                "datetime_eq": {"date": DATE + timedelta(seconds=target)},
                "int64_eq": {
                    "$and": [{"number": Int64(target)}, {"body": {"$ne": ""}}]
                },
                "decimal_eq": {"decimal": Decimal128(str(target))},
                "binary_eq": {"binary": Binary(target.to_bytes(4, "big"), 128)},
                "date_range": {
                    "date": {
                        "$gte": DATE + timedelta(seconds=target),
                        "$lt": DATE + timedelta(seconds=target + 3),
                    }
                },
                "numeric_range": {"number": {"$gte": target, "$lt": target + 3}},
                "indexed_or": {"$or": [{"oid": oid(target)}, {"number": target + 1}]},
            }
            for field in ("oid", "date", "number", "decimal", "binary"):
                col.create_index(field)
            before_write, _ = timed(
                lambda: col.update_one({"_id": 0}, {"$set": {"note": "before"}})
            )
            for name, query in queries.items():
                cold, documents = timed(lambda: list(col.find(query)))
                expected = (
                    3 if name.endswith("range") else 2 if name == "indexed_or" else 1
                )
                assert len(documents) == expected
                samples = [
                    timed(lambda: list(col.find(query)))[0] for _ in range(repeats)
                ]
                print(
                    json.dumps(
                        {
                            "documents": size,
                            "query": name,
                            "cold_ms": round(cold, 3),
                            "median_warm_ms": round(statistics.median(samples), 3),
                        }
                    ),
                    flush=True,
                )
            writes = [
                timed(lambda: col.update_one({"_id": 0}, {"$inc": {"counter": 1}}))[0]
                for _ in range(repeats)
            ]
            print(
                json.dumps(
                    {
                        "documents": size,
                        "query": "point_update",
                        "before_derived_indexes_ms": round(before_write, 3),
                        "median_warm_ms": round(statistics.median(writes), 3),
                    }
                ),
                flush=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[2000, 20000])
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    for size in args.sizes:
        measure(size, args.repeats)


if __name__ == "__main__":
    main()
