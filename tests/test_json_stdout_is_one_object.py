"""Under --json, a non-streaming mode's stdout is one JSON value -- failures included.

The boundary (common.cli) can only append its refusal object; it cannot take
back what a script already printed. These are the producers the code review of
the boundary PR reproduced printing prose ahead of the object (verbose progress,
a "Report saved" line, a dimension mismatch, help text), plus the logcat
failure that exited 1 with no object at all. Each mode runs as the real CLI, in
a subprocess, against a toolchain where every tool fails -- the exit-code
sweep's fault injector -- and the whole of stdout must parse.

The fake adb's words are recorded (``adb_shell_device_not_found``), not written
here. N5 generalises this to every sweep mode; these producers are pinned here
because this is where they were fixed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"
TIMEOUT = 60
WORKDIR = "<workdir>"

_FAKE_ADB = """#!/bin/sh
cat <<'RECORDED' >&2
{recorded}
RECORDED
exit 1
"""

_FAKE_MISSING = """#!/bin/sh
echo "$(basename "$0"): command not found" >&2
exit 127
"""


class Mode(NamedTuple):
    script: str
    argv: tuple[str, ...]
    status: int  # pinned from a run, not guessed
    why: str

    @property
    def ident(self) -> str:
        return f"{self.script.removesuffix('.py')} {' '.join(self.argv)}"


MODES = (
    Mode(
        "emulator_delete.py",
        ("--all", "--yes", "--json", "--verbose"),
        1,
        "verbose progress printed ahead of the refusal",
    ),
    Mode(
        "emulator_delete.py",
        ("--old", "1", "--yes", "--json", "--verbose"),
        1,
        "the same, for --old",
    ),
    Mode(
        "emulator_delete.py",
        ("--name", "probe", "--yes", "--json", "--verbose"),
        1,
        "the same, for one AVD",
    ),
    Mode("emulator_delete.py", ("--json",), 1, "no target printed the help text to stdout"),
    # The point here is that the verbose progress line no longer precedes the
    # object on stdout. The AVD home in this environment does not EXIST (no
    # home/.android/avd), so the object is a refusal, exit 1 -- this was pinned
    # at 0, an empty listing for "could not look", which is N1.
    Mode("emulator_erase.py", ("--all", "--json", "--verbose"), 1, "verbose progress"),
    Mode("emulator_erase.py", ("--name", "probe", "--json", "--verbose"), 1, "verbose progress"),
    Mode(
        "app_state_capture.py",
        ("--package", "com.example.app", "--output", f"{WORKDIR}/snap", "--json", "--verbose"),
        # Every adb call answers "device not found": the refusal is that, exit 3
        # (W3-L, A7; the partial-capture path made it 1).
        3,
        "verbose progress ahead of the partial-capture refusal",
    ),
    Mode("accessibility_audit.py", ("--json",), 3, "a failed audit left stdout empty"),
    Mode(
        "privacy_manager.py",
        ("--package", "com.example.app", "--json"),
        1,
        "no operation printed the help text to stdout",
    ),
    Mode("log_monitor.py", ("--last", "1m", "--json"), 3, "logcat failed: no object at all"),
)


@pytest.fixture(scope="module")
def no_toolchain(tmp_path_factory, recorded_anywhere) -> dict[str, str]:
    root = tmp_path_factory.mktemp("json-stdout")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    not_found = recorded_anywhere("adb_shell_device_not_found").rstrip("\n")
    (bin_dir / "adb").write_text(_FAKE_ADB.format(recorded=not_found), encoding="utf-8")
    for tool in ("emulator", "avdmanager", "sdkmanager"):
        (bin_dir / tool).write_text(_FAKE_MISSING, encoding="utf-8")
    for entry in bin_dir.iterdir():
        entry.chmod(0o755)
    for name in ("sdk", "home"):
        (root / name).mkdir()
    return {
        "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
        "HOME": str(root / "home"),
        "ANDROID_HOME": str(root / "sdk"),
        "ANDROID_SDK_ROOT": str(root / "sdk"),
        "ANDROID_SDK_HOME": str(root / "home"),
        "ANDROID_SERIAL": "emulator-5554",
    }


def _run(script: str, argv: list[str], env: dict[str, str], cwd: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *argv],
        env={**env, "TMPDIR": str(cwd)},
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


def _assert_one_object(completed, ident: str) -> dict:
    assert "Traceback" not in completed.stderr, f"{ident} crashed:\n{completed.stderr[-1500:]}"
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"{ident}: stdout under --json is not one JSON value ({error}):\n"
            f"{completed.stdout[:800]!r}"
        ) from None
    assert isinstance(value, dict), f"{ident}: stdout is JSON but not an object: {value!r}"
    return value


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.ident)
def test_stdout_under_json_is_one_value(mode: Mode, no_toolchain, tmp_path):
    argv = [arg.replace(WORKDIR, str(tmp_path)) for arg in mode.argv]
    completed = _run(mode.script, argv, no_toolchain, tmp_path)

    payload = _assert_one_object(completed, mode.ident)
    assert (
        completed.returncode == mode.status
    ), f"{mode.ident} exited {completed.returncode}, expected {mode.status}: {payload}"


def test_visual_diff_dimension_mismatch_is_the_refusal(no_toolchain, tmp_path):
    """Three lines of prose on stdout, under --json too; now the boundary's object."""
    from PIL import Image

    Image.new("RGB", (10, 10)).save(tmp_path / "base.png")
    Image.new("RGB", (12, 10)).save(tmp_path / "cur.png")

    completed = _run(
        "visual_diff.py",
        ["base.png", "cur.png", "--output", str(tmp_path / "out"), "--json"],
        no_toolchain,
        tmp_path,
    )

    payload = _assert_one_object(completed, "visual_diff mismatch")
    assert completed.returncode == 1
    assert payload["success"] is False
    assert "(10, 10)" in payload["error"] and "(12, 10)" in payload["error"]


def test_visual_diff_artifact_warning_does_not_break_the_report(no_toolchain, tmp_path):
    """The comparison succeeded; the pictures could not be written. Exit 0, and
    the warning goes to stderr rather than ahead of the report."""
    from PIL import Image

    Image.new("RGB", (10, 10)).save(tmp_path / "base.png")
    Image.new("RGB", (10, 10)).save(tmp_path / "cur.png")
    out = tmp_path / "out"
    (out / "diff.png").mkdir(parents=True)  # a directory where the image goes

    completed = _run(
        "visual_diff.py",
        ["base.png", "cur.png", "--output", str(out), "--json"],
        no_toolchain,
        tmp_path,
    )

    report = _assert_one_object(completed, "visual_diff warning")
    assert completed.returncode == 0
    assert report["summary"]["passed"] is True
    assert "Could not generate images" in completed.stderr


def test_visual_diff_report_file_failure_is_reported_before_any_result(no_toolchain, tmp_path):
    """diff-report.json was written AFTER the result was printed, so a failed
    write appended the refusal to a success already on stdout: two objects."""
    from PIL import Image

    Image.new("RGB", (10, 10)).save(tmp_path / "base.png")
    Image.new("RGB", (10, 10)).save(tmp_path / "cur.png")
    out = tmp_path / "out"
    (out / "diff-report.json").mkdir(parents=True)  # the report cannot be written

    completed = _run(
        "visual_diff.py",
        ["base.png", "cur.png", "--output", str(out), "--json"],
        no_toolchain,
        tmp_path,
    )

    payload = _assert_one_object(completed, "visual_diff report write")
    assert completed.returncode == 1
    assert payload["success"] is False


def test_accessibility_audit_report_saved_line_is_not_in_the_json(
    monkeypatch, tmp_path, capsys, recorded
):
    """A successful audit with --output printed "Report saved: ..." ahead of the
    JSON. In-process, on a recorded hierarchy, because a successful audit needs a
    screen and the fake toolchain has none."""
    import xml.etree.ElementTree as ET

    import accessibility_audit

    from common import cli
    from common.device_utils import _xml_to_dict

    hierarchy = _xml_to_dict(ET.fromstring(recorded.text("uiautomator_compose_default")))
    monkeypatch.setattr(accessibility_audit, "get_ui_hierarchy", lambda _serial: hierarchy)
    monkeypatch.setattr(
        accessibility_audit.AccessibilityAuditor, "_resolve_density", lambda self: 160
    )

    with pytest.raises(SystemExit) as exc:
        cli.run(accessibility_audit.main, ["--json", "--output", str(tmp_path)])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert "issues" in report
    # The audit's own CI gate: exit 1 when a critical issue is present.
    assert exc.value.code == (1 if report["critical"] else 0)
    assert "Report saved" in captured.err


def test_accessibility_audit_that_cannot_run_adb_is_one_object(no_toolchain, tmp_path):
    """The audit's generic failure branch -- not a device error, so it is the
    audit's own refusal, through cli.fail -- under --json. No adb at all on PATH:
    run_adb raises AdbNotInstalledError, which audit() reports as a failed audit.
    """
    env = {**no_toolchain, "PATH": os.pathsep.join(["/usr/bin", "/bin"])}
    completed = _run("accessibility_audit.py", ["--json"], env, tmp_path)

    payload = _assert_one_object(completed, "accessibility_audit without adb")
    assert completed.returncode == 1
    assert payload["success"] is False
    assert payload["error"].startswith("Audit failed: adb could not be run")


def test_visual_diff_without_pillow_is_one_object(no_toolchain, tmp_path):
    """Pillow missing: one object, exit 1. Run in a subprocess whose import of PIL
    fails (sys.modules["PIL"] = None), since the test environment has Pillow."""
    probe = (
        "import runpy, sys; sys.modules['PIL'] = None; "
        f"sys.path.insert(0, {str(SCRIPTS)!r}); "
        f"sys.argv = ['visual_diff.py', 'a.png', 'b.png', '--json']; "
        f"runpy.run_path({str(SCRIPTS / 'visual_diff.py')!r}, run_name='__main__')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        env={**no_toolchain, "TMPDIR": str(tmp_path)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )

    payload = _assert_one_object(completed, "visual_diff without Pillow")
    assert completed.returncode == 1
    assert payload["error"] == "Pillow is not installed"
