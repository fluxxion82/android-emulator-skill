"""What AGP 8.13.2 really does for ``connectedDebugAndroidTest`` (v0.9.0 Inc 0).

Ground truth for instrumented tests, recorded by
``tests/record_gradle_fixtures.py --serial emulator-NNNN`` from the compose
scaffold's androidTest sources on one emulator. Nothing here changes the
skill; it pins the facts the instrumented-test work will be built on, so that
work cannot be written against an imagined layout:

* the JUnit XML lands under ``build/outputs/androidTest-results/``, not under
  ``build/test-results/`` -- the unit-test root ``gradle/results.py`` searches;
* its file name carries the AVD name (spaces and parentheses included);
* a red run's ``<failure>`` has no ``message`` attribute;
* ``--console=plain`` does not stop the test runner colouring ``FAILED``;
* a serial that matches no device fails in Gradle's own error block.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import record_gradle_fixtures as recorder

from gradle.results import find_test_result_files, parse_junit_xml

MANIFEST = (
    Path(__file__).resolve().parent / "fixtures" / "recorded" / "gradle-8.13" / "MANIFEST.json"
)
LIVE_RUNS = ("connected_android_test_success", "connected_android_test_failure")


def _entry(name: str) -> dict:
    fixtures = json.loads(MANIFEST.read_text(encoding="utf-8"))["fixtures"]
    (entry,) = [entry for entry in fixtures if entry["name"] == name]
    return entry


def test_the_passing_class_parses_green(recorded_gradle):
    result = parse_junit_xml(recorded_gradle("junit_xml_instrumented_passing"), strict=True)

    assert result["total"] >= 1, result
    assert result["failed"] == 0, result
    assert result["errors"] == 0, result
    assert result["passed"] == result["total"], result


def test_the_mixed_class_parses_red_with_its_pass_kept(recorded_gradle):
    result = parse_junit_xml(recorded_gradle("junit_xml_instrumented_failing"), strict=True)

    assert result["failed"] >= 1, result
    assert result["passed"] >= 1, "the passing half of the mixed suite was lost"
    (failed,) = result["failed_tests"]
    assert (
        failed["test_name"] == "com.example.composefixture.FailingInstrumentedTest.failsOnPurpose"
    )
    # AGP writes no message="" attribute: the message comes from the first line
    # of the element's text, the stack trace that follows it.
    assert failed["failure_message"] == "java.lang.AssertionError: recorded failure"


@pytest.mark.parametrize("run", LIVE_RUNS)
def test_the_xml_is_not_under_the_unit_test_root(run, tmp_path: Path, recorded_gradle):
    """The point of recording: results.py's ``**/build/test-results`` search misses it."""
    results_path = _entry(run)["results_path"]
    module_relative = results_path.removeprefix("app/")

    assert results_path.startswith("app/build/outputs/androidTest-results/connected/debug/")
    assert not module_relative.startswith("build/test-results/")
    assert Path(results_path).name.startswith("TEST-")

    # Put the recorded report exactly where AGP put it and ask the real finder.
    xml_name = "junit_xml_instrumented_" + ("passing" if run.endswith("success") else "failing")
    placed = tmp_path / results_path
    placed.parent.mkdir(parents=True)
    placed.write_text(recorded_gradle(xml_name), encoding="utf-8")

    assert find_test_result_files(tmp_path) == []
    assert find_test_result_files(tmp_path, task="connectedDebugAndroidTest") == []
    assert find_test_result_files(tmp_path / "app", task="connectedDebugAndroidTest") == []


def test_the_recorded_tree_contains_the_xml_and_the_per_device_directory(recorded_gradle):
    tree = recorded_gradle("instrumented_results_tree").splitlines()
    results_path = _entry("connected_android_test_success")["results_path"]

    assert results_path in tree
    # The XML sits beside a per-device directory of UTP output, not inside it.
    device_dirs = [line for line in tree if line.endswith("/") and "(AVD)" in line]
    assert device_dirs, tree
    assert any(line.endswith("/test-result.pb") for line in tree)


def test_exit_codes_and_wall_time_are_recorded():
    assert _entry("connected_android_test_success")["exit_code"] == 0
    assert _entry("connected_android_test_failure")["exit_code"] != 0
    assert _entry("connected_android_test_no_device")["exit_code"] != 0
    for run in (*LIVE_RUNS, "connected_android_test_no_device"):
        assert _entry(run)["wall_seconds"] > 0


def test_a_red_run_names_the_failing_test_and_points_at_the_html_report(recorded_gradle):
    log = recorded_gradle("connected_android_test_failure")

    assert "Execution failed for task ':app:connectedDebugAndroidTest'." in log
    assert "> There were failing tests. See the report at: " in log
    assert "app/build/reports/androidTests/connected/debug/index.html" in log
    assert "java.lang.AssertionError: recorded failure" in log
    # --console=plain is passed, and the runner colours FAILED anyway.
    assert "\x1b[31mFAILED" in log


def test_a_green_run_says_how_many_tests_ran_on_which_device(recorded_gradle):
    log = recorded_gradle("connected_android_test_success")

    assert "Starting 1 tests on " in log
    assert "Finished 1 tests on " in log
    assert "BUILD SUCCESSFUL" in log


def test_a_serial_with_no_device_fails_in_gradles_error_block(recorded_gradle):
    """AGP raises it, Gradle prints it: no adb text reaches the log."""
    log = recorded_gradle("connected_android_test_no_device")

    assert "* What went wrong:" in log
    assert (
        "> com.android.builder.testing.api.DeviceException: "
        "Connected device with serial 'no-such-serial-xyz' not found!"
    ) in log
    assert "BUILD FAILED" in log


def test_every_recorder_fixture_uses_only_enumerated_args():
    assert recorder._args_refusals(recorder.FIXTURES) == []
    for run in (*LIVE_RUNS, "connected_android_test_no_device"):
        (fixture,) = [f for f in recorder.FIXTURES if f.name == run]
        assert set(fixture.args) & recorder.DEVICE_TASKS
        assert fixture.android_serial is not None


def _probe(args: list[str], android_serial: str | None = None) -> recorder.GradleFixture:
    return recorder.GradleFixture(
        name="probe", args=args, description="", android_serial=android_serial
    )


@pytest.mark.parametrize(
    "args",
    [
        [":app:cDAT"],  # Gradle would expand this to connectedDebugAndroidTest
        ["connectedDebugAndroidTest"],  # not module-qualified as enumerated
        [":app:connectedCheck"],
        [":app:installDebug"],
        [":app:properties", "-Pandroid.injected.device.serial=x"],
        [":app:properties", "--property"],
    ],
)
def test_an_unenumerated_arg_is_refused_quoted(args):
    why = recorder._args_refusal(_probe(args, android_serial="emulator-5554"))

    assert why is not None
    assert repr(args[-1]) in why or repr(args[0]) in why, why


def test_a_device_task_without_a_pinned_serial_is_refused():
    why = recorder._args_refusal(_probe([":app:connectedDebugAndroidTest"]))

    assert why is not None and "':app:connectedDebugAndroidTest'" in why
    assert recorder._args_refusal(_probe([":app:connectedDebugAndroidTest"], "emulator-5")) is None


@pytest.mark.parametrize(
    "args", [[":app:cDAT"], [":app:connectedDebugAndroidTest"]], ids=["abbreviated", "unpinned"]
)
def test_record_refuses_before_running_anything(args, monkeypatch, capsys):
    """No Gradle, no adb, no device: the refusal comes before the first subprocess."""

    def no_subprocess(*_args, **_kwargs):
        raise AssertionError("a refused fixture reached subprocess.run")

    monkeypatch.setattr(recorder.subprocess, "run", no_subprocess)
    monkeypatch.setattr(recorder, "FIXTURES", [*recorder.FIXTURES, _probe(args)])

    assert recorder.record("gradle", only={"probe"}, serial="emulator-5554") == 2
    assert repr(args[0]) in capsys.readouterr().err


@pytest.mark.parametrize("serial", [None, "", "3C211FDJH0030R", "127.0.0.1:5555"])
def test_the_recorder_refuses_anything_but_an_emulator(serial, monkeypatch):
    # A shell-level ANDROID_SERIAL is deliberately not a fallback.
    monkeypatch.setenv("ANDROID_SERIAL", "emulator-5554")
    live = [f for f in recorder.FIXTURES if f.name in LIVE_RUNS]

    assert recorder._serial_refusal(serial, live) is not None


def test_the_recorder_pins_the_emulator_in_the_childs_environment(monkeypatch):
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)
    (live,) = [f for f in recorder.FIXTURES if f.name == "connected_android_test_success"]
    (absent,) = [f for f in recorder.FIXTURES if f.name == "connected_android_test_no_device"]

    assert recorder._serial_refusal("emulator-5554", [live]) is None
    assert recorder._environment(live, "emulator-5554")["ANDROID_SERIAL"] == "emulator-5554"
    # The no-device run ignores --serial: its whole point is a serial that is absent.
    assert recorder._environment(absent, "emulator-5554")["ANDROID_SERIAL"] == "no-such-serial-xyz"
