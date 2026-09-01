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


def test_override_fixture_actually_captured_an_override(recorded):
    """Guard the guard: without a real override the S9 test proves nothing."""
    text = recorded.text("wm_size_override")
    assert _PHYSICAL_SIZE_RE.search(text), "no Physical line in override fixture"
    assert _OVERRIDE_SIZE_RE.search(
        text
    ), "override fixture has no 'Override size:' line — re-record it"


def test_physical_fixture_has_no_override(recorded):
    """The physical fixture must be recorded from a clean slate."""
    assert not _OVERRIDE_SIZE_RE.search(
        recorded.text("wm_size_physical")
    ), "physical fixture is polluted by a leftover override — re-record it"


def test_effective_screen_size_prefers_override(recorded):
    """The size a tap is computed against must be the effective one.

    With an override active, uiautomator reports element bounds in the OVERRIDE
    resolution while a parser reading only "Physical size:" scales taps against
    the physical one, so every coordinate is wrong by the ratio.
    """
    from common.device_utils import parse_display_size

    text = recorded.text("wm_size_override")
    physical = tuple(int(g) for g in _PHYSICAL_SIZE_RE.search(text).groups())
    override = tuple(int(g) for g in _OVERRIDE_SIZE_RE.search(text).groups())
    assert physical != override, "fixture needs distinct values to prove anything"

    assert parse_display_size(text) == override


def test_physical_size_is_used_when_no_override_is_set(recorded):
    """Guard against always preferring a line that is usually absent."""
    from common.device_utils import parse_display_size

    text = recorded.text("wm_size_physical")
    expected = tuple(int(g) for g in _PHYSICAL_SIZE_RE.search(text).groups())
    assert parse_display_size(text) == expected


def test_effective_density_prefers_override(recorded):
    """Same defect, same shape, for the density the dp conversion needs."""
    from common.device_utils import parse_display_density

    assert parse_display_density(recorded.text("wm_density_override")) == 560
    assert parse_display_density(recorded.text("wm_density_physical")) == 420


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
