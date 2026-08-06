"""Semantic contracts for compound, sparse, and partial indexes."""

import sqlite3
from uuid import uuid4

import pytest

import tinymongo
from tinymongo import table_backends
from tinymongo.errors import DuplicateKeyError, TinyMongoNotSupportedError
from tinymongo.indexes import IndexSpec
from tinymongo.storage_backends import clear_memory_namespace


SEMANTIC_BACKENDS = [
    pytest.param("memory", id="memory"),
    pytest.param("tinydb", id="json"),
    pytest.param("sqlite", id="sqlite"),
]


class AdvancedIndexBackend:
    """Open fresh clients against one durable test namespace."""

    def __init__(self, backend, address):
        self.backend = backend
        self.address = address
        self.clients = []

    def open(self):
        client = tinymongo.TinyMongoClient(self.address, backend=self.backend)
        self.clients.append(client)
        return client

    def close(self, client):
        client.close()

    def close_all(self):
        for client in reversed(self.clients):
            client.close()
        if self.backend == "memory":
            clear_memory_namespace(self.address)


@pytest.fixture(params=SEMANTIC_BACKENDS)
def advanced_index_backend(request, tmp_path):
    backend = request.param
    address = (
        "memory://advanced-index-{0}".format(uuid4().hex)
        if backend == "memory"
        else str(tmp_path / backend)
    )
    target = AdvancedIndexBackend(backend, address)
    try:
        yield target
    finally:
        target.close_all()


def _indexes_by_name(collection):
    return {index["name"]: index for index in collection.list_indexes()}


def test_advanced_index_metadata_round_trips_through_public_apis_and_restart(
    advanced_index_backend,
):
    first = advanced_index_backend.open()
    items = first.app.items

    assert items.create_indexes(
        [
            {
                "key": [("tenant", 1), ("email", 1)],
                "name": "tenant_email",
                "unique": True,
            },
            {"key": [("alias", 1)], "name": "alias_sparse", "sparse": True},
            {
                "key": [("sku", 1)],
                "name": "active_sku",
                "unique": True,
                "partialFilterExpression": {"active": True},
            },
        ]
    ) == ["tenant_email", "alias_sparse", "active_sku"]

    expected = {
        "_id_": {"name": "_id_", "key": [("_id", 1)]},
        "tenant_email": {
            "name": "tenant_email",
            "key": [("tenant", 1), ("email", 1)],
            "unique": True,
        },
        "alias_sparse": {
            "name": "alias_sparse",
            "key": [("alias", 1)],
            "sparse": True,
        },
        "active_sku": {
            "name": "active_sku",
            "key": [("sku", 1)],
            "unique": True,
            "partialFilterExpression": {"active": True},
        },
    }
    assert _indexes_by_name(items) == expected
    assert items.index_information() == {
        name: {key: value for key, value in metadata.items() if key != "name"}
        for name, metadata in expected.items()
    }

    # Public metadata must be a defensive copy, including the partial filter.
    exposed = _indexes_by_name(items)
    exposed["tenant_email"]["key"].append(("mutated", 1))
    exposed["active_sku"]["partialFilterExpression"]["active"] = False
    assert _indexes_by_name(items) == expected

    advanced_index_backend.close(first)
    reopened = advanced_index_backend.open()
    items = reopened.app.items
    assert _indexes_by_name(items) == expected

    items.insert_one(
        {"_id": 1, "tenant": "one", "email": "same", "sku": "sku", "active": True}
    )
    with pytest.raises(DuplicateKeyError):
        items.insert_one(
            {
                "_id": 2,
                "tenant": "one",
                "email": "same",
                "sku": "other",
                "active": False,
            }
        )
    with pytest.raises(DuplicateKeyError):
        items.insert_one(
            {
                "_id": 3,
                "tenant": "two",
                "email": "other",
                "sku": "sku",
                "active": True,
            }
        )


def test_unique_compound_index_enforces_insert_update_and_upsert_atomically(
    advanced_index_backend,
):
    client = advanced_index_backend.open()
    users = client.app.users
    users.create_index(
        [("tenant", 1), ("username", 1)],
        name="tenant_username",
        unique=True,
    )
    users.insert_many(
        [
            {"_id": 1, "tenant": "north", "username": "ada"},
            {"_id": 2, "tenant": "south", "username": "ada"},
            {"_id": 3, "tenant": "north", "username": "grace"},
        ]
    )

    with pytest.raises(DuplicateKeyError):
        users.insert_one({"_id": 4, "tenant": "north", "username": "ada"})
    with pytest.raises(DuplicateKeyError):
        users.update_one({"_id": 3}, {"$set": {"username": "ada"}})
    with pytest.raises(DuplicateKeyError):
        users.update_one(
            {"_id": 5},
            {"$set": {"tenant": "north", "username": "ada"}},
            upsert=True,
        )

    assert users.find_one({"_id": 3}) == {
        "_id": 3,
        "tenant": "north",
        "username": "grace",
    }
    assert users.find_one({"_id": 5}) is None

    result = users.update_one(
        {"_id": 6},
        {"$set": {"tenant": "north", "username": "linus"}},
        upsert=True,
    )
    assert result.upserted_id == 6
    assert users.find_one({"_id": 6})["username"] == "linus"


def test_compound_unique_keys_preserve_ordered_tuples_and_missing_values(
    advanced_index_backend,
):
    client = advanced_index_backend.open()
    values = client.app.values
    values.create_index([("left", 1), ("right", 1)], unique=True)

    values.insert_many(
        [
            {"_id": 1, "left": "a", "right": "b"},
            {"_id": 2, "left": "b", "right": "a"},
            {"_id": 3, "left": "a"},
            {"_id": 4, "left": "b"},
            {"_id": 5, "right": "a"},
            {"_id": 6},
        ]
    )

    duplicate_documents = [
        {"_id": 7, "left": "a", "right": "b"},
        {"_id": 8, "left": "a", "right": None},
        {"_id": 9, "left": None, "right": "a"},
        {"_id": 10, "left": None, "right": None},
    ]
    for document in duplicate_documents:
        with pytest.raises(DuplicateKeyError):
            values.insert_one(document)

    assert values.count_documents({}) == 6


def test_unique_compound_index_supports_one_flat_multikey_component(
    advanced_index_backend,
):
    client = advanced_index_backend.open()
    items = client.app.items
    items.create_index(
        [("owner.id", 1), ("labels", 1)],
        name="owner_labels",
        unique=True,
    )

    # Repeated values inside one document create one entry, not a conflict
    # with that same document. The owner remains part of every compound tuple.
    items.insert_one(
        {
            "_id": 1,
            "owner": {"id": "north"},
            "labels": ["red", "blue", "red"],
        }
    )
    items.insert_one({"_id": 2, "owner": {"id": "south"}, "labels": ["blue"]})
    items.insert_one({"_id": 3, "owner": {"id": "north"}, "labels": ["green"]})

    with pytest.raises(DuplicateKeyError):
        items.insert_one(
            {
                "_id": 4,
                "owner": {"id": "north"},
                "labels": ["yellow", "blue"],
            }
        )

    assert items.find_one({"_id": 4}) is None
    assert items.count_documents({}) == 3


def test_compound_parallel_arrays_fail_clearly_and_atomically(
    advanced_index_backend,
):
    client = advanced_index_backend.open()
    items = client.app.items
    items.create_index(
        [("regions", 1), ("labels", 1)],
        name="region_labels",
        unique=True,
    )

    with pytest.raises(
        TinyMongoNotSupportedError,
        match="cannot index parallel array fields: regions, labels",
    ):
        items.insert_one({"_id": 1, "regions": ["east", "west"], "labels": ["a", "b"]})
    assert items.find_one({"_id": 1}) is None

    items.insert_one({"_id": 2, "regions": "east", "labels": "a"})
    with pytest.raises(TinyMongoNotSupportedError, match="parallel array fields"):
        items.update_one(
            {"_id": 2},
            {"$set": {"regions": ["east", "west"], "labels": ["a", "b"]}},
        )
    assert items.find_one({"_id": 2}) == {
        "_id": 2,
        "regions": "east",
        "labels": "a",
    }


def test_unique_sparse_index_skips_missing_but_indexes_explicit_null(
    advanced_index_backend,
):
    client = advanced_index_backend.open()
    users = client.app.users
    users.create_index("email", name="email_sparse", unique=True, sparse=True)

    users.insert_many([{"_id": 1}, {"_id": 2}, {"_id": 3, "email": None}])
    with pytest.raises(DuplicateKeyError):
        users.insert_one({"_id": 4, "email": None})
    with pytest.raises(DuplicateKeyError):
        users.update_one({"_id": 1}, {"$set": {"email": None}})

    assert "email" not in users.find_one({"_id": 1})
    users.update_one({"_id": 3}, {"$unset": {"email": ""}})
    users.insert_one({"_id": 4, "email": None})
    result = users.update_one({"_id": 5}, {"$set": {"other": True}}, upsert=True)
    assert result.upserted_id == 5
    assert users.count_documents({"email": {"$exists": False}}) == 4


def test_sparse_compound_membership_uses_any_present_field_and_complete_tuple(
    advanced_index_backend,
):
    client = advanced_index_backend.open()
    values = client.app.values
    values.create_index(
        [("left", 1), ("right", 1)],
        name="sparse_pair",
        sparse=True,
        unique=True,
    )

    # Fully missing documents are absent from the sparse index.
    values.insert_many([{"_id": 1}, {"_id": 2}])
    values.insert_many(
        [
            {"_id": 3, "left": "a"},
            {"_id": 4, "right": "a"},
            {"_id": 5, "left": "a", "right": "a"},
            {"_id": 6, "left": None, "right": None},
        ]
    )

    with pytest.raises(DuplicateKeyError):
        values.insert_one({"_id": 7, "left": "a", "right": None})
    with pytest.raises(DuplicateKeyError):
        values.insert_one({"_id": 8, "left": None, "right": "a"})
    with pytest.raises(DuplicateKeyError):
        values.insert_one({"_id": 9, "right": None})

    assert values.count_documents({}) == 6


def test_partial_unique_membership_transitions_on_update_and_upsert(
    advanced_index_backend,
):
    client = advanced_index_backend.open()
    users = client.app.users
    users.create_index(
        "email",
        name="active_email",
        unique=True,
        partialFilterExpression={"active": True},
    )
    users.insert_many(
        [
            {"_id": 1, "email": "same", "active": True},
            {"_id": 2, "email": "same", "active": False},
            {"_id": 3, "email": "same"},
        ]
    )

    with pytest.raises(DuplicateKeyError):
        users.update_one({"_id": 2}, {"$set": {"active": True}})
    assert users.find_one({"_id": 2})["active"] is False

    users.update_one({"_id": 1}, {"$set": {"active": False}})
    users.update_one({"_id": 2}, {"$set": {"active": True}})
    assert users.find_one({"_id": 2})["active"] is True

    with pytest.raises(DuplicateKeyError):
        users.update_one(
            {"_id": 4},
            {"$set": {"email": "same", "active": True}},
            upsert=True,
        )
    assert users.find_one({"_id": 4}) is None

    result = users.update_one(
        {"_id": 5},
        {"$set": {"email": "same", "active": False}},
        upsert=True,
    )
    assert result.upserted_id == 5


def test_partial_filter_supports_boolean_range_in_type_and_logical_predicates(
    advanced_index_backend,
):
    client = advanced_index_backend.open()
    items = client.app.items
    expression = {
        "$and": [
            {"enabled": True},
            {"score": {"$gte": 10, "$lt": 20}},
            {"tier": {"$in": ["pro", "team"]}},
            {"code": {"$exists": True, "$type": "string"}},
        ]
    }
    items.create_index(
        "slug",
        name="eligible_slug",
        unique=True,
        partialFilterExpression=expression,
    )

    items.insert_many(
        [
            {
                "_id": 1,
                "slug": "same",
                "enabled": True,
                "score": 12,
                "tier": "pro",
                "code": "A",
            },
            {
                "_id": 2,
                "slug": "same",
                "enabled": True,
                "score": 9,
                "tier": "pro",
                "code": "B",
            },
            {
                "_id": 3,
                "slug": "same",
                "enabled": True,
                "score": 12,
                "tier": "free",
                "code": "C",
            },
            {
                "_id": 4,
                "slug": "same",
                "enabled": True,
                "score": 12,
                "tier": "team",
                "code": 4,
            },
        ]
    )

    with pytest.raises(DuplicateKeyError):
        items.insert_one(
            {
                "_id": 5,
                "slug": "same",
                "enabled": True,
                "score": 19,
                "tier": "team",
                "code": "E",
            }
        )
    assert items.count_documents({}) == 4


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"partialFilterExpression": []}, "must be a mapping"),
        ({"partialFilterExpression": {}}, "non-empty mapping"),
        (
            {"partialFilterExpression": {"active": {"$exists": False}}},
            "only \\$exists: true",
        ),
        (
            {"partialFilterExpression": {"tier": {"$in": "pro"}}},
            "requires an array",
        ),
        (
            {"partialFilterExpression": {"age": {"$gte": 18, "unit": "years"}}},
            "cannot mix operators",
        ),
        (
            {"partialFilterExpression": {"email": {"$ne": None}}},
            "Unsupported partialFilterExpression operator",
        ),
        (
            {"partialFilterExpression": {"$nor": [{"active": True}]}},
            "Unsupported partialFilterExpression operator",
        ),
        (
            {
                "sparse": True,
                "partialFilterExpression": {"active": True},
            },
            "cannot be combined",
        ),
        ({"sparse": 1}, "must be a boolean"),
        ({"unique": 1}, "must be a boolean"),
        ({"collation": {"locale": "en"}}, "Unsupported index option"),
    ],
)
def test_invalid_advanced_index_predicates_and_options_leave_no_metadata(
    tmp_path, options, message
):
    client = tinymongo.TinyMongoClient(str(tmp_path / uuid4().hex), backend="sqlite")
    items = client.app.items
    try:
        with pytest.raises(TinyMongoNotSupportedError, match=message):
            items.create_index("value", **options)
        assert items.list_indexes() == [{"name": "_id_", "key": [("_id", 1)]}]
    finally:
        client.close()


@pytest.mark.parametrize(
    "keys",
    [
        [("tenant", 1), ("tenant", 1)],
        [("tenant", 1), ("email", -1)],
        [("tenant", 1), ("", 1)],
    ],
)
def test_invalid_compound_key_definitions_leave_no_metadata(tmp_path, keys):
    client = tinymongo.TinyMongoClient(str(tmp_path / uuid4().hex), backend="sqlite")
    items = client.app.items
    try:
        with pytest.raises(TinyMongoNotSupportedError):
            items.create_index(keys)
        assert items.list_indexes() == [{"name": "_id_", "key": [("_id", 1)]}]
    finally:
        client.close()


def test_sqlite_materializes_compound_sparse_and_partial_native_indexes(tmp_path):
    client = tinymongo.TinyMongoClient(
        str(tmp_path / "native-indexes"), backend="sqlite"
    )
    items = client.app.items
    items.create_index(
        [("tenant", 1), ("email", 1)],
        name="tenant_email",
        unique=True,
    )
    items.create_index("alias", name="alias_sparse", sparse=True)
    items.create_index(
        "sku",
        name="active_sku",
        unique=True,
        partialFilterExpression={"active": True},
    )

    engine = items.parent.engine
    specs = {spec.name: spec for spec in engine.get_index_specs("items")}
    physical = {
        name: engine._physical_index_name("items", spec) for name, spec in specs.items()
    }
    conn = sqlite3.connect(engine.path)
    try:
        index_rows = {
            row[1]: row for row in conn.execute('PRAGMA index_list("items")').fetchall()
        }
        assert index_rows[physical["tenant_email"]][2] == 1
        assert index_rows[physical["tenant_email"]][4] == 0
        assert index_rows[physical["alias_sparse"]][2] == 0
        assert index_rows[physical["alias_sparse"]][4] == 1
        assert index_rows[physical["active_sku"]][2] == 1
        assert index_rows[physical["active_sku"]][4] == 1

        # The unique base index stores one exact BSON-aware tuple token. Its
        # companion lookup index stores the two native JSON expressions used
        # for query candidate selection.
        compound_constraint_parts = [
            row
            for row in conn.execute(
                'PRAGMA index_xinfo("{0}")'.format(physical["tenant_email"])
            ).fetchall()
            if row[5]
        ]
        compound_lookup_parts = [
            row
            for row in conn.execute(
                'PRAGMA index_xinfo("{0}_lookup")'.format(physical["tenant_email"])
            ).fetchall()
            if row[5]
        ]
        assert len(compound_constraint_parts) == 1
        assert len(compound_lookup_parts) == 2

        definitions = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "tinymongo_index_token" in definitions[physical["tenant_email"]]
        assert (
            definitions[physical["tenant_email"] + "_lookup"].count("json_extract") == 2
        )
        assert " WHERE " in definitions[physical["alias_sparse"]]
        assert "json_type" in definitions[physical["alias_sparse"]]
        assert " WHERE " in definitions[physical["active_sku"]]
        assert "tinymongo_index_member" in definitions[physical["active_sku"]]
    finally:
        conn.close()
        client.close()


def test_sqlite_compound_index_limits_decoding_before_exact_matching(
    tmp_path, monkeypatch
):
    client = tinymongo.TinyMongoClient(
        str(tmp_path / "compound-query"), backend="sqlite"
    )
    items = client.app.items
    documents = [
        {
            "_id": number,
            "tenant": "wanted" if number % 100 == 0 else "other",
            "kind": "match" if number % 200 == 0 else "miss",
            "payload": "x" * 500,
        }
        for number in range(1_000)
    ]
    items.insert_many(documents)
    items.create_index([("tenant", 1), ("kind", 1)], name="tenant_kind")

    decoded = []
    original_loads = table_backends._json_loads

    def track_loads(value):
        decoded.append(value)
        return original_loads(value)

    monkeypatch.setattr(table_backends, "_json_loads", track_loads)
    try:
        found = list(items.find({"tenant": "wanted", "kind": "match"}))
        assert [document["_id"] for document in found] == [0, 200, 400, 600, 800]
        assert len(decoded) <= 10
    finally:
        client.close()


@pytest.mark.parametrize(
    ("name", "options", "member", "nonmember"),
    [
        (
            "sparse_tags",
            {"sparse": True},
            {"tags": ["red", "red", "blue"]},
            {},
        ),
        (
            "active_tags",
            {"partialFilterExpression": {"active": True}},
            {"tags": ["red", "red", "blue"], "active": True},
            {"tags": ["blue"], "active": False},
        ),
    ],
    ids=["sparse", "partial"],
)
def test_sqlite_conditional_unique_single_field_multikey_indexes(
    tmp_path, name, options, member, nonmember
):
    client = tinymongo.TinyMongoClient(
        str(tmp_path / "conditional-multikey"), backend="sqlite"
    )
    items = client.app.items
    items.create_index("tags", name=name, unique=True, **options)

    first = {"_id": 1}
    first.update(member)
    excluded = {"_id": 2}
    excluded.update(nonmember)
    items.insert_one(first)
    items.insert_one(excluded)

    overlap = {"_id": 3, "tags": ["green", "blue"]}
    if "partialFilterExpression" in options:
        overlap["active"] = True
    with pytest.raises(DuplicateKeyError):
        items.insert_one(overlap)

    disjoint = {"_id": 4, "tags": ["green", "yellow"]}
    if "partialFilterExpression" in options:
        disjoint["active"] = True
    items.insert_one(disjoint)

    assert items.find_one({"_id": 3}) is None
    assert {document["_id"] for document in items.find({})} == {1, 2, 4}
    client.close()


def test_index_spec_metadata_v2_preserves_advanced_definition():
    spec = IndexSpec(
        keys=[("tenant", 1), ("email", 1)],
        name="active_tenant_email",
        unique=True,
        partial_filter={"active": True},
    )

    assert IndexSpec.from_metadata(spec.to_metadata()) == spec
    assert spec.to_metadata() == {
        "v": 2,
        "name": "active_tenant_email",
        "key": [["tenant", 1], ["email", 1]],
        "unique": True,
        "sparse": False,
        "partialFilterExpression": {"active": True},
    }
