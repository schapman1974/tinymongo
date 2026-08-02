"""Focused coverage for small compatibility and backend branches."""

import re
from types import SimpleNamespace

import pytest

from tinymongo import bson_types, cli
from tinymongo import tinymongo as core
from tinymongo.errors import DuplicateKeyError
from tinymongo.tinymongo import TinyMongoCollection


def test_bson_capabilities_are_enabled_atomically(monkeypatch):
    marker = object()

    monkeypatch.setattr(bson_types, "_ObjectId", marker)
    monkeypatch.setattr(bson_types, "_Binary", marker)
    monkeypatch.setattr(bson_types, "_Decimal128", marker)
    monkeypatch.setattr(bson_types, "_Regex", marker)
    assert bson_types.bson_capabilities() == {
        "objectid": True,
        "binary": True,
        "decimal128": True,
        "regex": True,
    }
    assert bson_types.supported_bson_types()["pymongo"] == (
        "binary",
        "decimal128",
        "objectid",
        "regex",
    )

    monkeypatch.setattr(bson_types, "_Regex", None)
    assert not any(bson_types.bson_capabilities().values())
    assert bson_types.supported_bson_types()["pymongo"] == ()
    monkeypatch.setattr(bson_types, "_Regex", marker)
    monkeypatch.setattr(bson_types, "_Binary", None)
    assert bson_types.bson_capabilities() == {
        "objectid": False,
        "binary": False,
        "decimal128": False,
        "regex": False,
    }

    monkeypatch.setattr(bson_types, "_ObjectId", None)
    monkeypatch.setattr(bson_types, "_Binary", marker)
    assert bson_types.bson_capabilities() == {
        "objectid": False,
        "binary": False,
        "decimal128": False,
        "regex": False,
    }


def test_bson_recursive_equality_handles_mixed_and_unregistered_values():
    marker = object()

    assert bson_types.bson_values_equal({"value": 1}, 1) is False
    assert bson_types.bson_values_equal(marker, marker) is True
    assert bson_types.bson_values_equal(object(), object()) is False


def test_cli_copies_unique_and_nonunique_indexes():
    class Source:
        def list_indexes(self):
            return [
                {"name": "_id_", "key": [("_id", 1)]},
                {"name": "name_1", "key": [("name", 1)]},
                {
                    "name": "email_1",
                    "key": [("email", 1)],
                    "unique": True,
                },
            ]

    class Target:
        def __init__(self):
            self.created = []

        def create_index(self, keys, **options):
            self.created.append((keys, options))

    target = Target()

    cli._copy_indexes(Source(), target)

    assert target.created == [
        ([("name", 1)], {"name": "name_1"}),
        ([("email", 1)], {"name": "email_1", "unique": True}),
    ]


class _ReplacementCollection:
    def __init__(self, previous, failing_insert_calls):
        self.documents = list(previous)
        self.failing_insert_calls = set(failing_insert_calls)
        self.insert_calls = 0

    def list_indexes(self):
        return [{"name": "_id_", "key": [("_id", 1)]}]

    def find(self, _filter):
        return list(self.documents)

    def delete_many(self, _filter):
        self.documents = []

    def insert_many(self, documents):
        self.insert_calls += 1
        if self.insert_calls in self.failing_insert_calls:
            raise RuntimeError("insert failure {0}".format(self.insert_calls))
        self.documents.extend(documents)


@pytest.mark.parametrize("previous", [[{"_id": "old"}], []])
def test_cli_replace_collection_restores_or_preserves_previous_data(previous):
    collection = _ReplacementCollection(previous, failing_insert_calls={1})

    with pytest.raises(RuntimeError, match="insert failure 1"):
        cli._replace_collection(
            {"items": collection},
            "items",
            [{"_id": "new"}],
        )

    assert collection.documents == previous


def test_cli_replace_collection_reports_a_rollback_failure():
    collection = _ReplacementCollection(
        [{"_id": "old"}],
        failing_insert_calls={1, 2},
    )

    with pytest.raises(
        RuntimeError,
        match="previous data could not be restored",
    ) as caught:
        cli._replace_collection(
            {"items": collection},
            "items",
            [{"_id": "new"}],
        )

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "insert failure 2"


def test_cli_migrate_records_an_empty_collection_without_inserting(monkeypatch, capsys):
    class SourceCollection:
        def __init__(self, documents):
            self.documents = documents

        def find(self, _filter):
            return self.documents

    class TargetCollection:
        def __init__(self, documents=None):
            self.documents = list(documents or [])
            self.deleted = False
            self.inserted = None
            self.dropped = False

        def find(self, _filter):
            return list(self.documents)

        def list_indexes(self):
            return [{"name": "_id_", "key": [("_id", 1)]}]

        def delete_many(self, _filter):
            self.deleted = True
            self.documents = []

        def insert_many(self, documents):
            self.inserted = list(documents)
            self.documents.extend(self.inserted)

        def drop(self):
            self.dropped = True

    source_collections = {
        "empty": SourceCollection([]),
        "filled": SourceCollection([{"_id": 1}]),
    }
    target_collections = {
        "empty": TargetCollection(),
        "filled": TargetCollection(),
    }

    class Database:
        def __init__(self, collections):
            self.collections = collections

        def __getitem__(self, name):
            return self.collections.setdefault(name, TargetCollection())

        def list_collection_names(self):
            return list(self.collections)

    clients = iter(
        [
            {"app": Database(source_collections)},
            {"app": Database(target_collections)},
        ]
    )
    monkeypatch.setattr(cli, "_client", lambda *args, **kwargs: next(clients))
    args = SimpleNamespace(
        source="source",
        target="target",
        from_backend="tinydb",
        to_backend="sqlite",
        source_uri=None,
        target_uri=None,
        source_dsn=None,
        target_dsn=None,
        database="app",
        output="-",
    )

    assert cli.cmd_migrate(args) == 0
    assert target_collections["empty"].deleted is True
    assert target_collections["empty"].inserted is None
    assert target_collections["filled"].inserted == [{"_id": 1}]
    assert '"count": 0' in capsys.readouterr().out


@pytest.mark.parametrize("ordered", [True, False])
def test_persistent_native_insert_rejection_returns_bulk_errors(ordered):
    class Engine:
        def __init__(self):
            self.attempts = 0

        def find(self, _collection, _filter):
            return []

        def get_index_specs(self, _collection):
            return []

        def insert_many(
            self,
            _collection,
            _documents,
            bypass_document_validation=False,
        ):
            self.attempts += 1
            raise DuplicateKeyError("persistent conflict")

    engine = Engine()
    collection = TinyMongoCollection(
        "items",
        SimpleNamespace(name="app", engine=engine),
    )
    document = {"_id": "conflict"}

    eids, accepted, write_errors = core._execute_engine_insert_many(
        collection,
        [document],
        ordered=ordered,
        bypass_document_validation=False,
    )

    assert engine.attempts == 3
    assert eids == []
    assert accepted == []
    assert [error["index"] for error in write_errors] == [0]


def test_direct_id_and_cached_match_helpers_cover_operator_routes():
    assert core._direct_id_equality({"_id": {"$eq": 1}}) == 1
    assert core._direct_id_equality({"_id": {"$in": [1]}}) is core._MISSING
    expression = re.compile("id")
    assert core._direct_id_equality({"_id": expression}) is core._MISSING
    assert core._direct_id_equality({"_id": {"$eq": expression}}) is core._MISSING
    assert core._cached_value_matches(1.0, 1, ("number", 1)) is True


def test_tinydb_updates_and_replacements_use_native_query_routes(tmp_path):
    client = core.TinyMongoClient(str(tmp_path / "db"))
    collection = client.app.people
    collection.insert_one({"_id": 1, "name": "Ada", "score": 1})

    updated = collection.update_one(
        {"score": {"$exists": True}}, {"$set": {"score": 2}}
    )
    replaced = collection.replace_one({"score": {"$exists": True}}, {"name": "Grace"})

    assert updated.matched_count == 1
    assert updated.modified_count == 1
    assert replaced.matched_count == 1
    assert collection.find_one({"_id": 1}) == {"_id": 1, "name": "Grace"}
    client.close()


def test_build_table_delegates_to_a_table_backend():
    class Engine:
        def __init__(self):
            self.created = []

        def create_collection(self, name):
            self.created.append(name)

    engine = Engine()
    collection = TinyMongoCollection(
        "events",
        SimpleNamespace(engine=engine),
    )

    collection.build_table()

    assert engine.created == ["events"]
    assert collection.table is None
