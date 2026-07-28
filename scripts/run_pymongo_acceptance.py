#!/usr/bin/env python3
"""Run an application's pytest suite against PyMongo or patched TinyMongo."""

import argparse
import importlib
from contextlib import nullcontext
from pathlib import Path


BACKENDS = ("memory", "json", "sqlite", "duckdb", "parquet", "mongodb")
APIS = ("sync", "async")


class AcceptanceMetadataPlugin:
    """Attach report dimensions to every application test result."""

    def __init__(self, api, backend, suite):
        self.api = api
        self.backend = backend
        self.suite = suite

    def pytest_collection_modifyitems(self, session, config, items):
        properties = (
            ("tinymongo.api", self.api),
            ("tinymongo.backend", self.backend),
            ("tinymongo.suite", self.suite),
        )
        for item in items:
            existing = {name for name, _ in item.user_properties}
            item.user_properties.extend(
                (name, value) for name, value in properties if name not in existing
            )


def _argument_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run application tests with PyMongo client construction routed to "
            "TinyMongo, while recording compatibility-report metadata."
        )
    )
    parser.add_argument("--api", choices=APIS, required=True)
    parser.add_argument("--backend", choices=BACKENDS, required=True)
    parser.add_argument(
        "--folder",
        type=Path,
        help="TinyMongo data folder; defaults to isolated memory or ./tinydb",
    )
    parser.add_argument(
        "--suite",
        default="talkpython-app",
        help="report suite label (default: talkpython-app)",
    )
    parser.add_argument(
        "--junitxml",
        type=Path,
        help="JUnit output path (default: acceptance-API-BACKEND.xml)",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="pytest paths and options after --",
    )
    return parser


def _pytest_arguments(args):
    pytest_args = list(args.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args.pop(0)
    if not pytest_args:
        pytest_args.append(".")
    if not any(value.startswith("--junitxml") for value in pytest_args):
        output = args.junitxml or Path(
            "acceptance-{0}-{1}.xml".format(args.api, args.backend)
        )
        pytest_args.append("--junitxml={0}".format(output))
    return pytest_args


def _patch_context(args):
    if args.backend == "mongodb":
        return nullcontext()

    tinymongo = importlib.import_module("tinymongo")
    folder = str(args.folder) if args.folder is not None else None
    return tinymongo.patch(folder=folder, backend=args.backend)


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    try:
        pytest = importlib.import_module("pytest")
    except ImportError as error:
        raise ImportError(
            "The acceptance runner requires pytest. Install it with: "
            "pip install pytest"
        ) from error

    plugin = AcceptanceMetadataPlugin(args.api, args.backend, args.suite)
    with _patch_context(args):
        return int(pytest.main(_pytest_arguments(args), plugins=[plugin]))


if __name__ == "__main__":
    raise SystemExit(main())
