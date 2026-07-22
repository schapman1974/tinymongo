import json

import pytest

from tinymongo.errors import TinyMongoNotSupportedError
from tinymongo.indexes import (
    IndexBatchPlan,
    IndexSpec,
    TinyMongoUnsupportedWarning,
    emit_index_plan_warnings,
    plan_index_model,
    plan_index_models,
)


class DuckIndexModel:
    """Minimal stand-in proving the helper does not import PyMongo."""

    def __init__(self, document):
        self.document = document


def test_supported_duck_typed_index_model_produces_an_effective_spec():
    plan = plan_index_model(
        DuckIndexModel(
            {
                "name": "login_email",
                "unique": True,
                "key": {"profile.email": 1},
            }
        )
    )

    assert plan.name == "login_email"
    assert plan.spec == IndexSpec(
        field="profile.email", direction=1, unique=True, name="login_email"
    )
    assert plan.requested_keys == (("profile.email", 1),)
    assert dict(plan.requested_options) == {
        "name": "login_email",
        "unique": True,
    }
    assert plan.degraded_features == ()
    assert plan.warning is None
    assert plan.outcome == "create"


def test_plain_mapping_and_pair_sequence_are_supported_and_name_is_generated():
    plan = plan_index_model({"key": [("created", -1)]})

    assert plan.name == "created_-1"
    assert plan.spec == IndexSpec(field="created", name="created_-1")
    assert plan.degraded_features == ("descending",)


def test_existing_index_spec_passes_through_unchanged():
    spec = IndexSpec("email", unique=True, name="login")

    plan = plan_index_model(spec)

    assert plan.spec is spec
    assert plan.name == "login"
    assert plan.requested_keys == (("email", 1),)
    assert plan.outcome == "create"


@pytest.mark.parametrize(
    ("document", "feature", "message"),
    [
        ({"key": {"created": -1}}, "descending", "treated as ascending"),
        ({"key": {"token": "hashed"}}, "hashed", "ascending equality"),
        ({"key": {"email": 1}, "sparse": True}, "sparse", "not honored"),
        (
            {"key": {"created": 1}, "expireAfterSeconds": 0},
            "ttl",
            "TTL expiration",
        ),
        (
            {"key": {"email": 1}, "background": True},
            "background",
            "background creation",
        ),
    ],
)
def test_single_field_performance_declarations_create_a_degraded_index(
    document, feature, message
):
    plan = plan_index_model(document)

    assert plan.spec == IndexSpec(document["key"].copy().popitem()[0], name=plan.name)
    assert plan.degraded_features == (feature,)
    assert plan.outcome == "create_degraded"
    assert message in plan.warning


def test_false_performance_flags_require_no_degradation():
    plan = plan_index_model({"key": {"email": 1}, "sparse": False, "background": False})

    assert plan.outcome == "create"
    assert plan.degraded_features == ()
    assert plan.warning is None


def test_nonunique_text_index_is_accepted_as_a_warned_noop():
    plan = plan_index_model({"key": {"content": "text"}, "name": "content_text"})

    assert plan.name == "content_text"
    assert plan.spec is None
    assert plan.outcome == "skip"
    assert plan.degraded_features == ("text",)
    assert "$text queries are not supported" in plan.warning
    assert plan.to_metadata()["effective_spec"] is None


def test_nonunique_compound_model_degrades_to_an_ascending_leading_field():
    plan = plan_index_model(
        DuckIndexModel(
            {
                "name": "account_recent",
                "key": {"account_id": 1, "created": -1},
                "sparse": True,
                "expireAfterSeconds": 60,
                "background": True,
            }
        )
    )

    assert plan.name == "account_recent"
    assert plan.spec == IndexSpec(field="account_id", name="account_recent")
    assert plan.outcome == "create_degraded"
    assert plan.degraded_features == (
        "compound",
        "descending",
        "sparse",
        "ttl",
        "background",
    )
    assert "created with reduced behavior" in plan.warning
    assert "ascending leading field" in plan.warning
    assert "descending direction" in plan.warning
    assert "sparse membership" in plan.warning
    assert "TTL expiration" in plan.warning
    assert "background creation" in plan.warning

    metadata = plan.to_metadata()
    assert metadata["requested_key"] == [
        ["account_id", 1],
        ["created", -1],
    ]
    assert metadata["outcome"] == "create_degraded"
    assert metadata["effective_spec"] == {
        "v": 1,
        "name": "account_recent",
        "key": [["account_id", 1]],
        "unique": False,
    }
    assert json.loads(json.dumps(metadata)) == metadata


def test_compound_default_name_matches_pymongo_shape():
    plan = plan_index_model({"key": [("account", 1), ("created", -1)]})

    assert plan.name == "account_1_created_-1"


def test_degraded_effective_spec_is_included_in_metadata():
    plan = plan_index_model(
        {"key": {"email": 1}, "name": "maybe_email", "sparse": True}
    )

    assert plan.to_metadata()["effective_spec"] == {
        "v": 1,
        "name": "maybe_email",
        "key": [["email", 1]],
        "unique": False,
    }


@pytest.mark.parametrize(
    ("document", "feature"),
    [
        ({"key": {"first": 1, "last": 1}, "unique": True}, "compound"),
        ({"key": {"email": "hashed"}, "unique": True}, "hashed"),
        ({"key": {"content": "text"}, "unique": True}, "text"),
        ({"key": {"email": 1}, "unique": True, "sparse": True}, "sparse"),
        (
            {"key": {"created": 1}, "unique": True, "expireAfterSeconds": 60},
            "ttl",
        ),
    ],
)
def test_unique_semantic_combinations_are_rejected(document, feature):
    with pytest.raises(
        TinyMongoNotSupportedError,
        match="cannot be degraded.*{0}".format(feature),
    ):
        plan_index_model(document)


def test_unique_descending_and_background_flags_preserve_uniqueness():
    plan = plan_index_model(
        {
            "key": {"email": -1},
            "name": "unique_email_desc",
            "unique": True,
            "background": True,
        }
    )

    assert plan.spec == IndexSpec("email", unique=True, name="unique_email_desc")
    assert plan.degraded_features == ("descending", "background")


@pytest.mark.parametrize(
    ("model", "message"),
    [
        (object(), "expose a mapping-valued document"),
        (DuckIndexModel([]), "document must be a mapping"),
    ],
)
def test_invalid_index_model_containers_are_rejected(model, message):
    with pytest.raises(TypeError, match=message):
        plan_index_model(model)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({}, "contain a key"),
        ({"key": 42}, "mapping or sequence"),
        ({"key": {}}, "at least one field"),
        ({"key": [("email",)]}, "pairs"),
        ({"key": [(42, 1)]}, "non-empty strings"),
        ({"key": {"email": True}}, "directions"),
        ({"key": {"email": "2dsphere"}}, "directions"),
    ],
)
def test_invalid_index_model_keys_are_rejected(document, message):
    with pytest.raises(TinyMongoNotSupportedError, match=message):
        plan_index_model(document)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"key": {"email": 1}, "collation": {"locale": "en"}}, "option"),
        ({"key": {"email": 1}, "unique": 1}, "unique"),
        ({"key": {"email": 1}, "sparse": 1}, "sparse"),
        ({"key": {"email": 1}, "background": 1}, "background"),
        ({"key": {"email": 1}, "expireAfterSeconds": True}, "non-negative"),
        ({"key": {"email": 1}, "expireAfterSeconds": "60"}, "non-negative"),
        ({"key": {"email": 1}, "expireAfterSeconds": -1}, "non-negative"),
        (
            {"key": {"email": 1}, "expireAfterSeconds": float("inf")},
            "non-negative",
        ),
        ({"key": {"email": 1}, "name": ""}, "non-empty strings"),
        ({"key": {"email": 1}, "name": "_id_"}, "reserved"),
    ],
)
def test_invalid_index_model_options_are_rejected(document, message):
    with pytest.raises(TinyMongoNotSupportedError, match=message):
        plan_index_model(document)


def test_valid_fractional_ttl_is_acknowledged_with_a_warning():
    plan = plan_index_model({"key": {"created": 1}, "expireAfterSeconds": 0.5})

    assert plan.degraded_features == ("ttl",)


def test_batch_plan_preserves_order_and_separates_effective_specs():
    supported = DuckIndexModel({"key": {"email": 1}, "unique": True})
    skipped = DuckIndexModel({"key": {"tenant": 1, "created": -1}})
    degraded = DuckIndexModel({"key": {"token": "hashed"}})

    batch = plan_index_models(model for model in (supported, skipped, degraded))

    assert isinstance(batch, IndexBatchPlan)
    assert tuple(batch) == batch.entries
    assert batch.names == ("email_1", "tenant_1_created_-1", "token_hashed")
    assert tuple(spec.name for spec in batch.specs) == (
        "email_1",
        "tenant_1_created_-1",
        "token_hashed",
    )
    assert len(batch.warnings) == 2
    assert "tenant_1_created_-1" in batch.warnings[0]
    assert "token_hashed" in batch.warnings[1]


def test_batch_planner_accepts_an_empty_batch():
    batch = plan_index_models([])

    assert batch.entries == ()
    assert batch.names == ()
    assert batch.specs == ()
    assert batch.warnings == ()


def test_batch_planner_rejects_a_noniterable():
    with pytest.raises(TypeError, match="must be an iterable"):
        plan_index_models(None)


def test_batch_planner_preserves_model_type_errors():
    with pytest.raises(TypeError, match="expose a mapping-valued document"):
        plan_index_models([object()])


def test_warning_emitter_uses_the_dedicated_warning_type():
    batch = plan_index_models(
        [
            {"key": {"email": 1}},
            {"key": {"created": -1}},
            {"key": {"token": "hashed"}},
        ]
    )

    with pytest.warns(TinyMongoUnsupportedWarning) as captured:
        emit_index_plan_warnings(batch)

    assert len(captured) == 2
    assert "created_-1" in str(captured[0].message)
    assert "token_hashed" in str(captured[1].message)


def test_warning_emitter_requires_a_batch_plan():
    with pytest.raises(TypeError, match="IndexBatchPlan"):
        emit_index_plan_warnings(())
