"""Focused invariants for the shared BSON type registry and JSON codec."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess
import sys
import textwrap
from uuid import UUID, uuid4

import pytest

import tinymongo
from tinymongo import bson_codec, bson_types
from tinymongo.errors import BulkWriteError
from tinymongo.sorting import sort_documents


def test_core_datetime_and_binary_values_do_not_require_pymongo():
    moment = datetime(2026, 7, 29, 12, 30)
    document = {
        "created": moment,
        "raw": b"\x00\x01\xff",
        "buffer": bytearray(b"mutable"),
    }

    restored = bson_codec.loads(bson_codec.dumps(document))

    assert restored == {
        "created": moment,
        "raw": b"\x00\x01\xff",
        "buffer": b"mutable",
    }


def test_core_memory_crud_sort_and_batch_work_without_pymongo():
    client = tinymongo.TinyMongoClient(
        "memory://core-bson-registry-{0}".format(uuid4().hex),
        backend="memory",
    )
    collection = client.app.events
    earlier = datetime(2026, 7, 29, 11, 30)
    later = datetime(2026, 7, 29, 12, 30)

    result = collection.insert_many(
        (
            {"_id": "later", "created": later, "payload": b"b"},
            {"_id": "earlier", "created": earlier, "payload": bytearray(b"a")},
        )
    )

    assert result.inserted_ids == ["later", "earlier"]
    assert collection.find_one({"payload": b"a"})["_id"] == "earlier"
    assert [document["_id"] for document in collection.find({}).sort("created", 1)] == [
        "earlier",
        "later",
    ]
    with pytest.raises(BulkWriteError) as caught:
        collection.insert_many(
            [{"_id": "later"}, {"_id": "new"}],
            ordered=False,
        )
    assert caught.value.details["nInserted"] == 1
    assert collection.find_one({"_id": "new"}) is not None
    client.close()


def test_registry_is_the_encoding_metadata_source():
    bson = pytest.importorskip("bson")
    uuid_binary = bson.Binary(bytes(range(16)), subtype=4)
    values = [
        (datetime(2026, 7, 29, 12, 30), "datetime"),
        (bson.ObjectId("000000000000000000000001"), "objectid"),
        (bson.Decimal128("19.950"), "decimal128"),
        (b"native", "binary"),
        (bytearray(b"mutable"), "binary"),
        (uuid_binary, "binary"),
        (UUID("00112233-4455-6677-8899-aabbccddeeff"), "uuid"),
        (re.compile("native"), "regex"),
        (bson.Regex("bson", "im"), "regex"),
    ]

    for value, expected_tag in values:
        spec = bson_types.bson_type_spec(value)
        encoded = bson_codec.encode_value(value)

        assert spec is not None
        assert spec.storage_tag == expected_tag
        assert spec.requires_python_comparison is True
        assert encoded["__tinymongo_type_v1__"] == spec.storage_tag
        assert bson_codec.contains_extended_value(value) is True


def test_binary_and_numeric_identity_keys_follow_bson_equality():
    bson = pytest.importorskip("bson")
    generic_binary = bson.Binary(b"same", subtype=0)
    uuid_binary = bson.Binary(bytes(range(16)), subtype=4)

    assert bson_types.bson_identity_key(generic_binary) == bson_types.bson_identity_key(
        b"same"
    )
    assert bson_types.bson_identity_key(bytearray(b"same")) == (
        "binary",
        (0, b"same"),
    )
    assert bson_types.bson_values_equal(generic_binary, b"same") is True
    assert bson_types.bson_values_equal(uuid_binary, bytes(uuid_binary)) is False
    assert bson_types.bson_identity_key(1) == bson_types.bson_identity_key(1.0)
    assert bson_types.bson_identity_key(1) == bson_types.bson_identity_key(
        bson.Decimal128("1.00")
    )
    assert bson_types.bson_identity_key(0.1) != bson_types.bson_identity_key(
        bson.Decimal128("0.1")
    )
    assert bson_types.bson_values_equal(1, 1.0) is True
    assert bson_types.bson_values_equal(True, 1) is False
    assert bson_types.bson_identity_key(float("nan")) == bson_types.bson_identity_key(
        float("nan")
    )


def test_uuid_and_regex_identity_keys_match_their_bson_wire_values():
    bson = pytest.importorskip("bson")
    value = UUID("00112233-4455-6677-8899-aabbccddeeff")

    assert bson_types.bson_identity_key(value) == bson_types.bson_identity_key(
        bson.Binary(value.bytes, subtype=4)
    )
    assert bson_types.bson_values_equal(
        re.compile("same", re.IGNORECASE),
        bson.Regex("same", "iu"),
    )
    assert not bson_types.bson_values_equal(
        re.compile("same", re.IGNORECASE),
        bson.Regex("same", "i"),
    )
    assert bson_types.bson_identity_key(bson.Regex("same", "iz")) == (
        "regex",
        ("same", "i"),
    )
    assert bson_types.bson_identity_key(bson.Regex(b"same", "im")) == (
        "regex",
        ("same", "im"),
    )
    assert bson_types.regex_components(bson.Regex("flags", "ilmsux")) == (
        "flags",
        "ilmsux",
    )

    with pytest.raises(TypeError, match="Unsupported BSON regular-expression"):
        bson_types.regex_components("not-regex")
    with pytest.raises(TypeError, match="Unsupported BSON regular-expression"):
        bson_types.regex_compile_components("not-regex")


def test_uuid_and_regex_follow_mongodb_scalar_sort_order():
    bson = pytest.importorskip("bson")
    first = UUID("00112233-4455-6677-8899-aabbccddeefe")
    second = UUID("00112233-4455-6677-8899-aabbccddeeff")
    values = [
        bson.Regex("same", "u"),
        bson.Regex("same", "iu"),
        bson.Regex("same", "im"),
        bson.Regex("same", "i"),
        bson.Regex("same", ""),
    ]

    assert bson_types.bson_scalar_sort_key(first) < bson_types.bson_scalar_sort_key(
        second
    )
    assert [
        value.flags for value in sorted(values, key=bson_types.bson_scalar_sort_key)
    ] == [bson.Regex("same", flags).flags for flags in ("", "i", "im", "iu", "u")]


def test_numeric_sort_places_nan_below_all_other_numbers():
    bson = pytest.importorskip("bson")
    values = [
        ("one", 1.0),
        ("nan", float("nan")),
        ("decimal-nan", bson.Decimal128("NaN")),
        ("negative", -1.0),
        ("negative-infinity", float("-inf")),
        ("zero", 0.0),
        ("infinity", float("inf")),
    ]

    assert [
        label
        for label, _value in sorted(
            values,
            key=lambda item: bson_types.bson_scalar_sort_key(item[1]),
        )
    ] == [
        "nan",
        "decimal-nan",
        "negative-infinity",
        "negative",
        "zero",
        "one",
        "infinity",
    ]


def test_equal_bson_sort_keys_preserve_input_order_in_both_directions():
    bson = pytest.importorskip("bson")
    numeric = [
        {"_id": "integer", "value": 1},
        {"_id": "double", "value": 1.0},
        {"_id": "decimal", "value": bson.Decimal128("1.00")},
    ]
    dates = [
        {
            "_id": "first",
            "value": datetime(2026, 8, 2, 12, 0, 0, 123100),
        },
        {
            "_id": "second",
            "value": datetime(2026, 8, 2, 12, 0, 0, 123900),
        },
    ]

    for documents in (numeric, dates):
        expected = [document["_id"] for document in documents]
        for direction in (1, -1):
            assert [
                document["_id"]
                for document in sort_documents(
                    documents,
                    (("value", direction),),
                )
            ] == expected


def test_recursive_bson_equality_preserves_nested_scalar_types():
    assert bson_types.bson_values_equal(
        {"items": [1, {"active": True}]},
        {"items": [1.0, {"active": True}]},
    )
    assert not bson_types.bson_values_equal(
        {"items": [1, {"active": True}]},
        {"items": [1.0, {"active": 1}]},
    )
    assert not bson_types.bson_values_equal({"value": True}, {"value": 1})
    assert not bson_types.bson_values_equal({"value": 1}, 1)
    assert bson_types.bson_values_equal({1}, {1})
    assert not bson_types.bson_values_equal({1}, {2})
    assert not bson_types.bson_values_equal(
        {"first": 1, "second": 2},
        {"second": 2, "first": 1},
    )
    assert not bson_types.bson_values_equal({"value": 1}, ["value", 1])

    unsupported = object()
    assert bson_types.bson_values_equal(unsupported, unsupported)
    assert not bson_types.bson_values_equal(unsupported, object())


def test_datetime_identity_normalizes_to_signed_utc_milliseconds():
    first = datetime(
        2026,
        7,
        29,
        8,
        30,
        0,
        123001,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    same_millisecond = datetime(
        2026,
        7,
        29,
        12,
        30,
        0,
        123999,
        tzinfo=timezone.utc,
    )
    next_millisecond = same_millisecond.replace(microsecond=124000)
    before_epoch = datetime(1969, 12, 31, 23, 59, 59, 999999)

    assert bson_types.bson_identity_key(first) == bson_types.bson_identity_key(
        same_millisecond
    )
    assert bson_types.bson_values_equal(first, same_millisecond) is True
    assert bson_types.bson_values_equal(first, next_millisecond) is False
    assert bson_types.bson_identity_key(before_epoch) == ("datetime", -1)


@pytest.mark.parametrize(
    "tagged",
    [
        {"__tinymongo_type_v1__": "datetime", "value": "not-a-date"},
        {"__tinymongo_type_v1__": "datetime", "value": 123},
        {"__tinymongo_type_v1__": "objectid", "value": "too-short"},
        {"__tinymongo_type_v1__": "objectid", "value": "z" * 24},
        {"__tinymongo_type_v1__": "objectid", "value": "00 " * 8},
        {"__tinymongo_type_v1__": "objectid", "value": 123},
        {
            "__tinymongo_type_v1__": "future-type",
            "value": {
                "__tinymongo_type_v1__": "datetime",
                "value": "2026-07-29T12:30:00",
            },
        },
    ],
)
def test_malformed_and_unknown_tags_are_preserved_exactly(tagged):
    assert bson_codec.decode_value(tagged) == tagged


def test_valid_datetime_and_object_id_tags_still_decode():
    bson = pytest.importorskip("bson")
    moment = datetime(
        2026,
        7,
        29,
        8,
        30,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    object_id = bson.ObjectId("000000000000000000000001")

    assert bson_codec.decode_value(
        {"__tinymongo_type_v1__": "datetime", "value": moment.isoformat()}
    ) == datetime(2026, 7, 29, 12, 30)
    assert (
        bson_codec.decode_value(
            {"__tinymongo_type_v1__": "objectid", "value": str(object_id)}
        )
        == object_id
    )


def test_fresh_process_without_pymongo_keeps_core_codec_available():
    # Import bson first, then block pymongo. This models an environment where a
    # top-level bson distribution exists but PyMongo's complete implementation
    # is unavailable; the registry must not report partial BSON capability.
    script = textwrap.dedent(
        """
        import builtins
        from datetime import datetime

        real_import = builtins.__import__

        try:
            import bson
        except ImportError:
            bson = None

        def without_pymongo(name, *args, **kwargs):
            if name == "pymongo" or name.startswith("pymongo."):
                raise ModuleNotFoundError(
                    "No module named 'pymongo'", name="pymongo"
                )
            return real_import(name, *args, **kwargs)

        builtins.__import__ = without_pymongo

        import tinymongo
        from tinymongo import bson_codec, bson_types
        from tinymongo.errors import InvalidDocument, TinyMongoError

        assert bson_types.bson_capabilities() == {
            "objectid": False,
            "binary": False,
            "decimal128": False,
            "regex": False,
        }
        assert bson_codec.bson_available() is False
        assert bson_codec.object_id_available() is False
        assert bson_codec.binary_available() is False
        assert bson_codec.decimal128_available() is False
        assert bson_codec.regex_available() is False
        assert bson_codec.loads(bson_codec.dumps(b"core")) == b"core"
        assert issubclass(InvalidDocument, TinyMongoError)

        explicit_id = tinymongo.generate_id()
        assert type(explicit_id) is str
        assert len(explicit_id) == 32

        client = tinymongo.TinyMongoClient(backend="memory")
        result = client.core.items.insert_one({"kind": "implicit-id"})
        assert type(result.inserted_id) is str
        assert len(result.inserted_id) == 32
        assert client.core.items.find_one({"_id": result.inserted_id}) is not None

        totals = client.core.totals
        totals.insert_many([{"value": 0.1}, {"value": 0.2}])
        assert totals.aggregate(
            [{"$group": {"_id": None, "total": {"$sum": "$value"}}}]
        ).to_list() == [{"_id": None, "total": 0.30000000000000004}]
        client.close()

        invalid_document = {
            "_id": "invalid",
            "nested": [{"unsupported": {1, 2}}],
        }
        try:
            bson_codec.dumps(
                invalid_document,
                document_context="collection 'core.items'",
            )
        except TinyMongoError as error:
            assert isinstance(error, InvalidDocument)
            assert not isinstance(error, TypeError)
            assert error.document is invalid_document
            assert "collection 'core.items'" in str(error)
            assert "$['nested'][0]['unsupported']" in str(error)
            assert "<class 'set'>" in str(error)
        else:
            raise AssertionError("unsupported set encoded without an error")

        moment = datetime(2026, 7, 29, 12, 30)
        assert bson_codec.loads(bson_codec.dumps(moment)) == moment

        from uuid import UUID
        import re

        native_uuid = UUID("00112233-4455-6677-8899-aabbccddeeff")
        assert bson_codec.loads(bson_codec.dumps(native_uuid)) == native_uuid
        native_regex = re.compile("native", re.IGNORECASE)
        restored_regex = bson_codec.loads(bson_codec.dumps(native_regex))
        assert restored_regex.pattern == native_regex.pattern
        assert restored_regex.flags == native_regex.flags

        client = tinymongo.TinyMongoClient(backend="memory")
        values = client.core.native_values
        values.insert_many(
            [
                {"_id": native_uuid, "value": native_regex},
                {"_id": "text", "value": "NATIVE"},
            ]
        )
        assert values.find_one({"_id": native_uuid})["value"].pattern == "native"
        assert {
            item["_id"] for item in values.find({"value": native_regex})
        } == {native_uuid, "text"}
        client.close()

        generic = {
            "__tinymongo_type_v1__": "binary",
            "value": {"base64": "Y29yZQ==", "subtype": 0},
        }
        assert bson_codec.decode_value(generic) == b"core"

        non_generic = {
            "__tinymongo_type_v1__": "binary",
            "value": {
                "base64": "AAECAwQFBgcICQoLDA0ODw==",
                "subtype": 4,
            },
        }
        try:
            bson_codec.decode_value(non_generic)
        except ImportError as error:
            assert "tinymongo[bson]" in str(error)
        else:
            raise AssertionError("non-zero Binary subtype decoded without PyMongo")

        object_id = {
            "__tinymongo_type_v1__": "objectid",
            "value": "000000000000000000000001",
        }
        try:
            bson_codec.decode_value(object_id)
        except ImportError as error:
            assert "tinymongo[bson]" in str(error)
        else:
            raise AssertionError("ObjectId decoded without PyMongo")
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[1]),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_sync_and_async_sqlite_uuid_regex_work_without_pymongo(tmp_path):
    script = textwrap.dedent(
        """
        import asyncio
        import builtins
        import re
        import sys
        from uuid import UUID

        real_import = builtins.__import__

        def without_pymongo(name, *args, **kwargs):
            if name == "pymongo" or name.startswith("pymongo."):
                raise ModuleNotFoundError("No module named 'pymongo'", name="pymongo")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = without_pymongo

        import tinymongo
        from tinymongo.asyncio import AsyncTinyMongoClient

        value = UUID("00112233-4455-6677-8899-aabbccddeeff")
        expression = re.compile("native", re.IGNORECASE)

        client = tinymongo.TinyMongoClient(sys.argv[1], backend="sqlite")
        collection = client.app.values
        collection.insert_many(
            [
                {"_id": value, "value": expression},
                {"_id": "text", "value": "NATIVE"},
            ]
        )
        client.close()

        client = tinymongo.TinyMongoClient(sys.argv[1], backend="sqlite")
        collection = client.app.values
        restored = collection.find_one({"_id": value})
        assert restored["value"].pattern == expression.pattern
        assert restored["value"].flags == expression.flags
        assert {item["_id"] for item in collection.find({"value": expression})} == {
            value,
            "text",
        }
        client.close()

        async def async_scenario():
            client = AsyncTinyMongoClient(sys.argv[2], backend="sqlite")
            collection = client.app.values
            await collection.insert_many(
                [
                    {"_id": value, "value": expression},
                    {"_id": "text", "value": "NATIVE"},
                ]
            )
            await client.close()

            client = AsyncTinyMongoClient(sys.argv[2], backend="sqlite")
            collection = client.app.values
            restored = await collection.find_one({"_id": value})
            assert restored["value"].pattern == expression.pattern
            assert restored["value"].flags == expression.flags
            rows = await collection.find({"value": expression}).to_list()
            assert {item["_id"] for item in rows} == {value, "text"}
            await client.close()

        asyncio.run(async_scenario())
        """
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "sync"),
            str(tmp_path / "async"),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
