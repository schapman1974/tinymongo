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
