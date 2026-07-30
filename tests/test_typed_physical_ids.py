"""Focused contracts for BSON-aware SQL and Parquet physical `_id` keys."""

from datetime import datetime, timedelta, timezone

import pytest

import tinymongo.table_backends as table_backends
from tinymongo.errors import DuplicateKeyError
from tinymongo.table_backends import (
    DuckDBTableBackend,
    ParquetDuckDBBackend,
    SQLCompiler,
    SQLiteTableBackend,
    _canonical_id_value,
    _json_dumps,
    _matching_physical_row_id,
    _physical_id_candidates,
    _physical_id_key,
    _quote_identifier,
)


bson = pytest.importorskip("bson")
Binary = bson.Binary
ObjectId = bson.ObjectId


@pytest.fixture(params=("sqlite", "duckdb", "parquet"))
def typed_backend(request, tmp_path):
    if request.param == "sqlite":
        backend = SQLiteTableBackend(str(tmp_path / "typed.sqlite"))
    elif request.param == "duckdb":
        pytest.importorskip("duckdb")
        backend = DuckDBTableBackend(str(tmp_path / "typed.duckdb"))
    else:
        pytest.importorskip("duckdb")
        pytest.importorskip("pyarrow")
        backend = ParquetDuckDBBackend(str(tmp_path / "parquet"))
    try:
        yield backend
    finally:
        backend.close()


def _stored_keys(backend, collection):
    if isinstance(backend, ParquetDuckDBBackend):
        return [row[0] for row in backend._read_all_rows(collection)]
    conn = backend._connect()
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT _id FROM {0}".format(_quote_identifier(collection))
            ).fetchall()
        ]
    finally:
        conn.close()


def _write_legacy_row(backend, collection, document):
    data = _json_dumps(document)
    legacy_id = str(document["_id"])
    if isinstance(backend, ParquetDuckDBBackend):
        backend._write_rows(collection, [(legacy_id, data)])
        return

    backend.create_collection(collection)
    conn = backend._connect()
    try:
        conn.execute(
            "INSERT INTO {0} (_id, data) VALUES (?, ?)".format(
                _quote_identifier(collection)
            ),
            (legacy_id, data),
        )
        try:
            conn.commit()
        except AttributeError:
            pass
    finally:
        conn.close()


def test_physical_id_keys_follow_registered_bson_identity():
    raw = bytes(range(16))
    instant = datetime(2026, 7, 29, 12, 0, 0, 999)
    same_instant = instant.replace(tzinfo=timezone.utc).astimezone(
        timezone(timedelta(hours=-4))
    )

    assert _physical_id_key(raw) == _physical_id_key(bytearray(raw))
    assert _physical_id_key(raw) == _physical_id_key(Binary(raw, subtype=0))
    assert _physical_id_key(raw) != _physical_id_key(Binary(raw, subtype=4))
    assert _physical_id_key(1) == _physical_id_key(1.0)
    assert _physical_id_key(1) != _physical_id_key(True)
    assert _physical_id_key(instant) == _physical_id_key(same_instant)
    assert _physical_id_key(ObjectId("000000000000000000000001")) != (
        _physical_id_key("000000000000000000000001")
    )
    assert _physical_id_key(float("inf")) != _physical_id_key(float("-inf"))
    assert _physical_id_key(float("nan")) == _physical_id_key(float("nan"))
    assert _physical_id_key([1, True]) == _physical_id_key((1, True))


def test_physical_id_fallback_and_malformed_legacy_rows(monkeypatch):
    monkeypatch.setattr(table_backends, "bson_identity_key", lambda value: None)

    assert _canonical_id_value(1) == ["encoded-scalar", 1]
    assert (
        _matching_physical_row_id(
            [
                ("invalid-json", "{"),
                ("missing-id", '{"value": 1}'),
            ],
            1,
        )
        is None
    )


def test_sql_compiler_supports_explicit_id_equality():
    where, params = SQLCompiler("sqlite").compile({"_id": {"$eq": 1}})

    assert "_id = ?" in where
    assert params == list(_physical_id_candidates(1))


def test_distinct_bson_id_types_coexist_and_remain_addressable(typed_backend):
    binary_zero = bytes(range(16))
    binary_four = Binary(bytes(range(16)), subtype=4)
    documents = [
        {"_id": binary_zero, "label": "generic"},
        {"_id": binary_four, "label": "custom"},
        {"_id": 1, "label": "number"},
        {"_id": True, "label": "boolean"},
    ]

    typed_backend.insert_many("items", documents)

    assert len(set(_stored_keys(typed_backend, "items"))) == 4
    assert all(
        key.startswith("__tinymongo_id_v2__:")
        for key in _stored_keys(typed_backend, "items")
    )
    assert typed_backend.find_one("items", {"_id": binary_zero})["label"] == "generic"
    assert (
        typed_backend.find_one("items", {"_id": Binary(binary_zero, 0)})["label"]
        == "generic"
    )
    assert typed_backend.find_one("items", {"_id": binary_four})["label"] == "custom"
    assert typed_backend.find_one("items", {"_id": 1})["label"] == "number"
    assert typed_backend.find_one("items", {"_id": True})["label"] == "boolean"

    typed_backend.replace_one(
        "items",
        binary_four,
        {"_id": binary_four, "label": "updated"},
    )
    typed_backend.delete_ids("items", [binary_zero])

    assert typed_backend.find_one("items", {"_id": binary_zero}) is None
    assert typed_backend.find_one("items", {"_id": binary_four})["label"] == "updated"
    assert (
        typed_backend.replace_one(
            "items",
            "missing",
            {"_id": "missing", "label": "missing"},
        )
        is None
    )


@pytest.mark.parametrize(
    ("first", "equivalent"),
    [
        (b"same", Binary(b"same", subtype=0)),
        (1, 1.0),
    ],
)
def test_bson_equivalent_ids_share_one_native_key(
    typed_backend,
    first,
    equivalent,
):
    typed_backend.insert_many("duplicates", [{"_id": first}])

    with pytest.raises(DuplicateKeyError):
        typed_backend.insert_many("duplicates", [{"_id": equivalent}])


def test_legacy_stringified_rows_remain_readable_mutable_and_type_safe(
    typed_backend,
):
    _write_legacy_row(
        typed_backend,
        "legacy",
        {"_id": 1, "label": "before"},
    )

    # The fallback legacy key "1" must not make a BSON string ID find an
    # existing numeric document that happens to have the same old storage key.
    assert typed_backend.find_one("legacy", {"_id": "1"}) is None
    assert typed_backend.find_one("legacy", {"_id": 1})["label"] == "before"

    typed_backend.replace_one(
        "legacy",
        1,
        {"_id": 1, "label": "after"},
    )
    assert typed_backend.find_one("legacy", {"_id": 1})["label"] == "after"

    typed_backend.delete_ids("legacy", [1])
    assert typed_backend.find_one("legacy", {"_id": 1}) is None


def test_legacy_string_id_survives_a_negated_numeric_id_filter(typed_backend):
    _write_legacy_row(
        typed_backend,
        "legacy_nor",
        {"_id": "1", "label": "string"},
    )

    assert typed_backend.find(
        "legacy_nor",
        {"$nor": [{"_id": 1}]},
    ) == [{"_id": "1", "label": "string"}]


def test_legacy_numeric_ids_accept_bson_equivalent_lookup_forms(typed_backend):
    _write_legacy_row(
        typed_backend,
        "legacy_numeric",
        {"_id": 1.0, "label": "legacy"},
    )
    typed_backend.insert_many(
        "legacy_numeric",
        [{"_id": 2, "label": "current"}],
    )

    assert typed_backend.find_one("legacy_numeric", {"_id": 1})["label"] == "legacy"
    assert sorted(
        document["label"]
        for document in typed_backend.find(
            "legacy_numeric",
            {"$or": [{"_id": 1}, {"_id": 2}]},
        )
    ) == ["current", "legacy"]

    typed_backend.replace_one(
        "legacy_numeric",
        1,
        {"_id": 1.0, "label": "updated"},
    )
    assert typed_backend.find_one("legacy_numeric", {"_id": 1})["label"] == "updated"

    typed_backend.delete_ids("legacy_numeric", [1])
    assert typed_backend.find_one("legacy_numeric", {"_id": 1.0}) is None


def test_native_key_collision_guards_remain_mapped_to_duplicate_errors(
    typed_backend,
):
    collection = "defensive_collision"
    mismatched_row = (
        _physical_id_key("target"),
        _json_dumps({"_id": 1, "label": "mismatched"}),
    )
    if isinstance(typed_backend, ParquetDuckDBBackend):
        typed_backend._write_rows(collection, [mismatched_row])
    else:
        typed_backend.create_collection(collection)
        conn = typed_backend._connect()
        try:
            conn.execute(
                "INSERT INTO {0} (_id, data) VALUES (?, ?)".format(
                    _quote_identifier(collection)
                ),
                mismatched_row,
            )
            try:
                conn.commit()
            except AttributeError:
                pass
        finally:
            conn.close()

    with pytest.raises(DuplicateKeyError):
        typed_backend.insert_many(
            collection,
            [{"_id": "target", "label": "new"}],
        )
