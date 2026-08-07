import asyncio
import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
import tinymongo
import tinymongo.sharded_sqlite as sharded_sqlite

from tinymongo.errors import (
    BulkWriteError,
    ConfigurationError,
    DuplicateKeyError,
    StorageCorruptionError,
)
from tinymongo.sharded_sqlite import ShardedSQLiteTableBackend
from tinymongo.table_backends import _physical_id_key


BACKEND = "sqlite-sharded"


def _open_client(root, **options):
    return tinymongo.TinyMongoClient(
        str(root),
        backend=BACKEND,
        **options,
    )


def _database_directory(root, database="app"):
    return root / "{0}.sqlite-sharded".format(database)


def _shard_file(root, index, database="app"):
    return (
        _database_directory(root, database)
        / "shards"
        / "{0:03d}".format(index)
        / "data.sqlite"
    )


def _id_for_shard(engine, shard_index, label="document"):
    for candidate in range(10_000):
        document_id = "{0}-{1}-{2}".format(label, shard_index, candidate)
        if engine._shard_index(document_id) == shard_index:
            return document_id
    raise AssertionError("could not find an id for shard {0}".format(shard_index))


def _journal_mode(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        conn.close()


def _table_row_count(path, table):
    conn = sqlite3.connect(str(path))
    try:
        try:
            return conn.execute('SELECT COUNT(*) FROM "{0}"'.format(table)).fetchone()[
                0
            ]
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            return 0
    finally:
        conn.close()


def test_retained_database_handle_cannot_write_after_drop_and_reshard(tmp_path):
    root = tmp_path / "stale-handle"
    owner = _open_client(root, sqlite_shards=2)
    stale_database = owner.app
    stale_database.items.insert_one({"_id": "before-drop"})

    owner.drop_database("app")

    replacement = _open_client(root, sqlite_shards=4)
    fresh_database = replacement.app
    fresh_database.items.insert_one({"_id": "fresh-seed"})
    document_id = next(
        candidate
        for candidate in ("rerouted-{0}".format(index) for index in range(10_000))
        if stale_database.engine._shard_index(candidate)
        != fresh_database.engine._shard_index(candidate)
    )

    with pytest.raises(StorageCorruptionError, match="manifest identity changed"):
        stale_database.items.insert_one({"_id": document_id, "source": "stale"})

    assert fresh_database.items.find_one({"_id": document_id}) is None
    fresh_database.items.insert_one({"_id": document_id, "source": "fresh"})
    assert fresh_database.items.find_one({"_id": document_id}) == {
        "_id": document_id,
        "source": "fresh",
    }

    owner.close()
    replacement.close()


def test_database_handle_reopens_after_close_and_client_can_drop_it(tmp_path):
    root = tmp_path / "reopen-after-close"
    client = _open_client(root, sqlite_shards=2)
    database = client.app
    database.items.insert_one({"_id": "before-close"})

    database.close()

    assert client.app is database
    assert database.items.find_one({"_id": "before-close"}) == {"_id": "before-close"}
    client.drop_database("app")
    assert not _database_directory(root).exists()
    client.close()


def test_backend_selection_crud_reopen_and_stable_routing(tmp_path):
    root = tmp_path / "store"
    client = _open_client(root)
    database = client.app
    collection = database.items

    assert isinstance(database.engine, ShardedSQLiteTableBackend)

    stable_id = _id_for_shard(database.engine, 2, "stable")
    other_id = _id_for_shard(database.engine, 1, "temporary")
    expected_shard = database.engine._shard_index(stable_id)
    collection.insert_many(
        [
            {"_id": stable_id, "name": "Ada", "visits": 1},
            {"_id": other_id, "name": "Temporary", "visits": 1},
        ]
    )

    assert collection.find_one({"_id": stable_id})["name"] == "Ada"
    assert (
        collection.update_one(
            {"_id": stable_id}, {"$inc": {"visits": 1}}
        ).modified_count
        == 1
    )
    assert collection.delete_one({"_id": other_id}).deleted_count == 1
    assert collection.find_one({"_id": stable_id})["visits"] == 2

    row_counts = [
        _table_row_count(_shard_file(root, shard), "items") for shard in range(4)
    ]
    assert row_counts[expected_shard] == 1
    assert sum(row_counts) == 1
    client.close()

    reopened = _open_client(root)
    try:
        assert reopened.app.engine._shard_index(stable_id) == expected_shard
        assert reopened.app.items.find_one({"_id": stable_id}) == {
            "_id": stable_id,
            "name": "Ada",
            "visits": 2,
        }
    finally:
        reopened.close()


def test_default_layout_and_wal_on_manifest_and_every_shard(tmp_path):
    root = tmp_path / "wal-store"
    client = _open_client(root)
    client.app.items.insert_one({"_id": "wal-check"})
    client.close()

    database_directory = _database_directory(root)
    manifest = database_directory / "manifest.sqlite"
    shard_files = [_shard_file(root, index) for index in range(4)]

    assert manifest.is_file()
    assert shard_files[0] == (database_directory / "shards" / "000" / "data.sqlite")
    assert all(path.is_file() for path in shard_files)
    assert _journal_mode(manifest) == "wal"
    assert [_journal_mode(path) for path in shard_files] == ["wal"] * 4


def test_reopen_rejects_a_different_shard_count(tmp_path):
    root = tmp_path / "configuration-store"
    original = _open_client(root, sqlite_shards=2)
    original.app.items.insert_one({"_id": 1, "value": "kept"})
    original.close()

    matching = _open_client(root, sqlite_shards=2)
    assert matching.app.items.find_one({"_id": 1})["value"] == "kept"
    matching.close()

    mismatched = _open_client(root, sqlite_shards=3)
    try:
        with pytest.raises(ConfigurationError, match="(?i)shard"):
            mismatched.app.items.count_documents({})
    finally:
        mismatched.close()


def test_scatter_count_query_and_global_sorted_window(tmp_path):
    root = tmp_path / "scatter-store"
    client = _open_client(root)
    database = client.app
    collection = database.items
    documents = []
    for shard in range(4):
        for ordinal in range(3):
            documents.append(
                {
                    "_id": _id_for_shard(
                        database.engine,
                        shard,
                        "scatter-{0}".format(ordinal),
                    ),
                    "group": "keep" if ordinal != 1 else "drop",
                    "score": shard * 10 + ordinal,
                }
            )
    collection.insert_many(documents)

    assert collection.count_documents({}) == 12
    assert collection.count_documents({"group": "keep"}) == 8
    assert {doc["_id"] for doc in collection.find({"group": "keep"})} == {
        doc["_id"] for doc in documents if doc["group"] == "keep"
    }

    query = {"group": "keep", "score": {"$gte": 10}}
    expected = sorted(
        (doc for doc in documents if doc["group"] == "keep" and doc["score"] >= 10),
        key=lambda doc: doc["score"],
        reverse=True,
    )[1:4]
    actual = list(
        collection.find(
            query,
            sort=[("score", -1)],
            skip=1,
            limit=3,
        )
    )

    assert [doc["_id"] for doc in actual] == [doc["_id"] for doc in expected]
    client.close()


def test_unfiltered_scans_use_read_only_attached_union_and_reuse_connections(
    tmp_path,
    monkeypatch,
):
    client = _open_client(tmp_path / "attached-union")
    collection = client.app.items
    documents = [
        {"_id": "attached-{0}".format(index), "value": index} for index in range(40)
    ]
    collection.insert_many(documents)
    engine = client.app.engine
    calls = []
    original = engine._find_existing_attached

    def record_attached_read(collection_name):
        calls.append(collection_name)
        return original(collection_name)

    monkeypatch.setattr(engine, "_find_existing_attached", record_attached_read)
    try:
        assert list(collection.find({})) == documents
        assert list(collection.find({})) == documents
        assert calls == ["items", "items"]
        assert len(engine._attached_read_pool_idle) == 1
        with engine._attached_read_connection() as conn:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute('DELETE FROM shard0."items"')
    finally:
        client.close()


def test_attached_union_explicitly_enables_portable_sqlite_uri_handling(
    tmp_path,
    monkeypatch,
):
    client = _open_client(tmp_path / "attached-uri")
    collection = client.app.items
    collection.insert_one({"_id": "portable"})
    engine = client.app.engine
    calls = []
    original = engine._manifest_connect

    def record_manifest_connect(**options):
        calls.append(options)
        return original(**options)

    monkeypatch.setattr(engine, "_manifest_connect", record_manifest_connect)
    try:
        assert list(collection.find({})) == [{"_id": "portable"}]
        assert calls == [{"check_same_thread": False, "uri": True}]
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["wal", "identity"])
def test_attached_reader_rejects_invalid_shard_metadata_and_closes(
    tmp_path,
    monkeypatch,
    failure,
):
    client = _open_client(tmp_path / ("attached-" + failure))
    engine = client.app.engine

    class Result:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        isolation_level = ""

        def __init__(self):
            self.closed = False

        def execute(self, statement, _parameters=()):
            if statement.startswith("PRAGMA shard") and statement.endswith(
                ".journal_mode"
            ):
                return Result(row=None if failure == "wal" else ("wal",))
            if statement.startswith("SELECT format_version"):
                return Result(rows=[])
            return Result()

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(
        engine,
        "_manifest_connect",
        lambda **_options: connection,
    )
    expected = "requires WAL" if failure == "wal" else "identity mismatch"
    error = ConfigurationError if failure == "wal" else StorageCorruptionError
    try:
        with pytest.raises(error, match=expected):
            engine._open_attached_read_connection()
        assert connection.closed is True
    finally:
        client.close()


def test_attached_reader_rejects_files_changed_during_open(tmp_path, monkeypatch):
    client = _open_client(tmp_path / "attached-file-change")
    engine = client.app.engine
    identities = engine._attached_shard_file_identities()
    observed = iter((identities, identities[:-1] + ((-1, -1),)))
    monkeypatch.setattr(
        engine,
        "_attached_shard_file_identities",
        lambda: next(observed),
    )
    try:
        with pytest.raises(StorageCorruptionError, match="changed while opening"):
            engine._open_attached_read_connection()
    finally:
        client.close()


def test_attached_reader_pool_retires_stale_poisoned_and_inherited_entries(
    tmp_path,
    monkeypatch,
):
    client = _open_client(tmp_path / "attached-pool-lifecycle")
    collection = client.app.items
    collection.insert_one({"_id": "kept"})
    engine = client.app.engine
    assert list(collection.find({})) == [{"_id": "kept"}]

    stale = engine._attached_read_pool_idle[-1]
    stale.file_identities = ((-1, -1),)
    assert list(collection.find({})) == [{"_id": "kept"}]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        stale.connection.execute("SELECT 1")

    with pytest.raises(RuntimeError, match="poison"):
        with engine._attached_read_connection():
            raise RuntimeError("poison")
    assert engine._attached_read_pool_idle == []

    context = engine._attached_read_connection()
    leased = context.__enter__()
    monkeypatch.setattr(
        engine,
        "_attached_shard_file_identities",
        lambda: (_ for _ in ()).throw(OSError("stat failed")),
    )
    context.__exit__(None, None, None)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        leased.execute("SELECT 1")
    monkeypatch.undo()

    assert list(collection.find({})) == [{"_id": "kept"}]
    inherited = engine._attached_read_pool_idle[-1].connection
    monkeypatch.setattr(
        sharded_sqlite,
        "_PROCESS_ID",
        lambda: engine._attached_read_pool_pid + 1,
    )
    engine._synchronize_attached_read_pid()
    assert engine._attached_read_pool_idle == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        inherited.execute("SELECT 1")
    client.close()


@pytest.mark.parametrize(
    ("message", "disabled", "raises"),
    [
        ("too many attached databases", True, False),
        ("no such table: shard0.items", False, False),
        ("disk I/O error", False, True),
    ],
)
def test_attached_scan_operational_error_fallbacks(
    tmp_path,
    monkeypatch,
    message,
    disabled,
    raises,
):
    client = _open_client(tmp_path / ("attached-error-" + str(disabled)))
    collection = client.app.items
    collection.insert_one({"_id": "kept"})
    engine = client.app.engine
    monkeypatch.setattr(
        engine,
        "_find_existing_attached",
        lambda _collection: (_ for _ in ()).throw(sqlite3.OperationalError(message)),
    )
    try:
        if raises:
            with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
                list(collection.find({}))
        else:
            assert list(collection.find({})) == [{"_id": "kept"}]
            assert engine._attached_reads_disabled is disabled
    finally:
        client.close()


def test_attached_union_preserves_legacy_null_order_and_filters_still_scatter(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "attached-order"
    client = _open_client(root)
    collection = client.app.items
    documents = [
        {"_id": "ordered-{0}".format(index), "group": index % 2} for index in range(12)
    ]
    collection.insert_many(documents)
    engine = client.app.engine
    delayed = documents[0]
    shard_index = engine._shard_index(delayed["_id"])
    conn = sqlite3.connect(str(_shard_file(root, shard_index)))
    try:
        conn.execute(
            'UPDATE "items" SET "__tinymongo_order" = NULL WHERE _id = ?',
            (_physical_id_key(delayed["_id"]),),
        )
        conn.commit()
    finally:
        conn.close()

    def unexpected_attached_read(_collection):
        raise AssertionError("filtered scans must retain Python matcher semantics")

    try:
        assert list(collection.find({})) == documents[1:] + [delayed]
        monkeypatch.setattr(
            engine,
            "_find_existing_attached",
            unexpected_attached_read,
        )
        assert list(collection.find({"group": 1})) == [
            document for document in documents if document["group"] == 1
        ]
    finally:
        client.close()


def test_attached_union_supports_concurrent_readers_and_collection_recreation(
    tmp_path,
):
    client = _open_client(tmp_path / "attached-concurrent")
    collection = client.app.items
    documents = [
        {"_id": "concurrent-{0}".format(index), "value": index} for index in range(100)
    ]
    collection.insert_many(documents)

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(
                    lambda _worker: list(collection.find({})),
                    range(4),
                )
            )
        assert results == [documents] * 4

        collection.drop()
        replacement = client.app.items
        replacement.insert_one({"_id": "replacement", "value": 1})
        assert list(replacement.find({})) == [{"_id": "replacement", "value": 1}]
    finally:
        client.close()


def test_more_than_default_attach_limit_uses_established_scatter_fallback(
    tmp_path,
    monkeypatch,
):
    client = _open_client(tmp_path / "attach-limit", sqlite_shards=11)
    collection = client.app.items
    collection.insert_many(
        [{"_id": "fallback-{0}".format(index)} for index in range(22)]
    )
    engine = client.app.engine

    def unexpected_attached_read(_collection):
        raise AssertionError("attached path exceeded SQLite's default limit")

    monkeypatch.setattr(
        engine,
        "_find_existing_attached",
        unexpected_attached_read,
    )
    try:
        assert len(list(collection.find({}))) == 22
    finally:
        client.close()


def test_index_fanout_and_cross_shard_unique_collisions(tmp_path):
    root = tmp_path / "index-store"
    client = _open_client(root)
    database = client.app
    users = database.users

    assert users.create_index("email", name="login_email", unique=True) == "login_email"
    assert users.index_information()["login_email"] == {
        "key": [("email", 1)],
        "unique": True,
    }

    for shard in range(4):
        conn = sqlite3.connect(str(_shard_file(root, shard)))
        try:
            catalog_entry = conn.execute(
                'SELECT index_name, unique_flag FROM "__tinymongo_indexes" '
                "WHERE collection_name = ? AND index_name = ?",
                ("users", "login_email"),
            ).fetchone()
        finally:
            conn.close()
        assert catalog_entry == ("login_email", 1)

    first_id = _id_for_shard(database.engine, 0, "first-user")
    second_id = _id_for_shard(database.engine, 1, "second-user")
    duplicate_id = _id_for_shard(database.engine, 1, "duplicate-user")
    users.insert_many(
        [
            {"_id": first_id, "email": "ada@example.com"},
            {"_id": second_id, "email": "grace@example.com"},
        ]
    )

    with pytest.raises(DuplicateKeyError):
        users.insert_one({"_id": duplicate_id, "email": "ada@example.com"})
    with pytest.raises(DuplicateKeyError):
        users.update_one(
            {"_id": second_id},
            {"$set": {"email": "ada@example.com"}},
        )

    assert users.count_documents({}) == 2
    assert users.find_one({"_id": second_id})["email"] == "grace@example.com"
    client.close()


def test_drop_collection_fans_out_and_drop_database_removes_layout(tmp_path):
    root = tmp_path / "drop-store"
    client = _open_client(root)
    database = client.app
    database.alpha.insert_one({"_id": 1})
    database.beta.insert_one({"_id": 2})

    assert database.drop_collection("alpha") is True
    assert database.list_collection_names() == ["beta"]
    for shard in range(4):
        conn = sqlite3.connect(str(_shard_file(root, shard)))
        try:
            alpha_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alpha'"
            ).fetchone()
        finally:
            conn.close()
        assert alpha_table is None

    database_directory = _database_directory(root)
    assert database_directory.is_dir()
    assert client.drop_database("app") is None
    assert not database_directory.exists()
    assert client.list_database_names() == []
    client.close()


def test_async_client_crud_with_configured_shards(tmp_path):
    root = tmp_path / "async-store"

    async def scenario():
        client = tinymongo.AsyncTinyMongoClient(
            str(root),
            backend=BACKEND,
            sqlite_shards=2,
        )
        collection = client.app.items
        try:
            inserted = await collection.insert_many(
                [
                    {"_id": "one", "value": 1},
                    {"_id": "two", "value": 2},
                ]
            )
            assert inserted.inserted_ids == ["one", "two"]
            assert await collection.count_documents({}) == 2
            assert await collection.find_one({"_id": "one"}) == {
                "_id": "one",
                "value": 1,
            }

            updated = await collection.update_one(
                {"_id": "two"},
                {"$inc": {"value": 3}},
            )
            assert updated.matched_count == 1
            assert updated.modified_count == 1
            assert (await collection.find_one({"_id": "two"}))["value"] == 5

            deleted = await collection.delete_one({"_id": "one"})
            assert deleted.deleted_count == 1
            assert await collection.count_documents({}) == 1
        finally:
            await client.close()

    asyncio.run(scenario())

    assert _shard_file(root, 0).is_file()
    assert _shard_file(root, 1).is_file()
    assert not _shard_file(root, 2).exists()


def test_mongo_client_recognizes_sqlite_shards_option(tmp_path):
    root = tmp_path / "mongo-client-store"
    client = tinymongo.MongoClient(
        "mongodb://unused.example",
        tinymongo_folder=str(root),
        backend=BACKEND,
        sqlite_shards=2,
    )
    try:
        database = client.app
        assert isinstance(database.engine, ShardedSQLiteTableBackend)
        assert database.engine.shard_count == 2
        assert database.items.insert_one({"_id": "recognized"}).inserted_id == (
            "recognized"
        )
    finally:
        client.close()


@pytest.mark.parametrize("shard_count", [True, False, 2.0, "2", [], {}])
def test_invalid_shard_count_types_are_rejected(tmp_path, shard_count):
    client = _open_client(tmp_path / "invalid-type", sqlite_shards=shard_count)
    try:
        with pytest.raises(TypeError, match="sqlite_shards must be an integer"):
            client.app
    finally:
        client.close()


@pytest.mark.parametrize("shard_count", [-1, 0, 1, 65, 100])
def test_invalid_shard_count_ranges_are_rejected(tmp_path, shard_count):
    client = _open_client(tmp_path / "invalid-range", sqlite_shards=shard_count)
    try:
        with pytest.raises(ValueError, match="sqlite_shards must be between 2 and 64"):
            client.app
    finally:
        client.close()


def test_sqlite_shards_option_does_not_change_normal_sqlite_backend(tmp_path):
    root = tmp_path / "ordinary-sqlite"
    client = tinymongo.TinyMongoClient(
        str(root),
        backend="sqlite",
        sqlite_shards=2,
    )
    try:
        database = client.app
        assert not isinstance(database.engine, ShardedSQLiteTableBackend)
        database.items.insert_one({"_id": "ordinary"})
        assert database.items.find_one({"_id": "ordinary"}) is not None
    finally:
        client.close()

    assert (root / "app.sqlite").is_file()
    assert not (root / "app.sqlite-sharded").exists()


@pytest.mark.parametrize(
    ("ordered", "expected_inserted", "expected_error_indexes"),
    [
        (True, 2, [2]),
        (False, 3, [2, 4]),
    ],
)
def test_multi_shard_batch_preserves_duplicate_ordering_semantics(
    tmp_path,
    ordered,
    expected_inserted,
    expected_error_indexes,
):
    root = tmp_path / ("ordered" if ordered else "unordered")
    client = _open_client(root, sqlite_shards=2)
    database = client.app
    collection = database.items
    seed_id = _id_for_shard(database.engine, 0, "seed")
    first_id = _id_for_shard(database.engine, 0, "first")
    second_id = _id_for_shard(database.engine, 1, "second")
    tail_id = _id_for_shard(database.engine, 1, "tail")
    collection.insert_one({"_id": seed_id})
    documents = [
        {"_id": first_id},
        {"_id": second_id},
        {"_id": seed_id},
        {"_id": tail_id},
        {"_id": second_id},
    ]

    try:
        with pytest.raises(BulkWriteError) as caught:
            collection.insert_many(documents, ordered=ordered)

        details = caught.value.details
        assert details["nInserted"] == expected_inserted
        assert [error["index"] for error in details["writeErrors"]] == (
            expected_error_indexes
        )
        assert all(
            error["op"] is documents[error["index"]] for error in details["writeErrors"]
        )
        expected_ids = {seed_id, first_id, second_id}
        if not ordered:
            expected_ids.add(tail_id)
        assert {document["_id"] for document in collection.find({})} == expected_ids
        assert _table_row_count(_shard_file(root, 0), "items") >= 1
        assert _table_row_count(_shard_file(root, 1), "items") >= 1
    finally:
        client.close()


def test_index_create_and_drop_survive_separate_reopens(tmp_path):
    root = tmp_path / "index-reopen"
    first = _open_client(root, sqlite_shards=2)
    users = first.app.users
    users.create_index("email", name="email_unique", unique=True)
    users.create_index("status", name="status_lookup")
    users.insert_one({"_id": "owner", "email": "owner@example.com"})
    first.close()

    second = _open_client(root, sqlite_shards=2)
    try:
        users = second.app.users
        assert users.index_information()["email_unique"] == {
            "key": [("email", 1)],
            "unique": True,
        }
        assert users.index_information()["status_lookup"] == {"key": [("status", 1)]}
        with pytest.raises(DuplicateKeyError):
            users.insert_one({"_id": "duplicate", "email": "owner@example.com"})
        users.drop_index("status_lookup")
    finally:
        second.close()

    third = _open_client(root, sqlite_shards=2)
    try:
        information = third.app.users.index_information()
        assert set(information) == {"_id_", "email_unique"}
        assert "status_lookup" not in information
    finally:
        third.close()


def test_compound_sparse_and_partial_unique_indexes_across_shards(tmp_path):
    client = _open_client(tmp_path / "advanced-indexes", sqlite_shards=2)
    database = client.app
    first_id = _id_for_shard(database.engine, 0, "advanced-first")
    second_id = _id_for_shard(database.engine, 1, "advanced-second")

    try:
        compound = database.compound
        compound.create_index(
            [("tenant", 1), ("username", 1)],
            name="tenant_username",
            unique=True,
        )
        compound.insert_one({"_id": first_id, "tenant": "north", "username": "ada"})
        with pytest.raises(DuplicateKeyError):
            compound.insert_one(
                {"_id": second_id, "tenant": "north", "username": "ada"}
            )

        sparse = database.sparse
        sparse.create_index("email", name="optional_email", unique=True, sparse=True)
        sparse.insert_many([{"_id": first_id}, {"_id": second_id}])
        sparse.insert_one({"_id": "email-owner", "email": "same@example.com"})
        with pytest.raises(DuplicateKeyError):
            sparse.insert_one({"_id": "email-duplicate", "email": "same@example.com"})

        partial = database.partial
        partial.create_index(
            "handle",
            name="active_handle",
            unique=True,
            partialFilterExpression={"active": True},
        )
        partial.insert_many(
            [
                {"_id": first_id, "handle": "same", "active": False},
                {"_id": second_id, "handle": "same", "active": False},
            ]
        )
        partial.insert_one({"_id": "active-owner", "handle": "same", "active": True})
        with pytest.raises(DuplicateKeyError):
            partial.insert_one(
                {"_id": "active-duplicate", "handle": "same", "active": True}
            )

        assert compound.index_information()["tenant_username"]["key"] == [
            ("tenant", 1),
            ("username", 1),
        ]
        assert sparse.index_information()["optional_email"]["sparse"] is True
        assert partial.index_information()["active_handle"][
            "partialFilterExpression"
        ] == {"active": True}
    finally:
        client.close()


def test_reopen_rejects_missing_or_swapped_shard_files(tmp_path):
    missing_root = tmp_path / "missing-shard"
    client = _open_client(missing_root, sqlite_shards=2)
    client.app.items.insert_one({"_id": "kept"})
    client.close()
    os.remove(_shard_file(missing_root, 1))

    with pytest.raises(StorageCorruptionError, match="missing required shard"):
        _open_client(missing_root, sqlite_shards=2).app

    swapped_root = tmp_path / "swapped-shards"
    client = _open_client(swapped_root, sqlite_shards=2)
    client.app.items.insert_many([{"_id": "one"}, {"_id": "two"}])
    client.close()
    first = _shard_file(swapped_root, 0)
    second = _shard_file(swapped_root, 1)
    temporary = swapped_root / "swap.sqlite"
    shutil.move(first, temporary)
    shutil.move(second, first)
    shutil.move(temporary, second)

    with pytest.raises(StorageCorruptionError, match="identity mismatch"):
        _open_client(swapped_root, sqlite_shards=2).app


def test_stale_client_recreates_dropped_collection_consistently(tmp_path):
    root = tmp_path / "cross-client-drop"
    first = _open_client(root, sqlite_shards=2)
    second = _open_client(root, sqlite_shards=2)
    try:
        first.app.items.insert_one({"_id": "before"})
        assert second.app.drop_collection("items") is True

        first.app.items.insert_one({"_id": "after"})

        assert first.app.list_collection_names() == ["items"]
        assert first.app.items.find_one({"_id": "before"}) is None
        assert first.app.items.find_one({"_id": "after"}) == {"_id": "after"}
    finally:
        first.close()
        second.close()
