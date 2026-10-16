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

import json
import os
import shlex
import stat
import subprocess
import sys
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


def _adb_stub(
    devices_body: str,
    failed_command: str | None = None,
    failed_diagnostic: str | None = None,
    raw_invalid_device_diagnostic: bool = False,
) -> str:
    """adb stub: --version is silent, ``devices -l`` prints header + body.

    ``devices_body`` is emitted via a quoted heredoc so the device lines are
    printed verbatim (not executed as shell commands).
    """
    diagnostic = failed_diagnostic or f"{failed_command} diagnostic"
    version_result = (
        f"  printf '%s\\n' {shlex.quote(diagnostic)} >&2\n  exit 1\n"
        if failed_command == "adb --version"
        else "  exit 0\n"
    )
    devices_result = (
        (
            "  printf '\\377\\n' >&2\n  exit 1\n"
            if raw_invalid_device_diagnostic
            else f"  printf '%s\\n' {shlex.quote(diagnostic)} >&2\n  exit 1\n"
        )
        if failed_command == "adb devices -l"
        else (
            '  echo "List of devices attached"\n'
            "  cat <<'DEVICES'\n"
            f"{devices_body}\n"
            "DEVICES\n"
            "  exit 0\n"
        )
    )
    return (
        'if [ "$1" = "--version" ]; then\n'
        f"{version_result}"
        "fi\n"
        'if [ "$1" = "devices" ]; then\n'
        f"{devices_result}"
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
    real_python = shlex.quote(sys.executable)
    return (
        'arg="$*"\n'
        'case "$arg" in\n'
        '  *version_info.major*) echo "' + major + '" ;;\n'
        '  *version_info.minor*) echo "' + minor + '" ;;\n'
        '  *"import PIL"*) exit ' + pil_exit + " ;;\n"
        '  *"json.dumps"*) exec ' + real_python + ' "$@" ;;\n'
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
        *argv: str,
        adb: bool = True,
        emulator: bool = True,
        avdmanager: bool = True,
        sdkmanager: bool = True,
        python_version: str | None = "3.12",
        pillow: bool = False,
        java: bool = True,
        devices_body: str = "emulator-5554          device transport_id:1",
        avds_body: str = "Pixel_7_API_34",
        failed_command: str | None = None,
        failed_diagnostic: str | None = None,
        raw_invalid_device_diagnostic: bool = False,
        java_version_stdout: bool = False,
        timeout_log: Path | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        if adb:
            _write_stub(
                bin_dir,
                "adb",
                _adb_stub(
                    devices_body,
                    failed_command,
                    failed_diagnostic,
                    raw_invalid_device_diagnostic,
                ),
            )
        if emulator:
            emulator_version_result = (
                f'echo "{failed_command} diagnostic" >&2; exit 1'
                if failed_command == "emulator -version"
                else 'echo "Android emulator version 34.1.0"; exit 0'
            )
            emulator_list_result = (
                f'echo "{failed_command} diagnostic" >&2; exit 1'
                if failed_command == "emulator -list-avds"
                else f'printf "%s\\n" "{avds_body}"; exit 0'
            )
            _write_stub(
                bin_dir,
                "emulator",
                f'if [ "$1" = "-version" ]; then {emulator_version_result}; fi\n'
                f'if [ "$1" = "-list-avds" ]; then {emulator_list_result}; fi\n'
                "exit 0\n",
            )
        if avdmanager:
            _write_stub(bin_dir, "avdmanager", "exit 0\n")
        if sdkmanager:
            sdkmanager_body = (
                f'echo "{failed_command} diagnostic" >&2\nexit 1\n'
                if failed_command == "sdkmanager --version"
                else 'echo "26.1.1"\nexit 0\n'
            )
            _write_stub(bin_dir, "sdkmanager", sdkmanager_body)
        if python_version is not None:
            _write_stub(bin_dir, "python3", _python_stub(python_version, pillow=pillow))
        else:
            # Hide /usr/bin/python3 while retaining only the system utilities the
            # Bash script needs, so this exercises the actual no-Python fallback.
            for tool in ("grep", "mktemp", "perl", "sed", "tr", "wc"):
                (bin_dir / tool).symlink_to(Path("/usr/bin") / tool)
        if java:
            java_body = (
                f'echo "{failed_command} diagnostic" >&2\nexit 1\n'
                if failed_command == "java -version"
                else (
                    'echo "openjdk version \\"17.0.15\\""\nexit 0\n'
                    if java_version_stdout
                    else 'echo "openjdk version \\"17.0.15\\"" 1>&2\nexit 0\n'
                )
            )
            _write_stub(
                bin_dir,
                "java",
                java_body,
            )
        if timeout_log is not None:
            _write_stub(
                bin_dir,
                "timeout",
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(timeout_log))}\n" 'shift\nexec "$@"\n',
            )

        env = dict(os.environ)
        # Prepend our stubs to a minimal system PATH. /usr/bin:/bin provides
        # bash and coreutils (sed/grep/wc/head/tr) but NOT adb/emulator/python3
        # (those live in the SDK / pyenv), so the only versions visible are the
        # stubs we installed above — the shell equivalent of monkeypatching.
        system_paths = ["/bin"] if python_version is None else ["/usr/bin", "/bin"]
        env["PATH"] = os.pathsep.join([str(bin_dir), *system_paths])
        env["ANDROID_HOME"] = str(tmp_path / "sdk")
        (tmp_path / "sdk").mkdir(exist_ok=True)
        return subprocess.run(
            ["bash", str(SCRIPT), *argv],
            capture_output=True,
            text=text,
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


def test_json_success_is_one_object_with_every_check(sandbox):
    result = sandbox("--json", python_version="3.12", pillow=True)

    assert result.returncode == 0
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["success"] is True
    assert len(payload["checks"]) == 10
    assert payload["summary"] == {"passed": 10, "warnings": 0, "failed": 0}
    assert "Environment Health Check" in result.stderr


@pytest.mark.parametrize(
    ("failed_command", "check_name"),
    [
        pytest.param("adb --version", "Checking adb (Android Debug Bridge)...", id="adb-version"),
        pytest.param("emulator -version", "Checking emulator...", id="emulator-version"),
        pytest.param(
            "sdkmanager --version",
            "Checking avdmanager and sdkmanager...",
            id="sdkmanager-version",
        ),
        pytest.param("java -version", "Checking java...", id="java-version"),
        pytest.param(
            "emulator -list-avds",
            "Checking devices and AVDs...",
            id="emulator-list-avds",
        ),
    ],
)
def test_failed_tool_probe_fails_health_check(sandbox, failed_command, check_name):
    result = sandbox("--json", python_version="3.12", pillow=True, failed_command=failed_command)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    failed_check = next(
        check
        for check in payload["checks"]
        if check["name"] == check_name and failed_command in check["message"]
    )
    assert failed_check["status"] == "failed"


def test_json_preserves_utf8_diagnostic(sandbox):
    result = sandbox(
        "--json",
        python_version="3.12",
        pillow=True,
        failed_command="adb devices -l",
        failed_diagnostic="café 😀",
    )

    payload = json.loads(result.stdout)
    assert payload["error"].endswith("café 😀")


def test_json_replaces_invalid_utf8_diagnostic(sandbox):
    result = sandbox(
        "--json",
        python_version="3.12",
        pillow=True,
        failed_command="adb devices -l",
        raw_invalid_device_diagnostic=True,
        text=False,
    )

    payload = json.loads(result.stdout.decode("utf-8"))
    assert payload["error"].endswith("\ufffd")


def test_json_without_python3_uses_parseable_fallback(sandbox):
    result = sandbox("--json", python_version=None, pillow=False)

    assert result.returncode == 0
    assert isinstance(json.loads(result.stdout), dict)


def test_java_version_uses_first_combined_output_line(sandbox):
    result = sandbox(java_version_stdout=True)

    assert 'java installed (openjdk version "17.0.15")' in result.stdout


def test_external_probes_are_bounded(sandbox, tmp_path):
    timeout_log = tmp_path / "timeout.log"

    result = sandbox(python_version="3.12", pillow=True, timeout_log=timeout_log)

    assert result.returncode == 0
    assert timeout_log.read_text().splitlines() == [
        "10 adb --version",
        "10 emulator -version",
        "10 sdkmanager --version",
        "10 java -version",
        "10 adb devices -l",
        "10 emulator -list-avds",
    ]


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
