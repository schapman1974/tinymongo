"""Warm SQLite query keys during application startup using the public API.

Run this temporary-store demonstration from the repository root:

    python -m pip install -e '.[bson]'
    python -m examples.sqlite_warmup

For your application, finish schema/data migrations and declare the indexes,
then call ``warm_sqlite_queries`` before accepting traffic. First use may scan
the whole collection, write keys, and hold the SQLite write lock. ``limit(1)``
bounds returned documents; it does not limit that setup work.
"""

import tempfile
from datetime import datetime, timedelta

from bson import ObjectId

from tinymongo import TinyMongoClient


def warm_sqlite_queries(collection, queries):
    """Execute one representative indexed read per field to warm query keys.

    Use separate filters for declared, non-partial indexes whose leading fields
    are top-level BSON scalars or dates. Combining fields in one filter may warm
    only one index. A probe need not match a document to warm a populated
    collection. Empty collections have no keys to compute; later writes leave
    keys for the next relevant read to refresh.

    Creating a cursor alone is insufficient: consume it to execute the read.
    Repeating these probes after a restart reuses persisted keys, and repeating
    them after writes refreshes invalidated keys.
    """
    for query in queries:
        list(collection.find(query, {"_id": 1}).limit(1))


def run_example():
    """Seed and warm an isolated SQLite store, then simulate serving a read."""
    published_at = datetime(2026, 1, 1)
    author_id = ObjectId("000000000000000000000001")
    with tempfile.TemporaryDirectory(prefix="tinymongo-sqlite-warmup-") as path:
        with TinyMongoClient(path, backend="sqlite") as client:
            database = client.app
            # Opening a handle is lazy; this operation performs upgrade cleanup.
            database.list_collection_names()
            articles = database.articles

            # Application schema/data migrations happen before warm-up.
            articles.insert_many(
                [
                    {
                        "_id": number,
                        "published_at": published_at + timedelta(days=number),
                        "author_id": author_id,
                        "title": "Example article {0}".format(number),
                    }
                    for number in range(3)
                ]
            )
            articles.create_index("published_at")
            articles.create_index("author_id")

            warm_sqlite_queries(
                articles,
                [
                    {"published_at": {"$gte": published_at}},
                    {"author_id": author_id},
                ],
            )

            # Start accepting application traffic only after warm-up returns.
            return articles.count_documents({"author_id": author_id})


if __name__ == "__main__":
    count = run_example()
    print("Startup warm-up finished for {0} example articles.".format(count))
