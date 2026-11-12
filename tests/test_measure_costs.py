"""The measurement table is measured, complete, and reproducible. No device needed.

``tests/measure_costs.py`` exists so that "token-efficient" has numbers behind
it that a reader can produce themselves. Four ways such a table goes wrong,
each held here:

- **A number somebody typed.** The committed table is compared with a fresh
  measurement, cell by cell, and the cells it is built from are recounted here
  by a second route (a file's size on disk; the script run by this file's own
  hand; a sum; a division).
- **A flattering failure.** ``navigator --find-text`` prints one short line
  when it finds nothing. Counting that as the cost of a lookup would make the
  tool look cheaper the more it failed, so a script that does not exit 0 is an
  error, not a row.
- **A flattering omission.** ``screen_mapper`` also asks the device which
  activity has focus, and prints "Unknown Screen" when it cannot. The first
  version of the harness refused that question, the script swallowed the
  refusal, and every mapper row came out 52 to 62 bytes short. A refused
  command is now an error whether or not the script carried on.
- **A measurement of whatever is plugged in.** The scripts run against
  recordings; reaching for a device is refused.

The size of SKILL.md is the one figure this release sets out to change, so it
is not in the committed table. It is measured live, and its v0.9.0 value is
frozen beside it for the comparison.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import xml.etree.ElementTree as ET

import measure_costs as mc
import navigator
import pytest
import screen_mapper
from conftest import RECORDED_ROOT, RecordedFixtures

from common import adb_exec, cli
from common.device_utils import parse_focused_activity

MODULES = {"navigator": navigator, "screen_mapper": screen_mapper}

# SKILL.md as v0.9.0 shipped it, pinned here as well as in the frozen file:
# `git show f9ab727:android-emulator-skill/skills/android-emulator-skill/SKILL.md`
# is 156,541 bytes with this digest. Two copies, so that editing the frozen
# file alone -- its hash included -- fails rather than quietly moving the
# "before" of the comparison.
V0_9_0_BYTES = 156_541
V0_9_0_SHA256 = "cf2eb70b618464bf5cafc2caa38254f1000b569662ee58d5e1d67b875fa90499"
V0_9_0_COMMIT = "f9ab727"

WALK_1 = {
    "fixture": "uiautomator_walk_1_catalog",
    "focus": "dumpsys_window_focus_walk_1_catalog",
}


@pytest.fixture(scope="module")
def measured() -> dict:
    return mc.measure()


@pytest.fixture(scope="module")
def cases() -> dict:
    return mc.load_cases()


def _api35() -> RecordedFixtures:
    return RecordedFixtures(RECORDED_ROOT / "emulator-api35")


def _screen(case: dict) -> tuple[str, str]:
    """A case's two recordings: its dump, and the focus lines taken on that screen."""
    return _api35().text(case["fixture"]), _api35().text(case["focus"])


# --- the committed table ------------------------------------------------------


def test_the_committed_table_is_what_the_harness_measures(measured):
    expected = json.loads(mc.EXPECTED.read_text(encoding="utf-8"))

    assert mc.differences(expected, mc.stable_view(measured)) == [], (
        "the committed table and a fresh measurement disagree; if a script's output "
        "changed on purpose, run: python tests/measure_costs.py --write"
    )


def test_a_changed_cell_a_missing_row_and_an_extra_row_are_all_differences(measured):
    """The comparison itself: one that passed on a shorter table would guard nothing."""
    expected = mc.stable_view(measured)

    changed = mc.stable_view(measured)
    changed["screens"][0]["rows"]["screen_mapper"]["bytes"] += 1
    missing = mc.stable_view(measured)
    del missing["screens"][-1]
    extra = mc.stable_view(measured)
    extra["walk"]["totals"]["invented"] = {"bytes": 1, "tokens_est": 0}

    assert mc.differences(expected, expected) == []
    assert len(mc.differences(expected, changed)) == 1
    assert mc.differences(expected, missing), "a table one screen short compared equal"
    assert mc.differences(expected, extra) == ["walk.totals.invented: not in the committed table"]


def test_the_table_has_a_row_for_every_case_and_every_output(measured, cases):
    assert [screen["id"] for screen in measured["screens"]] == [s["id"] for s in cases["screens"]]
    outputs = [output["id"] for output in cases["outputs"]]
    for screen in measured["screens"]:
        assert list(screen["rows"]) == outputs, screen["id"]


def test_the_committed_view_leaves_out_exactly_the_skill_and_resized_png_bytes(measured):
    """Asked of ``stable_view`` itself, not of the file it once wrote."""
    view = mc.stable_view(measured)

    assert set(measured) - set(view) == {"skill"}
    for whole, kept in zip(measured["screenshots"], view["screenshots"], strict=True):
        for preset, cell in whole["presets"].items():
            dropped = set(cell) - set(kept["presets"][preset])
            assert dropped == (set() if preset == "full" else {"bytes"}), preset
    assert "bytes" in measured["screenshots"][0]["presets"]["half"], "the input was edited"
    assert view["screens"] == measured["screens"]
    assert view["walk"] == measured["walk"]
    assert view["stream"] == measured["stream"]


# --- every figure, by a second route ----------------------------------------


def test_a_raw_dump_costs_exactly_its_recording(measured):
    for screen in measured["screens"]:
        on_disk = (RECORDED_ROOT / measured["profile"] / f"{screen['fixture']}.xml").stat().st_size

        assert screen["rows"]["raw_dump"]["bytes"] == on_disk, screen["id"]


def _run_by_hand(monkeypatch, capsys, tool: str, argv: list[str], xml: str, focus: str) -> str:
    """The script's stdout, obtained without any of the harness's own code."""
    module = MODULES[tool]
    root = ET.fromstring(xml)

    def device(cmd, **_keywords):
        assert list(cmd)[-3:] == ["shell", "dumpsys", "window"], f"unexpected adb call: {cmd}"
        return subprocess.CompletedProcess(cmd, 0, focus, "")

    with monkeypatch.context() as patch:
        patch.setattr(module, "capture_hierarchy", lambda _serial=None, **_keywords: root)
        patch.setattr(module, "resolve_device_identifier", lambda _identifier: "emulator-5554")
        patch.setattr(adb_exec.subprocess, "run", device)
        capsys.readouterr()
        with pytest.raises(SystemExit) as exit_:
            cli.run(module.main, list(argv))
    assert exit_.value.code in (0, None), f"{tool} {argv} exited {exit_.value.code}"
    return capsys.readouterr().out


def test_a_script_s_row_is_the_size_of_what_it_prints(measured, cases, monkeypatch, capsys):
    """Each script row, recounted by running the script here rather than through the harness."""
    recounted = 0
    for screen, case in zip(measured["screens"], cases["screens"], strict=True):
        xml, focus = _screen(case)
        for output in cases["outputs"]:
            if "tool" not in output:
                continue
            argv = [arg.format(find_text=case["find_text"]) for arg in output["argv"]]
            printed = _run_by_hand(monkeypatch, capsys, output["tool"], argv, xml, focus)
            assert screen["rows"][output["id"]]["bytes"] == len(
                printed.encode("utf-8")
            ), f"{case['id']} {output['id']}"
            recounted += 1
    assert recounted == 9 * 4, "nine screens, four script outputs each"


def test_every_lookup_in_the_table_found_what_it_looked_for(cases):
    for case in cases["screens"]:
        printed = mc.tool_output("navigator", ["--find-text", case["find_text"]], *_screen(case))

        assert printed.startswith("Found: "), f"{case['id']}: {printed!r}"


def test_the_mapper_names_every_screen_from_its_recorded_focus(cases):
    """No "Unknown Screen": that header is what a refused focus lookup looks like."""
    for case in cases["screens"]:
        header = mc.tool_output("screen_mapper", [], *_screen(case)).splitlines()[0]
        activity = parse_focused_activity(_api35().text(case["focus"]))

        assert activity, f"{case['focus']} names no focused activity"
        assert "Unknown Screen" not in header
        assert activity in header, f"{case['id']}: {header!r}"


def test_every_token_figure_is_bytes_over_four(measured):
    cells = [row for screen in measured["screens"] for row in screen["rows"].values()]
    cells += list(measured["walk"]["totals"].values())
    cells.append(measured["skill"])

    assert len(cells) > 50
    for cell in cells:
        assert cell["tokens_est"] == round(cell["bytes"] / 4 + 1e-9), cell
    assert [mc.estimated_tokens(n) for n in (0, 1, 2, 3, 4, 5, 6)] == [0, 0, 1, 1, 1, 1, 2]
    assert "estimate" in measured["estimate"]


def test_the_walk_is_the_six_walk_screens_in_order_and_its_totals_are_their_sums(measured):
    walk = measured["walk"]
    steps = [screen for screen in measured["screens"] if screen["walk"] is not None]

    assert walk["screens"] == [f"walk_{n}_{slug}" for n, slug in _walk_slugs()]
    assert [step["walk"] for step in steps] == [1, 2, 3, 4, 5, 6]
    for output, total in walk["totals"].items():
        assert total["bytes"] == sum(step["rows"][output]["bytes"] for step in steps), output
    raw = walk["totals"]["raw_dump"]["bytes"]
    for output, share in walk["percent_of_raw_dump"].items():
        assert share == round(100 * walk["totals"][output]["bytes"] / raw, 1), output
    assert walk["percent_of_raw_dump"]["raw_dump"] == 100.0


def _walk_slugs() -> list[tuple[int, str]]:
    """The walk as the recorder defines it, not as the inventory restates it."""
    import record_fixtures as rf

    return [(number, slug) for number, slug, _marker in rf.WALK_SCREENS]


def test_the_stream_is_its_document_plus_the_status_line(measured):
    stream = measured["stream"]
    recorded = _api35().text(stream["fixture"])

    assert stream["stream_bytes"] == len(recorded.encode("utf-8"))
    assert stream["framing_bytes"] == stream["stream_bytes"] - stream["document_bytes"]
    assert 0 < stream["framing_bytes"] < 100, "the framing is one status line"
    assert recorded.encode("utf-8")[stream["document_bytes"] :].decode("utf-8").startswith("UI ")


# --- screenshots --------------------------------------------------------------


def test_each_preset_scales_the_recorded_dimensions(measured, cases):
    from PIL import Image

    from common.screenshot_utils import get_size_preset

    assert [shot["id"] for shot in measured["screenshots"]] == [
        case["id"] for case in cases["screens"] if "screenshot" in case
    ]
    for shot in measured["screenshots"]:
        recorded = _api35().data(f"{shot['fixture']}.png")
        width, height = Image.open(io.BytesIO(recorded)).size
        assert list(shot["presets"]) == cases["screenshot_presets"]
        assert shot["presets"]["full"] == {"width": width, "height": height, "bytes": len(recorded)}
        for preset, measured_preset in shot["presets"].items():
            scale_x, scale_y = get_size_preset(preset)
            assert (measured_preset["width"], measured_preset["height"]) == (
                int(width * scale_x),
                int(height * scale_y),
            ), f"{shot['id']} {preset}"
            assert measured_preset["bytes"] > 0


# --- a failure is not a row; a device is not an input -------------------------


def test_a_lookup_that_finds_nothing_is_an_error_not_a_cheap_row():
    with pytest.raises(mc.MeasurementError, match="exited"):
        mc.tool_output(
            "navigator", ["--find-text", "No such text on this screen"], *_screen(WALK_1)
        )


def test_a_script_that_reaches_for_a_device_is_refused():
    """`--tap` would send an input event; the replay has no device to send it to."""
    with pytest.raises(mc.MeasurementError, match="tried to reach a device"):
        mc.tool_output("navigator", ["--find-text", "Open cart", "--tap"], *_screen(WALK_1))


def test_a_refusal_the_script_shrugged_off_is_still_an_error(monkeypatch):
    """The defect the first table had: refuse the focus lookup and the mapper carries on.

    With no command allowed, ``screen_mapper`` is refused its ``dumpsys window``,
    catches the exception, prints "Unknown Screen" and exits 0. That is a
    complete, plausible, shorter row -- and not a measurement of the recording.
    """
    monkeypatch.setattr(mc, "FOCUS_QUERY", ["shell", "a-command-nothing-sends"])

    with pytest.raises(mc.MeasurementError, match=r"tried to reach a device.*dumpsys window"):
        mc.tool_output("screen_mapper", [], *_screen(WALK_1))


def test_the_replay_puts_back_everything_it_replaced():
    before = (navigator.capture_hierarchy, navigator.resolve_device_identifier, subprocess.run)

    mc.tool_output("navigator", ["--list"], *_screen(WALK_1))
    with pytest.raises(mc.MeasurementError):
        mc.tool_output("navigator", ["--find-text", "nothing"], *_screen(WALK_1))
    with pytest.raises(mc.MeasurementError):
        mc.tool_output("navigator", ["--find-text", "Open cart", "--tap"], *_screen(WALK_1))

    after = (navigator.capture_hierarchy, navigator.resolve_device_identifier, subprocess.run)
    assert after == before
    assert adb_exec.subprocess.run is subprocess.run


@pytest.mark.parametrize("key", ["fixture", "focus"])
def test_a_case_whose_recording_is_missing_is_an_error(cases, key):
    broken = {**cases, "screens": [{**cases["screens"][0], key: "never_recorded"}]}

    with pytest.raises(mc.MeasurementError, match="record it with"):
        mc.measure_screens(broken)


def test_each_screen_s_focus_recording_is_of_that_screen(cases):
    """A focus line borrowed from another app would name the wrong screen in the header."""
    other_apps = {
        "settings_home": "com.android.settings",
        "dialer_keypad": "com.google.android.dialer",
    }
    for case in cases["screens"]:
        activity = parse_focused_activity(_api35().text(case["focus"]))
        package = other_apps.get(case["id"], "com.example.composefixture")

        assert activity.startswith(package + "/"), f"{case['id']}: {activity}"
        assert f'package="{package}"' in _api35().text(case["fixture"]), case["id"]
        if "walk" in case:
            assert activity.endswith("WalkActivity")
        if case["id"] == "compose_single_screen":
            assert activity.endswith("DefaultActivity"), "the walk's focus beside the older screen"


# --- the skill itself ---------------------------------------------------------


def test_skill_md_is_measured_from_the_file(measured):
    content = mc.SKILL_MD.read_bytes()

    assert measured["skill"]["bytes"] == len(content)
    assert measured["skill"]["sha256"] == hashlib.sha256(content).hexdigest()


def test_the_frozen_v0_9_0_size_is_the_pinned_one(measured):
    """The "before" of the diet: frozen, pinned twice, and checked against git when it can be.

    The frozen file cannot authenticate itself -- change its hash and its bytes
    together and it still agrees with itself -- so both are pinned in this
    module too. While SKILL.md is still the v0.9.0 file, a fresh measurement
    has to equal the frozen entry in every field. And where the repository's
    history holds the v0.9.0 commit (a shallow CI clone does not), the blob
    itself is hashed.
    """
    frozen = json.loads((mc.MEASUREMENTS / "skill_md_v0.9.0.json").read_text(encoding="utf-8"))

    assert frozen["release"] == "0.9.0"
    assert frozen["commit"] == V0_9_0_COMMIT
    assert frozen["bytes"] == V0_9_0_BYTES
    assert frozen["sha256"] == V0_9_0_SHA256
    assert frozen["tokens_est"] == mc.estimated_tokens(V0_9_0_BYTES)
    assert frozen["references"] == mc.NOT_PRESENT
    assert frozen["path"] == measured["skill"]["path"]
    if measured["skill"]["sha256"] == V0_9_0_SHA256:
        assert {key: frozen[key] for key in measured["skill"]} == measured["skill"]

    blob = subprocess.run(
        ["git", "-C", str(mc.REPO), "show", f"{V0_9_0_COMMIT}:{frozen['path']}"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if blob.returncode == 0:
        assert len(blob.stdout) == V0_9_0_BYTES
        assert hashlib.sha256(blob.stdout).hexdigest() == V0_9_0_SHA256


def test_reference_files_are_counted_when_there_are_some(tmp_path, monkeypatch):
    """Today there are none; the row has to be right the day they arrive."""
    skill_dir = tmp_path / "skill"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_bytes(b"x" * 1000)
    (skill_dir / "references" / "navigator.md").write_bytes(b"y" * 300)
    (skill_dir / "references" / "gesture.md").write_bytes(b"z" * 120)
    monkeypatch.setattr(mc, "SKILL_DIR", skill_dir)
    monkeypatch.setattr(mc, "SKILL_MD", skill_dir / "SKILL.md")
    monkeypatch.setattr(mc, "REPO", tmp_path)

    skill = mc.measure_skill()

    assert skill["bytes"] == 1000
    assert skill["references"] == {
        "files": 2,
        "total_bytes": 420,
        "smallest_bytes": 120,
        "largest_bytes": 300,
    }
    assert skill["skill_md_plus_largest_reference"] == {"bytes": 1300, "tokens_est": 325}


# --- the command a reader is told to run --------------------------------------


def _run_harness(*args: str, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(mc.REPO / "tests" / "measure_costs.py"), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=120,
        check=False,
    )


def test_the_harness_runs_from_any_directory_and_checks_clean(tmp_path):
    result = _run_harness("--check", cwd=tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "match" in result.stdout


def test_json_is_one_object_holding_every_section(tmp_path):
    result = _run_harness("--json", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)) == {
        "estimate",
        "profile",
        "skill",
        "screens",
        "walk",
        "stream",
        "screenshots",
    }


def test_the_printed_table_carries_every_measured_figure(measured):
    """What a person reads is the same measurement, not a second one."""
    table = mc.render(measured)

    for screen in measured["screens"]:
        for cell in screen["rows"].values():
            assert f"{cell['bytes']:,} (~{cell['tokens_est']:,})" in table
    walk = measured["walk"]
    for output, total in walk["totals"].items():
        assert f"**{total['bytes']:,} (~{total['tokens_est']:,})**" in table, output
        assert f"{walk['percent_of_raw_dump'][output]}%" in table, output
    assert f"walk, {len(walk['screens'])} screens" in table
    for figure in ("framing_bytes", "stream_bytes", "document_bytes"):
        assert f"{measured['stream'][figure]:,}" in table, figure
    assert f"{measured['skill']['bytes']:,}" in table
    assert mc.NOT_PRESENT in table
    assert "estimate" in table
    for shot in measured["screenshots"]:
        for cell in shot["presets"].values():
            assert f"{cell['width']}x{cell['height']}, {cell['bytes']:,}" in table


def test_the_printed_table_says_what_it_does_not_compare(measured):
    """The caveats travel with the numbers, so they cannot be quoted without them."""
    table = mc.render(measured)

    for said in (
        "not the cost of a task",
        "do not carry the same information",
        "testTagsAsResourceId",
        "status bar painted out",
        "not a function of its file size",
    ):
        assert said in table, said
