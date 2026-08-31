"""Opt-in smoke tests against a real booted device.

Deselected by default (see ``addopts`` in pyproject.toml). Run with::

    pytest -m emulator

These assert **semantic floors**, not command shapes: "did the agent get a
usable answer", not "was the right flag passed". The unit suite already
over-indexes on command shape, and that is exactly why 470 of those tests
passed while three capabilities were inert.

Every assertion here is one an agent's task actually depends on. Keep them
cheap, order-independent, and non-destructive — they run against whatever
device happens to be attached.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest

pytestmark = pytest.mark.emulator

TIMEOUT = 60


def _adb(adb: str, serial: str, *args: str, timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    """Run a bounded adb command against an explicit serial."""
    return subprocess.run(
        [adb, "-s", serial, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_device_reports_an_api_level(adb: str, live_device: str):
    """Baseline: the device answers at all, and identifies itself."""
    result = _adb(adb, live_device, "shell", "getprop", "ro.build.version.sdk")
    assert result.stdout.strip().isdigit(), f"no API level: {result.stdout!r}"


def test_hierarchy_dump_returns_parseable_xml(adb: str, live_device: str):
    """The agent must be able to see the screen at all.

    Uses ``exec-out``: over ``adb shell`` the device allocates a pty and
    uiautomator writes only its status line, so the XML never reaches the host.
    """
    result = _adb(adb, live_device, "exec-out", "uiautomator", "dump", "/dev/tty")
    assert "<hierarchy" in result.stdout, f"no hierarchy in output: {result.stdout[:200]!r}"
    assert "</hierarchy>" in result.stdout, "hierarchy truncated"


def test_screen_mapper_returns_a_usable_answer(adb: str, live_device: str, scripts_dir):
    """R11 floor: an agent that sees zero elements cannot act.

    Note this deliberately does **not** check the exit code: R2 means
    screen_mapper exits 0 even when it serialises an error, so the return code
    carries no information. Assert on the payload instead.
    """
    result = subprocess.run(
        ["python3", str(scripts_dir / "screen_mapper.py"), "--serial", live_device, "--json"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    assert result.stdout.strip(), f"no output; stderr={result.stderr[:300]}"
    payload = json.loads(result.stdout)
    assert "error" not in payload, f"screen_mapper reported: {payload['error']}"
    assert payload, "screen_mapper returned an empty analysis"


def test_logcat_produces_lines_the_monitor_can_parse(adb: str, live_device: str):
    """A1 floor: the diagnostics path must actually yield parsed records.

    Asserts against the format log_monitor requests, so this test flips from
    failing to passing exactly when A1 is fixed.
    """
    from log_monitor import LogMonitor

    monitor = LogMonitor(device_serial=live_device)
    cmd = monitor.build_logcat_command()
    requested = cmd[cmd.index("-v") + 1]

    result = _adb(adb, live_device, "logcat", "-d", "-v", requested, "-t", "200")
    lines = [
        line
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("--------- beginning of")
    ]
    assert lines, "device produced no log lines"

    parsed = [monitor.parse_logcat_line(line) for line in lines]
    assert any(
        p is not None for p in parsed
    ), f"log_monitor requests '-v {requested}' but parsed 0 of {len(lines)} real lines"


def test_effective_display_geometry_is_reported(adb: str, live_device: str):
    """S9 floor: coordinates depend on reading the *effective* resolution."""
    result = _adb(adb, live_device, "shell", "wm", "size")
    assert re.search(r"Physical size: \d+x\d+", result.stdout), f"unexpected: {result.stdout!r}"


def test_omitting_serial_fails_loudly_when_multiple_devices_attached(adb: str):
    """R5 floor: ambiguity must not silently target an arbitrary device.

    Skips unless two devices are actually attached, so it is a no-op on a
    single-device machine rather than a false pass.
    """
    listing = subprocess.run(
        [adb, "devices"], capture_output=True, text=True, timeout=20, check=False
    )
    serials = [
        line.split()[0]
        for line in listing.stdout.splitlines()[1:]
        if line.strip() and line.split()[-1] == "device"
    ]
    if len(serials) < 2:
        pytest.skip("needs two attached devices")

    result = subprocess.run(
        [adb, "shell", "echo", "hi"], capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode != 0, "adb silently picked a device instead of refusing"
    assert "more than one device" in (result.stderr + result.stdout).lower()
