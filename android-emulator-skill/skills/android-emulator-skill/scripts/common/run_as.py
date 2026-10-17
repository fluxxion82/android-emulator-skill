"""Reading a debuggable app's files with ``adb shell -T run-as`` -- and nothing else.

Why this exists
---------------
``adb exec-out run-as <pkg> cat <file>`` exits 0 whatever the device command
did, and delivers the device's stderr on STDOUT. A missing file comes back as
the bytes ``cat: <file>: No such file or directory``, and a run-as refusal as
its own message -- each indistinguishable, to a caller deciding by exit status,
from a file that holds those words (recorded: ``run_as_cat_missing_exec_out``,
exit 0, stderr empty). ``container.py`` read files that way until PR #22, and
``model_inspector.py`` pulled databases that way after it: a missing database
was written to a temp file and reported as "not a valid SQLite database".

``adb shell -T`` (no pty) returns the device command's exit status and its
stderr apart, and keeps stdout's bytes as exec-out delivers them -- fixture.db,
32768 bytes, cmp-identical over both (recorded: ``run_as_cat_missing``, exit 1,
stdout empty, the reason on stderr). Two scripts read app files now, so the
transport and its guard live here once.

The client guard
----------------
adb has understood ``shell -T`` since 1.0.36 (platform-tools 24). An older
CLIENT does not refuse it: it forwards ``-T`` to the device shell as command
text and exits 0 after printing the shell's error, and a read deciding by exit
status would take that error for the file. So the client's version is checked
once per process, before the first read (recorded: ``adb_version``).
"""

from __future__ import annotations

import functools
import re

from . import adb_exec, cli
from .device_utils import build_adb_command, quote_for_device_shell

MIN_ADB_FOR_READS = (1, 0, 36)
_ADB_VERSION_LINE = re.compile(r"^Android Debug Bridge version (\d+)\.(\d+)\.(\d+)\s*$", re.M)
ADB_UPDATE_REMEDY = (
    'Update Android SDK platform-tools (sdkmanager "platform-tools") so `adb version` '
    "reports 1.0.36 or later, and make sure that adb is the one on PATH."
)


def parse_adb_version(banner: str) -> tuple[int, int, int] | None:
    """The X.Y.Z of an ``adb version`` banner's first line, or None when it has none.

    Recorded: adb_version -- ``Android Debug Bridge version 1.0.41``.
    """
    match = _ADB_VERSION_LINE.search(banner)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


@functools.cache
def adb_client_version() -> tuple[int, int, int]:
    """The host adb client's version, asked once per process (``adb version``, bounded).

    Raises:
        cli.Failure: the banner names no version. Not assumed fine: a client
            whose version cannot be read cannot be shown to honour ``-T``.
        AdbError: adb is missing, timed out, or ``adb version`` failed.
    """
    banner = adb_exec.run_adb("version", None, check=True).stdout
    version = parse_adb_version(banner)
    if version is None:
        first = banner.strip().splitlines()[0] if banner.strip() else "(nothing)"
        cli.fail(
            f"cannot tell whether this adb can read app files safely: `adb version` "
            f"answered {first!r}, with no 'Android Debug Bridge version X.Y.Z' line",
            ADB_UPDATE_REMEDY,
        )
    return version


def require_adb_for_reads(version: tuple[int, int, int]) -> None:
    """Refuse, before any file is read, an adb client too old to honour ``shell -T``.

    The version is handed in rather than asked for here, so each script asks
    through its own ``adb_client_version`` name -- the one its tests replace.

    Raises:
        cli.Failure: ``version`` is older than ``MIN_ADB_FOR_READS``.
    """
    if version < MIN_ADB_FOR_READS:
        shown = ".".join(map(str, version))
        cli.fail(
            f"adb {shown} is older than 1.0.36 and cannot read app files safely",
            ADB_UPDATE_REMEDY,
        )


def read_command(serial: str | None, package: str, argv: list[str]) -> list[str]:
    """The argv of ``adb shell -T run-as <package> <argv...>``, quoted for the device shell.

    Run it with ``adb_exec.run_argv`` (bytes): its status and stderr are the
    read's verdict, and adb's own failures arrive typed.
    """
    quoted = [quote_for_device_shell(arg) for arg in argv]
    return build_adb_command(
        "shell", serial, "-T", "run-as", quote_for_device_shell(package), *quoted
    )
