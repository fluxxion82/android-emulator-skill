"""Four defects that returned a confident, false answer.

S1  `get_current_activity()` always returned None. It built an argv list
    containing a literal "|" and "grep", then ran it with shell=True -- which
    on POSIX executes only argv[0] (bare `adb`), discarding the rest. So
    `app_launcher --state` always reported "Foreground: No". It was also the
    only shell=True in the repo, against CLAUDE.md rule 4.

S3  The captured screenshot was dropped from the snapshot manifest.
    `capture_screenshot()` returns {"mode", "file_path", ...} and raises on
    failure -- there is no "success" key -- so `if result.get("success")` was
    always falsy and the PNG was written but never listed.

S5  `emulator_boot`'s already-booted check never matched. The emulator console
    appends "OK" to every reply, so `adb emu avd name` yields
    "Pixel_9\\r\\nOK\\r\\n" and .strip() gives "Pixel_9\\nOK". A second
    emulator was spawned for an AVD that was already running.

S6  `--logs 30s` returned 30 *lines*, not 30 seconds: the parsed duration was
    passed to `logcat -t`, where N means a line count. `log_monitor` already
    does this correctly with a timestamp; the two files disagreed.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime

# ---------------------------------------------------------------------------
# S1 — focused activity.
# ---------------------------------------------------------------------------


def test_no_shell_true_anywhere_in_the_skill(scripts_dir):
    """CLAUDE.md rule 4. This was the only violation; keep it that way.

    Parsed rather than grepped: several files legitimately mention shell=True in
    a comment explaining why they avoid it, and a substring search cannot tell
    that apart from a real call.
    """
    import ast

    offenders = []
    for path in scripts_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    offenders.append(f"{path.relative_to(scripts_dir)}:{node.lineno}")

    assert not offenders, f"shell=True call sites: {offenders}"


def test_focused_activity_parsed_from_real_dumpsys(recorded):
    """The parser must handle what `dumpsys window` actually prints."""
    from common.device_utils import parse_focused_activity

    activity = parse_focused_activity(recorded.text("dumpsys_window_focus"))
    assert activity == (
        "com.google.android.apps.nexuslauncher/"
        "com.google.android.apps.nexuslauncher.NexusLauncherActivity"
    )


def test_focused_activity_returns_none_when_absent():
    """No focus line must be a clean None, not a crash or a false match."""
    from common.device_utils import parse_focused_activity

    assert parse_focused_activity("") is None
    assert parse_focused_activity("mCurrentFocus=null") is None


def test_get_current_activity_does_not_use_a_shell_pipeline(monkeypatch):
    """The command must be a real argv list, with no shell metacharacters."""
    from common import adb_exec, device_utils

    captured = {}

    class _Result:
        stdout = ""
        stderr = ""
        returncode = 0

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)
    device_utils.get_current_activity("emulator-5554")

    assert captured["kwargs"].get("shell") is not True
    assert "|" not in captured["cmd"], f"shell pipeline in argv: {captured['cmd']}"
    assert "grep" not in captured["cmd"], "filtering belongs in Python, not a device grep"


def test_get_current_activity_returns_the_component(monkeypatch, recorded):
    """End to end against recorded device output."""
    from common import adb_exec, device_utils

    class _Result:
        stdout = recorded.text("dumpsys_window_focus")
        stderr = ""
        returncode = 0

    monkeypatch.setattr(adb_exec.subprocess, "run", lambda *_a, **_k: _Result())
    assert device_utils.get_current_activity("emulator-5554") is not None


# ---------------------------------------------------------------------------
# S3 — screenshot dropped from the manifest.
# ---------------------------------------------------------------------------


def test_capture_screenshot_has_no_success_key(tmp_path, monkeypatch):
    """Pin the real return shape, which the caller had guessed wrong."""
    # A real 1x1 PNG: the capture path opens it with PIL when resizing.
    import io as _io

    from PIL import Image as _Image

    from common import adb_exec, screenshot_utils

    buffer = _io.BytesIO()
    _Image.new("RGB", (4, 4)).save(buffer, format="PNG")
    png = buffer.getvalue()

    def _run(cmd, **_kwargs):
        # The pull step is what materialises the host-side file.
        if "pull" in cmd:
            pathlib.Path(cmd[-1]).write_bytes(png)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)
    target = tmp_path / "shot.png"

    result = screenshot_utils.capture_screenshot(None, output_path=str(target), size="full")
    assert (
        "success" not in result
    ), "a 'success' key now exists; the caller's check was written against it"
    assert "file_path" in result


def test_snapshot_lists_the_screenshot_it_captured(tmp_path, monkeypatch):
    """The PNG was written to disk but omitted from the manifest."""
    import app_state_capture

    monkeypatch.setattr(
        app_state_capture,
        "capture_screenshot",
        lambda *_a, **_k: {"mode": "file", "file_path": "screenshot.png", "size_bytes": 10},
    )
    monkeypatch.setattr(app_state_capture, "get_ui_hierarchy", lambda *_a, **_k: {"tag": "root"})
    monkeypatch.setattr(
        app_state_capture.adb_exec.subprocess,
        "run",
        lambda *_a, **_k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    capturer = app_state_capture.AppStateCapture(package="com.example.app")
    capturer.capture(output_dir=str(tmp_path), include_logs=False)

    # The manifest is written into the snapshot directory, not returned.
    summaries = list(tmp_path.rglob("snapshot-summary.json"))
    assert summaries, "no snapshot-summary.json written"
    artifacts = json.loads(summaries[0].read_text(encoding="utf-8"))["artifacts"]

    assert (
        "screenshot.png" in artifacts
    ), f"screenshot was captured but is missing from the manifest: {artifacts}"


# ---------------------------------------------------------------------------
# S5 — emulator console "OK" suffix.
# ---------------------------------------------------------------------------


def test_avd_name_reply_carries_the_console_ok_suffix(recorded):
    """Establish the premise from real output rather than asserting it."""
    raw = recorded.text("emu_avd_name")
    assert (
        raw.strip() != "Pixel_9"
    ), "fixture no longer shows the OK suffix; re-check whether S5 still applies"
    assert "OK" in raw


def test_avd_name_strips_the_console_ok_suffix(monkeypatch, recorded):
    """The parsed name must be the AVD name alone."""
    import emulator_boot

    class _Result:
        stdout = recorded.text("emu_avd_name")
        stderr = ""
        returncode = 0

    monkeypatch.setattr(emulator_boot.subprocess, "run", lambda *_a, **_k: _Result())

    booter = emulator_boot.EmulatorBooter(avd_name="Pixel_9")
    assert booter._get_avd_name_for_serial("emulator-5554") == "Pixel_9"


def test_avd_name_handles_an_empty_reply(monkeypatch):
    """A console that answers nothing must not yield a bogus name."""
    import emulator_boot

    class _Result:
        stdout = ""
        stderr = ""
        returncode = 0

    monkeypatch.setattr(emulator_boot.subprocess, "run", lambda *_a, **_k: _Result())
    booter = emulator_boot.EmulatorBooter(avd_name="Pixel_9")
    assert booter._get_avd_name_for_serial("emulator-5554") in (None, "")


# ---------------------------------------------------------------------------
# S6 — --logs DURATION must mean time, not lines.
# ---------------------------------------------------------------------------


def test_log_window_uses_a_timestamp_not_a_line_count(monkeypatch, tmp_path):
    """`logcat -t N` means N lines; a duration needs a timestamp."""
    import app_state_capture

    captured = []

    def _run(cmd, **_kwargs):
        captured.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", _run)

    capturer = app_state_capture.AppStateCapture(package="com.example.app")
    capturer._capture_logs(tmp_path / "logs.txt", "30s", 200)

    logcat_cmds = [c for c in captured if "logcat" in c]
    assert logcat_cmds, "no logcat command issued"
    cmd = logcat_cmds[0]
    value = cmd[cmd.index("-t") + 1]

    assert not value.isdigit(), (
        f"-t received {value!r}, a bare number, which logcat reads as a LINE "
        f"COUNT; a duration must be expressed as a timestamp"
    )
    # logcat's timestamp form is "MM-DD HH:MM:SS.mmm".
    datetime.strptime(value, "%m-%d %H:%M:%S.%f")


def test_log_window_timestamp_reflects_the_requested_duration(monkeypatch, tmp_path):
    """A 1m window must start further back than a 30s one."""
    import app_state_capture

    captured = []
    monkeypatch.setattr(
        app_state_capture.adb_exec.subprocess,
        "run",
        lambda cmd, **_k: (
            captured.append(cmd),
            type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        )[1],
    )

    capturer = app_state_capture.AppStateCapture(package="com.example.app")
    capturer._capture_logs(tmp_path / "a.txt", "30s", 200)
    capturer._capture_logs(tmp_path / "b.txt", "5m", 200)

    stamps = [c[c.index("-t") + 1] for c in captured if "logcat" in c and "-t" in c]
    assert len(stamps) == 2
    parsed = [datetime.strptime(s, "%m-%d %H:%M:%S.%f") for s in stamps]
    assert parsed[1] < parsed[0], "the longer window should start earlier"
