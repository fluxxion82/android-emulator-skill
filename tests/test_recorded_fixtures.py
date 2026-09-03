"""Tests that consume recorded tool output.

These are the payoff of ``tests/record_fixtures.py``. Every assertion here runs
against bytes a real device actually produced, never a hand-written literal.

Several tests are marked ``xfail(strict=True)`` against a defect ID. That is
deliberate and load-bearing:

- The suite stays honest — a known-broken parser is not reported as passing.
- ``strict=True`` means the test *fails* if it unexpectedly passes, so whoever
  fixes the defect is forced to come here and drop the marker. The defect list
  becomes executable instead of aspirational.

When you fix a defect, delete its ``xfail`` marker in the same commit.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

RECORDED_ROOT = Path(__file__).resolve().parent / "fixtures" / "recorded"
RECORDED_DIR = RECORDED_ROOT / "emulator-api35"


# ---------------------------------------------------------------------------
# Fixture-set integrity. These guard the guards: a fixture pack that silently
# empties out would turn every test below into a no-op.
# ---------------------------------------------------------------------------


def test_manifest_matches_files_on_disk(recorded):
    """Every manifest entry has a file, and every file has a manifest entry."""
    manifest = recorded.manifest
    listed = {entry["file"] for entry in manifest["fixtures"]}
    on_disk = {p.name for p in RECORDED_DIR.iterdir() if p.name != "MANIFEST.json"}
    assert listed == on_disk, (
        f"manifest/disk mismatch.\n"
        f"  in manifest only: {sorted(listed - on_disk)}\n"
        f"  on disk only:     {sorted(on_disk - listed)}\n"
        f"Re-run: python tests/record_fixtures.py"
    )


def test_manifest_records_device_provenance(recorded):
    """A fixture without provenance cannot be reasoned about after an OS bump."""
    device = recorded.manifest["device"]
    assert device["api_level"].isdigit(), "API level missing from manifest"
    assert device["model"], "device model missing from manifest"


def test_no_fixture_contains_a_recorder_placeholder():
    """The recorder must never write a placeholder in place of real output.

    A file that looks like ground truth and is not is precisely the failure mode
    this directory exists to eliminate.
    """
    offenders = [
        p.name
        for p in RECORDED_DIR.glob("*")
        if p.suffix in {".txt", ".xml"} and "<<RECORDER" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"placeholder content in: {offenders}"


# ---------------------------------------------------------------------------
# A1 — log_monitor requests `-v time` but its regex expects `-v threadtime`,
# so it parses nothing at all and `--follow` prints no lines.
# ---------------------------------------------------------------------------


def _log_monitor():
    from log_monitor import LogMonitor

    return LogMonitor()


def _content_lines(text: str) -> list[str]:
    """Logcat body lines, dropping the '--------- beginning of X' separators."""
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.startswith("--------- beginning of")
    ]


def test_parser_matches_the_threadtime_format_it_was_written_for(recorded):
    """Sanity check: the regex does work, against the format it expects."""
    monitor = _log_monitor()
    lines = _content_lines(recorded.text("logcat_threadtime"))
    assert lines, "threadtime fixture is empty"

    parsed = [monitor.parse_logcat_line(line) for line in lines]
    matched = [p for p in parsed if p is not None]
    assert len(matched) > len(lines) * 0.9, (
        f"only {len(matched)}/{len(lines)} threadtime lines parsed; "
        f"the regex should match nearly all of them"
    )


def test_requested_format_and_parsed_format_agree(recorded):
    """The command's `-v` argument must name the format the parser understands.

    This is the invariant that, held from the start, would have made A1
    impossible — regardless of which of the two sides was chosen.
    """
    monitor = _log_monitor()
    cmd = monitor.build_logcat_command()
    requested = cmd[cmd.index("-v") + 1]

    probe = _content_lines(recorded.text(f"logcat_{requested}"))[:50]
    parsed = [monitor.parse_logcat_line(line) for line in probe]
    assert any(p is not None for p in parsed), (
        f"build_logcat_command requests '-v {requested}', but parse_logcat_line "
        f"matched none of {len(probe)} real lines in that format"
    )


# ---------------------------------------------------------------------------
# S9 — `wm size` / `wm density` parsers match only the Physical line, so every
# coordinate is wrong by the ratio while an override is active.
# ---------------------------------------------------------------------------

_PHYSICAL_SIZE_RE = re.compile(r"Physical size: (\d+)x(\d+)")
_OVERRIDE_SIZE_RE = re.compile(r"Override size: (\d+)x(\d+)")
_PHYSICAL_DENSITY_RE = re.compile(r"Physical density: (\d+)")
_OVERRIDE_DENSITY_RE = re.compile(r"Override density: (\d+)")


def test_override_fixture_actually_captured_an_override(any_profile):
    """Guard the guard: without a real override the S9 test proves nothing."""
    if not any_profile.has("wm_size_override"):
        pytest.skip(f"{any_profile.name} did not record wm_size_override")
    text = any_profile.text("wm_size_override")
    assert _PHYSICAL_SIZE_RE.search(text), "no Physical line in override fixture"
    assert _OVERRIDE_SIZE_RE.search(
        text
    ), "override fixture has no 'Override size:' line — re-record it"


def test_physical_fixture_has_no_override(any_profile):
    """The physical fixture must be recorded from a clean slate."""
    if not any_profile.has("wm_size_physical"):
        pytest.skip(f"{any_profile.name} did not record wm_size_physical")
    assert not _OVERRIDE_SIZE_RE.search(
        any_profile.text("wm_size_physical")
    ), "physical fixture is polluted by a leftover override — re-record it"


@pytest.mark.parametrize(
    ("fixture_name", "override_re", "physical_re"),
    [
        ("wm_size_after_reset", _OVERRIDE_SIZE_RE, _PHYSICAL_SIZE_RE),
        ("wm_density_after_reset", _OVERRIDE_DENSITY_RE, _PHYSICAL_DENSITY_RE),
    ],
)
def test_the_override_reset_is_verified_after_an_override(
    any_profile, fixture_name, override_re, physical_re
):
    """The receipt for the teardown, from a device that HAD an override.

    ``wm_size_physical`` cannot serve as this receipt, which is what it was
    doing: it is captured from a device that never had an override set, so it
    shows only that a clean device is clean, and it passes just as happily when
    a teardown failed on some later fixture.

    These two are captured after an override was applied and then reset, so
    what they record is the reset actually taking effect. That matters because
    ``wm size reset`` prints nothing whether or not it worked, and on a real
    handset a failed reset leaves somebody's phone at the wrong resolution.
    """
    if not any_profile.has(fixture_name):
        pytest.skip(f"{any_profile.name} did not record {fixture_name}")
    text = any_profile.text(fixture_name)

    assert physical_re.search(text), f"{fixture_name} has no Physical line at all"
    assert not override_re.search(text), (
        f"{any_profile.name}: {fixture_name} still shows an override, so the "
        f"reset did not take and the recording device was left altered: "
        f"{text.strip()!r}"
    )


def test_effective_screen_size_prefers_override(any_profile):
    """The size a tap is computed against must be the effective one.

    With an override active, uiautomator reports element bounds in the OVERRIDE
    resolution while a parser reading only "Physical size:" scales taps against
    the physical one, so every coordinate is wrong by the ratio.

    Runs on every profile that recorded the override (T14). One device could
    only ever show that the parser prefers one of two lines *on that device*;
    two different physical resolutions show it is the Override line being
    chosen and not, say, the larger or the second of the pair.
    """
    if not any_profile.has("wm_size_override"):
        pytest.skip(f"{any_profile.name} did not record wm_size_override")
    from common.device_utils import parse_display_size

    text = any_profile.text("wm_size_override")
    physical = tuple(int(g) for g in _PHYSICAL_SIZE_RE.search(text).groups())
    override = tuple(int(g) for g in _OVERRIDE_SIZE_RE.search(text).groups())
    assert physical != override, (
        f"{any_profile.name}: the override equals the physical size, so this "
        f"capture cannot show which line was read. Re-record with a value that "
        f"differs from this device's physical resolution."
    )

    assert parse_display_size(text) == override


def test_physical_size_is_used_when_no_override_is_set(recorded):
    """Guard against always preferring a line that is usually absent."""
    from common.device_utils import parse_display_size

    text = recorded.text("wm_size_physical")
    expected = tuple(int(g) for g in _PHYSICAL_SIZE_RE.search(text).groups())
    assert parse_display_size(text) == expected


def test_effective_density_prefers_override(any_profile):
    """Same defect, same shape, for the density the dp conversion needs.

    The expected values are read out of the fixture rather than written here.
    They used to be the literals 560 and 420, which is what made this a
    single-device test: 560 is the Pixel 4 XL's PHYSICAL density, so the same
    assertion on that profile passed for the wrong reason.
    """
    if not any_profile.has("wm_density_override"):
        pytest.skip(f"{any_profile.name} did not record wm_density_override")
    from common.device_utils import parse_display_density

    override_text = any_profile.text("wm_density_override")
    physical = int(_PHYSICAL_DENSITY_RE.search(override_text).group(1))
    override = int(_OVERRIDE_DENSITY_RE.search(override_text).group(1))
    assert physical != override, (
        f"{any_profile.name}: override density equals physical density "
        f"({physical}), so this capture cannot show which line was read"
    )
    assert parse_display_density(override_text) == override

    physical_text = any_profile.text("wm_density_physical")
    assert _OVERRIDE_DENSITY_RE.search(physical_text) is None
    assert parse_display_density(physical_text) == int(
        _PHYSICAL_DENSITY_RE.search(physical_text).group(1)
    )


# ---------------------------------------------------------------------------
# S7 — touch targets are compared in pixels against a constant documented in dp.
# ---------------------------------------------------------------------------


def test_recorded_hierarchy_bounds_are_pixels_not_dp(recorded):
    """Establish the premise of S7 from real output, not from assertion.

    The hierarchy's bounds must exceed the physical width in dp, which is only
    possible if they are pixels.
    """
    hierarchy = recorded.text("uiautomator_current_screen")
    density = int(
        re.search(r"Physical density: (\d+)", recorded.text("wm_density_physical")).group(1)
    )
    width_px = int(_PHYSICAL_SIZE_RE.search(recorded.text("wm_size_physical")).group(1))

    bounds = [
        int(m.group(3)) for m in re.finditer(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', hierarchy)
    ]
    assert bounds, "no bounds in recorded hierarchy"

    width_dp = width_px / (density / 160)
    assert max(bounds) > width_dp, (
        f"max bound {max(bounds)} does not exceed screen width in dp "
        f"({width_dp:.0f}); the pixels-vs-dp premise does not hold on this device"
    )
    assert max(bounds) <= width_px, f"bound {max(bounds)} exceeds physical width {width_px}"


def test_touch_target_threshold_is_converted_to_pixels(recorded):
    """48dp must become a pixel threshold before being compared to bounds.

    uiautomator reports bounds in physical PIXELS. Comparing them against the
    literal 48 meant the check only fired on elements ~2.6x too small at 420dpi,
    and its message called the pixel figures "dp".
    """
    from accessibility_audit import AccessibilityAuditor

    density = int(
        re.search(r"Physical density: (\d+)", recorded.text("wm_density_physical")).group(1)
    )
    auditor = AccessibilityAuditor()
    auditor.density = density

    assert auditor.min_touch_target_px() == pytest.approx(48 * (density / 160), rel=0.01)


def test_a_genuinely_small_target_is_reported(recorded):
    """The check has to actually fire; before, it almost never did.

    At 420dpi a 100x100px control is ~38dp -- clearly under the 48dp minimum --
    yet 100 > 48 so the old comparison passed it.
    """
    from accessibility_audit import AccessibilityAuditor

    density = int(
        re.search(r"Physical density: (\d+)", recorded.text("wm_density_physical")).group(1)
    )
    auditor = AccessibilityAuditor()
    auditor.density = density
    assert (
        auditor.min_touch_target_px() > 100
    ), "a 100px control at 420dpi is ~38dp and must be flagged"


# ---------------------------------------------------------------------------
# S12 / S14 — code calls Android subcommands that do not exist.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subcommand",
    [
        "battery-level",
        "battery-charging",
        "wifi-enabled",
        "wifi-level",
        "mobile-enabled",
        "mobile-level",
        "mobile-datatype",
    ],
)
def test_statusbar_subcommands_used_by_status_bar_do_not_exist(any_profile, subcommand):
    """S12: these are SystemUI demo-mode broadcast extras, not statusbar verbs.

    status_bar.py already contains the correct implementation
    (``_demo_broadcast``); only the CLI dispatch routes to the invented path.
    """
    if not any_profile.has("cmd_statusbar_help"):
        pytest.skip(f"{any_profile.name} did not record cmd_statusbar_help")
    help_text = any_profile.text("cmd_statusbar_help")
    assert (
        subcommand not in help_text
    ), f"'{subcommand}' appears in `cmd statusbar` help on {any_profile.name} — re-check S12"


def test_cmd_notification_has_no_list_channels_subcommand(any_profile):
    """S14: `cmd notification list channels <pkg>` silently runs bare `list`.

    It does not error, which is worse than a hard failure: push_notification
    parses notification keys looking for channels and reports "none found".
    """
    if not any_profile.has("cmd_notification_help"):
        pytest.skip(f"{any_profile.name} did not record cmd_notification_help")
    help_text = any_profile.text("cmd_notification_help")
    subcommands = re.findall(r"^\s{2}(\S+)", help_text, re.MULTILINE)
    assert "list" in subcommands, "fixture does not look like the expected help output"
    assert "channels" not in subcommands


# ---------------------------------------------------------------------------
# T5 — `dumpsys activity anr` is not a command. anr_watcher pulled it on every
# --start, guarded on a non-zero exit that never came, and fed the resulting
# usage text to a logcat-line parser.
# ---------------------------------------------------------------------------


def _manifest_entry(profile, name: str) -> dict:
    """The manifest entry for one fixture, or fail naming what is missing."""
    for entry in profile.manifest["fixtures"]:
        if entry["name"] == name:
            return entry
    pytest.fail(f"{profile.name} has no manifest entry for {name!r}")


def test_dumpsys_activity_anr_is_not_a_subcommand(any_profile):
    """T5: ActivityManager rejects `anr` outright, on every profile recorded.

    Same shape as the `cmd statusbar` / `cmd notification` pins above. The
    token is not treated as a package filter and no ANR data is printed:
    ActivityManager answers with its "Unknown command" line and the generic
    "Bad activity command" line, then points at -h.
    """
    if not any_profile.has("dumpsys_activity_anr"):
        pytest.skip(f"{any_profile.name} did not record dumpsys_activity_anr")
    text = any_profile.text("dumpsys_activity_anr")

    assert "Unknown command: anr" in text, (
        f"{any_profile.name}: `dumpsys activity anr` no longer reports an "
        f"unknown command — re-check T5 on this API level"
    )
    assert "Bad activity command" in text
    # The point of the pin: no ANR data of any kind comes back.
    assert "ANR in " not in text, "this command returned actual ANR data; re-check T5"


def test_dumpsys_activity_anr_exits_zero_with_nothing_on_stderr(any_profile):
    """T5, the load-bearing half: the failure is invisible to a returncode check.

    ``_pull_dumpsys_anr`` bailed out on ``result.returncode != 0``. The command
    exits **0**, so that guard never fired and three lines of usage text went
    into ``parse_logcat_anr`` on every single session start.

    All three facts are asserted, because the argument needs all three: the
    usage text is on **stdout**, **stderr is empty**, and the **exit status is
    0**. The fixture file can only ever carry one stream, so "stderr said
    nothing" is recorded as a measured byte count in MANIFEST.json rather than
    left as something nobody checked.
    """
    if not any_profile.has("dumpsys_activity_anr"):
        pytest.skip(f"{any_profile.name} did not record dumpsys_activity_anr")
    entry = _manifest_entry(any_profile, "dumpsys_activity_anr")

    assert entry["exit_status"] == 0, (
        f"{any_profile.name}: the recording shows exit {entry['exit_status']}. "
        f"If this command now fails loudly, a returncode guard would have been "
        f"enough and T5's reasoning needs revisiting."
    )
    assert entry["stream"] == "stdout", "the usage text came back on stdout"
    assert "Unknown command: anr" in any_profile.text("dumpsys_activity_anr")
    assert entry["stderr_bytes"] == 0, (
        f"{any_profile.name}: stderr carried {entry['stderr_bytes']} bytes. "
        f"A diagnostic there would have given a caller something to detect; "
        f"the point of T5 is that there was none."
    )
    assert entry["stdout_bytes"] > 0


def test_the_invented_anr_pull_parses_nothing(any_profile):
    """What the deleted code actually achieved, measured rather than asserted.

    Every line of the real output is fed to the real parser. None of them
    produce an event, so the "historical ANR source" contributed exactly
    nothing to a session while looking like a feature.
    """
    if not any_profile.has("dumpsys_activity_anr"):
        pytest.skip(f"{any_profile.name} did not record dumpsys_activity_anr")
    from common.anr_pipeline import parse_logcat_anr

    lines = [ln for ln in any_profile.lines("dumpsys_activity_anr") if ln.strip()]
    assert lines, "fixture is empty"
    assert [parse_logcat_anr(ln) for ln in lines] == [None] * len(lines)


def test_anr_watcher_no_longer_issues_the_invented_command():
    """The call, its helper and its timeout constant are gone from the source.

    Checked by parsing, not by substring search. This file's own docstrings now
    *explain* `_pull_dumpsys_anr` and why it went, and a `not in source` test
    would fire on the explanation -- the repo rule is that guards parse rather
    than grep, for exactly that reason.
    """
    import anr_watcher

    tree = ast.parse(Path(anr_watcher.__file__).read_text(encoding="utf-8"))

    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert "_pull_dumpsys_anr" not in defined, "the invented ANR pull is defined again"

    referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "_pull_dumpsys_anr" not in referenced, "something calls the invented ANR pull"

    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "DUMPSYS_TIMEOUT_SECONDS" not in assigned
    assert "DUMPSYS_TIMEOUT_SECONDS" not in referenced

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        args = [a.value for a in node.args if isinstance(a, ast.Constant)]
        assert not ("dumpsys" in args and "anr" in args), f"invented ANR pull is back: {args}"


def test_the_anr_guard_is_not_fooled_by_a_docstring():
    """Guard the guard: prose naming the removed helper must not count.

    The test above would be worthless in reverse -- passing because the symbol
    is absent while a substring check would have failed on the comment that
    explains its absence. This pins the distinction.
    """
    prose = ast.parse('"""We deleted _pull_dumpsys_anr and DUMPSYS_TIMEOUT_SECONDS."""\n')
    defined = {
        node.name
        for node in ast.walk(prose)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert "_pull_dumpsys_anr" not in defined

    real = ast.parse("def _pull_dumpsys_anr(self):\n    pass\n")
    defined_real = {node.name for node in ast.walk(real) if isinstance(node, ast.FunctionDef)}
    assert "_pull_dumpsys_anr" in defined_real, "the check cannot see a real definition"


# ---------------------------------------------------------------------------
# D4 — `pidof` answers "not running" with silence, not with a message.
# ---------------------------------------------------------------------------


def test_pidof_of_a_dead_process_prints_nothing_and_exits_one(any_profile):
    """D4: liveness is in the exit status; stdout cannot express the answer.

    The recorded file is deliberately zero bytes. A caller that decides "is it
    running" by reading stdout gets the empty string — indistinguishable from a
    device that never answered.
    """
    if not any_profile.has("pidof_not_running"):
        pytest.skip(f"{any_profile.name} did not record pidof_not_running")
    assert any_profile.text("pidof_not_running") == ""

    entry = _manifest_entry(any_profile, "pidof_not_running")
    assert entry["exit_status"] == 1
    assert entry["stream"] == "empty", (
        "nothing was written to stderr either, so there is no diagnostic to "
        "parse and no way to tell this apart from an unreachable device"
    )


# ---------------------------------------------------------------------------
# C8 — which IME visibility key actually exists.
# ---------------------------------------------------------------------------


def test_ime_visibility_keys_that_exist(any_profile):
    """C8: `mInputShown` and `mIsInputViewShown` are real; `mImeWindowVis` is not.

    Recorded on API 33 and API 35. Neither carries an `mImeWindowVis` key, so
    code keying on it reads nothing on every version we have evidence for.
    """
    if not any_profile.has("dumpsys_input_method_shown"):
        pytest.skip(f"{any_profile.name} did not record dumpsys_input_method_shown")
    text = any_profile.text("dumpsys_input_method_shown")

    assert "mInputShown=" in text, f"{any_profile.name}: mInputShown is gone — re-check C8"
    assert "mIsInputViewShown=" in text
    assert "mImeWindowVis" not in text, (
        f"{any_profile.name}: mImeWindowVis now exists on this API level; "
        f"C8's premise no longer holds everywhere"
    )


def test_ime_visibility_line_carries_more_than_one_field(any_profile):
    """The parsing hazard, recorded: `mIsInputViewShown` shares its line.

    Measured: `  mIsInputViewShown=false mStatusIcon=0`. A parser that splits
    the line on whitespace and takes the remainder gets two key=value pairs,
    not one value.
    """
    if not any_profile.has("dumpsys_input_method_shown"):
        pytest.skip(f"{any_profile.name} did not record dumpsys_input_method_shown")
    line = next(
        ln for ln in any_profile.lines("dumpsys_input_method_shown") if "mIsInputViewShown=" in ln
    )
    assert len(line.split()) > 1, "the shared-line hazard this test pins is gone"


# ---------------------------------------------------------------------------
# C12 — `input text` is ASCII-only, and reports that by crashing.
# ---------------------------------------------------------------------------


def test_input_text_with_a_non_ascii_character_throws(recorded):
    """C12: not a mangled string — an uncaught NullPointerException, exit 255.

    The failure arrives on stderr as a Java stack trace, so a caller reading
    stdout sees nothing at all and a caller checking for a friendly error
    message finds none.
    """
    text = recorded.text("input_text_nonascii")
    assert "java.lang.NullPointerException" in text
    assert "InputShellCommand.sendText" in text

    entry = _manifest_entry(recorded, "input_text_nonascii")
    assert entry["exit_status"] == 255, "the crash is what makes this detectable at all"
    assert entry["stream"] == "stderr", "nothing was written to stdout"


# ---------------------------------------------------------------------------
# L2 — demo mode's clock takes HHMM and silently ignores anything shorter.
# ---------------------------------------------------------------------------


def test_demo_mode_clock_captures_are_a_usable_pair(recorded):
    """L2: two status bars, one broadcast apart, differing only in hhmm width.

    The images are the evidence and have to be looked at (941 shows the
    device's real time, 0941 shows 9:41). What is asserted here is that the
    pair is intact and genuinely different — a re-record that captured the same
    screen twice would leave the L2 claim resting on nothing.
    """
    ours = RECORDED_DIR / "demo_mode_clock_941.png"
    padded = RECORDED_DIR / "demo_mode_clock_0941.png"
    assert ours.exists() and padded.exists()

    ours_bytes = ours.read_bytes()
    assert ours_bytes[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert ours_bytes != padded.read_bytes(), (
        "the two demo-mode captures are byte-identical, so they cannot show "
        "different clocks; re-record them"
    )

    unpadded_entry = _manifest_entry(recorded, "demo_mode_clock_941")
    padded_entry = _manifest_entry(recorded, "demo_mode_clock_0941")
    for entry in (unpadded_entry, padded_entry):
        assert entry["bytes"] <= 50 * 1024, "status-bar strip is over the size budget"

    # What each image SHOWS, recorded at capture time rather than written from
    # memory. The manifest once claimed the unpadded crop read 12:16 while the
    # committed PNG read 12:25 -- a false observed value in the evidence, which
    # is the failure this corpus exists to prevent, reintroduced in the prose.
    assert padded_entry["clock_reads"] == "9:41", (
        "the padded capture must record the time SystemUI rendered; "
        f"got {padded_entry.get('clock_reads')!r}"
    )
    assert unpadded_entry["clock_reads"] == unpadded_entry["device_clock_at_capture"], (
        "the unpadded capture shows the device's own clock, because SystemUI "
        "dropped the 3-digit hhmm; those two fields must therefore agree"
    )
    assert unpadded_entry["clock_reads"] != "9:41", (
        "the unpadded capture reads 9:41, so it cannot show that the broadcast "
        "was ignored; re-record it (the recorder refuses this, so this means "
        "the manifest was edited by hand)"
    )


# ---------------------------------------------------------------------------
# E1 — `am start -W` ground truth. app_launcher --launch parses this and was
# written with nothing to read; these two recordings are what it should be
# tested against.
# ---------------------------------------------------------------------------


def test_am_start_wait_reports_a_different_activity_than_it_was_asked_for(recorded):
    """E1: success cannot be confirmed by comparing the component strings.

    `.Settings` is an activity-alias, so a launch of
    `com.android.settings/.Settings` reports
    `com.android.settings/.homepage.SettingsHomepageActivity`. A parser that
    decides "did my activity start" by matching the requested component against
    the Activity: line concludes the launch went somewhere else.
    """
    text = recorded.text("am_start_wait_settings")
    requested = "com.android.settings/.Settings"
    assert f"cmp={requested}" in text, "fixture did not request the component it should have"

    activity = next(ln for ln in text.splitlines() if ln.startswith("Activity:"))
    reported = activity.split(":", 1)[1].strip()
    assert reported != requested, (
        "the recording no longer shows the alias indirection this test pins; " "re-check E1"
    )
    assert reported.startswith("com.android.settings/")

    assert "Status: ok" in text
    assert "LaunchState: COLD" in text, "the capture is not a cold start; force-stop first"
    assert _manifest_entry(recorded, "am_start_wait_settings")["exit_status"] == 0


def test_am_start_wait_failure_is_on_stdout_with_no_status_line(recorded):
    """E1: the failure has no Status: line and writes nothing to stderr.

    Both halves matter for a parser. Keying on `Status:` to find the outcome
    finds nothing here, and keying on stderr to find the error finds nothing
    either -- the whole diagnostic is on stdout, at exit 1.
    """
    text = recorded.text("am_start_wait_missing")
    assert "Error type 3" in text
    assert "does not exist" in text
    assert "Status:" not in text, (
        "a Status: line now appears on the failure path, which changes what a " "parser can key on"
    )

    entry = _manifest_entry(recorded, "am_start_wait_missing")
    assert entry["exit_status"] == 1
    assert entry["stderr_bytes"] == 0, "the diagnostic is on stdout, not stderr"
    assert entry["stdout_bytes"] > 0


# ---------------------------------------------------------------------------
# S4 — a broadcast to a receiver that does not exist still exits 0.
# ---------------------------------------------------------------------------


def test_am_broadcast_reports_success_for_a_nonexistent_receiver(any_profile):
    """S4: this is why push_notification's success check can never fail.

    The recorded command targeted ``com.android.settings/.NoSuchReceiverExists``,
    a class that does not exist. `am broadcast` still printed a completion line
    with result=0. Any check of the form ``"result=" in stdout`` is therefore
    unconditionally true.
    """
    if not any_profile.has("am_broadcast_missing_receiver"):
        pytest.skip(f"{any_profile.name} did not record am_broadcast_missing_receiver")
    output = any_profile.text("am_broadcast_missing_receiver")
    assert "NoSuchReceiverExists" in output, "fixture did not target the bogus receiver"
    assert "Broadcast completed: result=0" in output
    assert "result=" in output.lower(), (
        "the exact substring push_notification tests for is present even though "
        "the receiver does not exist"
    )


# ---------------------------------------------------------------------------
# S10 — launcher activity resolution.
# ---------------------------------------------------------------------------


def test_resolve_activity_returns_a_single_component(recorded):
    """S10: `cmd package resolve-activity --brief` gives one unambiguous answer.

    app_launcher used to grep `pm dump` for the first line containing both
    'Activity' and the package name, which readily matched an ActivityRecord or
    a provider. It now uses this command (S10, fixed during the adb_exec
    migration); this fixture is what it parses.
    """
    lines = [ln for ln in recorded.lines("resolve_activity_launcher") if ln.strip()]
    component = lines[-1]
    assert re.fullmatch(
        r"[\w.]+/[\w.]+", component
    ), f"expected a single pkg/activity component, got {component!r}"


# ---------------------------------------------------------------------------
# Manifest bookkeeping: every defect a fixture claims to catch is real.
# ---------------------------------------------------------------------------


def test_every_claimed_defect_id_is_referenced_by_a_test():
    """A fixture claiming to catch a defect must have a test that uses it."""
    manifest = json.loads((RECORDED_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    claimed = {d for entry in manifest["fixtures"] for d in entry.get("catches", [])}

    # Scan the whole suite: a fixture's tests legitimately live wherever the
    # defect does, not necessarily in this file.
    corpus = "\n".join(
        path.read_text(encoding="utf-8") for path in Path(__file__).parent.glob("test_*.py")
    )

    unreferenced = {d for d in claimed if d not in corpus}
    assert not unreferenced, (
        f"fixtures claim to catch {sorted(unreferenced)} but no test references "
        f"those IDs — either write the test or drop the claim"
    )


# ---------------------------------------------------------------------------
# R5 — device targeting under multiple attached devices.
# ---------------------------------------------------------------------------


def _parse_devices(text: str) -> list[str]:
    """Serials in state 'device', mirroring what device_utils does."""
    return [
        line.split()[0]
        for line in text.splitlines()[1:]
        if line.strip() and line.split()[1] == "device"
    ]


def test_multi_device_fixture_really_has_two_devices(recorded):
    """Guard the guard: a one-device capture would make the R5 test vacuous."""
    serials = _parse_devices(recorded.text("adb_devices_multiple"))
    assert len(serials) >= 2, f"expected >=2 devices in fixture, found {serials}"


def test_omitting_serial_is_ambiguous_when_two_devices_are_attached(recorded):
    """R5: resolve_device_identifier(None) returns None, so `-s` is omitted.

    With two devices attached, adb then exits 1 with 'more than one
    device/emulator'. That is a hard failure rather than a silent mistarget —
    but the agent gets a raw adb error instead of a remedy. The fix is to
    resolve to a concrete serial and warn on stderr, naming the pick.
    """
    from common.device_utils import build_adb_command, resolve_device_identifier

    serials = _parse_devices(recorded.text("adb_devices_multiple"))
    assert len(serials) >= 2

    resolved = resolve_device_identifier(None)
    assert resolved is None, "resolve_device_identifier(None) no longer returns None"

    cmd = build_adb_command("shell", resolved, "echo", "hi")
    assert "-s" not in cmd, (
        "with no serial the command carries no device target; against "
        f"{len(serials)} attached devices adb exits 1 with "
        "'more than one device/emulator'"
    )


# ---------------------------------------------------------------------------
# S7, quantified on real hardware. The emulator understates this badly.
# ---------------------------------------------------------------------------


def test_touch_target_bug_severity_scales_with_density(any_profile):
    """Show how much of the dp range the pixel-comparison wrongly accepts.

    ``accessibility_audit`` compares pixel bounds against the literal 48. Any
    element between 48px and 48dp-in-pixels is undersized but passes. That
    window widens with density, so a fixture set recorded only on a low-density
    emulator makes the bug look far milder than it is on real hardware:

        420dpi (emulator):  48px .. 126px  -> misses down to ~18dp
        560dpi (Pixel 4XL): 48px .. 168px  -> misses down to ~14dp
    """
    if not any_profile.has("wm_density_physical"):
        pytest.skip(f"{any_profile.name} did not record wm_density_physical")

    density_text = any_profile.text("wm_density_physical")
    density = int(re.search(r"Physical density: (\d+)", density_text).group(1))
    scale = density / 160

    correct_threshold_px = 48 * scale
    smallest_dp_wrongly_accepted = 48 / scale

    assert correct_threshold_px > 48, (
        f"{any_profile.name}: density {density} gives no px/dp gap; "
        f"this profile cannot demonstrate S7"
    )
    assert smallest_dp_wrongly_accepted < 48, (
        f"{any_profile.name}: a {smallest_dp_wrongly_accepted:.0f}dp target "
        f"passes a check documented as requiring 48dp"
    )
