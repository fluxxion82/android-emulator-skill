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
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import test_recorder
from test_recorder import RecorderError, RecorderSessionStore, TestRecorder

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
    """Run the real CLI entry point with the given arguments."""
    monkeypatch.setattr(sys, "argv", ["test_recorder.py", *argv])
    return test_recorder.main()


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
    assert len(store.read_steps(meta.session_id)) == 2


def test_step_targets_the_newest_recording_session(home, device):
    """With two sessions open, an unqualified --step goes to the newer one."""
    older = _fresh().start("First run").session_id
    newer = _fresh().start("Second run").session_id
    assert older != newer

    _fresh().step("Open the app")

    store = _fresh().store
    assert len(store.read_steps(newer)) == 1
    assert store.read_steps(older) == []


def test_explicit_session_id_overrides_the_active_one(home, device):
    older = _fresh().start("First run").session_id
    _fresh().start("Second run")

    _fresh().step("Open the app", session_id=older)

    assert len(_fresh().store.read_steps(older)) == 1


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
    assert "Error:" in capsys.readouterr().err


def test_cli_step_that_captures_nothing_exits_nonzero(home, device, monkeypatch, capsys):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("device offline")

    _cli(monkeypatch, "--start", "Login flow")
    monkeypatch.setattr(test_recorder, "capture_screenshot", _boom)
    monkeypatch.setattr(test_recorder, "get_ui_hierarchy", _boom)
    capsys.readouterr()

    assert _cli(monkeypatch, "--step", "Open the app") == 1
    assert "device offline" in capsys.readouterr().err


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
