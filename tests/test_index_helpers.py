import json

import pytest

from tinymongo.errors import DuplicateKeyError, TinyMongoNotSupportedError
from tinymongo.indexes import (
    INDEX_METADATA_VERSION,
    IndexSpec,
    index_catalog_id,
    index_tokens,
    parse_index_spec,
    validate_unique_documents,
)


def test_catalog_identity_is_unambiguous():
    assert index_catalog_id("a:b", "c") != index_catalog_id("a", "b:c")
    assert json.loads(index_catalog_id("café", "名前")) == ["café", "名前"]


def test_parse_string_index_uses_canonical_name():
    spec = parse_index_spec("profile.email")

    assert spec == IndexSpec(
        field="profile.email",
        direction=1,
        unique=False,
        name="profile.email_1",
    )


@pytest.mark.parametrize(
    "key",
    [
        ("email", 1),
        ["email", 1],
        [("email", 1)],
        [["email", 1]],
    ],
)
def test_parse_accepts_one_ascending_pair(key):
    assert parse_index_spec(key).field == "email"


def test_parse_normalizes_supported_options():
    spec = parse_index_spec("email", unique=True, name="login_email")

    assert spec.unique is True
    assert spec.name == "login_email"


@pytest.mark.parametrize(
    "key",
    [
        [],
        [("first", 1), ("last", 1)],
        ("email", -1),
        ("email", "text"),
        ("email", "hashed"),
        ("email", True),
        [("email", 1, "extra")],
        [(("not-a-string",), 1)],
        42,
    ],
)
def test_parse_rejects_unsupported_keys(key):
    with pytest.raises(TinyMongoNotSupportedError):
        parse_index_spec(key)


@pytest.mark.parametrize(
    "option",
    [
        {"expireAfterSeconds": 10},
        {"sparse": True},
        {"partialFilterExpression": {"active": True}},
        {"collation": {"locale": "en"}},
        {"wildcardProjection": {"field": 1}},
        {"background": True},
    ],
)
def test_parse_rejects_unknown_semantic_options(option):
    with pytest.raises(TinyMongoNotSupportedError, match="Unsupported index option"):
        parse_index_spec("email", **option)


@pytest.mark.parametrize(
    "field", ["", ".email", "email.", "a..b", "$**", "a.$**", "a\x00b"]
)
def test_parse_rejects_invalid_fields(field):
    with pytest.raises(TinyMongoNotSupportedError):
        parse_index_spec(field)


@pytest.mark.parametrize(
    "options",
    [
        {"unique": 1},
        {"name": ""},
        {"name": 1},
        {"name": "_id"},
        {"name": "_id_"},
    ],
)
def test_parse_rejects_invalid_supported_options(options):
    with pytest.raises(TinyMongoNotSupportedError):
        parse_index_spec("email", **options)


def test_metadata_is_json_safe_and_round_trips():
    spec = parse_index_spec("profile.email", unique=True, name="profile_login")

    encoded = json.loads(json.dumps(spec.to_metadata()))

    assert encoded == {
        "v": INDEX_METADATA_VERSION,
        "name": "profile_login",
        "key": [["profile.email", 1]],
        "unique": True,
    }
    assert IndexSpec.from_metadata(encoded) == spec


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"v": 2, "name": "email_1", "key": [["email", 1]], "unique": False},
        {
            "v": INDEX_METADATA_VERSION,
            "name": "email_1",
            "key": [["email", 1]],
            "unique": False,
            "extra": True,
        },
    ],
)
def test_invalid_metadata_is_rejected(metadata):
    with pytest.raises(ValueError):
        IndexSpec.from_metadata(metadata)


def test_nested_missing_and_null_values_share_a_token():
    assert index_tokens({}, "profile.email") == ("null:",)
    assert index_tokens({"profile": {}}, "profile.email") == ("null:",)
    assert index_tokens({"profile": {"email": None}}, "profile.email") == ("null:",)
    assert index_tokens({"profile": "not-an-object"}, "profile.email") == ("null:",)


def test_dotted_index_rejects_array_traversal():
    with pytest.raises(TinyMongoNotSupportedError, match="Array traversal"):
        index_tokens({"items": [{"sku": "one"}]}, "items.sku")


def test_scalar_tokens_are_deterministic_and_type_aware():
    documents = [
        {"value": False},
        {"value": 0},
        {"value": 0.0},
        {"value": "0"},
        {"value": "café"},
    ]

    tokens = [index_tokens(document, "value")[0] for document in documents]

    assert tokens[1] == tokens[2]
    assert len(set(tokens)) == len(tokens) - 1
    assert tokens[-1] == 'string:"café"'


def test_arrays_fan_out_and_deduplicate_within_one_document():
    assert index_tokens({"tags": ["a", "b", "a", None]}, "tags") == (
        'string:"a"',
        'string:"b"',
        "null:",
    )
    assert index_tokens({"tags": []}, "tags") == ("undefined:",)


def test_unique_validation_treats_equivalent_numbers_as_duplicates():
    with pytest.raises(DuplicateKeyError):
        validate_unique_documents(
            [{"_id": 1, "value": 1}, {"_id": 2, "value": 1.0}],
            [parse_index_spec("value", unique=True)],
        )

    with pytest.raises(DuplicateKeyError):
        validate_unique_documents(
            [{"_id": 1, "value": 0}, {"_id": 2, "value": -0.0}],
            [parse_index_spec("value", unique=True)],
        )


@pytest.mark.parametrize(
    "value",
    [
        {"nested": "object"},
        [["nested", "array"]],
        ("tuple",),
        b"bytes",
        float("inf"),
        float("nan"),
    ],
)
def test_unsupported_indexed_values_are_rejected(value):
    with pytest.raises(TinyMongoNotSupportedError):
        index_tokens({"value": value}, "value")


def test_unique_validation_rejects_duplicate_scalar_post_images():
    spec = parse_index_spec("email", unique=True)

    with pytest.raises(DuplicateKeyError, match="email_1"):
        validate_unique_documents(
            [
                {"_id": 1, "email": "a@example.com"},
                {"_id": 2, "email": "a@example.com"},
            ],
            [spec],
        )


def test_unique_validation_treats_missing_and_null_as_duplicates():
    spec = parse_index_spec("email", unique=True)

    with pytest.raises(DuplicateKeyError):
        validate_unique_documents([{"_id": 1}, {"_id": 2, "email": None}], [spec])


def test_unique_validation_applies_array_tokens_across_documents():
    spec = parse_index_spec("tags", unique=True)

    with pytest.raises(DuplicateKeyError):
        validate_unique_documents(
            [{"_id": 1, "tags": ["a", "a"]}, {"_id": 2, "tags": ["a"]}],
            [spec],
        )


def test_unique_validation_ignores_repeated_array_token_in_same_document():
    validate_unique_documents(
        [{"_id": 1, "tags": ["a", "a"]}],
        [parse_index_spec("tags", unique=True)],
    )


def test_unique_validation_skips_non_unique_specs_and_keeps_types_distinct():
    validate_unique_documents(
        [
            {"_id": 1, "email": "same", "value": True},
            {"_id": 2, "email": "same", "value": 1},
        ],
        [parse_index_spec("email"), parse_index_spec("value", unique=True)],
    )


def test_unique_validation_checks_each_unique_index():
    documents = [
        {"_id": 1, "email": "one@example.com", "username": "same"},
        {"_id": 2, "email": "two@example.com", "username": "same"},
    ]

    with pytest.raises(DuplicateKeyError, match="username_1"):
        validate_unique_documents(
            documents,
            [
                parse_index_spec("email", unique=True),
                parse_index_spec("username", unique=True),
            ],
        )


def test_unique_validation_rejects_invalid_inputs():
    with pytest.raises(TypeError, match="IndexSpec"):
        validate_unique_documents([], ["email"])
    with pytest.raises(TypeError, match="mappings"):
        validate_unique_documents(
            ["not-a-document"], [parse_index_spec("x", unique=True)]
        )
    with pytest.raises(TypeError, match="mappings"):
        index_tokens("not-a-document", "x")
