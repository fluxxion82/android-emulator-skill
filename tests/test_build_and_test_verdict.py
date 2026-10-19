"""``build_and_test.py --test``: the verdict an agent reads, end to end (N6, X7).

The specs in test_diagnostics_specs.py pin the two defects. These cover what
the fix added around them: the no-results refusal in both output modes, the
log its remedy points at (followed, not just printed), a passing run in every
mode, and the `--rerun` the fix depends on reaching Gradle.

Everything the fake ``gradlew`` prints or writes is recorded output, read
through ``recorded_gradle`` (tests/fake_toolchain.py).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from fake_toolchain import FakeToolchain, Rule, assert_refusal


@pytest.fixture
def toolchain(tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    return chain


def _project(
    toolchain: FakeToolchain,
    tmp_path: Path,
    rule: Rule,
    project_dir_answer: str = "gradle_properties_project_dir",
) -> Path:
    """A project whose gradlew answers ``rule`` and the builder's projectDir question.

    A ``--module`` test run first asks ``<module>:properties --property
    projectDir``. The answers are recorded (flavored scaffold, Gradle 8.13), with
    the recorder's ``/scaffold`` put back as this project: ``:app`` gets
    ``project_dir_answer``; ``:nope`` gets Gradle's refusal, on stderr as measured.
    """
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    (project / "app").mkdir(exist_ok=True)
    recorded = _recorded_gradle_text
    answers = [
        Rule(
            (":app:properties", "projectDir"),
            stdout=recorded(project_dir_answer).replace("/scaffold", str(project)),
        ),
        Rule(
            (":nope:properties", "projectDir"),
            stderr=recorded("gradle_properties_unknown_project").replace("/scaffold", str(project)),
            exit=1,
        ),
    ]
    toolchain.install("gradlew", [*answers, rule], directory=project)
    return project


def _recorded_gradle_text(name: str) -> str:
    return (RECORDED_ROOT / "gradle-8.13" / f"{name}.txt").read_text(encoding="utf-8")


def _results_dir(project: Path) -> Path:
    return project / "app" / "build" / "test-results" / "testDebugUnitTest"


def _silent_green_run(toolchain, tmp_path, recorded_gradle) -> Path:
    """Gradle prints BUILD SUCCESSFUL, exits 0 and writes no report -- NO-SOURCE's shape."""
    rule = Rule(("testDebugUnitTest",), stdout=recorded_gradle("gradle_build_successful"), exit=0)
    return _project(toolchain, tmp_path, rule)


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a_run_with_no_results_is_refused(json_mode, toolchain, tmp_path, recorded_gradle):
    """N6: "0 tests, 0 failed" is not a pass; it is a refusal in the boundary's shape."""
    project = _silent_green_run(toolchain, tmp_path, recorded_gradle)
    argv = ["--project", str(project), "--test", *(["--json"] if json_mode else [])]
    result = toolchain.run("build_and_test.py", *argv)

    if not toolchain.calls("gradlew"):
        pytest.fail("gradlew was never run")
    assert_refusal(result, json_mode=json_mode, naming=("no test results",), status=1)


def test_the_remedy_leads_to_this_runs_log(toolchain, tmp_path, recorded_gradle):
    """The refusal names a build id; following it must give back the log Gradle printed.

    A remedy that says "read the log" is only a remedy if the command in it
    works, so the id is taken from the printed remedy and used as an agent
    would use it.
    """
    project = _silent_green_run(toolchain, tmp_path, recorded_gradle)
    refused = toolchain.run("build_and_test.py", "--project", str(project), "--test", "--json")
    payload = assert_refusal(refused, json_mode=True, naming=("no test results",), status=1)

    match = re.search(r"--get-log (build-[\w-]+)", payload["remedy"])
    assert match, f"the remedy names no build to read: {payload['remedy']!r}"
    followed = toolchain.run("build_and_test.py", "--get-log", match.group(1), "--json")

    assert followed.returncode == 0, followed.stderr
    log = json.loads(followed.stdout)["log"]
    assert log.strip() == recorded_gradle("gradle_build_successful").strip()


def test_a_failed_gradle_run_with_no_results_is_reported_as_a_build_failure(
    toolchain, tmp_path, recorded_gradle
):
    """Gradle's own failure explains the missing reports; that is not the refusal."""
    rule = Rule(("testDebugUnitTest",), stdout=recorded_gradle("gradle_task_not_found"), exit=1)
    project = _project(toolchain, tmp_path, rule)
    result = toolchain.run("build_and_test.py", "--project", str(project), "--test", "--json")

    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert payload["success"] is False
    assert "build_id" in payload, "a build failure is cached, with its id"
    assert "test_results" not in payload, "no test ran; there are no counts to report"
    assert payload["errors"], "the recorded failure's reason is the verdict"


RECORDED_ROOT = Path(__file__).resolve().parent / "fixtures" / "recorded"


def test_the_old_gradle_recording_is_from_a_gradle_without_rerun():
    """Anti-vacuity for the refusal below: the evidence really is a pre-7.6 Gradle's."""
    manifest = json.loads((RECORDED_ROOT / "gradle-7.5.1" / "MANIFEST.json").read_text("utf-8"))
    (entry,) = [f for f in manifest["fixtures"] if f["name"] == "gradle_rerun_unknown_option"]
    major, minor = (int(part) for part in manifest["gradle_version"].split(".")[:2])
    assert (major, minor) < (7, 6), manifest["gradle_version"]
    assert "--rerun" in entry["command"].split(), entry["command"]


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a_gradle_without_rerun_is_refused_by_name(json_mode, toolchain, tmp_path, recorded_gradle):
    """Gradle < 7.6 rejects --rerun before any test runs; say that, not "Build: FAILED"."""
    rule = Rule(
        ("testDebugUnitTest",),
        stdout=recorded_gradle("gradle_rerun_unknown_option", version="7.5.1"),
        exit=1,
    )
    project = _project(toolchain, tmp_path, rule)
    argv = ["--project", str(project), "--test", *(["--json"] if json_mode else [])]
    result = toolchain.run("build_and_test.py", *argv)

    if not toolchain.calls("gradlew"):
        pytest.fail("gradlew was never run")
    payload = assert_refusal(result, json_mode=json_mode, naming=("7.6",), status=1)
    if payload is not None:
        assert "--rerun" in payload["error"], payload
        assert "wrapper" in payload["remedy"], payload


X7_MODES = {"text": (), "verbose": ("--verbose",), "json": ("--json",)}


@pytest.mark.parametrize("mode", list(X7_MODES))
def test_a_green_run_passes_in_every_mode(mode, toolchain, tmp_path, recorded_gradle):
    """The other half of X7's parity: a run that passed says so everywhere, exit 0."""
    rule = Rule(
        ("testDebugUnitTest",),
        stdout=recorded_gradle("gradle_build_successful"),
        exit=0,
        writes=(
            (
                str(_results_dir(tmp_path / "proj") / "TEST-com.example.PassingTest.xml"),
                recorded_gradle("junit_xml_passing_case"),
            ),
        ),
    )
    project = _project(toolchain, tmp_path, rule)
    result = toolchain.run(
        "build_and_test.py", "--project", str(project), "--test", *X7_MODES[mode]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    if mode == "json":
        payload = json.loads(result.stdout)
        assert payload["success"] is True
        assert payload["test_results"]["passed"] == 1
    else:
        assert result.stdout.splitlines()[0].startswith("Tests: PASS (1/1 passed, 0 failed")


def test_rerun_reaches_gradle_right_after_the_test_task(toolchain, tmp_path, recorded_gradle):
    """The argv the stub received, not the one the builder meant to send."""
    project = _silent_green_run(toolchain, tmp_path, recorded_gradle)
    toolchain.run(
        "build_and_test.py",
        "--project",
        str(project),
        "--module",
        ":app",
        "--test",
        "--suite",
        "com.example.PassingTest",
        "--json",
    )

    question, argv = toolchain.calls("gradlew")
    assert ":app:properties" in question, question
    task = argv.index(":app:testDebugUnitTest")
    assert argv[task + 1] == "--rerun", argv
    assert argv[argv.index("--tests") + 1] == "com.example.PassingTest", argv


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a_module_gradle_does_not_know_is_refused_before_its_tests(
    json_mode, toolchain, tmp_path, recorded_gradle
):
    """Gradle's own "not found in root project" is the refusal; the test task never starts."""
    project = _silent_green_run(toolchain, tmp_path, recorded_gradle)
    stale = _results_dir(project) / "TEST-stale.xml"
    stale.parent.mkdir(parents=True)
    stale.write_text(recorded_gradle("junit_xml_passing_case"), encoding="utf-8")

    result = toolchain.run(
        "build_and_test.py",
        "--project",
        str(project),
        "--module",
        ":nope",
        "--test",
        *(["--json"] if json_mode else []),
    )

    payload = assert_refusal(
        result,
        json_mode=json_mode,
        naming=("not found in root project",),
        status=1,
    )
    if payload is not None:
        assert "module :nope" in payload["error"], payload
        assert "./gradlew projects" in payload["remedy"], payload
        assert ":nope:properties" in payload["command"], payload
    calls = toolchain.calls("gradlew")
    assert calls and all(":nope:properties" in argv for argv in calls), calls
    assert stale.exists(), "a report was deleted for a module Gradle does not have"


def test_a_remapped_module_is_cleaned_and_read_where_gradle_put_it(
    toolchain, tmp_path, recorded_gradle
):
    """End to end: Gradle answers mobile/, the run passes on mobile/'s report alone.

    The answer is the recording from a scaffold copy with
    ``project(':app').projectDir = file('mobile')`` appended to its settings.
    app/ holds a stale FAILING report: reading it would fail the run.
    """
    project = tmp_path / "proj"
    mobile = project / "mobile" / "build" / "test-results" / "testDebugUnitTest"
    rule = Rule(
        # A stub matches whole argv elements; a --module run names the scoped task.
        (":app:testDebugUnitTest",),
        stdout=recorded_gradle("gradle_build_successful"),
        exit=0,
        writes=(
            (
                str(mobile / "TEST-com.example.PassingTest.xml"),
                recorded_gradle("junit_xml_passing_case"),
            ),
        ),
    )
    project = _project(
        toolchain, tmp_path, rule, project_dir_answer="gradle_properties_project_dir_remapped"
    )
    mobile_stale = mobile / "TEST-stale.xml"
    app_stale = _results_dir(project) / "TEST-stale.xml"
    for stale, text in (
        (mobile_stale, "junit_xml_error_case"),
        (app_stale, "junit_xml_error_case"),
    ):
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(recorded_gradle(text), encoding="utf-8")

    result = toolchain.run(
        "build_and_test.py", "--project", str(project), "--module", ":app", "--test", "--json"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["test_results"] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    assert not mobile_stale.exists(), "cleanup missed the directory Gradle named"
    assert app_stale.exists(), "cleanup reached app/, which Gradle did not name"


@pytest.mark.parametrize(
    ("test_mode", "timeout_constant", "timeout_variable"),
    [
        (False, "BUILD_TIMEOUT", "ANDROID_EMU_BUILD_TIMEOUT"),
        (True, "TEST_TIMEOUT", "ANDROID_EMU_TEST_TIMEOUT"),
    ],
    ids=["build", "test"],
)
def test_gradle_timeout_names_its_mode_and_preserves_the_partial_log(
    test_mode,
    timeout_constant,
    timeout_variable,
    *,
    toolchain,
    tmp_path,
    monkeypatch,
    capsys,
    recorded_gradle,
):
    """B9: timeout refusal caches the log and names the active mode's setting."""
    import build_and_test as build_script

    import gradle.builder as builder_mod
    from common import cli
    from gradle import BuildResultCache

    partial = recorded_gradle("gradle_build_successful")
    project = _project(toolchain, tmp_path, Rule(("unused",), stdout=partial))
    cache = BuildResultCache(cache_dir=str(tmp_path / "cache"))
    monkeypatch.setattr(build_script, "BuildResultCache", lambda: cache)
    monkeypatch.setattr(builder_mod, timeout_constant, 1)

    def time_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output=partial, stderr="")

    monkeypatch.setattr(builder_mod.subprocess, "run", time_out)
    with pytest.raises(SystemExit) as stopped:
        cli.run(
            build_script.main,
            ["--project", str(project), *(["--test"] if test_mode else []), "--json"],
        )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert stopped.value.code == 1
    assert "error" in payload, payload
    assert "timed out" in payload["error"].lower()
    assert timeout_variable in payload["remedy"]
    match = re.search(r"--get-log (build-[\w-]+)", payload["remedy"])
    assert match, payload["remedy"]
    assert partial.strip() in cache.get_log(match.group(1))
