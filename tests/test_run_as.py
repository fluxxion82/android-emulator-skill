"""What a run-as refusal is, decided once in ``common.run_as`` for both inspectors.

The final post-merge review of v0.8.0 (W4-D item 2) forged one: a FILE NAME
holding ``\\n`` or ``\\r`` and then ``run-as: package not debuggable``. ``cat``
names the file it could not open on stderr, ``run_as_refused`` split stderr with
``str.splitlines`` (a CR is a break there) and took any line starting
``run-as:`` with a marker in it -- so container.py and model_inspector.py both
answered a debuggable app's missing file with ``run_as_denied: true`` and
"install a debug build". The same attribution problem #32 closed for adb's own
errors, reopened one layer up.

What a refusal is now, each layer recorded:

- **The first line of stderr, split on ``\\n`` only.** run-as refuses before it
  runs the command (run_as_*_stderr, run_as_id_*: one line, stdout empty, exit
  1); a command that ran names itself first (``cat: ``, ``ls: ``), so whatever
  a name it prints holds is never the first line's head.
- **The whole recorded form for THIS package**: ``run-as: <reason>:
  <package>``, reason one of the three the device printed. Both inspectors;
  container.py read a marker list without the package until the review of #38.
- **One bounded ``run-as <package> id``** before the debug-build remedy is
  given. ``id`` takes no argument, so nothing in its answer is a name an app
  chose; run-as working for the package (run_as_id) overturns the verdict.

Forged names are DERIVED: ``run_as_cat_missing`` with only the file name
substituted (fake_toolchain.recorded_missing_file). Every other byte is recorded.
"""

from __future__ import annotations

import subprocess

import pytest
from fake_toolchain import FakeToolchain, Rule, assert_refusal, recorded_missing_file

from common import adb_exec, run_as

FIXTURE_PKG = "com.example.composefixture"
FIXTURE_DIR = f"/data/data/{FIXTURE_PKG}"

# Each recorded refusal and the package it names. The two `ls` ones were recorded
# for `run-as <pkg> ls`, the rest for the probe itself (`run-as <pkg> id`).
RECORDED_REFUSALS = (
    ("run_as_not_an_application_stderr", "com.android.settings"),
    ("run_as_unknown_package_stderr", "com.example.app1"),
    ("run_as_id_not_an_application", "com.android.settings"),
    ("run_as_id_not_debuggable", "com.android.chrome"),
    ("run_as_id_unknown_package", "com.example.app1"),
)

# The probe's own refusal for each package a refusal above names.
PROBE_REFUSAL_FOR = {
    "com.android.settings": "run_as_id_not_an_application",
    "com.android.chrome": "run_as_id_not_debuggable",
    "com.example.app1": "run_as_id_unknown_package",
}

# File names that forge a refusal line after cat's own words. The first four are
# the reviewers' (both separators, both markers); the last two are the exact form
# for the fixture app on a line of its own -- what matching the form alone, on
# any line, would accept: after a LF, or after a CR where `splitlines` breaks.
FORGED_NAMES = (
    "x\nrun-as: package not debuggable",
    "x\rrun-as: package not debuggable",
    "x\nrun-as: permission denied",
    "x\rrun-as: permission denied",
    f"x\nrun-as: package not debuggable: {FIXTURE_PKG}\n",
    f"x\rrun-as: package not debuggable: {FIXTURE_PKG}\n",
)
FORGED_IDS = (
    "lf-debuggable",
    "cr-debuggable",
    "lf-permission",
    "cr-permission",
    "lf-exact",
    "cr-exact",
)

# The reviewers' four, end to end. (The exact form needs the name's trailing
# newline, which container.py strips from a path it is given.)
REVIEWED_NAMES, REVIEWED_IDS = FORGED_NAMES[:4], FORGED_IDS[:4]


def _status(recorded, name: str) -> int:
    """The exit status MANIFEST.json recorded for ``name``."""
    return {f["name"]: f.get("exit_status") for f in recorded.manifest["fixtures"]}[name]


# ---------------------------------------------------------------------------
# The classifier, on recorded stderr.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("recording", "package"), RECORDED_REFUSALS)
def test_each_recorded_refusal_is_one_for_the_package_it_names(recorded, recording, package):
    stderr, status = recorded.text(recording), _status(recorded, recording)
    assert status != 0

    assert run_as.run_as_refused(stderr, status, package)
    assert not run_as.run_as_refused(stderr, status, FIXTURE_PKG), "another package's refusal"
    assert not run_as.run_as_refused(stderr, 0, package), "a zero exit refused"


@pytest.mark.parametrize("name", FORGED_NAMES, ids=FORGED_IDS)
def test_a_file_name_cannot_forge_a_run_as_refusal(recorded, name):
    """cat's stderr for a missing file whose name holds a refusal line is cat's, not run-as's."""
    stderr = recorded_missing_file(recorded, f"{FIXTURE_DIR}/shared_prefs", name)
    status = _status(recorded, "run_as_cat_missing")

    assert not run_as.run_as_refused(stderr, status, FIXTURE_PKG), stderr


@pytest.mark.parametrize(
    "recording", ["run_as_ls_shared_prefs_unreadable", "run_as_ls_databases_unreadable"]
)
def test_the_filesystem_refusing_a_path_is_not_run_as_refusing_the_app(recorded, recording):
    stderr, status = recorded.text(recording), _status(recorded, recording)
    assert not run_as.run_as_refused(stderr, status, FIXTURE_PKG)


# ---------------------------------------------------------------------------
# The probe: `adb shell -T run-as <package> id`, bounded.
# ---------------------------------------------------------------------------


def _answering(monkeypatch, *answers: tuple[str, str, int]) -> list[list[str]]:
    """subprocess.run answering each call in turn with (stdout, stderr, status), as bytes."""
    calls: list[list[str]] = []
    queue = list(answers)

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        assert kwargs.get("timeout"), "an unbounded probe"
        stdout, stderr, status = queue.pop(0)
        return subprocess.CompletedProcess(cmd, status, stdout.encode(), stderr.encode())

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    return calls


def test_the_probe_reads_run_as_working_off_the_recorded_id(monkeypatch, recorded):
    calls = _answering(
        monkeypatch, (recorded.text("run_as_id"), "", _status(recorded, "run_as_id"))
    )

    assert run_as.run_as_works("emulator-5554", FIXTURE_PKG, timeout=5) is True
    assert calls == [run_as.read_command("emulator-5554", FIXTURE_PKG, ["id"])]
    assert calls[0][-4:] == ["-T", "run-as", FIXTURE_PKG, "id"], "not the recorded command"


@pytest.mark.parametrize("recording", sorted(set(PROBE_REFUSAL_FOR.values())))
def test_the_probe_reads_each_recorded_refusal_as_not_working(monkeypatch, recorded, recording):
    _answering(monkeypatch, ("", recorded.text(recording), _status(recorded, recording)))
    assert run_as.run_as_works("emulator-5554", "com.android.settings", timeout=5) is False


def test_the_probe_needs_an_app_uid_not_just_a_zero_exit(monkeypatch, recorded):
    """`id` run as anything but an app (uid < 10000) is not run-as switching to it.

    Derived: run_as_id with only its uid number replaced by the shell's (2000).
    """
    answer = recorded.text("run_as_id")
    assert answer.startswith("uid=10205(")
    _answering(monkeypatch, (answer.replace("uid=10205(", "uid=2000(", 1), "", 0))
    assert run_as.run_as_works("emulator-5554", FIXTURE_PKG, timeout=5) is False


def test_a_refusal_the_probe_contradicts_is_not_a_refusal(monkeypatch, recorded):
    """The probe has the last word: the exact form for the package, then run-as working.

    Derived: run_as_id_not_debuggable with only the package name replaced by
    the fixture app's, answered by the fixture app's recorded probe.
    """
    stderr = recorded.text("run_as_id_not_debuggable").replace("com.android.chrome", FIXTURE_PKG)
    status = _status(recorded, "run_as_id_not_debuggable")
    assert run_as.run_as_refused(stderr, status, FIXTURE_PKG), "the derivation is not a refusal"
    calls = _answering(monkeypatch, (recorded.text("run_as_id"), "", 0))

    assert not run_as.refused("emulator-5554", FIXTURE_PKG, stderr, status, timeout=5)
    assert len(calls) == 1


def test_a_refusal_the_probe_confirms_stands(monkeypatch, recorded):
    stderr, status = recorded.text("run_as_id_not_debuggable"), 1
    calls = _answering(monkeypatch, ("", stderr, status))

    assert run_as.refused("emulator-5554", "com.android.chrome", stderr, status, timeout=5)
    assert len(calls) == 1


def test_no_probe_is_asked_when_stderr_holds_no_refusal(monkeypatch, recorded):
    calls = _answering(monkeypatch)
    stderr = recorded.text("run_as_cat_missing")
    assert not run_as.refused("emulator-5554", FIXTURE_PKG, stderr, 1, timeout=5)
    assert calls == []


def test_the_probes_device_error_is_not_put_to_get_state(monkeypatch, recorded, recorded_anywhere):
    """`id` names no file, so a device error on the probe is adb's: raised, never confirmed away.

    The device answers `device` to get-state a moment later (recorded), which
    is what dropped the error when the probe asked for confirmation (Codex
    review of #38). `adb devices` is the error's message listing serials.
    """
    calls: list[list[str]] = []
    lost = recorded_anywhere("adb_shell_device_not_found").encode()

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        assert kwargs.get("timeout"), "an unbounded call"
        if "get-state" in cmd:
            return subprocess.CompletedProcess(cmd, 0, recorded.text("adb_get_state").encode(), b"")
        if cmd[-1] == "devices":
            return subprocess.CompletedProcess(cmd, 0, recorded.text("adb_devices_single"), "")
        return subprocess.CompletedProcess(cmd, 1, b"", lost)

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)

    with pytest.raises(adb_exec.DeviceNotFoundError):
        run_as.run_as_works("emulator-5554", FIXTURE_PKG, timeout=5)
    assert not [call for call in calls if "get-state" in call], calls


# ---------------------------------------------------------------------------
# Both inspectors, end to end: a forged name is the file's failure, a recorded
# refusal is a denial, and the probe decides between them.
# ---------------------------------------------------------------------------


def _device(tmp_path, recorded, *rules: Rule) -> FakeToolchain:
    """A fake adb answering with recorded output: the device list and client banner, then ``rules``."""
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    chain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("version",), stdout=recorded.text("adb_version")),
            *rules,
        ],
        default_exit=127,
    )
    return chain


def _probes(chain: FakeToolchain) -> list[list[str]]:
    """The `run-as <pkg> id` calls the run made."""
    return [call for call in chain.calls("adb") if call[-1] == "id" and "run-as" in call]


# A read of a file the app does not have, per inspector: the argv, and the
# directory cat names in its error.
READS = {
    "container": (
        lambda name: ("container.py", "--package", FIXTURE_PKG, "--cat", f"shared_prefs/{name}"),
        f"{FIXTURE_DIR}/shared_prefs",
    ),
    "model_inspector": (
        lambda name: ("model_inspector.py", "--package", FIXTURE_PKG, "--db", name),
        f"{FIXTURE_DIR}/databases",
    ),
}


@pytest.mark.parametrize("inspector", sorted(READS))
@pytest.mark.parametrize("name", REVIEWED_NAMES, ids=REVIEWED_IDS)
def test_a_forged_name_is_the_files_failure_in_both_inspectors(tmp_path, recorded, inspector, name):
    """The reviewers' reproduction, end to end: exit 1 with cat's reason, never the debug-build hint."""
    argv, directory = READS[inspector]
    chain = _device(
        tmp_path,
        recorded,
        Rule(
            ("-T", "cat"),
            stderr=recorded_missing_file(recorded, directory, name),
            exit=_status(recorded, "run_as_cat_missing"),
        ),
        Rule(("run-as", "id"), stdout=recorded.text("run_as_id")),
    )

    result = chain.run(*argv(name), "--serial", "emulator-5554", "--json")

    payload = assert_refusal(
        result, json_mode=True, naming=("no such file or directory",), status=1
    )
    assert "run_as_denied" not in payload and "hint" not in payload, payload
    assert "Install a debug build" not in payload["remedy"], payload["remedy"]
    assert "run-as denied" not in payload["error"], payload["error"]


# Listing a directory, per inspector -- the command the refusal answers.
LISTINGS = {
    "container": (("container.py", "--shared-prefs"), ("find", "-printf")),
    "model_inspector": (("model_inspector.py",), ("find", "-printf")),
}


@pytest.mark.parametrize("inspector", sorted(LISTINGS))
@pytest.mark.parametrize("package", sorted(PROBE_REFUSAL_FOR))
def test_a_recorded_refusal_the_probe_confirms_is_a_denial_in_both_inspectors(
    tmp_path, recorded, inspector, package
):
    """The refusal the device printed keeps the debug-build remedy, after one probe.

    The listing is answered with the probe's own recorded refusal for the
    package: run-as refuses before it runs any command, so `ls` and `id` get the
    same line (compare run_as_not_an_application_stderr, run_as_id_not_an_application).
    """
    (script, *action), listing = LISTINGS[inspector]
    refusal = PROBE_REFUSAL_FOR[package]
    chain = _device(
        tmp_path,
        recorded,
        Rule(listing, stderr=recorded.text(refusal), exit=_status(recorded, refusal)),
        Rule(("run-as", "id"), stderr=recorded.text(refusal), exit=_status(recorded, refusal)),
    )

    result = chain.run(script, "--package", package, *action, "--serial", "emulator-5554", "--json")

    payload = assert_refusal(result, json_mode=True, naming=("run-as denied",), status=1)
    assert payload["run_as_denied"] is True, payload
    assert "Install a debug build" in payload["remedy"]
    assert recorded.text(refusal).strip() in payload["error"]
    assert _probes(chain) == [
        ["-s", "emulator-5554", "shell", "-T", "run-as", package, "id"]
    ], chain.calls("adb")


@pytest.mark.parametrize("inspector", sorted(LISTINGS))
def test_a_refusal_run_as_working_contradicts_is_the_listings_failure(
    tmp_path, recorded, inspector
):
    """A first line in the refusal's exact form, and the probe showing run-as working: not a denial.

    Derived: run_as_id_not_debuggable with only the package name replaced by
    the fixture app's, answering the listing; the probe answers as the fixture
    app's did (run_as_id). Without the probe this was "install a debug build"
    for an app that is one.
    """
    (script, *action), listing = LISTINGS[inspector]
    status = _status(recorded, "run_as_id_not_debuggable")
    line = recorded.text("run_as_id_not_debuggable").replace("com.android.chrome", FIXTURE_PKG)
    chain = _device(
        tmp_path,
        recorded,
        Rule(listing, stderr=line, exit=status),
        Rule(("run-as", "id"), stdout=recorded.text("run_as_id")),
    )

    result = chain.run(
        script, "--package", FIXTURE_PKG, *action, "--serial", "emulator-5554", "--json"
    )

    payload = assert_refusal(result, json_mode=True, naming=("not debuggable",), status=1)
    assert "run_as_denied" not in payload and "hint" not in payload, payload
    assert "Install a debug build" not in payload["remedy"], payload["remedy"]
    assert len(_probes(chain)) == 1, chain.calls("adb")


@pytest.mark.parametrize("inspector", sorted(LISTINGS))
@pytest.mark.parametrize("get_state", ["device", "not-found"])
def test_a_device_gone_during_the_probe_is_unavailable(
    tmp_path, recorded, recorded_anywhere, inspector, get_state
):
    """The probe is an adb call like any other: a device error there is exit 3, not a verdict.

    Whatever `adb get-state` answers next. The probe names no file, so its
    stderr is adb's alone and its device error is not put to get-state (Codex
    review of #38: with confirmation on, a `device` answer a moment later
    dropped the error, and the recorded refusal became "install a debug build"
    at exit 1). `adb_shell_device_not_found` is adb's own stderr for a shell
    command whose device is not there.
    """
    (script, *action), listing = LISTINGS[inspector]
    refusal = "run_as_id_not_an_application"
    after = (
        Rule(("get-state",), stdout=recorded.text("adb_get_state"))
        if get_state == "device"
        else Rule(("get-state",), stderr=recorded_anywhere("adb_device_not_found"), exit=1)
    )
    chain = _device(
        tmp_path,
        recorded,
        Rule(listing, stderr=recorded.text(refusal), exit=_status(recorded, refusal)),
        Rule(("run-as", "id"), stderr=recorded.text("adb_shell_device_not_found"), exit=1),
        after,
    )

    result = chain.run(
        script,
        "--package",
        "com.android.settings",
        *action,
        "--serial",
        "emulator-5554",
        "--json",
    )

    payload = assert_refusal(result, json_mode=True, naming=("not attached",), status=3)
    assert "run_as_denied" not in payload and "hint" not in payload, payload
    assert len(_probes(chain)) == 1, chain.calls("adb")
