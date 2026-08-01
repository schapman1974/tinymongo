"""Warning locations stay attached to application calls across async workers."""

import asyncio
from datetime import date
import inspect
from pathlib import Path
from types import SimpleNamespace
import warnings

import pytest

import tinymongo.warning_context as warning_context
from tinymongo.aggregation import AggregationEngine
from tinymongo.asyncio import AsyncTinyMongoClient
from tinymongo.indexes import TinyMongoUnsupportedWarning
from tinymongo.tinymongo import TinyMongoClient, TinyMongoCollection, TinyMongoCursor


def run(coroutine):
    return asyncio.run(coroutine)


def _assert_origin(caught, expected_line):
    assert len(caught) == 1
    assert Path(caught[0].filename).resolve() == Path(__file__).resolve()
    assert caught[0].lineno == expected_line


def test_sync_cursor_sort_warning_points_to_application_call():
    cursor = TinyMongoCursor([{"published": date(2026, 1, 1)}])

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", TinyMongoUnsupportedWarning)
        expected_line = inspect.currentframe().f_lineno + 1
        cursor.sort("published")

    _assert_origin(caught, expected_line)


def test_sync_aggregation_sort_warning_points_to_application_call():
    engine = AggregationEngine()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", TinyMongoUnsupportedWarning)
        expected_line = inspect.currentframe().f_lineno + 1
        engine.run(
            [{"published": date(2026, 1, 1)}],
            [{"$sort": {"published": 1}}],
        )

    _assert_origin(caught, expected_line)


def test_sync_create_indexes_warning_keeps_application_call_origin():
    client = TinyMongoClient(
        "memory://warning-origin-sync-create-indexes",
        backend="memory",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", TinyMongoUnsupportedWarning)
        expected_line = inspect.currentframe().f_lineno + 1
        client.db.items.create_indexes([{"key": {"created": -1}}])

    client.close()
    _assert_origin(caught, expected_line)


def test_async_create_indexes_warning_keeps_await_call_origin():
    async def scenario():
        client = AsyncTinyMongoClient(
            "memory://warning-origin-create-indexes",
            backend="memory",
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", TinyMongoUnsupportedWarning)
            expected_line = inspect.currentframe().f_lineno + 1
            await client.db.items.create_indexes([{"key": {"created": -1}}])
        await client.close()
        _assert_origin(caught, expected_line)

    run(scenario())


def test_async_find_cursor_sort_warning_keeps_sort_origin_through_clone(monkeypatch):
    def find_with_unsupported_value(_collection, *args, **kwargs):
        return TinyMongoCursor([{"published": date(2026, 1, 1)}])

    monkeypatch.setattr(TinyMongoCollection, "find", find_with_unsupported_value)

    async def scenario():
        client = AsyncTinyMongoClient(
            "memory://warning-origin-find-sort",
            backend="memory",
        )
        cursor = client.db.items.find({})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", TinyMongoUnsupportedWarning)
            expected_line = inspect.currentframe().f_lineno + 1
            cursor.sort("published")
            clone = cursor.clone()
            await clone.to_list()
        await client.close()
        _assert_origin(caught, expected_line)

    run(scenario())


def test_async_aggregation_sort_warning_keeps_await_call_origin(monkeypatch):
    def aggregate_with_unsupported_value(_collection, pipeline, *args, **kwargs):
        documents = [{"published": date(2026, 1, 1)}]
        return TinyMongoCursor(AggregationEngine().run(documents, pipeline))

    monkeypatch.setattr(
        TinyMongoCollection,
        "aggregate",
        aggregate_with_unsupported_value,
    )

    async def scenario():
        client = AsyncTinyMongoClient(
            "memory://warning-origin-aggregation-sort",
            backend="memory",
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", TinyMongoUnsupportedWarning)
            expected_line = inspect.currentframe().f_lineno + 1
            await client.db.items.aggregate([{"$sort": {"published": 1}}])
        await client.close()
        _assert_origin(caught, expected_line)

    run(scenario())


def test_warning_context_defensive_and_module_fallbacks(monkeypatch):
    with warning_context.use_warning_origin(None):
        pass

    namespace = {"capture_warning_origin": warning_context.capture_warning_origin}
    exec("origin = capture_warning_origin()", namespace)
    moduleless_origin = namespace["origin"]
    assert moduleless_origin.module is None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", TinyMongoUnsupportedWarning)
        with warning_context.use_warning_origin(moduleless_origin):
            warning_context.emit_warning(
                "moduleless warning",
                TinyMongoUnsupportedWarning,
            )
        with warning_context.use_warning_origin(
            warning_context.WarningOrigin(
                filename=__file__,
                lineno=1,
                module="tinymongo.warning_origin_module_not_loaded",
            )
        ):
            warning_context.emit_warning(
                "unloaded module warning",
                TinyMongoUnsupportedWarning,
            )
    assert len(caught) == 2

    dotted_frame = SimpleNamespace(
        f_globals={"__name__": "tinymongo.synthetic"},
        f_back=None,
    )
    package_frame = SimpleNamespace(
        f_globals={"__name__": "tinymongo"},
        f_back=dotted_frame,
    )
    current_frame = SimpleNamespace(f_back=package_frame)
    monkeypatch.setattr(
        warning_context.inspect,
        "currentframe",
        lambda: current_frame,
    )
    assert warning_context.capture_warning_origin() is None

    monkeypatch.setattr(warning_context, "capture_warning_origin", lambda: None)
    with pytest.warns(TinyMongoUnsupportedWarning, match="fallback warning"):
        warning_context.emit_warning(
            "fallback warning",
            TinyMongoUnsupportedWarning,
        )
