"""``build_and_test.py --instrumented``: the verdict an agent reads, end to end.

Run as a subprocess against the fake toolchain (tests/fake_toolchain.py): a
fake ``adb`` answering ``devices -l`` with a recorded listing, and a fake
``gradlew`` answering the connected task with a recorded AGP 8.13.2 run --
its console log, its exit status, and the JUnit XML it wrote, put where AGP
put it (the manifest's ``results_path``).

The fake ``gradlew`` answers a connected task only when ``ANDROID_SERIAL`` is
in its environment with the expected serial (``Rule.env``). That is how the
device is pinned -- an environment variable, not an argument -- so argv alone
cannot show it; a Gradle started without it gets no answer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fake_toolchain import FakeToolchain, Rule, assert_refusal

RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded" / "gradle-8.13"
CLASS_ARG = "-Pandroid.testInstrumentationRunnerArguments.class="
PASSING = "com.example.composefixture.PassingInstrumentedTest"
FAILING = "com.example.composefixture.FailingInstrumentedTest"
FAILING_TEST = f"{FAILING}.failsOnPurpose"
NO_SUCH = "com.example.composefixture.NoSuchTest"


def _entry(name: str) -> dict:
    fixtures = json.loads((RECORDED / "MANIFEST.json").read_text(encoding="utf-8"))["fixtures"]
    (entry,) = [entry for entry in fixtures if entry["name"] == name]
    return entry


@pytest.fixture
def toolchain(tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    return chain


@pytest.fixture
def serial(recorded) -> str:
    """The one emulator in the recorded single-device listing."""
    (row,) = [line.split() for line in recorded.lines("adb_devices_single")[1:] if line.strip()]
    return row[0]


def _adb(toolchain: FakeToolchain, listing: str) -> None:
    toolchain.install("adb", [Rule(("devices",), stdout=listing)])


def _nothing_attached(recorded) -> str:
    """The recorded listing's header alone: adb's answer with nothing attached."""
    return recorded.lines("adb_devices_single")[0] + "\n"


def _run_answer(project: Path, run: str, xml: str | None, recorded_gradle, serial: str, *match):
    """A connected run's recorded answer: its log and exit, and its report where AGP wrote it."""
    writes = ()
    if xml is not None:
        writes = ((str(project / _entry(run)["results_path"]), recorded_gradle(xml)),)
    return Rule(
        match,
        stdout=recorded_gradle(run),
        exit=_entry(run)["exit_code"],
        writes=writes,
        env=(("ANDROID_SERIAL", serial),),
    )


def _project(toolchain: FakeToolchain, tmp_path: Path, rules: list[Rule], recorded_gradle):
    """A project whose gradlew answers ``rules``, and the --module projectDir question."""
    project = tmp_path / "proj"
    (project / "app").mkdir(parents=True, exist_ok=True)
    answer = Rule(
        (":app:properties", "projectDir"),
        stdout=recorded_gradle("gradle_properties_project_dir").replace("/scaffold", str(project)),
    )
    toolchain.install("gradlew", [answer, *rules], directory=project)
    return project


def _connected(toolchain, tmp_path, recorded_gradle, serial, run, xml, *match):
    project = tmp_path / "proj"
    rule = _run_answer(project, run, xml, recorded_gradle, serial, *match)
    return _project(toolchain, tmp_path, [rule], recorded_gradle)


def _instrumented(toolchain, project, *argv):
    return toolchain.run("build_and_test.py", "--project", str(project), "--instrumented", *argv)


# --- exit criterion: instrumented tests are a feature -----------------------


def test_instrumented_tests_are_a_feature(toolchain, tmp_path, recorded, recorded_gradle, serial):
    """The recorded green run, its XML where AGP wrote it, read by the real finder and verdict.

    Red on v0.8.0, where `--instrumented` is not an option at all (and the
    finder returned [] for this path: test_the_xml_is_not_under_the_unit_test_root).
    """
    _adb(toolchain, recorded.text("adb_devices_single"))
    project = _connected(
        toolchain,
        tmp_path,
        recorded_gradle,
        serial,
        "connected_android_test_success",
        "junit_xml_instrumented_passing",
        "connectedDebugAndroidTest",
        f"{CLASS_ARG}{PASSING}",
    )

    result = _instrumented(toolchain, project, "--serial", serial, "--suite", PASSING, "--json")

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
    assert payload["serial"] == serial
    assert payload["device_task"] == "connectedDebugAndroidTest"
    # Everything a unit run's JSON carries, too.
    for key in ("build_id", "duration", "error_count", "warning_count", "variant", "module"):
        assert key in payload, key


@pytest.mark.parametrize("verbose", [False, True], ids=["text", "verbose"])
def test_a_green_run_says_pass_and_names_the_device(
    verbose, toolchain, tmp_path, recorded, recorded_gradle, serial
):
    _adb(toolchain, recorded.text("adb_devices_single"))
    project = _connected(
        toolchain,
        tmp_path,
        recorded_gradle,
        serial,
        "connected_android_test_success",
        "junit_xml_instrumented_passing",
        "connectedDebugAndroidTest",
    )

    result = _instrumented(
        toolchain, project, "--serial", serial, *(["--verbose"] if verbose else [])
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("Tests: PASS (1/1 passed, 0 failed, 0 skipped)"), result.stdout
    assert serial in result.stdout


@pytest.mark.parametrize("mode", ["json", "text", "verbose"])
def test_a_red_run_names_the_failing_test(
    mode, toolchain, tmp_path, recorded, recorded_gradle, serial
):
    _adb(toolchain, recorded.text("adb_devices_single"))
    project = _connected(
        toolchain,
        tmp_path,
        recorded_gradle,
        serial,
        "connected_android_test_failure",
        "junit_xml_instrumented_failing",
        f"{CLASS_ARG}{FAILING}",
    )
    flags = {"json": ["--json"], "text": [], "verbose": ["--verbose"]}[mode]

    result = _instrumented(toolchain, project, "--serial", serial, "--suite", FAILING, *flags)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    if mode == "json":
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert [t["test_name"] for t in payload["failed_tests"]] == [FAILING_TEST]
        assert payload["test_results"]["failed"] == 1 and payload["test_results"]["passed"] == 1
    else:
        assert result.stdout.startswith("Tests: FAIL (1/2 passed, 1 failed"), result.stdout
        assert FAILING_TEST in result.stdout
        assert "java.lang.AssertionError: recorded failure" in result.stdout


def test_an_agent_can_rerun_the_failing_class_from_the_printed_verdict(
    toolchain, tmp_path, recorded, recorded_gradle, serial
):
    """Step N's JSON is step N+1's input: the failing test's class becomes --suite.

    Then the build id it printed opens that run's errors. Nothing is taken
    from the test's own knowledge of the recording.
    """
    _adb(toolchain, recorded.text("adb_devices_single"))
    project = _connected(
        toolchain,
        tmp_path,
        recorded_gradle,
        serial,
        "connected_android_test_failure",
        "junit_xml_instrumented_failing",
        "connectedDebugAndroidTest",
    )
    first = json.loads(_instrumented(toolchain, project, "--serial", serial, "--json").stdout)

    failing_class = first["failed_tests"][0]["test_name"].rsplit(".", 1)[0]
    second = _instrumented(
        toolchain, project, "--serial", serial, "--suite", failing_class, "--json"
    )
    errors = toolchain.run("build_and_test.py", "--get-errors", first["build_id"], "--json")

    *_, argv = toolchain.calls("gradlew")
    assert f"{CLASS_ARG}{failing_class}" in argv, argv
    assert json.loads(second.stdout)["failed_tests"][0]["test_name"].startswith(failing_class)
    assert errors.returncode == 0, errors.stderr
    assert any("There were failing tests" in e["message"] for e in json.loads(errors.stdout))


# --- no test results --------------------------------------------------------


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a_class_filter_matching_nothing_is_no_test_results(
    json_mode, toolchain, tmp_path, recorded, recorded_gradle, serial
):
    """Measured: AGP reports one failing `initializationError`, exit 1. No test ran.

    Reported as "1 failed" it reads as a red test; it is a filter that matched
    nothing, refused as no test results with the instrumented remedy.
    """
    _adb(toolchain, recorded.text("adb_devices_single"))
    project = _connected(
        toolchain,
        tmp_path,
        recorded_gradle,
        serial,
        "connected_android_test_no_match",
        "junit_xml_instrumented_no_match",
        f"{CLASS_ARG}{NO_SUCH}",
    )

    result = _instrumented(
        toolchain,
        project,
        "--serial",
        serial,
        "--suite",
        NO_SUCH,
        *(["--json"] if json_mode else []),
    )

    payload = assert_refusal(result, json_mode=json_mode, naming=("no test results",), status=1)
    said = json.dumps(payload) if payload else result.stderr
    assert NO_SUCH in said, said
    assert "src/androidTest" in said, said
    assert "--suite" in said, said
    assert "passed" not in said, said


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a_green_gradle_that_wrote_no_report_is_no_test_results(
    json_mode, toolchain, tmp_path, recorded, recorded_gradle, serial
):
    _adb(toolchain, recorded.text("adb_devices_single"))
    project = _connected(
        toolchain,
        tmp_path,
        recorded_gradle,
        serial,
        "connected_android_test_success",
        None,
        "connectedDebugAndroidTest",
    )

    result = _instrumented(
        toolchain, project, "--serial", serial, *(["--json"] if json_mode else [])
    )

    payload = assert_refusal(result, json_mode=json_mode, naming=("no test results",), status=1)
    said = json.dumps(payload) if payload else result.stderr
    assert "build/outputs/androidTest-results/connected" in said, said
    assert "src/androidTest" in said and "testInstrumentationRunner" in said, said
    assert "unit tests under src/test" not in said, said


def test_the_unit_remedy_is_unchanged(toolchain, tmp_path, recorded_gradle):
    rule = Rule(("testDebugUnitTest",), stdout=recorded_gradle("gradle_build_successful"))
    project = _project(toolchain, tmp_path, [rule], recorded_gradle)

    result = toolchain.run("build_and_test.py", "--project", str(project), "--test", "--json")

    payload = assert_refusal(result, json_mode=True, naming=("no test results",), status=1)
    assert "unit tests under src/test" in payload["remedy"]
    assert "src/androidTest" not in payload["remedy"]


# --- the device -------------------------------------------------------------


def test_the_resolved_serial_reaches_gradle_in_its_environment(
    toolchain, tmp_path, recorded, recorded_gradle, serial
):
    """`--serial emulator` resolves to the attached emulator; THAT serial is pinned.

    The fake gradlew answers only with ANDROID_SERIAL=<resolved serial> in its
    environment; argv carries no serial at all.
    """
    _adb(toolchain, recorded.text("adb_devices_single"))
    project = _connected(
        toolchain,
        tmp_path,
        recorded_gradle,
        serial,
        "connected_android_test_success",
        "junit_xml_instrumented_passing",
        ":app:connectedDebugAndroidTest",
    )

    result = _instrumented(
        toolchain,
        project,
        "--module",
        ":app",
        "--serial",
        "emulator",
        "--suite",
        PASSING,
        "--json",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["serial"] == serial
    _question, argv = toolchain.calls("gradlew")
    task = argv.index(":app:connectedDebugAndroidTest")
    assert argv[task + 1] == "--rerun", argv
    assert argv[task + 2] == f"{CLASS_ARG}{PASSING}", argv
    assert "--tests" not in argv
    assert not any(serial in arg for arg in argv), argv


@pytest.mark.parametrize("serial_flag", [True, False], ids=["serial", "no-serial"])
@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_no_device_attached_is_exit_3_before_gradle(
    serial_flag, json_mode, toolchain, tmp_path, recorded, recorded_gradle, serial
):
    _adb(toolchain, _nothing_attached(recorded))
    project = _connected(
        toolchain, tmp_path, recorded_gradle, serial, "connected_android_test_success", None
    )

    result = _instrumented(
        toolchain,
        project,
        *(["--serial", serial] if serial_flag else []),
        *(["--json"] if json_mode else []),
    )

    assert_refusal(result, json_mode=json_mode, naming=("no devices",), status=3)
    assert toolchain.calls("gradlew") == [], "Gradle started with no device to pin"


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_several_devices_and_no_serial_is_exit_1_before_gradle(
    json_mode, toolchain, tmp_path, recorded, recorded_gradle, serial
):
    """With several attached, an unpinned connected task runs on every one of them."""
    _adb(toolchain, recorded.text("adb_devices_multiple"))
    project = _connected(
        toolchain, tmp_path, recorded_gradle, serial, "connected_android_test_success", None
    )

    result = _instrumented(toolchain, project, *(["--json"] if json_mode else []))

    payload = assert_refusal(result, json_mode=json_mode, naming=("more than one",), status=1)
    said = json.dumps(payload) if payload else result.stderr
    for attached in [
        line.split()[0] for line in recorded.lines("adb_devices_multiple")[1:] if line.strip()
    ]:
        assert attached in said, said
    assert toolchain.calls("gradlew") == []


def test_one_of_several_is_pinned_when_named(toolchain, tmp_path, recorded, recorded_gradle):
    _adb(toolchain, recorded.text("adb_devices_multiple"))
    chosen = [
        line.split()[0] for line in recorded.lines("adb_devices_multiple")[1:] if line.strip()
    ][-1]
    project = _connected(
        toolchain,
        tmp_path,
        recorded_gradle,
        chosen,
        "connected_android_test_success",
        "junit_xml_instrumented_passing",
        "connectedDebugAndroidTest",
    )

    result = _instrumented(toolchain, project, "--serial", chosen, "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["serial"] == chosen


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a_device_gone_when_gradle_ran_is_exit_3(
    json_mode, toolchain, tmp_path, recorded, recorded_gradle, serial
):
    """Resolved, then gone: AGP's DeviceException, read from Gradle's error block."""
    _adb(toolchain, recorded.text("adb_devices_single"))
    project = _connected(
        toolchain,
        tmp_path,
        recorded_gradle,
        serial,
        "connected_android_test_no_device",
        None,
        "connectedDebugAndroidTest",
    )

    result = _instrumented(
        toolchain, project, "--serial", serial, *(["--json"] if json_mode else [])
    )

    payload = assert_refusal(result, json_mode=json_mode, naming=("not found",), status=3)
    said = json.dumps(payload) if payload else result.stderr
    # The serial AGP named, from its own sentence.
    assert "no-such-serial-xyz" in said, said
    match = re.search(r"--get-log (build-[\w-]+)", said)
    assert match, said
    log = toolchain.run("build_and_test.py", "--get-log", match.group(1), "--json")
    assert "DeviceException" in json.loads(log.stdout)["log"]


# --- usage ------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [["--instrumented", "--test"], ["--serial", "emulator-5554"], ["--test", "--serial", "x"]],
    ids=["with-test", "serial-alone", "serial-with-test"],
)
def test_usage_errors_are_exit_2_before_anything_runs(
    argv, toolchain, tmp_path, recorded, recorded_gradle, serial
):
    _adb(toolchain, recorded.text("adb_devices_single"))
    project = _connected(
        toolchain, tmp_path, recorded_gradle, serial, "connected_android_test_success", None
    )

    result = toolchain.run("build_and_test.py", "--project", str(project), *argv, "--json")

    assert_refusal(
        result, json_mode=True, naming=("--instrumented", "--serial", "--test"), status=2
    )
    assert toolchain.calls("gradlew") == [] and toolchain.calls("adb") == []


def test_a_timeout_names_the_instrumented_knob_and_keeps_the_partial_log(
    toolchain, tmp_path, monkeypatch, capsys, recorded_gradle, serial
):
    """Its own setting, higher than the unit tests' by default; the partial log is cached."""
    import subprocess

    import build_and_test as build_script

    import gradle.builder as builder_mod
    from common import cli
    from gradle import BuildResultCache

    partial = recorded_gradle("connected_android_test_success")
    project = _project(toolchain, tmp_path, [], recorded_gradle)
    cache = BuildResultCache(cache_dir=str(tmp_path / "cache"))
    monkeypatch.setattr(build_script, "BuildResultCache", lambda: cache)
    monkeypatch.setattr(build_script, "resolve_one_device", lambda requested: serial)
    monkeypatch.setattr(builder_mod, "INSTRUMENTED_TEST_TIMEOUT", 1)

    def time_out(cmd, **kwargs):
        assert kwargs["env"]["ANDROID_SERIAL"] == serial
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output=partial, stderr="")

    monkeypatch.setattr(builder_mod.subprocess, "run", time_out)
    with pytest.raises(SystemExit) as stopped:
        cli.run(build_script.main, ["--project", str(project), "--instrumented", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert stopped.value.code == 1
    assert "timed out after 1s" in payload["error"]
    assert "ANDROID_EMU_INSTRUMENTED_TEST_TIMEOUT" in payload["remedy"]
    match = re.search(r"--get-log (build-[\w-]+)", payload["remedy"])
    assert match and partial.strip() in cache.get_log(match.group(1))


# --- a test's output cannot make a red run a device error -------------------


def _spoofed_failure_log(recorded_gradle) -> str:
    """The recorded red run's log, with the recorded no-device block appended.

    An adversarial input, not a recording: what a test could print. The three
    lines are the no-device recording's own -- its "* What went wrong:"
    heading, AGP's DeviceException line and the "* Try:" heading after it.
    """
    no_device = recorded_gradle("connected_android_test_no_device").splitlines()
    start = no_device.index("* What went wrong:")
    (exception,) = [line for line in no_device if "DeviceException" in line]
    block = [no_device[start], exception, no_device[no_device.index("* Try:", start)]]
    return recorded_gradle("connected_android_test_failure") + "\n".join(block) + "\n"


def test_a_run_that_wrote_a_report_had_its_device(recorded_gradle, serial):
    """AGP raises the DeviceException while choosing a device, before any test runs."""
    from gradle import BuildResult, parse_junit_xml

    results = parse_junit_xml(recorded_gradle("junit_xml_instrumented_failing"), strict=True)
    result = BuildResult(
        success=False,
        stdout=_spoofed_failure_log(recorded_gradle),
        stderr="",
        errors=[],
        warnings=[],
        failed_tasks=[],
        test_results=results,
        variant="debug",
        module=None,
        tasks=["connectedDebugAndroidTest"],
        duration=1.0,
        returncode=1,
        results_dir="results",
        serial=serial,
    )

    assert result.missing_device is None


def test_a_spoofed_device_error_beside_a_red_report_stays_a_red_run(
    toolchain, tmp_path, recorded, recorded_gradle, serial
):
    _adb(toolchain, recorded.text("adb_devices_single"))
    project = tmp_path / "proj"
    rule = Rule(
        ("connectedDebugAndroidTest",),
        stdout=_spoofed_failure_log(recorded_gradle),
        exit=1,
        writes=(
            (
                str(project / _entry("connected_android_test_failure")["results_path"]),
                recorded_gradle("junit_xml_instrumented_failing"),
            ),
        ),
        env=(("ANDROID_SERIAL", serial),),
    )
    project = _project(toolchain, tmp_path, [rule], recorded_gradle)

    result = _instrumented(toolchain, project, "--serial", serial, "--json")

    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert [t["test_name"] for t in payload["failed_tests"]] == [FAILING_TEST], payload


# --- the flavored aggregate -------------------------------------------------


def test_an_aggregate_variant_is_refused_naming_the_flavored_variants(
    toolchain, tmp_path, recorded, recorded_gradle, serial
):
    """`--variant debug` in a flavored project: reports under flavors/*, none in debug/."""
    _adb(toolchain, recorded.text("adb_devices_single"))
    project = tmp_path / "proj"
    pro = project / _entry("connected_android_test_flavored")["results_path"]
    free = pro.parent.parent / "free" / pro.name.replace("-pro.xml", "-free.xml")
    xml = recorded_gradle("junit_xml_instrumented_flavored")
    rule = Rule(
        ("connectedDebugAndroidTest",),
        stdout=recorded_gradle("connected_android_test_flavored"),
        writes=((str(pro), xml), (str(free), xml)),
        env=(("ANDROID_SERIAL", serial),),
    )
    project = _project(toolchain, tmp_path, [rule], recorded_gradle)

    result = _instrumented(toolchain, project, "--serial", serial, "--json")

    payload = assert_refusal(result, json_mode=True, naming=("no test results",), status=1)
    said = payload["error"] + payload["remedy"]
    assert "--variant proDebug" in said and "--variant freeDebug" in said, said
    assert "testInstrumentationRunner" not in said, said
