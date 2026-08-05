import importlib.util
from pathlib import Path


def _benchmark_module():
    path = Path(__file__).parent / "benchmarks" / "bench_sqlite_comparison.py"
    spec = importlib.util.spec_from_file_location("bench_sqlite_comparison", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_comparison_markdown_includes_point_update_schema():
    benchmark = _benchmark_module()
    available = benchmark._result(
        "tinymongo-sqlite",
        10,
        2,
        1.0,
        [0.001, 0.003],
        [0.004, 0.006],
        0.5,
        4,
        0.25,
        2,
    )
    unavailable = {
        "engine": "mongodb",
        "available": False,
        "reason": "not running",
    }

    markdown = benchmark.format_markdown([available, unavailable])
    lines = markdown.splitlines()

    assert "Point update avg ms" in lines[0]
    assert "Point update p95 ms" in lines[0]
    assert lines[2].count("|") == lines[0].count("|")
    assert "| 5.000 | 6.000 |" in lines[2]
    assert lines[3].count("unavailable") == 9
    assert lines[3].count("|") == lines[0].count("|")
