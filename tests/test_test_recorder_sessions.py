"""test_recorder as an agent actually drives it: one session, many processes.

The defect being fixed was total. `main()` built a `TestRecorder`, dropped it
on the floor, and printed usage advice; the recording methods could only be
reached by importing the class into a Python program that then had to stay
alive for the whole test. An agent calls the script once per step, so every
invocation started from nothing and the CLI recorded nothing, ever.

So the load-bearing test here is `test_two_steps_from_separate_recorder_...`:
four independent `TestRecorder` instances (start, step, step, stop), each one
resolving its own store from the environment the way four separate processes
would, and one report at the end containing both steps.

The rest pins the contracts this repo has been burned by:

  * the UI hierarchy shape — fields live under ``node["attributes"]`` as
    strings, so the summariser is exercised against the *recorded* dump
    (`uiautomator_current_screen.xml`) converted by the same `_xml_to_dict`
    production uses, never against a hand-written dict;
  * `capture_screenshot` raising rather than returning ``{"success": ...}``;
  * free text reaching the device shell (`x;id`) being quoted;
  * user state landing under ``~/.android-emulator-skill``, not inside the
    installed plugin package.

adb is never called: the four device-touching entry points are monkeypatched on
the `test_recorder` module.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import test_recorder
from test_recorder import RecorderError, RecorderSessionStore, TestRecorder

from common import cli
from common.device_utils import _xml_to_dict

SCRIPTS_DIR = Path(test_recorder.__file__).resolve().parent

# A real 1x1 PNG. Nothing in the recorder decodes it; it exists so the artifact
# on disk is a genuine file with genuine bytes.
FAKE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

ACTIVITY = "com.android.settings/com.android.settings.Settings"


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """Redirect the storage root, so the suite never touches the real home."""
    fake_home = tmp_path / "home"
    monkeypatch.setenv("ANDROID_EMU_RECORDER_HOME", str(fake_home))
    return fake_home


@pytest.fixture
def real_hierarchy(recorded) -> dict:
    """The recorded uiautomator dump, in the shape production hands around.

    Converted by `device_utils._xml_to_dict` — the same function
    `get_ui_hierarchy` uses — so the summariser is reading a real device's
    attribute names and string values, not an invented dict.
    """
    return _xml_to_dict(ET.fromstring(recorded.text("uiautomator_current_screen")))


@pytest.fixture
def device(monkeypatch, real_hierarchy):
    """Mock the four device-touching calls and record what they were given."""
    calls: dict = {"screenshots": [], "adb": []}

    def _capture(serial=None, output_path=None, size="half", inline=False, **_kwargs):
        # capture_screenshot returns {"mode", "file_path", ...} and RAISES on
        # failure. There is no "success" key -- checking for one dropped the
        # screenshot from the manifest in a previous defect.
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(FAKE_PNG)
        calls["screenshots"].append({"serial": serial, "path": str(path), "size": size})
        return {
            "mode": "file",
            "file_path": str(path),
            "size_bytes": len(FAKE_PNG),
            "width": 540,
            "height": 1212,
            "size_preset": size,
        }

    def _adb_run(cmd, **kwargs):
        calls["adb"].append({"cmd": cmd, "kwargs": kwargs})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(test_recorder, "capture_screenshot", _capture)
    monkeypatch.setattr(test_recorder, "get_ui_hierarchy", lambda serial=None: real_hierarchy)
    monkeypatch.setattr(test_recorder, "get_current_activity", lambda serial=None: ACTIVITY)
    monkeypatch.setattr(
        test_recorder, "resolve_device_identifier", lambda identifier: identifier or "emulator-5554"
    )
    monkeypatch.setattr(test_recorder.subprocess, "run", _adb_run)
    return calls


def _fresh() -> TestRecorder:
    """A recorder as a *new process* would build one: store resolved from env."""
    return TestRecorder()


def _cli(monkeypatch, *argv: str) -> int:
    """Run the real CLI entry point -- through common.cli, as the script does --
    and return its exit status."""
    monkeypatch.setattr(sys, "argv", ["test_recorder.py", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.run(test_recorder.main)
    return 0 if exc.value.code is None else exc.value.code


# ---------------------------------------------------------------------------
# Where sessions live.
# ---------------------------------------------------------------------------


def test_sessions_are_not_stored_inside_the_installed_package(home, device):
    """The skill ships as a plugin; writing user state into it was a real bug."""
    before = {p.name for p in SCRIPTS_DIR.iterdir()}

    meta = _fresh().start("Login flow")
    _fresh().step("Open the app")

    assert {p.name for p in SCRIPTS_DIR.iterdir()} == before
    session_dir = _fresh().store.session_dir(meta.session_id)
    assert home in session_dir.parents, f"{session_dir} is not under the user home"


def test_explicit_base_dir_overrides_the_env_root(tmp_path, device, monkeypatch):
    """The constructor argument is what lets a caller point elsewhere."""
    monkeypatch.delenv("ANDROID_EMU_RECORDER_HOME", raising=False)
    store = RecorderSessionStore(base_dir=tmp_path / "elsewhere")
    meta = TestRecorder(store).start("Login flow")
    assert (tmp_path / "elsewhere" / meta.session_id / "meta.json").exists()


def test_session_id_shape(home, device):
    """rec-YYYYMMDD-HHMMSS-XXXX; the hex suffix avoids same-second collisions."""
    session_id = _fresh().start("Login flow").session_id
    assert test_recorder.SESSION_ID_RE.match(session_id), session_id
    assert session_id.startswith("rec-")


def test_start_builds_the_session_tree(home, device):
    meta = _fresh().start("Login flow", app_name="MyApp", screenshot_size="quarter")
    store = _fresh().store

    assert store.steps_path(meta.session_id).exists()
    assert store.screenshots_dir(meta.session_id).is_dir()
    assert store.ui_dumps_dir(meta.session_id).is_dir()

    # meta.json must be complete, parseable JSON (atomic tmp+replace).
    payload = json.loads((store.session_dir(meta.session_id) / "meta.json").read_text())
    assert payload["name"] == "Login flow"
    assert payload["app_name"] == "MyApp"
    assert payload["screenshot_size"] == "quarter"
    assert payload["status"] == "recording"
    assert payload["serial"] == "emulator-5554"


# ---------------------------------------------------------------------------
# The point of the rebuild: state survives between invocations.
# ---------------------------------------------------------------------------


def test_two_steps_from_separate_recorder_instances_reach_one_report(home, device):
    """Start, two steps, and stop — four processes, one session, one report."""
    session_id = _fresh().start("Login flow", app_name="MyApp").session_id

    # Each of these is what a separate `python test_recorder.py --step ...`
    # invocation does: build a recorder from scratch, find the open session.
    first = _fresh().step("Open the app", screen_name="Home")
    second = _fresh().step("Tap sign in", assertion="Login form shown")

    result = _fresh().stop()

    assert (first["number"], second["number"]) == (1, 2)
    assert result["session_id"] == session_id
    assert result["steps"] == 2

    report = Path(result["report"]).read_text(encoding="utf-8")
    assert "Open the app" in report
    assert "Tap sign in" in report
    assert "Login flow" in report
    assert "✓ Login form shown" in report

    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert [step["description"] for step in manifest["steps"]] == [
        "Open the app",
        "Tap sign in",
    ]
    assert manifest["session"]["status"] == "stopped"

    # Both steps left real artifacts on disk, named for their step number.
    screenshots = sorted(p.name for p in Path(manifest["artifacts"]["screenshots_dir"]).iterdir())
    assert screenshots == ["001-open-the-app.png", "002-tap-sign-in.png"]
    assert len(list(Path(manifest["artifacts"]["ui_dumps_dir"]).iterdir())) == 2


def test_step_numbering_comes_from_the_steps_file_not_from_meta(home, device):
    """A step that landed must not be renumbered over if meta lagged behind."""
    meta = _fresh().start("Login flow")
    _fresh().step("Open the app")

    # Simulate an invocation that appended its step and then died before
    # updating meta.
    store = _fresh().store
    stale = store.load_meta(meta.session_id)
    stale.step_count = 0
    store.write_meta(stale)

    assert _fresh().step("Tap sign in")["number"] == 2
    assert len(store.read_steps(meta.session_id).steps) == 2


def test_step_targets_the_newest_recording_session(home, device):
    """With two sessions open, an unqualified --step goes to the newer one."""
    older = _fresh().start("First run").session_id
    newer = _fresh().start("Second run").session_id
    assert older != newer

    _fresh().step("Open the app")

    store = _fresh().store
    assert len(store.read_steps(newer).steps) == 1
    assert store.read_steps(older).steps == []


def test_explicit_session_id_overrides_the_active_one(home, device):
    older = _fresh().start("First run").session_id
    _fresh().start("Second run")

    _fresh().step("Open the app", session_id=older)

    assert len(_fresh().store.read_steps(older).steps) == 1


def test_step_without_a_session_explains_how_to_start_one(home, device):
    with pytest.raises(RecorderError, match="--start"):
        _fresh().step("Open the app")


def test_step_on_a_stopped_session_is_refused(home, device):
    session_id = _fresh().start("Login flow").session_id
    _fresh().step("Open the app")
    _fresh().stop()

    with pytest.raises(RecorderError, match="stopped"):
        _fresh().step("Tap sign in", session_id=session_id)


def test_stopping_twice_is_refused(home, device):
    session_id = _fresh().start("Login flow").session_id
    _fresh().stop()
    with pytest.raises(RecorderError, match="already stopped"):
        _fresh().stop(session_id=session_id)


def test_stop_records_a_failed_run(home, device):
    _fresh().start("Login flow")
    _fresh().step("Tap sign in", assertion="Login form shown", assertion_passed=False)
    result = _fresh().stop(passed=False)

    assert result["passed"] is False
    report = Path(result["report"]).read_text(encoding="utf-8")
    assert "✗ FAILED" in report
    assert "✗ Login form shown" in report


# ---------------------------------------------------------------------------
# Hierarchy contract: fields live under node["attributes"], as strings.
# ---------------------------------------------------------------------------


def test_summary_counts_match_the_recorded_dump(recorded, real_hierarchy):
    """Counts are checked against the raw XML, not against a remembered number."""
    raw = recorded.text("uiautomator_current_screen")
    summary = TestRecorder.summarize_hierarchy(real_hierarchy)

    # The <hierarchy> root is a wrapper, not an element.
    assert summary["elements"] == raw.count("<node ")
    # `long-clickable="true"` contains `clickable="true"`; a substring count
    # would silently inflate this.
    assert summary["clickable"] == len(re.findall(r'(?<!-)clickable="true"', raw))
    assert summary["with_text"] == len(re.findall(r'(?<![-\w])text="([^"]+)"', raw))
    assert summary["package"] == "com.android.settings"


def test_summary_labels_are_real_on_screen_text(real_hierarchy):
    """Labels must be readable strings from the device, capped for the report."""
    summary = TestRecorder.summarize_hierarchy(real_hierarchy)
    assert "Network & internet" in summary["labels"]
    assert len(summary["labels"]) <= test_recorder.MAX_STEP_LABELS
    assert summary["label_total"] >= len(summary["labels"])


def test_summary_reads_attributes_not_top_level_keys():
    """`node.get("text")` returns nothing on a real node; the decoy proves it.

    Not tool output: a probe shaped like a node whose top-level keys disagree
    with its attributes, so a summariser reading the wrong level is visible.
    """
    decoy = {
        "tag": "node",
        "text": "WRONG-LEVEL",
        "clickable": "true",
        "attributes": {"text": "Sign in", "clickable": "false", "package": "com.example.app"},
        "children": [],
    }
    summary = TestRecorder.summarize_hierarchy(decoy)
    assert summary["labels"] == ["Sign in"]
    assert summary["clickable"] == 0
    assert summary["package"] == "com.example.app"


def test_summary_of_a_missing_hierarchy_is_empty_not_a_crash():
    summary = TestRecorder.summarize_hierarchy(None)
    assert summary["elements"] == 0
    assert summary["labels"] == []


def test_step_writes_the_whole_hierarchy_to_its_ui_dump(home, device, real_hierarchy):
    """The summary is a preview; the dump on disk is the evidence."""
    _fresh().start("Login flow")
    step = _fresh().step("Open the app")

    dumped = json.loads(Path(step["ui_dump"]).read_text(encoding="utf-8"))
    assert dumped == real_hierarchy
    assert step["screen"]["elements"] > 0


# ---------------------------------------------------------------------------
# Screenshot contract: raises on failure, and has no "success" key.
# ---------------------------------------------------------------------------


def test_screenshot_path_is_recorded_without_any_success_key(home, device):
    """S3 again: `if result.get("success")` dropped a screenshot that existed."""
    _fresh().start("Login flow")
    step = _fresh().step("Open the app")

    assert step["screenshot"], "screenshot missing from the step record"
    assert Path(step["screenshot"]).exists()
    assert device["screenshots"][0]["size"] == "half"


def test_screenshot_failure_is_surfaced_and_the_step_still_lands(home, device, monkeypatch):
    """A raising capture must not lose the step, nor pass silently."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("Failed to capture screenshot: device offline")

    _fresh().start("Login flow")
    monkeypatch.setattr(test_recorder, "capture_screenshot", _boom)
    step = _fresh().step("Open the app")

    assert step["screenshot"] is None
    assert any("device offline" in error for error in step["errors"])
    # The hierarchy still landed, so the step is worth keeping.
    assert step["captured"] is True
    assert step["ui_dump"]

    stopped = _fresh().stop()
    assert stopped["capture_failures"] == 1
    assert "device offline" in Path(stopped["report"]).read_text(encoding="utf-8")


def test_a_step_that_captures_nothing_reports_failure(home, device, monkeypatch):
    """Both captures failing means the step recorded nothing; say so."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("device offline")

    _fresh().start("Login flow")
    monkeypatch.setattr(test_recorder, "capture_screenshot", _boom)
    monkeypatch.setattr(test_recorder, "get_ui_hierarchy", _boom)

    step = _fresh().step("Open the app")
    assert step["captured"] is False
    assert len(step["errors"]) == 2


# ---------------------------------------------------------------------------
# Device-shell quoting, and a bounded adb call.
# ---------------------------------------------------------------------------


def test_step_description_cannot_run_a_second_command_on_the_device(home, device):
    """The step marker carries free text from argv into `adb shell log`."""
    _fresh().start("Login flow")
    _fresh().step("x;id")

    payload = device["adb"][0]["cmd"][-1]
    assert "x;id" in payload, "the description should still be present, just inert"
    assert not payload.endswith(" x;id"), f"unquoted payload reached the device: {payload!r}"
    assert payload.startswith("'") or "\\;" in payload, payload


def test_marker_call_is_bounded_and_never_uses_a_shell(home, device):
    """An unbounded adb call wedges every later call on the same connection."""
    _fresh().start("Login flow")
    _fresh().step("Open the app")

    call = device["adb"][0]
    assert call["kwargs"].get("timeout"), "adb call has no timeout"
    assert call["kwargs"].get("shell") is not True
    assert "check" in call["kwargs"], "subprocess.run needs an explicit check="
    assert call["cmd"][0] == "adb"
    assert call["cmd"][1:3] == ["-s", "emulator-5554"]


def test_marker_failure_does_not_abort_the_step(home, device, monkeypatch):
    """`log` may not exist on every device; the step is still worth recording."""

    def _timeout(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

    _fresh().start("Login flow")
    monkeypatch.setattr(test_recorder.subprocess, "run", _timeout)

    step = _fresh().step("Open the app")
    assert step["marker_logged"] is False
    assert step["captured"] is True


# ---------------------------------------------------------------------------
# Untrusted strings that become paths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    ["../../etc/passwd", "../outside", "a/b/c", "....//....//x"],
)
def test_step_artifacts_stay_inside_the_session_directory(home, device, description):
    """A description is joined onto a path; the old slug only replaced spaces."""
    session_id = _fresh().start("Login flow").session_id
    step = _fresh().step(description)

    session_dir = _fresh().store.session_dir(session_id).resolve()
    for key in ("screenshot", "ui_dump"):
        artifact = Path(step[key]).resolve()
        assert session_dir in artifact.parents, f"{key} escaped to {artifact}"


def test_slug_never_contains_path_separators():
    assert "/" not in TestRecorder._slugify("../../etc/passwd")
    assert ".." not in TestRecorder._slugify("../../etc/passwd")
    # An all-punctuation description still needs a usable filename.
    assert TestRecorder._slugify("...") == "step"
    assert len(TestRecorder._slugify("x" * 200)) <= test_recorder.STEP_NAME_MAXLEN


@pytest.mark.parametrize(
    "session_id",
    ["../../../etc", "rec-x", "", "rec-20260101-000000-zzzz", "rec-20260101-000000-abcd/../.."],
)
def test_a_traversing_session_id_is_rejected(home, device, session_id):
    """The id arrives from argv and is joined onto the storage root."""
    victim = home / "test-sessions" / "keepme.txt"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("not a session", encoding="utf-8")

    with pytest.raises(RecorderError):
        _fresh().get_details(session_id)
    assert victim.exists()


# ---------------------------------------------------------------------------
# Retrieval.
# ---------------------------------------------------------------------------


def test_list_returns_sessions_newest_first(home, device):
    first = _fresh().start("First run").session_id
    second = _fresh().start("Second run").session_id
    _fresh().step("Open the app")
    _fresh().stop()

    listed = _fresh().list_sessions()
    # Both sessions start in the same millisecond here, which is exactly the
    # tie --step resolves against.
    assert [entry["session_id"] for entry in listed] == [second, first]
    assert listed[0]["status"] == "stopped"
    assert listed[0]["steps"] == 1
    assert listed[1]["status"] == "recording"


def test_get_details_returns_the_stored_steps(home, device):
    session_id = _fresh().start("Login flow").session_id
    _fresh().step("Open the app")
    _fresh().stop()

    details = _fresh().get_details(session_id)
    assert details["session"]["session_id"] == session_id
    assert [step["description"] for step in details["steps"]] == ["Open the app"]
    assert Path(details["report"]).exists()


def test_get_details_on_an_unknown_session_says_so(home, device):
    with pytest.raises(RecorderError, match="No such session"):
        _fresh().get_details("rec-20260101-000000-abcd")


# ---------------------------------------------------------------------------
# Housekeeping — session directories hold PNGs.
# ---------------------------------------------------------------------------


def test_expired_sessions_are_pruned_on_start(home, device):
    old = _fresh().start("Old run").session_id
    store = _fresh().store
    meta = store.load_meta(old)
    meta.started_at_ms -= 1000 * 60 * 60 * 24 * 30  # 30 days ago
    store.write_meta(meta)

    fresh_id = _fresh().start("New run").session_id

    remaining = {entry["session_id"] for entry in _fresh().list_sessions()}
    assert remaining == {fresh_id}


def test_clear_older_than_keeps_recent_sessions(home, device):
    old = _fresh().start("Old run").session_id
    store = _fresh().store
    meta = store.load_meta(old)
    meta.started_at_ms -= 1000 * 60 * 60 * 5  # 5 hours ago
    store.write_meta(meta)
    recent = _fresh().start("Recent run").session_id

    assert _fresh().clear(older_than="1h") == 1
    assert [entry["session_id"] for entry in _fresh().list_sessions()] == [recent]


def test_clear_without_a_filter_removes_everything(home, device):
    _fresh().start("First run")
    _fresh().start("Second run")
    assert _fresh().clear() == 2
    assert _fresh().list_sessions() == []


def test_clear_rejects_a_malformed_duration(home, device):
    with pytest.raises(RecorderError, match="Invalid duration"):
        _fresh().clear(older_than="soon")


# ---------------------------------------------------------------------------
# The CLI itself — the half that used to do nothing.
# ---------------------------------------------------------------------------


def test_cli_start_then_step_then_stop_records_everything(home, device, monkeypatch, capsys):
    """End to end through `main()`, which previously recorded nothing at all."""
    assert _cli(monkeypatch, "--start", "Login flow") == 0
    session_id = capsys.readouterr().out.strip()
    assert test_recorder.SESSION_ID_RE.match(session_id), session_id

    assert _cli(monkeypatch, "--step", "Open the app") == 0
    step_line = capsys.readouterr().out
    assert "[1] Open the app" in step_line
    assert "elements" in step_line

    assert _cli(monkeypatch, "--step", "Tap sign in", "--assert", "Form shown") == 0
    assert "✓ [2] Tap sign in" in capsys.readouterr().out

    assert _cli(monkeypatch, "--stop") == 0
    stop_out = capsys.readouterr().out
    assert "2 steps" in stop_out

    report = _fresh().store.report_path(session_id).read_text(encoding="utf-8")
    assert "Open the app" in report
    assert "Tap sign in" in report


def test_cli_step_json_carries_the_step_record(home, device, monkeypatch, capsys):
    _cli(monkeypatch, "--start", "Login flow")
    capsys.readouterr()

    _cli(monkeypatch, "--step", "Open the app", "--json")
    step = json.loads(capsys.readouterr().out)
    assert step["number"] == 1
    assert step["screen"]["elements"] > 0
    assert step["activity"] == ACTIVITY


def test_cli_stop_failed_exits_nonzero(home, device, monkeypatch, capsys):
    _cli(monkeypatch, "--start", "Login flow")
    _cli(monkeypatch, "--step", "Open the app")
    capsys.readouterr()

    assert _cli(monkeypatch, "--stop", "--failed") == 1
    assert "✗ FAILED" in capsys.readouterr().out


def test_cli_step_without_a_session_fails_loudly(home, device, monkeypatch, capsys):
    assert _cli(monkeypatch, "--step", "Open the app") == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: "), err


def test_cli_step_that_captures_nothing_exits_nonzero(home, device, monkeypatch, capsys):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("device offline")

    _cli(monkeypatch, "--start", "Login flow")
    monkeypatch.setattr(test_recorder, "capture_screenshot", _boom)
    monkeypatch.setattr(test_recorder, "get_ui_hierarchy", _boom)
    capsys.readouterr()

    assert _cli(monkeypatch, "--step", "Open the app") == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err, err
    assert "device offline" in err


def test_cli_list_and_get_details_are_machine_readable(home, device, monkeypatch, capsys):
    _cli(monkeypatch, "--start", "Login flow")
    session_id = capsys.readouterr().out.strip()
    _cli(monkeypatch, "--step", "Open the app")
    capsys.readouterr()

    _cli(monkeypatch, "--list", "--json")
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["session_id"] == session_id
    assert listed[0]["steps"] == 1

    _cli(monkeypatch, "--get-details", session_id, "--json")
    details = json.loads(capsys.readouterr().out)
    assert details["steps"][0]["description"] == "Open the app"


def test_cli_clear_reports_what_it_deleted(home, device, monkeypatch, capsys):
    _cli(monkeypatch, "--start", "Login flow")
    capsys.readouterr()

    assert _cli(monkeypatch, "--clear", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": 1}


# ---------------------------------------------------------------------------
# A damaged steps file is never silent.
#
# `read_steps` used to drop any line that would not parse and say nothing: a
# session whose only step was cut short mid-write stopped as a zero-step report,
# "✓ PASSED", exit 0. A recording cannot be re-run -- the device has moved on --
# so what can still be read is kept, and what cannot is counted and said: in
# `--stop`, `--get-details` and `--list`, text and JSON, and in `--stop`'s exit
# status.
#
# The damage below is always made from lines the recorder itself wrote, never
# from a hand-written record.
# ---------------------------------------------------------------------------


def _steps_file(session_id: str) -> Path:
    return _fresh().store.steps_path(session_id)


def _cut_last_line_short(path: Path) -> None:
    """What an append cut short leaves behind: half a record and no newline."""
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:-1]) + lines[-1][: len(lines[-1]) // 2])


def _corrupt_line(path: Path, index: int) -> None:
    """Halve one line in place, keeping its newline: a corrupt middle record."""
    lines = path.read_bytes().splitlines(keepends=True)
    lines[index] = lines[index][: len(lines[index]) // 2] + b"\n"
    path.write_bytes(b"".join(lines))


def _record(monkeypatch, capsys, *descriptions: str) -> str:
    """Start a session and record one step per description, through the CLI."""
    assert _cli(monkeypatch, "--start", "Login flow") == 0
    session_id = capsys.readouterr().out.strip()
    for description in descriptions:
        assert _cli(monkeypatch, "--step", description) == 0
    capsys.readouterr()
    return session_id


def test_stop_after_the_only_step_was_cut_short_is_not_a_clean_pass(
    home, device, monkeypatch, capsys
):
    """The reported defect: zero-step report, "PASSED", exit 0."""
    session_id = _record(monkeypatch, capsys, "Open the app")
    steps_file = _steps_file(session_id)
    _cut_last_line_short(steps_file)

    assert _cli(monkeypatch, "--stop") == 1
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out, out
    assert "1 unreadable line" in out, out
    assert str(steps_file) in out, out

    report = _fresh().store.report_path(session_id).read_text(encoding="utf-8")
    assert "INCOMPLETE" in report
    assert "No steps were recorded" not in report


def test_stop_json_counts_what_a_cut_short_write_lost(home, device, monkeypatch, capsys):
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in")
    steps_file = _steps_file(session_id)
    _cut_last_line_short(steps_file)

    assert _cli(monkeypatch, "--stop", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["passed"] is True  # the caller's verdict is kept, not rewritten
    assert result["incomplete"] is True
    assert result["unreadable_lines"] == 1
    assert result["lost_steps"] == 1
    assert result["steps"] == 1
    assert result["steps_file"] == str(steps_file)

    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["incomplete"] is True
    assert manifest["unreadable_lines"] == 1
    assert manifest["lost_steps"] == 1
    assert [step["number"] for step in manifest["steps"]] == [1]


def test_a_step_after_a_cut_short_line_takes_a_new_number_and_is_readable(
    home, device, monkeypatch, capsys
):
    """The lost line's number is not handed out again, and the next record does
    not land on the end of the fragment -- which would lose that one too."""
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in")
    _cut_last_line_short(_steps_file(session_id))

    assert _cli(monkeypatch, "--step", "Submit", "--json") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["number"] == 3
    assert "incomplete" in captured.err.lower(), captured.err

    details = _fresh().get_details(session_id)
    assert [step["number"] for step in details["steps"]] == [1, 3]
    assert details["unreadable_lines"] == 1
    assert details["lost_steps"] == 1


def test_a_corrupt_middle_line_is_counted_and_its_neighbours_kept(
    home, device, monkeypatch, capsys
):
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in", "Submit")
    steps_file = _steps_file(session_id)
    _corrupt_line(steps_file, 1)

    assert _cli(monkeypatch, "--step", "Home shown", "--json") == 0
    assert json.loads(capsys.readouterr().out)["number"] == 4

    assert _cli(monkeypatch, "--get-details", session_id, "--json") == 0
    details = json.loads(capsys.readouterr().out)
    assert [step["number"] for step in details["steps"]] == [1, 3, 4]
    assert details["incomplete"] is True
    assert details["unreadable_lines"] == 1
    assert details["lost_steps"] == 1
    assert details["steps_file"] == str(steps_file)

    assert _cli(monkeypatch, "--get-details", session_id) == 0
    text = capsys.readouterr().out
    assert "1 unreadable line" in text, text
    assert str(steps_file) in text, text

    assert _cli(monkeypatch, "--list", "--json") == 0
    listed = json.loads(capsys.readouterr().out)[0]
    assert listed["incomplete"] is True
    assert listed["unreadable_lines"] == 1
    assert listed["lost_steps"] == 1
    assert listed["steps"] == 3

    assert _cli(monkeypatch, "--list") == 0
    line = capsys.readouterr().out
    assert "INCOMPLETE" in line, line
    assert "1 unreadable line" in line, line


def test_a_stopped_session_still_says_it_is_incomplete(home, device, monkeypatch, capsys):
    """The disclosure outlives `--stop`: it is read back from the session, not
    only printed once."""
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in", "Submit")
    _corrupt_line(_steps_file(session_id), 1)
    assert _cli(monkeypatch, "--stop") == 1
    capsys.readouterr()

    report = _fresh().store.report_path(session_id).read_text(encoding="utf-8")
    assert "### 1. Open the app" in report
    assert "### 3. Submit" in report
    assert "INCOMPLETE" in report

    assert _cli(monkeypatch, "--list", "--json") == 0
    listed = json.loads(capsys.readouterr().out)[0]
    assert (listed["status"], listed["incomplete"], listed["unreadable_lines"]) == (
        "stopped",
        True,
        1,
    )
    assert _cli(monkeypatch, "--get-details", session_id, "--json") == 0
    assert json.loads(capsys.readouterr().out)["lost_steps"] == 1


def test_an_empty_steps_file_before_any_step_is_a_whole_recording(
    home, device, monkeypatch, capsys
):
    """`--start` leaves the file empty: nothing recorded is not something lost."""
    session_id = _record(monkeypatch, capsys)
    assert _steps_file(session_id).read_bytes() == b""

    assert _cli(monkeypatch, "--list", "--json") == 0
    listed = json.loads(capsys.readouterr().out)[0]
    assert (listed["incomplete"], listed["unreadable_lines"], listed["lost_steps"]) == (
        False,
        0,
        0,
    )
    assert _cli(monkeypatch, "--step", "Open the app", "--json") == 0
    assert json.loads(capsys.readouterr().out)["number"] == 1

    assert _cli(monkeypatch, "--stop", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["incomplete"], result["unreadable_lines"], result["lost_steps"]) == (
        False,
        0,
        0,
    )


def test_a_steps_file_emptied_after_steps_were_recorded_is_disclosed(
    home, device, monkeypatch, capsys
):
    """No line is unreadable because no line is left; meta still counts two."""
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in")
    _steps_file(session_id).write_bytes(b"")

    assert _cli(monkeypatch, "--step", "Submit", "--json") == 0
    assert json.loads(capsys.readouterr().out)["number"] == 3

    assert _cli(monkeypatch, "--stop", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["incomplete"] is True
    assert result["unreadable_lines"] == 0
    assert result["lost_steps"] == 2
    assert result["steps"] == 1

    # ...and still after the stop, which used to reset the count to what parsed.
    assert _cli(monkeypatch, "--get-details", session_id, "--json") == 0
    assert json.loads(capsys.readouterr().out)["lost_steps"] == 2


def test_stopping_with_every_step_lost_does_not_say_no_steps_were_recorded(
    home, device, monkeypatch, capsys
):
    session_id = _record(monkeypatch, capsys, "Open the app")
    _steps_file(session_id).write_bytes(b"")

    assert _cli(monkeypatch, "--stop") == 1
    assert "INCOMPLETE" in capsys.readouterr().out
    report = _fresh().store.report_path(session_id).read_text(encoding="utf-8")
    assert "No steps were recorded" not in report


@pytest.mark.parametrize(
    "junk",
    [b"null\n", b"[]\n", b'{"number": 2}\n', b"\xff\xfe\n"],
    ids=["json-null", "json-array", "object-without-description", "not-utf8"],
)
def test_a_line_that_is_not_a_step_record_is_unreadable_not_a_crash(
    home, device, monkeypatch, capsys, junk
):
    """Valid JSON that is not a step used to reach the report writer as one."""
    session_id = _record(monkeypatch, capsys, "Open the app")
    with open(_steps_file(session_id), "ab") as handle:
        handle.write(junk)

    assert _cli(monkeypatch, "--stop", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert (result["steps"], result["unreadable_lines"], result["lost_steps"]) == (1, 1, 1)


def test_a_whole_recording_is_not_called_incomplete(home, device, monkeypatch, capsys):
    """The other direction: the new check must not cry wolf, including when
    meta lags the file (an invocation that died after its append)."""
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in")
    store = _fresh().store
    stale = store.load_meta(session_id)
    stale.step_count = 1
    store.write_meta(stale)

    assert _cli(monkeypatch, "--get-details", session_id) == 0
    assert "ncomplete" not in capsys.readouterr().out

    assert _cli(monkeypatch, "--stop", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["steps"], result["incomplete"], result["lost_steps"]) == (2, False, 0)
    assert "INCOMPLETE" not in Path(result["report"]).read_text(encoding="utf-8")

    assert _cli(monkeypatch, "--list") == 0
    assert "INCOMPLETE" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Lines that read as JSON and are still not this session's next step: a copy of
# another line, a record whose number is not a step number, a record whose
# fields are not what the report writer indexes. Each is edited from a line the
# recorder wrote.
# ---------------------------------------------------------------------------

_GONE = object()
# Half of a surrogate pair: valid in a JSON escape, a `str` once parsed, and
# not text any file or terminal can be handed.
_LONE_SURROGATE = "\ud83d"


def _rewrite_line(path: Path, index: int, key: str, value: object) -> None:
    """Change one field of one recorded line, leaving it valid JSON.

    Written as ASCII with escapes, so the line can carry what a UTF-8 file
    cannot hold raw: a lone surrogate is the six characters of its escape.
    """
    lines = path.read_bytes().splitlines(keepends=True)
    record = json.loads(lines[index])
    if value is _GONE:
        del record[key]
    else:
        record[key] = value
    lines[index] = json.dumps(record).encode("ascii") + b"\n"
    path.write_bytes(b"".join(lines))


def test_a_line_overwritten_by_a_copy_of_another_is_a_lost_step(home, device, monkeypatch, capsys):
    """Two readable rows are not two steps when both are step 1."""
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in")
    steps_file = _steps_file(session_id)
    lines = steps_file.read_bytes().splitlines(keepends=True)
    steps_file.write_bytes(lines[0] + lines[0])

    assert _cli(monkeypatch, "--stop", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["incomplete"] is True
    assert (result["steps"], result["lost_steps"], result["unreadable_lines"]) == (1, 1, 1)
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert [step["number"] for step in manifest["steps"]] == [1]


def test_a_repeated_line_is_said_without_inventing_a_lost_step(home, device, monkeypatch, capsys):
    """Every step is still there, so none is lost -- but the file is not what
    the recorder wrote, and that is said. The count `--stop` keeps must not
    grow by the repeat: with the file put right, nothing is missing."""
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in")
    steps_file = _steps_file(session_id)
    whole = steps_file.read_bytes()
    steps_file.write_bytes(whole + whole.splitlines(keepends=True)[-1])

    assert _cli(monkeypatch, "--stop", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["incomplete"] is True
    assert (result["steps"], result["lost_steps"], result["unreadable_lines"]) == (2, 0, 1)

    steps_file.write_bytes(whole)
    assert _cli(monkeypatch, "--get-details", session_id, "--json") == 0
    details = json.loads(capsys.readouterr().out)
    assert (details["incomplete"], details["lost_steps"], details["unreadable_lines"]) == (
        False,
        0,
        0,
    )


def test_a_step_after_a_repeated_line_takes_the_next_number(home, device, monkeypatch, capsys):
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in")
    steps_file = _steps_file(session_id)
    whole = steps_file.read_bytes()
    steps_file.write_bytes(whole + whole.splitlines(keepends=True)[-1])

    assert _cli(monkeypatch, "--step", "Submit", "--json") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["number"] == 3
    assert "incomplete" in captured.err.lower(), captured.err


@pytest.mark.parametrize(
    "value",
    [_GONE, True, 0, -1, 1.5, "2", None, 10**7],
    ids=["missing", "bool", "zero", "negative", "float", "string", "null", "past-the-limit"],
)
def test_a_record_whose_number_is_not_a_step_number_is_unreadable(
    home, device, monkeypatch, capsys, value
):
    """The first line is the one rewritten: `True` there would otherwise read
    as step 1, and on the second line it would only be taken for a repeat."""
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in")
    _rewrite_line(_steps_file(session_id), 0, "number", value)

    assert _cli(monkeypatch, "--stop", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert (result["steps"], result["unreadable_lines"], result["lost_steps"]) == (1, 1, 1)
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert [step["number"] for step in manifest["steps"]] == [2]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("elapsed_seconds", "oops"),
        ("elapsed_seconds", None),
        ("screen", ["oops"]),
        ("screen", "oops"),
        ("screen", {"labels": "oops"}),
        ("screen", {"labels": [1, 2]}),
        ("screenshot", 5),
        ("ui_dump", ["oops"]),
        ("errors", 5),
        ("description", _LONE_SURROGATE),
        ("activity", _LONE_SURROGATE),
        ("assertion", _LONE_SURROGATE),
        ("screenshot", f"screenshots/{_LONE_SURROGATE}.png"),
        ("ui_dump", f"ui-dumps/{_LONE_SURROGATE}.json"),
        ("errors", [_LONE_SURROGATE]),
        ("screen", {"labels": [_LONE_SURROGATE]}),
        ("elapsed_seconds", 10**400),
        ("elapsed_seconds", float("inf")),
        ("elapsed_seconds", float("nan")),
        ("assertion_passed", "false"),
        ("captured", "yes"),
        ("screen", {"elements": "many"}),
        ("a_field_the_recorder_never_wrote", 1),
        ("screen", {"a_field_the_recorder_never_wrote": 1}),
    ],
    ids=[
        "elapsed-string",
        "elapsed-null",
        "screen-list",
        "screen-string",
        "labels-string",
        "labels-of-numbers",
        "screenshot-number",
        "ui-dump-list",
        "errors-number",
        "description-surrogate",
        "activity-surrogate",
        "assertion-surrogate",
        "screenshot-surrogate",
        "ui-dump-surrogate",
        "error-surrogate",
        "label-surrogate",
        "elapsed-400-digits",
        "elapsed-infinite",
        "elapsed-nan",
        "assertion-passed-string",
        "captured-string",
        "elements-string",
        "unknown-field",
        "unknown-screen-field",
    ],
)
def test_a_record_the_report_cannot_render_is_unreadable_not_a_traceback(
    home, device, monkeypatch, capsys, key, value
):
    """`--stop` used to die in the report writer on these, after marking the
    session stopped and before writing a report or a manifest. A text field
    that cannot be written as UTF-8 and a number no float holds died there
    too; a string where a flag belongs rendered as a passed assertion."""
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in")
    _rewrite_line(_steps_file(session_id), 1, key, value)

    assert _cli(monkeypatch, "--get-details", session_id) == 0
    assert "Incomplete:" in capsys.readouterr().out

    assert _cli(monkeypatch, "--stop", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert (result["steps"], result["unreadable_lines"], result["lost_steps"]) == (1, 1, 1)
    assert "INCOMPLETE" in Path(result["report"]).read_text(encoding="utf-8")
    assert Path(result["manifest"]).exists()


def test_a_stop_that_cannot_write_its_report_leaves_the_session_recording(
    home, device, monkeypatch, capsys
):
    """Stopped-with-no-report is a session nothing can finish: `--stop` refuses
    a stopped session. The status changes only once the report is on disk."""
    session_id = _record(monkeypatch, capsys, "Open the app")

    def refuse(*_args, **_kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(TestRecorder, "render_report", refuse)
        with pytest.raises(OSError, match="disk full"):
            _fresh().stop()
    store = _fresh().store
    assert store.load_meta(session_id).status == "recording"

    assert _cli(monkeypatch, "--stop", "--json") == 0
    assert Path(json.loads(capsys.readouterr().out)["report"]).exists()


def test_a_line_nested_past_the_parser_is_one_unreadable_line(home, device, monkeypatch, capsys):
    """The JSON parser gives up on this with RecursionError, not ValueError."""
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in")
    steps_file = _steps_file(session_id)
    lines = steps_file.read_bytes().splitlines(keepends=True)
    depth = 100_000
    lines[1] = (
        b'{"number": 2, "description": "Tap sign in", "errors": '
        + b"[" * depth
        + b"]" * depth
        + b"}\n"
    )
    steps_file.write_bytes(b"".join(lines))

    assert _cli(monkeypatch, "--get-details", session_id) == 0
    assert "Incomplete:" in capsys.readouterr().out
    assert _cli(monkeypatch, "--stop", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert (result["steps"], result["unreadable_lines"], result["lost_steps"]) == (1, 1, 1)


def _full_step(monkeypatch, capsys) -> tuple[str, dict]:
    """A session whose second step carries every field `--step` can write."""
    session_id = _record(monkeypatch, capsys, "Open the app")
    argv = ("--step", "Tap sign in", "--screen", "Login", "--state", "empty", "--assert", "shown")
    assert _cli(monkeypatch, *argv) == 0
    capsys.readouterr()
    return session_id, json.loads(_steps_file(session_id).read_bytes().splitlines()[1])


def test_the_record_the_recorder_writes_is_one_it_reads_back(home, device, monkeypatch, capsys):
    """The reader's field table is closed, so it must hold every field the
    writer has: one it lacked would turn each new step into an unreadable line."""
    _, record = _full_step(monkeypatch, capsys)

    assert set(record) == set(test_recorder._STEP_FIELDS), sorted(record)
    assert set(record["screen"]) == set(test_recorder._SCREEN_FIELDS), sorted(record["screen"])
    assert _cli(monkeypatch, "--stop", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["steps"], result["unreadable_lines"], result["lost_steps"]) == (2, 0, 0)


_HOSTILE = [
    None,
    True,
    0,
    -1,
    1.5,
    10**400,
    float("nan"),
    float("-inf"),
    "text",
    _LONE_SURROGATE,
    [],
    ["text"],
    [_LONE_SURROGATE],
    [[]],
    [None],
    {},
    {"key": _LONE_SURROGATE},
]
_STEP_KEYS = [
    "number",
    "description",
    "recorded_at",
    "elapsed_seconds",
    "screenshot",
    "ui_dump",
    "screen",
    "activity",
    "marker_logged",
    "errors",
    "captured",
    "screen_name",
    "state",
    "assertion",
    "assertion_passed",
    "screen.elements",
    "screen.clickable",
    "screen.with_text",
    "screen.labels",
    "screen.label_total",
    "screen.package",
]


@pytest.mark.parametrize("key", _STEP_KEYS)
def test_no_value_in_any_field_of_a_step_ends_a_command_in_a_defect(
    home, device, monkeypatch, capsys, key
):
    """Every field of a written record, given every kind of JSON value in turn.

    Whatever the reader makes of the line, it has two honest answers: the step
    is there and everything renders, or the line is unreadable and that is
    said. No value may reach a renderer that cannot print it.
    """
    for value in _HOSTILE:
        session_id, record = _full_step(monkeypatch, capsys)
        assert set(record) | {f"screen.{name}" for name in record["screen"]} == set(_STEP_KEYS)
        if key.startswith("screen."):
            _rewrite_line(
                _steps_file(session_id), 1, "screen", {**record["screen"], key[7:]: value}
            )
        else:
            _rewrite_line(_steps_file(session_id), 1, key, value)
        shown = f"{key}={value!r}"

        for argv in (
            ("--get-details", session_id),
            ("--get-details", session_id, "--json"),
            ("--list",),
            ("--list", "--json"),
        ):
            assert _cli(monkeypatch, *argv) == 0, (shown, argv)
            captured = capsys.readouterr()
            assert "Traceback" not in captured.err, (shown, argv, captured.err)
            captured.out.encode("utf-8")

        status = _cli(monkeypatch, "--stop", "--session", session_id, "--json")
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err, (shown, captured.err)
        result = json.loads(captured.out)
        counts = (result["steps"], result["unreadable_lines"], result["lost_steps"])
        assert (status, counts) in ((0, (2, 0, 0)), (1, (1, 1, 1))), (shown, status, counts)
        Path(result["report"]).read_text(encoding="utf-8")
        manifest = Path(result["manifest"]).read_text(encoding="utf-8")
        assert json.loads(manifest, parse_constant=pytest.fail)["steps"], shown


@pytest.mark.parametrize(
    "argv",
    [
        ("--step", _LONE_SURROGATE),
        ("--step", "Submit", "--screen", _LONE_SURROGATE),
        ("--step", "Submit", "--state", _LONE_SURROGATE),
        ("--step", "Submit", "--assert", _LONE_SURROGATE),
    ],
    ids=["description", "screen", "state", "assertion"],
)
def test_a_step_that_could_not_be_read_back_is_refused_before_capture(
    home, device, monkeypatch, capsys, argv
):
    """Bytes on the command line that are not UTF-8 arrive as lone surrogates.
    Appending them died in the encoder, after the screenshot was on disk."""
    session_id = _record(monkeypatch, capsys, "Open the app")
    before = _steps_file(session_id).read_bytes()
    captures = len(device["screenshots"])

    assert _cli(monkeypatch, *argv) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, captured.err
    assert "UTF-8" in captured.out + captured.err
    assert _steps_file(session_id).read_bytes() == before
    assert len(device["screenshots"]) == captures


def _unreadable_activity(monkeypatch) -> None:
    monkeypatch.setattr(test_recorder, "get_current_activity", lambda serial=None: _LONE_SURROGATE)


def _activity_of_the_wrong_type(monkeypatch) -> None:
    monkeypatch.setattr(test_recorder, "get_current_activity", lambda serial=None: 5)


def _unreadable_capture_error(monkeypatch) -> None:
    def _capture(*_args, **_kwargs):
        raise RuntimeError(f"adb said {_LONE_SURROGATE}")

    monkeypatch.setattr(test_recorder, "capture_screenshot", _capture)


@pytest.mark.parametrize(
    ("spoil", "field"),
    [
        (_unreadable_activity, "activity"),
        (_activity_of_the_wrong_type, "activity"),
        (_unreadable_capture_error, "errors"),
    ],
    ids=["activity-not-utf8", "activity-not-text", "capture-error-not-utf8"],
)
def test_a_record_the_reader_would_refuse_is_never_appended(
    home, device, monkeypatch, capsys, spoil, field
):
    """The check before capture knows the step's own text and number. What the
    capture brings back is known only afterwards, so the append itself refuses
    whatever the reader's table would: a traceback from the encoder, or a line
    that is lost the moment it is written, is not a recorded step."""
    session_id = _record(monkeypatch, capsys, "Open the app")
    before = _steps_file(session_id).read_bytes()

    with monkeypatch.context() as spoiled:
        spoil(spoiled)
        assert _cli(monkeypatch, "--step", "Submit") == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, captured.err
    assert f"its {field}" in captured.out + captured.err
    assert _steps_file(session_id).read_bytes() == before

    # The number was not spent: the next step that can be stored takes it.
    assert _cli(monkeypatch, "--step", "Submit") == 0
    capsys.readouterr()
    assert _cli(monkeypatch, "--stop", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["steps"], result["unreadable_lines"], result["lost_steps"]) == (2, 0, 0)


@pytest.mark.parametrize(
    "argv",
    [("--start", "Login flow"), ("--step", "Open"), ("--stop",), ("--list",)],
    ids=["start", "step", "stop", "list"],
)
def test_a_storage_root_that_is_not_utf8_is_refused_whole(
    tmp_path, device, monkeypatch, capsys, argv
):
    """Every artifact path a step records starts with the storage root. One
    that is not UTF-8 let a session start, ran the capture, and died appending
    the screenshot's path. Refused once, where the root is read, no command
    gets as far as a path it cannot write down."""
    root = tmp_path / "home"
    monkeypatch.setenv("ANDROID_EMU_RECORDER_HOME", os.fsdecode(os.fsencode(root) + b"-\xff"))

    assert _cli(monkeypatch, *argv) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, captured.err
    said = captured.out + captured.err
    assert "ANDROID_EMU_RECORDER_HOME" in said
    assert "UTF-8" in said
    said.encode("utf-8")
    assert device["screenshots"] == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "argv",
    [
        ("--start", _LONE_SURROGATE),
        ("--start", "Login flow", "--app-name", _LONE_SURROGATE),
        ("--start", "Login flow", "--serial", _LONE_SURROGATE),
    ],
    ids=["name", "app-name", "serial"],
)
def test_a_session_that_could_not_be_reported_is_not_started(
    home, device, monkeypatch, capsys, argv
):
    """A session's name, app and serial go into meta.json, where an escape
    holds anything, and then into report.md, which is UTF-8: the session
    started, took its steps, and `--stop` died writing the report."""
    assert _cli(monkeypatch, *argv) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, captured.err
    assert "UTF-8" in captured.out + captured.err

    assert _cli(monkeypatch, "--list", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_a_session_at_the_last_step_number_takes_no_more(home, device, monkeypatch, capsys):
    """The reader refuses a number past the limit, so the writer must not hand
    one out: it would be a step that is unreadable the moment it is written."""
    session_id = _record(monkeypatch, capsys, "Open the app", "Tap sign in")
    steps_file = _steps_file(session_id)
    _rewrite_line(steps_file, 1, "number", test_recorder.MAX_STEP_NUMBER)
    before = steps_file.read_bytes()

    assert _cli(monkeypatch, "--step", "Submit") == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, captured.err
    assert str(test_recorder.MAX_STEP_NUMBER) in captured.out + captured.err
    assert steps_file.read_bytes() == before
