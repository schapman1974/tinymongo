"""Contracts for conservative SQLite complex-read candidate selection."""

import sqlite3

import pytest

import tinymongo as tm
from tinymongo import table_backends
from tinymongo.errors import DuplicateKeyError
from tinymongo.indexes import parse_index_spec


BENCHMARK_QUERY = {
    "$and": [
        {"group": {"$in": ["g1", "g3", "g7"]}},
        {"i": {"$gte": 40, "$lt": 360}},
        {"i": {"$mod": [7, 0]}},
    ]
}


def _collection(tmp_path, name="items"):
    client = tm.TinyMongoClient(str(tmp_path), backend="sqlite")
    return client, client.app[name]


def _track_decodes(monkeypatch):
    decoded_payloads = []
    original_loads = table_backends._json_loads

    def tracked_loads(value):
        decoded_payloads.append(value)
        return original_loads(value)

    monkeypatch.setattr(table_backends, "_json_loads", tracked_loads)
    return decoded_payloads


def _matching_ids(documents, query):
    return [
        document["_id"]
        for document in documents
        if table_backends.matches_filter(document, query)
    ]


def _add_benchmark_indexes(collection):
    collection.create_index("group", name="group_lookup")
    collection.create_index("i", name="i_lookup")


def test_sqlite_complex_and_in_range_mod_decodes_only_final_scalar_candidates(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path, "complex_candidates")
    documents = [
        {
            "_id": index,
            "group": "g{0}".format(index % 10),
            "i": index,
            "payload": "x" * 1_000,
        }
        for index in range(400)
    ]
    collection.insert_many(documents)
    _add_benchmark_indexes(collection)
    expected_ids = _matching_ids(documents, BENCHMARK_QUERY)
    decodes = _track_decodes(monkeypatch)

    try:
        found = list(collection.find(BENCHMARK_QUERY))

        assert [document["_id"] for document in found] == expected_ids
        assert len(decodes) == len(expected_ids)
        assert len(decodes) < len(documents) // 10
    finally:
        client.close()


def test_sqlite_complex_candidates_preserve_array_member_matches(tmp_path):
    client, collection = _collection(tmp_path, "complex_arrays")
    documents = [
        {"_id": "scalar", "group": "g1", "i": 56},
        {"_id": "array-group", "group": ["other", "g3"], "i": 63},
        {"_id": "array-number", "group": "g7", "i": [-1, 70, 500]},
        {"_id": "wrong-group", "group": ["g2", "g4"], "i": 77},
        {"_id": "wrong-mod", "group": "g1", "i": [57, 58]},
    ]
    collection.insert_many(documents)
    _add_benchmark_indexes(collection)

    try:
        assert {row["_id"] for row in collection.find(BENCHMARK_QUERY)} == {
            "scalar",
            "array-group",
            "array-number",
        }
    finally:
        client.close()


def test_sqlite_complex_candidates_keep_mixed_scalar_types_distinct(tmp_path):
    client, collection = _collection(tmp_path, "complex_mixed_types")
    documents = [
        {"_id": "bool", "group": True, "i": 7},
        {"_id": "number", "group": 1, "i": 7},
        {"_id": "string", "group": "1", "i": 7},
        {"_id": "bool-array", "group": [False, True], "i": 7},
        {"_id": "number-array", "group": [0, 1], "i": 7},
        {"_id": "string-array", "group": ["0", "1"], "i": 7},
        {"_id": "miss", "group": False, "i": 7},
    ]
    query = {
        "$and": [
            {"group": {"$in": [True, 1, "1"]}},
            {"i": {"$mod": [7, 0]}},
        ]
    }
    collection.insert_many(documents)
    collection.create_index("group")

    try:
        assert [row["_id"] for row in collection.find(query)] == [
            "bool",
            "number",
            "string",
            "bool-array",
            "number-array",
            "string-array",
        ]
    finally:
        client.close()


def test_sqlite_complex_candidates_preserve_safe_and_int64_numeric_boundaries(
    tmp_path,
):
    client, collection = _collection(tmp_path, "complex_numeric_boundaries")
    safe = table_backends._SQLITE_SAFE_QUERY_NUMBER
    int64_min = -(2**63)
    documents = [
        {"_id": "below-safe", "group": "g1", "i": -safe - 1},
        {"_id": "safe-match", "group": "g1", "i": -safe},
        {"_id": "safe-neighbor", "group": "g1", "i": -safe + 1},
        {"_id": "safe-array", "group": "g1", "i": [-safe - 1, -safe]},
        {"_id": "int64-match", "group": "g1", "i": int64_min},
        {"_id": "int64-neighbor", "group": "g1", "i": int64_min + 1},
        {"_id": "wrong-group", "group": "g2", "i": -safe},
    ]
    safe_query = {
        "$and": [
            {"group": {"$in": ["g1"]}},
            {"i": {"$gte": -safe, "$lte": safe}},
            {"i": {"$mod": [7, -3]}},
        ]
    }
    int64_remainder = table_backends._truncating_remainder(int64_min, 7)
    int64_query = {
        "$and": [
            {"group": {"$in": ["g1"]}},
            {"i": {"$gte": int64_min, "$lte": int64_min + 1}},
            {"i": {"$mod": [7, int64_remainder]}},
        ]
    }
    collection.insert_many(documents)
    collection.create_index("group")

    try:
        assert [row["_id"] for row in collection.find(safe_query)] == [
            "safe-match",
            "safe-array",
        ]
        assert [row["_id"] for row in collection.find(int64_query)] == ["int64-match"]
    finally:
        client.close()


def test_sqlite_complex_candidates_restore_natural_order_before_bounds(tmp_path):
    client, collection = _collection(tmp_path, "complex_candidate_order")
    documents = [
        {"_id": "scalar-first", "group": "g1", "i": 7},
        {"_id": "array-first", "group": ["g1"], "i": 14},
        {"_id": "scalar-miss", "group": "g2", "i": 21},
        {"_id": "array-second", "group": ["other", "g1"], "i": 28},
        {"_id": "scalar-second", "group": "g1", "i": 35},
    ]
    query = {
        "$and": [
            {"group": {"$in": ["g1"]}},
            {"i": {"$mod": [7, 0]}},
        ]
    }
    collection.insert_many(documents)
    collection.create_index("group")

    try:
        assert [row["_id"] for row in collection.find(query).skip(1).limit(2)] == [
            "array-first",
            "array-second",
        ]
    finally:
        client.close()


def test_sqlite_complex_candidates_preserve_embedded_object_in_matches(tmp_path):
    client, collection = _collection(tmp_path, "complex_objects")
    documents = [
        {"_id": "match", "group": {"name": "g1", "rank": 1}, "i": 14},
        {"_id": "different-object", "group": {"name": "g1"}, "i": 14},
        {"_id": "outside-range", "group": {"name": "g1", "rank": 1}, "i": -1},
    ]
    query = {
        "$and": [
            {"group": {"$in": [{"name": "g1", "rank": 1}]}},
            {"i": {"$gte": 0, "$lt": 100}},
        ]
    }
    collection.insert_many(documents)
    _add_benchmark_indexes(collection)

    try:
        assert [row["_id"] for row in collection.find(query)] == ["match"]
    finally:
        client.close()


def test_sqlite_complex_candidates_preserve_decimal128_numeric_matches(tmp_path):
    bson = pytest.importorskip("bson")
    client, collection = _collection(tmp_path, "complex_decimals")
    documents = [
        {"_id": "decimal-match", "group": "g1", "i": bson.Decimal128("56")},
        {"_id": "decimal-mod-miss", "group": "g1", "i": bson.Decimal128("57")},
        {"_id": "ordinary-match", "group": "g3", "i": 63},
    ]
    collection.insert_many(documents)
    _add_benchmark_indexes(collection)

    try:
        assert {row["_id"] for row in collection.find(BENCHMARK_QUERY)} == {
            "decimal-match",
            "ordinary-match",
        }
    finally:
        client.close()


def test_sqlite_complex_candidates_preserve_large_integer_matches(tmp_path):
    client, collection = _collection(tmp_path, "complex_large_numbers")
    huge = 2**100
    documents = [
        {"_id": "match", "group": "g1", "i": huge},
        {"_id": "nearby", "group": "g1", "i": huge + 10},
        {"_id": "wrong-group", "group": "g2", "i": huge},
    ]
    query = {
        "$and": [
            {"group": {"$in": ["g1"]}},
            {"i": {"$gte": huge - 1, "$lte": huge + 1}},
        ]
    }
    collection.insert_many(documents)
    _add_benchmark_indexes(collection)

    try:
        assert [row["_id"] for row in collection.find(query)] == ["match"]
    finally:
        client.close()


def test_sqlite_complex_unsupported_or_branch_falls_back_without_false_negatives(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path, "complex_unsupported")
    documents = [
        {"_id": 1, "group": "g1", "score": 1, "label": "inside"},
        {"_id": 2, "group": "g1", "score": 2, "label": "inside"},
        {"_id": 3, "group": "g2", "score": 3, "label": "outside-match"},
        {"_id": 4, "group": "g2", "score": 4, "label": "outside-miss"},
    ]
    query = {
        "$or": [
            {
                "$and": [
                    {"group": {"$in": ["g1"]}},
                    {"score": {"$ne": 2}},
                ]
            },
            {"label": {"$regex": "^outside-match$"}},
        ]
    }
    collection.insert_many(documents)
    collection.create_index("group")
    decodes = _track_decodes(monkeypatch)

    try:
        assert [row["_id"] for row in collection.find(query)] == [1, 3]
        assert len(decodes) == len(documents)
    finally:
        client.close()


def test_sqlite_complex_negative_only_filter_uses_safe_full_scan(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path, "complex_negative")
    documents = [
        {"_id": 1, "group": "g1"},
        {"_id": 2, "group": "g2"},
        {"_id": 3},
    ]
    collection.insert_many(documents)
    collection.create_index("group")
    decodes = _track_decodes(monkeypatch)

    try:
        assert [row["_id"] for row in collection.find({"group": {"$nin": ["g1"]}})] == [
            2,
            3,
        ]
        assert len(decodes) == len(documents)
    finally:
        client.close()


def test_sqlite_complex_candidates_keep_bounds_projection_and_count_semantics(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path, "complex_consumers")
    documents = [
        {
            "_id": index,
            "group": "g{0}".format(index % 10),
            "i": index,
            "label": "item-{0}".format(index),
            "payload": "x" * 1_000,
        }
        for index in range(400)
    ]
    collection.insert_many(documents)
    _add_benchmark_indexes(collection)
    expected_ids = _matching_ids(documents, BENCHMARK_QUERY)
    decodes = _track_decodes(monkeypatch)

    try:
        bounded = list(collection.find(BENCHMARK_QUERY).skip(2).limit(3))
        assert [row["_id"] for row in bounded] == expected_ids[2:5]
        assert len(decodes) <= 5

        decodes.clear()
        projected = list(
            collection.find(BENCHMARK_QUERY, {"label": 1, "_id": 0}).skip(1).limit(2)
        )
        assert projected == [
            {"label": "item-{0}".format(index)} for index in expected_ids[1:3]
        ]
        assert len(decodes) <= 3

        decodes.clear()
        assert collection.count_documents(BENCHMARK_QUERY) == len(expected_ids)
        assert len(decodes) <= len(expected_ids)
    finally:
        client.close()


def test_sqlite_complex_query_without_declared_indexes_remains_complete(tmp_path):
    client, collection = _collection(tmp_path, "complex_no_index")
    documents = [
        {"_id": index, "group": "g{0}".format(index % 10), "i": index}
        for index in range(400)
    ]
    collection.insert_many(documents)
    expected_ids = _matching_ids(documents, BENCHMARK_QUERY)

    try:
        assert [row["_id"] for row in collection.find(BENCHMARK_QUERY)] == (
            expected_ids
        )
    finally:
        client.close()


def test_sqlite_complex_query_recovers_dropped_native_index_storage(tmp_path):
    client, collection = _collection(tmp_path, "complex_dropped_index")
    documents = [
        {"_id": index, "group": "g{0}".format(index % 10), "i": index}
        for index in range(100)
    ]
    collection.insert_many(documents)
    _add_benchmark_indexes(collection)
    backend = collection.parent.engine
    specs = [
        parse_index_spec("group", name="group_lookup"),
        parse_index_spec("i", name="i_lookup"),
    ]

    # Warm the query metadata and the native companion-index cache first.
    assert list(collection.find(BENCHMARK_QUERY))
    connection = sqlite3.connect(backend.path)
    try:
        for spec in specs:
            physical = backend._physical_index_name(collection.name, spec)
            connection.execute(
                "DROP INDEX IF EXISTS {0}".format(
                    table_backends._quote_identifier(physical)
                )
            )
            connection.execute(
                "DROP INDEX IF EXISTS {0}".format(
                    table_backends._quote_identifier(physical + "_types")
                )
            )
        connection.commit()
    finally:
        connection.close()

    try:
        assert [row["_id"] for row in collection.find(BENCHMARK_QUERY)] == (
            _matching_ids(documents, BENCHMARK_QUERY)
        )

        connection = sqlite3.connect(backend.path)
        try:
            rebuilt = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        finally:
            connection.close()
        anchor_spec = next(spec for spec in specs if spec.field == "group")
        physical = backend._physical_index_name(collection.name, anchor_spec)
        assert physical in rebuilt
        assert physical + "_types" in rebuilt
    finally:
        client.close()


def test_sqlite_complex_query_handles_missing_index_catalog_as_no_index(tmp_path):
    client, collection = _collection(tmp_path, "complex_missing_catalog")
    documents = [
        {"_id": index, "group": "g{0}".format(index % 10), "i": index}
        for index in range(100)
    ]
    collection.insert_many(documents)
    _add_benchmark_indexes(collection)
    backend = collection.parent.engine

    # Populate the schema-versioned query cache before another writer removes
    # the catalog. The next read must invalidate the cache and scan safely.
    assert list(collection.find(BENCHMARK_QUERY))
    connection = sqlite3.connect(backend.path)
    try:
        connection.execute(
            "DROP TABLE {0}".format(
                table_backends._quote_identifier(backend.index_catalog_table)
            )
        )
        connection.commit()
    finally:
        connection.close()

    try:
        assert [row["_id"] for row in collection.find(BENCHMARK_QUERY)] == (
            _matching_ids(documents, BENCHMARK_QUERY)
        )
    finally:
        client.close()


def test_sqlite_complex_query_propagates_index_catalog_failures(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path, "complex_catalog_failure")
    collection.insert_many(
        [
            {"_id": 1, "group": "g1", "i": 56},
            {"_id": 2, "group": "g2", "i": 63},
        ]
    )
    _add_benchmark_indexes(collection)
    backend = collection.parent.engine

    def fail_catalog(_connection, _collection_name):
        raise DuplicateKeyError("unsafe index migration")

    monkeypatch.setattr(
        backend,
        "_get_query_index_specs_on_connection",
        fail_catalog,
    )
    try:
        with pytest.raises(DuplicateKeyError, match="unsafe index migration"):
            list(collection.find(BENCHMARK_QUERY))
    finally:
        client.close()


def test_sqlite_complex_query_falls_back_when_total_parameters_are_too_large(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path, "complex_parameter_limit")
    documents = [
        {"_id": "first", "group": 1, "i": 7},
        {"_id": "second", "group": 899, "i": 14},
        {"_id": "outside", "group": 900, "i": 21},
    ]
    collection.insert_many(documents)
    collection.create_index("group")
    query = {
        "$and": [
            {"group": {"$in": list(range(900))}},
            {"i": {"$gte": 0}},
        ]
    }
    decodes = _track_decodes(monkeypatch)

    try:
        assert [row["_id"] for row in collection.find(query)] == [
            "first",
            "second",
        ]
        assert len(decodes) == len(documents)
    finally:
        client.close()


def test_sqlite_native_filter_does_not_consult_complex_candidate_planner(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path, "native_filter")
    collection.insert_many(
        [
            {"_id": 1, "group": "g1"},
            {"_id": 2},
        ]
    )
    backend = collection.parent.engine

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("complex candidate planner should not run")

    monkeypatch.setattr(
        backend,
        "_find_complex_index_candidates",
        fail_if_called,
    )
    try:
        assert [
            row["_id"] for row in collection.find({"group": {"$exists": True}})
        ] == [1]
    finally:
        client.close()


def test_sqlite_complex_candidate_helper_boundaries(tmp_path):
    client, collection = _collection(tmp_path, "candidate_boundaries")
    backend = collection.parent.engine

    try:
        assert table_backends._positive_filter_conjuncts("not-a-filter") == []
        assert backend._sqlite_candidate_scalar(True) == ("boolean", 1)
        assert backend._sqlite_candidate_scalar(1.5) == ("number", 1.5)
        assert backend._sqlite_candidate_scalar(float("inf")) is None
        assert backend._sqlite_index_candidate_values({"$eq": True}) == (True,)
        assert backend._sqlite_index_candidate_values({"$in": []}) == ()
        assert backend._sqlite_index_candidate_values({"$in": list(range(901))}) is None

        empty_sql, empty_params = backend._sqlite_index_candidate_id_query(
            collection.name,
            "group",
            (),
        )
        assert "WHERE 0" in empty_sql
        assert empty_params == []

        grouped_sql, grouped_params = backend._sqlite_index_candidate_id_query(
            collection.name,
            "group",
            (True, True, 1.5, "g1"),
        )
        assert "'true', 'false'" in grouped_sql
        assert grouped_params == [1, 1.5, "g1"]

        assert backend._sqlite_range_candidate_operand(1.5) == 1.5
        assert backend._sqlite_range_candidate_operand(float("inf")) is None
        assert backend._sqlite_range_candidate_operand("1.5") is None

        clauses, params = backend._sqlite_candidate_residual_where(
            [
                ("_id", {"$gt": 0}),
                ("nested.value", {"$lt": 10}),
                ("plain", 1),
                ("unsafe_range", {"$gt": float("inf")}),
                ("huge_mod", {"$mod": [2**54, 0]}),
                ("ignored", {"$exists": True}),
            ]
        )
        assert clauses == []
        assert params == []

        collection.create_index("group")
        connection = backend._connect()
        try:
            candidate = backend._sqlite_complex_candidate_query(
                connection,
                collection.name,
                {
                    "$and": [
                        {"_id": {"$ne": "not-present"}},
                        {"group": {"$in": ["g1"]}},
                    ]
                },
            )
        finally:
            connection.close()
        assert candidate is not None
    finally:
        client.close()


def test_sqlite_complex_candidates_cover_direct_backend_and_overflow_fallback(
    tmp_path,
    monkeypatch,
):
    client, collection = _collection(tmp_path, "candidate_backend_paths")
    documents = [
        {"_id": index, "group": "g{0}".format(index % 10), "i": index}
        for index in range(100)
    ]
    collection.insert_many(documents)
    _add_benchmark_indexes(collection)
    backend = collection.parent.engine

    try:
        assert [
            row["_id"] for row in backend.find(collection.name, BENCHMARK_QUERY)
        ] == (_matching_ids(documents, BENCHMARK_QUERY))

        def overflow(*_args, **_kwargs):
            raise OverflowError("SQLite integer conversion overflow")

        monkeypatch.setattr(backend, "_run_collection_read", overflow)
        assert (
            backend._find_complex_index_candidates(
                collection.name,
                BENCHMARK_QUERY,
            )
            is table_backends._MISSING
        )
    finally:
        client.close()
