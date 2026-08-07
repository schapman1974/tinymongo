import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path

import pytest


def _benchmark_module():
    path = Path(__file__).parent / "benchmarks" / "bench_storage.py"
    module_name = "tests.benchmarks.bench_storage"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def benchmark():
    return _benchmark_module()


def test_insert_batches_preserve_documents_and_target_four_distinct_shards(
    benchmark,
):
    documents = benchmark._docs(1000)
    batches = benchmark._document_batches(documents, 4)

    assert [len(batch) for batch in batches] == [243, 263, 241, 253]
    assert {document["_id"] for batch in batches for document in batch} == {
        document["_id"] for document in documents
    }
    for worker, batch in enumerate(batches):
        assert {benchmark._shard_index(document["_id"], 4) for document in batch} == {
            worker
        }


def test_concurrent_insert_runner_starts_all_workers_together_and_cleans_up(
    benchmark,
):
    entered = set()
    prepared = []
    cleaned = []
    lock = benchmark.threading.Lock()
    all_entered = benchmark.threading.Event()

    def prepare(worker, batch):
        resource = (worker, tuple(batch))
        with lock:
            prepared.append(resource)
        return resource

    def operation(worker, batch, resource):
        assert resource == (worker, tuple(batch))
        with lock:
            entered.add(worker)
            if len(entered) == 4:
                all_entered.set()
        assert all_entered.wait(timeout=2)
        return sum(batch)

    def cleanup(_worker, _batch, resource):
        with lock:
            cleaned.append(resource)

    elapsed, results = benchmark._run_simultaneously(
        [[1], [2], [3], [4]],
        operation,
        prepare=prepare,
        cleanup=cleanup,
    )

    assert elapsed > 0
    assert results == [1, 2, 3, 4]
    assert sorted(prepared) == sorted(cleaned)
    assert entered == {0, 1, 2, 3}


def test_concurrent_insert_runner_identifies_worker_failures(benchmark):
    def operation(worker, _batch, _resource):
        if worker == 2:
            raise ValueError("broken batch")
        return worker

    with pytest.raises(
        RuntimeError,
        match=r"worker 2 failed for a batch of 1 documents: ValueError: broken batch",
    ):
        benchmark._run_simultaneously([[0], [1], [2], [3]], operation)


@pytest.mark.parametrize(
    (
        "backend",
        "label",
        "persistence_verified",
        "expected_file_size",
        "durability",
    ),
    [
        (
            "sqlite",
            "TinyMongo SQLite",
            True,
            "positive",
            "sqlite-runtime",
        ),
        (
            "sqlite-sharded",
            "TinyMongo SQLite-sharded (4)",
            True,
            "positive",
            {"journal_mode": "WAL", "synchronous": "FULL"},
        ),
        (
            "raw-sqlite",
            "Raw SQLite (native SQL)",
            True,
            "positive",
            {"journal_mode": "WAL", "synchronous": "NORMAL"},
        ),
    ],
)
def test_real_local_backends_report_equivalent_crud_semantics(
    benchmark,
    tmp_path,
    backend,
    label,
    persistence_verified,
    expected_file_size,
    durability,
):
    result = benchmark.run_backend(
        backend,
        doc_count=13,
        query_count=5,
        work_root=str(tmp_path),
        sqlite_shards=4,
    )

    assert {
        "backend",
        "label",
        "available",
        "documents",
        "queries",
        "updated_docs",
        "deleted_docs",
        "remaining_docs",
        "persistence_verified",
        "durability",
    } <= result.keys()
    assert result["available"] is True
    assert result["backend"] == backend
    assert result["label"] == label
    assert result["documents"] == 13
    assert result["queries"] == 5
    assert result["updated_docs"] == 2
    assert result["deleted_docs"] == 2
    assert result["remaining_docs"] == 11
    assert result["persistence_verified"] is persistence_verified
    assert result["insert_workers"] == 4
    assert result["insert_batches"] == 4
    assert result["insert_batch_sizes"] == [5, 3, 2, 3]
    if backend == "raw-sqlite":
        assert result["insert_mode"] == "4 spawned executemany bulks"
    elif backend == "sqlite-sharded":
        assert result["insert_mode"] == "4 spawned shard-affine insert_many bulks"
        assert result["point_mode"] == "4 spawned shard-affine exact-ID streams"
    else:
        assert result["insert_mode"] == "4 spawned insert_many bulks"
    assert len(result["insert_process_ids"]) == 4
    assert len(set(result["insert_process_ids"])) == 4
    assert result["read_mode"] == "4 spawned full-collection scans"
    assert len(result["read_process_ids"]) == 4
    assert len(set(result["read_process_ids"])) == 4
    assert len(result["point_process_ids"]) == 4
    assert len(set(result["point_process_ids"])) == 4
    assert sum(result["point_batch_sizes"]) == result["queries"]
    assert result["point_wall_seconds"] > 0
    assert result["point_reads_per_second"] > 0
    assert result["read_count"] == result["documents"] * 4
    assert result["update_mode"] == "4 spawned disjoint-ID streams"
    assert len(result["update_process_ids"]) == 4
    assert len(set(result["update_process_ids"])) == 4
    assert result["delete_mode"] == "4 spawned disjoint-ID streams"
    assert len(result["delete_process_ids"]) == 4
    assert len(set(result["delete_process_ids"])) == 4
    if durability == "sqlite-runtime":
        assert result["durability"]["journal_mode"] == "WAL"
        assert result["durability"]["synchronous"] in {
            "OFF",
            "NORMAL",
            "FULL",
            "EXTRA",
        }
    else:
        assert result["durability"] == durability

    if expected_file_size is None:
        assert result["file_kib"] is None
    else:
        assert result["file_kib"] > 0

    for key in (
        "insert_seconds",
        "insert_docs_per_second",
        "read_seconds",
        "read_docs_per_second",
        "point_avg_ms",
        "point_p95_ms",
        "update_seconds",
        "update_docs_per_second",
        "delete_seconds",
        "delete_docs_per_second",
    ):
        assert result[key] > 0


def test_single_worker_profile_uses_one_process_for_every_phase(
    benchmark,
    tmp_path,
):
    result = benchmark.run_backend(
        "sqlite",
        doc_count=13,
        query_count=5,
        work_root=str(tmp_path),
        insert_workers=1,
    )

    assert result["insert_workers"] == 1
    assert result["insert_batch_sizes"] == [13]
    assert result["point_batch_sizes"] == [5]
    assert result["read_count"] == 13
    for key in (
        "insert_process_ids",
        "read_process_ids",
        "point_process_ids",
        "update_process_ids",
        "delete_process_ids",
    ):
        assert len(result[key]) == 1


@pytest.mark.parametrize("backend", ["memory", "duckdb"])
def test_non_shareable_backends_are_not_mixed_into_process_results(
    benchmark,
    tmp_path,
    backend,
):
    result = benchmark.run_backend(
        backend,
        doc_count=13,
        query_count=5,
        work_root=str(tmp_path),
    )

    assert result["available"] is False
    assert "process" in result["reason"]


def _sample_result(benchmark, backend="memory", file_kib=None):
    return benchmark._result(
        backend,
        10,
        2,
        0.5,
        0.25,
        10,
        [0.001, 0.003],
        0.2,
        1,
        0.1,
        1,
        9,
        file_kib,
        backend != "memory",
        {"policy": "test"},
    )


def test_markdown_formatter_keeps_na_and_unavailable_rows_aligned(benchmark):
    available = _sample_result(benchmark)
    unavailable = benchmark._unavailable("mongodb", "not configured")

    lines = benchmark.format_markdown([available, unavailable]).splitlines()

    assert lines == [
        "| Backend | Insert workload | Insert docs/s | Read-all docs/s | "
        "Point reads/s | Point avg ms | Point p95 ms | Update docs/s | "
        "Delete docs/s | Final KiB |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        "| TinyMongo Memory | single bulk | 20 | 40 | 500 | 2.000 | 3.000 | "
        "5 | 10 | N/A |",
        "| MongoDB | not run | not run | not run | not run | not run | not run | "
        "not run | not run | N/A |",
    ]
    assert {line.count("|") for line in lines} == {11}


def test_repeats_rotate_backends_and_aggregate_each_backend_in_input_order(
    benchmark,
    monkeypatch,
    tmp_path,
):
    backends = ("memory", "sqlite", "raw-sqlite")
    calls = []
    visits = defaultdict(int)

    def fake_run_backend(
        backend,
        doc_count,
        query_count,
        work_root,
        sqlite_shards=4,
        insert_workers=4,
        mongo_uri=None,
    ):
        assert doc_count == 100
        assert query_count == 3
        assert work_root == str(tmp_path)
        assert sqlite_shards == 8
        assert insert_workers == 8
        assert mongo_uri == "mongodb://unused"
        calls.append(backend)
        visits[backend] += 1
        result = benchmark._result(
            backend,
            doc_count,
            query_count,
            1.0,
            1.0,
            doc_count,
            [0.001],
            1.0,
            1,
            1.0,
            1,
            doc_count - 1,
            1.0,
            True,
            {"policy": "test"},
        )
        for key in benchmark._MEDIAN_KEYS:
            result[key] = float(visits[backend])
        return result

    monkeypatch.setattr(benchmark, "run_backend", fake_run_backend)

    payload = benchmark.run_benchmark(
        backends,
        documents=100,
        queries=3,
        repeats=4,
        work_root=str(tmp_path),
        sqlite_shards=8,
        insert_workers=8,
        mongo_uri="mongodb://unused",
    )

    assert calls == [
        "memory",
        "sqlite",
        "raw-sqlite",
        "sqlite",
        "raw-sqlite",
        "memory",
        "raw-sqlite",
        "memory",
        "sqlite",
        "memory",
        "sqlite",
        "raw-sqlite",
    ]
    assert [result["backend"] for result in payload["results"]] == list(backends)
    expected_orders = {
        "memory": [1, 3, 2, 1],
        "sqlite": [2, 1, 3, 2],
        "raw-sqlite": [3, 2, 1, 3],
    }
    for result in payload["results"]:
        assert result["repeat_count"] == 4
        assert [run["repeat"] for run in result["runs"]] == [1, 2, 3, 4]
        assert [run["execution_order"] for run in result["runs"]] == expected_orders[
            result["backend"]
        ]
        assert result["persistence_verified"] is True
        for key in benchmark._MEDIAN_KEYS:
            assert result[key] == 2.5
        assert result["insert_docs_per_second"] == 40.0
        assert result["read_docs_per_second"] == 40.0
        assert result["update_docs_per_second"] == 0.4
        assert result["delete_docs_per_second"] == 0.4
    assert payload["workload"]["insert_workers"] == 8
    assert payload["workload"]["workers"] == 8


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--docs", "0"], "--docs, --queries, and --repeats must all be positive"),
        (
            ["--queries", "0"],
            "--docs, --queries, and --repeats must all be positive",
        ),
        (
            ["--repeats", "0"],
            "--docs, --queries, and --repeats must all be positive",
        ),
        (["--sqlite-shards", "1"], "--sqlite-shards must be between 2 and 64"),
        (["--sqlite-shards", "65"], "--sqlite-shards must be between 2 and 64"),
        (["--insert-workers", "0"], "--insert-workers must be between 1 and 64"),
        (["--insert-workers", "65"], "--insert-workers must be between 1 and 64"),
        (
            ["--insert-workers", "3"],
            "--insert-workers must equal --sqlite-shards",
        ),
    ],
)
def test_cli_rejects_invalid_positive_counts_and_shard_bounds(
    benchmark,
    monkeypatch,
    capsys,
    arguments,
    message,
):
    def unexpected_run(*args, **kwargs):
        raise AssertionError("invalid CLI input reached the benchmark runner")

    monkeypatch.setattr(benchmark, "run_benchmark", unexpected_run)

    with pytest.raises(SystemExit) as exc_info:
        benchmark.main(arguments)

    assert exc_info.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_selects_backends_and_writes_json_without_external_services(
    benchmark,
    monkeypatch,
    tmp_path,
    capsys,
):
    output_path = tmp_path / "result.json"
    work_root = tmp_path / "work"
    captured = {}
    payload = {
        "environment": {"python": "test"},
        "workload": {
            "documents": 13,
            "queries": 3,
            "repeats": 2,
            "sqlite_shards": 4,
            "insert_workers": 4,
        },
        "results": [
            _sample_result(benchmark),
            _sample_result(benchmark, "raw-sqlite", 12.5),
        ],
    }

    def fake_run_benchmark(
        backends,
        documents,
        queries,
        repeats,
        work_root,
        sqlite_shards=4,
        insert_workers=4,
        mongo_uri=None,
    ):
        captured.update(
            {
                "backends": backends,
                "documents": documents,
                "queries": queries,
                "repeats": repeats,
                "work_root": work_root,
                "sqlite_shards": sqlite_shards,
                "insert_workers": insert_workers,
                "mongo_uri": mongo_uri,
            }
        )
        return payload

    monkeypatch.setattr(benchmark, "run_benchmark", fake_run_benchmark)

    return_code = benchmark.main(
        [
            "--backend",
            "memory",
            "--backend",
            "raw-sqlite",
            "--docs",
            "13",
            "--queries",
            "3",
            "--repeats",
            "2",
            "--sqlite-shards",
            "4",
            "--workers",
            "4",
            "--work-root",
            str(work_root),
            "--json-output",
            str(output_path),
        ]
    )

    assert return_code == 0
    assert captured == {
        "backends": ("memory", "raw-sqlite"),
        "documents": 13,
        "queries": 3,
        "repeats": 2,
        "work_root": str(work_root),
        "sqlite_shards": 4,
        "insert_workers": 4,
        "mongo_uri": None,
    }
    assert json.loads(output_path.read_text(encoding="utf-8")) == payload
    stdout = capsys.readouterr().out
    assert "TinyMongo Memory" in stdout
    assert "Raw SQLite (native SQL)" in stdout
