#!/usr/bin/env python3
"""Generate deterministic compatibility reports from pytest JUnit XML."""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Optional


SCHEMA_VERSION = 2
DEFAULT_APIS = ("sync", "async")
DEFAULT_BACKENDS = ("memory", "json", "sqlite", "duckdb", "parquet", "mongodb")
DEFAULT_TARGETS = tuple(
    "{0}-{1}".format(api, backend)
    for api, backend in product(DEFAULT_APIS, DEFAULT_BACKENDS)
)
REFERENCE_BACKEND = "mongodb"
REFERENCE_TARGET = REFERENCE_BACKEND
UNATTRIBUTED = "unattributed"
UNATTRIBUTED_TARGET = UNATTRIBUTED
OUTCOMES = ("passed", "xpassed", "xfailed", "failed", "error", "skipped")
COMPATIBLE_OUTCOMES = frozenset(("passed", "xpassed"))
EVALUATED_OUTCOMES = frozenset(("passed", "xpassed", "xfailed", "failed", "error"))
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*$")
STRICT_XPASS_TYPES = frozenset(("xpass", "pytest.xpass"))
ABSOLUTE_PATH_TOKEN = "<ABSOLUTE_PATH>"
QUOTED_ABSOLUTE_PATH_PATTERN = re.compile(
    r"""(?P<quote>["'])(?:[A-Za-z]:[\\/]|\\\\|/(?!/))[^"']*(?P=quote)"""
)
WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(
    r"""(?<![A-Za-z0-9_\\/])(?:[A-Za-z]:[\\/]|\\\\)[^\s"'<>()[\]{},;]+"""
)
UNIX_ABSOLUTE_PATH_PATTERN = re.compile(
    r"""(?<![A-Za-z0-9_:/\\])/(?!/)[^\s"'<>()[\]{},;]+"""
)


@dataclass(frozen=True)
class ContractCase:
    """One API/backend contract outcome read from JUnit."""

    api: str
    backend: str
    suite: str
    classname: str
    name: str
    contract: str
    outcome: str
    reason: Optional[str] = None

    @property
    def target(self):
        """Return the backend name retained for callers of schema version 1."""

        return self.backend

    @property
    def nodeid(self):
        """Return a stable test identifier without filesystem-specific paths."""

        if self.classname:
            return "{0}::{1}".format(self.classname, self.name)
        return self.name

    @property
    def contract_id(self):
        """Return the contract identity shared by all API/backend executions."""

        if self.classname:
            return "{0}::{1}".format(self.classname, self.contract)
        return self.contract


def _local_name(tag):
    """Return an XML tag without an optional namespace."""

    return tag.rsplit("}", 1)[-1]


def _normalize_message(value):
    if not value:
        return None
    message = " ".join(value.split())
    message = QUOTED_ABSOLUTE_PATH_PATTERN.sub(
        lambda match: "{0}{1}{0}".format(match.group("quote"), ABSOLUTE_PATH_TOKEN),
        message,
    )
    message = WINDOWS_ABSOLUTE_PATH_PATTERN.sub(ABSOLUTE_PATH_TOKEN, message)
    return UNIX_ABSOLUTE_PATH_PATTERN.sub(ABSOLUTE_PATH_TOKEN, message)


def _result_child(testcase):
    for child in testcase:
        if _local_name(child.tag) in ("failure", "error", "skipped"):
            return child
    return None


def _result_message(result):
    if result is None:
        return None
    message = _normalize_message(result.get("message"))
    if message:
        return message
    for line in (result.text or "").splitlines():
        message = _normalize_message(line)
        if message:
            return message
    return None


def _classify(testcase):
    result = _result_child(testcase)
    if result is None:
        return "passed", None

    tag = _local_name(result.tag)
    result_type = (result.get("type") or "").lower()
    message = _result_message(result)

    if tag == "skipped":
        if "xfail" in result_type:
            return "xfailed", message
        return "skipped", message
    if tag == "error":
        return "error", message
    if tag == "failure":
        if result_type in STRICT_XPASS_TYPES or (
            message and message.startswith("[XPASS(strict)]")
        ):
            return "xpassed", message
        return "failed", message
    raise AssertionError("unreachable JUnit result type")


def _parameter_id(test_name):
    match = re.search(r"\[([^][]*)\]$", test_name)
    if match:
        return match.group(1)
    return None


def _value_from_name(test_name, values):
    parameter_id = _parameter_id(test_name)
    if parameter_id is None:
        return UNATTRIBUTED

    matches = [
        value
        for value in values
        if re.search(r"(?:^|-){0}(?:-|$)".format(re.escape(value)), parameter_id)
    ]
    if len(matches) == 1:
        return matches[0]
    return UNATTRIBUTED


def _properties(testcase):
    properties = defaultdict(list)
    for element in testcase.iter():
        if _local_name(element.tag) != "property":
            continue
        name = element.get("name")
        if not name:
            continue
        value = element.get("value")
        if value is None:
            value = element.text or ""
        properties[name].append(value.strip())
    return properties


def _property_value(properties, name, allowed=None):
    if name not in properties:
        return None
    values = set(properties[name])
    if len(values) != 1:
        return UNATTRIBUTED
    value = values.pop()
    if not value or not NAME_PATTERN.fullmatch(value):
        return UNATTRIBUTED
    if allowed is not None and value not in allowed:
        return UNATTRIBUTED
    return value


def _suite_from_name(classname, test_name):
    parameter_id = _parameter_id(test_name) or ""
    match = re.search(
        r"(?:^|-)suite[_=]([A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*)", parameter_id
    )
    if match:
        return match.group(1)

    normalized = classname.replace("\\", "/")
    leaf = normalized.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    match = re.search(r"(?:^|\.?)test_([A-Za-z0-9_]+)_contract$", leaf)
    if match and match.group(1) == "talkpython":
        return "talkpython"
    if "contracts" in normalized:
        return "core"
    if match:
        return match.group(1)
    return UNATTRIBUTED


def _remove_subsequence(parts, subsequence):
    if not subsequence:
        return parts
    for index in range(len(parts) - len(subsequence) + 1):
        if parts[index : index + len(subsequence)] == subsequence:
            return parts[:index] + parts[index + len(subsequence) :]
    return parts


def _contract_from_name(name, api, backend):
    base = name.split("[", 1)[0]
    parameter_id = _parameter_id(name)
    if not parameter_id or UNATTRIBUTED in (api, backend):
        return base

    parts = parameter_id.split("-")
    for sequence in (
        [api] + backend.split("-"),
        backend.split("-") + [api],
        [api],
        backend.split("-"),
    ):
        updated = _remove_subsequence(parts, sequence)
        if updated != parts:
            parts = updated
            if len(sequence) > 1:
                break
    if parts:
        return "{0}[{1}]".format(base, "-".join(parts))
    return base


def _validate_names(values, label):
    values = tuple(values)
    if not values:
        raise ValueError("at least one {0} is required".format(label))
    if len(set(values)) != len(values):
        raise ValueError("{0} names must be unique".format(label))
    for value in values:
        if value == UNATTRIBUTED:
            raise ValueError(
                "'unattributed' is reserved for unmatched {0} values".format(label)
            )
        if not NAME_PATTERN.fullmatch(value):
            raise ValueError(
                "{0} names must use alphanumeric or underscore segments "
                "separated by single hyphens: {1!r}".format(label, value)
            )
    return values


def validate_apis(apis):
    """Return validated API names suitable for pytest parameter matching."""

    return _validate_names(apis, "API")


def validate_backends(backends):
    """Return validated backend names suitable for pytest parameter matching."""

    return _validate_names(backends, "backend")


def validate_targets(targets):
    """Compatibility alias for validating schema-version-1 backend targets."""

    return validate_backends(targets)


def _input_paths(paths):
    if isinstance(paths, (str, Path)):
        return (Path(paths),)
    paths = tuple(Path(path) for path in paths)
    if not paths:
        raise ValueError("at least one JUnit input is required")
    return paths


def read_junit(
    paths,
    apis=DEFAULT_APIS,
    backends=DEFAULT_BACKENDS,
    targets=None,
):
    """Parse contract cases from one or more pytest JUnit XML files."""

    if targets is not None:
        backends = targets
    apis = validate_apis(apis)
    backends = validate_backends(backends)
    cases = []
    for path in _input_paths(paths):
        root = ET.parse(path).getroot()
        for testcase in root.iter():
            if _local_name(testcase.tag) != "testcase":
                continue
            name = testcase.get("name") or "unnamed-testcase"
            classname = testcase.get("classname") or ""
            properties = _properties(testcase)

            api = _property_value(properties, "tinymongo.api", apis)
            if api is None:
                api = _value_from_name(name, apis)
            backend = _property_value(properties, "tinymongo.backend", backends)
            if backend is None:
                backend = _value_from_name(name, backends)
            suite = _property_value(properties, "tinymongo.suite")
            if suite is None:
                suite = _suite_from_name(classname, name)

            outcome, reason = _classify(testcase)
            cases.append(
                ContractCase(
                    api=api,
                    backend=backend,
                    suite=suite,
                    classname=classname,
                    name=name,
                    contract=_contract_from_name(name, api, backend),
                    outcome=outcome,
                    reason=reason,
                )
            )
    return sorted(cases, key=_case_sort_key)


def _case_sort_key(case):
    return (
        case.api,
        case.backend,
        case.suite,
        case.contract_id,
        case.nodeid,
        case.outcome,
        case.reason or "",
    )


def _counts(cases):
    observed = Counter(case.outcome for case in cases)
    return {outcome: observed.get(outcome, 0) for outcome in OUTCOMES}


def _score(cases):
    compatible = sum(case.outcome in COMPATIBLE_OUTCOMES for case in cases)
    evaluated = sum(case.outcome in EVALUATED_OUTCOMES for case in cases)
    percentage = None
    if evaluated:
        percentage = round(compatible * 100.0 / evaluated, 2)
    return {
        "compatible": compatible,
        "evaluated": evaluated,
        "percentage": percentage,
    }


def _case_dict(case):
    result = {
        "api": case.api,
        "backend": case.backend,
        "suite": case.suite,
        "contract": case.contract,
        "contract_id": case.contract_id,
        "name": case.nodeid,
        "outcome": case.outcome,
    }
    if case.reason:
        result["reason"] = case.reason
    return result


def _cell_key(case):
    return (case.api, case.backend, case.suite, case.classname, case.contract)


def _reference_key(case):
    return (case.api, case.suite, case.classname, case.contract)


def _contract_key(case):
    return (case.suite, case.classname, case.contract)


def _cell_dict(key):
    api, backend, suite, classname, contract = key
    contract_id = contract
    if classname:
        contract_id = "{0}::{1}".format(classname, contract)
    return {
        "api": api,
        "backend": backend,
        "suite": suite,
        "contract": contract,
        "contract_id": contract_id,
    }


def _summary(
    name,
    cases,
    scored_cases,
    expected_keys,
    observed_keys,
    missing_keys,
    skipped_keys,
    duplicate_keys,
):
    return {
        "name": name,
        "counts": _counts(cases),
        "score": _score(scored_cases),
        "expected_cells": len(expected_keys),
        "observed_cells": len(observed_keys),
        "missing_cells": len(missing_keys),
        "skipped_cells": len(skipped_keys),
        "duplicate_cells": len(duplicate_keys),
    }


def _matches_dimension(key, dimension, value):
    indexes = {"api": 0, "backend": 1, "suite": 2}
    return key[indexes[dimension]] == value


def _dimension_summaries(
    dimension,
    values,
    cases,
    scored_cases,
    expected_keys,
    observed_keys,
    missing_keys,
    skipped_keys,
    duplicate_keys,
):
    summaries = []
    for value in values:
        summaries.append(
            _summary(
                value,
                [case for case in cases if getattr(case, dimension) == value],
                [case for case in scored_cases if getattr(case, dimension) == value],
                [
                    key
                    for key in expected_keys
                    if _matches_dimension(key, dimension, value)
                ],
                [
                    key
                    for key in observed_keys
                    if _matches_dimension(key, dimension, value)
                ],
                [
                    key
                    for key in missing_keys
                    if _matches_dimension(key, dimension, value)
                ],
                [
                    key
                    for key in skipped_keys
                    if _matches_dimension(key, dimension, value)
                ],
                [
                    key
                    for key in duplicate_keys
                    if _matches_dimension(key, dimension, value)
                ],
            )
        )
    return summaries


def _blockers(
    missing_targets,
    missing_keys,
    skipped_cases,
    unattributed_cases,
    duplicate_keys,
    unqualified_references,
    has_contracts,
):
    blockers = []
    if not has_contracts:
        blockers.append("no-contracts")
    if missing_targets:
        blockers.append("missing-targets")
    if missing_keys:
        blockers.append("missing-cells")
    if skipped_cases:
        blockers.append("skipped-cells")
    if unattributed_cases:
        blockers.append("unattributed-cases")
    if duplicate_keys:
        blockers.append("duplicate-cells")
    if unqualified_references:
        blockers.append("unqualified-mongodb-references")
    return blockers


def build_report(
    cases,
    apis=DEFAULT_APIS,
    backends=DEFAULT_BACKENDS,
    reference_backend=REFERENCE_BACKEND,
    targets=None,
    reference_target=None,
):
    """Build the versioned, deterministic compatibility-report data model."""

    if targets is not None:
        backends = targets
    if reference_target is not None:
        reference_backend = reference_target
    apis = validate_apis(apis)
    backends = validate_backends(backends)
    if reference_backend not in backends:
        raise ValueError("reference backend must be one of the configured backends")

    cases = sorted(cases, key=_case_sort_key)
    attributed_cases = [
        case
        for case in cases
        if case.api in apis and case.backend in backends and case.suite != UNATTRIBUTED
    ]
    unattributed_cases = [case for case in cases if case not in attributed_cases]

    cells = defaultdict(list)
    for case in attributed_cases:
        cells[_cell_key(case)].append(case)
    observed_keys = set(cells)
    duplicate_keys = {key for key, cell_cases in cells.items() if len(cell_cases) > 1}

    contract_keys = sorted({_contract_key(case) for case in attributed_cases})
    expected_keys = {
        (api, backend, suite, classname, contract)
        for api, backend in product(apis, backends)
        for suite, classname, contract in contract_keys
    }
    missing_keys = expected_keys - observed_keys
    skipped_cases = [case for case in attributed_cases if case.outcome == "skipped"]
    skipped_keys = {_cell_key(case) for case in skipped_cases}

    expected_targets = set(product(apis, backends))
    observed_targets = {(case.api, case.backend) for case in attributed_cases}
    missing_targets = sorted(expected_targets - observed_targets)

    passed_references = set()
    unqualified_references = []
    for api in apis:
        for suite, classname, contract in contract_keys:
            key = (api, reference_backend, suite, classname, contract)
            cell_cases = cells.get(key, ())
            if len(cell_cases) == 1 and cell_cases[0].outcome == "passed":
                passed_references.add((api, suite, classname, contract))
                continue
            reference = _cell_dict(key)
            reference["outcomes"] = sorted(case.outcome for case in cell_cases)
            unqualified_references.append(reference)

    scored_cases = []
    excluded_cases = []
    for key in sorted(observed_keys):
        cell_cases = cells[key]
        case = cell_cases[0]
        if case.backend == reference_backend or case.outcome not in EVALUATED_OUTCOMES:
            continue
        if key in duplicate_keys:
            excluded = _case_dict(case)
            excluded["reason"] = "duplicate-cell"
            excluded_cases.append(excluded)
        elif _reference_key(case) not in passed_references:
            excluded = _case_dict(case)
            excluded["reason"] = "mongodb-reference-not-passed"
            excluded_cases.append(excluded)
        else:
            scored_cases.append(case)

    complete = bool(contract_keys) and not any(
        (
            missing_targets,
            missing_keys,
            skipped_cases,
            unattributed_cases,
            duplicate_keys,
        )
    )
    publishable = complete and not unqualified_references
    blockers = _blockers(
        missing_targets,
        missing_keys,
        skipped_cases,
        unattributed_cases,
        duplicate_keys,
        unqualified_references,
        bool(contract_keys),
    )

    target_reports = []
    for api, backend in product(apis, backends):
        target_cases = [
            case
            for case in attributed_cases
            if case.api == api and case.backend == backend
        ]
        target_scored = [
            case for case in scored_cases if case.api == api and case.backend == backend
        ]
        target_expected = {
            key for key in expected_keys if key[0] == api and key[1] == backend
        }
        target_observed = {
            key for key in observed_keys if key[0] == api and key[1] == backend
        }
        target_report = _summary(
            "{0}-{1}".format(api, backend),
            target_cases,
            target_scored,
            target_expected,
            target_observed,
            target_expected - target_observed,
            {key for key in skipped_keys if key in target_expected},
            {key for key in duplicate_keys if key in target_expected},
        )
        target_report.update(
            {
                "api": api,
                "backend": backend,
                "role": "reference" if backend == reference_backend else "backend",
                "tests": [_case_dict(case) for case in target_cases],
            }
        )
        target_reports.append(target_report)

    suites = sorted({case.suite for case in attributed_cases})
    summaries = {
        "apis": _dimension_summaries(
            "api",
            apis,
            attributed_cases,
            scored_cases,
            expected_keys,
            observed_keys,
            missing_keys,
            skipped_keys,
            duplicate_keys,
        ),
        "backends": _dimension_summaries(
            "backend",
            backends,
            attributed_cases,
            scored_cases,
            expected_keys,
            observed_keys,
            missing_keys,
            skipped_keys,
            duplicate_keys,
        ),
        "suites": _dimension_summaries(
            "suite",
            suites,
            attributed_cases,
            scored_cases,
            expected_keys,
            observed_keys,
            missing_keys,
            skipped_keys,
            duplicate_keys,
        ),
    }

    known_gaps = [_case_dict(case) for case in cases if case.outcome == "xfailed"]
    unexpected_passes = [
        _case_dict(case) for case in cases if case.outcome == "xpassed"
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "baseline": {
            "status": (
                "publishable"
                if publishable
                else "complete-unpublishable" if complete else "incomplete"
            ),
            "complete": complete,
            "publishable": publishable,
            "blockers": blockers,
            "expected_targets": (
                list(DEFAULT_TARGETS)
                if apis == DEFAULT_APIS and backends == DEFAULT_BACKENDS
                else [
                    "{0}-{1}".format(api, backend)
                    for api, backend in product(apis, backends)
                ]
            ),
            "observed_targets": [
                "{0}-{1}".format(api, backend)
                for api, backend in sorted(observed_targets)
            ],
            "missing_targets": [
                {"api": api, "backend": backend} for api, backend in missing_targets
            ],
            "expected_cells": len(expected_keys),
            "observed_cells": len(observed_keys),
            "missing_cells": [_cell_dict(key) for key in sorted(missing_keys)],
            "skipped_cells": [
                _case_dict(case) for case in sorted(skipped_cases, key=_case_sort_key)
            ],
            "unattributed_cases": [
                _case_dict(case)
                for case in sorted(unattributed_cases, key=_case_sort_key)
            ],
            "duplicate_cells": [
                dict(
                    _cell_dict(key),
                    count=len(cells[key]),
                    outcomes=sorted(case.outcome for case in cells[key]),
                    tests=sorted(case.nodeid for case in cells[key]),
                )
                for key in sorted(duplicate_keys)
            ],
            "unqualified_references": sorted(
                unqualified_references,
                key=lambda reference: (
                    reference["api"],
                    reference["suite"],
                    reference["contract_id"],
                ),
            ),
        },
        "scoring": {
            "description": (
                "Only unique, evaluated non-MongoDB cells whose matching same-API "
                "MongoDB reference contract passed are scored. Expected failures "
                "are evaluated incompatibilities; ordinary skips are excluded."
            ),
            "formula": (
                "(passed + xpassed) / (passed + xpassed + xfailed + failed + error)"
            ),
            "reference_backend": reference_backend,
            "overall": _score(scored_cases),
            "excluded": sorted(
                excluded_cases,
                key=lambda case: (
                    case["api"],
                    case["backend"],
                    case["suite"],
                    case["contract_id"],
                ),
            ),
        },
        "totals": _counts(cases),
        "targets": target_reports,
        "summaries": summaries,
        "known_gaps": sorted(
            known_gaps,
            key=lambda gap: (
                gap["api"],
                gap["backend"],
                gap["suite"],
                gap["name"],
            ),
        ),
        "unexpected_passes": sorted(
            unexpected_passes,
            key=lambda unexpected: (
                unexpected["api"],
                unexpected["backend"],
                unexpected["suite"],
                unexpected["name"],
            ),
        ),
    }


def render_json(report):
    """Render canonical JSON suitable for diffs and downstream tools."""

    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _markdown_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def _percentage(score):
    value = score["percentage"]
    if value is None:
        return "not evaluated"
    return "{0:.2f}%".format(value)


def _summary_table(lines, heading, label, summaries):
    lines.extend(
        [
            "",
            "## {0}".format(heading),
            "",
            (
                "| {0} | Score | Evaluated | Expected | Observed | Missing | "
                "Skipped | Duplicates |"
            ).format(label),
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for summary in summaries:
        lines.append(
            "| {name} | {score} | {evaluated} | {expected} | {observed} | "
            "{missing} | {skipped} | {duplicates} |".format(
                name=_markdown_cell(summary["name"]),
                score=_percentage(summary["score"]),
                evaluated=summary["score"]["evaluated"],
                expected=summary["expected_cells"],
                observed=summary["observed_cells"],
                missing=summary["missing_cells"],
                skipped=summary["skipped_cells"],
                duplicates=summary["duplicate_cells"],
            )
        )


def render_markdown(report):
    """Render a concise human-readable report from the JSON data model."""

    baseline = report["baseline"]
    overall = report["scoring"]["overall"]
    blockers = ", ".join(baseline["blockers"]) or "none"
    lines = [
        "# TinyMongo compatibility report",
        "",
        "Baseline status: **{0}**".format(baseline["status"]),
        "",
        "- Complete matrix: **{0}**".format("yes" if baseline["complete"] else "no"),
        "- Publishable baseline: **{0}**".format(
            "yes" if baseline["publishable"] else "no"
        ),
        "- Blockers: {0}".format(_markdown_cell(blockers)),
        "",
        "## Compatibility score",
        "",
        "**{0}** ({1} compatible of {2} reference-qualified cells)".format(
            _percentage(overall), overall["compatible"], overall["evaluated"]
        ),
        "",
        report["scoring"]["description"],
        "",
        "Formula: `{0}`".format(report["scoring"]["formula"]),
    ]

    _summary_table(lines, "Per-API summary", "API", report["summaries"]["apis"])
    _summary_table(
        lines,
        "Per-backend summary",
        "Backend",
        report["summaries"]["backends"],
    )
    _summary_table(
        lines,
        "Per-suite summary",
        "Suite",
        report["summaries"]["suites"],
    )

    lines.extend(["", "## Matrix integrity", ""])
    lines.append(
        "{0} of {1} expected cells were observed.".format(
            baseline["observed_cells"], baseline["expected_cells"]
        )
    )
    flag_groups = (
        ("Missing cells", baseline["missing_cells"]),
        ("Skipped cells", baseline["skipped_cells"]),
        ("Unattributed cases", baseline["unattributed_cases"]),
        ("Duplicate cells", baseline["duplicate_cells"]),
        ("Unqualified MongoDB references", baseline["unqualified_references"]),
    )
    for label, values in flag_groups:
        lines.append("- {0}: **{1}**".format(label, len(values)))

    lines.extend(["", "## Known compatibility gaps", ""])
    if report["known_gaps"]:
        lines.extend(
            [
                "| API | Backend | Suite | Contract | Reason |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for gap in report["known_gaps"]:
            lines.append(
                "| {api} | {backend} | {suite} | `{contract}` | {reason} |".format(
                    api=_markdown_cell(gap["api"]),
                    backend=_markdown_cell(gap["backend"]),
                    suite=_markdown_cell(gap["suite"]),
                    contract=_markdown_cell(gap["contract"]),
                    reason=_markdown_cell(gap.get("reason", "Expected failure")),
                )
            )
    else:
        lines.append("No expected compatibility gaps were reported.")

    problems = []
    for target in report["targets"]:
        for case in target["tests"]:
            if case["outcome"] in ("failed", "error"):
                problems.append(case)
    problems.extend(
        case
        for case in baseline["unattributed_cases"]
        if case["outcome"] in ("failed", "error")
    )
    lines.extend(["", "## Failures and errors", ""])
    if problems:
        lines.extend(
            [
                "| API | Backend | Suite | Test | Outcome | Reason |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for case in sorted(
            problems,
            key=lambda item: (
                item["api"],
                item["backend"],
                item["suite"],
                item["name"],
            ),
        ):
            lines.append(
                "| {api} | {backend} | {suite} | `{name}` | {outcome} | "
                "{reason} |".format(
                    api=_markdown_cell(case["api"]),
                    backend=_markdown_cell(case["backend"]),
                    suite=_markdown_cell(case["suite"]),
                    name=_markdown_cell(case["name"]),
                    outcome=case["outcome"],
                    reason=_markdown_cell(case.get("reason", "No reason recorded")),
                )
            )
    else:
        lines.append("No failures or errors were reported.")

    lines.extend(["", "## Unexpected strict-xfail passes", ""])
    if report["unexpected_passes"]:
        for unexpected in report["unexpected_passes"]:
            lines.append(
                "- `{0}` on **{1}-{2}** ({3}): {4}".format(
                    _markdown_cell(unexpected["contract"]),
                    _markdown_cell(unexpected["api"]),
                    _markdown_cell(unexpected["backend"]),
                    _markdown_cell(unexpected["suite"]),
                    _markdown_cell(
                        unexpected.get("reason", "Strict expected failure passed")
                    ),
                )
            )
    else:
        lines.append("No strict expected failures passed unexpectedly.")

    return "\n".join(lines) + "\n"


def _parse_names(value, label):
    names = tuple(name.strip() for name in value.split(",") if name.strip())
    try:
        return _validate_names(names, label)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _parse_apis(value):
    return _parse_names(value, "API")


def _parse_backends(value):
    return _parse_names(value, "backend")


def _argument_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Generate JSON and Markdown compatibility reports from pytest JUnit"
        )
    )
    parser.add_argument(
        "junit",
        nargs="+",
        type=Path,
        help="one or more pytest JUnit XML inputs",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("compatibility-report.json"),
        help="JSON output path (default: compatibility-report.json)",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=Path("compatibility-report.md"),
        help="Markdown output path (default: compatibility-report.md)",
    )
    parser.add_argument(
        "--apis",
        type=_parse_apis,
        default=DEFAULT_APIS,
        help="comma-separated API IDs (default: sync,async)",
    )
    parser.add_argument(
        "--backends",
        "--targets",
        dest="backends",
        type=_parse_backends,
        default=DEFAULT_BACKENDS,
        help="comma-separated backend IDs",
    )
    parser.add_argument(
        "--reference-backend",
        "--reference-target",
        dest="reference_backend",
        default=REFERENCE_BACKEND,
        help="reference backend used to qualify scoring (default: mongodb)",
    )
    return parser


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    try:
        cases = read_junit(
            args.junit,
            apis=args.apis,
            backends=args.backends,
        )
        report = build_report(
            cases,
            apis=args.apis,
            backends=args.backends,
            reference_backend=args.reference_backend,
        )
        _write(args.json_output, render_json(report))
        _write(args.markdown_output, render_markdown(report))
    except (ET.ParseError, OSError, ValueError) as error:
        print(
            "Could not generate compatibility report: {0}".format(error),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
