import json

import pytest
import tinymongo as tm
from tinymongo import cli


def test_cli_inspect_export_import_and_migrate(tmp_path, capsys):
    source = tmp_path / "source"
    target = tmp_path / "target"
    export_file = tmp_path / "users.json"

    client = tm.TinyMongoClient(str(source))
    users = client.app.users
    users.insert_many([
        {"_id": 1, "name": "Ada"},
        {"_id": 2, "name": "Grace"},
    ])

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

    assert cli.main(["export", str(source), "app", "users", "-o", str(export_file)]) == 0
    assert json.loads(export_file.read_text()) == [
        {"_id": 1, "name": "Ada"},
        {"_id": 2, "name": "Grace"},
    ]

    assert cli.main([
        "import",
        str(target),
        "app",
        "users",
        str(export_file),
        "--mode",
        "replace",
    ]) == 0
    capsys.readouterr()
    assert tm.TinyMongoClient(str(target)).app.users.count() == 2

    migrated = tmp_path / "migrated"
    assert cli.main([
        "migrate",
        str(source),
        str(migrated),
        "--to-backend",
        "sqlite",
        "--database",
        "app",
    ]) == 0
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

    assert cli.main([
        "export",
        str(db_dir),
        "app",
        "users",
        "--backend",
        "sqlite",
    ]) == 0

    assert json.loads(capsys.readouterr().out) == [{"_id": 1, "name": "Ada"}]


def test_cli_import_from_stdin_and_replace_mode(tmp_path, monkeypatch, capsys):
    db_dir = tmp_path / "db"
    client = tm.TinyMongoClient(str(db_dir))
    client.app.users.insert_one({"_id": 1, "name": "old"})
    monkeypatch.setattr(
        "sys.stdin",
        type("Input", (), {"read": lambda self: '[{"_id": 2, "name": "new"}]'})(),
    )

    assert cli.main([
        "import",
        str(db_dir),
        "app",
        "users",
        "-",
        "--mode",
        "replace",
    ]) == 0

    assert "imported 1 documents" in capsys.readouterr().out
    assert list(client.app.users.find({})) == [{"_id": 2, "name": "new"}]


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

    assert cli.main(["migrate", str(source), str(target), "--to-backend", "sqlite"]) == 0

    payload = json.loads(capsys.readouterr().out)
    migrated = {
        (item["database"], item["collection"]): item["count"]
        for item in payload["migrated"]
    }
    assert migrated[("app", "users")] == 1
    assert migrated[("audit", "events")] == 1
    assert tm.TinyMongoClient(str(target), backend="sqlite").audit.events.count() == 1


def test_cli_storage_uri_options_are_passed_through(monkeypatch, tmp_path, capsys):
    calls = []

    class FakeCollection:
        def __init__(self):
            self.docs = [{"_id": 1}]

        def find(self, _filter=None):
            return self.docs

        def delete_many(self, _filter):
            return None

        def insert_many(self, docs):
            self.docs = docs

    class FakeDatabase:
        def __init__(self):
            self.users = FakeCollection()

        def __getitem__(self, name):
            return self.users

        def collection_names(self):
            return ["users"]

    class FakeClient:
        def __init__(self, path, backend, storage_uri=None):
            calls.append((path, backend, storage_uri))
            self.app = FakeDatabase()

        def __getitem__(self, name):
            return self.app

    monkeypatch.setattr(cli, "TinyMongoClient", FakeClient)
    monkeypatch.setattr(cli, "_db_names", lambda path, backend, storage_uri=None: ["app"])

    assert cli.main([
        "export",
        str(tmp_path),
        "app",
        "users",
        "--backend",
        "parquet",
        "--storage-uri",
        "s3://bucket/root",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == [{"_id": 1}]

    assert cli.main([
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
    ]) == 0

    assert calls[0] == (str(tmp_path), "parquet", "s3://bucket/root")
    assert calls[1] == (str(tmp_path / "source"), "parquet", "s3://source/root")
    assert calls[2] == (str(tmp_path / "target"), "parquet", "s3://target/root")


def test_cli_storage_uri_inspect_and_db_names(monkeypatch, tmp_path, capsys):
    class FakeCollection:
        def count(self):
            return 3

    class FakeDatabase:
        def __getitem__(self, name):
            return FakeCollection()

        def collection_names(self):
            return ["users"]

    class FakeClient:
        def __init__(self, path, backend, storage_uri=None):
            self.storage_uri = storage_uri

        def list_database_names(self):
            return ["app"]

        def __getitem__(self, name):
            return FakeDatabase()

    monkeypatch.setattr(cli, "TinyMongoClient", FakeClient)

    assert cli._db_names(str(tmp_path), "parquet", storage_uri="s3://bucket/root") == ["app"]
    assert cli.main([
        "inspect",
        str(tmp_path),
        "--backend",
        "parquet",
        "--storage-uri",
        "s3://bucket/root",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["storage_uri"] == "s3://bucket/root"
    assert payload["databases"][0]["collections"][0]["count"] == 3
