"""Issue #176 axis C: fixed writes beside an untouched growing byte payload.

Run with python -m tests.benchmarks.bench_json_resident_payload.
Uses the reporter's async API, 400 archive rows, 200 target rows, and fixed
400-byte inserted documents. Each case checks that target and archive document counts are preserved.
"""

import argparse
import asyncio
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path

from tinymongo import AsyncMongoClient
from tinymongo.storage_backends import clear_memory_namespace


async def measure(backend, payload, repeats):
    with tempfile.TemporaryDirectory(prefix="tm176-bytes-") as directory:
        address = str(Path(directory) / "data")
        try:
            async with AsyncMongoClient(
                tinymongo_folder=address, backend=backend
            ) as client:
                db = client["app"]
                for start in range(0, 400, 250):
                    await db["archive"].insert_many(
                        [
                            {"_id": f"archive-{i}", "body": "x" * payload}
                            for i in range(start, min(start + 250, 400))
                        ],
                        ordered=False,
                    )
                target = db["docs"]
                await target.insert_many(
                    [{"_id": f"doc-{i}", "body": "x" * 400} for i in range(200)],
                    ordered=False,
                )
                times = []
                for i in range(repeats):
                    started = time.perf_counter()
                    await target.insert_one({"_id": f"probe-{i}", "body": "x" * 400})
                    times.append((time.perf_counter() - started) * 1000)
                assert await target.count_documents({}) == 200 + repeats
                assert await db["archive"].count_documents({}) == 400
                return statistics.median(times)
        finally:
            if backend == "memory":
                clear_memory_namespace(address)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backends", nargs="+", default=["json", "memory", "sqlite"])
    parser.add_argument("--payloads", nargs="+", type=int, default=[1024, 524288])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    print(
        json.dumps(
            {"python": platform.python_version(), "platform": platform.platform()}
        )
    )
    for backend in args.backends:
        for payload in args.payloads:
            elapsed = await measure(backend, payload, args.repeats)
            print(
                json.dumps(
                    {
                        "backend": backend,
                        "archive_payload_bytes": payload,
                        "median_insert_ms": elapsed,
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
