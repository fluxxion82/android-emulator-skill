"""``build_and_test.py --instrumented`` against a real emulator: both outcomes, live.

The mocked specs (test_build_and_test_instrumented.py) replay recorded AGP
runs. These run the real thing -- the real script, the real Gradle wrapper,
AGP and AndroidJUnitRunner on the lane's emulator -- in a temporary copy of
the compose scaffold, and assert only what an agent reads: the exit status,
JSON ``success``, the counts, the failing test's name, the refusal.

Three live invocations, one per outcome: the passing class, the mixed class
whose ``failsOnPurpose`` fails, and a class that does not exist (refused as
no test results). The corpus records 8-36 s per connected run on a warm local
arm64 daemon (tests/fixtures/recorded/gradle-8.13/MANIFEST.json,
``wall_seconds``), so all three fit the lane's budget live; the first pays the
APK build, which the workflow's pre-boot step has already put in Gradle's
build cache.

``emulator_only_device`` and never ``live_device``: a connected task installs
two APKs, runs instrumentation, then uninstalls them. AGP also uninstalls the
app under test when it finishes, so a fixture app the lane installed for
other tests is put back afterwards -- best effort. A failed reinstall is
reported in the step summary, not raised: raised in teardown it would be an
ERROR against the last test, failing the lane on a reinstall rather than on an
instrumented verdict. A later test that needs the app reports its own missing
app.

When ``EMULATOR_LANE_INSTRUMENTED_REPORT`` names a file (the lane script sets
it), each outcome observed is appended there for the lane's step summary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.emulator

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"
COMPOSE = REPO_ROOT / "tests" / "fixtures" / "scaffold" / "compose"
PACKAGE = "com.example.composefixture"
PASSING = f"{PACKAGE}.PassingInstrumentedTest"
FAILING = f"{PACKAGE}.FailingInstrumentedTest"
NO_SUCH = f"{PACKAGE}.NoSuchTest"
# A cold first run compiles the app and its test APK; the skill's own bound
# (ANDROID_EMU_INSTRUMENTED_TEST_TIMEOUT) is longer still.
RUN_TIMEOUT = 1800
# Set by .github/scripts/run-emulator-lane.sh; read by emulator.yml's verdict step.
REPORT_VARIABLE = "EMULATOR_LANE_INSTRUMENTED_REPORT"
REPORT = os.environ.get(REPORT_VARIABLE)


def _installed(adb: str, device: str) -> bool:
    """`pm path` answers by exit status: 1 for a package that is not installed."""
    result = subprocess.run(
        [adb, "-s", device, "shell", "pm", "path", PACKAGE],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


@pytest.fixture(scope="module")
def compose_copy(tmp_path_factory, adb: str, emulator_only_device: str):
    """A throwaway copy of the compose scaffold, as the Gradle recorder makes one.

    Yields the copy; afterwards reinstalls the fixture app if it was installed
    before and AGP's end-of-run uninstall took it away -- best effort, the
    outcome observed either way (see the module docstring).
    """
    if not (os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")):
        pytest.skip("the compose scaffold builds with AGP: set ANDROID_HOME")
    copy = tmp_path_factory.mktemp("instrumented") / COMPOSE.name
    shutil.copytree(COMPOSE, copy, ignore=shutil.ignore_patterns(".gradle", "build", ".kotlin"))
    was_installed = _installed(adb, emulator_only_device)
    yield copy
    if was_installed and not _installed(adb, emulator_only_device):
        apk = copy / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
        try:
            installed = subprocess.run(
                [adb, "-s", emulator_only_device, "install", "-r", str(apk)],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            said = (installed.stderr or installed.stdout).strip().splitlines()
            failure = None if installed.returncode == 0 else (said[-1] if said else "no output")
        except (OSError, subprocess.TimeoutExpired) as error:
            failure = str(error)
        if failure is None:
            _observe("fixture app: uninstalled by AGP after the connected runs; reinstalled")
        else:
            _observe(
                "fixture app: uninstalled by AGP after the connected runs; "
                f"reinstall FAILED: {failure}"
            )


def _observe(line: str) -> None:
    print(line)
    if REPORT:
        with open(REPORT, "a", encoding="utf-8") as handle:
            handle.write(f"- {line}\n")


def _instrumented(copy: Path, device: str, suite: str) -> subprocess.CompletedProcess:
    # Nothing inherited may choose the device: only --serial does.
    env = {key: value for key, value in os.environ.items() if key != "ANDROID_SERIAL"}
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT / "build_and_test.py"),
            "--project",
            str(copy),
            "--module",
            ":app",
            "--instrumented",
            "--serial",
            device,
            "--suite",
            suite,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT,
        check=False,
        env=env,
    )


def _payload(result: subprocess.CompletedProcess) -> dict:
    shown = f"exit {result.returncode}\nstdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
    assert result.stdout.strip().startswith("{"), shown
    return json.loads(result.stdout)


def test_a_passing_class_passes_live(compose_copy: Path, emulator_only_device: str):
    result = _instrumented(compose_copy, emulator_only_device, PASSING)
    payload = _payload(result)

    assert result.returncode == 0, payload
    assert payload["success"] is True, payload
    assert payload["serial"] == emulator_only_device
    assert payload["device_task"] == ":app:connectedDebugAndroidTest"
    counts = payload["test_results"]
    assert counts["passed"] == counts["total"] >= 1, counts
    assert counts["failed"] == counts["errors"] == 0, counts
    _observe(
        f"passing run (--suite {PASSING}): exit 0, success true, "
        f"{counts['passed']}/{counts['total']} passed on {emulator_only_device} (live)"
    )


def test_a_failing_class_fails_live_and_names_the_test(
    compose_copy: Path, emulator_only_device: str
):
    result = _instrumented(compose_copy, emulator_only_device, FAILING)
    payload = _payload(result)

    assert result.returncode == 1, payload
    assert payload["success"] is False, payload
    names = [test["test_name"] for test in payload["failed_tests"]]
    assert f"{FAILING}.failsOnPurpose" in names, payload
    assert payload["test_results"]["passed"] >= 1, "the passing half of the mixed class was lost"
    _observe(
        f"failing run (--suite {FAILING}): exit 1, success false, "
        f"failed: {', '.join(names)} (live)"
    )


def test_a_class_that_does_not_exist_is_refused_live(compose_copy: Path, emulator_only_device: str):
    result = _instrumented(compose_copy, emulator_only_device, NO_SUCH)
    payload = _payload(result)

    assert result.returncode == 1, payload
    assert payload["success"] is False, payload
    assert "no test results" in payload["error"], payload
    assert NO_SUCH in payload["error"], payload
    assert "src/androidTest" in payload["remedy"], payload
    assert "test_results" not in payload, "a refusal carries no counts"
    _observe(
        f"no-results refusal (--suite {NO_SUCH}): exit 1, " f"\"{payload['error'][:80]}...\" (live)"
    )
