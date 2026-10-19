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


def _python_stub(
    version: str,
    pillow: bool = False,
    encoder_preamble: str = "",
    encoder_preamble_newline: bool = True,
) -> str:
    """Python stub honoring the queries the script makes.

    ``version`` is "MAJOR.MINOR". ``import PIL`` succeeds only when ``pillow`` is
    True, so callers control whether the Pillow check passes or warns without
    needing a real Pillow install. ``"broken"`` is an interpreter that resolves
    on PATH and cannot run: every invocation exits 42, printing nothing (the
    final v0.8.0 review's reproduction). ``encoder_preamble`` is a line the
    interpreter prints before running the encoder, as a startup diagnostic would.
    """
    if version == "broken":
        return "exit 42\n"
    major, minor = version.split(".")
    pil_exit = "0" if pillow else "1"
    real_python = shlex.quote(sys.executable)
    preamble_format = "%s\\n" if encoder_preamble_newline else "%s"
    return (
        'arg="$*"\n'
        'case "$arg" in\n'
        '  *version_info.major*) echo "' + major + '" ;;\n'
        '  *version_info.minor*) echo "' + minor + '" ;;\n'
        '  *"import PIL"*) exit ' + pil_exit + " ;;\n"
        '  *"json.dumps"*) '
        + (
            f"printf {shlex.quote(preamble_format)} {shlex.quote(encoder_preamble)}; "
            if encoder_preamble
            else ""
        )
        + "exec "
        + real_python
        + ' "$@" ;;\n'
        '  *"json.loads"*) exec ' + real_python + ' "$@" ;;\n'
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
        python_command: str = "python3",
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
        tmpdir: Path | None = None,
        before_list_avds: str = "",
        encoder_preamble: str = "",
        encoder_preamble_newline: bool = True,
        python3_shadow_log: Path | None = None,
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
                f'if [ "$1" = "-list-avds" ]; then {before_list_avds} {emulator_list_result}; fi\n'
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
            _write_stub(
                bin_dir,
                python_command,
                _python_stub(
                    python_version,
                    pillow=pillow,
                    encoder_preamble=encoder_preamble,
                    encoder_preamble_newline=encoder_preamble_newline,
                ),
            )
        if python_version is None or python_command != "python3":
            shadow_body = "exit 127\n"
            if python3_shadow_log is not None:
                shadow_body = (
                    f"printf '%s\\n' called >> {shlex.quote(str(python3_shadow_log))}\n"
                    + shadow_body
                )
            _write_stub(bin_dir, "python3", shadow_body)
            # Keep the system utilities the Bash script needs available. The
            # python3 shadow above prevents either /usr/bin or /bin from leaking
            # a host interpreter into the sandbox.
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
            # One record per call, ended by RS (octal 036): the JSON encoder's argv
            # is a whole Python program, newlines and all.
            _write_stub(
                bin_dir,
                "timeout",
                f"printf '%s\\036' \"$*\" >> {shlex.quote(str(timeout_log))}\n"
                'shift\nexec "$@"\n',
            )

        env = dict(os.environ)
        # Prepend our stubs to a minimal system PATH. The only tool versions
        # visible are the stubs installed above — the shell equivalent of
        # monkeypatching.
        system_paths = ["/usr/bin", "/bin"]
        env["PATH"] = os.pathsep.join([str(bin_dir), *system_paths])
        if python_command != "python3":
            # This scenario exercises selection of the legacy ``python`` name.
            # Treat the python3 sentinel as unavailable to ``command -v``; if
            # the script invokes python3 directly anyway, the sentinel records
            # that isolation failure and exits 127.
            bash_env = tmp_path / "bash_env"
            bash_env.write_text(
                "command() {\n"
                '  if [ "$#" -eq 2 ] && [ "$1" = -v ] && [ "$2" = python3 ]; then\n'
                "    return 1\n"
                "  fi\n"
                '  builtin command "$@"\n'
                "}\n"
            )
            env["BASH_ENV"] = str(bash_env)
        env["ANDROID_HOME"] = str(tmp_path / "sdk")
        (tmp_path / "sdk").mkdir(exist_ok=True)
        if tmpdir is not None:
            env["TMPDIR"] = str(tmpdir)
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


def test_json_guard_uses_detected_python_interpreter(sandbox, tmp_path):
    """A runnable ``python`` must validate the Bash report when python3 is absent."""
    timeout_log = tmp_path / "timeout.log"
    python3_shadow_log = tmp_path / "python3-shadow.log"

    result = sandbox(
        "--json",
        python_version="3.12",
        python_command="python",
        pillow=True,
        timeout_log=timeout_log,
        python3_shadow_log=python3_shadow_log,
    )

    assert result.returncode == 0, result.stderr[-800:]
    payload = _one_object(result)
    assert payload["success"] is True and len(payload["checks"]) == 10, payload
    calls = _bounded_calls(timeout_log)
    validators = [
        call for call in calls if call.startswith("10 python -c ") and "json.loads" in call
    ]
    assert len(validators) == 1, calls
    assert not python3_shadow_log.exists(), "the script tried to invoke the shadowed python3"


def test_java_version_uses_first_combined_output_line(sandbox):
    result = sandbox(java_version_stdout=True)

    assert 'java installed (openjdk version "17.0.15")' in result.stdout


def _bounded_calls(timeout_log: Path) -> list[str]:
    """Each call the `timeout` stub saw, as `<seconds> <argv...>`."""
    return timeout_log.read_text().split("\036")[:-1]


def test_external_probes_are_bounded(sandbox, tmp_path):
    """Every probe runs under the bound, the Python ones too (final v0.8.0 review)."""
    timeout_log = tmp_path / "timeout.log"

    result = sandbox(python_version="3.12", pillow=True, timeout_log=timeout_log)

    assert result.returncode == 0
    assert _bounded_calls(timeout_log) == [
        "10 adb --version",
        "10 emulator -version",
        "10 sdkmanager --version",
        "10 java -version",
        "10 python3 -c import sys; print(sys.version_info.major)",
        "10 python3 -c import sys; print(sys.version_info.minor)",
        "10 python3 -c import PIL",
        "10 adb devices -l",
        "10 emulator -list-avds",
    ]


def test_the_json_encoder_is_bounded(sandbox, tmp_path):
    """A python3 that hangs while encoding must not hang the health check."""
    timeout_log = tmp_path / "timeout.log"

    result = sandbox("--json", python_version="3.12", pillow=True, timeout_log=timeout_log)

    assert result.returncode == 0, result.stderr[-800:]
    encoders = [
        call
        for call in _bounded_calls(timeout_log)
        if call.startswith("10 python3 -c ") and "json.dumps" in call
    ]
    assert len(encoders) == 1, _bounded_calls(timeout_log)
    assert json.loads(result.stdout)["success"] is True


# ---------------------------------------------------------------------------
# C5 (final v0.8.0 review): --json must always end in one JSON object. The
# emitter ran python3 without checking it, so a python3 that resolves on PATH
# and cannot run left stdout EMPTY at exit 0; and a check record that could not
# be written was never noticed.
# ---------------------------------------------------------------------------


def _one_object(result: subprocess.CompletedProcess) -> dict:
    """The run's stdout as the single JSON object --json promises."""
    shown = f"\nstdout: {result.stdout[-400:]!r}\nstderr: {result.stderr[-800:]!r}"
    assert len(result.stdout.splitlines()) == 1, f"not one line of JSON on stdout{shown}"
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict), payload
    return payload


def test_a_python3_that_cannot_run_still_gets_a_json_report(sandbox):
    """python3 exits 42 on every call; the Android probes succeed.

    Was: exit 0 and an empty stdout, and the Python check read "Python 0.0
    found". Now the Bash encoder answers, and the check names the failure.
    """
    result = sandbox("--json", python_version="broken")

    assert result.returncode == 0, result.stderr[-800:]
    payload = _one_object(result)
    assert payload["success"] is True and len(payload["checks"]) == 10, payload
    (python,) = [c for c in payload["checks"] if c["name"] == "Checking Python 3.12+..."]
    assert python["status"] == "warning", python
    assert "could not run" in python["message"] and "exited 42" in python["message"], python
    (pillow,) = [c for c in payload["checks"] if c["name"] == "Checking Python Pillow (PIL)..."]
    assert pillow["status"] == "warning" and "could not run" in pillow["message"], pillow
    assert "Python 0.0" not in result.stdout + result.stderr


def test_a_check_record_that_cannot_be_written_is_one_structured_error(sandbox, tmp_path):
    """The record directory is gone before the last check is recorded.

    The `emulator -list-avds` stub removes it -- TMPDIR is this test's own
    directory, so the glob reaches nothing else. A report missing a check
    would claim a completeness it does not have: one JSON error line, exit 1.
    """
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    result = sandbox(
        "--json",
        pillow=True,
        tmpdir=tmpdir,
        before_list_avds='rm -rf "$TMPDIR"/android-health-check-json.*;',
    )

    assert result.returncode == 1, result.stderr[-800:]
    payload = _one_object(result)
    assert payload["success"] is False and payload["remedy"], payload
    assert "could not write the record of check 10" in payload["error"], payload
    assert list(tmpdir.iterdir()) == []


def test_records_neither_encoder_can_read_are_one_structured_error(sandbox, tmp_path):
    """python3 cannot run, and the Bash encoder finds the records gone: exit 1, one line.

    The stub deletes the records written so far, keeping their directory, so
    every write succeeds and only the reads fail.
    """
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    result = sandbox(
        "--json",
        python_version="broken",
        tmpdir=tmpdir,
        before_list_avds='rm -f "$TMPDIR"/android-health-check-json.*/check-*;',
    )

    assert result.returncode == 1, result.stderr[-800:]
    payload = _one_object(result)
    assert payload["success"] is False and payload["remedy"], payload
    assert "could not emit" in payload["error"], payload


def test_no_directory_for_the_records_is_one_structured_error(sandbox, tmp_path):
    """TMPDIR names a directory that does not exist: refused at once, one JSON line."""
    missing = tmp_path / "missing"

    result = sandbox("--json", pillow=True, tmpdir=missing)

    assert result.returncode == 1, result.stderr[-800:]
    payload = _one_object(result)
    assert payload["success"] is False and "TMPDIR" in payload["remedy"], payload
    assert "could not create a directory for the check records" in payload["error"], payload
    assert str(missing) in payload["error"], payload


def test_encoder_output_that_is_not_one_object_is_not_the_report(sandbox):
    """A line printed ahead of the report made stdout two documents (review of #33).

    The check was braces only: `{...}` + newline + `{...}` starts with one and
    ends with the other, so it passed, and --json printed two lines at exit 0.
    Now the encoder's output must be exactly one line, and the Bash encoder
    answers instead.
    """
    result = sandbox("--json", pillow=True, encoder_preamble='{"startup": "diagnostic"}')

    assert result.returncode == 0, result.stderr[-800:]
    payload = _one_object(result)
    assert payload["success"] is True and len(payload["checks"]) == 10, payload
    assert "the Bash encoder did" in result.stderr


def test_concatenated_python_documents_are_not_accepted_as_one_report(sandbox):
    """A startup hook can print an object without a newline before the real report."""
    result = sandbox(
        "--json",
        pillow=True,
        encoder_preamble="{}",
        encoder_preamble_newline=False,
    )

    assert result.returncode == 0, result.stderr[-800:]
    payload, end = json.JSONDecoder().raw_decode(result.stdout)
    trailing = result.stdout[end:].strip()
    assert not trailing, f"stdout has a second JSON document: {result.stdout[end:]!r}"
    assert payload["success"] is True and len(payload["checks"]) == 10, payload
    assert "the Bash encoder did" in result.stderr


def _shell_function(name: str) -> str:
    """Extract one top-level Bash function so its return status can be exercised."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"{name}() {{")
    end = next(index for index in range(start + 1, len(lines)) if lines[index] == "}")
    return "\n".join(lines[start : end + 1])


def test_python_document_guard_requires_the_report_keys():
    """A JSON object is not enough: it must have the health report's required fields."""
    functions = f'{_shell_function("is_json_object")}\n{_shell_function("run_bounded")}'
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'{functions}\nPYTHON_RUNS=true\nis_json_object "$1"',
            "json-guard",
            "{}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, "the Python guard accepted an object without report keys"


def test_bash_document_guard_rejects_concatenated_objects():
    """The no-Python path still rejects a second top-level object."""
    function = _shell_function("is_json_object")

    result = subprocess.run(
        [
            "bash",
            "-c",
            f'{function}\nPYTHON_RUNS=false\nis_json_object "$1"',
            "json-guard",
            "{}{}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, "the Bash fallback accepted two top-level objects"


def test_a_probe_with_no_temporary_file_says_it_did_not_run(sandbox, tmp_path):
    """The same shape in the probes: their output files are mktemp'd too.

    The probe used to run against an empty path, and its failure read "exited
    1 with no error output" -- the tool's, though the tool never ran.
    """
    result = sandbox(pillow=True, tmpdir=tmp_path / "missing")

    assert result.returncode == 1, result.stdout[-800:]
    assert "adb --version failed: did not run, no temporary file" in result.stdout, result.stdout
    assert "could not list devices (adb devices -l): did not run" in result.stdout
    assert "with no error output" not in result.stdout


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
