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
UNATTRIBUTED = compatibility_report.UNATTRIBUTED
build_report = compatibility_report.build_report
main = compatibility_report.main
read_junit = compatibility_report.read_junit
render_json = compatibility_report.render_json
render_markdown = compatibility_report.render_markdown
validate_apis = compatibility_report.validate_apis
validate_backends = compatibility_report.validate_backends


def _testcase(
    name,
    api=None,
    backend=None,
    suite=None,
    classname="tests.contracts.test_api_contract",
    result="",
):
    properties = []
    for key, value in (
        ("tinymongo.api", api),
        ("tinymongo.backend", backend),
        ("tinymongo.suite", suite),
    ):
        if value is not None:
            properties.append('<property name="{0}" value="{1}" />'.format(key, value))
    properties_xml = ""
    if properties:
        properties_xml = "<properties>{0}</properties>".format("".join(properties))
    return '<testcase classname="{0}" name="{1}">{2}{3}</testcase>'.format(
        classname, name, properties_xml, result
    )


def _junit(*cases):
    return "<testsuite>{0}</testsuite>".format("".join(cases))


def _write_junit(tmp_path, content, name="results.xml"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _complete_matrix_xml(contract="test_contract", outcome_overrides=None):
    outcome_overrides = outcome_overrides or {}
    cases = []
    for api in ("sync", "async"):
        for backend in ("memory", "mongodb"):
            cases.append(
                _testcase(
                    "{0}[{1}-{2}]".format(contract, api, backend),
                    api=api,
                    backend=backend,
                    suite="talkpython",
                    result=outcome_overrides.get((api, backend), ""),
                )
            )
    return _junit(*cases)


def _summary(report, dimension, name):
    return next(
        summary for summary in report["summaries"][dimension] if summary["name"] == name
    )


def test_default_targets_cover_sync_and_async_for_every_backend():
    assert DEFAULT_TARGETS == (
        "sync-memory",
        "sync-json",
        "sync-sqlite",
        "sync-duckdb",
        "sync-parquet",
        "sync-mongodb",
        "async-memory",
        "async-json",
        "async-sqlite",
        "async-duckdb",
        "async-parquet",
        "async-mongodb",
    )


def test_junit_properties_are_preferred_over_parameter_name_fallback(tmp_path):
    xml = _junit(
        _testcase(
            "test_case[async-json]",
            api="sync",
            backend="memory",
            suite="talkpython",
        )
    )

    case = read_junit(_write_junit(tmp_path, xml))[0]

    assert (case.api, case.backend, case.suite) == (
        "sync",
        "memory",
        "talkpython",
    )
    assert case.contract == "test_case[async-json]"


def test_name_and_classname_fallback_attribute_legacy_junit(tmp_path):
    xml = _junit(
        _testcase(
            "test_case[value-async-remote-sql]",
            classname="tests.contracts.test_talkpython_contract",
        )
    )

    case = read_junit(
        _write_junit(tmp_path, xml),
        backends=("memory", "remote-sql"),
    )[0]

    assert (case.api, case.backend, case.suite) == (
        "async",
        "remote-sql",
        "talkpython",
    )
    assert case.contract == "test_case[value]"


def test_conflicting_or_unknown_properties_are_unattributed(tmp_path):
    xml = """
    <testsuite>
      <testcase classname="tests.contracts.test_api_contract"
                name="test_case[sync-memory]">
        <properties>
          <property name="tinymongo.api" value="sync" />
          <property name="tinymongo.api" value="async" />
          <property name="tinymongo.backend" value="unknown" />
          <property name="tinymongo.suite" value="bad suite" />
        </properties>
      </testcase>
    </testsuite>
    """

    case = read_junit(_write_junit(tmp_path, xml))[0]
    report = build_report([case])

    assert (case.api, case.backend, case.suite) == (
        UNATTRIBUTED,
        UNATTRIBUTED,
        UNATTRIBUTED,
    )
    assert report["baseline"]["unattributed_cases"][0]["name"].endswith(
        "test_case[sync-memory]"
    )


def test_read_junit_classifies_all_pytest_outcomes(tmp_path):
    xml = _junit(
        _testcase("test_pass[sync-memory]", "sync", "memory", "core"),
        _testcase(
            "test_skip[sync-memory]",
            "sync",
            "memory",
            "core",
            result='<skipped type="pytest.skip" message="driver missing" />',
        ),
        _testcase(
            "test_gap[sync-memory]",
            "sync",
            "memory",
            "core",
            result='<skipped type="pytest.xfail" message="#77: known gap" />',
        ),
        _testcase(
            "test_fail[sync-memory]",
            "sync",
            "memory",
            "core",
            result='<failure type="AssertionError" message="different" />',
        ),
        _testcase(
            "test_error[sync-memory]",
            "sync",
            "memory",
            "core",
            result='<error type="RuntimeError">setup failed\nmore</error>',
        ),
        _testcase(
            "test_fixed[sync-memory]",
            "sync",
            "memory",
            "core",
            result=(
                '<failure type="builtins.Failed" message="[XPASS(strict)] fixed" />'
            ),
        ),
    )

    cases = read_junit(_write_junit(tmp_path, xml))

    assert {case.contract: case.outcome for case in cases} == {
        "test_error": "error",
        "test_fail": "failed",
        "test_fixed": "xpassed",
        "test_gap": "xfailed",
        "test_pass": "passed",
        "test_skip": "skipped",
    }
    assert next(case for case in cases if case.contract == "test_error").reason == (
        "setup failed"
    )


def test_complete_reference_backed_matrix_is_publishable(tmp_path):
    cases = read_junit(
        _write_junit(tmp_path, _complete_matrix_xml()),
        backends=("memory", "mongodb"),
    )

    report = build_report(cases, backends=("memory", "mongodb"))

    assert report["schema_version"] == 2
    assert report["baseline"]["status"] == "publishable"
    assert report["baseline"]["complete"] is True
    assert report["baseline"]["publishable"] is True
    assert report["baseline"]["blockers"] == []
    assert report["baseline"]["expected_cells"] == 4
    assert report["baseline"]["observed_cells"] == 4
    assert report["scoring"]["overall"] == {
        "compatible": 2,
        "evaluated": 2,
        "percentage": 100.0,
    }


def test_only_cells_with_same_api_passing_mongodb_reference_are_scored(tmp_path):
    xml = _complete_matrix_xml(
        outcome_overrides={
            ("async", "mongodb"): (
                '<failure type="AssertionError" message="reference failed" />'
            )
        }
    )
    cases = read_junit(
        _write_junit(tmp_path, xml),
        backends=("memory", "mongodb"),
    )

    report = build_report(cases, backends=("memory", "mongodb"))

    assert report["baseline"]["complete"] is True
    assert report["baseline"]["publishable"] is False
    assert report["baseline"]["status"] == "complete-unpublishable"
    assert report["scoring"]["overall"] == {
        "compatible": 1,
        "evaluated": 1,
        "percentage": 100.0,
    }
    assert report["scoring"]["excluded"][0]["api"] == "async"
    assert report["scoring"]["excluded"][0]["reason"] == "mongodb-reference-not-passed"
    assert _summary(report, "apis", "async")["score"]["evaluated"] == 0
    assert _summary(report, "backends", "mongodb")["score"]["evaluated"] == 0
    assert _summary(report, "suites", "talkpython")["score"]["evaluated"] == 1


def test_matrix_flags_missing_skipped_unattributed_and_duplicates(tmp_path):
    first = _junit(
        _testcase(
            "test_contract[sync-memory]",
            "sync",
            "memory",
            "core",
            result='<skipped type="pytest.skip" message="optional dependency" />',
        ),
        _testcase(
            "test_contract[sync-mongodb]",
            "sync",
            "mongodb",
            "core",
        ),
        _testcase("not_a_contract", classname="collection"),
    )
    second = _junit(
        _testcase(
            "test_contract[sync-mongodb]",
            "sync",
            "mongodb",
            "core",
        )
    )
    cases = read_junit(
        (
            _write_junit(tmp_path, first, "first.xml"),
            _write_junit(tmp_path, second, "second.xml"),
        ),
        backends=("memory", "mongodb"),
    )

    report = build_report(cases, backends=("memory", "mongodb"))
    baseline = report["baseline"]

    assert baseline["complete"] is False
    assert baseline["publishable"] is False
    assert set(baseline["blockers"]) == {
        "missing-targets",
        "missing-cells",
        "skipped-cells",
        "unattributed-cases",
        "duplicate-cells",
        "unqualified-mongodb-references",
    }
    assert baseline["missing_targets"] == [
        {"api": "async", "backend": "memory"},
        {"api": "async", "backend": "mongodb"},
    ]
    assert len(baseline["missing_cells"]) == 2
    assert len(baseline["skipped_cells"]) == 1
    assert len(baseline["unattributed_cases"]) == 1
    assert baseline["duplicate_cells"][0]["count"] == 2


def test_reports_have_api_backend_and_suite_summaries(tmp_path):
    cases = read_junit(
        _write_junit(tmp_path, _complete_matrix_xml()),
        backends=("memory", "mongodb"),
    )
    report = build_report(cases, backends=("memory", "mongodb"))

    assert [item["name"] for item in report["summaries"]["apis"]] == [
        "sync",
        "async",
    ]
    assert [item["name"] for item in report["summaries"]["backends"]] == [
        "memory",
        "mongodb",
    ]
    assert [item["name"] for item in report["summaries"]["suites"]] == ["talkpython"]
    assert _summary(report, "apis", "sync")["expected_cells"] == 2
    assert _summary(report, "backends", "memory")["observed_cells"] == 2
    assert _summary(report, "suites", "talkpython")["missing_cells"] == 0


def test_known_gaps_and_xpasses_include_all_dimensions(tmp_path):
    xml = _complete_matrix_xml(
        outcome_overrides={
            ("sync", "memory"): (
                '<skipped type="pytest.xfail" message="#77: array | gap" />'
            ),
            ("async", "memory"): ('<failure type="pytest.xpass" message="fixed" />'),
        }
    )
    cases = read_junit(
        _write_junit(tmp_path, xml),
        backends=("memory", "mongodb"),
    )

    report = build_report(cases, backends=("memory", "mongodb"))

    assert report["known_gaps"][0]["api"] == "sync"
    assert report["known_gaps"][0]["backend"] == "memory"
    assert report["known_gaps"][0]["suite"] == "talkpython"
    assert report["unexpected_passes"][0]["api"] == "async"
    assert "#77: array \\| gap" in render_markdown(report)


def test_absolute_paths_are_redacted_but_urls_are_preserved(tmp_path):
    template = """
    <testsuite>
      <testcase classname="tests.contracts.test_api_contract"
                name="test_missing[sync-memory]">
        <properties>
          <property name="tinymongo.api" value="sync" />
          <property name="tinymongo.backend" value="memory" />
          <property name="tinymongo.suite" value="core" />
        </properties>
        <failure message="Missing '{path}' at https://example.test/a/b" />
      </testcase>
    </testsuite>
    """
    first = read_junit(
        _write_junit(
            tmp_path,
            template.format(path="/private/tmp/run-one/file.json"),
            "first.xml",
        )
    )
    second = read_junit(
        _write_junit(
            tmp_path,
            template.format(path="/var/folders/run-two/file.json"),
            "second.xml",
        )
    )

    assert first[0].reason == ("Missing '<ABSOLUTE_PATH>' at https://example.test/a/b")
    assert render_json(build_report(first)) == render_json(build_report(second))


def test_windows_drive_and_unc_paths_are_redacted(tmp_path):
    xml = _junit(
        _testcase(
            "test_windows[sync-memory]",
            "sync",
            "memory",
            "core",
            result=(
                r'<failure message="Missing '
                r"'C:\Users\Ada Lovelace\Temp\file.json' and "
                r'\\server\share\file.json" />'
            ),
        )
    )

    case = read_junit(_write_junit(tmp_path, xml))[0]

    assert case.reason == "Missing '<ABSOLUTE_PATH>' and <ABSOLUTE_PATH>"


def test_json_and_markdown_are_deterministic_across_input_order(tmp_path):
    sync = _junit(
        _testcase("test_contract[sync-memory]", "sync", "memory", "core"),
        _testcase("test_contract[sync-mongodb]", "sync", "mongodb", "core"),
    )
    async_xml = _junit(
        _testcase("test_contract[async-memory]", "async", "memory", "core"),
        _testcase("test_contract[async-mongodb]", "async", "mongodb", "core"),
    )
    first = _write_junit(tmp_path, sync, "sync.xml")
    second = _write_junit(tmp_path, async_xml, "async.xml")

    report_one = build_report(
        read_junit((first, second), backends=("memory", "mongodb")),
        backends=("memory", "mongodb"),
    )
    report_two = build_report(
        read_junit((second, first), backends=("memory", "mongodb")),
        backends=("memory", "mongodb"),
    )

    assert render_json(report_one) == render_json(report_two)
    assert render_json(report_one).endswith("\n")
    assert json.loads(render_json(report_one)) == report_one
    assert render_markdown(report_one) == render_markdown(report_two)
    assert "Baseline status: **publishable**" in render_markdown(report_one)


def test_cli_accepts_multiple_inputs_and_emits_incomplete_reports(tmp_path):
    first = _write_junit(
        tmp_path,
        _junit(_testcase("test_contract[sync-memory]", "sync", "memory", "core")),
        "first.xml",
    )
    second = _write_junit(
        tmp_path,
        _junit(
            _testcase(
                "test_contract[sync-mongodb]",
                "sync",
                "mongodb",
                "core",
            )
        ),
        "second.xml",
    )
    json_output = tmp_path / "reports" / "compatibility.json"
    markdown_output = tmp_path / "reports" / "compatibility.md"

    result = main(
        [
            str(first),
            str(second),
            "--backends",
            "memory,mongodb",
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
        ]
    )

    assert result == 0
    report = json.loads(json_output.read_text(encoding="utf-8"))
    assert report["baseline"]["complete"] is False
    assert report["baseline"]["status"] == "incomplete"
    assert markdown_output.read_text(encoding="utf-8").startswith(
        "# TinyMongo compatibility report\n"
    )


def test_cli_errors_only_for_malformed_config_or_input(tmp_path, capsys):
    invalid = _write_junit(tmp_path, "<testsuite>")

    assert main([str(invalid)]) == 2
    assert "Could not generate compatibility report" in capsys.readouterr().err
    assert main([str(tmp_path / "missing.xml")]) == 2
    with pytest.raises(SystemExit):
        main([str(invalid), "--apis", "bad api"])


@pytest.mark.parametrize(
    "validator,values",
    [
        (validate_apis, ()),
        (validate_backends, ("json", "json")),
        (validate_apis, ("unattributed",)),
        (validate_backends, ("bad target",)),
        (validate_backends, ("-leading",)),
        (validate_backends, ("trailing-",)),
        (validate_backends, ("two--hyphens",)),
    ],
)
def test_invalid_or_reserved_dimension_names_are_rejected(validator, values):
    with pytest.raises(ValueError):
        validator(values)


def test_reference_backend_must_be_configured(tmp_path):
    cases = read_junit(
        _write_junit(tmp_path, _complete_matrix_xml()),
        backends=("memory", "mongodb"),
    )

    with pytest.raises(ValueError, match="reference backend"):
        build_report(
            cases,
            backends=("memory", "mongodb"),
            reference_backend="server",
        )
