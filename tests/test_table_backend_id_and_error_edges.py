"""Focused coverage for typed row IDs and native backend error boundaries."""

from datetime import datetime, timezone
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from tinymongo import table_backends as backends
from tinymongo.errors import DuplicateKeyError


def test_physical_id_keys_cover_nonfinite_container_and_fallback_values(monkeypatch):
    positive = backends._physical_id_key(float("inf"))
    negative = backends._physical_id_key(float("-inf"))
    not_a_number = backends._physical_id_key(float("nan"))

    assert positive.startswith(backends._PHYSICAL_ID_PREFIX)
    assert len({positive, negative, not_a_number}) == 3
    assert backends._canonical_id_value((1, "two")) == [
        "array",
        [
            ["registered-scalar", ["number", [1, 1]]],
            ["registered-scalar", ["string", "two"]],
        ],
    ]

    monkeypatch.setattr(backends, "bson_identity_key", lambda _value: None)
    assert backends._canonical_id_value("plain JSON") == [
        "encoded-scalar",
        "plain JSON",
    ]


def test_legacy_id_candidate_and_scan_edge_cases():
    assert "0.0" in backends._physical_id_candidates(0)
    assert "-0.0" in backends._physical_id_candidates(0)
    assert str(2**53 + 1) in backends._physical_id_candidates(2**53 + 1)
    assert str(float(2**53 + 1)) not in backends._physical_id_candidates(2**53 + 1)
    assert backends._physical_id_candidates(10**400)[-1] == str(10**400)
    assert backends._requires_legacy_id_scan((1, 2))


def test_legacy_id_comparison_and_temporary_remote_wrapper_compatibility():
    assert backends._legacy_id_values_equal([1, {"b": 2}], (1.0, {"b": 2.0}))
    assert not backends._legacy_id_values_equal([1], [1, 2])

    backend = backends.RemoteSQLTableBackend("", database="app", dsn="remote")
    document = {"_id": 1, "value": "wrapped"}
    temporarily_wrapped = backends._json_dumps(backends._json_dumps(document))

    assert backend._decode_data_value(temporarily_wrapped) == document


def test_local_matching_id_falls_back_to_a_legacy_container_scan():
    target = {"first": 1, "second": 2}
    data = backends._json_dumps({"_id": target, "label": "legacy"})

    class Connection:
        def execute(self, sql, _params=None):
            if " WHERE " in sql:
                return _RemoteCursor(rows=[])
            return _RemoteCursor(rows=[("unpredictable-legacy-key", data)])

    assert (
        backends._local_matching_physical_row_id(
            Connection(),
            '"items"',
            target,
        )
        == "unpredictable-legacy-key"
    )


def test_legacy_scan_detection_and_postfilter_fallbacks():
    container = {"value": 1}
    assert backends._id_condition_requires_legacy_scan(container)
    assert backends._id_condition_requires_legacy_scan({"$eq": container})
    assert backends._id_condition_requires_legacy_scan({"$in": container})
    assert backends._id_condition_requires_legacy_scan({"$in": [container]})
    assert backends._id_condition_requires_legacy_scan({"$in": [1], "$eq": container})
    assert not backends._id_condition_requires_legacy_scan({"$eq": 1})
    assert not backends._id_condition_requires_legacy_scan({"$unknown": container})
    assert not backends._filter_requires_legacy_id_scan("not-a-filter")
    assert backends._filter_requires_legacy_id_scan({"_id": container})

    recovered = {"_id": container, "label": "legacy"}
    assert backends._postfilter_id_candidates(
        [],
        {"_id": {"$eq": container}},
        lambda: [recovered],
    ) == [recovered]


def test_legacy_id_candidate_and_scan_routing_edges():
    huge = 10**400
    nonrepresentable = 2**53 + 1

    assert backends._physical_id_candidates(huge)[1:] == (str(huge),)
    assert backends._physical_id_candidates(nonrepresentable)[1:] == (
        str(nonrepresentable),
    )
    assert backends._physical_id_candidates(0)[1:] == ("0", "0.0", "-0.0")
    assert "1" in backends._physical_id_candidates(1.0)
    assert backends._requires_legacy_id_scan({"nested": 1})
    assert backends._requires_legacy_id_scan(datetime(2026, 7, 29))
    assert not backends._requires_legacy_id_scan("ordinary")

    moment = datetime(2026, 7, 29, tzinfo=timezone.utc)
    assert backends._id_condition_requires_legacy_scan({"nested": 1})
    assert backends._id_condition_requires_legacy_scan({"$eq": moment})
    assert backends._id_condition_requires_legacy_scan({"$in": [moment]})
    assert not backends._id_condition_requires_legacy_scan(
        {"$in": ["ordinary"], "$ne": "ordinary"}
    )
    assert not backends._id_condition_requires_legacy_scan({"$ne": moment})
    assert not backends._filter_requires_legacy_id_scan("not-a-filter")
    assert backends._filter_requires_legacy_id_scan({"_id": moment})

    legacy_document = {"_id": moment.replace(tzinfo=None), "value": "legacy"}
    assert backends._postfilter_id_candidates(
        [],
        {"_id": moment},
        lambda: [legacy_document],
    ) == [legacy_document]


def test_matching_physical_row_id_skips_malformed_and_mismatched_rows():
    rows = [
        ("invalid-json", "{"),
        ("missing-id", '{"value": 1}'),
        ("different-id", '{"_id": 2}'),
    ]

    assert backends._matching_physical_row_id(rows, 1) is None


def test_legacy_id_recovery_and_wrapped_remote_payload_edges():
    assert backends._legacy_id_values_equal(
        [{"nested": {"number": 1}}],
        [{"nested": {"number": 1.0}}],
    )
    assert not backends._legacy_id_values_equal([1], [1, 2])

    backend = backends.RemoteSQLTableBackend("", database="app", dsn="remote")
    wrapped = backends._json_dumps(backends._json_dumps({"_id": 1}))
    assert backend._decode_data_value(wrapped) == {"_id": 1}


def test_sql_compiler_supports_explicit_id_equality():
    where, params = backends.SQLCompiler("sqlite").compile({"_id": {"$eq": 1}})

    assert "_id = ?" in where
    assert params == list(backends._physical_id_candidates(1))


def test_sqlite_maps_native_insert_conflicts_and_ignores_missing_replacements(
    tmp_path, monkeypatch
):
    backend = backends.SQLiteTableBackend(str(tmp_path / "items.sqlite"))

    class ConflictConnection:
        def executemany(self, _sql, _rows):
            raise sqlite3.IntegrityError("native primary-key conflict")

        def close(self):
            pass

    monkeypatch.setattr(backend, "create_collection", lambda _collection: None)
    monkeypatch.setattr(backend, "find", lambda _collection, _filter: [])
    monkeypatch.setattr(backend, "validate_unique_post_image", lambda *_args: None)
    monkeypatch.setattr(backend, "_connect", ConflictConnection)

    with pytest.raises(DuplicateKeyError, match="native primary-key conflict"):
        backend.insert_many("items", [{"_id": 1}])

    missing = backends.SQLiteTableBackend(str(tmp_path / "missing.sqlite"))
    assert missing.replace_one("items", 1, {"_id": 1, "value": "new"}) is None


def test_duckdb_maps_native_insert_conflicts_and_ignores_missing_replacements(
    tmp_path, monkeypatch
):
    pytest.importorskip("duckdb")
    backend = backends.DuckDBTableBackend(str(tmp_path / "items.duckdb"))

    class ConflictConnection:
        def executemany(self, _sql, _rows):
            raise backend.duckdb.ConstraintException("native primary-key conflict")

        def close(self):
            pass

    monkeypatch.setattr(backend, "create_collection", lambda _collection: None)
    monkeypatch.setattr(backend, "find", lambda _collection, _filter: [])
    monkeypatch.setattr(backend, "validate_unique_post_image", lambda *_args: None)
    monkeypatch.setattr(backend, "_connect", ConflictConnection)

    with pytest.raises(DuplicateKeyError, match="native primary-key conflict"):
        backend.insert_many("items", [{"_id": 1}])

    missing = backends.DuckDBTableBackend(str(tmp_path / "missing.duckdb"))
    assert missing.replace_one("items", 1, {"_id": 1, "value": "new"}) is None


def test_parquet_rejects_a_physical_id_collision_in_inconsistent_rows(
    tmp_path, monkeypatch
):
    pytest.importorskip("duckdb")
    backend = backends.ParquetDuckDBBackend(str(tmp_path / "items.parquet"))
    inconsistent_rows = [
        (
            backends._physical_id_key(1),
            backends._json_dumps({"_id": 2, "value": "mismatched"}),
        )
    ]
    monkeypatch.setattr(
        backend,
        "_read_all_rows",
        lambda collection: inconsistent_rows if collection == "items" else [],
    )

    with pytest.raises(DuplicateKeyError, match="_id:1 already exists"):
        backend.insert_many("items", [{"_id": 1, "value": "new"}])


class _RemoteConnection:
    def close(self):
        pass


class _RemoteCursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = [] if rows is None else rows

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def close(self):
        pass


def test_remote_backend_maps_insert_conflicts_and_ignores_missing_replacements(
    monkeypatch,
):
    backend = backends.RemoteSQLTableBackend("", database="app", dsn="remote")
    monkeypatch.setattr(backend, "create_collection", lambda _collection: None)
    monkeypatch.setattr(backend, "_all_docs_unfiltered", lambda _collection: [])
    monkeypatch.setattr(backend, "validate_unique_post_image", lambda *_args: None)
    monkeypatch.setattr(backend, "_connect", _RemoteConnection)
    monkeypatch.setattr(
        backend,
        "_insert_rows",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("duplicate key")),
    )

    with pytest.raises(DuplicateKeyError, match="duplicate key"):
        backend.insert_many("items", [{"_id": 1}])

    monkeypatch.setattr(
        backend, "_stored_row_by_id", lambda _collection, _doc_id: (None, None)
    )
    assert backend.replace_one("items", 1, {"_id": 1, "value": "new"}) is None


def test_remote_stored_row_lookup_skips_a_mismatched_current_row(monkeypatch):
    backend = backends.RemoteSQLTableBackend("", database="app", dsn="remote")
    candidates = backends._physical_id_candidates(1)
    responses = {
        candidates[0]: (backends._json_dumps({"_id": 2}),),
        candidates[1]: (backends._json_dumps({"_id": 1}),),
    }

    monkeypatch.setattr(backend, "_connect", _RemoteConnection)
    monkeypatch.setattr(
        backend,
        "_execute",
        lambda _conn, _sql, params=None: _RemoteCursor(responses.get(params[0])),
    )

    assert backend._stored_row_by_id("items", 1) == (
        candidates[1],
        {"_id": 1},
    )


def test_remote_missing_scalar_id_does_not_scan_the_complete_table(monkeypatch):
    backend = backends.RemoteSQLTableBackend("", database="app", dsn="remote")
    statements = []

    def execute(_conn, sql, params=None):
        statements.append((sql, params))
        return _RemoteCursor(None)

    monkeypatch.setattr(backend, "_connect", _RemoteConnection)
    monkeypatch.setattr(backend, "_execute", execute)

    assert backend._stored_row_by_id("items", "missing") == (None, None)
    assert statements
    assert all("SELECT _id, data" not in sql for sql, _params in statements)


def test_remote_stored_row_lookup_recovers_a_legacy_numeric_equivalent(monkeypatch):
    backend = backends.RemoteSQLTableBackend("", database="app", dsn="remote")
    legacy_data = backends._json_dumps({"_id": 1, "value": "legacy"})
    statements = []

    def execute(_conn, sql, params=None):
        statements.append((sql, params))
        row = (legacy_data,) if params == ("1",) else None
        return _RemoteCursor(row)

    monkeypatch.setattr(backend, "_connect", _RemoteConnection)
    monkeypatch.setattr(backend, "_execute", execute)

    assert backend._stored_row_by_id("items", 1.0) == (
        "1",
        {"_id": 1, "value": "legacy"},
    )
    assert all("SELECT _id, data" not in sql for sql, _params in statements)


@pytest.mark.parametrize("matching", [True, False])
def test_remote_stored_row_lookup_scans_legacy_container_ids(monkeypatch, matching):
    backend = backends.RemoteSQLTableBackend("", database="app", dsn="remote")
    target = {"first": 1, "second": 2}
    stored = target if matching else {"different": 1}
    rows = [
        (
            "malformed-legacy-key",
            backends._json_dumps({"label": "missing-id"}),
        ),
        (
            "unpredictable-legacy-key",
            backends._json_dumps({"_id": stored, "label": "legacy"}),
        ),
    ]

    def execute(_conn, _sql, params=None):
        if params is not None:
            return _RemoteCursor(None)
        return _RemoteCursor(rows=rows)

    monkeypatch.setattr(backend, "_connect", _RemoteConnection)
    monkeypatch.setattr(backend, "_execute", execute)

    result = backend._stored_row_by_id("items", target)
    if matching:
        assert result == (
            "unpredictable-legacy-key",
            {"_id": target, "label": "legacy"},
        )
    else:
        assert result == (None, None)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(1060, "already exists"),
        RuntimeError(9999, "duplicate column name"),
    ],
)
def test_mysql_ordered_column_upgrade_tolerates_duplicate_column_races(
    monkeypatch,
    error,
):
    backend = object.__new__(backends.MySQLTableBackend)
    backend.database = "app"
    calls = []

    def execute(_conn, sql, params=None):
        calls.append((sql, params))
        if "information_schema.columns" in sql:
            return _RemoteCursor()
        raise error

    monkeypatch.setattr(backend, "_execute", execute)

    backend._ensure_ordered_data_column(_RemoteConnection(), "items", "`app__items`")
    assert len(calls) == 2


def test_mysql_ordered_column_upgrade_propagates_unrelated_errors(monkeypatch):
    backend = object.__new__(backends.MySQLTableBackend)
    backend.database = "app"

    def execute(_conn, sql, params=None):
        if "information_schema.columns" in sql:
            return _RemoteCursor()
        raise RuntimeError(9999, "permission denied")

    monkeypatch.setattr(backend, "_execute", execute)

    with pytest.raises(RuntimeError, match="permission denied"):
        backend._ensure_ordered_data_column(
            _RemoteConnection(),
            "items",
            "`app__items`",
        )


def test_remote_stored_row_lookup_scans_for_legacy_datetime_ids(monkeypatch):
    backend = backends.RemoteSQLTableBackend("", database="app", dsn="remote")
    stored_id = datetime(2026, 7, 29, 12, 30)
    requested_id = stored_id.replace(tzinfo=timezone.utc)
    legacy_row = (
        str(stored_id),
        backends._json_dumps({"_id": stored_id, "value": "legacy"}),
    )

    def execute_with_match(_conn, _sql, params=None):
        if params is not None:
            return _RemoteCursor()
        return _RemoteCursor(rows=[legacy_row])

    monkeypatch.setattr(backend, "_connect", _RemoteConnection)
    monkeypatch.setattr(backend, "_execute", execute_with_match)
    assert backend._stored_row_by_id("items", requested_id) == (
        str(stored_id),
        {"_id": stored_id, "value": "legacy"},
    )

    monkeypatch.setattr(
        backend,
        "_execute",
        lambda _conn, _sql, params=None: _RemoteCursor(),
    )
    assert backend._stored_row_by_id("items", requested_id) == (None, None)


def test_remote_legacy_scan_skips_rows_without_an_id(monkeypatch):
    backend = backends.RemoteSQLTableBackend("", database="app", dsn="remote")

    def execute(_conn, _sql, params=None):
        if params is not None:
            return _RemoteCursor()
        return _RemoteCursor(rows=[("legacy", None, '{"value": "missing-id"}')])

    monkeypatch.setattr(backend, "_connect", _RemoteConnection)
    monkeypatch.setattr(backend, "_execute", execute)

    assert backend._stored_row_by_id("items", {"target": 1}) == (None, None)


@pytest.mark.parametrize(
    ("error", "should_raise"),
    [
        (RuntimeError(1060, "duplicate column"), False),
        (RuntimeError("duplicate column name"), False),
        (RuntimeError("permission denied"), True),
    ],
)
def test_mysql_ordered_column_upgrade_handles_only_duplicate_races(
    monkeypatch,
    error,
    should_raise,
):
    monkeypatch.setitem(sys.modules, "pymysql", SimpleNamespace())
    backend = backends.MySQLTableBackend(
        "",
        database="app",
        dsn="mysql://localhost/db",
    )

    def execute(_conn, sql, params=None):
        if "information_schema.columns" in sql:
            return _RemoteCursor()
        raise error

    monkeypatch.setattr(backend, "_execute", execute)

    if should_raise:
        with pytest.raises(RuntimeError, match="permission denied"):
            backend._ensure_ordered_data_column(None, "items", "`app__items`")
    else:
        backend._ensure_ordered_data_column(None, "items", "`app__items`")

    assert backend.ordered_data_type == "LONGTEXT"


def test_local_stored_row_lookup_scans_for_legacy_datetime_ids():
    connection = sqlite3.connect(":memory:")
    stored_id = datetime(2026, 7, 29, 12, 30)
    requested_id = stored_id.replace(tzinfo=timezone.utc)
    connection.execute("CREATE TABLE items (_id TEXT PRIMARY KEY, data TEXT)")
    connection.execute(
        "INSERT INTO items (_id, data) VALUES (?, ?)",
        (
            str(stored_id),
            backends._json_dumps({"_id": stored_id, "value": "legacy"}),
        ),
    )

    try:
        assert backends._local_matching_physical_row_id(
            connection,
            '"items"',
            requested_id,
        ) == str(stored_id)
    finally:
        connection.close()
