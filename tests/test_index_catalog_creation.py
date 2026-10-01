"""Creating the first catalog must persist metadata in one atomic write."""

import pytest

from tinymongo import TinyMongoClient
from tinymongo.storage_backends import AtomicJSONStorage


@pytest.mark.parametrize("batch", [False, True])
def test_first_index_uses_one_file_write(tmp_path, monkeypatch, batch):
    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.docs
        col.insert_one({"_id": 1, "email": "one"})
        client.app.archive.insert_one({"body": "x" * 100000})
        original = AtomicJSONStorage._write_cached_tables
        writes = []

        def tracked(self, data, changed):
            writes.append(changed)
            return original(self, data, changed)

        monkeypatch.setattr(AtomicJSONStorage, "_write_cached_tables", tracked)
        if batch:
            assert col.create_indexes([{"key": {"email": 1}, "unique": True}]) == [
                "email_1"
            ]
        else:
            assert col.create_index("email", unique=True) == "email_1"
        assert len(writes) == 1


@pytest.mark.parametrize("backend", ["json", "memory"])
@pytest.mark.parametrize("batch", [False, True])
def test_catalog_failure_retry_reopen_and_uniqueness(
    tmp_path, monkeypatch, backend, batch
):
    from uuid import uuid4

    from tinymongo.errors import DuplicateKeyError
    from tinymongo.indexes import INDEX_CATALOG_TABLE
    from tinymongo.storage_backends import MemoryStorage

    address = str(tmp_path) if backend == "json" else "memory://" + uuid4().hex
    storage = AtomicJSONStorage if backend == "json" else MemoryStorage
    with TinyMongoClient(address, backend=backend) as client:
        col = client.app.docs
        col.insert_one({"_id": 1, "email": "one"})
        original = storage.write_table

        def fail(self, name, data):
            assert name == INDEX_CATALOG_TABLE
            raise OSError("failed catalog")

        def create():
            if batch:
                return col.create_indexes([{"key": {"email": 1}, "unique": True}])
            return col.create_index("email", unique=True)

        with monkeypatch.context() as patch:
            patch.setattr(storage, "write_table", fail)
            with pytest.raises(OSError, match="failed catalog"):
                create()
        assert INDEX_CATALOG_TABLE not in col.parent.tinydb.tables()
        assert [i["name"] for i in col.list_indexes()] == ["_id_"]
        create()
        # Retrying an equivalent declaration must not perform another write.
        with monkeypatch.context() as patch:
            patch.setattr(storage, "write_table", fail)
            create()
        col.create_index("other")
        catalog = col.parent.tinydb.table(INDEX_CATALOG_TABLE)
        assert [doc.doc_id for doc in catalog.all()] == [1, 2]
        assert storage.write_table is original
    with TinyMongoClient(address, backend=backend) as other:
        assert {i["name"] for i in other.app.docs.list_indexes()} == {
            "_id_",
            "email_1",
            "other_1",
        }
        with pytest.raises(DuplicateKeyError):
            other.app.docs.insert_one({"_id": 2, "email": "one"})


@pytest.mark.parametrize(
    "custom", ["db", "table", "proxy", "storage", "cached", "existing"]
)
def test_new_table_helper_keeps_custom_and_existing_paths(custom):
    from uuid import uuid4

    from tinymongo import storage_backends as sb

    class CustomDB(sb.MemoryTinyDB):
        pass

    class CustomTable(sb.MemoryTable):
        pass

    class CustomProxy(sb.MemoryStorageProxy):
        pass

    class CustomStorage(sb.MemoryStorage):
        pass

    db_class = CustomDB if custom == "db" else sb.MemoryTinyDB
    kwargs = {"storage": CustomStorage if custom == "storage" else sb.MemoryStorage}
    if custom == "table":
        kwargs["table_class"] = CustomTable
    if custom == "proxy":
        kwargs["storage_proxy_class"] = CustomProxy
    with db_class(str(uuid4()), **kwargs) as db:
        if custom == "cached":
            db.table("catalog")
        if custom == "existing":
            db._storage.write_table("catalog", {"7": {"old": True}})
        before = db._storage.read()
        assert db._insert_new_table("catalog", [{"new": True}]) is False
        assert db._storage.read() == before


@pytest.mark.parametrize("batch", [False, True])
def test_failed_atomic_replace_keeps_catalog_absent(tmp_path, monkeypatch, batch):
    from tinymongo import storage_backends as sb
    from tinymongo.indexes import INDEX_CATALOG_TABLE

    with TinyMongoClient(str(tmp_path), backend="json") as client:
        col = client.app.docs
        col.insert_one({"_id": 1})
        path = tmp_path / "app.json"
        before = path.read_bytes()

        def fail(*args):
            raise OSError("replace failed")

        def create():
            if batch:
                col.create_indexes([{"key": {"a": 1}}, {"key": {"b": 1}}])
            else:
                col.create_index("a")

        with monkeypatch.context() as patch:
            patch.setattr(sb.os, "replace", fail)
            with pytest.raises(OSError, match="replace failed"):
                create()
        assert path.read_bytes() == before
        assert INDEX_CATALOG_TABLE not in col.parent.tinydb.tables()
        create()
        assert {i["name"] for i in col.list_indexes()} == (
            {"_id_", "a_1", "b_1"} if batch else {"_id_", "a_1"}
        )


@pytest.mark.parametrize("backend", ["json", "memory"])
def test_two_clients_create_first_catalog_concurrently(tmp_path, backend):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from uuid import uuid4

    address = str(tmp_path) if backend == "json" else "memory://" + uuid4().hex
    with TinyMongoClient(address, backend=backend) as first:
        first.app.docs.insert_one({"_id": 1})
        with TinyMongoClient(address, backend=backend) as second:
            gate = Barrier(2)

            def create(client, field):
                col = client.app.docs
                gate.wait(timeout=10)
                return col.create_index(field)

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(create, first, "a"),
                    pool.submit(create, second, "b"),
                ]
                assert [f.result(timeout=20) for f in futures] == ["a_1", "b_1"]
    with TinyMongoClient(address, backend=backend) as reopened:
        assert {i["name"] for i in reopened.app.docs.list_indexes()} == {
            "_id_",
            "a_1",
            "b_1",
        }
