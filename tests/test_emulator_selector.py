"""Device-free tests for emulator_selector.

These exercise the pure logic — config.ini parsing, API-level extraction,
common-model matching, and the candidate ranking function — plus the
recent-use config read/write and adb-serial -> AVD-name resolution, all with
``subprocess``/filesystem mocked so no emulator or adb is needed.
"""

from __future__ import annotations

import importlib
import json
import subprocess

import emulator_selector
import pytest

from common import adb_exec
from common.sdk_tools import SdkToolError

# ---------------------------------------------------------------------------
# config.ini parsing + API-level extraction (pure)
# ---------------------------------------------------------------------------
# The only config.ini text in this file, and the one `KNOWN_VIOLATIONS` already
# freezes as `test_emulator_selector.py::parse_config_ini`. Hoisted so the
# read-from-disk test below reuses it rather than inventing a second one (F4):
# no AVD config.ini is recorded on any profile, and one cannot be captured here
# -- recording it is the next candidate for `tests/record_fixtures.py`.
CONFIG_INI = "\n".join(
    [
        "# a comment",
        "AvdId=Pixel_9_Pro",
        "abi.type = arm64-v8a",
        "image.sysdir.1=system-images/android-36/google_apis_playstore/arm64-v8a/",
        "",
        "blank-without-equals",
    ]
)


def test_parse_config_ini_basic():
    config = emulator_selector.parse_config_ini(CONFIG_INI)
    assert config["AvdId"] == "Pixel_9_Pro"
    assert config["abi.type"] == "arm64-v8a"
    assert "image.sysdir.1" in config
    assert "blank-without-equals" not in config


def test_parse_api_level_from_sysdir():
    config = {"image.sysdir.1": "system-images/android-34/google_apis/x86_64/"}
    assert emulator_selector.parse_api_level(config) == 34


def test_parse_api_level_falls_back_to_target():
    config = {"target": "android-30"}
    assert emulator_selector.parse_api_level(config) == 30


def test_parse_api_level_none_when_absent():
    assert emulator_selector.parse_api_level({"foo": "bar"}) is None


# ---------------------------------------------------------------------------
# common-model matching (pure)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "candidate,expected_rank",
    [
        ({"name": "Pixel_8_Pro"}, 0),
        ({"device": "pixel_8"}, 1),
        ({"display_name": "Pixel 7 Pro"}, 2),
        ({"name": "My_Pixel_AVD"}, 5),
        ({"name": "Samsung_S22_Plus"}, None),
    ],
)
def test_common_model_rank(candidate, expected_rank):
    assert emulator_selector.common_model_rank(candidate) == expected_rank


# ---------------------------------------------------------------------------
# ranking function (pure) — the primary unit-test target
# ---------------------------------------------------------------------------
def _candidate(name, api=None, running=False, recent_index=None, device=""):
    return {
        "name": name,
        "api_level": api,
        "device": device,
        "display_name": "",
        "running": running,
        "recent_index": recent_index,
    }


def test_rank_empty():
    assert emulator_selector.rank_candidates([]) == []


def test_rank_running_beats_everything():
    candidates = [
        _candidate("Old_Pixel", api=30, running=True),
        _candidate("Pixel_8_Pro", api=36, recent_index=0),
    ]
    ranked = emulator_selector.rank_candidates(candidates)
    assert ranked[0]["name"] == "Old_Pixel"
    assert "Recommended" in ranked[0]["reasons"]
    assert "Currently running" in ranked[0]["reasons"]


def test_rank_recent_beats_latest_api_and_model():
    candidates = [
        _candidate("Legacy_AVD", api=28, recent_index=0),
        _candidate("Pixel_8_Pro", api=36),
    ]
    ranked = emulator_selector.rank_candidates(candidates)
    assert ranked[0]["name"] == "Legacy_AVD"
    assert "Recently used" in ranked[0]["reasons"]


def test_rank_latest_api_reason_and_tiebreak():
    candidates = [
        _candidate("Pixel_7", api=33),
        _candidate("Pixel_8_Pro", api=34),
    ]
    ranked = emulator_selector.rank_candidates(candidates)
    # Latest API (34) + better common-model rank wins.
    assert ranked[0]["name"] == "Pixel_8_Pro"
    assert any("Latest API (34)" in r for r in ranked[0]["reasons"])


def test_rank_common_model_breaks_tie_when_api_equal():
    candidates = [
        _candidate("Generic_Device", api=34, device="generic"),
        _candidate("Pixel_8", api=34, device="pixel_8"),
    ]
    ranked = emulator_selector.rank_candidates(candidates)
    assert ranked[0]["name"] == "Pixel_8"


def test_rank_is_stable_and_nondestructive():
    original = [_candidate("B_AVD", api=34), _candidate("A_AVD", api=34)]
    ranked = emulator_selector.rank_candidates(original)
    # Equal score -> alphabetical name tie-break.
    assert [c["name"] for c in ranked] == ["A_AVD", "B_AVD"]
    # Inputs are not mutated (no score/reasons leak back).
    assert "score" not in original[0]
    assert "reasons" not in original[0]


# ---------------------------------------------------------------------------
# recent-use config read/write (filesystem via tmp_path, no mocking needed)
# ---------------------------------------------------------------------------
def test_record_and_load_recent_dedup_and_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(emulator_selector, "RECENT_HISTORY_MAX", 3)
    config_path = tmp_path / "config.json"
    selector = emulator_selector.EmulatorSelector(config_path=config_path)

    selector.record_recent("A")
    selector.record_recent("B")
    selector.record_recent("A")  # move A back to front, dedup
    selector.record_recent("C")
    selector.record_recent("D")  # exceeds cap of 3

    recent = selector.load_recent()
    assert recent == ["D", "C", "A"]
    # File is valid JSON with the documented shape.
    assert json.loads(config_path.read_text())["recent"] == ["D", "C", "A"]


def test_load_recent_missing_file_returns_empty(tmp_path):
    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "nope.json")
    assert selector.load_recent() == []


# ---------------------------------------------------------------------------
# adb serial -> AVD name resolution (the subprocess boundary under adb_exec)
# ---------------------------------------------------------------------------
def _fake_adb(monkeypatch, responses):
    """Answer adb / emulator calls at the subprocess boundary under adb_exec.

    ``responses`` maps a command prefix tuple to (returncode, stdout, stderr).
    Patching ``adb_exec.subprocess`` patches the one module every one of these
    callers shares, so `emulator -list-avds` is served here too.
    """
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        for prefix, (returncode, stdout, stderr) in responses.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    return calls


def test_running_avd_names_resolves_serials(monkeypatch, tmp_path, recorded):
    calls = _fake_adb(
        monkeypatch,
        {("adb", "-s"): (0, recorded.text("emu_avd_name"), "")},
    )
    monkeypatch.setattr(
        emulator_selector,
        "get_connected_devices",
        lambda: [
            {"serial": "emulator-5554", "state": "device", "type": "emulator"},
            {"serial": "emulator-5556", "state": "offline", "type": "emulator"},
            {"serial": "ABC123", "state": "device", "type": "device"},
        ],
    )

    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "config.json")
    running = selector.running_avd_names()
    # Only the ready emulator is resolved; offline + real device are skipped.
    assert running == {"Pixel_9"}
    assert calls[0]["cmd"] == ["adb", "-s", "emulator-5554", "emu", "avd", "name"]
    assert calls[0]["kwargs"].get("timeout"), "the AVD-name probe went out unbounded"


def test_a_device_error_is_not_ranked_as_not_running(monkeypatch, tmp_path, recorded_anywhere):
    """An unanswered running-check must not quietly read as "idle".

    The invariant is unchanged; where it is enforced moved (R3). It used to be
    a raise out of this ranking, which made a *suggestion* fatal over a
    condition it cannot do anything about. Now the emulator is reported as
    unidentified with adb's own message as the reason, it contributes no name
    to the running set, and the thing that must not happen -- a second copy of
    a live AVD being booted -- is refused by `emulator_boot` itself, which is
    the only place that can actually prevent it.
    """
    _fake_adb(monkeypatch, {("adb", "-s"): (1, "", recorded_anywhere("adb_device_not_found"))})
    monkeypatch.setattr(
        emulator_selector,
        "get_connected_devices",
        lambda: [{"serial": "emulator-5554", "state": "device", "type": "emulator"}],
    )

    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "config.json")

    assert selector.running_avd_names() == set(), "a name was invented for an unanswered probe"
    probes = selector.probe_running()
    assert [p.serial for p in probes] == ["emulator-5554"], "the emulator vanished from the picture"
    assert probes[0].identified is False
    assert "emulator-5554" in probes[0].reason


def test_suggest_lists_an_emulator_it_could_not_identify(monkeypatch, tmp_path, recorded):
    """R3: the ranking says when it was taken over an incomplete picture.

    An emulator that will not say which AVD it is gets no "currently running"
    bonus -- ranking on a guess is worse than ranking without one -- but it used
    to vanish from the output entirely, so the ranking claimed a completeness it
    did not have.
    """
    serials = [
        line.split()[0]
        for line in recorded.text("adb_devices_multiple").splitlines()
        if line.split() and line.split()[0].startswith("emulator-")
    ]
    monkeypatch.setattr(
        emulator_selector,
        "get_connected_devices",
        lambda: [
            {"serial": serials[0], "state": "offline", "type": "emulator"},
            {"serial": serials[1], "state": "device", "type": "emulator"},
        ],
    )
    _fake_emulator_on_path(monkeypatch)
    _fake_adb(
        monkeypatch,
        {
            ("emulator",): (0, recorded.text("emulator_list_avds"), ""),
            ("adb", "-s"): (0, recorded.text("emu_avd_name"), ""),
        },
    )

    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "config.json")
    suggestions = selector.suggest(4)

    assert selector.running_avd_names() == {"Pixel_9"}, "the identified emulator lost its bonus"
    assert [p.serial for p in selector.unidentified] == [serials[0]]

    text = emulator_selector.format_candidates(suggestions, False, selector.unidentified)
    assert serials[0] in text and "offline" in text, f"the unidentified emulator is absent: {text}"

    payload = json.loads(
        emulator_selector.format_candidates(suggestions, True, selector.unidentified)
    )
    assert payload["unidentified_emulators"][0]["serial"] == serials[0]
    assert payload["unidentified_emulators"][0]["state"] == "offline"


def test_a_complete_picture_adds_no_noise_to_the_output(monkeypatch, tmp_path, recorded):
    """The false-positive control: nothing unidentified, nothing extra printed."""
    monkeypatch.setattr(
        emulator_selector,
        "get_connected_devices",
        lambda: [{"serial": "emulator-5554", "state": "device", "type": "emulator"}],
    )
    _fake_emulator_on_path(monkeypatch)
    _fake_adb(
        monkeypatch,
        {
            ("emulator",): (0, recorded.text("emulator_list_avds"), ""),
            ("adb", "-s"): (0, recorded.text("emu_avd_name"), ""),
        },
    )

    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "config.json")
    suggestions = selector.suggest(4)

    assert selector.unidentified == ()
    payload = json.loads(
        emulator_selector.format_candidates(suggestions, True, selector.unidentified)
    )
    assert "unidentified_emulators" not in payload


def test_a_listing_that_failed_is_disclosed_not_ranked_as_all_idle(
    monkeypatch, tmp_path, recorded, recorded_anywhere
):
    """F2: `except RuntimeError: return []` ranked every AVD as idle, silently.

    On a host where adb cannot be reached at all, every candidate lost its
    "currently running" bonus and nothing said why -- a ranking computed over a
    picture nobody had, printed as a recommendation. Ranking still proceeds
    (the AVDs on disk are real, and a suggestion is not destructive), but the
    reason is carried out to the reader.
    """
    _fake_emulator_on_path(monkeypatch)
    _fake_adb(
        monkeypatch,
        {
            ("emulator",): (0, recorded.text("emulator_list_avds"), ""),
            ("adb", "devices"): (1, "", recorded_anywhere("adb_device_not_found")),
        },
    )

    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "config.json")
    suggestions = selector.suggest(4)

    assert suggestions, "the ranking stopped instead of ranking what it could"
    assert all(not c["running"] for c in suggestions), "a running bonus was invented"
    assert selector.running_unavailable, "the failed listing left no trace"

    payload = json.loads(
        emulator_selector.format_candidates(
            suggestions, True, selector.unidentified, selector.running_unavailable
        )
    )
    assert payload["warnings"], "the JSON document does not disclose the gap"
    assert "running state unavailable" in payload["warnings"][0]

    text = emulator_selector.format_candidates(
        suggestions, False, selector.unidentified, selector.running_unavailable
    )
    assert "running state unavailable" in text


def test_read_avd_config_honours_the_resolved_avd_home(monkeypatch, tmp_path):
    """R4: it was hard-coded to ~/.android/avd.

    On a host that relocates the AVD tree every config read returned {}, so
    every AVD silently lost its API level, device profile and ABI from the
    ranking -- a ranking with no inputs, reported as a recommendation.
    """
    avd_home = tmp_path / "avds"
    (avd_home / "Pixel_9.avd").mkdir(parents=True)
    # The file's single config.ini text, reused rather than a second invention.
    (avd_home / "Pixel_9.avd" / "config.ini").write_text(CONFIG_INI)
    monkeypatch.setenv("ANDROID_AVD_HOME", str(avd_home))

    config = emulator_selector.read_avd_config("Pixel_9")

    assert config, "the config under $ANDROID_AVD_HOME was not read"
    assert config == emulator_selector.parse_config_ini(CONFIG_INI)


def _fake_emulator_on_path(monkeypatch):
    """Pin emulator resolution to the bare name so argv assertions stay stable."""
    monkeypatch.setattr(emulator_selector, "get_emulator_path", lambda: "emulator")


def test_avd_listing_is_bounded(monkeypatch, tmp_path):
    """`emulator -list-avds` is an SDK tool, not adb -- but still not unbounded."""
    _fake_emulator_on_path(monkeypatch)
    calls = _fake_adb(monkeypatch, {("emulator",): (0, "Pixel_9_Pro\n", "")})

    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "config.json")
    assert [c["name"] for c in selector.list_avds()] == ["Pixel_9_Pro"]
    assert calls[0]["cmd"] == ["emulator", "-list-avds"]
    assert calls[0]["kwargs"].get("timeout"), "`emulator -list-avds` went out unbounded"


def test_the_cli_names_an_emulator_it_could_not_identify(monkeypatch, tmp_path, capsys):
    """At the CLI boundary the agent gets the remedy, not a stack trace.

    Retargeted with R3: an unauthorized emulator no longer makes `--suggest`
    exit 1. It ranks what it can and says on stderr which emulator it could not
    identify, its state and adb's own remedy -- so the ranking never silently
    claims a completeness it does not have. The CLI's failure boundary is still
    covered, by the AVD-discovery tests below.
    """
    _fake_emulator_on_path(monkeypatch)
    monkeypatch.setattr(emulator_selector, "FALLBACK_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(emulator_selector, "LEGACY_CONFIG_PATH", tmp_path / "absent.json")
    _fake_adb(
        monkeypatch,
        {
            ("emulator",): (0, "Pixel_9_Pro\n", ""),
            ("adb", "devices"): (0, "List of devices attached\nemulator-5554\tdevice\n", ""),
            ("adb", "-s"): (1, "", "error: device unauthorized.\n"),
        },
    )
    monkeypatch.setattr(emulator_selector.sys, "argv", ["emulator_selector.py", "--suggest"])

    with pytest.raises(SystemExit) as exc:
        emulator_selector.main()

    assert exc.value.code == 0, "a suggestion was made fatal by one unreadable emulator"
    captured = capsys.readouterr()
    assert captured.err.startswith("Warning: ")
    assert "emulator-5554" in captured.err, "the warning does not say which emulator"
    assert "Traceback" not in captured.err
    assert "usb debugging" in captured.err.lower(), "no remedy named"
    assert "Recommended:" in captured.out, "the ranking it could compute was not printed"


# ---------------------------------------------------------------------------
# tunables
# ---------------------------------------------------------------------------
def test_default_tunables():
    assert emulator_selector.DEFAULT_SUGGEST_COUNT == 4
    assert emulator_selector.RECENT_HISTORY_MAX == 10
    assert emulator_selector.RUNNING_SCORE == 10000
    assert emulator_selector.RECENT_SCORE == 1000


def test_tunables_env_override(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_SELECTOR_COUNT", "2")
    monkeypatch.setenv("ANDROID_EMU_SELECTOR_RUNNING_PTS", "999")
    reloaded = importlib.reload(emulator_selector)
    try:
        assert reloaded.DEFAULT_SUGGEST_COUNT == 2
        assert reloaded.RUNNING_SCORE == 999
    finally:
        monkeypatch.delenv("ANDROID_EMU_SELECTOR_COUNT", raising=False)
        monkeypatch.delenv("ANDROID_EMU_SELECTOR_RUNNING_PTS", raising=False)
        importlib.reload(emulator_selector)


# ---------------------------------------------------------------------------
# Emulator resolution (SDK-root-on-PATH regression)
# ---------------------------------------------------------------------------
def test_list_avd_names_reports_a_permission_error_from_a_directory_argv0(monkeypatch, tmp_path):
    """`emulator` resolving to a directory raises PermissionError, not ENOENT."""
    monkeypatch.setattr(emulator_selector, "get_emulator_path", lambda: "/sdk/emulator/emulator")

    def boom(_cmd, **_kwargs):
        raise PermissionError(13, "Permission denied", "emulator")

    monkeypatch.setattr(emulator_selector.subprocess, "run", boom)

    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "config.json")
    with pytest.raises(SdkToolError) as excinfo:
        selector._list_avd_names()
    assert "$ANDROID_HOME/emulator" in str(excinfo.value), "no remedy for the SDK-root PATH"


def test_list_avd_names_reports_actionable_hint_when_unresolvable(monkeypatch, tmp_path):
    monkeypatch.setattr(emulator_selector, "get_emulator_path", lambda: None)

    def unexpected(_cmd, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("must not exec an unresolved emulator")

    monkeypatch.setattr(emulator_selector.subprocess, "run", unexpected)

    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "config.json")
    with pytest.raises(SdkToolError) as excinfo:
        selector._list_avd_names()
    message = str(excinfo.value)
    assert "Looked in" in message, "the failure does not say where it looked"
    assert "$ANDROID_HOME/emulator" in message


def test_list_avd_names_parses_recorded_emulator_output(monkeypatch, tmp_path, recorded):
    monkeypatch.setattr(emulator_selector, "get_emulator_path", lambda: "/sdk/emulator/emulator")

    seen: list[list[str]] = []

    class _Result:
        stdout = recorded.text("emulator_list_avds")
        stderr = ""
        returncode = 0

    def fake_run(cmd, **_kwargs):
        seen.append(cmd)
        return _Result()

    monkeypatch.setattr(emulator_selector.subprocess, "run", fake_run)

    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "config.json")

    expected = [ln.strip() for ln in recorded.lines("emulator_list_avds") if ln.strip()]
    assert selector._list_avd_names() == expected
    assert seen == [["/sdk/emulator/emulator", "-list-avds"]]


def test_boot_via_cli_uses_the_resolved_emulator_path(monkeypatch):
    monkeypatch.setattr(emulator_selector, "get_emulator_path", lambda: "/sdk/emulator/emulator")

    launched: list[list[str]] = []
    monkeypatch.setattr(
        emulator_selector.subprocess, "Popen", lambda cmd, **_kw: launched.append(cmd)
    )

    success, _message = emulator_selector.EmulatorSelector._boot_via_cli("Pixel_9", headless=False)

    assert success is True
    assert launched == [["/sdk/emulator/emulator", "-avd", "Pixel_9"]]


def test_boot_via_cli_reports_actionable_hint_when_unresolvable(monkeypatch):
    monkeypatch.setattr(emulator_selector, "get_emulator_path", lambda: None)

    def unexpected(_cmd, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("must not exec an unresolved emulator")

    monkeypatch.setattr(emulator_selector.subprocess, "Popen", unexpected)

    success, message = emulator_selector.EmulatorSelector._boot_via_cli("Pixel_9", headless=False)

    assert success is False
    assert "$ANDROID_HOME/emulator" in message


def test_the_cli_exits_non_zero_when_avd_discovery_fails(monkeypatch, tmp_path, capsys):
    """X3, as the agent meets it: `--suggest` on a host with no emulator binary.

    This replaces a test that asserted the hint was printed only once per run --
    a print-and-carry-on shape that only makes sense while a broken SDK still
    produces a ranking. It does not: `--suggest` now fails, once, with the
    remedy, and ranks nothing.
    """
    monkeypatch.setattr(emulator_selector, "get_emulator_path", lambda: None)
    monkeypatch.setattr(emulator_selector, "FALLBACK_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(emulator_selector, "LEGACY_CONFIG_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(emulator_selector.sys, "argv", ["emulator_selector.py", "--suggest"])

    with pytest.raises(SystemExit) as exc:
        emulator_selector.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.count("emulator' binary not found") == 1
    assert "Traceback" not in captured.err
    assert "No AVDs" not in captured.out, "an empty ranking was printed as well"


@pytest.mark.parametrize(
    "argv",
    [["--suggest", "--json"], ["--list", "--json"], ["--json"]],
    ids=["suggest", "list", "default"],
)
def test_a_json_caller_gets_the_failure_in_the_json(monkeypatch, tmp_path, capsys, argv):
    """--json means "answer on stdout in JSON", failures included.

    The first version of this fix caught SdkToolError in `main()`, which cannot
    see `args`: the exit status was right and stdout was EMPTY, so an agent
    parsing it got a decode error rather than the remedy. Every mode is now
    dispatched under a handler that knows what was asked for, which is why all
    three are exercised here rather than the one that was reported.
    """
    monkeypatch.setattr(emulator_selector, "get_emulator_path", lambda: None)
    monkeypatch.setattr(emulator_selector, "FALLBACK_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(emulator_selector, "LEGACY_CONFIG_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(emulator_selector.sys, "argv", ["emulator_selector.py", *argv])

    with pytest.raises(SystemExit) as exc:
        emulator_selector.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    payload = json.loads(captured.out)
    assert "error" in payload, f"--json reported no error: {payload}"
    assert "$ANDROID_HOME/emulator" in payload["error"], "the JSON error names no remedy"
    assert "candidates" not in payload, "an empty ranking was printed beside the error"
