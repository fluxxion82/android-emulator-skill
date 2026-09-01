"""Device-free tests for crash_triage.py.

Every parser assertion is made against ``tests/fixtures/recorded/`` output via
the ``recorded`` fixture — never an inlined literal. Where a case the recorder
did not capture is needed (a crash *loop*, an app frame in the stack), the input
is **derived from the recorded lines by substitution**, so the line shape under
test is still the one the device produced. Each derivation says which measured
fact it reproduces.

Facts these tests are pinned to, all measured on emulator-5554 / API 35:

- ``adb logcat -b crash -d`` prints one ``--------- beginning of crash``
  separator per dump, however many crashes follow. Crash blocks are delimited by
  ``FATAL EXCEPTION``, not by the separator.
- A repeat crash appends a further block with a new PID and timestamp; the
  exception, process and frames are identical (forced three times with
  ``adb shell am crash``, then dumped).
- A device that has not crashed dumps zero lines and exits 0.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import crash_triage
import pytest
from crash_triage import (
    group_crashes,
    parse_crash_buffer,
    scan_crash_buffer,
    select_app_frame,
)

from common.adb_exec import AdbResult

FIXTURE = "logcat_crash_java"


# === helpers: derived inputs, each anchored to a recorded line ===


def _repeat_of(text: str, *, pid: str, timestamp: str) -> str:
    """The recorded crash block again, as a real crash loop produces it.

    Measured: a repeat is another ``FATAL EXCEPTION`` block appended directly to
    the previous one — no second separator, no blank line — carrying a new PID
    (the process restarted) and a new timestamp, with everything else identical.
    Built by substituting into the recorded lines rather than by hand.
    """
    crash = parse_crash_buffer(text)[0]
    block = [line for line in text.splitlines() if not line.lstrip().startswith("---")]
    rewritten = [
        line.replace(str(crash.pid), pid).replace(crash.timestamp, timestamp) for line in block
    ]
    return "\n".join(rewritten)


def _rename_frame(text: str, index: int, symbol: str) -> str:
    """Rewrite one recorded frame's symbol, leaving the recorded line shape intact.

    The recorded trace is a shell-induced crash, so every frame in it is
    framework code. Renaming a frame is how a stack containing app code is
    exercised without hand-writing logcat output.
    """
    original = parse_crash_buffer(text)[0].frames[index].symbol
    return text.replace(original, symbol, 1)


def _fake_adb(monkeypatch, stdout: str, *, serial: str | None = "emulator-5554") -> list[dict]:
    """Route crash_triage's adb calls into a recorder. No subprocess is spawned."""
    calls: list[dict] = []

    def fake_run_adb(operation, device=None, *args, **kwargs):
        calls.append({"operation": operation, "serial": device, "args": args, "kwargs": kwargs})
        return AdbResult(returncode=0, stdout=stdout, stderr="", command=["adb", operation])

    monkeypatch.setattr(crash_triage, "run_adb", fake_run_adb)
    monkeypatch.setattr(crash_triage, "resolve_device_identifier", lambda _identifier: serial)
    return calls


# === parsing the recorded trace ===


def test_recorded_trace_parses_into_one_crash(recorded):
    crashes = parse_crash_buffer(recorded.text(FIXTURE))
    assert len(crashes) == 1

    crash = crashes[0]
    assert crash.package == "com.example.composefixture"
    assert crash.pid == 5731
    assert crash.tid == 5731
    assert crash.thread == "main"
    assert crash.exception_class == "android.app.RemoteServiceException$CrashedByAdbException"
    assert crash.message == "shell-induced crash"
    assert crash.short_class == "RemoteServiceException$CrashedByAdbException"


def test_recorded_trace_keeps_the_whole_frame_list(recorded):
    """The frame list is the only multi-line part; losing frames loses the trace."""
    text = recorded.text(FIXTURE)
    frames = parse_crash_buffer(text)[0].frames

    bodies = [
        line.split("AndroidRuntime:", maxsplit=1)[1].strip()
        for line in text.splitlines()
        if "AndroidRuntime:" in line
    ]
    assert len(frames) == sum(1 for body in bodies if body.startswith("at "))
    assert frames[0].symbol == "android.app.ActivityThread.throwRemoteServiceException"
    assert frames[0].source == "ActivityThread.java:2257"
    assert frames[-1].symbol == "com.android.internal.os.ZygoteInit.main"


def test_recorded_trace_keeps_non_file_frame_sources_verbatim(recorded):
    """`Native Method` and `Unknown Source:0` are not file:line and must not be mangled."""
    frames = parse_crash_buffer(recorded.text(FIXTURE))[0].frames
    sources = {frame.source for frame in frames}
    assert "Native Method" in sources
    assert "Unknown Source:0" in sources


def test_separator_line_is_not_mistaken_for_content(recorded):
    """The `--------- beginning of crash` line is a reader artifact, not a crash."""
    text = recorded.text(FIXTURE)
    assert any(
        line.startswith("---------") for line in text.splitlines()
    ), "fixture no longer contains the separator; re-check what the buffer prints"

    scan = scan_crash_buffer(text)
    assert len(scan.crashes) == 1
    assert scan.unparsed_lines == 0, "a recorded line was dropped without being accounted for"
    assert scan.orphan_lines == 0
    assert scan.other_tags == {}


def test_every_recorded_line_is_either_parsed_or_counted(recorded):
    """Nothing may vanish silently: parsed lines plus skipped lines equal the dump."""
    text = recorded.text(FIXTURE)
    scan = scan_crash_buffer(text)
    crash = scan.crashes[0]

    separators = sum(1 for line in text.splitlines() if line.startswith("---------"))
    accounted = (
        separators
        + 3  # FATAL EXCEPTION, Process, exception header
        + len(crash.frames)
        + len(crash.extra_lines)
        + scan.orphan_lines
        + scan.unparsed_lines
        + sum(scan.other_tags.values())
    )
    assert accounted == scan.total_lines


# === empty buffer (a healthy device) ===


def test_empty_buffer_parses_to_no_crashes():
    """Measured: a device that has not crashed dumps zero lines."""
    assert parse_crash_buffer("") == []
    assert scan_crash_buffer("").total_lines == 0


def test_empty_buffer_is_reported_as_no_crashes(monkeypatch, capsys):
    _fake_adb(monkeypatch, "")
    monkeypatch.setattr(sys, "argv", ["crash_triage.py"])

    assert crash_triage.main() == 0, "an empty crash buffer is a healthy answer, not an error"
    out = capsys.readouterr().out
    assert "No crashes" in out


def test_empty_buffer_exits_zero_even_with_fail_on_crash(monkeypatch, capsys):
    _fake_adb(monkeypatch, "")
    monkeypatch.setattr(sys, "argv", ["crash_triage.py", "--fail-on-crash", "--json"])

    assert crash_triage.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["crash_count"] == 0
    assert payload["groups"] == []


# === dedup / counting a crash loop ===


def test_repeated_crash_is_one_group_with_a_count(recorded):
    text = recorded.text(FIXTURE)
    loop = text + "\n" + _repeat_of(text, pid="9042", timestamp="09-01 09:25:00.276")

    crashes = parse_crash_buffer(loop)
    assert len(crashes) == 2, "the second block was not recognised as a separate crash"
    assert [crash.pid for crash in crashes] == [5731, 9042]

    groups = group_crashes(crashes)
    assert len(groups) == 1
    assert groups[0].count == 2
    assert [crash.pid for crash in groups[0].occurrences] == [5731, 9042]


def test_a_restarted_process_does_not_split_the_group(recorded):
    """PID and timestamp change on every repeat, so neither may be in the key."""
    text = recorded.text(FIXTURE)
    original = parse_crash_buffer(text)[0]
    repeat = parse_crash_buffer(_repeat_of(text, pid="9042", timestamp="09-01 09:25:00.276"))[0]

    assert original.pid != repeat.pid
    assert original.timestamp != repeat.timestamp
    assert crash_triage.crash_signature(original) == crash_triage.crash_signature(repeat)


def test_a_different_exception_is_a_different_group(recorded):
    text = recorded.text(FIXTURE)
    original_class = parse_crash_buffer(text)[0].exception_class
    other = _repeat_of(text, pid="9042", timestamp="09-01 09:25:00.276").replace(
        original_class, "java.lang.IllegalStateException"
    )

    groups = group_crashes(parse_crash_buffer(text + "\n" + other))
    assert len(groups) == 2
    assert {group.count for group in groups} == {1}


def test_same_exception_at_a_different_site_is_a_different_group(recorded):
    """Two faults raising the same class in the same app are not one bug."""
    text = recorded.text(FIXTURE)
    app_a = _rename_frame(text, 0, "com.example.composefixture.HomeScreen.render")
    app_b = _rename_frame(
        _repeat_of(text, pid="9042", timestamp="09-01 09:25:00.276"),
        0,
        "com.example.composefixture.Settings.save",
    )

    groups = group_crashes(parse_crash_buffer(app_a + "\n" + app_b))
    assert len(groups) == 2


def test_groups_are_ordered_by_count(recorded):
    text = recorded.text(FIXTURE)
    once = _rename_frame(text, 0, "com.example.composefixture.Rare.path")
    twice = _repeat_of(text, pid="9042", timestamp="09-01 09:25:00.276")
    thrice = _repeat_of(text, pid="9111", timestamp="09-01 09:25:10.276")

    groups = group_crashes(parse_crash_buffer("\n".join([once, twice, thrice])))
    assert [group.count for group in groups] == [2, 1]


# === app-frame selection ===


def test_recorded_trace_has_no_app_frame(recorded):
    """Ground truth: the recorded crash is shell-induced, so every frame is framework.

    Reporting "none" here is the honest answer; inventing an app frame from
    `android.app.ActivityThread` would send an agent to the wrong file.
    """
    crash = parse_crash_buffer(recorded.text(FIXTURE))[0]
    choice = crash.app_frame()

    assert choice.frame is None
    assert choice.basis == "none"
    assert all(frame.is_framework for frame in crash.frames)


def test_app_frame_is_the_topmost_frame_in_the_apps_package(recorded):
    text = recorded.text(FIXTURE)
    text = _rename_frame(text, 3, "com.example.composefixture.HomeScreen.render")
    text = _rename_frame(text, 5, "com.example.composefixture.MainActivity.onCreate")

    crash = parse_crash_buffer(text)[0]
    choice = crash.app_frame()

    assert choice.basis == "package"
    assert choice.frame.symbol == "com.example.composefixture.HomeScreen.render"
    assert choice.frame is not crash.frames[0], "the framework frame above it was preferred"


def test_app_frame_matches_a_sibling_module_by_vendor_prefix(recorded):
    """A multi-module app crashes in `com.example.core`, not in its applicationId."""
    text = _rename_frame(recorded.text(FIXTURE), 2, "com.example.core.Repository.load")

    choice = parse_crash_buffer(text)[0].app_frame()
    assert choice.basis == "vendor"
    assert choice.frame.symbol == "com.example.core.Repository.load"


def test_app_frame_falls_back_to_the_topmost_non_framework_frame(recorded):
    """No app frame at all: a third-party lib frame is the best remaining lead."""
    text = _rename_frame(recorded.text(FIXTURE), 2, "okhttp3.RealCall.execute")

    choice = parse_crash_buffer(text)[0].app_frame()
    assert choice.basis == "non-framework"
    assert choice.frame.symbol == "okhttp3.RealCall.execute"


def test_select_app_frame_on_an_empty_stack():
    assert select_app_frame([], "com.example.app").frame is None
    assert select_app_frame([], "com.example.app").basis == "none"


def test_secondary_process_still_matches_its_package(recorded):
    """`Process: com.example.app:remote` must not defeat the package match."""
    text = recorded.text(FIXTURE)
    text = text.replace("com.example.composefixture, PID", "com.example.composefixture:remote, PID")
    text = _rename_frame(text, 4, "com.example.composefixture.SyncService.onHandleWork")

    crash = parse_crash_buffer(text)[0]
    assert crash.package == "com.example.composefixture:remote"
    assert crash.app_frame().basis == "package"


# === package filter ===


def test_package_filter_keeps_only_the_named_process(recorded):
    text = recorded.text(FIXTURE)
    other = _repeat_of(text, pid="9042", timestamp="09-01 09:25:00.276").replace(
        "com.example.composefixture", "com.other.app"
    )
    scan = scan_crash_buffer(text + "\n" + other)

    report = crash_triage.build_report(
        scan, device="emulator-5554", package_filter="com.example.composefixture"
    )
    assert report["crash_count"] == 1
    assert report["crashes_in_buffer"] == 2
    assert report["groups"][0]["package"] == "com.example.composefixture"


def test_package_filter_miss_says_the_buffer_was_not_empty(recorded):
    scan = scan_crash_buffer(recorded.text(FIXTURE))
    report = crash_triage.build_report(
        scan, device="emulator-5554", package_filter="com.not.installed"
    )

    text = crash_triage.format_report(report)
    assert "com.not.installed" in text
    assert "1 crash(es) from other processes" in text


# === adb surface ===


def test_reads_the_dedicated_crash_buffer(monkeypatch, recorded):
    calls = _fake_adb(monkeypatch, recorded.text(FIXTURE))

    report = crash_triage.CrashTriage(serial=None).triage()

    assert len(calls) == 1
    call = calls[0]
    assert call["operation"] == "logcat"
    assert call["serial"] == "emulator-5554"
    assert list(call["args"]) == ["-b", "crash", "-d", "-v", "threadtime"]
    assert call["kwargs"]["timeout"], "the adb call must be bounded"
    assert report["crash_count"] == 1


def test_clear_uses_the_crash_buffer_clear_flag(monkeypatch, capsys):
    calls = _fake_adb(monkeypatch, "")
    monkeypatch.setattr(sys, "argv", ["crash_triage.py", "--clear"])

    assert crash_triage.main() == 0
    assert calls[0]["args"] == ("-b", "crash", "-c")
    assert "cleared" in capsys.readouterr().out.lower()


def test_clear_refuses_a_package_it_cannot_honour(monkeypatch):
    """adb has no per-package clear, so pretending to do one would be a lie."""
    _fake_adb(monkeypatch, "")
    monkeypatch.setattr(sys, "argv", ["crash_triage.py", "--clear", "--package", "com.example.app"])

    with pytest.raises(SystemExit) as excinfo:
        crash_triage.main()
    assert excinfo.value.code == 2


# === CLI contract ===


def test_default_output_is_a_few_lines(monkeypatch, recorded, capsys):
    _fake_adb(monkeypatch, recorded.text(FIXTURE))
    monkeypatch.setattr(sys, "argv", ["crash_triage.py"])

    assert crash_triage.main() == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) <= 5, f"default output is meant to stay tight, got:\n{out}"
    assert "com.example.composefixture" in out[1]


def test_default_output_caps_groups_and_says_it_did(monkeypatch, recorded, capsys):
    """A device with many distinct crashes must not flood the default output."""
    text = recorded.text(FIXTURE)
    original_class = parse_crash_buffer(text)[0].exception_class
    blocks = [
        _repeat_of(text, pid=str(9000 + index), timestamp="09-01 09:25:00.276").replace(
            original_class, f"com.example.composefixture.Boom{index}"
        )
        for index in range(crash_triage.MAX_GROUPS_SHOWN + 2)
    ]
    _fake_adb(monkeypatch, "\n".join(blocks))
    monkeypatch.setattr(sys, "argv", ["crash_triage.py"])

    crash_triage.main()
    out = capsys.readouterr().out
    assert "+2 more group(s)" in out


def test_verbose_shows_the_whole_stack(monkeypatch, recorded, capsys):
    _fake_adb(monkeypatch, recorded.text(FIXTURE))
    monkeypatch.setattr(sys, "argv", ["crash_triage.py", "--verbose"])

    crash_triage.main()
    out = capsys.readouterr().out
    frames = parse_crash_buffer(recorded.text(FIXTURE))[0].frames
    for frame in frames:
        assert frame.symbol in out


def test_json_output_is_machine_readable(monkeypatch, recorded, capsys):
    _fake_adb(monkeypatch, recorded.text(FIXTURE))
    monkeypatch.setattr(sys, "argv", ["crash_triage.py", "--json"])

    assert crash_triage.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["crash_count"] == 1
    assert payload["unique_count"] == 1
    group = payload["groups"][0]
    assert group["package"] == "com.example.composefixture"
    assert group["app_frame"] is None
    assert group["app_frame_basis"] == "none"
    assert group["top_frame"].startswith("android.app.ActivityThread.throwRemoteServiceException")


def test_crashes_found_exits_zero_unless_asked_otherwise(monkeypatch, recorded, capsys):
    """The documented contract: exit status answers 'did the command work'."""
    _fake_adb(monkeypatch, recorded.text(FIXTURE))
    monkeypatch.setattr(sys, "argv", ["crash_triage.py"])
    assert crash_triage.main() == 0
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["crash_triage.py", "--fail-on-crash"])
    assert crash_triage.main() == crash_triage.EXIT_CRASHES_FOUND


def test_help_documents_the_exit_status_choice():
    help_text = crash_triage._build_parser().format_help()
    assert "--fail-on-crash" in help_text
    assert "Exit status" in help_text


def test_adb_failure_is_distinguishable_from_a_crash_free_device(monkeypatch, capsys):
    def explode(*_args, **_kwargs):
        raise crash_triage.AdbError("device offline")

    monkeypatch.setattr(crash_triage, "run_adb", explode)
    monkeypatch.setattr(crash_triage, "resolve_device_identifier", lambda _i: "emulator-5554")
    monkeypatch.setattr(sys, "argv", ["crash_triage.py"])

    assert crash_triage.main() == crash_triage.EXIT_ERROR
    assert "device offline" in capsys.readouterr().err


# === unparsed content is named, never dropped ===


def test_native_crash_lines_are_counted_rather_than_ignored(recorded):
    """The crash buffer also carries tombstone output this parser does not read.

    Derived from a recorded line so the shape is real: the same threadtime line,
    retagged. Reporting "0 crashes" over such a buffer without saying anything
    would be the silent-failure mode this repo exists to prevent.
    """
    line = next(
        entry for entry in recorded.text(FIXTURE).splitlines() if "E AndroidRuntime: FATAL" in entry
    )
    native = line.replace("E AndroidRuntime:", "F DEBUG:")

    scan = scan_crash_buffer(native)
    assert scan.crashes == []
    assert scan.other_tags == {"DEBUG": 1}

    report = crash_triage.build_report(scan, device="emulator-5554", package_filter=None)
    assert "DEBUG" in crash_triage.format_report(report)


# === live device (opt in with `pytest -m emulator`) ===


@pytest.mark.emulator
def test_crash_buffer_triage_on_a_live_device(live_device, scripts_dir):
    """Semantic floor: the agent gets a usable answer from a real device."""
    result = subprocess.run(
        [sys.executable, str(scripts_dir / "crash_triage.py"), "--serial", live_device, "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "crash_count" in payload
    assert isinstance(payload["groups"], list)


@pytest.mark.emulator
def test_a_forced_crash_shows_up_in_triage(live_device, scripts_dir, adb):
    """`am crash` produces a real trace; triage must find and attribute it.

    Keyed on the PID of the process that was crashed, not merely on "some crash
    exists": the crash buffer is not cleared by reading, so a stale entry from an
    earlier run would satisfy a count-based assertion without this run working.

    Uses the repo's own fixture app when it is installed, and skips otherwise
    rather than crashing an unrelated app on someone's device.
    """
    package = "com.example.composefixture"

    def device(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [adb, "-s", live_device, "shell", *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    if package not in device("pm", "list", "packages", package).stdout:
        pytest.skip(f"{package} is not installed on {live_device}")

    # Force-stop before launching so the PID below is guaranteed fresh. Without
    # it, a process left alive by an earlier forced crash keeps its PID, and its
    # crash is already in the buffer — the assertion would pass without this run
    # having done anything.
    device("am", "force-stop", package)
    device("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")

    # `am crash` is a no-op against a process that is not running, so establish
    # the PID first; it is also the identity the assertion below keys on.
    pid = ""
    deadline = time.time() + 30
    while time.time() < deadline and not pid:
        running = device("pidof", package).stdout.split()
        pid = running[0] if running else ""
        if not pid:
            time.sleep(1)
    if not pid:
        pytest.skip(f"{package} would not stay running on {live_device}")

    def triage() -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(scripts_dir / "crash_triage.py"),
                "--serial",
                live_device,
                "--package",
                package,
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    before = triage()
    assert not any(
        int(pid) in group["pids"] for group in before["groups"]
    ), f"pid {pid} is already in the crash buffer; the assertion below would be vacuous"

    device("am", "crash", package)

    payload = triage()
    deadline = time.time() + 30
    while time.time() < deadline and not any(int(pid) in g["pids"] for g in payload["groups"]):
        time.sleep(1)
        payload = triage()

    matched = [group for group in payload["groups"] if int(pid) in group["pids"]]
    assert matched, f"the crash forced on pid {pid} was not reported: {payload}"

    group = matched[0]
    assert group["package"].startswith(package)
    assert group["exception_class"], "a crash was found with no exception class parsed"
    assert group["frames"], "a crash was found with no stack frames parsed"
