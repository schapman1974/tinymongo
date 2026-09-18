"""The documented startup probes actually prepare and refresh SQLite keys."""

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bson import ObjectId

from examples.sqlite_warmup import warm_sqlite_queries
from tinymongo import TinyMongoClient, table_backends


DATE = datetime(2026, 1, 1)
AUTHOR = ObjectId("000000000000000000000001")
OTHER_AUTHOR = ObjectId("000000000000000000000002")


def _uncomputed_key_counts(collection):
    # Inspect durable state rather than assuming a consumed cursor warmed it.
    with sqlite3.connect(collection.parent.engine.path) as connection:
        columns = [
            row[1]
            for row in connection.execute('PRAGMA table_info("articles")')
            if row[1].endswith("_bson_key_v2")
        ]
        return [
            connection.execute(
                'SELECT COUNT(*) FROM "articles" WHERE "{0}" IS NULL'.format(column)
            ).fetchone()[0]
            for column in columns
        ]


@pytest.mark.parametrize("matching_probes", [True, False])
def test_startup_warmup_persists_keys_and_refreshes_later_writes(
    tmp_path, monkeypatch, matching_probes
):
    probes = [
        {
            "published_at": {
                "$gte": DATE if matching_probes else DATE + timedelta(days=100)
            }
        },
        {"author_id": AUTHOR if matching_probes else OTHER_AUTHOR},
    ]
    computed = []
    original = table_backends._sqlite_bson_query_key_from_row

    def record_computation(data, field):
        computed.append(field)
        return original(data, field)

    monkeypatch.setattr(
        table_backends, "_sqlite_bson_query_key_from_row", record_computation
    )
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        articles = client.app.articles
        articles.insert_many(
            [
                {
                    "_id": number,
                    "published_at": DATE + timedelta(days=number),
                    "author_id": AUTHOR,
                }
                for number in range(3)
            ]
        )
        articles.create_index("published_at")
        articles.create_index("author_id")
        assert _uncomputed_key_counts(articles) == []

        warm_sqlite_queries(articles, probes)
        assert _uncomputed_key_counts(articles) == [0, 0]
        assert computed.count("published_at") == 3
        assert computed.count("author_id") == 3

    computed.clear()
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        articles = client.app.articles
        assert _uncomputed_key_counts(articles) == [0, 0]
        warm_sqlite_queries(articles, probes)
        assert computed == []  # Restarting does not rebuild existing keys.

        changed = {"published_at": DATE + timedelta(days=20), "author_id": OTHER_AUTHOR}
        articles.update_one({"_id": 0}, {"$set": changed})
        articles.insert_one(dict(changed, _id=3))
        assert _uncomputed_key_counts(articles) == [2, 2]

        warm_sqlite_queries(articles, probes)
        assert _uncomputed_key_counts(articles) == [0, 0]
        assert computed.count("published_at") == 2
        assert computed.count("author_id") == 2
        assert [row["_id"] for row in articles.find({"author_id": OTHER_AUTHOR})] == [
            0,
            3,
        ]
        assert articles.count_documents({"published_at": changed["published_at"]}) == 2


def test_sqlite_warmup_example_runs_as_documented():
    result = subprocess.run(
        [sys.executable, "-m", "examples.sqlite_warmup"],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert result.stdout.strip() == "Startup warm-up finished for 3 example articles."


def test_documented_collection_listing_performs_legacy_index_cleanup(tmp_path):
    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        articles = client.app.articles
        articles.insert_one({"_id": 1, "author_id": AUTHOR})
        articles.create_index("author_id")
        engine = articles.parent.engine
        path = engine.path
        spec = engine.get_index_specs("articles")[0]
        legacy_name = engine._physical_index_name("articles", spec) + "_bson_v1"
    with sqlite3.connect(path) as connection:
        connection.create_function(
            "tinymongo_bson_query_key_v1",
            2,
            table_backends._sqlite_bson_query_key_from_row,
            deterministic=True,
        )
        connection.execute(
            'CREATE INDEX "{0}" ON articles '
            "(tinymongo_bson_query_key_v1(data, 'author_id'))".format(legacy_name)
        )

    with TinyMongoClient(str(tmp_path), backend="sqlite") as client:
        database = client["app"]
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE name = ?", (legacy_name,)
            ).fetchone() == (legacy_name,)
        assert database.list_collection_names() == ["articles"]
        with sqlite3.connect(path) as connection:
            assert (
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = ?", (legacy_name,)
                ).fetchone()
                is None
            )
            connection.execute("VACUUM")
            connection.execute("REINDEX")
            connection.execute("UPDATE articles SET data = data")
