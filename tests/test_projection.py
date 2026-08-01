import pytest

import tinymongo as tm
from tinymongo.errors import OperationFailure, TinyMongoNotSupportedError
from tinymongo.projection import normalize_projection, project_document


@pytest.fixture
def collection():
    client = tm.TinyMongoClient(backend="memory")
    collection = client.app.items
    collection.insert_many(
        [
            {
                "_id": 1,
                "name": "Ada",
                "secret": "x",
                "score": 7,
                "profile": {"email": "ada@example.com", "age": 36},
                "items": [
                    {"sku": "a", "qty": 1},
                    {"qty": 2},
                    {},
                    "scalar",
                    None,
                ],
            },
            {
                "_id": 2,
                "name": "Grace",
                "score": 9,
                "profile": {"age": 40},
                "items": [],
            },
            {"_id": 3, "name": "Lin", "score": 8, "profile": "unknown"},
        ]
    )
    try:
        yield collection
    finally:
        client.close()


def test_projection_inclusion_sequences_and_id_rules(collection):
    assert list(collection.find({}, {"name": 1}).sort("_id", 1)) == [
        {"_id": 1, "name": "Ada"},
        {"_id": 2, "name": "Grace"},
        {"_id": 3, "name": "Lin"},
    ]
    assert collection.find_one({"_id": 1}, ["name"]) == {
        "_id": 1,
        "name": "Ada",
    }
    assert collection.find_one({"_id": 1}, ("name",)) == {
        "_id": 1,
        "name": "Ada",
    }
    assert collection.find_one({"_id": 1}, ["name", "name"]) == {
        "_id": 1,
        "name": "Ada",
    }
    assert collection.find_one({"_id": 1}, {"name"}) == {
        "_id": 1,
        "name": "Ada",
    }
    assert collection.find_one({"_id": 1}, {"name": 2, "_id": 0}) == {"name": "Ada"}
    assert project_document(
        {"_id": 1, "name": "Ada", "secret": True},
        normalize_projection({"name": 1, "_id": 0}),
    ) == {"name": "Ada"}
    assert collection.find_one({"_id": 1}, {"_id": 1}) == {"_id": 1}
    assert collection.find_one({"_id": 1}, {"_id": 0}) == {
        "name": "Ada",
        "secret": "x",
        "score": 7,
        "profile": {"email": "ada@example.com", "age": 36},
        "items": [
            {"sku": "a", "qty": 1},
            {"qty": 2},
            {},
            "scalar",
            None,
        ],
    }


def test_projection_exclusion_nested_paths_and_id_rules(collection):
    projected = collection.find_one(
        {"_id": 1}, {"secret": 0, "profile.age": 0, "_id": 1}
    )
    assert projected == {
        "_id": 1,
        "name": "Ada",
        "score": 7,
        "profile": {"email": "ada@example.com"},
        "items": [
            {"sku": "a", "qty": 1},
            {"qty": 2},
            {},
            "scalar",
            None,
        ],
    }
    assert "missing" not in collection.find_one({"_id": 2}, {"missing": 0})


def test_nested_inclusion_merges_siblings_and_projects_arrays(collection):
    assert collection.find_one(
        {"_id": 1}, {"profile.email": 1, "profile.age": 1, "_id": 0}
    ) == {"profile": {"email": "ada@example.com", "age": 36}}
    assert collection.find_one({"_id": 1}, {"profile": {"email": 1}}) == {
        "_id": 1,
        "profile": {"email": "ada@example.com"},
    }
    assert collection.find_one({"_id": 2}, {"profile.email": 1}) == {
        "_id": 2,
        "profile": {},
    }
    assert collection.find_one({"_id": 3}, {"profile.email": 1}) == {"_id": 3}
    assert collection.find_one({"_id": 1}, {"items.sku": 1}) == {
        "_id": 1,
        "items": [{"sku": "a"}, {}, {}],
    }
    assert collection.find_one({"_id": 1}, {"items.sku": 0})["items"] == [
        {"qty": 1},
        {"qty": 2},
        {},
        "scalar",
        None,
    ]


def test_nested_id_projection_follows_regular_path_semantics(collection):
    full = collection.find_one({"_id": 1})

    assert collection.find_one({"_id": 1}, {"_id.missing": 1}) == {}
    assert collection.find_one({"_id": 1}, {"_id.missing": 0}) == full
    assert project_document(
        {"_id": {"value": 1, "other": 2}, "name": "Ada"},
        normalize_projection({"_id.value": 1}),
    ) == {"_id": {"value": 1}}
    assert project_document({"name": "Ada"}, normalize_projection({"name": 1})) == {
        "name": "Ada"
    }


def test_projection_is_applied_after_sort_and_on_every_cursor_access(collection):
    cursor = collection.find({}, {"name": 1}).sort("score", -1)
    assert cursor.next() == {"_id": 2, "name": "Grace"}
    assert cursor[1] == {"_id": 3, "name": "Lin"}

    cursor = collection.find({}, {"name": 1}, sort=[("score", 1)])
    assert list(cursor) == [
        {"_id": 1, "name": "Ada"},
        {"_id": 3, "name": "Lin"},
        {"_id": 2, "name": "Grace"},
    ]
    cursor = collection.find({"name": "Ada"}, {"name": 1})
    assert cursor[0] == {"_id": 1, "name": "Ada"}
    assert cursor["name"] == "Ada"


def test_projection_works_with_index_fast_path_and_is_mutation_safe(collection):
    collection.create_index("name")
    result = collection.find_one({"name": "Ada"}, {"profile.email": 1})
    result["profile"]["email"] = "changed@example.com"

    assert collection.find_one({"name": "Ada"})["profile"]["email"] == (
        "ada@example.com"
    )
    assert list(collection.find({"name": "Ada"}, {"name": 1})) == [
        {"_id": 1, "name": "Ada"}
    ]


def test_empty_projection_filter_alias_and_capability(collection):
    full = collection.find_one({"_id": 1})
    assert collection.find_one(filter={"_id": 1}, projection={}) == full
    assert collection.find_one({"_id": 1}, []) == full
    assert list(collection.find(filter={"_id": 1}, projection=[])) == [full]
    assert collection.parent.database == "app"
    assert tm.TinyMongoClient(backend="memory").capabilities()["projections"] is True


@pytest.mark.parametrize(
    "projection,code",
    [
        ({"name": 1, "secret": 0}, 31254),
        ({"secret": 0, "name": 1}, 31253),
        ({"profile": 1, "profile.email": 1}, 31249),
        ({"profile.email": 1, "profile": 1}, 31250),
        ({"profile": 1, "profile.email": 0}, 31249),
        ({"profile.email": 0, "profile": 1}, 31250),
        ({"_id": 1, "_id.value": 1}, 31249),
        ({"_id.value": 1, "_id": 1}, 31250),
        ({"_id": 0, "_id.value": 1}, 31249),
        ({"profile": {"email": 1}, "profile.email": 1}, 31250),
        ({"profile.email": 1, "profile": {"email": 1}}, 31250),
    ],
)
def test_invalid_projection_combinations_raise_operation_failure(
    collection, projection, code
):
    with pytest.raises(OperationFailure) as error:
        list(collection.find({}, projection))
    assert error.value.code == code


@pytest.mark.parametrize("projection", [42, "name", ["name", 1], {1: 1}])
def test_projection_requires_a_mapping_or_string_field_sequence(projection):
    with pytest.raises(TypeError):
        normalize_projection(projection)


@pytest.mark.parametrize("projection", [{"": 1}, {"a..b": 1}, {"bad\x00key": 1}])
def test_invalid_projection_paths_raise_operation_failure(projection):
    with pytest.raises(OperationFailure):
        normalize_projection(projection)


@pytest.mark.parametrize(
    "projection",
    [
        {"items.0.sku": 1},
        {"items.$": 1},
        {"name": {"$meta": "textScore"}},
        {"name": {}},
        {"name": "$other"},
        {"name": None},
        {"name": [1]},
    ],
)
def test_unsupported_projection_expressions_fail_clearly(projection):
    with pytest.raises(TinyMongoNotSupportedError):
        normalize_projection(projection)


def test_projector_handles_nested_arrays_and_scalar_branches():
    document = {
        "_id": 1,
        "groups": [[], [{"name": "a", "other": 1}], "scalar"],
        "scalar": "value",
        "parent": {"scalar": "value", "kept": {"name": "yes"}},
    }
    spec = normalize_projection(
        {
            "groups.name": 1,
            "scalar.child": 1,
            "missing.child": 1,
            "parent.scalar.child": 1,
            "parent.kept.name": 1,
        }
    )
    assert project_document(document, spec) == {
        "_id": 1,
        "groups": [[], [{"name": "a"}]],
        "parent": {"kept": {"name": "yes"}},
    }
    assert project_document(None, spec) is None


def test_projector_exclusion_skips_missing_and_scalar_branches():
    document = {
        "_id": 1,
        "secret": "x",
        "profile": "unknown",
    }
    spec = normalize_projection({"missing": 0, "secret": 0, "profile.email": 0})
    assert project_document(document, spec) == {
        "_id": 1,
        "profile": "unknown",
    }
