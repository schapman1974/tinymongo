"""Tests for deterministic JUnit compatibility reporting."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "generate_compatibility_report.py"
SPEC = importlib.util.spec_from_file_location("compatibility_report", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
compatibility_report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compatibility_report
SPEC.loader.exec_module(compatibility_report)

DEFAULT_TARGETS = compatibility_report.DEFAULT_TARGETS
UNATTRIBUTED_TARGET = compatibility_report.UNATTRIBUTED_TARGET
build_report = compatibility_report.build_report
main = compatibility_report.main
read_junit = compatibility_report.read_junit
render_json = compatibility_report.render_json
render_markdown = compatibility_report.render_markdown
validate_targets = compatibility_report.validate_targets


JUNIT_CASES = """
<testsuites xmlns="urn:junit">
  <testsuite name="contracts" tests="8" failures="3" errors="1" skipped="2">
    <testcase classname="tests.contracts.test_api" name="test_pass[memory]" />
    <testcase classname="tests.contracts.test_api" name="test_skip[json]">
      <skipped type="pytest.skip" message="optional driver missing" />
    </testcase>
    <testcase classname="tests.contracts.test_api" name="test_gap[sqlite]">
      <skipped type="pytest.xfail" message="#77: known | array gap" />
    </testcase>
    <testcase classname="tests.contracts.test_api" name="test_fail[duckdb]">
      <failure type="AssertionError" message="documents differ" />
    </testcase>
    <testcase classname="tests.contracts.test_api" name="test_error[parquet]">
      <error type="RuntimeError">setup failed\nwith details</error>
    </testcase>
    <testcase classname="tests.contracts.test_api" name="test_fixed[memory]">
      <failure type="builtins.Failed" message="[XPASS(strict)] #73 fixed" />
    </testcase>
    <testcase classname="tests.contracts.test_api" name="test_reference[mongodb]" />
    <testcase classname="tests.contracts.test_api" name="collection_error">
      <failure message="not target-specific" />
    </testcase>
  </testsuite>
</testsuites>
""".strip()


def _write_junit(tmp_path, content=JUNIT_CASES, name="results.xml"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _target(report, name):
    return next(target for target in report["targets"] if target["name"] == name)


def test_read_junit_classifies_pytest_outcomes_and_targets(tmp_path):
    cases = read_junit(_write_junit(tmp_path))
    outcomes = {(case.target, case.contract): case.outcome for case in cases}

    assert outcomes[("memory", "test_pass")] == "passed"
    assert outcomes[("json", "test_skip")] == "skipped"
    assert outcomes[("sqlite", "test_gap")] == "xfailed"
    assert outcomes[("duckdb", "test_fail")] == "failed"
    assert outcomes[("parquet", "test_error")] == "error"
    assert outcomes[("memory", "test_fixed")] == "xpassed"
    assert outcomes[("mongodb", "test_reference")] == "passed"
    assert outcomes[(UNATTRIBUTED_TARGET, "collection_error")] == "failed"
    assert next(case for case in cases if case.contract == "test_error").reason == (
        "setup failed"
    )


def test_target_detection_supports_additional_pytest_parameters(tmp_path):
    xml = """
    <testsuite>
      <testcase classname="contracts" name="test_case[value-json-fast]" />
      <testcase classname="contracts" name="test_ambiguous[json-mongodb]" />
    </testsuite>
    """

    cases = read_junit(_write_junit(tmp_path, xml))

    assert cases[0].target == "json"
    assert cases[1].target == UNATTRIBUTED_TARGET


def test_target_detection_supports_full_hyphenated_names(tmp_path):
    xml = """
    <testsuite>
      <testcase classname="contracts" name="test_exact[remote-sql]" />
      <testcase classname="contracts" name="test_nested[value-remote-sql-fast]" />
      <testcase classname="contracts" name="test_boundary[notremote-sqlish]" />
    </testsuite>
    """

    cases = read_junit(_write_junit(tmp_path, xml), targets=("remote-sql", "memory"))
    outcomes = {case.contract: case.target for case in cases}

    assert outcomes["test_exact"] == "remote-sql"
    assert outcomes["test_nested"] == "remote-sql"
    assert outcomes["test_boundary"] == UNATTRIBUTED_TARGET


def test_target_detection_reports_overlapping_names_as_ambiguous(tmp_path):
    xml = """
    <testsuite>
      <testcase classname="contracts" name="test_case[remote-sql]" />
    </testsuite>
    """

    cases = read_junit(_write_junit(tmp_path, xml), targets=("sql", "remote-sql"))

    assert cases[0].target == UNATTRIBUTED_TARGET


@pytest.mark.parametrize(
    "targets",
    [
        (),
        ("json", "json"),
        ("unattributed",),
        ("bad target",),
        ("-leading",),
        ("trailing-",),
        ("two--hyphens",),
    ],
)
def test_invalid_or_reserved_target_names_are_rejected(targets):
    with pytest.raises(ValueError):
        validate_targets(targets)


def test_xpass_text_in_normal_failures_does_not_change_the_outcome(tmp_path):
    xml = """
    <testsuite>
      <testcase classname="contracts" name="test_message[memory]">
        <failure message="AssertionError: xpass was unexpected" />
      </testcase>
      <testcase classname="contracts" name="test_type[memory]">
        <failure type="CustomXpassAssertion" message="ordinary failure" />
      </testcase>
      <testcase classname="contracts" name="test_trace[memory]">
        <failure message="AssertionError: ordinary failure">
          traceback mentioned [XPASS(strict)] later
        </failure>
      </testcase>
      <testcase classname="contracts" name="test_not_prefix[memory]">
        <failure message="AssertionError: [XPASS(strict)] is data" />
      </testcase>
    </testsuite>
    """

    cases = read_junit(_write_junit(tmp_path, xml))

    assert {case.outcome for case in cases} == {"failed"}


def test_exact_xpass_result_type_is_supported(tmp_path):
    xml = """
    <testsuite>
      <testcase classname="contracts" name="test_type[memory]">
        <failure type="pytest.xpass" message="known gap fixed" />
      </testcase>
    </testsuite>
    """

    cases = read_junit(_write_junit(tmp_path, xml))

    assert cases[0].outcome == "xpassed"


def test_absolute_temp_paths_do_not_change_rendered_reports(tmp_path):
    template = """
    <testsuite>
      <testcase classname="contracts" name="test_missing[memory]">
        <failure message="FileNotFoundError: missing '{path}'" />
      </testcase>
    </testsuite>
    """
    first_cases = read_junit(
        _write_junit(
            tmp_path,
            template.format(path="/private/tmp/run-one/missing.json"),
            "first-path.xml",
        )
    )
    second_cases = read_junit(
        _write_junit(
            tmp_path,
            template.format(path="/var/folders/run-two/missing.json"),
            "second-path.xml",
        )
    )

    first = build_report(first_cases)
    second = build_report(second_cases)

    assert first_cases[0].reason == ("FileNotFoundError: missing '<ABSOLUTE_PATH>'")
    assert render_json(first) == render_json(second)
    assert render_markdown(first) == render_markdown(second)


def test_windows_drive_and_unc_paths_are_redacted(tmp_path):
    xml = r"""
    <testsuite>
      <testcase classname="contracts" name="test_windows[memory]">
        <failure message="Missing 'C:\Users\Ada Lovelace\Temp\file.json' and \\server\share\file.json" />
      </testcase>
    </testsuite>
    """

    cases = read_junit(_write_junit(tmp_path, xml))

    assert cases[0].reason == ("Missing '<ABSOLUTE_PATH>' and <ABSOLUTE_PATH>")


def test_report_scores_only_evaluated_non_reference_backend_cells(tmp_path):
    report = build_report(read_junit(_write_junit(tmp_path)))

    assert report["schema_version"] == 1
    assert report["scoring"]["overall"] == {
        "compatible": 2,
        "evaluated": 5,
        "percentage": 40.0,
    }
    assert _target(report, "memory")["score"] == {
        "compatible": 2,
        "evaluated": 2,
        "percentage": 100.0,
    }
    assert _target(report, "json")["score"] == {
        "compatible": 0,
        "evaluated": 0,
        "percentage": None,
    }
    assert _target(report, "mongodb")["role"] == "reference"
    assert _target(report, UNATTRIBUTED_TARGET)["role"] == "unattributed"
    assert report["totals"] == {
        "passed": 2,
        "xpassed": 1,
        "xfailed": 1,
        "failed": 2,
        "error": 1,
        "skipped": 1,
    }


def test_report_exposes_known_gaps_and_strict_xpasses(tmp_path):
    report = build_report(read_junit(_write_junit(tmp_path)))

    assert report["known_gaps"] == [
        {
            "contract": "test_gap",
            "name": "tests.contracts.test_api::test_gap[sqlite]",
            "reason": "#77: known | array gap",
            "target": "sqlite",
        }
    ]
    assert report["unexpected_passes"] == [
        {
            "contract": "test_fixed",
            "name": "tests.contracts.test_api::test_fixed[memory]",
            "reason": "[XPASS(strict)] #73 fixed",
            "target": "memory",
        }
    ]


def test_json_and_markdown_render_deterministically(tmp_path):
    first_cases = read_junit(_write_junit(tmp_path, name="first.xml"))
    reordered = JUNIT_CASES.replace(
        '<testcase classname="tests.contracts.test_api" name="test_pass[memory]" />',
        "",
    ).replace(
        "</testsuite>",
        '<testcase classname="tests.contracts.test_api" '
        'name="test_pass[memory]" />\n</testsuite>',
    )
    second_cases = read_junit(_write_junit(tmp_path, reordered, "second.xml"))

    first = build_report(first_cases)
    second = build_report(second_cases)

    assert render_json(first) == render_json(second)
    assert render_json(first).endswith("\n")
    assert json.loads(render_json(first)) == first
    markdown = render_markdown(first)
    assert "**40.00%** (2 compatible of 5 evaluated" in markdown
    assert "#77: known \\| array gap" in markdown
    assert "| mongodb | reference | 100.00%" in markdown


def test_cli_writes_both_reports_even_when_junit_contains_failures(tmp_path):
    junit = _write_junit(tmp_path)
    json_output = tmp_path / "reports" / "compatibility.json"
    markdown_output = tmp_path / "reports" / "compatibility.md"

    result = main(
        [
            str(junit),
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
        ]
    )

    assert result == 0
    assert json.loads(json_output.read_text(encoding="utf-8"))["totals"]["failed"] == 2
    assert markdown_output.read_text(encoding="utf-8").startswith(
        "# TinyMongo compatibility report\n"
    )


def test_cli_reports_invalid_or_missing_xml(tmp_path, capsys):
    invalid = _write_junit(tmp_path, "<testsuite>")

    assert main([str(invalid)]) == 2
    assert "Could not generate compatibility report" in capsys.readouterr().err
    assert main([str(tmp_path / "missing.xml")]) == 2


def test_custom_target_list_and_reference_are_supported(tmp_path):
    xml = """
    <testsuite>
      <testcase classname="contracts" name="test_one[alpha]" />
      <testcase classname="contracts" name="test_one[server]" />
    </testsuite>
    """
    cases = read_junit(_write_junit(tmp_path, xml), targets=("alpha", "server"))
    report = build_report(
        cases,
        targets=("alpha", "server"),
        reference_target="server",
    )

    assert [target["name"] for target in report["targets"]] == ["alpha", "server"]
    assert report["scoring"]["overall"]["percentage"] == 100.0
    assert report["scoring"]["reference_target"] == "server"
    assert "server reference target is excluded" in report["scoring"]["description"]
    assert "MongoDB" not in report["scoring"]["description"]
    assert DEFAULT_TARGETS[-1] == "mongodb"


def test_reference_target_must_be_configured(tmp_path):
    cases = read_junit(_write_junit(tmp_path), targets=DEFAULT_TARGETS)

    with pytest.raises(ValueError, match="reference target"):
        build_report(cases, targets=DEFAULT_TARGETS, reference_target="server")
