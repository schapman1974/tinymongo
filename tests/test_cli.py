import json
from datetime import datetime, timezone

import pytest
import tinymongo as tm
from tinymongo import cli
from tinymongo.errors import BulkWriteError


def test_cli_inspect_export_import_and_migrate(tmp_path, capsys):
    source = tmp_path / "source"
    target = tmp_path / "target"
    export_file = tmp_path / "users.json"

    client = tm.TinyMongoClient(str(source))
    users = client.app.users
    users.insert_many(
        [
            {"_id": 1, "name": "Ada"},
            {"_id": 2, "name": "Grace"},
        ]
    )

    assert cli.main(["list-dbs", str(source)]) == 0
    assert "app" in capsys.readouterr().out

    assert cli.main(["list-collections", str(source), "app"]) == 0
    assert "users" in capsys.readouterr().out

    assert cli.main(["inspect", str(source)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["databases"][0]["name"] == "app"
    collections = {
        item["name"]: item["count"] for item in inspected["databases"][0]["collections"]
    }
    assert collections["users"] == 2

    assert (
        cli.main(["export", str(source), "app", "users", "-o", str(export_file)]) == 0
    )
    assert json.loads(export_file.read_text()) == [
        {"_id": 1, "name": "Ada"},
        {"_id": 2, "name": "Grace"},
    ]

    assert (
        cli.main(
            [
                "import",
                str(target),
                "app",
                "users",
                str(export_file),
                "--mode",
                "replace",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert tm.TinyMongoClient(str(target)).app.users.count() == 2

    migrated = tmp_path / "migrated"
    assert (
        cli.main(
            [
                "migrate",
                str(source),
                str(migrated),
                "--to-backend",
                "sqlite",
                "--database",
                "app",
            ]
        )
        == 0
    )
    migrated_payload = json.loads(capsys.readouterr().out)
    migrated_counts = {
        item["collection"]: item["count"] for item in migrated_payload["migrated"]
    }
    assert migrated_counts["users"] == 2
    assert (migrated / "app.sqlite").exists()


def test_cli_backend_option_exports_sqlite_backend(tmp_path, capsys):
    db_dir = tmp_path / "sqlite-db"
    client = tm.TinyMongoClient(str(db_dir), backend="sqlite")
    client.app.users.insert_one({"_id": 1, "name": "Ada"})

    assert (
        cli.main(
            [
                "export",
                str(db_dir),
                "app",
                "users",
                "--backend",
                "sqlite",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == [{"_id": 1, "name": "Ada"}]


def test_cli_export_import_preserves_datetime_and_bytes_without_bson(tmp_path, capsys):
    source = tmp_path / "core-source"
    target = tmp_path / "core-target"
    export_file = tmp_path / "core-events.json"
    created = datetime(2026, 7, 29, 14, 30)

    tm.TinyMongoClient(str(source)).app.events.insert_one(
        {
            "_id": "core",
            "created": created,
            "raw": b"\x00\x01\xff",
            "buffer": bytearray(b"mutable"),
        }
    )
    assert (
        cli.main(
            [
                "export",
                str(source),
                "app",
                "events",
                "--output",
                str(export_file),
            ]
        )
        == 0
    )
    assert cli.main(["import", str(target), "app", "events", str(export_file)]) == 0

    assert "imported 1 documents" in capsys.readouterr().out
    assert tm.TinyMongoClient(str(target)).app.events.find_one({"_id": "core"}) == {
        "_id": "core",
        "created": created,
        "raw": b"\x00\x01\xff",
        "buffer": b"mutable",
    }


def test_cli_export_import_and_migrate_preserve_supported_bson_values(tmp_path, capsys):
    bson = pytest.importorskip("bson")
    source = tmp_path / "bson-source"
    imported_target = tmp_path / "bson-imported"
    migrated_target = tmp_path / "bson-migrated"
    export_file = tmp_path / "events.json"
    object_id = bson.ObjectId("507f1f77bcf86cd799439011")
    created = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
    document = {
        "_id": object_id,
        "created": created,
        "raw": b"\x00\x01\xff",
        "buffer": bytearray(b"mutable"),
        "binary": bson.Binary(b"0123456789abcdef", subtype=4),
    }

    tm.TinyMongoClient(str(source)).app.events.insert_one(document)
    assert (
        cli.main(
            [
                "export",
                str(source),
                "app",
                "events",
                "--output",
                str(export_file),
            ]
        )
        == 0
    )

    exported_json = json.loads(export_file.read_text(encoding="utf-8"))
    assert exported_json[0]["_id"]["__tinymongo_type_v1__"] == "objectid"
    assert exported_json[0]["created"]["__tinymongo_type_v1__"] == "datetime"
    assert exported_json[0]["raw"]["__tinymongo_type_v1__"] == "binary"
    assert exported_json[0]["binary"]["value"]["subtype"] == 4

    assert (
        cli.main(
            [
                "import",
                str(imported_target),
                "app",
                "events",
                str(export_file),
            ]
        )
        == 0
    )
    assert "imported 1 documents" in capsys.readouterr().out
    imported = tm.TinyMongoClient(str(imported_target)).app.events.find_one(
        {"_id": object_id}
    )
    assert imported == {
        "_id": object_id,
        "created": created,
        "raw": b"\x00\x01\xff",
        "buffer": b"mutable",
        "binary": bson.Binary(b"0123456789abcdef", subtype=4),
    }

    assert (
        cli.main(
            [
                "migrate",
                str(source),
                str(migrated_target),
                "--to-backend",
                "sqlite",
            ]
        )
        == 0
    )
    capsys.readouterr()
    migrated = tm.TinyMongoClient(
        str(migrated_target), backend="sqlite"
    ).app.events.find_one({"_id": object_id})
    assert migrated == document


def test_cli_import_from_stdin_and_replace_mode(tmp_path, monkeypatch, capsys):
    db_dir = tmp_path / "db"
    client = tm.TinyMongoClient(str(db_dir))
    client.app.users.insert_one({"_id": 1, "name": "old"})
    monkeypatch.setattr(
        "sys.stdin",
        type("Input", (), {"read": lambda self: '[{"_id": 2, "name": "new"}]'})(),
    )

    assert (
        cli.main(
            [
                "import",
                str(db_dir),
                "app",
                "users",
                "-",
                "--mode",
                "replace",
            ]
        )
        == 0
    )

    assert "imported 1 documents" in capsys.readouterr().out
    client.close()
    refreshed = tm.TinyMongoClient(str(db_dir))
    assert list(refreshed.app.users.find({})) == [{"_id": 2, "name": "new"}]
    refreshed.close()


def test_cli_export_import_preserves_embedded_document_field_order(tmp_path):
    source = tmp_path / "ordered-source"
    target = tmp_path / "ordered-target"
    export_file = tmp_path / "ordered.json"
    document_id = {"z": 1, "a": 2}
    nested = {"second": 2, "first": 1}

    source_client = tm.TinyMongoClient(str(source))
    source_client.app.items.insert_one({"_id": document_id, "nested": nested})
    source_client.close()

    assert (
        cli.main(
            [
                "export",
                str(source),
                "app",
                "items",
                "-o",
                str(export_file),
            ]
        )
        == 0
    )
    exported = json.loads(export_file.read_text(encoding="utf-8"))
    assert list(exported[0]["_id"]) == ["z", "a"]
    assert list(exported[0]["nested"]) == ["second", "first"]

    assert (
        cli.main(
            [
                "import",
                str(target),
                "app",
                "items",
                str(export_file),
                "--mode",
                "replace",
            ]
        )
        == 0
    )
    restored_client = tm.TinyMongoClient(str(target))
    restored = restored_client.app.items.find_one({"_id": document_id})
    assert restored is not None
    assert list(restored["_id"]) == ["z", "a"]
    assert list(restored["nested"]) == ["second", "first"]
    restored_client.close()


def test_copy_indexes_preserves_names_keys_and_uniqueness():
    class Source:
        def list_indexes(self):
            return [
                {"name": "_id_", "key": [("_id", 1)]},
                {
                    "name": "email_unique",
                    "key": [("email", 1)],
                    "unique": True,
                },
                {"name": "created_desc", "key": [("created", -1)]},
            ]

    class Target:
        def __init__(self):
            self.created = []

        def create_index(self, keys, **options):
            self.created.append((keys, options))

    target = Target()
    cli._copy_indexes(Source(), target)

    assert target.created == [
        (
            [("email", 1)],
            {"name": "email_unique", "unique": True},
        ),
        ([("created", -1)], {"name": "created_desc"}),
    ]


@pytest.mark.parametrize("previous", [[], [{"_id": "old"}]])
def test_replace_collection_rolls_back_a_failed_destination_write(previous):
    class Collection:
        def __init__(self):
            self.documents = list(previous)
            self.insert_calls = 0

        def list_indexes(self):
            return [{"name": "_id_", "key": [("_id", 1)]}]

        def find(self, _filter):
            return list(self.documents)

        def delete_many(self, _filter):
            self.documents = []

        def insert_many(self, documents):
            self.insert_calls += 1
            if self.insert_calls == 1:
                raise RuntimeError("destination write failed")
            self.documents.extend(documents)

    collection = Collection()
    with pytest.raises(RuntimeError, match="destination write failed"):
        cli._replace_collection(
            {"users": collection},
            "users",
            [{"_id": "new"}],
        )

    assert collection.documents == previous


def test_replace_collection_reports_a_failed_rollback_with_runtime_error():
    class Collection:
        def __init__(self):
            self.documents = [{"_id": "old"}]

        def list_indexes(self):
            return [{"name": "_id_", "key": [("_id", 1)]}]

        def find(self, _filter):
            return list(self.documents)

        def delete_many(self, _filter):
            self.documents = []

        def insert_many(self, _documents):
            raise RuntimeError("storage unavailable")

    with pytest.raises(
        RuntimeError,
        match="previous data could not be restored",
    ) as error:
        cli._replace_collection(
            {"users": Collection()},
            "users",
            [{"_id": "new"}],
        )

    assert isinstance(error.value.__cause__, RuntimeError)


def test_cli_import_rejects_non_array_input(tmp_path):
    input_file = tmp_path / "bad.json"
    input_file.write_text('{"_id": 1}', encoding="utf-8")

    with pytest.raises(SystemExit, match="JSON array"):
        cli.main(["import", str(tmp_path / "db"), "app", "users", str(input_file)])


def test_cli_migrate_all_databases(tmp_path, capsys):
    source = tmp_path / "source"
    target = tmp_path / "target"
    client = tm.TinyMongoClient(str(source))
    client.app.users.insert_one({"_id": 1})
    client.audit.events.insert_one({"_id": "event"})

    assert (
        cli.main(["migrate", str(source), str(target), "--to-backend", "sqlite"]) == 0
    )

    payload = json.loads(capsys.readouterr().out)
    migrated = {
        (item["database"], item["collection"]): item["count"]
        for item in payload["migrated"]
    }
    assert migrated[("app", "users")] == 1
    assert migrated[("audit", "events")] == 1
    assert tm.TinyMongoClient(str(target), backend="sqlite").audit.events.count() == 1


def test_cli_hides_tinydb_default_table_from_listing_inspection_and_migration(
    tmp_path, capsys
):
    source = tmp_path / "source-with-default"
    target = tmp_path / "target-without-default"
    database = tm.TinyMongoClient(str(source)).app
    database.users.insert_one({"_id": 1})
    assert "_default" not in database.collection_names()

    assert cli.main(["list-collections", str(source), "app"]) == 0
    assert capsys.readouterr().out.splitlines() == ["users"]

    assert cli.main(["inspect", str(source)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert [
        collection["name"] for collection in inspected["databases"][0]["collections"]
    ] == ["users"]

    assert (
        cli.main(["migrate", str(source), str(target), "--to-backend", "sqlite"]) == 0
    )
    migrated = json.loads(capsys.readouterr().out)
    assert [
        item["collection"] for item in migrated["migrated"] if item["database"] == "app"
    ] == ["users"]
    assert tm.TinyMongoClient(
        str(target), backend="sqlite"
    ).app.list_collection_names() == ["users"]


def test_replace_import_preflights_before_deleting_existing_data(tmp_path):
    target = tmp_path / "replace-preflight"
    input_file = tmp_path / "duplicates.json"
    client = tm.TinyMongoClient(str(target))
    client.app.users.insert_one({"_id": "old", "name": "Keep me"})
    client.close()
    input_file.write_text(
        json.dumps([{"_id": 1}, {"_id": 1}, {"_id": 2}]),
        encoding="utf-8",
    )

    with pytest.raises(BulkWriteError):
        cli.main(
            [
                "import",
                str(target),
                "app",
                "users",
                str(input_file),
                "--mode",
                "replace",
            ]
        )

    documents = list(tm.TinyMongoClient(str(target)).app.users.find({}))
    assert documents == [{"_id": "old", "name": "Keep me"}]


def test_migrate_preflights_target_indexes_before_deleting_existing_data(tmp_path):
    source = tmp_path / "migration-source"
    target = tmp_path / "migration-target"
    source_client = tm.TinyMongoClient(str(source))
    source_client.app.users.insert_many(
        [
            {"_id": 1, "email": "duplicate@example.com"},
            {"_id": 2, "email": "duplicate@example.com"},
        ]
    )
    source_client.close()
    target_client = tm.TinyMongoClient(str(target))
    target_client.app.users.create_index("email", unique=True)
    target_client.app.users.insert_one({"_id": "old", "email": "old@example.com"})
    target_client.close()

    with pytest.raises(BulkWriteError):
        cli.main(
            [
                "migrate",
                str(source),
                str(target),
                "--to-backend",
                "tinydb",
                "--database",
                "app",
            ]
        )

    documents = list(tm.TinyMongoClient(str(target)).app.users.find({}))
    assert documents == [{"_id": "old", "email": "old@example.com"}]


def test_replace_collection_restores_previous_data_after_late_write_failure():
    class Collection:
        def __init__(self):
            self.documents = [{"_id": "old"}]
            self.insert_attempts = 0

        def list_indexes(self):
            return [
                {"name": "_id_", "key": [("_id", 1)]},
                {"name": "label_1", "key": [("label", 1)]},
            ]

        def find(self, _filter):
            return list(self.documents)

        def delete_many(self, _filter):
            self.documents = []

        def insert_many(self, documents):
            self.insert_attempts += 1
            if self.insert_attempts == 1:
                raise ValueError("late destination failure")
            self.documents = list(documents)

    collection = Collection()
    database = {"events": collection}

    with pytest.raises(ValueError, match="late destination failure"):
        cli._replace_collection(database, "events", [{"_id": "new"}])

    assert collection.documents == [{"_id": "old"}]
    assert collection.insert_attempts == 2


def test_replace_collection_reports_a_failed_rollback():
    class Collection:
        def __init__(self):
            self.documents = [{"_id": "old"}]

        def list_indexes(self):
            return [{"name": "_id_", "key": [("_id", 1)]}]

        def find(self, _filter):
            return list(self.documents)

        def delete_many(self, _filter):
            self.documents = []

        def insert_many(self, _documents):
            raise ValueError("destination unavailable")

    collection = Collection()

    with pytest.raises(
        RuntimeError,
        match="previous data could not be restored",
    ) as caught:
        cli._replace_collection(
            {"events": collection},
            "events",
            [{"_id": "new"}],
        )

    assert isinstance(caught.value.__cause__, ValueError)


def test_cli_storage_uri_options_are_passed_through(monkeypatch, tmp_path, capsys):
    calls = []

    class FakeCollection:
        def __init__(self, docs=None):
            self.docs = list(docs or [])

        def find(self, _filter=None):
            return self.docs

        def delete_many(self, _filter):
            return None

        def insert_many(self, docs):
            self.docs = list(docs)

        def list_indexes(self):
            return [{"name": "_id_", "key": [("_id", 1)]}]

        def create_index(self, *args, **kwargs):
            return kwargs.get("name")

        def drop(self):
            self.docs = []
            return True

    class FakeDatabase:
        def __init__(self):
            self.collections = {"users": FakeCollection([{"_id": 1}])}

        def __getitem__(self, name):
            return self.collections.setdefault(name, FakeCollection())

        def collection_names(self):
            return ["_default", "users"]

    class FakeClient:
        def __init__(self, path, backend, storage_uri=None, dsn=None):
            calls.append((path, backend, storage_uri, dsn))
            self.app = FakeDatabase()

        def __getitem__(self, name):
            return self.app

    monkeypatch.setattr(cli, "TinyMongoClient", FakeClient)
    monkeypatch.setattr(
        cli,
        "_db_names",
        lambda path, backend, storage_uri=None, dsn=None: ["app"],
    )

    assert (
        cli.main(
            [
                "export",
                str(tmp_path),
                "app",
                "users",
                "--backend",
                "parquet",
                "--storage-uri",
                "s3://bucket/root",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == [{"_id": 1}]

    assert (
        cli.main(
            [
                "migrate",
                str(tmp_path / "source"),
                str(tmp_path / "target"),
                "--from-backend",
                "parquet",
                "--to-backend",
                "parquet",
                "--source-uri",
                "s3://source/root",
                "--target-uri",
                "s3://target/root",
            ]
        )
        == 0
    )

    assert calls[0] == (str(tmp_path), "parquet", "s3://bucket/root", None)
    assert calls[1] == (
        str(tmp_path / "source"),
        "parquet",
        "s3://source/root",
        None,
    )
    assert calls[2] == (
        str(tmp_path / "target"),
        "parquet",
        "s3://target/root",
        None,
    )

    assert (
        cli.main(
            [
                "export",
                str(tmp_path),
                "app",
                "users",
                "--backend",
                "postgres",
                "--dsn",
                "postgresql://db",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert calls[3] == (str(tmp_path), "postgres", None, "postgresql://db")


def test_cli_storage_uri_inspect_and_db_names(monkeypatch, tmp_path, capsys):
    clients = []

    class FakeCollection:
        def count(self):
            return 3

    class FakeDatabase:
        def __getitem__(self, name):
            return FakeCollection()

        def collection_names(self):
            return ["users"]

    class FakeClient:
        def __init__(self, path, backend, storage_uri=None, dsn=None):
            clients.append(self)
            self.storage_uri = storage_uri
            self.dsn = dsn

        def list_database_names(self):
            return ["app"]

        def __getitem__(self, name):
            return FakeDatabase()

    monkeypatch.setattr(cli, "TinyMongoClient", FakeClient)

    assert cli._db_names(str(tmp_path), "parquet", storage_uri="s3://bucket/root") == [
        "app"
    ]
    assert (
        cli.main(
            [
                "inspect",
                str(tmp_path),
                "--backend",
                "parquet",
                "--storage-uri",
                "s3://bucket/root",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["storage_uri"] == "s3://bucket/root"
    assert payload["databases"][0]["collections"][0]["count"] == 3

    monkeypatch.setenv("TINYMONGO_STORAGE_URI", "gs://env-bucket/root")
    assert cli._db_names(str(tmp_path), "parquet") == ["app"]
    assert clients[-1].storage_uri == "gs://env-bucket/root"

    assert (
        cli.main(
            [
                "inspect",
                str(tmp_path),
                "--backend",
                "parquet",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["storage_uri"] == "gs://env-bucket/root"

    monkeypatch.setenv("TINYMONGO_POSTGRES_DSN", "postgresql://env-db")
    assert cli._db_names(str(tmp_path), "postgres") == ["app"]
    assert clients[-1].dsn == "postgresql://env-db"


def test_cli_migrate_reports_environment_only_remote_configuration(
    monkeypatch,
    tmp_path,
    capsys,
):
    calls = []

    class EmptyClient:
        def __init__(self, path, backend, storage_uri=None, dsn=None):
            calls.append((path, backend, storage_uri, dsn))

        def list_database_names(self):
            return []

    monkeypatch.setattr(cli, "TinyMongoClient", EmptyClient)
    monkeypatch.setenv("TINYMONGO_STORAGE_URI", "s3://source/root")
    monkeypatch.setenv("TINYMONGO_POSTGRES_DSN", "postgresql://env-db")

    assert (
        cli.main(
            [
                "migrate",
                str(tmp_path / "unused-source"),
                str(tmp_path / "unused-target"),
                "--from-backend",
                "parquet",
                "--to-backend",
                "postgres",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["source_uri"] == "s3://source/root"
    assert payload["target_uri"] is None
    assert payload["source_dsn_configured"] is False
    assert payload["target_dsn_configured"] is True
    assert calls == [
        (
            str(tmp_path / "unused-source"),
            "parquet",
            "s3://source/root",
            None,
        ),
        (
            str(tmp_path / "unused-target"),
            "postgres",
            None,
            "postgresql://env-db",
        ),
        (
            str(tmp_path / "unused-source"),
            "parquet",
            "s3://source/root",
            None,
        ),
    ]


def test_cli_effective_remote_configuration_precedence(monkeypatch, tmp_path):
    names = (
        "TINYMONGO_POSTGRES_DSN",
        "TINYMONGO_POSTGRESQL_DSN",
        "DATABASE_URL",
        "TINYMONGO_MYSQL_DSN",
        "TINYMONGO_MARIADB_DSN",
        "MYSQL_URL",
        "MARIADB_URL",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    assert cli._effective_dsn("mysql") is None
    assert cli._effective_dsn("tinydb") is None

    monkeypatch.setenv("MARIADB_URL", "mysql://mariadb-url")
    monkeypatch.setenv("MYSQL_URL", "mysql://mysql-url")
    monkeypatch.setenv("TINYMONGO_MARIADB_DSN", "mysql://mariadb-dsn")
    monkeypatch.setenv("TINYMONGO_MYSQL_DSN", "mysql://mysql-dsn")

    assert cli._effective_dsn("mariadb") == "mysql://mysql-dsn"
    assert cli._effective_dsn("mysql", "mysql://explicit") == "mysql://explicit"
    assert cli._effective_dsn("tinydb", "mysql://irrelevant") is None

    monkeypatch.setenv("TINYMONGO_STORAGE_URI", "s3://environment/root")
    assert cli._effective_storage_uri("tinydb", "s3://irrelevant/root") is None
    assert (
        cli._client(
            str(tmp_path / "local-parquet"),
            "parquet",
            storage_uri="",
        )._storage_uri
        is None
    )
    assert (
        cli._client(
            str(tmp_path / "remote"),
            "mysql",
            dsn="",
        )._dsn
        is None
    )
