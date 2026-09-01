#!/usr/bin/env bash
#
# Run the device-backed test lane inside the emulator-runner action.
#
# The point of this script is that a SKIP must not read as a PASS. Every test in
# the emulator lane skips politely when its precondition is missing -- no
# device, fixture app not installed -- which is right for a developer laptop and
# wrong for a gate. In CI a skip means the thing we are gating on did not run,
# so the preconditions are established here and asserted, and the lane is
# checked afterwards for a non-zero test count.
#
# `test_agent_task_e2e.py` is the reason for the Gradle build below. It drives
# the real CLIs through see -> act -> verify -> diagnose against a Compose
# screen, and it is the single test that measures whether this skill works as a
# skill. It skips unless the fixture app is installed, so without the build the
# most valuable test in the repo would quietly not run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_APP="${REPO_ROOT}/tests/fixtures/scaffold/compose"
PACKAGE="com.example.composefixture"

echo "::group::Device"
adb devices
adb wait-for-device
adb shell getprop ro.build.version.sdk
adb shell getprop ro.product.model
echo "::endgroup::"

echo "::group::Build and install the Compose fixture app"
# Sources are committed; build output is gitignored, so this always builds.
gradle --project-dir "${COMPOSE_APP}" --no-daemon :app:installDebug
echo "::endgroup::"

# Assert rather than hope: if this is missing, test_agent_task_e2e skips and the
# gate passes without having tested anything.
if ! adb shell pm list packages | grep -q "${PACKAGE}"; then
  echo "ERROR: ${PACKAGE} is not installed after installDebug."
  echo "The end-to-end agent-task test would skip, and this gate would pass"
  echo "without exercising the loop it exists to protect."
  exit 1
fi
echo "${PACKAGE} is installed."

echo "::group::pytest -m emulator"
cd "${REPO_ROOT}"
pytest tests/ -m emulator -v | tee emulator-lane.log
echo "::endgroup::"

# A lane in which every test skipped is not a lane that passed. pytest reports
# that as success, so the summary line is checked for tests that actually ran.
# Read from pytest's own summary rather than a JUnit XML: no parser, no
# dependency, and nothing to get subtly wrong.
if ! grep -qE '[0-9]+ passed' emulator-lane.log; then
  echo "ERROR: no emulator test passed -- they all skipped, or none were collected."
  echo "This gate would otherwise be green without exercising a device at all."
  tail -20 emulator-lane.log
  exit 1
fi

grep -E '^=+ .*(passed|failed).* =+$' emulator-lane.log | tail -1
