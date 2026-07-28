"""Tests for the external PyMongo application acceptance runner."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts import run_pymongo_acceptance


class _Item:
    def __init__(self, properties=()):
        self.user_properties = list(properties)


def test_metadata_plugin_adds_dimensions_without_replacing_existing_values():
    plugin = run_pymongo_acceptance.AcceptanceMetadataPlugin(
        "async", "sqlite", "talkpython-app"
    )
    first = _Item()
    second = _Item((("tinymongo.api", "custom"),))

    plugin.pytest_collection_modifyitems(None, None, [first, second])

    assert first.user_properties == [
        ("tinymongo.api", "async"),
        ("tinymongo.backend", "sqlite"),
        ("tinymongo.suite", "talkpython-app"),
    ]
    assert second.user_properties == [
        ("tinymongo.api", "custom"),
        ("tinymongo.backend", "sqlite"),
        ("tinymongo.suite", "talkpython-app"),
    ]


def test_main_patches_embedded_backend_and_adds_default_junit(monkeypatch, tmp_path):
    events = []
    calls = []

    @contextmanager
    def fake_patch(**kwargs):
        events.append(("enter", kwargs))
        yield
        events.append(("exit", kwargs))

    fake_pytest = SimpleNamespace(
        main=lambda args, plugins: calls.append((args, plugins)) or 0
    )

    def fake_import(name):
        if name == "pytest":
            return fake_pytest
        if name == "tinymongo":
            return SimpleNamespace(patch=fake_patch)
        raise AssertionError(name)

    monkeypatch.setattr(run_pymongo_acceptance.importlib, "import_module", fake_import)

    result = run_pymongo_acceptance.main(
        [
            "--api",
            "async",
            "--backend",
            "sqlite",
            "--folder",
            str(tmp_path),
            "--",
            "app_tests",
            "-q",
        ]
    )

    assert result == 0
    assert events == [
        ("enter", {"folder": str(tmp_path), "backend": "sqlite"}),
        ("exit", {"folder": str(tmp_path), "backend": "sqlite"}),
    ]
    assert calls[0][0] == [
        "app_tests",
        "-q",
        "--junitxml=acceptance-async-sqlite.xml",
    ]
    plugin = calls[0][1][0]
    assert (plugin.api, plugin.backend, plugin.suite) == (
        "async",
        "sqlite",
        "talkpython-app",
    )


def test_main_leaves_real_mongodb_unpatched_and_keeps_explicit_junit(monkeypatch):
    calls = []
    fake_pytest = SimpleNamespace(
        main=lambda args, plugins: calls.append((args, plugins)) or 5
    )

    def fake_import(name):
        if name == "pytest":
            return fake_pytest
        raise AssertionError("MongoDB reference run must not import {0}".format(name))

    monkeypatch.setattr(run_pymongo_acceptance.importlib, "import_module", fake_import)

    result = run_pymongo_acceptance.main(
        [
            "--api",
            "sync",
            "--backend",
            "mongodb",
            "--suite",
            "external-app",
            "--junitxml",
            "reference.xml",
        ]
    )

    assert result == 5
    assert calls[0][0] == [".", "--junitxml=reference.xml"]
    plugin = calls[0][1][0]
    assert (plugin.api, plugin.backend, plugin.suite) == (
        "sync",
        "mongodb",
        "external-app",
    )


def test_main_reports_missing_pytest(monkeypatch):
    def missing_pytest(name):
        assert name == "pytest"
        raise ImportError("missing")

    monkeypatch.setattr(
        run_pymongo_acceptance.importlib, "import_module", missing_pytest
    )

    with pytest.raises(ImportError, match="pip install pytest"):
        run_pymongo_acceptance.main(["--api", "async", "--backend", "memory"])
