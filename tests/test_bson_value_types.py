"""Focused codec and registry regressions for Mike's TM-035 findings."""

from collections import OrderedDict
from datetime import datetime
import re
from uuid import uuid4

import pytest

import tinymongo as tm
from tinymongo import bson_codec, bson_types
from tinymongo import tinymongo as core
from tinymongo.errors import (
    BulkWriteError,
    InvalidDocument,
    OperationFailure,
    TinyMongoNotSupportedError,
    WriteError,
)


bson = pytest.importorskip("bson")
Code = bson.Code
MaxKey = bson.MaxKey
MinKey = bson.MinKey
Timestamp = bson.Timestamp


def test_tm037_logical_timestamp_clock_and_copy_boundaries(monkeypatch):
    clock = iter((1000, 1000, 999, 1001))
    monkeypatch.setattr(core.time_module, "time", lambda: next(clock))
    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_SECONDS", 0)
    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_INCREMENT", 0)

    assert core._next_server_timestamp() == Timestamp(1000, 1)
    assert core._next_server_timestamp() == Timestamp(1000, 2)
    assert core._next_server_timestamp() == Timestamp(1000, 3)
    assert core._next_server_timestamp() == Timestamp(1001, 1)

    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_SECONDS", 2000)
    monkeypatch.setattr(
        core,
        "_SERVER_TIMESTAMP_INCREMENT",
        core._MAX_TIMESTAMP_COMPONENT,
    )
    monkeypatch.setattr(core.time_module, "time", lambda: 2000)
    assert core._next_server_timestamp() == Timestamp(2001, 1)

    zero = Timestamp(0, 0)
    original = {
        "_id": zero,
        "stamp": zero,
        "near-zero": Timestamp(0, 1),
        "nested": {"stamp": zero},
    }
    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_SECONDS", 0)
    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_INCREMENT", 0)
    monkeypatch.setattr(core.time_module, "time", lambda: 1002)
    stamped = core._stamp_top_level_server_timestamps(original)

    assert stamped is not original
    assert original["stamp"] == zero
    assert stamped["_id"] == zero
    assert stamped["stamp"] == Timestamp(1002, 1)
    assert stamped["near-zero"] == Timestamp(0, 1)
    assert stamped["nested"]["stamp"] == zero

    unchanged = {"stamp": Timestamp(1, 0)}
    assert core._stamp_top_level_server_timestamps(unchanged) is unchanged

    monkeypatch.setattr(core, "_TIMESTAMP", None)
    unavailable = {"stamp": zero}
    assert core._stamp_top_level_server_timestamps(unavailable) is unavailable


def test_tm037_ordered_and_unordered_batches_consume_expected_increments(
    monkeypatch,
):
    monkeypatch.setattr(core.time_module, "time", lambda: 1000)
    zero = Timestamp(0, 0)
    client = tm.TinyMongoClient(backend="memory")

    ordered = client.tm037.ordered
    ordered.insert_one({"_id": "duplicate"})
    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_SECONDS", 0)
    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_INCREMENT", 0)
    ordered_documents = [
        {"_id": "accepted", "stamp": zero},
        {"_id": "duplicate", "stamp": zero},
        {"_id": "not-processed", "stamp": zero},
    ]
    with pytest.raises(BulkWriteError) as ordered_error:
        ordered.insert_many(ordered_documents, ordered=True)
    ordered.insert_one({"_id": "after", "stamp": zero})

    ordered_operation = ordered_error.value.details["writeErrors"][0]["op"]
    assert ordered_operation is ordered_documents[1]
    assert ordered_operation["stamp"] == zero
    assert ordered.find_one({"_id": "accepted"})["stamp"] == Timestamp(1000, 1)
    assert ordered.find_one({"_id": "not-processed"}) is None
    assert ordered.find_one({"_id": "after"})["stamp"] == Timestamp(1000, 3)

    unordered = client.tm037.unordered
    unordered.insert_one({"_id": "duplicate"})
    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_SECONDS", 0)
    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_INCREMENT", 0)
    unordered_documents = [
        {"_id": "accepted", "stamp": zero},
        {"_id": "duplicate", "stamp": zero},
        {"_id": "continued", "stamp": zero},
    ]
    with pytest.raises(BulkWriteError) as unordered_error:
        unordered.insert_many(unordered_documents, ordered=False)
    unordered.insert_one({"_id": "after", "stamp": zero})

    unordered_operation = unordered_error.value.details["writeErrors"][0]["op"]
    assert unordered_operation is unordered_documents[1]
    assert unordered_operation["stamp"] == zero
    assert unordered.find_one({"_id": "accepted"})["stamp"] == Timestamp(1000, 1)
    assert unordered.find_one({"_id": "continued"})["stamp"] == Timestamp(1000, 3)
    assert unordered.find_one({"_id": "after"})["stamp"] == Timestamp(1000, 4)
    client.close()


def test_tm037_invalid_insert_does_not_advance_clock_or_replace_error_document(
    monkeypatch,
):
    monkeypatch.setattr(core.time_module, "time", lambda: 1000)
    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_SECONDS", 0)
    monkeypatch.setattr(core, "_SERVER_TIMESTAMP_INCREMENT", 0)
    zero = Timestamp(0, 0)
    client = tm.TinyMongoClient(backend="memory")
    collection = client.tm037.validation
    invalid = {"_id": "invalid", "stamp": zero, "bad": object()}

    with pytest.raises(InvalidDocument) as caught:
        collection.insert_one(invalid)

    assert caught.value.document is invalid
    collection.insert_one({"_id": "valid", "stamp": zero})
    assert collection.find_one({"_id": "valid"})["stamp"] == Timestamp(1000, 1)
    client.close()


@pytest.mark.parametrize(
    ("value", "tag"),
    [
        (MinKey(), {"__tinymongo_type_v1__": "minkey", "value": 1}),
        (MaxKey(), {"__tinymongo_type_v1__": "maxkey", "value": 1}),
        (
            Timestamp(1_700_000_000, 17),
            {
                "__tinymongo_type_v1__": "timestamp",
                "value": {"time": 1_700_000_000, "inc": 17},
            },
        ),
        (
            Code("return value;"),
            {
                "__tinymongo_type_v1__": "code",
                "value": {"code": "return value;", "scope": None},
            },
        ),
    ],
)
def test_tm035_codec_uses_exact_tags_and_round_trips_types(value, tag):
    assert bson_codec.encode_value(value) == tag

    restored = bson_codec.decode_value(tag)

    assert restored == value
    assert type(restored) is type(value)


def test_tm035_codec_round_trips_nested_scoped_code_exactly():
    value = Code(
        "return nested;",
        {
            "timestamp": Timestamp(1000, 3),
            "bounds": [MinKey(), MaxKey()],
            "nested": {"code": Code("return inner;")},
        },
    )

    encoded = bson_codec.encode_value(value)

    assert encoded == {
        "__tinymongo_type_v1__": "code",
        "value": {
            "code": "return nested;",
            "scope": {
                "timestamp": {
                    "__tinymongo_type_v1__": "timestamp",
                    "value": {"time": 1000, "inc": 3},
                },
                "bounds": [
                    {"__tinymongo_type_v1__": "minkey", "value": 1},
                    {"__tinymongo_type_v1__": "maxkey", "value": 1},
                ],
                "nested": {
                    "code": {
                        "__tinymongo_type_v1__": "code",
                        "value": {"code": "return inner;", "scope": None},
                    }
                },
            },
        },
    }

    restored = bson_codec.decode_value(encoded)

    assert type(restored) is Code
    assert restored == value
    assert type(restored.scope["timestamp"]) is Timestamp
    assert type(restored.scope["bounds"][0]) is MinKey
    assert type(restored.scope["bounds"][1]) is MaxKey
    assert type(restored.scope["nested"]["code"]) is Code


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("minkey", None),
        ("minkey", True),
        ("minkey", 0),
        ("minkey", 1.0),
        ("maxkey", None),
        ("maxkey", True),
        ("maxkey", 2),
        ("timestamp", None),
        ("timestamp", {"time": 1}),
        ("timestamp", {"time": 1, "inc": 2, "extra": 3}),
        ("timestamp", {"time": True, "inc": 0}),
        ("timestamp", {"time": 0, "inc": False}),
        ("timestamp", {"time": -1, "inc": 0}),
        ("timestamp", {"time": 2**32, "inc": 0}),
        ("timestamp", {"time": 0, "inc": 2**32}),
        ("code", None),
        ("code", {"code": "return 1;"}),
        ("code", {"code": "return 1;", "scope": None, "extra": 1}),
        ("code", {"code": 1, "scope": None}),
        ("code", {"code": "return 1;", "scope": []}),
    ],
)
def test_tm035_codec_preserves_malformed_tags_as_user_data(kind, payload):
    tagged = {"__tinymongo_type_v1__": kind, "value": payload}

    assert bson_codec.decode_value(tagged) == tagged


@pytest.mark.parametrize(
    ("attribute", "tag", "type_name"),
    [
        (
            "_MinKey",
            {"__tinymongo_type_v1__": "minkey", "value": 1},
            "MinKey",
        ),
        (
            "_MaxKey",
            {"__tinymongo_type_v1__": "maxkey", "value": 1},
            "MaxKey",
        ),
        (
            "_Timestamp",
            {
                "__tinymongo_type_v1__": "timestamp",
                "value": {"time": 1000, "inc": 1},
            },
            "Timestamp",
        ),
        (
            "_Code",
            {
                "__tinymongo_type_v1__": "code",
                "value": {"code": "return 1;", "scope": None},
            },
            "Code",
        ),
    ],
)
def test_tm035_decode_explains_optional_dependency(
    monkeypatch,
    attribute,
    tag,
    type_name,
):
    monkeypatch.setattr(bson_codec, attribute, None)

    assert bson_codec.bson_available() is False
    with pytest.raises(
        ImportError,
        match=rf"{type_name} values.*pip install 'tinymongo\[bson\]'",
    ):
        bson_codec.decode_value(tag)


def test_tm035_registry_identity_equality_and_subclass_precedence():
    timestamp = Timestamp(1000, 2)
    same_timestamp = Timestamp(1000, 2)
    later_timestamp = Timestamp(1000, 3)
    plain_code = Code("same")
    scoped_code = Code("same", {"answer": 42})
    reordered_scope = Code("same", {"b": 2, "a": 1})
    ordered_scope = Code("same", {"a": 1, "b": 2})

    assert bson_types.bson_identity_key(MinKey()) == ("minKey", None)
    assert bson_types.bson_identity_key(MaxKey()) == ("maxKey", None)
    assert bson_types.bson_identity_key(timestamp) == ("timestamp", (1000, 2))
    assert bson_types.bson_identity_key(plain_code) == ("javascript", "same")
    assert bson_types.bson_identity_key(scoped_code)[0] == "javascriptWithScope"
    assert isinstance(hash(bson_types.bson_identity_key(scoped_code)), int)

    assert bson_types.bson_values_equal(timestamp, same_timestamp)
    assert not bson_types.bson_values_equal(timestamp, later_timestamp)
    assert bson_types.bson_values_equal(MinKey(), MinKey())
    assert bson_types.bson_values_equal(MaxKey(), MaxKey())
    assert not bson_types.bson_values_equal(MinKey(), MaxKey())
    assert not bson_types.bson_values_equal(plain_code, "same")
    assert not bson_types.bson_values_equal(plain_code, scoped_code)
    assert not bson_types.bson_values_equal(ordered_scope, reordered_scope)
    assert not bson_codec.storage_values_equal(plain_code, "same")

    assert bson_types.bson_type_spec(plain_code).name == "javascript"
    assert bson_types.bson_type_spec("same").name == "string"


def test_tm035_registry_uses_the_complete_supported_bson_sort_order():
    values = [
        MinKey(),
        None,
        1,
        "text",
        {"value": 1},
        [],
        bson.Binary(b"x"),
        bson.ObjectId("000000000000000000000001"),
        False,
        datetime(2026, 8, 3, 12, 0),
        Timestamp(1000, 1),
        bson.Regex("pattern"),
        Code("return 1;"),
        Code("return scoped;", {"value": 1}),
        MaxKey(),
    ]

    keys = [bson_types.bson_value_sort_key(value) for value in values]

    assert [key[0] for key in keys] == list(range(-1, 14))
    assert keys == sorted(keys)
    assert bson_types.bson_value_sort_key([]) < bson_types.bson_value_sort_key(
        [MinKey()]
    )
    assert [
        type(value)
        for value in sorted(reversed(values), key=bson_types.bson_value_sort_key)
    ] == [type(value) for value in values]


def test_tm035_native_regex_preserves_its_python_representation():
    value = re.compile("native", re.IGNORECASE | re.MULTILINE)

    restored = bson_codec.loads(bson_codec.dumps({"nested": [value]}))["nested"][0]

    assert type(restored) is type(value)
    assert restored.pattern == value.pattern
    assert restored.flags == value.flags


def test_tm035_public_read_preserves_unscoped_code_type():
    client = tm.MongoClient(
        "memory://tm035-unscoped-code-{0}".format(uuid4().hex),
        backend="memory",
        document_class=OrderedDict,
    )
    collection = client.app.items

    try:
        collection.insert_one({"_id": "plain-code", "script": Code("return 1;")})

        document = collection.find_one({"_id": "plain-code"})
        restored = document["script"]

        assert type(document) is OrderedDict
        assert type(restored) is Code
        assert restored == Code("return 1;")
        assert restored.scope is None
    finally:
        client.close()


def test_tm035_code_is_not_accepted_as_a_rename_destination():
    original = {"_id": "rename-code", "source": 1}

    with pytest.raises(WriteError) as caught:
        tm.tinymongo._apply_update_document(
            original,
            {"$rename": {"source": Code("destination")}},
        )

    assert caught.value.code == 2
    assert original == {"_id": "rename-code", "source": 1}


def test_tm035_code_is_not_accepted_as_an_aggregation_count_field():
    client = tm.TinyMongoClient(
        "memory://tm035-count-code-{0}".format(uuid4().hex),
        backend="memory",
    )
    collection = client.app.items
    collection.insert_one({"_id": "count-code"})

    try:
        with pytest.raises(OperationFailure) as caught:
            list(collection.aggregate([{"$count": Code("total")}]))

        assert caught.value.code == 40156
        assert collection.find_one({"_id": "count-code"}) is not None
    finally:
        client.close()


def test_tm035_code_is_not_accepted_as_an_index_name():
    client = tm.TinyMongoClient(
        "memory://tm035-index-code-{0}".format(uuid4().hex),
        backend="memory",
    )
    collection = client.app.items

    try:
        with pytest.raises(
            TinyMongoNotSupportedError,
            match="Index names must be non-empty strings",
        ):
            collection.create_index("value", name=Code("value_index"))

        assert collection.index_information() == {"_id_": {"key": [("_id", 1)]}}
    finally:
        client.close()


@pytest.mark.parametrize(
    "key",
    [Code("value"), [(Code("value"), 1)]],
    ids=("scalar", "pair"),
)
def test_tm035_code_index_fields_are_rejected_before_catalog_writes(key):
    client = tm.TinyMongoClient(
        "memory://tm035-index-field-code-{0}".format(uuid4().hex),
        backend="memory",
    )
    collection = client.app.items
    original = {"_id": "index-field-code", "value": 1}
    collection.insert_one(original)

    try:
        with pytest.raises(
            TinyMongoNotSupportedError,
            match="Index (fields|keys) must",
        ):
            collection.create_index(key)

        assert collection.index_information() == {"_id_": {"key": [("_id", 1)]}}
        assert collection.find_one({"_id": "index-field-code"}) == original
    finally:
        client.close()


def test_tm035_code_is_not_treated_as_a_distinct_or_drop_index_name():
    client = tm.TinyMongoClient(
        "memory://tm035-command-code-{0}".format(uuid4().hex),
        backend="memory",
    )
    collection = client.app.items
    collection.insert_one({"_id": "command-code", "value": 1})
    collection.create_index("value", name="value_index")

    try:
        with pytest.raises(OperationFailure) as distinct_error:
            collection.distinct(Code("value"))
        with pytest.raises(OperationFailure) as drop_error:
            collection.drop_index(Code("value_index"))

        assert distinct_error.value.code == 14
        assert drop_error.value.code == 14
        assert "value_index" in collection.index_information()
    finally:
        client.close()


def test_tm035_code_is_not_treated_as_a_cursor_sort_field():
    client = tm.TinyMongoClient(
        "memory://tm035-sort-field-code-{0}".format(uuid4().hex),
        backend="memory",
    )
    collection = client.app.items
    collection.insert_many(
        [
            {"_id": "first", "value": 2},
            {"_id": "second", "value": 1},
        ]
    )

    try:
        with pytest.raises(TypeError):
            collection.find({}).sort(Code("value"), 1)
        with pytest.raises(TypeError):
            collection.find({}).sort([(Code("value"), 1)])
        with pytest.raises(TypeError):
            collection.find({}, sort=[(Code("value"), 1)])

        assert {row["_id"] for row in collection.find({})} == {"first", "second"}
    finally:
        client.close()


def test_tm035_invalid_scoped_code_value_keeps_rich_write_context():
    client = tm.TinyMongoClient(
        "memory://tm035-invalid-{0}".format(uuid4().hex),
        backend="memory",
    )
    collection = client.app.items
    document = {
        "_id": "broken-code",
        "outer": {
            "script": Code(
                "return nested;",
                {"nested": [{"unsupported": {1, 2}}]},
            )
        },
    }

    try:
        with pytest.raises(InvalidDocument) as caught:
            collection.insert_one(document)

        message = str(caught.value)
        assert caught.value.document is document
        assert "collection 'app.items'" in message
        assert "document _id='broken-code'" in message
        assert "$['outer']['script']['scope']['nested'][0]['unsupported']" in message
        assert "<class 'set'>" in message
        assert collection.find_one({"_id": "broken-code"}) is None
    finally:
        client.close()
