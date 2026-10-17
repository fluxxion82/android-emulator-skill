#!/usr/bin/env bash
#
# Run the device-backed test lane inside the emulator-runner action.
#
# The point of this script is that a lane which did not test what it gates on
# must not read as a pass. Every test in the emulator lane skips politely when
# its precondition is missing -- no device, fixture app not installed -- which
# is right for a developer laptop and wrong for a gate. In CI a skip means the
# thing we are gating on did not run, so the preconditions are established here
# and asserted, and pytest's summary is held to ZERO skipped afterwards.
#
# `test_agent_task_e2e.py` is the reason for the fixture app. It drives the real
# CLIs through see -> act -> verify -> diagnose against a Compose screen, and it
# is the single test that measures whether this skill works as a skill. It skips
# unless the fixture app is installed, so without the app the most valuable test
# in the repo would quietly not run.
#
# Exit status, which emulator.yml reads from emulator-lane-diagnostics/:
#   0  pytest ran, at least one test passed, nothing skipped, failed or errored
#   1  anything else: a test failed or skipped, nothing passed, a precondition
#      was missing
#   3  the device was gone when pytest finished: "DEVICE LOST". The workflow
#      retries this status, and only this one, once: a new emulator process
#      on the same AVD (the runner keeps it; force-avd-creation is false).
# pytest's own status is never passed through: its 3 means "internal error",
# and must not read as a lost device and be retried.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APK="${REPO_ROOT}/tests/fixtures/scaffold/compose/app/build/outputs/apk/debug/app-debug.apk"
PACKAGE="com.example.composefixture"
DIAGNOSTICS="${REPO_ROOT}/emulator-lane-diagnostics/attempt-${LANE_ATTEMPT:-1}"

cd "${REPO_ROOT}"
mkdir -p "${DIAGNOSTICS}"
rm -f emulator-lane.log emulator-lane.summary

# The exit status goes to a file on every exit, so the workflow retries on what
# this script decided rather than on the runner action's pass/fail. On failure
# the host's view goes there too, captured HERE because the emulator is still
# up: once the runner action returns it has killed the emulator, and
# `adb devices` then says nothing about why the lane failed. Best-effort, each.
record_exit() {
  local status=$?
  echo "${status}" > "${DIAGNOSTICS}/exit-code"
  if [[ "${status}" -ne 0 ]]; then
    cp emulator-lane.log "${DIAGNOSTICS}/" 2>/dev/null || true
    timeout 30 adb devices -l > "${DIAGNOSTICS}/adb-devices.txt" 2>&1 || true
    pgrep -fl emulator > "${DIAGNOSTICS}/pgrep-emulator.txt" 2>&1 || true
    { sudo -n dmesg 2>&1 || dmesg 2>&1; } | tail -n 50 > "${DIAGNOSTICS}/dmesg-tail.txt" || true
  fi
}
trap record_exit EXIT

echo "::group::Device"
adb devices
adb wait-for-device
adb shell getprop ro.build.version.sdk
adb shell getprop ro.product.model
echo "::endgroup::"

echo "::group::Install the Compose fixture app"
# Built by the workflow's own step, before the emulator boots; only the install
# needs the device. The build used to run here, beside the booted emulator --
# that it was the stressor behind the lane's flakes is a hypothesis, not a
# finding.
if [[ ! -f "${APK}" ]]; then
  echo "ERROR: ${APK} does not exist; the workflow's build step did not produce it."
  exit 1
fi
adb install -r "${APK}"
echo "::endgroup::"

# Assert rather than hope: if this is missing, test_agent_task_e2e skips and the
# gate fails on the skip below -- this says why. `pm path` answers by exit
# status, 1 for a package that is not installed; `pm list packages <name>`
# exits 0 either way, and grepping it also matched any longer package name.
if ! adb shell pm path "${PACKAGE}" > /dev/null; then
  echo "ERROR: ${PACKAGE} is not installed after adb install."
  echo "The end-to-end agent-task test would skip, and this gate would fail"
  echo "without exercising the loop it exists to protect."
  exit 1
fi
echo "${PACKAGE} is installed."

echo "::group::pytest -m emulator"
# `two_devices` tests are deselected, not skipped: they need two attached
# devices and this lane boots one, so a skip there would fail the floor on
# every run. By marker rather than node id: a node id that stops matching
# excludes nothing and says nothing, and tests/test_packaging_contract.py
# checks every marker excluded here is registered and still on a test.
#
# The status is kept rather than left to `set -e`: tests that failed because the
# device went away must reach the device check below, not end the script here.
pytest_status=0
pytest tests/ -m "emulator and not two_devices" -v --color=no | tee emulator-lane.log || pytest_status=$?
echo "::endgroup::"

# pytest's final line, e.g. "== 23 passed, 2171 deselected in 76.37s (0:01:16) ==".
grep -E '^=+ .* in [0-9.]+s' emulator-lane.log | tail -n 1 > emulator-lane.summary || true
passed="$(grep -oE '[0-9]+ passed' emulator-lane.summary | grep -oE '[0-9]+' || true)"

# Judged BEFORE the tests. `live_device` resolves the serial once per session,
# so a device that dies after the first test makes what follows fail or error:
# that is a lost device (3, retried), not a failing test (1, never retried).
if [[ "$(timeout 30 adb get-state 2>/dev/null)" != "device" ]]; then
  echo "DEVICE LOST after ${passed:-0} passed: adb get-state no longer answers 'device'."
  tail -n 20 emulator-lane.log
  exit 3
fi

# The floor. A skip in the gated lane means a precondition failed, so the lane
# passes only on N passed with N >= 1 and no skipped, failed or error term.
# Read from pytest's own summary rather than a JUnit XML: no parser, no
# dependency, and nothing to get subtly wrong.
if ! grep -qE '[1-9][0-9]* passed' emulator-lane.summary || grep -qE '[0-9]+ (skipped|failed|errors?)' emulator-lane.summary; then
  echo "ERROR: the lane passes only on N passed (N >= 1) with nothing skipped, failed or errored."
  echo "pytest said: $(cat emulator-lane.summary)"
  tail -n 30 emulator-lane.log
  exit 1
fi

# A clean summary from a pytest that still failed: interrupted, or an internal
# error. Reported as 1 whatever pytest's own status was.
if [[ "${pytest_status}" -ne 0 ]]; then
  echo "ERROR: pytest exited ${pytest_status} although its summary reads clean."
  exit 1
fi

cat emulator-lane.summary
