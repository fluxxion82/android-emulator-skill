"""Unit tests for android_health_check.sh.

The script is POSIX bash, so the "pure logic" under test is its decision making:
the Python-version gate (>= 3.12), the hard-requirement exit-code contract (adb
missing => non-zero), the device-listing parse from ``adb devices -l``, and the
PASS/WARN/FAIL summary classification.

No real SDK or device is required: we build a sandbox ``bin`` directory of fake
``adb`` / ``emulator`` / ``python3`` / ``java`` shell stubs and point ``PATH`` at
it, which is the shell-script equivalent of monkeypatching ``subprocess.run``.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    REPO_ROOT
    / "android-emulator-skill"
    / "skills"
    / "android-emulator-skill"
    / "scripts"
    / "android_health_check.sh"
)


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    """Create an executable shell stub named ``name`` in ``bin_dir``."""
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _adb_stub(devices_body: str) -> str:
    """adb stub: --version prints a banner, ``devices -l`` prints header + body.

    ``devices_body`` is emitted via a quoted heredoc so the device lines are
    printed verbatim (not executed as shell commands).
    """
    return (
        'if [ "$1" = "--version" ]; then\n'
        '  echo "Android Debug Bridge version 1.0.41"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "devices" ]; then\n'
        '  echo "List of devices attached"\n'
        "  cat <<'DEVICES'\n"
        f"{devices_body}\n"
        "DEVICES\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )


def _python_stub(version: str, pillow: bool = False) -> str:
    """python3 stub honoring the queries the script makes.

    ``version`` is "MAJOR.MINOR". ``import PIL`` succeeds only when ``pillow`` is
    True, so callers control whether the Pillow check passes or warns without
    needing a real Pillow install.
    """
    major, minor = version.split(".")
    pil_exit = "0" if pillow else "1"
    return (
        'arg="$*"\n'
        'case "$arg" in\n'
        '  *version_info.major*) echo "' + major + '" ;;\n'
        '  *version_info.minor*) echo "' + minor + '" ;;\n'
        '  *"import PIL"*) exit ' + pil_exit + " ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )


@pytest.fixture
def sandbox(tmp_path: Path):
    """Return a builder that runs the script with a controlled fake PATH.

    The builder accepts keyword flags for which stubs to install and returns the
    completed ``subprocess`` result (stdout captured, no real tools invoked).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def run(
        *,
        adb: bool = True,
        emulator: bool = True,
        avdmanager: bool = True,
        sdkmanager: bool = True,
        python_version: str | None = "3.12",
        pillow: bool = False,
        java: bool = True,
        devices_body: str = "emulator-5554          device transport_id:1",
        avds_body: str = "Pixel_7_API_34",
    ) -> subprocess.CompletedProcess:
        if adb:
            _write_stub(bin_dir, "adb", _adb_stub(devices_body))
        if emulator:
            _write_stub(
                bin_dir,
                "emulator",
                'if [ "$1" = "-version" ]; then echo "Android emulator version 34.1.0"; exit 0; fi\n'
                'if [ "$1" = "-list-avds" ]; then printf "%s\\n" "' + avds_body + '"; exit 0; fi\n'
                "exit 0\n",
            )
        if avdmanager:
            _write_stub(bin_dir, "avdmanager", "exit 0\n")
        if sdkmanager:
            _write_stub(bin_dir, "sdkmanager", 'echo "26.1.1"\nexit 0\n')
        if python_version is not None:
            _write_stub(bin_dir, "python3", _python_stub(python_version, pillow=pillow))
        if java:
            _write_stub(
                bin_dir,
                "java",
                'echo "openjdk version \\"17.0.15\\"" 1>&2\nexit 0\n',
            )

        env = dict(os.environ)
        # Prepend our stubs to a minimal system PATH. /usr/bin:/bin provides
        # bash and coreutils (sed/grep/wc/head/tr) but NOT adb/emulator/python3
        # (those live in the SDK / pyenv), so the only versions visible are the
        # stubs we installed above — the shell equivalent of monkeypatching.
        env["PATH"] = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])
        env["ANDROID_HOME"] = str(tmp_path / "sdk")
        (tmp_path / "sdk").mkdir(exist_ok=True)
        return subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    return run


def test_help_exits_zero():
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Environment Health Check" in result.stdout


def test_syntax_is_valid():
    # bash -n is the shell equivalent of a compile/lint gate.
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_all_present_passes_zero(sandbox):
    # Everything present and Pillow importable => a clean PASS with no warnings.
    result = sandbox(python_version="3.12", pillow=True)
    assert result.returncode == 0
    assert "PASS" in result.stdout
    assert "Failed:   0" in result.stdout
    assert "Warnings: 0" in result.stdout


def test_missing_adb_is_hard_failure(sandbox):
    result = sandbox(adb=False)
    assert result.returncode == 1
    assert "FAIL" in result.stdout
    assert "adb not found on PATH (required)" in result.stdout


def test_old_python_is_warning_not_failure(sandbox):
    # Version gate: 3.9 < 3.12 => WARN, but adb present => exit 0.
    result = sandbox(python_version="3.9")
    assert result.returncode == 0
    assert "Python 3.9 found" in result.stdout
    assert "WARN" in result.stdout


def test_new_python_passes_version_gate(sandbox):
    result = sandbox(python_version="3.13")
    assert "Python 3.13 (>= 3.12 required)" in result.stdout
    assert result.returncode == 0


def test_devices_are_listed_from_adb_output(sandbox):
    result = sandbox(
        devices_body=(
            "emulator-5554          device transport_id:1\n"
            "ABC123DEF              device transport_id:2"
        )
    )
    assert "2 device(s)/emulator(s) connected" in result.stdout
    assert "emulator-5554" in result.stdout
    assert "ABC123DEF" in result.stdout


def test_no_devices_warns_but_passes(sandbox):
    result = sandbox(devices_body="")
    assert "No devices or emulators connected" in result.stdout
    assert result.returncode == 0


def test_avds_listed_from_emulator(sandbox):
    result = sandbox(avds_body="Pixel_7_API_34")
    assert "1 AVD(s) defined" in result.stdout
    assert "Pixel_7_API_34" in result.stdout
