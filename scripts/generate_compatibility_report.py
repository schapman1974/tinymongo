#!/usr/bin/env python3
"""Generate deterministic compatibility reports from pytest JUnit XML."""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


SCHEMA_VERSION = 1
DEFAULT_TARGETS = ("memory", "json", "sqlite", "duckdb", "parquet", "mongodb")
REFERENCE_TARGET = "mongodb"
UNATTRIBUTED_TARGET = "unattributed"
OUTCOMES = ("passed", "xpassed", "xfailed", "failed", "error", "skipped")
COMPATIBLE_OUTCOMES = frozenset(("passed", "xpassed"))
EVALUATED_OUTCOMES = frozenset(("passed", "xpassed", "xfailed", "failed", "error"))
TARGET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*$")
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
    """One target-specific contract outcome read from JUnit."""

    target: str
    classname: str
    name: str
    contract: str
    outcome: str
    reason: Optional[str] = None

    @property
    def nodeid(self):
        """Return a stable test identifier without filesystem-specific paths."""

        if self.classname:
            return "{0}::{1}".format(self.classname, self.name)
        return self.name


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


def _target_from_name(test_name, targets):
    parameter_id = _parameter_id(test_name)
    if parameter_id is None:
        return UNATTRIBUTED_TARGET

    matches = [
        target
        for target in targets
        if re.search(r"(?:^|-){0}(?:-|$)".format(re.escape(target)), parameter_id)
    ]
    if len(matches) == 1:
        return matches[0]
    return UNATTRIBUTED_TARGET


def validate_targets(targets):
    """Return validated target names suitable for pytest parameter matching."""

    targets = tuple(targets)
    if not targets:
        raise ValueError("at least one target is required")
    if len(set(targets)) != len(targets):
        raise ValueError("target names must be unique")
    for target in targets:
        if target == UNATTRIBUTED_TARGET:
            raise ValueError("'unattributed' is reserved for unmatched test cases")
        if not TARGET_NAME_PATTERN.fullmatch(target):
            raise ValueError(
                "target names must use alphanumeric or underscore segments "
                "separated by single hyphens: {0!r}".format(target)
            )
    return targets


def read_junit(path, targets=DEFAULT_TARGETS):
    """Parse target-specific contract cases from a pytest JUnit XML file."""

    targets = validate_targets(targets)
    root = ET.parse(path).getroot()
    cases = []
    for testcase in root.iter():
        if _local_name(testcase.tag) != "testcase":
            continue
        name = testcase.get("name") or "unnamed-testcase"
        outcome, reason = _classify(testcase)
        cases.append(
            ContractCase(
                target=_target_from_name(name, targets),
                classname=testcase.get("classname") or "",
                name=name,
                contract=name.split("[", 1)[0],
                outcome=outcome,
                reason=reason,
            )
        )
    return sorted(cases, key=lambda case: (case.target, case.nodeid, case.outcome))


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
        "contract": case.contract,
        "name": case.nodeid,
        "outcome": case.outcome,
    }
    if case.reason:
        result["reason"] = case.reason
    return result


def build_report(cases, targets=DEFAULT_TARGETS, reference_target=REFERENCE_TARGET):
    """Build the versioned, deterministic compatibility-report data model."""

    targets = validate_targets(targets)
    if reference_target not in targets:
        raise ValueError("reference target must be one of the configured targets")
    ordered_targets = list(targets)
    if any(case.target == UNATTRIBUTED_TARGET for case in cases):
        ordered_targets.append(UNATTRIBUTED_TARGET)

    target_reports = []
    for target in ordered_targets:
        target_cases = sorted(
            (case for case in cases if case.target == target),
            key=lambda case: (case.nodeid, case.outcome),
        )
        role = "backend"
        if target == reference_target:
            role = "reference"
        elif target == UNATTRIBUTED_TARGET:
            role = "unattributed"
        target_reports.append(
            {
                "name": target,
                "role": role,
                "counts": _counts(target_cases),
                "score": _score(target_cases),
                "tests": [_case_dict(case) for case in target_cases],
            }
        )

    scored_cases = [
        case
        for case in cases
        if case.target not in (reference_target, UNATTRIBUTED_TARGET)
    ]
    known_gaps = [
        {
            "contract": case.contract,
            "name": case.nodeid,
            "reason": case.reason or "Expected failure",
            "target": case.target,
        }
        for case in cases
        if case.outcome == "xfailed"
    ]
    unexpected_passes = [
        {
            "contract": case.contract,
            "name": case.nodeid,
            "reason": case.reason or "Strict expected failure passed",
            "target": case.target,
        }
        for case in cases
        if case.outcome == "xpassed"
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "scoring": {
            "description": (
                "Passed and strict-xpass backend-contract cells divided by all "
                "evaluated backend-contract cells; expected failures count as "
                "known incompatibilities, ordinary skips are excluded, and the "
                "{0} reference target is excluded."
            ).format(reference_target),
            "formula": (
                "(passed + xpassed) / " "(passed + xpassed + xfailed + failed + error)"
            ),
            "reference_target": reference_target,
            "overall": _score(scored_cases),
        },
        "totals": _counts(cases),
        "targets": target_reports,
        "known_gaps": sorted(known_gaps, key=lambda gap: (gap["target"], gap["name"])),
        "unexpected_passes": sorted(
            unexpected_passes,
            key=lambda unexpected: (unexpected["target"], unexpected["name"]),
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


def render_markdown(report):
    """Render a concise human-readable report from the JSON data model."""

    overall = report["scoring"]["overall"]
    lines = [
        "# TinyMongo compatibility report",
        "",
        "This deterministic report summarizes the target-specific pytest contract "
        "results.",
        "",
        "## Compatibility score",
        "",
        "**{0}** ({1} compatible of {2} evaluated backend-contract cells)".format(
            _percentage(overall), overall["compatible"], overall["evaluated"]
        ),
        "",
        report["scoring"]["description"],
        "",
        "Formula: `{0}`".format(report["scoring"]["formula"]),
        "",
        "## Per-target outcomes",
        "",
        (
            "| Target | Role | Score | Evaluated | Passed | XPASS | Known gaps "
            "(xfail) | Failed | Errors | Skipped |"
        ),
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for target in report["targets"]:
        counts = target["counts"]
        lines.append(
            "| {name} | {role} | {score} | {evaluated} | {passed} | "
            "{xpassed} | {xfailed} | {failed} | {error} | {skipped} |".format(
                name=_markdown_cell(target["name"]),
                role=target["role"],
                score=_percentage(target["score"]),
                evaluated=target["score"]["evaluated"],
                **counts,
            )
        )

    lines.extend(["", "## Known compatibility gaps", ""])
    if report["known_gaps"]:
        lines.extend(
            [
                "| Target | Contract | Reason |",
                "| --- | --- | --- |",
            ]
        )
        for gap in report["known_gaps"]:
            lines.append(
                "| {0} | `{1}` | {2} |".format(
                    _markdown_cell(gap["target"]),
                    _markdown_cell(gap["contract"]),
                    _markdown_cell(gap["reason"]),
                )
            )
    else:
        lines.append("No expected compatibility gaps were reported.")

    problems = []
    for target in report["targets"]:
        for case in target["tests"]:
            if case["outcome"] in ("failed", "error"):
                problems.append((target["name"], case))
    lines.extend(["", "## Failures and errors", ""])
    if problems:
        lines.extend(
            ["| Target | Test | Outcome | Reason |", "| --- | --- | --- | --- |"]
        )
        for target, case in problems:
            lines.append(
                "| {0} | `{1}` | {2} | {3} |".format(
                    _markdown_cell(target),
                    _markdown_cell(case["name"]),
                    case["outcome"],
                    _markdown_cell(case.get("reason", "No reason recorded")),
                )
            )
    else:
        lines.append("No failures or errors were reported.")

    lines.extend(["", "## Unexpected strict-xfail passes", ""])
    if report["unexpected_passes"]:
        for unexpected in report["unexpected_passes"]:
            lines.append(
                "- `{0}` on **{1}**: {2}".format(
                    _markdown_cell(unexpected["contract"]),
                    _markdown_cell(unexpected["target"]),
                    _markdown_cell(unexpected["reason"]),
                )
            )
    else:
        lines.append("No strict expected failures passed unexpectedly.")

    return "\n".join(lines) + "\n"


def _parse_targets(value):
    targets = tuple(target.strip() for target in value.split(",") if target.strip())
    try:
        return validate_targets(targets)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _argument_parser():
    parser = argparse.ArgumentParser(
        description="Generate JSON and Markdown compatibility reports from pytest JUnit"
    )
    parser.add_argument("junit", type=Path, help="pytest JUnit XML input")
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
        "--targets",
        type=_parse_targets,
        default=DEFAULT_TARGETS,
        help="comma-separated pytest target IDs",
    )
    parser.add_argument(
        "--reference-target",
        default=REFERENCE_TARGET,
        help="target excluded as the reference (default: mongodb)",
    )
    return parser


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    try:
        cases = read_junit(args.junit, targets=args.targets)
        report = build_report(
            cases,
            targets=args.targets,
            reference_target=args.reference_target,
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
