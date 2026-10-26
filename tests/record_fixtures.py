#!/usr/bin/env python3
"""Record verbatim tool output into tests/fixtures/recorded/.

Why this exists
---------------
Both the skill's code and its tests were originally written against *imagined*
`adb` / `gradle` / `avdmanager` output. When the imagination is wrong, the code
and the test are wrong in the same direction, so the suite stays green while the
script does nothing. Recording real output is the only way out of that.

The rule this tool enforces: **parser tests consume files under
``tests/fixtures/recorded/``; they never inline tool output as string literals.**

Fixtures are written with no header or annotation, so a parser under test sees
what it would see from the real tool. Provenance lives alongside in
``MANIFEST.json``.

Five documented departures from verbatim, all visible in the code below:

1. CRLF is normalised to LF (``_strip_cr``). In practice the text-mode decode
   in ``_run`` / ``_run_host`` already does it, and does more: Python's
   universal-newline translation turns a *bare* CR into LF as well, so
   avdmanager's progress bar, which repaints itself with bare CRs, arrives as
   separate lines. The scripts read these tools in text mode too, so they see
   the same translation. The cost worth knowing: the emulator console really
   does answer ``Pixel_9\r\nOK\r\n``, and that CR is what caused defect S5, so
   no fixture can reproduce S5. ``tests/test_emu_console.py`` covers the CRLF
   framing with constructed input and records the measured bytes in its
   docstring.
2. Non-AOSP package names are aliased (``_redact_private_packages``).
3. The recording machine's home directory is replaced by ``/Users/<user>``
   (``_scrub_home_paths``) wherever an SDK tool's output or a file it wrote
   might carry it -- the avdmanager and sdkmanager fixtures and the AVD's
   ``config.ini``. Fixtures are public; an account name is not theirs to
   publish. The placeholder keeps the path absolute, so a parser that splits
   or joins it exercises the same code it would on the real one.
4. ``logcat_app_restart`` and ``logcat_app_restart_usec`` keep only some LINES
   of their window
   (``_app_restart_window``): those that name the fixture package or carry one
   of the pids ``pidof`` reported for it, plus the reader's ``beginning of``
   separators. A full window carries every other app's free text, which is not
   ours to publish. Each kept line is verbatim; what is absent is whole lines.
5. In the ``adb devices`` fixtures, every serial that is not ``emulator-NNNN``
   -- a phone attached to the recording host -- is replaced by
   ``SERIALREDACTED`` (``_redact_serials``). Its row and state are kept.

Three fixtures are written through ``capture_bytes`` and so skip (1) and (2)
entirely; each carries its own check instead.

- The status-bar PNGs and ``am_profile_hang.trace`` are binary: newline
  translation or aliasing would corrupt them -- the trace's record stream is
  little-endian integers, and a 0x0d byte in a clock value is not a line
  ending. The PNGs are cropped to the status bar; the trace is refused unless
  every declaring class, every class in a method signature and every thread
  name is on an allowlist (``_check_hang_trace``).
- ``avd_config_ini`` is text, but its keys are dotted lowercase names
  (``hw.camera.back``, ``image.sysdir.1``) that the aliaser's pattern takes for
  package names, so aliasing would rewrite the very keys a parser looks up.
  avdmanager writes it from the system image and the device definition, so it
  carries nothing of the user's; it still gets (3), and the private AVD home it
  was written in is replaced by ``<avd home>`` (``_ThrowawayAvd``).

Device safety: a fixture marked ``emulator_only`` changes the device it runs on
(installs state, launches, taps, grants, writes files). ``record`` refuses the
whole run before any adb call unless the target serial -- ``--serial``, or
$ANDROID_SERIAL when it is omitted -- starts with ``emulator-``, the same rule
tests/conftest.py's ``emulator_only_device`` applies to live tests. That serial
is resolved once and every adb call names it with ``-s`` (``_adb_argv``); with
neither ``--serial`` nor $ANDROID_SERIAL the run is refused, so adb never picks.

Usage
-----
    python tests/record_fixtures.py --list
    python tests/record_fixtures.py --serial emulator-5554       # record everything
    python tests/record_fixtures.py --serial emulator-5554 --only logcat_threadtime

Requires a booted device or emulator. Re-run after an Android version bump and
commit the diff: a fixture that changes shape is exactly the signal you want.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

RECORDED_ROOT = Path(__file__).resolve().parent / "fixtures" / "recorded"

# Fixtures live under recorded/<profile>/ so the same command recorded on a
# different API level is a new file, not a silent overwrite. Diffing two
# profiles is how you find a format that shifted between releases.
DEFAULT_PROFILE = "emulator-api35"

# Every adb call is bounded. An unbounded `adb shell dumpsys window` will wedge
# the adb connection, which is the same defect class this repo ships (R1).
DEFAULT_TIMEOUT = 30

# Captured out-of-band: needs `adb exec-out`, not the plain call the rest use.
HIERARCHY_FIXTURE = "uiautomator_current_screen"
HIERARCHY_COMMAND = "adb shell uiautomator dump /dev/tty"
HIERARCHY_DESCRIPTION = (
    "A real uiautomator hierarchy. Note bounds are in physical "
    "PIXELS, which is what makes accessibility_audit's comparison "
    "against a 48 'dp' constant wrong."
)

# Captured out-of-band too, on the HOST: one selector, one file per JDK major
# installed here (`java_version_<major>.txt`), so how many files it writes
# depends on the machine rather than on a FIXTURES entry. The device is not
# touched; `record` still reads its getprop for the profile's provenance.
JAVA_VERSION_FIXTURE = "java_version"


@dataclass(frozen=True)
class Capture:
    """One command's full result: both streams and the exit status.

    The recorder used to keep only ``stdout or stderr``. That sufficed while
    every fixture's evidence was its text, but two of them carry their meaning
    in the exit status instead: ``dumpsys activity anr`` prints
    "Unknown command: anr" and still **exits 0**, and ``pidof`` for a package
    that is not running prints nothing at all and **exits 1**. Collapsing a
    command to its text throws that fact away — and in the ANR case the
    discarded fact is precisely the one that let a nonexistent subcommand look
    like a working data source for three releases.

    The exit status is recorded in MANIFEST.json rather than in the fixture
    file, so the file stays byte-for-byte what the tool emitted and a parser
    under test still sees exactly what it would see from the real tool.
    """

    stdout: str
    stderr: str
    returncode: int

    @property
    def text(self) -> str:
        """The stream a caller reads: stdout, or stderr when stdout is empty.

        Mirrors what the skill's own ``adb_exec`` does. Some tools (am
        broadcast, avdmanager, and ``input text`` when it throws) put the
        interesting part on stderr.
        """
        return self.stdout if self.stdout.strip() else self.stderr

    @property
    def stream(self) -> str:
        """Which stream the recorded file holds, for the manifest.

        "empty" is a distinct answer from "stderr": ``pidof`` for a package
        that is not running writes to neither stream, and recording that as
        "stderr" would imply a diagnostic that was never printed.
        """
        if self.stdout.strip():
            return "stdout"
        return "stderr" if self.stderr.strip() else "empty"


@dataclass(frozen=True)
class Fixture:
    """One recorded artifact.

    Attributes:
        name: Basename written under ``recorded/`` (extension appended).
        args: Arguments after ``adb [-s SERIAL]``.
        description: What a parser test uses this for.
        ext: File extension; ``xml`` for hierarchy dumps, ``png`` for the
            screen captures, else ``txt``.
        catches: Defect IDs this fixture would have caught. Empty for context.
        setup: Optional adb argument lists run before capture.
        teardown: Optional adb argument lists run after capture, always.
        timeout: Per-fixture bound on the adb call.
        post: Optional transform applied to the captured text before it is
            written. It may raise CaptureError to reject a bad capture.
        prepare: Optional callable run before capture, given (scratch_dir,
            serial). For binary payloads: adb's text mode would corrupt them,
            and a device-side ``>`` redirect runs as the wrong uid under
            ``run-as`` (measured: it yields a 0-byte file).
        host_argv: Optional HOST command to capture instead of an adb call, as
            a format string list interpolated with ``{tmp}`` (a scratch
            directory). Needed for the one case where the text a parser
            consumes is produced on the host rather than the device: `sqlite3`
            was removed from user builds, so `model_inspector`'s real path
            pulls the database and runs the host's sqlite3 against it. Use
            ``setup`` to pull the file first.
        capture_bytes: Optional callable returning the fixture's bytes, given
            the serial and a metadata dict it may add provenance to. For
            artifacts that are not text at all — the status-bar PNGs — where
            adb's ``text=True`` decoding would corrupt the payload. Mutually
            exclusive with ``args`` / ``host_argv``.
        record_both_streams: Persist the SIZE of both streams in MANIFEST.json,
            not just the one the file holds. The file can only ever carry one
            stream, so "stdout said this" and "stderr said nothing" are two
            different claims and only the first is provable from the file.
            ``dumpsys activity anr`` is the case that matters: the T5 argument
            is that it printed usage text on stdout, printed NOTHING on stderr,
            and still exited 0. Without the stderr size that middle claim rests
            on nobody having looked.
        teardown_verify: Optional callable given the serial, run after teardown
            and required not to raise. For fixtures whose setup CHANGES THE
            DEVICE: a teardown that silently failed leaves a real phone in the
            altered state, and nothing else in this file would notice.
        metadata: Optional callable given the serial, returning extra manifest
            fields. For provenance that has to be measured at capture time
            rather than written from memory.
        capture_with: Optional callable given (scratch_dir, serial, extra)
            and returning the :class:`Capture` to record, for a fixture whose
            evidence takes more than one command to produce. Two shapes need
            it: the `am profile` refusals, where the state the refusal LEAVES
            (a 0-byte file on /sdcard) has to be read before teardown deletes
            it, and the avdmanager lifecycle, where create, config.ini and
            delete must happen in that order in one run whatever ``--only``
            selected. The Capture still goes through the text pipeline, so the
            manifest's exit status and stream are the returned command's.
        command: The manifest's ``command`` string, when the capture is not
            one adb or host argv that could be shown instead.
        emulator_only: The fixture changes the device -- launches, taps,
            grants, broadcasts, overrides, files written -- so it is recorded
            only from a target whose serial starts with ``emulator-``. A
            physical phone is somebody's, and `adb devices` lists one before
            the emulator. Checked in :func:`record` before any adb call.
    """

    name: str
    args: list[str]
    description: str
    ext: str = "txt"
    catches: tuple[str, ...] = ()
    setup: list[list[str]] = field(default_factory=list)
    teardown: list[list[str]] = field(default_factory=list)
    timeout: int = DEFAULT_TIMEOUT
    post: Callable[[str], str] | None = None
    prepare: Callable[[Path, str], None] | None = None
    host_argv: list[str] | None = None
    capture_bytes: Callable[[str, dict], bytes] | None = None
    record_both_streams: bool = False
    teardown_verify: Callable[[str], None] | None = None
    metadata: Callable[[str], dict] | None = None
    capture_with: Callable[[Path, str, dict], Capture] | None = None
    command: str | None = None
    emulator_only: bool = False


class CaptureError(RuntimeError):
    """Raised when a fixture could not be captured.

    Deliberately fatal for that fixture: a half-captured or timed-out artifact
    must never reach disk. Writing a placeholder would recreate precisely the
    defect this whole exercise exists to eliminate — a file that looks like
    ground truth and is not.
    """


def _adb_argv(serial: str, args: list[str]) -> list[str]:
    """``adb -s <serial> <args>``: the ONLY way this file addresses a device.

    The serial is required, never defaulted. An argv without ``-s`` lets adb
    pick for itself -- $ANDROID_SERIAL, or the only device attached -- and a
    device-writing step must never be aimed by anything but the serial
    :func:`record` resolved and checked. Host-scope adb (``version``,
    ``devices``) goes through :func:`_run_host_adb` instead.

    Raises:
        ValueError: ``serial`` is empty or not a string -- a programming error,
            deliberately not a CaptureError a run would note and carry on past.
    """
    if not isinstance(serial, str) or not serial:
        raise ValueError(f"an adb device command needs a serial; got {serial!r} for {args}")
    return ["adb", "-s", serial, *args]


# The adb subcommands that address no device: the host's own client and
# server. Anything else goes through _adb_argv.
_HOST_ADB_SUBCOMMANDS = frozenset({"version", "devices"})


def _run_host_adb(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> Capture:
    """A host-scope adb command (``devices``, ``version``), with no ``-s``.

    Raises:
        ValueError: ``args`` is not one of :data:`_HOST_ADB_SUBCOMMANDS`.
        CaptureError: on timeout.
    """
    if not args or args[0] not in _HOST_ADB_SUBCOMMANDS:
        raise ValueError(f"not a host-scope adb command: {args}")
    try:
        result = subprocess.run(
            ["adb", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"timed out after {timeout}s") from exc
    return Capture(result.stdout, result.stderr, result.returncode)


def _run(args: list[str], serial: str, timeout: int = DEFAULT_TIMEOUT) -> Capture:
    """Run one adb command and return both streams plus its exit status.

    Raises:
        CaptureError: on timeout, or when the device reports a dump timeout.

    A non-zero exit is *not* an error here: "what this prints when it goes
    wrong" is itself worth recording (see am_broadcast_missing_receiver, whose
    entire point is that a doomed broadcast still exits 0).
    """
    cmd = _adb_argv(serial, args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"timed out after {timeout}s") from exc

    capture = Capture(result.stdout, result.stderr, result.returncode)

    # dumpsys reports its own internal timeout in-band and still exits 0.
    if "DUMP TIMEOUT" in capture.text:
        raise CaptureError("device-side dumpsys timeout (device is loaded or unhealthy)")
    return capture


def _run_bytes(
    args: list[str], serial: str, timeout: int = DEFAULT_TIMEOUT, *, allow_empty: bool = False
) -> bytes:
    """Run one adb command and return its stdout as raw bytes.

    ``_run`` decodes with ``text=True``, which mangles a PNG beyond repair.
    Used by the screencap fixtures and the binary logcat reads. ``allow_empty``
    accepts no output at exit 0 -- a logcat window nothing was logged in.
    """
    cmd = _adb_argv(serial, args)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"timed out after {timeout}s") from exc
    if result.returncode != 0 or not (result.stdout or allow_empty):
        raise CaptureError(
            f"{' '.join(args)} produced no bytes "
            f"(exit {result.returncode}): {result.stderr[:200]!r}"
        )
    return result.stdout


def _sdk_tool(name: str) -> str:
    """Absolute path to an Android cmdline-tool.

    They are not on PATH by default -- and until `cmdline-tools` was installed
    they could not run at all here, which is why `avdmanager` output has no
    fixture despite seven scripts parsing it. The legacy `tools/bin` copies are
    not a fallback: they use JAXB, removed in Java 11.
    """
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not home:
        home = str(Path.home() / "Library" / "Android" / "sdk")
    tool = Path(home) / "cmdline-tools" / "latest" / "bin" / name
    if not tool.exists():
        raise CaptureError(
            f"{name} not found at {tool}. Install the SDK command-line tools "
            f"(Android Studio > SDK Manager > SDK Tools > Android SDK "
            f"Command-line Tools), or set ANDROID_HOME."
        )
    return str(tool)


def _emulator_tool() -> str:
    """Absolute path to the `emulator` binary.

    Not `_sdk_tool`: emulator lives at ``<sdk>/emulator/emulator``, not under
    ``cmdline-tools/latest/bin``. Resolution is delegated to the skill's own
    ``common.sdk_tools`` so the recorder and the scripts cannot disagree about
    which binary "emulator" means -- the disagreement that produced the
    PermissionError this fixture exists to make testable.
    """
    _make_scripts_importable()
    from common.sdk_tools import EMULATOR_NOT_FOUND_MESSAGE, get_emulator_path

    resolved = get_emulator_path()
    if not resolved:
        raise CaptureError(EMULATOR_NOT_FOUND_MESSAGE)
    return resolved


# The Java class every `sdkmanager` ran until cmdline-tools 23, and the one the
# GitHub runner image's cmdline-tools still run.
_JAVA_SDKMANAGER_MAIN = "com.android.sdklib.tool.sdkmanager.SdkManagerCli"


def _java_sdkmanager() -> list[str]:
    """argv prefix that runs the Java ``SdkManagerCli`` directly.

    cmdline-tools 23's ``bin/sdkmanager`` is a shell wrapper that hands the
    command to the Android CLI, which prints slash paths in whitespace columns
    (sdkmanager_list*). The Java tool prints a pipe table keyed by the
    semicolon install id -- a different format, and the one CI and most
    installs see. 23 still ships the class, in ``lib/sdklib/tools.sdklib.jar``;
    it is run here the way the pre-23 wrapper ran it, with avdmanager's
    classpath (the same sdklib) and ``com.android.sdklib.toolsdir`` set so it
    finds its SDK root.
    """
    tools = Path(_sdk_tool("avdmanager")).parents[1]
    jar = tools / "lib" / "sdklib" / "tools.sdklib.jar"
    classpath = tools / "lib" / "avdmanager-classpath.jar"
    for path in (jar, classpath):
        if not path.exists():
            raise CaptureError(f"{path} not found; this cmdline-tools has no Java sdkmanager")
    java_home = os.environ.get("JAVA_HOME")
    java = str(Path(java_home) / "bin" / "java") if java_home else shutil.which("java")
    if not java:
        raise CaptureError("no `java` on PATH and JAVA_HOME is not set")
    return [
        java,
        f"-Dcom.android.sdklib.toolsdir={tools}",
        "-classpath",
        f"{classpath}{os.pathsep}{jar}",
        _JAVA_SDKMANAGER_MAIN,
    ]


def _capture_java_sdkmanager(*args: str, timeout: int) -> Callable[..., Capture]:
    """A ``capture_with`` that runs the Java sdkmanager with ``args``."""

    def capture(_scratch: Path, _serial: str, extra: dict) -> Capture:
        argv = _java_sdkmanager()
        tools = Path(argv[1].split("=", 1)[1])
        properties = (tools / "source.properties").read_text(encoding="utf-8")
        revision = re.search(r"^Pkg\.Revision=(.+)$", properties, re.MULTILINE)
        version = subprocess.run(
            [argv[0], "-version"], capture_output=True, text=True, timeout=30, check=False
        )
        extra["cmdline_tools_revision"] = revision.group(1).strip() if revision else None
        extra["java_version"] = (version.stderr or version.stdout).splitlines()[0]
        return _run_host([*argv, *args], timeout)

    return capture


_JAVA_SDKMANAGER_COMMAND = (
    "java -Dcom.android.sdklib.toolsdir=<cmdline-tools/latest> -classpath "
    "<cmdline-tools/latest>/lib/avdmanager-classpath.jar:"
    f"<cmdline-tools/latest>/lib/sdklib/tools.sdklib.jar {_JAVA_SDKMANAGER_MAIN}"
)


def _make_scripts_importable() -> None:
    """Put the skill's scripts directory on sys.path, for ``common.*``."""
    scripts = (
        Path(__file__).resolve().parents[1]
        / "android-emulator-skill"
        / "skills"
        / "android-emulator-skill"
        / "scripts"
    )
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))


def _avd_home() -> Path:
    """The directory holding ``<name>.avd``, resolved as the scripts resolve it.

    Delegated to ``common.sdk_tools.resolve_avd_home`` for the same reason as
    :func:`_emulator_tool`: a recorder that looked somewhere else would read a
    config.ini the scripts never would.
    """
    _make_scripts_importable()
    from common.sdk_tools import resolve_avd_home

    return resolve_avd_home()


def _scrub_home_paths(text: str) -> str:
    """Replace the recording machine's home directory with a placeholder.

    `avdmanager list avd` prints `Path: /Users/<someone>/.android/avd/...`.
    Fixtures are committed to a public repository, so the account name of
    whoever recorded them must not travel with the file.
    """
    return text.replace(str(Path.home()), "/Users/<user>")


def _pull_app_database(scratch: Path, serial: str) -> None:
    """Copy the fixture app's database to the host, byte for byte.

    `adb exec-out` rather than a device-side redirect: `run-as ... cat > /sdcard/x`
    runs the redirect as the shell uid while `cat` runs as the app uid, and the
    measured result is a 0-byte file. exec-out streams the bytes straight to the
    host with no pty and no intermediate file.
    """
    cmd = _adb_argv(
        serial, ["exec-out", "run-as", "com.example.composefixture", "cat", "databases/fixture.db"]
    )

    result = subprocess.run(cmd, capture_output=True, timeout=DEFAULT_TIMEOUT, check=False)
    if result.returncode != 0 or not result.stdout:
        raise CaptureError(
            "could not read the fixture app's database. Install it first:\n"
            "  cd tests/fixtures/scaffold/compose && gradle :app:installDebug\n"
            "then launch it once so DefaultActivity seeds the data dir."
        )
    (scratch / "fx.db").write_bytes(result.stdout)


def _run_host(
    argv: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    *,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> Capture:
    """Run a command on the HOST and return its output.

    For the one case where the text a parser consumes is not produced by adb:
    `sqlite3` is absent from Android user builds (verified missing on API 35),
    so `model_inspector`'s working path pulls the database and runs the host's
    sqlite3 against it. Recording the adb call instead would capture the pull,
    not the schema the parser actually reads.

    ``stdin`` is for `avdmanager create avd`, which emulator_create feeds
    ``no\n`` in case it asks about a custom hardware profile. Without it the
    child inherits the recorder's stdin, and a prompt would wait on a terminal.
    ``env`` is for the same lifecycle: it points ANDROID_AVD_HOME at a private
    directory so avdmanager cannot reach the user's own AVDs.

    Raises:
        ValueError: ``argv`` is an adb command that could address a device.
            Those go through :func:`_adb_argv`, which always names the serial.
    """
    if Path(argv[0]).name == "adb" and (len(argv) < 2 or argv[1] not in _HOST_ADB_SUBCOMMANDS):
        raise ValueError(f"a device adb command must name its serial: {argv}")
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            input=stdin,
            env=env,
        )
    except FileNotFoundError as exc:
        raise CaptureError(f"{argv[0]} is not installed on this host") from exc
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"timed out after {timeout}s") from exc

    capture = Capture(result.stdout, result.stderr, result.returncode)
    if not capture.text.strip():
        raise CaptureError(f"{' '.join(argv)} produced no output")
    return capture


def _strip_cr(text: str) -> str:
    """Normalise adb's CRLF line endings to LF.

    adb's shell transport converts LF to CRLF. Real consumers on the host see
    the CRLF, but committing it makes diffs unreadable and trips editors, so we
    normalise and require parsers to be newline-agnostic (they must be anyway).
    """
    return text.replace("\r\n", "\n")


# Package prefixes that ship with AOSP / Google and carry no private
# information. Anything else on a developer's device is their own work and must
# not be committed to a public repository.
_PUBLIC_PACKAGE_PREFIXES = (
    "android.",
    "com.android.",
    "com.google.",
    "androidx.",
    "java.",
    "javax.",
    "kotlin.",
    "dalvik.",
    "libcore.",
    "org.chromium.",
    "com.example.",
)

_PACKAGE_RE = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z0-9_]+){2,}\b")


def _redact_private_packages(text: str, mapping: dict[str, str]) -> str:
    """Replace non-AOSP package names with stable ``com.example.appN`` aliases.

    Fixtures are recorded from a real developer device, which routinely has the
    developer's own apps installed. Their package names would otherwise be
    committed to a public repository.

    This is the single deliberate exception to the verbatim rule, and it is
    safe for the purpose: parsers care about the *shape* of a line, and an alias
    of the same shape exercises exactly the same code path. The substitution is
    stable across a run (so cross-references stay consistent) and the count is
    recorded in MANIFEST.json so the redaction is never invisible.
    """

    def alias(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith(_PUBLIC_PACKAGE_PREFIXES):
            return token
        if token not in mapping:
            mapping[token] = f"com.example.app{len(mapping) + 1}"
        return mapping[token]

    return _PACKAGE_RE.sub(alias, text)


# Any alias _redact_private_packages (or a binary fixture's own aliasing) wrote.
_ALIAS_RE = re.compile(rb"\bcom\.example\.app\d+\b")


def _aliases_present(recorded_dir: Path) -> int:
    """How many distinct ``com.example.appN`` aliases this profile's fixtures hold.

    MANIFEST.json's ``redacted_package_count`` was the CURRENT run's count, so a
    partial re-record (``--only``) that aliased nothing wrote 0 over a corpus
    still holding nine aliases. It is counted over every fixture file instead,
    as bytes (some are binary). Each run numbers its aliases from app1, so two
    runs can reuse a number for different packages: this is a lower bound on
    the packages redacted, and exact for a corpus recorded in one run.
    """
    return len(
        {
            alias
            for path in recorded_dir.iterdir()
            if path.is_file() and path.name != "MANIFEST.json"
            for alias in _ALIAS_RE.findall(path.read_bytes())
        }
    )


# The Compose scaffold under tests/fixtures/scaffold/compose. Two exported
# activities render the same screen; only the root Modifier differs. See that
# directory's AndroidManifest.xml for the build-and-install commands.
COMPOSE_PACKAGE = "com.example.composefixture"

# Its data dir as container.py and model_inspector.py name it: absolute, so a
# listing they run -- and the error a failed one prints -- carries this path.
COMPOSE_DATA_DIR = f"/data/data/{COMPOSE_PACKAGE}"


def _trim_hierarchy(raw: str) -> str:
    """Trim an ``exec-out uiautomator dump`` stream to just its XML document.

    uiautomator writes its status line ("UI hierchary dumped to: /dev/tty" --
    Android's typo, not ours) onto the same stream, so the raw bytes are not a
    parseable document.
    """
    start = raw.find("<?xml")
    if start == -1:
        raise CaptureError(f"no XML in uiautomator output: {raw.strip()[:120]!r}")
    end = raw.rfind("</hierarchy>")
    if end == -1:
        raise CaptureError("uiautomator output truncated: no closing </hierarchy>")
    return raw[start : end + len("</hierarchy>")] + "\n"


def _requires(*needles: str, absent: tuple[str, ...] = ()) -> Callable[[str], str]:
    """Refuse a capture that does not carry the fact it exists to record.

    A grep that matched nothing, a device with only one serial attached, a
    command whose failure mode changed -- all of them produce a short, valid,
    completely useless file. Writing it would put something that looks like
    ground truth on disk, which is the failure this recorder exists to prevent.

    Args:
        *needles: Text that must appear.
        absent: Text that must NOT appear. Some fixtures exist to record an
            absence -- ``dumpsys_package_after_silent_grant`` is the read-back
            proving ``pm grant`` did nothing, and it is worthless if the
            permission turns out to be there.
    """

    def check(text: str) -> str:
        for needle in needles:
            if needle not in text:
                raise CaptureError(
                    f"capture does not contain {needle!r}, so it is not the "
                    f"output this fixture is meant to record"
                )
        for needle in absent:
            if needle in text:
                raise CaptureError(
                    f"capture contains {needle!r}, whose absence is the whole "
                    f"point of this fixture"
                )
        return text

    return check


def _requires_empty() -> Callable[[str], str]:
    """Refuse a capture that produced any output at all.

    The mirror image of :func:`_requires`, and it exists because one fixture's
    whole content is nothing: ``pidof`` for a package that is not running
    prints no bytes and exits 1. A zero-byte file is indistinguishable from a
    capture that failed, which is exactly the ambiguity this recorder refuses
    to commit — so the emptiness is *asserted* here, and the exit status that
    gives it meaning is written to MANIFEST.json alongside.
    """

    def check(text: str) -> str:
        if text.strip():
            raise CaptureError(
                f"expected no output, got {text.strip()[:120]!r}. This fixture "
                f"records an empty result; a non-empty one means the premise "
                f"changed and the test built on it is now wrong"
            )
        return text

    return check


def _grep_lines(pattern: str, *, minimum: int = 1) -> Callable[[str], str]:
    """Keep only the lines matching ``pattern``, and require at least ``minimum``.

    Filtering happens **before** anything reaches disk, which is the point:
    ``dumpsys input_method`` is 7000+ lines on a real device and names the
    user's IME, their keyboard app and every window they have open. Package
    redaction does not catch free text, so the whole dump must never be
    written — only the handful of state flags a parser needs.

    The minimum guards the other failure: a filter that matches nothing yields
    a valid, empty, worthless file that looks like ground truth.
    """
    compiled = re.compile(pattern)

    def keep(text: str) -> str:
        lines = [line for line in text.splitlines() if compiled.search(line)]
        if len(lines) < minimum:
            raise CaptureError(
                f"filter {pattern!r} matched {len(lines)} line(s), need at "
                f"least {minimum}; the keys this fixture records are not in "
                f"this dump"
            )
        return "\n".join(lines) + "\n"

    return keep


# Height in pixels of the strip kept from a screencap. The status bar is ~100px
# tall at 420dpi; 120 keeps it whole with a margin, and cropping is what keeps
# these fixtures small enough to commit AND removes the app content below,
# which on a real device is the user's own screen.
STATUS_BAR_STRIP_PX = 120

# A fixture that has to be looked at is worthless if it is too big to open in a
# diff. Both status-bar strips come in around 5KB against a flat background.
MAX_PNG_BYTES = 50 * 1024


def _status_bar_strip(serial: str, extra: dict) -> bytes:
    """Screencap the device and return the top strip as a PNG.

    Cropped rather than recorded whole for two reasons: a full 1080x2424
    screenshot is 1.4MB, and everything below the status bar is whatever
    happened to be on screen.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise CaptureError(
            "Pillow is required to record the status-bar PNGs. Run the "
            "recorder with it available, e.g.\n"
            "  uv run --python 3.12 --with pillow python tests/record_fixtures.py ..."
        ) from exc

    import io

    raw = _run_bytes(["exec-out", "screencap", "-p"], serial, timeout=60)
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    strip = image.crop((0, 0, image.width, STATUS_BAR_STRIP_PX))
    buffer = io.BytesIO()
    strip.save(buffer, format="PNG", optimize=True)
    payload = buffer.getvalue()
    if len(payload) > MAX_PNG_BYTES:
        raise CaptureError(
            f"status-bar strip is {len(payload)} bytes, over the "
            f"{MAX_PNG_BYTES}-byte budget. Record it over a flat background "
            f"(Settings), not the home-screen wallpaper: the gradient is what "
            f"makes the PNG large."
        )
    return payload


def _display_restored(*, size: bool = False, density: bool = False) -> Callable[[str], None]:
    """Read the display state back and refuse to believe an unverified reset.

    `wm size reset` prints nothing whether it worked or not, so the recorder
    used to take its silence as success. It is the only thing standing between
    a recording run and leaving somebody's phone at 1080x2400 -- the override
    survives the process exiting, and no other check in this file would notice.

    So the reset is verified the way a person would verify it: read `wm size`
    back and require the Override line to be gone.
    """

    def verify(serial: str) -> None:
        for enabled, subcommand in ((size, "size"), (density, "density")):
            if not enabled:
                continue
            readback = _run(["shell", "wm", subcommand], serial, timeout=15)
            if readback.returncode != 0:
                raise CaptureError(
                    f"could not read `wm {subcommand}` back to confirm the "
                    f"reset (exit {readback.returncode})"
                )
            if "Override" in readback.text:
                raise CaptureError(
                    f"`wm {subcommand} reset` did not take -- the device still "
                    f"reports an override: {readback.text.strip()!r}"
                )

    return verify


DEMO_CLOCK_RENDERED = "9:41"


def _device_clock(serial: str) -> str:
    """The device's own wall-clock time, in the form SystemUI renders.

    `%I:%M` is the 12-hour form the status bar uses (measured: the bar reads
    "12:25" where `date +%H:%M` says "00:25"), with the leading zero stripped
    the way the bar strips it.

    Read twice and required to agree, so a value recorded across a minute
    boundary is refused rather than written -- the manifest once claimed the
    unpadded capture showed 12:16 when the committed PNG showed 12:25, and a
    wrong observed value in the evidence is worse than none.
    """

    def now() -> str:
        result = _run(["shell", "date", "+%I:%M"], serial, timeout=15)
        if result.returncode != 0 or not result.text.strip():
            raise CaptureError("could not read the device clock")
        return result.text.strip().lstrip("0")

    before = now()
    if before != now():
        raise CaptureError(
            "the device clock ticked while the status bar was being captured, "
            "so the observed time cannot be pinned to the image; re-run"
        )
    return before


def _status_bar_strip_showing(demo_clock_applied: bool) -> Callable[[str, dict], bytes]:
    """Screencap the status bar and record, measured, what its clock reads.

    Neither `dumpsys` nor `uiautomator` exposes the rendered status-bar clock
    (checked: the SystemUI dump carries only the keyguard clock, and the status
    bar is not in a uiautomator dump at all), so the value cannot be read back
    out of the device directly. What CAN be measured is the device's own clock
    at the moment of capture, and that is enough to make each image unambiguous:

    * unpadded (`hhmm 941`): SystemUI ignored the argument, so the bar is
      showing the device clock, and that is what gets recorded.
    * padded (`hhmm 0941`): the bar is showing 9:41. Recorded together with the
      device clock at capture, and the capture is REFUSED if the device clock
      happens to be 9:41 too -- because then the image could not distinguish a
      broadcast that worked from one that was ignored.

    The same refusal guards the unpadded case, so neither PNG can be recorded
    at the one minute of the day that would make the pair meaningless.
    """

    def capture(serial: str, extra: dict) -> bytes:
        device_clock = _device_clock(serial)
        if device_clock == DEMO_CLOCK_RENDERED:
            raise CaptureError(
                f"the device clock reads {device_clock}, which is exactly the "
                f"time the demo broadcast sets. Neither capture could then show "
                f"whether the broadcast was applied. Wait a minute and re-run."
            )
        extra["device_clock_at_capture"] = device_clock
        if demo_clock_applied:
            extra["clock_reads"] = DEMO_CLOCK_RENDERED
            extra["clock_source"] = (
                "the hhmm argument SystemUI accepted and rendered; the device "
                "clock at capture was different (see device_clock_at_capture), "
                "so the bar cannot be showing real time by coincidence"
            )
        else:
            extra["clock_reads"] = device_clock
            extra["clock_source"] = (
                "adb shell date +%I:%M, read either side of the screencap: "
                "SystemUI dropped the 3-digit hhmm, so the bar is still showing "
                "the device's own time"
            )
        return _status_bar_strip(serial, extra)

    return capture


def _anr_for(package: str) -> Callable[[str], str]:
    """Refuse an ANR capture that is not an ANR of ``package``.

    An ANR is provoked, not requested, and every way of provoking one can
    quietly fail to: the app may not have been foreground, the broadcast may
    have gone to a process that was not running, the wait may have been too
    short. Each of those yields an empty grep, and an empty ANR fixture would
    assert nothing while looking like evidence.
    """

    def check(text: str) -> str:
        if "ANR in " not in text:
            raise CaptureError(
                "no 'ANR in' line was captured: the app did not ANR. Install "
                "the fixture app first (cd tests/fixtures/scaffold/compose && "
                "gradle :app:installDebug) -- its AnrReceiver is what earns the "
                "ANR."
            )
        if package not in text:
            raise CaptureError(f"the captured ANR is not {package}'s; refusing to record it")
        return text

    return check


def _anr_stream_for(package: str, *, requires: tuple[str, ...]) -> Callable[[str], str]:
    """Keep one provoked ANR as the stream carries it, from the provocation on.

    These captures do not clear the buffer first (``logcat -c`` would empty it
    under anyone else reading the emulator's log), so the dump starts with
    whatever the buffer held. It is cut at the LAST ``Broadcasting: Intent``
    line for the provoking action -- ActivityManager logs it as the broadcast
    is sent -- so what is kept is this provocation and what followed it.

    Args:
        package: The app that must have ANRed.
        requires: Text the kept stream must carry: the lines the test built on
            it reads. A capture missing one did not record what it exists for.
    """
    marker = f"Broadcasting: Intent {{ act={package}.PROVOKE_ANR"

    def cut(text: str) -> str:
        lines = text.splitlines(keepends=True)
        starts = [i for i, line in enumerate(lines) if marker in line]
        if not starts:
            raise CaptureError(
                f"no {marker!r} line: the broadcast was not sent, or ActivityManager no "
                f"longer logs it -- either way the stream cannot be cut to this ANR"
            )
        kept = "".join(lines[starts[-1] :])
        _anr_for(package)(kept)
        for needle in requires:
            if needle not in kept:
                raise CaptureError(
                    f"the stream after the provocation does not contain {needle!r}; "
                    f"the ANR did not unfold the way this fixture records it"
                )
        return kept

    return cut


def _hierarchy_of(package: str) -> Callable[[str], str]:
    """Trim the dump and assert it is of ``package``.

    A dump always succeeds: if the activity never launched, uiautomator happily
    describes the launcher instead, and the result would be a file that looks
    like a Compose hierarchy and is not -- the exact failure this recorder
    exists to prevent.
    """

    def trim(raw: str) -> str:
        xml = _trim_hierarchy(raw)
        if f'package="{package}"' not in xml:
            raise CaptureError(
                f"dump contains no {package!r} node: the activity did not launch. "
                f"Build and install tests/fixtures/scaffold/compose first."
            )
        return xml

    return trim


def _screen_of(
    package: str,
    *,
    require: tuple[str, ...] = (),
    forbid: tuple[str, ...] = (),
    scrollable: bool | None = None,
) -> Callable[[str], str]:
    """Trim the dump and refuse it unless it is the screen that was asked for.

    A scroll fixture is only worth having if the scroll actually happened, and
    a dump gives no sign either way: swipe the wrong distance, or catch the
    list mid-fling, and you get a perfectly valid hierarchy of the wrong
    screen. A test written against that pair would prove nothing while looking
    like ground truth. So each capture states the fact it is meant to carry --
    the target text is present, or absent, or the screen has no scrollable
    container -- and a capture that does not carry it is dropped rather than
    written.

    Args:
        package: Package that must own nodes in the dump.
        require: Substrings that must appear (raw XML text, so escape as
            uiautomator does: ``&amp;`` for an ampersand).
        forbid: Substrings that must not appear.
        scrollable: When set, whether the screen must have at least one
            ``scrollable="true"`` node.

    Returns:
        A post-processor for :class:`Fixture`.
    """

    def check(raw: str) -> str:
        xml = _trim_hierarchy(raw)
        if f'package="{package}"' not in xml:
            raise CaptureError(f"dump contains no {package!r} node: the screen did not open")
        for needle in require:
            if needle not in xml:
                raise CaptureError(
                    f"captured screen does not contain {needle!r}: it is not the "
                    f"screen this fixture is supposed to record"
                )
        for needle in forbid:
            if needle in xml:
                raise CaptureError(
                    f"captured screen still contains {needle!r}: the scroll did "
                    f"not move the list as far as this fixture requires"
                )
        if scrollable is not None:
            found = xml.count('scrollable="true"')
            if bool(found) is not scrollable:
                raise CaptureError(
                    f"captured screen has {found} scrollable node(s); this "
                    f"fixture requires scrollable={scrollable}"
                )
        return xml

    return check


def _chain(*steps: Callable[[str], str]) -> Callable[[str], str]:
    """Apply several ``post`` transforms in order: scrub first, then check."""

    def run(text: str) -> str:
        for step in steps:
            text = step(text)
        return text

    return run


def _device_path_absent(listing: list[str]) -> Callable[[str], None]:
    """Read a teardown's deletion back instead of trusting ``rm``'s silence.

    ``rm -f`` exits 0 whether or not there was anything to remove, so its
    status says nothing. The listing must fail AND say why: a listing refused
    for permission would also exit non-zero, and would prove nothing.
    """

    def verify(serial: str) -> None:
        result = _run(listing, serial, timeout=15)
        if result.returncode == 0 or "No such file" not in result.text:
            raise CaptureError(
                f"`adb {' '.join(listing)}` does not show the path gone after "
                f"teardown (exit {result.returncode}): {result.text.strip()[:200]!r}"
            )

    return verify


# The name a fixture moves one of the fixture app's directories aside to while
# it records what a listing of the missing directory prints.
_MOVED_ASIDE = ".aes_moved"


def _app_dir_restored(directory: str, holds: str) -> Callable[[str], None]:
    """Read back that the fixture app's ``directory`` is listable again and whole.

    For the fixtures that move a directory aside, or take its owner's read bit
    away, to record what a failed listing prints. ``mv`` and ``chmod`` exiting
    0 is not the same claim: the listing must succeed, still hold ``holds`` (a
    file the app wrote there), and the moved-aside name must exist neither
    beside it nor inside it -- which is where ``mv`` would have put it had the
    app recreated the directory meanwhile (the teardown's ``-T`` refuses that).
    """

    def verify(serial: str) -> None:
        argv = ["shell", "run-as", COMPOSE_PACKAGE, "ls", "-la", directory]
        listing = _run(argv, serial, timeout=15)
        if (
            listing.returncode != 0
            or f" {holds}\n" not in _strip_cr(listing.stdout)
            or _MOVED_ASIDE in listing.stdout
        ):
            raise CaptureError(
                f"`adb {' '.join(argv)}` does not show {directory}/ restored (exit "
                f"{listing.returncode}): {listing.text.strip()[:200]!r}"
            )
        moved = ["shell", "run-as", COMPOSE_PACKAGE, "ls", f"{directory}{_MOVED_ASIDE}"]
        _device_path_absent(moved)(serial)

    return verify


def _all_of(*verifies: Callable[[str], None]) -> Callable[[str], None]:
    """One ``teardown_verify`` running each of ``verifies``, for a setup that moved two things."""

    def verify(serial: str) -> None:
        for each in verifies:
            each(serial)

    return verify


# --- Listing a directory: the skill's own command -----------------------------
#
# container.py and model_inspector.py list an app directory with
# common.run_as.listing_command (toybox find, NUL-terminated fields). These
# fixtures run exactly that argv, built and quoted by the skill's own code, so
# the recorder and the scripts cannot disagree about what was run.


def _listing(directory: str) -> list[str]:
    """`adb shell run-as <fixture app> <the skill's listing of directory>`, quoted as the scripts quote it."""
    _make_scripts_importable()
    from common.device_utils import quote_for_device_shell
    from common.run_as import listing_command

    argv = [quote_for_device_shell(arg) for arg in listing_command(directory)]
    return ["shell", "run-as", COMPOSE_PACKAGE, *argv]


# --- Reading a file: container.py's --cat --------------------------------------
#
# container.py reads a file with common.run_as.read_command (`adb shell -T
# run-as <pkg> cat <absolute path>`). These fixtures run that argv, built by the
# skill's own code, and keep stdout byte for byte (capture_bytes): the text
# pipeline's decode and newline translation would hide the very bytes --cat
# decides "text" or "binary" on.


def _read_file(path: str) -> list[str]:
    """`adb shell -T run-as <fixture app> cat <path>`, as container.py builds it."""
    _make_scripts_importable()
    from common.run_as import read_command

    return read_command(None, COMPOSE_PACKAGE, ["cat", path])[1:]


# container.py's default CAT_MAX_BYTES. The recording is the WHOLE file; the
# cap is applied by the script, so the manifest says where it falls.
CAT_CAP_BYTES = 65_536
UTF8_CAP_NAME = "aes_utf8_cap.txt"
UTF8_CAP_STAGED = f"/data/local/tmp/{UTF8_CAP_NAME}"
UTF8_CAP_PATH = f"{COMPOSE_DATA_DIR}/files/{UTF8_CAP_NAME}"
_UTF8_CAP_LINE = "Grüße aus Köln — 日本語のテキスト — naïve café, señor\n"


def _utf8_cap_text() -> bytes:
    """UTF-8 text longer than CAT_CAP_BYTES whose cap cuts a three-byte character after two.

    Numbered lines of mixed-script text, behind an ASCII header padded until
    byte CAT_CAP_BYTES - 1 is the second byte of a CJK character: the cut then
    leaves an incomplete sequence at the very end and nothing invalid before it.
    """
    lines = "".join(f"{n:05d} {_UTF8_CAP_LINE}" for n in range(1, 900))
    for pad in range(64):
        data = (f"# --cat cap probe{'.' * pad}\n" + lines).encode("utf-8")
        lead = data[CAT_CAP_BYTES - 2]
        if 0xE0 <= lead <= 0xEF:
            return data
    raise CaptureError("no padding puts the cap inside a three-byte character")


def _push_utf8_cap(scratch: Path, serial: str) -> None:
    """Push :func:`_utf8_cap_text` into the fixture app's files/ (staged in /data/local/tmp)."""
    local = scratch / UTF8_CAP_NAME
    local.write_bytes(_utf8_cap_text())
    for argv in (
        ["push", str(local), UTF8_CAP_STAGED],
        ["shell", "-T", "run-as", COMPOSE_PACKAGE, "cp", UTF8_CAP_STAGED, UTF8_CAP_PATH],
    ):
        result = _run(argv, serial)
        if result.returncode != 0:
            raise CaptureError(f"`adb {' '.join(argv)}` exited {result.returncode}: {result.text}")


def _utf8_cap_read(serial: str, extra: dict) -> bytes:
    """The app's own read of the pushed file, refused unless it is the file pushed."""
    payload = _run_bytes(_read_file(UTF8_CAP_PATH), serial)
    if payload != _utf8_cap_text():
        raise CaptureError(
            f"read back {len(payload)} bytes that are not the {len(_utf8_cap_text())} pushed"
        )
    head = payload[:CAT_CAP_BYTES]
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason != "unexpected end of data" or exc.end != len(head):
            raise CaptureError(f"the cap does not fall inside a character: {exc}") from exc
        extra["cap_bytes"] = CAT_CAP_BYTES
        extra["split_character_starts_at"] = exc.start
    else:
        raise CaptureError("the first CAT_CAP_BYTES bytes decode whole: the cap is on a boundary")
    return payload


def _sqlite_read(serial: str, _extra: dict) -> bytes:
    """The app's own read of databases/fixture.db, refused unless it is an SQLite file."""
    payload = _run_bytes(_read_file(f"{COMPOSE_DATA_DIR}/databases/fixture.db"), serial)
    if not payload.startswith(b"SQLite format 3\x00"):
        raise CaptureError(f"databases/fixture.db is not an SQLite file: {payload[:16]!r}")
    return payload


def _both_streams(args: list[str], name: str) -> Callable[[Path, str, dict], Capture]:
    """Capture ``adb <args>`` once; stdout is fixture ``name``, stderr its twin ``name``_stderr.

    A find that fails after printing prints on BOTH streams -- an unreadable
    directory's own record, or a record cut at an escape it does not know, on
    stdout, then the reason on stderr -- and a fixture file holds one stream.
    Both come from this one run; the stderr is written by the twin fixture that
    follows this one.
    """

    def capture(_scratch: Path, serial: str, extra: dict) -> Capture:
        result = _run(args, serial)
        if not (result.stdout and result.stderr) or result.returncode == 0:
            raise CaptureError(
                f"expected a failure on both streams, got exit {result.returncode}, "
                f"stdout {result.stdout[:80]!r}, stderr {result.stderr[:200]!r}"
            )
        _stash_twin(
            f"{name}_stderr",
            result.stderr.encode("utf-8"),
            {"first_half": name, "exit_status": result.returncode, "stream": "stderr"},
        )
        return result

    return capture


# A scratch directory in the fixture app's files/, made by the names fixtures
# and removed after. It holds the names a listing has to keep whole: one with a
# newline followed by a complete `ls -la` line for ghost.db (the final review's
# forgery), one with a backslash, one with a space, a symlink to that, a symlink
# whose TARGET holds a newline, a subdirectory holding one file, and a symlink
# to that subdirectory. run-as's shell has a umask of 0, so the files are 0666
# and the directories 0777.
NAMES_DIR = f"{COMPOSE_DATA_DIR}/files/aes_names"
_NAMES_SETUP = (
    f"rm -rf {NAMES_DIR} && mkdir {NAMES_DIR} && cd {NAMES_DIR} && "
    "touch \"$(printf 'x.db\\n-rw-rw---- 1 u0_a205 u0_a205 32768 2026-10-18 22:28 ghost.db')\" && "
    "touch 'c\\d.db' 'e f.db' && ln -s 'e f.db' link && "
    "ln -s \"$(printf 'dangling\\ntarget')\" lflink && mkdir sub && touch sub/inner && "
    "ln -s sub dirlink"
)
_NAMES_MADE = [["shell", "run-as", COMPOSE_PACKAGE, "sh", "-c", shlex.quote(_NAMES_SETUP)]]
_NAMES_REMOVED = [["shell", "run-as", COMPOSE_PACKAGE, "rm", "-rf", NAMES_DIR]]
_NAMES_GONE = _device_path_absent(["shell", "run-as", COMPOSE_PACKAGE, "ls", NAMES_DIR])
# The listing with its last escape, %P, swapped for one this find does not have.
_UNKNOWN_ESCAPE = [arg.replace("%P", "%Q") for arg in _listing(f"{COMPOSE_DATA_DIR}/databases")]


# --- am profile: sampling a hung app without root ----------------------------
#
# On a `user` build there is no root, no simpleperf for an app that is not
# profileable from shell, and no Studio on a CI host. `am profile start` still
# works for a debuggable app, and it is the one way to see what a hung main
# thread is doing while it hangs. These recordings are the ground truth the
# v0.9.0 sampler is parsed against, and the two destinations it must not offer.

# The destination that works, measured on API 35. system_server opens the file
# and hands the descriptor to the app, so it has to be a path system_server may
# create (not /sdcard -- an SELinux denial -- and not the app's own data dir),
# and the shell user has to be able to read it back.
TRACE_DEVICE_PATH = "/data/local/tmp/aes_hang.trace"

# Sample every 1000 us. Sampled rather than instrumented, so the app runs at
# close to normal speed while it is being watched.
TRACE_SAMPLING_US = "1000"

# AnrReceiver blocks main for 40 s from dispatch. The stop must come after main
# has unblocked, or the sleep's exit is not in the trace and its dwell cannot be
# measured. 45 s is the margin of the original capture (48 s of trace).
TRACE_STOP_AFTER_S = 45

# Before the profiler starts, the app is left alone long enough for Jetpack
# ProfileInstaller's one-shot worker -- posted 5-6 s after the first frame, run
# on a pool thread with no keep-alive -- to start AND exit. Observed in 1 of 3
# recorder runs on 2026-10-16: the app aborted with ART `thread.cc:2698 Check
# failed: tlsPtr_.method_trace_buffer == nullptr` in that worker 57 ms after it
# started, mid-trace, and the trace was lost with the process. (The ANR held the
# worker's message behind the blocked main thread, so it ran the moment main
# unblocked.) One observation, from logcat at the time; nothing here measures
# how often it happens or proves the wait prevents it.
TRACE_QUIET_BEFORE_S = "10"

# The trace is binary, so the package aliaser never sees it. These allowlists
# are its privacy check instead, applied to every name its text header carries:
# each method's declaring class, every class type in each method's signature,
# and each thread's name. They list exactly what the committed trace needs --
# NOT the aliaser's broad `com.example.` (any app's package), only the fixture
# app's own. Other captures of this same procedure named more: the 12.5 KB
# original reached jdk.internal.misc.Unsafe and ran threads `SharedPreferences`,
# `pool-3-thread-1` and `binder:6457_2`; one refused run reached libcore's
# org.apache.harmony.dalvik.ddmc.DdmServer; and the 51,132-byte capture this one
# replaced ran `RenderThread`. None is listed. A capture naming one is refused;
# re-run it, or add the name here with the capture it came from.
_TRACE_CLASS_PREFIXES = (
    f"{COMPOSE_PACKAGE}.",
    "android.",
    "androidx.",
    "com.android.",
    "dalvik.",
    "java.",
    "kotlin.",
    "kotlinx.",
    "libcore.",
    "sun.misc.",
)
_TRACE_THREAD_NAMES = frozenset(
    {
        "main",
        "FinalizerDaemon",
        "FinalizerWatchdogDaemon",
        "HeapTaskDaemon",
        "ReferenceQueueDaemon",
        "SurfaceSyncGroupTimer",
    }
)

# A JVM method descriptor: `(` parameter types `)` return type, each a
# primitive letter or `L<binary/name>;`, optionally behind `[`s. Anything else in
# the signature column is refused rather than half-read.
_DESCRIPTOR_RE = re.compile(r"\((?:\[*(?:[ZBCSIJFD]|L[^;]+;))*\)\[*(?:[ZBCSIJFDV]|L[^;]+;)")
_DESCRIPTOR_CLASS_RE = re.compile(r"L([^;]+);")

# The trace and both refusals are taken from a running, foreground app: `am
# profile start` names a PROCESS, and a background ANR may simply kill it.
_LAUNCH_COMPOSE = [
    ["shell", "am", "force-stop", COMPOSE_PACKAGE],
    ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.DefaultActivity"],
    ["shell", "sleep", "2"],
]

_PROVOKE_ANR = [
    "shell",
    "am",
    "broadcast",
    "-f",
    "0x10000000",
    "-n",
    f"{COMPOSE_PACKAGE}/.AnrReceiver",
    "-a",
    f"{COMPOSE_PACKAGE}.PROVOKE_ANR",
]

_PROFILE_STOP = ["shell", "am", "profile", "stop", COMPOSE_PACKAGE]


def _profile_start(destination: str) -> list[str]:
    """The `am profile start` argv every fixture here records."""
    return [
        "shell",
        "am",
        "profile",
        "start",
        "--sampling",
        TRACE_SAMPLING_US,
        COMPOSE_PACKAGE,
        destination,
    ]


def _check_hang_trace(payload: bytes) -> None:
    """Refuse a trace that is not the hang it is meant to be, or names private code.

    Checks only what makes the capture usable and publishable; what the trace
    SAYS is the test's business (tests/test_am_profile_trace.py).
    """
    end = payload.find(b"\n*end\n")
    if not payload.startswith(b"*version\n") or end == -1:
        raise CaptureError("not an ART method trace: no `*version` ... `*end` text header")
    body = payload[end + len(b"\n*end\n") :]
    if not body.startswith(b"SLOW"):
        raise CaptureError(f"no SLOW magic after *end: {body[:4]!r}")

    lines = payload[:end].decode("utf-8", errors="replace").splitlines()
    if "data-file-overflow=false" not in lines:
        raise CaptureError("the trace buffer overflowed, so the record stream is truncated")
    try:
        threads = lines[lines.index("*threads") + 1 : lines.index("*methods")]
        methods = lines[lines.index("*methods") + 1 :]
    except ValueError as exc:
        raise CaptureError("the text header has no *threads / *methods section") from exc

    thread_names = [line.partition("\t")[2] for line in threads]
    if "main" not in thread_names:
        raise CaptureError("no `main` thread in *threads")

    # Every name the header carries, against the allowlists. A method line is
    # `<id>\t<class>\t<name>\t<descriptor>\t<source file>`; the method name and
    # source file belong to the declaring class, so bounding the class bounds
    # them, but a descriptor can name ANY type, so its classes are checked too.
    outside: list[str] = [
        f"thread {name!r}" for name in thread_names if name not in _TRACE_THREAD_NAMES
    ]
    classes: set[str] = set()
    for line in methods:
        fields = line.split("\t")
        if len(fields) != 5 or not _DESCRIPTOR_RE.fullmatch(fields[3]):
            outside.append(f"unreadable method line {line[:80]!r}")
            continue
        classes.add(fields[1])
        for name in [fields[1], *_DESCRIPTOR_CLASS_RE.findall(fields[3])]:
            dotted = name.replace("/", ".")
            if not dotted.startswith(_TRACE_CLASS_PREFIXES):
                outside.append(f"class {dotted}")

    if f"{COMPOSE_PACKAGE}.AnrReceiver" not in classes:
        raise CaptureError(
            "AnrReceiver is not in the trace: the broadcast did not land while the "
            "profiler was running"
        )
    if outside:
        found = sorted(set(outside))
        raise CaptureError(
            f"the trace names {len(found)} thing(s) outside its allowlists "
            f"({found[:5]}); refusing to commit it. See _TRACE_CLASS_PREFIXES."
        )


def _wait_for_complete_file(serial: str, path: str, *, within_s: int = 20) -> int:
    """Wait until the app has finished writing ``path``; return its size.

    `am profile stop` returns before the app has written the trace -- the stop
    is a request to the process, which then dumps its buffer. The size has to
    be non-zero and the same twice running before the file is taken.
    """
    deadline = time.monotonic() + within_s
    previous = -1
    while time.monotonic() < deadline:
        result = _run(["shell", "stat", "-c", "%s", path], serial, timeout=15)
        size = int(result.text.strip()) if result.text.strip().isdigit() else -1
        if size > 0 and size == previous:
            return size
        previous = size
        time.sleep(1)
    raise CaptureError(f"{path} did not reach a stable non-zero size within {within_s}s")


def _profile_hang_trace(serial: str, extra: dict) -> bytes:
    """Sample the fixture app while AnrReceiver blocks its main thread for 40 s."""
    start = _run(_profile_start(TRACE_DEVICE_PATH), serial)
    if start.returncode != 0:
        raise CaptureError(
            f"am profile start exited {start.returncode}: {start.text.strip()[:200]!r}"
        )

    began = time.monotonic()
    broadcast = _run(_PROVOKE_ANR, serial, timeout=60)
    extra["broadcast_returned_after_s"] = round(time.monotonic() - began, 1)
    if broadcast.returncode != 0:
        raise CaptureError(f"the ANR broadcast exited {broadcast.returncode}")

    remaining = TRACE_STOP_AFTER_S - (time.monotonic() - began)
    if remaining > 0:
        time.sleep(remaining)
    # A dead process cannot answer the stop, and the file then stays 0 bytes;
    # say why instead of timing out on the size poll.
    alive = _run(["shell", "pidof", COMPOSE_PACKAGE], serial, timeout=15)
    if alive.returncode != 0:
        raise CaptureError(
            f"{COMPOSE_PACKAGE} died while it was being profiled, so the trace "
            f"went with it. Look in logcat for an ART `Check failed: "
            f"tlsPtr_.method_trace_buffer == nullptr` abort; see "
            f"TRACE_QUIET_BEFORE_S"
        )
    stop = _run(_PROFILE_STOP, serial)
    extra["stop_issued_after_s"] = round(time.monotonic() - began, 1)
    if stop.returncode != 0:
        raise CaptureError(f"am profile stop exited {stop.returncode}: {stop.text.strip()!r}")

    extra["device_bytes"] = _wait_for_complete_file(serial, TRACE_DEVICE_PATH)
    payload = _run_bytes(["exec-out", "cat", TRACE_DEVICE_PATH], serial, timeout=60)
    if len(payload) != extra["device_bytes"]:
        raise CaptureError(
            f"read {len(payload)} bytes of a {extra['device_bytes']}-byte trace; "
            f"the stream was altered on the way"
        )
    _check_hang_trace(payload)
    extra["sampling_interval_us"] = int(TRACE_SAMPLING_US)
    return payload


def _profile_refusal(destination: str, listing: list[str]) -> Callable[[Path, str, dict], Capture]:
    """`am profile start` to a destination it refuses, and what the refusal leaves.

    The listing is taken here, not in teardown, because teardown deletes what it
    would show. For /sdcard that is the point: the refusal still leaves a 0-byte
    file behind, so "the trace file exists" is no evidence profiling started.
    """

    def capture(_scratch: Path, serial: str, extra: dict) -> Capture:
        refusal = _run(_profile_start(destination), serial)
        if refusal.returncode == 0:
            raise CaptureError(
                f"am profile start to {destination} exited 0. This fixture records "
                f"a refusal; an accepted destination means the premise changed"
            )
        left = _run(listing, serial, timeout=15)
        extra["left_behind"] = {
            "command": "adb " + " ".join(listing),
            "exit_status": left.returncode,
            "output": _strip_cr(left.text).strip(),
        }
        return refusal

    return capture


# --- avdmanager create / delete: a throwaway AVD, made and removed in one run --

# Named so that nobody has one. The lifecycle runs in a private AVD home, so it
# cannot reach an AVD of the user's by these names -- but it still refuses to
# start if the user's own home has one, as a second guard.
THROWAWAY_AVD = "aes_fixture_tmp"
MISSING_AVD = "aes_does_not_exist"

# What emulator_create builds for `--api 34 --variant google_apis --abi
# arm64-v8a --device pixel_9`, from an image installed on the recording host
# (see sdkmanager_list_installed) and a device in avdmanager_list_device.
THROWAWAY_AVD_PACKAGE = "system-images;android-34;google_apis;arm64-v8a"
THROWAWAY_AVD_DEVICE = "pixel_9"

# avdmanager starts a JVM and loads the local repository on every call; the
# create measured a few seconds, so this is margin, not an estimate.
AVD_TOOL_TIMEOUT = 120

# What the private AVD home is called in anything recorded. avdmanager prints no
# path in the four captures (measured), but its `.ini` beside the AVD does, so
# the temporary directory is replaced wherever it appears rather than trusted
# never to.
PRIVATE_AVD_HOME = "<avd home>"
_PRIVATE_AVD_HOME_PREFIX = "aes_avd_home_"

# The argv each fixture records, in the exact shape emulator_create runs --
# long options, `--device` given -- with the tool as a placeholder so the
# manifest carries no host path. tests/test_emulator_create.py compares the
# script's argv against these.
_AVD_ARGV = {
    "avdmanager_create": [
        "{avdmanager}",
        "create",
        "avd",
        "--name",
        THROWAWAY_AVD,
        "--package",
        THROWAWAY_AVD_PACKAGE,
        "--device",
        THROWAWAY_AVD_DEVICE,
    ],
    "avdmanager_delete": ["{avdmanager}", "delete", "avd", "--name", THROWAWAY_AVD],
    "avdmanager_delete_missing": ["{avdmanager}", "delete", "avd", "--name", MISSING_AVD],
}


def _avd_present(home: Path, name: str) -> bool:
    """Whether ``name`` has either half of an AVD on disk: the dir or the .ini."""
    return (home / f"{name}.avd").exists() or (home / f"{name}.ini").exists()


def _avd_home_entries(home: Path) -> set[str]:
    """The names in an AVD home: which AVDs (and other entries) it holds.

    Names, not mtimes: the user's running emulator rewrites its own `.avd`
    directory continuously, so a timestamp comparison would report the run as
    having touched an AVD it never named.
    """
    try:
        return {entry.name for entry in home.iterdir()}
    except FileNotFoundError:
        return set()


def _throwaway_avd_removed(_serial: str) -> None:
    """Teardown check for the four avdmanager fixtures: nothing in the user's home."""
    home = _avd_home()
    if _avd_present(home, THROWAWAY_AVD):
        raise CaptureError(
            f"an AVD named {THROWAWAY_AVD} is in this host's own AVD home "
            f"({_scrub_home_paths(str(home))}), which the recorder's private home "
            f"should have made impossible. Remove it: avdmanager delete avd "
            f"--name {THROWAWAY_AVD}"
        )


def _without_private_home(text: str, home: Path) -> str:
    """Replace the private AVD home with :data:`PRIVATE_AVD_HOME` in ``text``.

    Both spellings: macOS hands out /var/folders/... and resolves it to
    /private/var/folders/..., and the longer one has to go first or the shorter
    would leave a stray `/private` in front of the placeholder.
    """
    for spelling in sorted({str(home), str(home.resolve())}, key=len, reverse=True):
        text = text.replace(spelling, PRIVATE_AVD_HOME)
    return text


class _ThrowawayAvd:
    """Create an AVD, read its config.ini, delete it, and prove it is gone.

    Four fixtures come out of one lifecycle, and the order is the point:
    config.ini exists only between the create and the delete. So the lifecycle
    runs once per recorder run, the first time any of the four is asked for,
    and each fixture takes its piece -- `--only avdmanager_delete` still
    creates first, and `--only avdmanager_create` still deletes after.

    Every avdmanager call runs with ANDROID_AVD_HOME pointing at a directory
    made for this run and removed after it (measured: avdmanager honours it, and
    `list avd -c` under it lists only what is in it). So the recorder is not
    careful about the user's AVDs, it is unable to reach them: a create that
    fails half-way, a delete by name, a race with another creator -- all of it
    happens in a directory nobody else uses. The user's own AVD home is still
    checked before and after, as a second guard: the throwaway names must not be
    in it, and the set of entries in it must not change.
    """

    def __init__(self) -> None:
        self._captures: dict[str, Capture] | None = None
        self._config_ini = ""
        self._error: CaptureError | None = None

    def capture(self, name: str) -> Callable[[Path, str, dict], Capture]:
        """A ``capture_with`` for one of the three avdmanager commands."""

        def get(_scratch: Path, _serial: str, extra: dict) -> Capture:
            captures = self._run_once()
            extra["argv"] = _AVD_ARGV[name]
            extra["avd_home"] = f"{PRIVATE_AVD_HOME}: a directory made for the run, then removed"
            return captures[name]

        return get

    def config_ini(self, _serial: str, extra: dict) -> bytes:
        """A ``capture_bytes`` for the config.ini, read between create and delete.

        Bytes rather than text so the package aliaser never sees it: its keys
        are dotted lowercase names (`hw.camera.back`, `image.sysdir.1`) that the
        aliaser's pattern takes for package names, and it would rewrite the
        keys a parser looks up. Nothing in it is private -- avdmanager writes it
        from the system image and the device definition -- and the home
        directory and the private AVD home are scrubbed all the same.
        """
        self._run_once()
        extra["source"] = (
            f"{PRIVATE_AVD_HOME}/{THROWAWAY_AVD}.avd/config.ini, read after "
            f"avdmanager_create and before avdmanager_delete"
        )
        text = _requires("image.sysdir.1=", "hw.device.name=", "abi.type=")(
            _scrub_home_paths(self._config_ini)
        )
        return text.encode("utf-8")

    def _run_once(self) -> dict[str, Capture]:
        if self._error is not None:
            raise self._error
        if self._captures is None:
            try:
                self._captures = self._lifecycle()
            except CaptureError as exc:
                self._error = exc
                raise
        return self._captures

    def _lifecycle(self) -> dict[str, Capture]:
        tool = _sdk_tool("avdmanager")
        user_home = _avd_home()
        for name in (THROWAWAY_AVD, MISSING_AVD):
            if _avd_present(user_home, name):
                raise CaptureError(
                    f"an AVD named {name!r} already exists in this host's AVD home. "
                    f"The recorder works in a private home and could not touch it, "
                    f"but refuses anyway rather than guess why it is there; if it "
                    f"is a leftover, remove it with: avdmanager delete avd --name {name}"
                )
        user_entries = _avd_home_entries(user_home)

        private = Path(tempfile.mkdtemp(prefix=_PRIVATE_AVD_HOME_PREFIX))
        try:
            captures = self._in(private, tool)
        finally:
            shutil.rmtree(private, ignore_errors=True)

        if _avd_home_entries(user_home) != user_entries:
            raise CaptureError(
                "the set of entries in this host's own AVD home changed during the "
                "run, although every avdmanager call was pointed elsewhere. Look "
                "before trusting anything recorded here."
            )
        leaked = [
            name
            for name, capture in captures.items()
            if _PRIVATE_AVD_HOME_PREFIX in capture.stdout + capture.stderr
        ]
        if leaked or _PRIVATE_AVD_HOME_PREFIX in self._config_ini:
            raise CaptureError(f"the private AVD home survived the scrub in {leaked}")
        return captures

    def _in(self, home: Path, tool: str) -> dict[str, Capture]:
        """The lifecycle proper, with ANDROID_AVD_HOME=``home`` for every call."""
        env = {**os.environ, "ANDROID_AVD_HOME": str(home)}

        def run(name: str, stdin: str | None = None) -> Capture:
            argv = [tool if arg == "{avdmanager}" else arg for arg in _AVD_ARGV[name]]
            raw = _run_host(argv, AVD_TOOL_TIMEOUT, stdin=stdin, env=env)
            return Capture(
                _without_private_home(raw.stdout, home),
                _without_private_home(raw.stderr, home),
                raw.returncode,
            )

        captures: dict[str, Capture] = {}
        try:
            # emulator_create feeds `no` for the custom-hardware-profile prompt.
            # With --device given the prompt is never printed (measured), but
            # the input is part of the command the script runs, so it is here.
            create = run("avdmanager_create", stdin="no\n")
            captures["avdmanager_create"] = create
            if create.returncode != 0:
                raise CaptureError(
                    f"avdmanager create exited {create.returncode}: {create.text.strip()[:300]!r}"
                )
            config_path = home / f"{THROWAWAY_AVD}.avd" / "config.ini"
            try:
                self._config_ini = _without_private_home(
                    config_path.read_text(encoding="utf-8"), home
                )
            except OSError as exc:
                raise CaptureError(
                    f"the AVD was created but its config.ini is unreadable: {exc}"
                ) from exc
        finally:
            # Only this run can have put anything in `home`, so deleting what
            # is there deletes nothing of anyone else's.
            if _avd_present(home, THROWAWAY_AVD):
                captures["avdmanager_delete"] = run("avdmanager_delete")

        captures["avdmanager_delete_missing"] = run("avdmanager_delete_missing")

        # Read back both ways: the files on disk, and what avdmanager itself
        # still lists. Not through _run_host, which refuses empty output -- an
        # empty private home lists nothing, and that is success.
        try:
            listing = subprocess.run(
                [tool, "list", "avd", "-c"],
                capture_output=True,
                text=True,
                timeout=AVD_TOOL_TIMEOUT,
                check=False,
                env=env,
            ).stdout.split()
        except subprocess.TimeoutExpired as exc:
            raise CaptureError("could not list AVDs to confirm the delete") from exc
        if _avd_present(home, THROWAWAY_AVD) or THROWAWAY_AVD in listing:
            raise CaptureError(f"{THROWAWAY_AVD} is still present after `avdmanager delete`")
        return captures


_THROWAWAY_AVD = _ThrowawayAvd()


# --- scroll-into-view corpus ------------------------------------------------
# navigator's --scroll-to-find needs successive states of one real list: the
# target below the fold, the same list after a scroll, and the list at its end.
# The Settings home screen is the case that motivated the feature.
SETTINGS_PACKAGE = "com.android.settings"
DIALER_PACKAGE = "com.google.android.dialer"

# What GestureSimulator.scroll("down") issues: the finger travels from 80% to
# 20% of the screen height at the horizontal centre, over 300ms. These are
# those pixels for this profile's 1080x2424 screen (see wm_size_physical) and
# must be recomputed before recording a differently sized device.
_SCROLL_DOWN = ["shell", "input", "swipe", "540", "1939", "540", "484", "300"]
_SETTLE = ["shell", "sleep", "1"]
# SystemUI's demo-mode broadcast. The status-bar setters that matter are extras
# on this action, not `cmd statusbar` subcommands (see cmd_statusbar_help).
DEMO_ACTION = "com.android.systemui.demo"


def _demo_clock(hhmm: str) -> list[list[str]]:
    """Setup steps that set the demo-mode clock and let SystemUI redraw."""
    return [
        [
            "shell",
            "am",
            "broadcast",
            "-a",
            DEMO_ACTION,
            "-e",
            "command",
            "clock",
            "-e",
            "hhmm",
            hhmm,
        ],
        ["shell", "sleep", "2"],
    ]


_OPEN_SETTINGS = [
    ["shell", "am", "force-stop", SETTINGS_PACKAGE],
    ["shell", "am", "start", "-a", "android.settings.SETTINGS"],
    ["shell", "sleep", "2"],
]


# --- one app, three processes, one logcat window ----------------------------

_LAUNCH_COMPOSE = ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.DefaultActivity"]

# How each of the three processes ends: a Java crash, a force-stop, and still
# running when the window is read. A restarted app's earlier process is what a
# pid filter taken from `pidof` loses (D4), and these are the ways it ends.
_RESTART_ENDINGS: tuple[tuple[str, ...] | None, ...] = (("am", "crash"), ("am", "force-stop"), None)


def _app_restart_window(_scratch: Path, serial: str, extra: dict, *, usec: bool = True) -> Capture:
    """Three processes of the fixture app in one logcat window, and their pids.

    ``usec`` asks for microsecond headers (logcat_app_restart_usec, what
    log_monitor and app_state_capture read); without it the headers are
    logcat's default milliseconds (logcat_app_restart, what the ANR readers
    read). Both record the same oracles.

    Each pid is measured with ``pidof`` while that process is alive and stored
    in the manifest (``pids``), so a test can hold what a parser attributes to
    the package against what the device said at the time -- not against the
    parser's own reading of the same text.

    The window is filtered on the HOST to the lines that name the package or
    come from one of those pids, plus the reader's ``beginning of`` separators:
    the full window carries every other app's free text, which is not ours to
    publish. Every line that remains is verbatim.
    """
    _run(["shell", "am", "force-stop", COMPOSE_PACKAGE], serial)
    _run(["logcat", "-c"], serial)
    pids: list[str] = []
    for ending in _RESTART_ENDINGS:
        launched = _run(_LAUNCH_COMPOSE, serial, timeout=60)
        if launched.returncode != 0:
            raise CaptureError(
                f"could not launch {COMPOSE_PACKAGE} (exit {launched.returncode}). Install "
                f"the fixture app first (cd tests/fixtures/scaffold/compose && gradle "
                f":app:installDebug)"
            )
        time.sleep(2)
        pid = _run(["shell", "pidof", COMPOSE_PACKAGE], serial, timeout=15).text.strip()
        if not pid.isdigit():
            raise CaptureError(f"{COMPOSE_PACKAGE} is not running after launch: pidof {pid!r}")
        pids.append(pid)
        if ending:
            _run(["shell", *ending, COMPOSE_PACKAGE], serial)
            time.sleep(3)
    if len(set(pids)) != len(pids):
        raise CaptureError(f"expected a new process per launch; pidof saw {pids}")

    # Microsecond headers, as log_monitor and app_state_capture ask for them:
    # the lines of ONE multi-line message share their header to the
    # microsecond, while two messages logged in the same millisecond do not --
    # measured: `Force stopping` and `Killing` once shared `.651` at ms
    # precision. The continuation rule in common.logcat needs that resolution.
    usec_args = ["-v", "usec"] if usec else []
    window = _run(["logcat", "-d", "-v", "threadtime", *usec_args], serial, timeout=60)

    def _pid_column(line: str) -> str | None:
        fields = line.split()
        return fields[2] if len(fields) > 2 else None

    kept = [
        line
        for line in window.stdout.splitlines(keepends=True)
        if COMPOSE_PACKAGE in line
        or line.startswith("--------- beginning of")
        or _pid_column(line) in pids
    ]
    silent = [pid for pid in pids if not any(_pid_column(line) == pid for line in kept)]
    if silent:
        raise CaptureError(f"process(es) {silent} logged nothing in the window; re-record")

    # Who may say which pids were the app's. Any app can log under
    # ActivityManager's tag; only system_server writes it with its own pid, so
    # that pid is the oracle the parser's producer check is held against --
    # read from the device in the same run, not from the lines being checked.
    answer = _run(["shell", "pidof", "system_server"], serial, timeout=15).text.strip()
    if not answer.isdigit():
        raise CaptureError(f"pidof system_server named no single pid: {answer!r}")
    # Every launch, not only the first: a capture where any `Start proc` line
    # came from another writer is not the evidence this fixture records.
    for pid in pids:
        starts = [line for line in kept if f"ActivityManager: Start proc {pid}:" in line]
        if not starts or any(_pid_column(line) != answer for line in starts):
            raise CaptureError(
                f"system_server is pid {answer}, but the `Start proc {pid}` line(s) came "
                f"from {sorted({_pid_column(line) for line in starts})}"
            )
    # The uid oracles, read in the same run: the package's uid, and the uid of
    # the process still alive, as `ps` reports it. A pid is the app's only when
    # its uid is the package's -- any app may name a process of its own after
    # another package (`android:process`), and system_server then really logs
    # `Start proc N:<that package>/<the impostor's uid>`.
    listing = _run(
        ["shell", "cmd", "package", "list", "packages", "-U", COMPOSE_PACKAGE], serial, timeout=15
    )
    ps = _run(["shell", "ps", "-o", "PID,UID", "-p", pids[-1]], serial, timeout=15)
    if f"package:{COMPOSE_PACKAGE} uid:" not in listing.text or pids[-1] not in ps.text:
        raise CaptureError(
            f"uid oracles incomplete: listing {listing.text.strip()!r}, ps {ps.text.strip()!r}"
        )
    extra["pids"] = pids
    extra["pid_endings"] = ["am crash", "am force-stop", "running when the window was read"]
    extra["system_server_pid"] = answer
    extra["package_uid_listing"] = {
        "command": f"adb shell cmd package list packages -U {COMPOSE_PACKAGE}",
        "exit_status": listing.returncode,
        "output": _strip_cr(listing.text),
    }
    extra["ps_alive"] = {
        "command": f"adb shell ps -o PID,UID -p {pids[-1]}",
        "exit_status": ps.returncode,
        "output": _strip_cr(ps.text),
    }
    if usec:
        # The same window as logd's records, dumped right after the text: what
        # log_monitor and app_state_capture now read (common.logcat). Filtered
        # the same way, a RECORD at a time: one that names the package or
        # comes from one of its pids is kept whole.
        records = _run_bytes(["logcat", "-d", "-B"], serial, timeout=60)
        _stash_twin(
            "logcat_app_restart_records",
            _keep_records(records, pids, [COMPOSE_PACKAGE]),
            {**extra, "text_twin": "logcat_app_restart_usec", **_device_zone(serial)},
        )
        return Capture(_alias_pair_text("".join(kept)), window.stderr, window.returncode)
    return Capture("".join(kept), window.stderr, window.returncode)


def _app_restart_window_ms(scratch: Path, serial: str, extra: dict) -> Capture:
    """:func:`_app_restart_window` with logcat's default millisecond headers."""
    return _app_restart_window(scratch, serial, extra, usec=False)


def _package_uids_two_users(_scratch: Path, serial: str, extra: dict) -> Capture:
    """`cmd package list packages -U` with the fixture app installed for two users.

    A throwaway secondary user is created, the app installed for it, the
    listing read, and the user removed -- in a `finally`, then read back until
    the listing names one uid again (measured: removal settles within seconds).
    Only for an emulator (`emulator_only`).
    """
    created = _run(["shell", "pm", "create-user", "u5-fixture-tmp"], serial, timeout=60)
    match = re.search(r"created user id (\d+)", created.text)
    if created.returncode != 0 or match is None:
        raise CaptureError(f"pm create-user failed: {created.text.strip()!r}")
    user = match.group(1)
    listing_args = ["shell", "cmd", "package", "list", "packages", "-U", COMPOSE_PACKAGE]
    try:
        installed = _run(
            ["shell", "pm", "install-existing", "--user", user, COMPOSE_PACKAGE], serial, timeout=60
        )
        if installed.returncode != 0:
            raise CaptureError(f"install-existing failed: {installed.text.strip()!r}")
        capture = _run(listing_args, serial, timeout=15)
        if "," not in capture.text:
            raise CaptureError(f"expected one uid per user, got {capture.text.strip()!r}")
    finally:
        _run(["shell", "pm", "remove-user", user], serial, timeout=60)
    for _ in range(15):
        if "," not in _run(listing_args, serial, timeout=15).text:
            break
        time.sleep(1)
    else:
        raise CaptureError(f"user {user} still listed after pm remove-user; check the emulator")
    extra["secondary_user"] = user
    return capture


def _ps_two_owners(_scratch: Path, serial: str, extra: dict) -> Capture:
    """`ps -o PID,UID` for the fixture app's process and system_server's, in one call.

    Two rows with two different uids: the evidence a `pidof` answer is checked
    against, including the case where one of its pids is not the package's.
    """
    _run(["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.DefaultActivity"], serial)
    _run(["shell", "sleep", "2"], serial)
    app = _run(["shell", "pidof", COMPOSE_PACKAGE], serial, timeout=15).text.strip()
    system_server = _run(["shell", "pidof", "system_server"], serial, timeout=15).text.strip()
    if not (app.isdigit() and system_server.isdigit()):
        raise CaptureError(f"pidof answered app {app!r}, system_server {system_server!r}")
    args = ["shell", "ps", "-o", "PID,UID", "-p", f"{app},{system_server}"]
    capture = _run(args, serial, timeout=15)
    if app not in capture.text or system_server not in capture.text:
        raise CaptureError(f"ps did not list both processes: {capture.text.strip()!r}")
    extra["pids"] = {"app": app, "system_server": system_server}
    extra["argv"] = ["adb", "shell", "ps", "-o", "PID,UID", "-p", "<app>,<system_server>"]
    return capture


# --- logd's records, and the device's own text for the same records ----------
#
# log_monitor and app_state_capture read `logcat -B` (common.logcat): records
# whole, with the pid and uid logd took from the writer's socket credentials
# (the tid and time are the writer's own header's). Each binary
# fixture is one half of a PAIR recorded in one run: the device's threadtime
# text, dumped first, and the records, dumped right after. The text is the
# oracle the reader's printing is held against, and the pair lets one test run
# red over text (what v0.8.0 read) and green over records.

# struct logger_entry as `logcat -B` writes it (measured, API 35): u16 payload
# length, u16 header size (28), i32 pid, u32 tid, sec, nsec, log id, uid.
_LOGGER_ENTRY = struct.Struct("<HHiIIIII")

# A second half, captured inside its first half's run and written by the
# fixture listed after it (`_twin`).
_TWINS: dict[str, tuple[bytes, dict]] = {}


def _split_records(data: bytes) -> list[tuple[int, bytes]]:
    """``(pid, raw record)`` for every record of a ``logcat -B`` dump.

    Raises:
        CaptureError: The dump is not whole records.
    """
    records: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < _LOGGER_ENTRY.size:
            raise CaptureError(f"`logcat -B` ended inside a record header at byte {offset}")
        length, header, pid, *_ = _LOGGER_ENTRY.unpack_from(data, offset)
        if header < _LOGGER_ENTRY.size or offset + header + length > len(data):
            raise CaptureError(f"`logcat -B` is not whole records at byte {offset}")
        records.append((pid, data[offset : offset + header + length]))
        offset += header + length
    return records


def _record_payload(raw: bytes) -> bytes:
    """``<priority><tag>NUL<message>NUL`` of one raw record."""
    return raw[struct.unpack_from("<H", raw, 2)[0] :]


# Aliases for the record pairs, shared by both halves of a pair so they name
# the same alias for the same package (record()'s own mapping never sees the
# binary half). ``com.example.privateN``, so they cannot be read as the
# ``com.example.appN`` aliases record() gives other fixtures in the same run.
_PAIR_ALIASES: dict[str, str] = {}


def _alias_pair_text(text: str) -> str:
    """``text`` with non-AOSP package names aliased the way its binary half is."""

    def alias(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith(_PUBLIC_PACKAGE_PREFIXES):
            return token
        return _PAIR_ALIASES.setdefault(token, f"com.example.private{len(_PAIR_ALIASES) + 1}")

    return _PACKAGE_RE.sub(alias, text)


def _aliased_record(raw: bytes) -> bytes:
    """``raw`` with its tag's and message's private package names aliased.

    The payload is rebuilt and its length field rewritten, so the record is
    framed exactly as logd frames one holding that text: the substitution is
    of text, the way the text halves' aliasing is, never of the framing. A
    record naming no private package is returned byte for byte.
    """
    header = struct.unpack_from("<H", raw, 2)[0]
    payload = raw[header:]
    tag, _, rest = payload[1:].partition(b"\0")
    message, _, trailer = rest.partition(b"\0")
    try:
        fields = [part.decode("utf-8") for part in (tag, message)]
    except UnicodeDecodeError as exc:
        raise CaptureError(f"a kept record is not UTF-8 text: {raw[:80]!r}") from exc
    aliased = [_alias_pair_text(part) for part in fields]
    if aliased == fields:
        return raw
    body = payload[:1] + aliased[0].encode() + b"\0" + aliased[1].encode() + b"\0" + trailer
    return struct.pack("<H", len(body)) + raw[2:header] + body


def _keep_records(data: bytes, pids: list[str], needles: list[str]) -> bytes:
    """The records of ``data`` from ``pids`` or naming one of ``needles``, whole.

    The binary counterpart of the line filter the text halves use: the full
    window carries every other app's free text, which is not ours to publish.
    A kept record that names a package outside the public prefixes has it
    aliased (:func:`_aliased_record`), as the text half has (measured: on a
    developer's emulator system_server's AppsFilter records name their apps).
    """
    kept = [
        _aliased_record(raw)
        for pid, raw in _split_records(data)
        if str(pid) in pids or any(n.encode() in _record_payload(raw) for n in needles)
    ]
    if not kept:
        raise CaptureError("no record of the window is the fixture's")
    return b"".join(kept)


def _device_zone(serial: str) -> dict:
    """The device's time zone, which its threadtime text is printed in.

    A test printing a binary half's records to compare with its text half has
    to use this zone, whatever zone the machine running the test is in.
    """
    zone = _run(["shell", "getprop", "persist.sys.timezone"], serial, timeout=15).text.strip()
    if not zone:
        raise CaptureError("the device names no time zone (persist.sys.timezone)")
    return {"timezone": zone}


def _stash_twin(name: str, data: bytes, provenance: dict) -> None:
    _TWINS[name] = (data, provenance)


def _twin(name: str, first_half: str) -> Callable[[str, dict], bytes]:
    """A ``capture_bytes`` writing what ``first_half``'s run captured for ``name``."""

    def capture(_serial: str, extra: dict) -> bytes:
        if name not in _TWINS:
            raise CaptureError(
                f"{name} is captured in the same run as {first_half}; record them together: "
                f"--only {first_half} {name}"
            )
        data, provenance = _TWINS.pop(name)
        extra.update(provenance)
        return data

    return capture


def _logcat_text(args: list[str], serial: str) -> str:
    """A logcat dump as text, decoded without newline translation.

    ``_run``'s text mode would turn a raw carriage return inside a message
    into a line break, which logcat never does.
    """
    return _run_bytes(["logcat", *args], serial, timeout=60).decode("utf-8", errors="replace")


def _format_uid(uid: int) -> str:
    """UserHandle.formatUid for an app uid: 10205 -> ``u0a205``."""
    return f"u{uid // 100_000}a{uid % 100_000 - 10_000}"


def _pid_column(line: str) -> str | None:
    """The PID column of a threadtime line."""
    fields = line.split()
    return fields[2] if len(fields) > 2 else None


def _tag_and_message(raw: bytes) -> tuple[str, str]:
    """The tag and message of one raw record, as text."""
    payload = _record_payload(raw)
    tag, _, rest = payload[1:].partition(b"\0")
    return tag.decode("utf-8", errors="replace"), rest.split(b"\0", 1)[0].decode(
        "utf-8", errors="replace"
    )


# A record the recorder writes itself, immediately before it provokes an ANR,
# so a window can start somewhere every API level logs. ActivityManager's
# `Broadcasting: Intent { act=... }` record, the original anchor, is not logged
# on API 33 (measured: the ANR happens, the record does not).
_WINDOW_TAG = "aes-recorder"
_WINDOW_MARK = "anr-window"
_LOG_WINDOW_MARK = ["shell", "log", "-t", _WINDOW_TAG, _WINDOW_MARK]


def _record_time(raw: bytes) -> tuple[int, int]:
    """``(sec, nsec)`` from one raw record's header."""
    return struct.unpack_from("<II", raw, 12)


def _anr_window(
    records: list[tuple[int, bytes]], broadcast: str, extra: dict
) -> list[tuple[int, bytes]]:
    """The records from the provoking broadcast on.

    The LAST ActivityManager record starting ``broadcast`` when there is one
    (API 35: what the emulator-api35 fixtures were cut at). Otherwise every
    record at or after the time of the last window mark the recorder logged
    (:data:`_LOG_WINDOW_MARK`) just before provoking. ``extra["window_anchor"]``
    says which.

    Raises:
        CaptureError: The buffer holds neither.
    """
    starts = [
        index
        for index, (_pid, raw) in enumerate(records)
        if _tag_and_message(raw)[0] == "ActivityManager"
        and _tag_and_message(raw)[1].startswith(broadcast)
    ]
    if starts:
        extra["window_anchor"] = "ActivityManager `Broadcasting: Intent` record"
        return records[starts[-1] :]
    marks = [raw for _pid, raw in records if _tag_and_message(raw) == (_WINDOW_TAG, _WINDOW_MARK)]
    if not marks:
        raise CaptureError(
            "no provoking broadcast and no window mark in the buffer: the ANR was not provoked"
        )
    since = _record_time(marks[-1])
    extra["window_anchor"] = f"the recorder's `{_WINDOW_TAG}: {_WINDOW_MARK}` record"
    return [(pid, raw) for pid, raw in records if _record_time(raw) >= since]


# How long the input ANR's key event may block before its adb call is killed.
# Where `input` has no --async (API 33) the injection waits on the stalled
# window -- exactly what timed the step out at 30 s.
_KEY_EVENT_BOUND_S = 5


def _send_key_bounded(serial: str, extra: dict) -> None:
    """``input keyevent KEYCODE_SHIFT_LEFT`` to the blocked window, never blocking the run.

    With ``--async`` where the device's ``input`` usage lists it (API 35);
    without it elsewhere, its adb call killed after :data:`_KEY_EVENT_BOUND_S`
    seconds. ``extra["key_event"]`` records the argv and whether it was killed.
    """
    usage = _run(["shell", "input"], serial, timeout=15)
    asynchronous = "--async" in usage.stdout + usage.stderr
    argv = [
        "shell",
        "input",
        "keyevent",
        *(["--async"] if asynchronous else []),
        "KEYCODE_SHIFT_LEFT",
    ]
    child = subprocess.Popen(
        _adb_argv(serial, argv),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        child.wait(timeout=_KEY_EVENT_BOUND_S)
        killed = False
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=DEFAULT_TIMEOUT)
        killed = True
    extra["key_event"] = {
        "argv": ["adb", *argv],
        "input_lists_async": asynchronous,
        "bound_s": _KEY_EVENT_BOUND_S,
        "killed": killed,
    }


def _key_then_wait(serial: str, extra: dict) -> None:
    """The input ANR's provocation after its setup: a bounded key event, then 15 s."""
    _send_key_bounded(serial, extra)
    time.sleep(15)


def _anr_records(
    *,
    requires: tuple[str, ...],
    expects: tuple[str, ...] = (),
    before: Callable[[str, dict], None] | None = None,
) -> Callable[[str, dict], bytes]:
    """One provoked ANR as logd's records -- what the ANR readers read since PR #34.

    The ``-B`` counterpart of the ``*_stream`` fixtures: ActivityManager's and
    WindowManager's records naming the fixture package, from the provoking
    broadcast on (:func:`_anr_window`; the buffer is not cleared, as there).
    ``requires`` is text the kept records' messages must hold on every API
    level; ``expects`` is the rest of the shape measured on API 35 -- among it
    ``"\\nPID: "``, which is only there if ActivityManager's report is ONE
    record of many lines. A missing expectation does not refuse the capture: it
    is listed under the manifest's ``expected_absent``, so a level that differs
    is recorded as it is and says how. Every kept record must have been written
    by the pid ``pidof system_server`` names in the same run
    (``system_server_pid``). ``before`` runs first, given the serial and the
    manifest extras (the input ANR's key event).
    """
    marker = f"Broadcasting: Intent {{ act={COMPOSE_PACKAGE}.PROVOKE_ANR"

    def capture(serial: str, extra: dict) -> bytes:
        if before is not None:
            before(serial, extra)
        system_server = _run(["shell", "pidof", "system_server"], serial, timeout=15).text.strip()
        if not system_server.isdigit():
            raise CaptureError(f"pidof system_server named no single pid: {system_server!r}")
        records = _split_records(_run_bytes(["logcat", "-d", "-B"], serial, timeout=60))
        kept = [
            (pid, raw)
            for pid, raw in _anr_window(records, marker, extra)
            if _tag_and_message(raw)[0] in ("ActivityManager", "WindowManager")
            and COMPOSE_PACKAGE in _tag_and_message(raw)[1]
        ]
        messages = "\n".join(_tag_and_message(raw)[1] for _pid, raw in kept)
        for needle in requires:
            if needle not in messages:
                raise CaptureError(f"the provoked ANR's records do not hold {needle!r}")
        writers = {str(pid) for pid, _raw in kept}
        if writers != {system_server}:
            raise CaptureError(f"system_server is {system_server}; the records came from {writers}")
        extra["system_server_pid"] = system_server
        extra["expected_absent"] = [needle for needle in expects if needle not in messages]
        extra.update(_device_zone(serial))
        return b"".join(_aliased_record(raw) for _pid, raw in kept)

    return capture


# The process the fixture app's ForeignProcessAnrReceiver runs in: named after
# another package (android:process), under the fixture app's own uid.
_FOREIGN_PROCESS = "com.example.victim"
_FOREIGN_ACTION = f"{COMPOSE_PACKAGE}.PROVOKE_FOREIGN_PROCESS_ANR"


def _poll(check: Callable[[], str | None], seconds: int) -> str | None:
    """``check()`` once a second until it answers, for at most ``seconds``."""
    for _ in range(seconds):
        answer = check()
        if answer:
            return answer
        time.sleep(1)
    return None


def _foreign_process_anr(serial: str, extra: dict) -> bytes:
    """An ANR ActivityManager names after ANOTHER package: the app's own, in a process so named.

    The fixture app declares ForeignProcessAnrReceiver with
    ``android:process="com.example.victim"``, a global process name any app may
    choose, which runs under the app's own uid. Its block is AnrReceiver's, so
    ActivityManager reports ``ANR in com.example.victim`` -- the process name,
    ``mApp.processName``, not a package. While the process is alive (between
    system_server's ``Start proc`` and the ANR, which kills a background
    process at once: ``Killing … bg anr``), ``ps -o PID,UID -p <pid>`` is asked,
    in the recorded ps_pid_uid syntax; both packages' uid listings are asked
    too (the claimed one is not installed). Kept: ActivityManager's and
    WindowManager's records naming the process or the provoking action, from
    the provoking broadcast on, all written by system_server.
    """
    # --async: `am broadcast` otherwise waits for the broadcast to finish --
    # here, for the ANR that kills the process -- so nothing could be asked of
    # the process while it lived (measured: every question came too late).
    _run(_LOG_WINDOW_MARK, serial, timeout=15)
    _run(
        [
            "shell",
            "am",
            "broadcast",
            "--async",
            "-f",
            "0x10000000",
            "-n",
            f"{COMPOSE_PACKAGE}/.ForeignProcessAnrReceiver",
            "-a",
            _FOREIGN_ACTION,
        ],
        serial,
        timeout=30,
    )
    started = re.compile(rf"Start proc (\d+):{re.escape(_FOREIGN_PROCESS)}/(\S+) ")

    def started_pid() -> str | None:
        text = _run(["logcat", "-d", "-s", "ActivityManager:I"], serial, timeout=30).text
        found = started.findall(text)
        return found[-1][0] if found else None

    pid = _poll(started_pid, 20)
    if pid is None:
        raise CaptureError(f"no `Start proc <pid>:{_FOREIGN_PROCESS}` within 20 s")
    # Asked while the process lives: its Killing record must come after the
    # device's clock read here.
    ps = _run(["shell", "ps", "-o", "PID,UID", "-p", pid], serial, timeout=15)
    asked_at = _run(["shell", "date", "+%s"], serial, timeout=15).text.strip()
    listings = {
        package: _run(
            ["shell", "cmd", "package", "list", "packages", "-U", package], serial, timeout=15
        )
        for package in (COMPOSE_PACKAGE, _FOREIGN_PROCESS)
    }
    owner = re.search(
        rf"package:{re.escape(COMPOSE_PACKAGE)} uid:(\d+)", listings[COMPOSE_PACKAGE].text
    )
    row = re.search(rf"^\s*{pid}\s+(\d+)\s*$", ps.text, re.MULTILINE)
    if not (asked_at.isdigit() and owner and row and row.group(1) == owner.group(1)):
        raise CaptureError(
            f"pid {pid} was not shown alive under {COMPOSE_PACKAGE}'s uid at {asked_at!r}: "
            f"ps {ps.text.strip()!r}, listing {listings[COMPOSE_PACKAGE].text.strip()!r}"
        )

    def reported() -> str | None:
        text = _run(["logcat", "-d", "-s", "ActivityManager:E"], serial, timeout=30).text
        return "yes" if f"ANR in {_FOREIGN_PROCESS}" in text else None

    if _poll(reported, 30) is None:
        raise CaptureError(f"ActivityManager reported no `ANR in {_FOREIGN_PROCESS}` within 30 s")
    time.sleep(2)

    system_server = _run(["shell", "pidof", "system_server"], serial, timeout=15).text.strip()
    if not system_server.isdigit():
        raise CaptureError(f"pidof system_server named no single pid: {system_server!r}")
    records = _split_records(_run_bytes(["logcat", "-d", "-B"], serial, timeout=60))
    marker = f"Broadcasting: Intent {{ act={_FOREIGN_ACTION}"
    kept = [
        (writer, raw)
        for writer, raw in _anr_window(records, marker, extra)
        if _tag_and_message(raw)[0] in ("ActivityManager", "WindowManager")
        and (
            _FOREIGN_PROCESS in _tag_and_message(raw)[1]
            or _FOREIGN_ACTION in _tag_and_message(raw)[1]
        )
    ]
    messages = "\n".join(_tag_and_message(raw)[1] for _writer, raw in kept)
    for needle in (f"Start proc {pid}:{_FOREIGN_PROCESS}/", f"ANR in {_FOREIGN_PROCESS}"):
        if needle not in messages:
            raise CaptureError(f"the provoked ANR's records do not hold {needle!r}")
    # The rest of the shape measured on API 35; listed, not refused, when absent.
    expects = (f"ANR in {_FOREIGN_PROCESS}\nPID: {pid}\n", f"Killing {pid}:{_FOREIGN_PROCESS}/")
    extra["expected_absent"] = [needle for needle in expects if needle not in messages]
    writers = {str(writer) for writer, _raw in kept}
    if writers != {system_server}:
        raise CaptureError(f"system_server is {system_server}; the records came from {writers}")
    killed = [
        struct.unpack_from("<I", raw, 12)[0]
        for _writer, raw in kept
        if _tag_and_message(raw)[1].startswith(f"Killing {pid}:")
    ]
    # ps showing the pid under the app's uid is itself proof it was alive; the
    # Killing record, where there is one, must also come after it was asked.
    if killed and int(asked_at) >= killed[-1]:
        raise CaptureError(f"ps was asked at {asked_at}, not before pid {pid} was killed {killed}")
    extra["system_server_pid"] = system_server
    extra["stalled_pid"] = pid
    extra["ps_alive"] = {
        "command": f"adb shell ps -o PID,UID -p {pid}",
        "exit_status": ps.returncode,
        "output": _strip_cr(ps.text),
        "device_epoch": int(asked_at),
    }
    extra["package_uid_listing"] = {
        "command": f"adb shell cmd package list packages -U {COMPOSE_PACKAGE}",
        "exit_status": listings[COMPOSE_PACKAGE].returncode,
        "output": _strip_cr(listings[COMPOSE_PACKAGE].text),
    }
    extra["claimed_package_uid_listing"] = {
        "command": f"adb shell cmd package list packages -U {_FOREIGN_PROCESS}",
        "exit_status": listings[_FOREIGN_PROCESS].returncode,
        "output": _strip_cr(listings[_FOREIGN_PROCESS].text),
    }
    extra.update(_device_zone(serial))
    return b"".join(_aliased_record(raw) for _writer, raw in kept)


# The marker the injection's records carry, so they can be kept and found.
_INJECTION_MARKER = "w3l-tag-injection"


def _tag_injection(_scratch: Path, serial: str, extra: dict) -> Capture:
    """An app-controlled TAG that forges system_server's line, and the records beneath it.

    The fixture app is launched (its own `Start proc`; its pid from `pidof`),
    then the shell -- standing in for any app: a tag is the writer's to choose
    -- logs one record whose TAG is ``x:``, a newline, and a threadtime header
    claiming system_server's pid and the tag ActivityManager, and whose
    message is system_server's `Start proc <victim>:<package>/<package uid>`
    sentence. The victim is the logging shell itself: ``exec log`` keeps its
    pid ($$) and logs one line from it afterwards. logcat prints a tag
    verbatim, so in the text the record is two lines, and the second is, to
    any text parser, system_server naming the victim as the app's process.
    """
    _run(["shell", "am", "force-stop", COMPOSE_PACKAGE], serial)
    _run(["logcat", "-c"], serial)
    launched = _run(_LAUNCH_COMPOSE, serial, timeout=60)
    if launched.returncode != 0:
        raise CaptureError(f"could not launch {COMPOSE_PACKAGE} (exit {launched.returncode})")
    time.sleep(2)
    alive = _run(["shell", "pidof", COMPOSE_PACKAGE], serial, timeout=15).text.strip()
    system_server = _run(["shell", "pidof", "system_server"], serial, timeout=15).text.strip()
    listing = _run(
        ["shell", "cmd", "package", "list", "packages", "-U", COMPOSE_PACKAGE], serial, timeout=15
    )
    uid = re.search(rf"package:{re.escape(COMPOSE_PACKAGE)} uid:(\d+)", listing.text)
    if not (alive.isdigit() and system_server.isdigit() and uid):
        raise CaptureError(f"pidof {alive!r}, system_server {system_server!r}, {listing.text!r}")
    # One device-shell command line; the newline inside the quoted tag is a
    # real one, and the header's time is the device's own clock.
    forged_tag = (
        'x:\n$(date +"%m-%d %H:%M:%S").000000 '
        f"{system_server:>5} {system_server:>5} I ActivityManager"
    )
    sentence = (
        f"Start proc $$:{COMPOSE_PACKAGE}/{_format_uid(int(uid.group(1)))} for {_INJECTION_MARKER}"
    )
    script = (
        f'log -p e -t "{forged_tag}" "{sentence}"; '
        f'exec log -p e -t W3L-victim "{_INJECTION_MARKER} victim line"'
    )
    injected = _run(["shell", script], serial, timeout=15)
    if injected.returncode != 0:
        raise CaptureError(f"the injection did not log: {injected.text.strip()!r}")
    time.sleep(1)

    text = _logcat_text(["-d", "-v", "threadtime", "-v", "usec"], serial)
    data = _run_bytes(["logcat", "-d", "-B"], serial, timeout=60)
    marked = [(pid, raw) for pid, raw in _split_records(data) if _INJECTION_MARKER.encode() in raw]
    forger = [str(pid) for pid, raw in marked if b"Start proc" in raw]
    victim = [str(pid) for pid, raw in marked if b"victim line" in raw]
    if len(forger) != 1 or len(victim) != 1 or forger == victim:
        raise CaptureError(f"expected one forging and one victim record, got {marked!r}")
    pids = [alive, forger[0], victim[0]]
    kept = [
        line
        for line in text.splitlines(keepends=True)
        if COMPOSE_PACKAGE in line
        or line.startswith("--------- beginning of")
        or _pid_column(line) in pids
    ]
    forged = [line for line in kept if f"Start proc {victim[0]}:" in line]
    if len(forged) != 1 or _pid_column(forged[0]) != system_server:
        raise CaptureError(f"the text does not show the forged line: {forged!r}")
    extra["pids"] = [alive]
    extra["system_server_pid"] = system_server
    extra["package_uid_listing"] = {
        "command": f"adb shell cmd package list packages -U {COMPOSE_PACKAGE}",
        "exit_status": listing.returncode,
        "output": _strip_cr(listing.text),
    }
    extra["injection"] = {
        "forger_pid": forger[0],
        "victim_pid": victim[0],
        "command": f"adb shell {script!r}",
    }
    _stash_twin(
        "logcat_tag_injection_records",
        _keep_records(data, pids, [COMPOSE_PACKAGE]),
        {**extra, "text_twin": "logcat_tag_injection", **_device_zone(serial)},
    )
    return Capture(_alias_pair_text("".join(kept)), "", 0)


def _crash_tail(_scratch: Path, serial: str, extra: dict) -> Capture:
    """`logcat -T 1` against a buffer whose last record is a many-line crash.

    The crash buffer is cleared and the fixture app crashed twice (`am
    crash`), so it holds exactly two records, each a FATAL EXCEPTION of many
    lines. Recorded in one run: the whole buffer as text and as records, and
    `-T 1` as text and as records. logd tails RECORDS, so `-T 1` is the whole
    last crash, every line -- not its last line.
    """
    _run(["logcat", "-b", "crash", "-c"], serial)
    pids = []
    for _ in range(2):
        launched = _run(_LAUNCH_COMPOSE, serial, timeout=60)
        if launched.returncode != 0:
            raise CaptureError(f"could not launch {COMPOSE_PACKAGE} (exit {launched.returncode})")
        time.sleep(2)
        pids.append(_run(["shell", "pidof", COMPOSE_PACKAGE], serial, timeout=15).text.strip())
        _run(["shell", "am", "crash", COMPOSE_PACKAGE], serial)
        # Gone before the next launch, or that launch finds the dying process.
        for _ in range(15):
            time.sleep(1)
            if not _run(["shell", "pidof", COMPOSE_PACKAGE], serial, timeout=15).text.strip():
                break
        else:
            raise CaptureError(f"{COMPOSE_PACKAGE} was still running 15 s after `am crash`")
    usec = ["-v", "threadtime", "-v", "usec"]
    whole = _logcat_text(["-b", "crash", "-d", *usec], serial)
    whole_records = _run_bytes(["logcat", "-b", "crash", "-d", "-B"], serial, timeout=60)
    tail = _logcat_text(["-b", "crash", "-d", *usec, "-T", "1"], serial)
    tail_records = _run_bytes(["logcat", "-b", "crash", "-d", "-B", "-T", "1"], serial, timeout=60)
    records = _split_records(whole_records)
    if [str(pid) for pid, _ in records] != pids:
        raise CaptureError(f"the crash buffer holds {[p for p, _ in records]}, not {pids}")
    if tail_records != records[-1][1]:
        raise CaptureError("`-T 1` as records is not the buffer's last record")
    extra["pids"] = pids
    provenance = {**extra, **_device_zone(serial)}
    _stash_twin(
        "logcat_crash_buffer", whole.encode(), {**provenance, "first_half": "logcat_crash_tail"}
    )
    _stash_twin(
        "logcat_crash_buffer_records",
        _keep_records(whole_records, pids, []),
        {**provenance, "text_twin": "logcat_crash_buffer"},
    )
    _stash_twin(
        "logcat_crash_tail_records",
        _keep_records(tail_records, pids, []),
        {**provenance, "text_twin": "logcat_crash_tail"},
    )
    return Capture(tail, "", 0)


# The one device-shaped adb argv in this file without -s: adb_more_than_one_device
# records adb refusing it.
_NO_TARGET_ARGV = ["adb", "shell", "getprop", "ro.build.version.sdk"]


def _more_than_one_device(_scratch: Path, _serial: str, _extra: dict) -> Capture:
    """``adb shell`` with no ``-s`` and no $ANDROID_SERIAL, while two devices are attached.

    It can reach no device: the host's list is read first and fewer than two
    rows refuse before it runs, and ANDROID_SERIAL is removed from its
    environment (with it set, adb would simply use it). adb then has nothing to
    choose by and refuses -- which is the output recorded.
    """
    rows = _device_rows(_run_host_adb(["devices"], timeout=15).stdout)
    if len(rows) < 2:
        raise CaptureError(f"adb lists {len(rows)} device(s); this needs two or more attached")
    env = {key: value for key, value in os.environ.items() if key != "ANDROID_SERIAL"}
    try:
        result = subprocess.run(
            _NO_TARGET_ARGV,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"timed out after {DEFAULT_TIMEOUT}s") from exc
    return Capture(result.stdout, result.stderr, result.returncode)


def _waiting_for_device(_scratch: Path, _serial: str, _extra: dict) -> Capture:
    """What a streaming `adb logcat` says while its device is not there: it waits.

    Host adb, aimed at a serial that is not attached through ANDROID_SERIAL --
    how a caller that passed no --serial reaches one -- and stopped after a
    few seconds, since it does not stop by itself. It prints a banner on
    stderr and nothing on stdout. No device is touched.
    """
    env = {**os.environ, "ANDROID_SERIAL": "no-such-serial-xyz"}
    try:
        child = subprocess.Popen(
            ["adb", "logcat", "-B", "-T", "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise CaptureError("adb is not installed on this host") from exc
    try:
        out, err = child.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        child.terminate()
        out, err = child.communicate(timeout=10)
    else:
        raise CaptureError(f"adb logcat ended by itself (exit {child.returncode}): {err!r}")
    if out or not err.strip():
        raise CaptureError(f"expected a stderr banner and no stdout, got {out!r} / {err!r}")
    return Capture("", err.decode("utf-8", errors="replace"), child.returncode)


# How long `adb logcat` is left running before the recorder ends it: it
# never ends on its own; this only has to outlast its first answer.
_TAIL_ZERO_SECONDS = 3


def _logcat_tail_zero(_scratch: Path, serial: str, extra: dict) -> Capture:
    """`logcat -T 0`: can a live read start with NO replayed entry? (It cannot.)

    Asked with the session worker's own format terms, millisecond and
    microsecond. The file holds what logcat says on stderr; the entries it
    then prints are other processes' text and are not kept -- only the first
    one's timestamp, beside the device's clock read just before the call, to
    show that the entry it starts on was already in the buffer.
    """
    stamp = _run(["shell", "date '+%m-%d %H:%M:%S'"], serial, timeout=15).text.strip()
    measured: dict[str, dict] = {}
    capture = None
    for name, terms in (("threadtime", []), ("threadtime_usec", ["-v", "usec"])):
        args = ["logcat", "-v", "threadtime", *terms, "-T", "0"]
        child = subprocess.Popen(
            _adb_argv(serial, args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        try:
            stdout, stderr = child.communicate(timeout=_TAIL_ZERO_SECONDS)
        except subprocess.TimeoutExpired:
            child.terminate()
            stdout, stderr = child.communicate(timeout=DEFAULT_TIMEOUT)
        entries = [line for line in stdout.splitlines() if re.match(r"\d{2}-\d{2} ", line)]
        if "-T 0 invalid" not in stderr or not entries:
            raise CaptureError(
                f"`logcat -T 0` no longer replays an entry (stderr {stderr.strip()!r}); "
                f"anr_watcher's -T 1 rests on it"
            )
        first = entries[0][:14]
        measured[name] = {
            "argv": ["adb", "-s", "<serial>", *args],
            "stderr": stderr,
            "first_entry_timestamp": first,
            "first_entry_predates_the_call": first <= stamp,
        }
        capture = Capture("", stderr, child.returncode)
    extra["device_clock_before_the_calls"] = stamp
    extra["measured"] = measured
    assert capture is not None
    return capture


# --- v0.9.0 slice 1: what a live `logcat -B` has delivered when it is stopped --
#
# A live read is stopped by its reader: log_monitor's --duration terminates
# `adb logcat -B -T 1`, and so do the ANR readers. Whatever logcat had written
# but not yet flushed is lost with it. Each scenario below records, in one run:
#
#   <name>_records         what `logcat -B -T 1` delivered before the recorder
#                          stopped it (complete records; see "kept" below)
#   <name>_open_records    `logcat -d -B -T 1` just before the stream opened: the
#                          newest record the device held; the window runs after it
#   <name>_window_records  `logcat -d -B -t <the open record's epoch - 1 ns>` once
#                          the stream is gone: the same window read back in dump
#                          mode, which exits by itself and so writes everything it
#                          read -- starting with the open record itself (manifest
#                          `window.open_record_at`), which is how a reader proves
#                          the buffer still holds the window's beginning
#
# The newest record at the stop is asked too, before the stream is terminated,
# so the manifest can say how much of the window the stream had delivered by
# then (`window.records_through_close`, `of_them_delivered_by_the_stream`).
#
# Kept: records a uid below 10000 wrote (system daemons, and the shell, which
# writes the recorder's marks), each with private package names aliased; an
# app's free text is not ours to publish. What the stream delivered and when
# -- every chunk, timed, before that filter -- is measured on the whole stream
# and kept in the manifest (`stream`, `window`).

_STREAM_ARGV = ["logcat", "-B", "-T", "1"]
_NEWEST_ARGV = ["logcat", "-d", "-B", "-T", "1"]
# How long each scenario's stream runs before the recorder stops it.
_STREAM_SECONDS = 5
# Android's first app uid (AID_APP_START): below it, a system daemon or the shell.
_FIRST_APP_UID = 10_000
_MAX_NEWEST_TRIES = 20


def _record_uid(raw: bytes) -> int:
    """The uid logd stamped on one raw record (``struct logger_entry``'s last field)."""
    return struct.unpack_from("<I", raw, 24)[0]


def _publishable(raw: bytes) -> bool:
    """Whether a record's writer was a system daemon or the shell, not an app."""
    return _record_uid(raw) < _FIRST_APP_UID


def _epoch_before(raw: bytes) -> str:
    """One nanosecond before a record's time, as ``-t`` reads an epoch."""
    sec, nsec = _record_time(raw)
    return f"{sec}.{nsec - 1:09d}" if nsec else f"{sec - 1}.999999999"


def _epoch(raw: bytes) -> str:
    """A record's time as logcat's ``-t`` reads an epoch: always with a fraction.

    ``-t 1792912778`` would be a COUNT of records (logcat --help: "count is
    pure numerical"); the nine-digit fraction keeps it a time.
    """
    sec, nsec = _record_time(raw)
    return f"{sec}.{nsec:09d}"


def _complete_records(data: bytes) -> tuple[list[bytes], bytes]:
    """The whole records of a stopped stream, and the bytes of the one it was cut inside."""
    records: list[bytes] = []
    offset = 0
    while len(data) - offset >= _LOGGER_ENTRY.size:
        length, header = struct.unpack_from("<HH", data, offset)
        if header < _LOGGER_ENTRY.size:
            raise CaptureError(f"the stream is not logd's records at byte {offset}")
        if offset + header + length > len(data):
            break
        records.append(data[offset : offset + header + length])
        offset += header + length
    return records, data[offset:]


def _ours_partial(trailing: bytes) -> bool:
    """Whether a cut record's visible bytes are the recorder's own mark."""
    if len(trailing) < _LOGGER_ENTRY.size + 1 + len(_WINDOW_TAG) + 1:
        return False
    header = struct.unpack_from("<H", trailing, 2)[0]
    tag = trailing[header + 1 : header + 1 + len(_WINDOW_TAG) + 1]
    return _publishable(trailing) and tag == _WINDOW_TAG.encode() + b"\0"


def _newest_record(serial: str, *, mark: str | None) -> bytes:
    """``logcat -d -B -T 1``: the newest record, retried until it can be kept.

    With ``mark``, the recorder logs ``<mark>-<try>`` first and takes the
    answer only when it is that mark; without, only when a uid below 10000
    wrote it. Another process logging in between moves the newest record on,
    so the question is put again.
    """
    for attempt in range(_MAX_NEWEST_TRIES):
        expected = f"{mark}-{attempt}" if mark is not None else None
        if expected is not None:
            _run(["shell", "log", "-t", _WINDOW_TAG, expected], serial, timeout=15)
        records = _split_records(_run_bytes(_NEWEST_ARGV, serial, timeout=30))
        if len(records) != 1:
            raise CaptureError(f"`logcat -d -B -T 1` returned {len(records)} records, not one")
        raw = records[0][1]
        if expected is None and _publishable(raw):
            return raw
        if expected is not None and _tag_and_message(raw) == (_WINDOW_TAG, expected):
            return raw
        time.sleep(0.3)
    raise CaptureError(f"no keepable newest record in {_MAX_NEWEST_TRIES} tries")


class _StreamTap:
    """A live ``adb logcat`` whose stdout is read on a thread, every chunk timed."""

    def __init__(self, serial: str, args: list[str]) -> None:
        self.started = time.monotonic()
        self.child = subprocess.Popen(
            _adb_argv(serial, args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        self.chunks: list[tuple[float, bytes]] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.child.stdout is not None
        while chunk := self.child.stdout.read1(65536):
            self.chunks.append((time.monotonic() - self.started, chunk))

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def stop(self) -> int:
        """Terminate the stream as log_monitor's deadline does; its exit status."""
        self.child.terminate()
        try:
            self.child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait(timeout=DEFAULT_TIMEOUT)
        self._reader.join(timeout=10)
        if self._reader.is_alive():
            raise CaptureError("the stream's reader did not finish after the stream was stopped")
        return self.child.returncode


def _since_is_strict(serial: str, raw: bytes) -> dict:
    """Does ``-t <epoch>`` include a record stamped exactly then? And one ns earlier?"""
    answers = {}
    for name, since in (("at_its_epoch", _epoch(raw)), ("one_ns_earlier", _epoch_before(raw))):
        data = _run_bytes(["logcat", "-d", "-B", "-t", since], serial, timeout=60, allow_empty=True)
        answers[name] = {
            "since": since,
            "includes_the_record": raw in [record for _pid, record in _split_records(data)],
        }
    return answers


def _stream_scenario(
    name: str, *, marks: int = 0, padding: int = 0
) -> Callable[[str, dict], bytes]:
    """``<name>_records``' ``capture_bytes``: the stream's records; open and window as twins.

    ``marks`` is how many marks the recorder logs into the window, half a
    second after the stream opens -- each ``stream-mark-<run>-<n>`` plus
    ``padding`` bytes, so a few can fill logcat's 4 KiB stdio buffer. With
    none, the recorder writes nothing to the device and only reads (the
    scenario recorded on a device that is not ours to write).
    """

    def capture(serial: str, extra: dict) -> bytes:
        run = os.urandom(4).hex()
        ours = marks > 0
        opened = _newest_record(serial, mark=f"stream-open-{run}" if ours else None)
        tap = _StreamTap(serial, _STREAM_ARGV)
        written: list[str] = []
        try:
            if ours:
                time.sleep(0.5)
                for index in range(marks):
                    text = f"stream-mark-{run}-{index}" + (f" {'x' * padding}" if padding else "")
                    _run(["shell", "log", "-t", _WINDOW_TAG, text], serial, timeout=15)
                    written.append(text)
            time.sleep(max(0.0, _STREAM_SECONDS - tap.elapsed()))
            stopped_at = tap.elapsed()
            closed = _newest_record(serial, mark=f"stream-close-{run}" if ours else None)
        finally:
            exit_status = tap.stop()
        window = [
            raw
            for _pid, raw in _split_records(
                _run_bytes(
                    ["logcat", "-d", "-B", "-t", _epoch_before(opened)],
                    serial,
                    timeout=60,
                    allow_empty=True,
                )
            )
        ]
        if opened not in window:
            raise CaptureError("the window read back does not hold the open record")
        after = window[len(window) - window[::-1].index(opened) :]
        delivered = b"".join(chunk for _at, chunk in tap.chunks)
        complete, trailing = _complete_records(delivered)
        end = _record_time(closed)
        through_close = [raw for raw in after if _record_time(raw) <= end]
        streamed = set(complete)
        extra["stream"] = {
            "argv": ["adb", "-s", "<serial>", *_STREAM_ARGV],
            "stopped_after_s": round(stopped_at, 3),
            "exit_status_after_terminate": exit_status,
            "bytes_delivered": len(delivered),
            "chunks_at_s_and_bytes": [[round(at, 3), len(chunk)] for at, chunk in tap.chunks],
            "complete_records_delivered": len(complete),
            "cut_record_bytes": len(trailing),
        }
        extra["window"] = {
            "argv": ["adb", "-s", "<serial>", "logcat", "-d", "-B", "-t", "<open's epoch - 1 ns>"],
            "open_epoch": _epoch(opened),
            "close_epoch": _epoch(closed),
            "open_record_at": window.index(opened),
            "records_after_open": len(after),
            "records_through_close": len(through_close),
            "of_them_delivered_by_the_stream": sum(raw in streamed for raw in through_close),
        }
        if ours:
            texts = [_tag_and_message(raw) for raw in through_close]
            arrived = [_tag_and_message(raw) for raw in complete]
            extra["marks"] = {
                "written": len(written),
                "padding_bytes": padding,
                "in_window": sum((_WINDOW_TAG, text) in texts for text in written),
                "delivered_by_the_stream": sum((_WINDOW_TAG, text) in arrived for text in written),
            }
            if extra["marks"]["in_window"] != len(written):
                raise CaptureError(f"the window holds {extra['marks']} of the marks written")
            first_mark = next(
                raw for raw in through_close if _tag_and_message(raw) == (_WINDOW_TAG, written[0])
            )
            extra["since_is_strict"] = _since_is_strict(serial, first_mark)
        kept = [_aliased_record(raw) for raw in complete if _publishable(raw)]
        cut_kept = bool(trailing) and _ours_partial(trailing)
        window_kept = [_aliased_record(raw) for raw in window if _publishable(raw)]
        extra["kept"] = {
            "rule": f"records whose uid is below {_FIRST_APP_UID}, private package names aliased",
            "stream_records": len(kept),
            "cut_record_kept": cut_kept,
            "window_records": len(window_kept),
        }
        provenance = {"first_half": f"{name}_records", "run": run}
        _stash_twin(f"{name}_open_records", _aliased_record(opened), provenance)
        _stash_twin(f"{name}_window_records", b"".join(window_kept), provenance)
        return b"".join(kept) + (trailing if cut_kept else b"")

    return capture


def _stream_fixtures(
    name: str, description: str, *, marks: int = 0, padding: int = 0
) -> list[Fixture]:
    """One stream scenario's three fixtures, recorded together (``_stream_scenario``)."""
    together = f"recorded in {name}_records' run"
    halves = (
        (
            "open",
            _NEWEST_ARGV,
            "The newest record the device held just before the stream opened: the "
            "window runs after it (the twin's `-t` is one nanosecond before its epoch).",
        ),
        (
            "window",
            ["logcat", "-d", "-B", "-t", "<open's epoch - 1 ns>"],
            "The stream's window read back in dump mode once the stream was gone: the "
            "open record itself (manifest `window.open_record_at`), then every record "
            "after it that was still in the buffer.",
        ),
    )
    return [
        Fixture(
            name=f"{name}_records",
            # Its marks write to the device's log; the read-only scenario still
            # takes a capture callable, which the device guard cannot see into.
            emulator_only=True,
            ext="bin",
            args=[],
            command=(
                f"adb -s <serial> logcat -B -T 1, stopped by the recorder after "
                f"{_STREAM_SECONDS} s" + (f", {marks} marks logged into it" if marks else "")
            ),
            description=description,
            capture_bytes=_stream_scenario(name, marks=marks, padding=padding),
        ),
        *(
            Fixture(
                name=f"{name}_{half}_records",
                emulator_only=True,
                ext="bin",
                args=args,
                command=f"{together}: adb -s <serial> {' '.join(args)}",
                description=text,
                capture_bytes=_twin(f"{name}_{half}_records", f"{name}_records"),
            )
            for half, args, text in halves
        ),
    ]


_TAIL_COUNTS = (1, 5, 50, 500)


def _tail_records(serial: str, extra: dict) -> bytes:
    """``logcat -d -B -T N`` for N in 1, 5, 50, 500: how many whole frames each answers.

    Only reads. The manifest's ``measured`` gives, per N, the bytes, the whole
    frames and the bytes left over after them (a cut record); the file keeps
    the publishable records (uid below 10000, private package names aliased)
    of the ``-T 50`` answer.
    """
    measured: dict[str, dict[str, int]] = {}
    kept: list[bytes] = []
    for count in _TAIL_COUNTS:
        data = _run_bytes(["logcat", "-d", "-B", "-T", str(count)], serial, timeout=60)
        complete, cut = _complete_records(data)
        measured[str(count)] = {"bytes": len(data), "frames": len(complete), "cut_bytes": len(cut)}
        if count == 50:
            kept = [_aliased_record(raw) for raw in complete if _publishable(raw)]
    extra["measured"] = measured
    extra["kept"] = {
        "rule": f"records whose uid is below {_FIRST_APP_UID}, private package names aliased",
        "of": "the -T 50 answer",
        "records": len(kept),
    }
    return b"".join(kept)


# --- v0.9.0 Inc 0: ground truth for uid ownership, users and adb states -------

# A serial adb gives an emulator. Every other serial -- a USB handset's, an
# `adb connect` host:port, an mDNS name -- belongs to somebody's hardware and is
# not ours to publish.
_EMULATOR_SERIAL_RE = re.compile(r"emulator-\d+")
SERIAL_REDACTED = "SERIALREDACTED"


def _redact_serials(text: str) -> str:
    """``adb devices`` text with every non-emulator serial replaced by SERIALREDACTED.

    The row is kept, and its state column with it: a phone listed beside the
    emulator is part of what the output looks like, and its STATE is what a
    parser reads. Only the first column changes; the header and adb's
    ``* daemon ...`` lines are left alone.
    """
    out = []
    for line in text.splitlines(keepends=True):
        fields = line.split()
        if (
            fields
            and not line.startswith(("List of devices", "*"))
            and not _EMULATOR_SERIAL_RE.fullmatch(fields[0])
        ):
            line = line.replace(fields[0], SERIAL_REDACTED, 1)
        out.append(line)
    return "".join(out)


def _device_rows(text: str) -> list[list[str]]:
    """The device rows of ``adb devices`` text, split into fields."""
    return [
        line.split()
        for line in text.splitlines()
        if line.split() and not line.startswith(("List of devices", "*"))
    ]


def _exactly_one_device(text: str) -> str:
    """Refuse an ``adb devices`` capture that is not one device row.

    adb_devices_single exists to show the one-device shape. Recorded with a
    second emulator or a phone attached it would be a multi-device listing
    under a name that says otherwise.
    """
    rows = _device_rows(text)
    if len(rows) != 1:
        raise CaptureError(
            f"adb lists {len(rows)} devices; adb_devices_single needs exactly one attached"
        )
    return text


# A user created for one capture and removed before it is accepted. Named so a
# leftover is recognisable in `pm list users`.
_TMP_USER_NAME = "v9-fixture-tmp"

# What `pm grant --user` grants in the two-user window: the one runtime
# permission the fixture app requests (added to its manifest for this), so the
# secondary user's block can read granted=true while user 0's reads false.
_TWO_USER_GRANT = "android.permission.CAMERA"
# A package in a shared uid whose runtime grants are NOT system-fixed, so a
# per-user grant can differ: android.uid.shared, where READ_PHONE_NUMBERS is
# denied without SYSTEM_FIXED (measured, API 33). android.uid.system's grants
# (com.android.localtransport) are all SYSTEM_FIXED and read the same for
# every user.
_SHARED_UID_PACKAGE = "com.android.providers.contacts"
_SHARED_UID_GRANT = "android.permission.READ_PHONE_NUMBERS"

# The text halves of a multi-fixture run, written by the fixtures listed after
# it (`_text_twin`). Captures, so they still go through the text pipeline.
_TEXT_TWINS: dict[str, tuple[Capture, dict]] = {}


def _text_twin(name: str, first_half: str) -> Callable[[Path, str, dict], Capture]:
    """A ``capture_with`` returning what ``first_half``'s run captured for ``name``."""

    def capture(_scratch: Path, _serial: str, extra: dict) -> Capture:
        if name not in _TEXT_TWINS:
            raise CaptureError(
                f"{name} is captured in the same run as {first_half}; record them together: "
                f"--only {first_half} {name}"
            )
        result, provenance = _TEXT_TWINS.pop(name)
        extra.update(provenance)
        return result

    return capture


def _users_listed(serial: str) -> str:
    return _run(["shell", "pm", "list", "users"], serial, timeout=15).text


def _secondary_user_gone(serial: str) -> None:
    """``teardown_verify``: no user this recorder created is still on the device."""
    listing = _users_listed(serial)
    if f":{_TMP_USER_NAME}:" in listing:
        raise CaptureError(
            f"user {_TMP_USER_NAME!r} is still on the device; remove it by hand: "
            f"adb -s <serial> shell pm remove-user <id>  ({listing.strip()!r})"
        )


def _two_users_window(_scratch: Path, serial: str, extra: dict) -> Capture:
    """The fixture app installed for two users: a per-user grant and both dumps.

    A throwaway user is created and the app installed for it (as
    package_list_uid_two_users does). Inside the user's lifetime: ``pm grant
    --user <id>`` (pm_grant_user, its stdout and stderr together and its exit
    status echoed), the same for a shared-uid package
    (:data:`_SHARED_UID_PACKAGE`), then ``dumpsys package`` for the fixture app
    (this fixture) and for that package (dumpsys_package_shared_uid_two_users).
    Each dump must show its permission denied for user 0 and granted for the
    new user. The user is removed in a ``finally`` and the removal read back
    from ``pm list users``.
    """
    created = _run(["shell", "pm", "create-user", _TMP_USER_NAME], serial, timeout=60)
    match = re.search(r"created user id (\d+)", created.text)
    if created.returncode != 0 or match is None:
        raise CaptureError(f"pm create-user failed: {created.text.strip()!r}")
    user = match.group(1)
    try:
        installed = _run(
            ["shell", "pm", "install-existing", "--user", user, COMPOSE_PACKAGE], serial, timeout=60
        )
        if installed.returncode != 0:
            raise CaptureError(f"install-existing failed: {installed.text.strip()!r}")
        grant = _run(
            [
                "shell",
                f"pm grant --user {user} {COMPOSE_PACKAGE} {_TWO_USER_GRANT} 2>&1; echo exit=$?",
            ],
            serial,
            timeout=30,
        )
        shared_grant = _run(
            [
                "shell",
                f"pm grant --user {user} {_SHARED_UID_PACKAGE} {_SHARED_UID_GRANT} 2>&1; "
                f"echo exit=$?",
            ],
            serial,
            timeout=30,
        )
        dump = _run(["shell", "dumpsys", "package", COMPOSE_PACKAGE], serial, timeout=60)
        shared = _run(["shell", "dumpsys", "package", _SHARED_UID_PACKAGE], serial, timeout=60)
    finally:
        removed = _run(["shell", "pm", "remove-user", user], serial, timeout=60)
    for _ in range(15):
        if f"UserInfo{{{user}:" not in _users_listed(serial):
            break
        time.sleep(1)
    else:
        raise CaptureError(f"user {user} still listed after pm remove-user: {removed.text!r}")
    for name, result in (("dumpsys package", dump), ("dumpsys package (shared uid)", shared)):
        for needle in ("User 0:", f"User {user}:"):
            if needle not in result.text:
                raise CaptureError(f"{name} names no {needle!r} block with two users installed")
    if "exit=" not in grant.text:
        raise CaptureError(f"pm grant --user printed no exit status: {grant.text.strip()!r}")
    shared_users = shared.text.partition("\nShared users:")[2]
    for text, permission, result in (
        (dump.text, _TWO_USER_GRANT, grant),
        (shared_users, _SHARED_UID_GRANT, shared_grant),
    ):
        seen = _granted_by_user(text, permission)
        if seen.get("0") != "false" or seen.get(user) != "true":
            raise CaptureError(
                f"expected {permission} denied for user 0 and granted for user {user}; the "
                f"dump's user blocks say {seen} (pm grant: {result.text.strip()!r})"
            )
    provenance = {"secondary_user": user}
    extra.update(provenance)
    _TEXT_TWINS["pm_grant_user"] = (grant, {**provenance, "permission": _TWO_USER_GRANT})
    _TEXT_TWINS["dumpsys_package_shared_uid_two_users"] = (
        shared,
        {
            **provenance,
            "permission": _SHARED_UID_GRANT,
            "grant": {
                "command": (
                    f"adb shell 'pm grant --user {user} {_SHARED_UID_PACKAGE} "
                    f"{_SHARED_UID_GRANT} 2>&1; echo exit=$?'"
                ),
                "output": _strip_cr(shared_grant.text),
            },
        },
    )
    return dump


def _granted_by_user(text: str, permission: str) -> dict[str, str | None]:
    """``{user id: "true"/"false"/None}`` for ``permission`` in each ``    User N: `` block."""
    parts = re.split(r"^    User (\d+): ", text, flags=re.MULTILINE)
    seen: dict[str, str | None] = {}
    for at in range(1, len(parts) - 1, 2):
        state = re.search(rf"^\s+{re.escape(permission)}: granted=(\w+)", parts[at + 1], re.M)
        seen[parts[at]] = state.group(1) if state else None
    return seen


# What the user-refusal window grants for the installed secondary user: a
# runtime permission the fixture app does NOT request. On API 35 user 0 that is
# a silent no-op (pm_grant_not_requested); on API 33 it throws.
_UNREQUESTED_GRANT = "android.permission.POST_NOTIFICATIONS"


def _user_refusals_window(_scratch: Path, serial: str, extra: dict) -> Capture:
    """What a per-user grant does when the user, the install or the request is missing.

    A throwaway user is created. While it exists, in this order: ``pm list
    users`` (pm_list_users_two_users); ``pm grant --user <id>`` for an id the
    listing does not name (pm_grant_user_missing); a grant of CAMERA for the
    new user BEFORE the app is installed for it, then ``dumpsys package``
    (dumpsys_package_user_not_installed); ``pm install-existing --user <id>``;
    a grant of :data:`_UNREQUESTED_GRANT`, which the app does not request, for
    that user (this fixture, both streams and the exit status kept apart); and
    a grant then revoke of CAMERA for that user, then ``dumpsys package``
    (dumpsys_package_user_revoked). The user is removed in a ``finally`` and
    the removal read back.
    """
    created = _run(["shell", "pm", "create-user", _TMP_USER_NAME], serial, timeout=60)
    match = re.search(r"created user id (\d+)", created.text)
    if created.returncode != 0 or match is None:
        raise CaptureError(f"pm create-user failed: {created.text.strip()!r}")
    user = match.group(1)
    try:
        listing = _run(["shell", "pm", "list", "users"], serial, timeout=15)
        listed = set(re.findall(r"UserInfo\{(\d+):", listing.text))
        if listed != {"0", user}:
            raise CaptureError(f"expected users 0 and {user} listed: {listing.text.strip()!r}")
        missing = str(max(int(u) for u in listed) + 100)
        missing_grant = _run(
            [
                "shell",
                f"pm grant --user {missing} {COMPOSE_PACKAGE} {_TWO_USER_GRANT} 2>&1; "
                f"echo exit=$?",
            ],
            serial,
            timeout=30,
        )
        uninstalled_grant = _run(
            [
                "shell",
                f"pm grant --user {user} {COMPOSE_PACKAGE} {_TWO_USER_GRANT} 2>&1; echo exit=$?",
            ],
            serial,
            timeout=30,
        )
        uninstalled_dump = _run(["shell", "dumpsys", "package", COMPOSE_PACKAGE], serial, 60)
        installed = _run(
            ["shell", "pm", "install-existing", "--user", user, COMPOSE_PACKAGE], serial, timeout=60
        )
        if installed.returncode != 0:
            raise CaptureError(f"install-existing failed: {installed.text.strip()!r}")
        refused = _run(
            ["shell", "pm", "grant", "--user", user, COMPOSE_PACKAGE, _UNREQUESTED_GRANT],
            serial,
            timeout=30,
        )
        cycle = _run(
            [
                "shell",
                f"pm grant --user {user} {COMPOSE_PACKAGE} {_TWO_USER_GRANT} 2>&1; "
                f"echo exit=$?; pm revoke --user {user} {COMPOSE_PACKAGE} {_TWO_USER_GRANT} "
                f"2>&1; echo exit=$?",
            ],
            serial,
            timeout=30,
        )
        revoked_dump = _run(["shell", "dumpsys", "package", COMPOSE_PACKAGE], serial, 60)
    finally:
        removed = _run(["shell", "pm", "remove-user", user], serial, timeout=60)
    for _ in range(15):
        if f"UserInfo{{{user}:" not in _users_listed(serial):
            break
        time.sleep(1)
    else:
        raise CaptureError(f"user {user} still listed after pm remove-user: {removed.text!r}")

    if "exit=" not in missing_grant.text:
        raise CaptureError(f"pm grant --user {missing} printed no exit status")
    if not re.search(rf"^    User {user}: .*\binstalled=false\b", uninstalled_dump.text, re.M):
        raise CaptureError(f"the dump before install-existing names no installed=false user {user}")
    if "SecurityException" not in refused.stderr:
        raise CaptureError(
            f"expected a SecurityException granting an unrequested permission for user {user}; "
            f"got exit {refused.returncode}: {refused.text.strip()!r}"
        )
    if cycle.text.count("exit=0") != 2:
        raise CaptureError(f"the grant/revoke cycle did not exit 0 twice: {cycle.text!r}")
    if _granted_by_user(revoked_dump.text, _TWO_USER_GRANT).get(user) is not None:
        raise CaptureError(
            f"expected no {_TWO_USER_GRANT} entry left under user {user} after the revoke; "
            f"the dump's user blocks say {_granted_by_user(revoked_dump.text, _TWO_USER_GRANT)}"
        )
    provenance = {"secondary_user": user}
    extra.update({**provenance, "permission": _UNREQUESTED_GRANT})
    _TEXT_TWINS["pm_list_users_two_users"] = (listing, provenance)
    _TEXT_TWINS["pm_grant_user_missing"] = (
        missing_grant,
        {**provenance, "missing_user": missing, "permission": _TWO_USER_GRANT},
    )
    _TEXT_TWINS["dumpsys_package_user_not_installed"] = (
        uninstalled_dump,
        {
            **provenance,
            "permission": _TWO_USER_GRANT,
            "grant": {
                "command": (
                    f"adb shell 'pm grant --user {user} {COMPOSE_PACKAGE} {_TWO_USER_GRANT} "
                    f"2>&1; echo exit=$?'"
                ),
                "output": _strip_cr(uninstalled_grant.text),
            },
        },
    )
    _TEXT_TWINS["dumpsys_package_user_revoked"] = (
        revoked_dump,
        {
            **provenance,
            "permission": _TWO_USER_GRANT,
            "grant_revoke": {
                "command": (
                    f"adb shell 'pm grant --user {user} {COMPOSE_PACKAGE} {_TWO_USER_GRANT} "
                    f"2>&1; echo exit=$?; pm revoke --user {user} {COMPOSE_PACKAGE} "
                    f"{_TWO_USER_GRANT} 2>&1; echo exit=$?'"
                ),
                "output": _strip_cr(cycle.text),
            },
        },
    )
    return refused


def _strictmode_records(serial: str, extra: dict) -> bytes:
    """StrictModeActivity's violation as logd's records, with the app's uid beside it.

    Run after the setup that provokes it (the same as
    logcat_strictmode_violation's). Kept: the records from the app's pid
    (``pidof`` in the same run) or naming the package. The manifest carries the
    package's uid listing, so the uid logd stamped on the StrictMode records
    can be held against the app's.
    """
    pid = _run(["shell", "pidof", COMPOSE_PACKAGE], serial, timeout=15).text.strip()
    if not pid.isdigit():
        raise CaptureError(f"{COMPOSE_PACKAGE} is not running after StrictModeActivity: {pid!r}")
    listing = _run(
        ["shell", "cmd", "package", "list", "packages", "-U", COMPOSE_PACKAGE], serial, timeout=15
    )
    if f"package:{COMPOSE_PACKAGE} uid:" not in listing.text:
        raise CaptureError(f"no uid listing for {COMPOSE_PACKAGE}: {listing.text.strip()!r}")
    data = _keep_records(
        _run_bytes(["logcat", "-d", "-B"], serial, timeout=60), [pid], [COMPOSE_PACKAGE]
    )
    tags = [_tag_and_message(raw)[0] for _pid, raw in _split_records(data)]
    if "StrictMode" not in tags:
        raise CaptureError(f"no StrictMode record among the app's {len(tags)} record(s)")
    extra["pids"] = [pid]
    extra["package_uid_listing"] = {
        "command": f"adb shell cmd package list packages -U {COMPOSE_PACKAGE}",
        "exit_status": listing.returncode,
        "output": _strip_cr(listing.text),
    }
    extra.update(_device_zone(serial))
    return data


def _system_server_pid(serial: str) -> dict:
    """``metadata``: system_server's pid, read with ``pidof`` right after the capture."""
    answer = _run(["shell", "pidof", "system_server"], serial, timeout=15).text.strip()
    if not answer.isdigit():
        raise CaptureError(f"pidof system_server named no single pid: {answer!r}")
    return {"system_server_pid": answer}


# How long a rebooting emulator is watched for an `offline` row, and how long
# it then gets to come back. A cold boot of an API 33 image is ~1 min.
_OFFLINE_WITHIN_S = 60
_REBOOT_BACK_WITHIN_S = 240


def _booted(serial: str) -> bool:
    try:
        answer = _run(["shell", "getprop", "sys.boot_completed"], serial, timeout=10)
    except CaptureError:
        return False
    return answer.returncode == 0 and answer.text.strip() == "1"


def _rebooted_back(serial: str) -> None:
    """``teardown_verify``: the rebooted emulator finished booting again."""
    if not _booted(serial):
        raise CaptureError(f"{serial} has not finished booting (sys.boot_completed != 1)")


def _offline_window(_scratch: Path, serial: str, extra: dict) -> Capture:
    """``adb devices -l`` (host-wide, no serial) while the target reads ``offline``.

    The target is rebooted and the HOST's device list polled until the target's
    row says ``offline``; that listing is the capture. The emulator is then
    waited for (``wait-for-device``, then ``sys.boot_completed``) before the
    capture is returned, so a run never ends with the device down.
    """
    if not serial or not _EMULATOR_SERIAL_RE.fullmatch(serial):
        raise CaptureError(f"adb_devices_offline reboots its target; {serial!r} is no emulator")
    rebooted = _run(["reboot"], serial, timeout=60)
    if rebooted.returncode != 0:
        raise CaptureError(f"adb reboot exited {rebooted.returncode}: {rebooted.text.strip()!r}")
    seen: list[str] = []
    capture: Capture | None = None
    try:
        deadline = time.monotonic() + _OFFLINE_WITHIN_S
        while time.monotonic() < deadline:
            listing = _run_host_adb(["devices", "-l"], timeout=15)
            row = next((r for r in _device_rows(listing.stdout) if r[0] == serial), None)
            state = row[1] if row and len(row) > 1 else "absent"
            if not seen or seen[-1] != state:
                seen.append(state)
            if state == "offline":
                capture = listing
                break
            time.sleep(0.1)
    finally:
        _run(["wait-for-device"], serial, timeout=_REBOOT_BACK_WITHIN_S)
        deadline = time.monotonic() + _REBOOT_BACK_WITHIN_S
        while not _booted(serial):
            if time.monotonic() > deadline:
                raise CaptureError(f"{serial} did not finish booting within the wait")
            time.sleep(2)
    if capture is None:
        raise CaptureError(
            f"{serial} never read `offline` within {_OFFLINE_WITHIN_S}s of `adb reboot`; "
            f"states seen in order: {seen}"
        )
    extra["target_serial"] = serial
    extra["states_seen"] = seen
    return capture


# --- agent text in a failing command's streams ---------------------------------
#
# run_adb types a device error from any stderr line that starts with `adb: ` or
# `error: ` and names a device problem, and an `adb shell` command's own stderr
# arrives on that same pipe. So a command that ECHOES the text an agent handed
# it -- a package, a permission, a path, a locale -- can print a line that reads
# as adb's, and the device "vanishes" (exit 3) while it answers. These record,
# for each run_adb caller whose argv carries agent text, one representative call
# with that text holding a newline and a forged adb line, and say which stream,
# if any, carried it back. The marker is under com.example. so the aliaser keeps
# it whole.
ECHO_FORGED = "adb: device 'aes-spoof' not found"
ECHO_TEXT = f"com.example.echo\n{ECHO_FORGED}"
# The path an agent would pass to `app_launcher --install`: adb reads it on the
# host and names it in its OWN error line, so no newline is needed.
ECHO_APK = "/nonexistent/device 'aes-spoof' not found.apk"
_ECHO_STREAMS = ("stdout", "stderr")


def _echo_capture(
    args: list[str],
    *,
    on: tuple[str, ...],
    name: str | None = None,
    marker: str = "com.example.echo",
) -> Callable[[Path, str, dict], Capture]:
    """Run ``args`` once; require ``marker`` on exactly the streams in ``on``.

    The manifest says where it was (``agent_text_on``) and whether a line on
    that stream STARTS with the forged adb line (``forged_line_starts_on``),
    which is what adb_exec's classifier reads. ``name`` set: the run printed on
    both streams, the file holds stdout and stderr goes to the twin
    ``<name>_stderr`` (:func:`_text_twin`).
    """

    def capture(_scratch: Path, serial: str, extra: dict) -> Capture:
        result = _run(args, serial)
        streams = {"stdout": result.stdout, "stderr": result.stderr}
        found = tuple(s for s in _ECHO_STREAMS if marker in streams[s])
        if found != on:
            raise CaptureError(
                f"the agent text came back on {found or 'neither stream'}, not {on or 'neither'}"
                f" (exit {result.returncode}, stdout {result.stdout[:200]!r}, "
                f"stderr {result.stderr[:200]!r})"
            )
        extra["agent_text_on"] = list(found) or "neither"
        extra["forged_line_starts_on"] = [
            s
            for s in _ECHO_STREAMS
            if any(line.startswith(ECHO_FORGED) for line in streams[s].split("\n"))
        ]
        if name is not None:
            if not (result.stdout.strip() and result.stderr.strip()):
                raise CaptureError(f"{name}: expected output on both streams")
            _TEXT_TWINS[f"{name}_stderr"] = (
                Capture(result.stderr, "", result.returncode),
                {"first_half": name, "exit_status": result.returncode, "stream": "stderr"},
            )
        return result

    return capture


FIXTURES: list[Fixture] = [
    # --- device discovery -------------------------------------------------
    Fixture(
        name="adb_devices_single",
        args=["devices", "-l"],
        description="`adb devices -l` with exactly one device attached.",
        post=_chain(_redact_serials, _exactly_one_device),
    ),
    Fixture(
        name="adb_devices_multiple",
        args=["devices", "-l"],
        description=(
            "`adb devices -l` with two devices attached. resolve_device_identifier"
            "(None) returns None, so build_adb_command omits -s entirely and adb "
            "fails with a raw 'more than one device/emulator'. Record by starting "
            "a second emulator instance: emulator -avd NAME -read-only."
        ),
        catches=("R5",),
        post=_redact_serials,
    ),
    Fixture(
        name="adb_devices_offline",
        emulator_only=True,
        args=["devices", "-l"],
        command=(
            "adb -s <emulator> reboot; poll adb devices -l (host-wide, no serial) every 0.1 s "
            "until the emulator's row reads offline; adb -s <emulator> wait-for-device; poll "
            "getprop sys.boot_completed until 1"
        ),
        description=(
            "`adb devices -l` while a device is in the `offline` state: ground truth for what "
            "device_list's parse_adb_devices and serial resolution see for a device that is "
            "attached but not usable. Produced by rebooting the recording emulator and "
            "keeping the first host listing whose row for it reads `offline`; the emulator is "
            "waited for until sys.boot_completed is 1 before the capture is accepted. Every "
            "serial that is not `emulator-NNNN` (a phone attached to the same host) is "
            "replaced by SERIALREDACTED; its row and state are kept. Manifest: "
            "`target_serial`, and `states_seen`, the target's states in the order they were "
            "polled. `unauthorized` is NOT recorded: it needs a new adb host key and an "
            "authorization prompt on a real phone."
        ),
        capture_with=_offline_window,
        teardown_verify=_rebooted_back,
        post=_redact_serials,
    ),
    Fixture(
        name="adb_device_not_found",
        args=["-s", "no-such-serial-xyz", "get-state"],
        description=(
            "What adb prints for an unknown serial, using a non-blocking command. "
            "Note the prefix is 'error:', not 'adb:'. `adb -s <unknown> shell` "
            "does NOT print this -- it blocks waiting for the device to appear, "
            "which is why log_monitor validates the serial up front instead of "
            "inferring from exit status."
        ),
        catches=("R5",),
    ),
    # --- logcat: the format mismatch that makes log_monitor inert ---------
    Fixture(
        name="logcat_time",
        args=["logcat", "-d", "-v", "time", "-t", "200"],
        description=(
            "`logcat -v time` output: 'MM-DD HH:MM:SS.mmm P/Tag( PID): msg'. "
            "log_monitor requests this format but its regex expects threadtime, "
            "so every line falls into the unparsed branch."
        ),
        catches=("A1",),
    ),
    Fixture(
        name="logcat_threadtime",
        args=["logcat", "-d", "-v", "threadtime", "-t", "200"],
        description=(
            "`logcat -v threadtime` output: 'MM-DD HH:MM:SS.mmm PID TID P Tag: msg'. "
            "This is the layout log_monitor's regex actually expects, and what "
            "anr_watcher correctly requests."
        ),
        catches=("A1",),
    ),
    # --- display geometry: Override vs Physical ---------------------------
    Fixture(
        name="wm_size_physical",
        emulator_only=True,
        args=["shell", "wm", "size"],
        description="`wm size` with no override set — only a 'Physical size:' line.",
        setup=[["shell", "wm", "size", "reset"]],
    ),
    Fixture(
        name="wm_density_physical",
        emulator_only=True,
        args=["shell", "wm", "density"],
        description="`wm density` with no override set — only a 'Physical density:' line.",
        setup=[["shell", "wm", "density", "reset"]],
    ),
    Fixture(
        name="wm_size_override",
        emulator_only=True,
        args=["shell", "wm", "size"],
        description=(
            "`wm size` with an override active — emits BOTH 'Physical size:' and "
            "'Override size:'. device_utils matches only Physical, so every tap "
            "coordinate is wrong by the ratio while an override is set."
        ),
        catches=("S9",),
        setup=[["shell", "wm", "size", "1080x2400"]],
        teardown=[["shell", "wm", "size", "reset"]],
        teardown_verify=_display_restored(size=True),
    ),
    Fixture(
        name="wm_density_override",
        emulator_only=True,
        args=["shell", "wm", "density"],
        description=(
            "`wm density` with an override active — emits BOTH 'Physical density:' "
            "and 'Override density:'. Needed for a correct px->dp conversion. "
            "The override is 480 because it has to differ from the PHYSICAL "
            "density of every profile this is recorded on: it used to be 560, "
            "which is exactly the Pixel 4 XL's physical density, so the same "
            "capture on that device produced Physical 560 / Override 560 and "
            "could not show which of the two lines a parser had read."
        ),
        catches=("S7", "S9"),
        setup=[["shell", "wm", "density", "480"]],
        teardown=[["shell", "wm", "density", "reset"]],
        teardown_verify=_display_restored(density=True),
    ),
    # --- the reset receipt ------------------------------------------------
    # wm_size_physical is captured from a device that never had an override, so
    # it can only show that a clean device is clean. These two apply the
    # override and then reset it, so what they record is the RESET working --
    # the one thing the recorder has to get right on a device somebody owns.
    Fixture(
        name="wm_size_after_reset",
        emulator_only=True,
        args=["shell", "wm", "size"],
        description=(
            "`wm size` read back after an override was set and then reset. Only "
            "a Physical line remains. This is the receipt for the teardown: "
            "`wm size reset` prints nothing whether or not it worked, so its "
            "silence proves nothing and this capture is what does."
        ),
        catches=("S9",),
        setup=[["shell", "wm", "size", "1080x2400"], ["shell", "wm", "size", "reset"]],
        teardown=[["shell", "wm", "size", "reset"]],
        teardown_verify=_display_restored(size=True),
        post=_requires("Physical size:", absent=("Override",)),
    ),
    Fixture(
        name="wm_density_after_reset",
        emulator_only=True,
        args=["shell", "wm", "density"],
        description=(
            "`wm density` read back after an override was set and then reset. "
            "Only a Physical line remains. Same receipt, for the density the dp "
            "conversion depends on."
        ),
        catches=("S7", "S9"),
        setup=[["shell", "wm", "density", "480"], ["shell", "wm", "density", "reset"]],
        teardown=[["shell", "wm", "density", "reset"]],
        teardown_verify=_display_restored(density=True),
        post=_requires("Physical density:", absent=("Override",)),
    ),
    # --- invented commands: proof they do not do what the code assumes ----
    Fixture(
        name="cmd_statusbar_help",
        args=["shell", "cmd", "statusbar"],
        description=(
            "Authoritative `cmd statusbar` subcommand list. Contains no "
            "battery-level / battery-charging / wifi-enabled / wifi-level / "
            "mobile-enabled / mobile-level / mobile-datatype — the seven "
            "subcommands status_bar.py invents. Those are SystemUI demo-mode "
            "broadcast extras, not statusbar subcommands."
        ),
        catches=("S12",),
    ),
    Fixture(
        name="cmd_notification_help",
        args=["shell", "cmd", "notification"],
        description=(
            "Authoritative `cmd notification` subcommand list. There is no "
            "'list channels' subcommand — `cmd notification list channels <pkg>` "
            "silently ignores the extra args and runs bare `list`, exiting 0, "
            "which is why push_notification always reports 'no channels found'."
        ),
        catches=("S14",),
    ),
    Fixture(
        name="dumpsys_activity_anr",
        record_both_streams=True,
        args=["shell", "dumpsys", "activity", "anr"],
        description=(
            "There is no `anr` subcommand of `dumpsys activity`. ActivityManager "
            "answers 'Unknown command: anr' / 'Bad activity command, or no "
            "activities match: anr' / 'Use -h for help.' -- and **exits 0**, "
            "which is the part that matters (see exit_status in this entry). "
            "anr_watcher._pull_dumpsys_anr guarded on `returncode != 0`, so the "
            "guard never fired; it then fed these three usage lines to a "
            "logcat-line parser, which matched none of them. The 'historical ANR "
            "pull' the docstring advertised has always been three lines of help "
            "text going into a parser that discards them."
        ),
        catches=("T5",),
    ),
    Fixture(
        name="am_broadcast_missing_receiver",
        emulator_only=True,
        args=[
            "shell",
            "am",
            "broadcast",
            "-n",
            "com.android.settings/.NoSuchReceiverExists",
            "-a",
            "android.intent.action.VIEW",
        ],
        description=(
            "`am broadcast` targeting a receiver class that does not exist still "
            "prints 'Broadcast completed: result=0' and exits 0. This is why "
            "push_notification's success check ('result=' in stdout) can never "
            "report failure."
        ),
        catches=("S4",),
    ),
    # --- notification state: the replacement capability -------------------
    Fixture(
        name="cmd_notification_list",
        args=["shell", "cmd", "notification", "list"],
        description=(
            "Posted-notification keys: 'userId|package|id|tag|uid'. The primitive "
            "for asserting what the app under test actually posted."
        ),
    ),
    # NOTE: `dumpsys notification --noredact` is deliberately NOT recorded.
    # On a real developer device it carries app-internal notification channel
    # ids and human-readable channel names, which package-name redaction cannot
    # reliably catch, and the dump is ~150KB. When Increment 3 needs real channel
    # readout, record a narrow fixture from a clean AVD instead.
    Fixture(
        name="resolve_activity_launcher",
        args=[
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-c",
            "android.intent.category.LAUNCHER",
            "com.android.settings",
        ],
        description=(
            "Correct launcher-activity resolution. app_launcher now uses this; it "
            "previously grepped `pm dump` for the first line containing both 'Activity' "
            "and the package name, which readily matched an ActivityRecord or a provider "
            "(S10, fixed during the adb_exec migration)."
        ),
        catches=("S10",),
    ),
    # --- increment 3: SMS/OTP, snapshots, crash triage --------------------
    Fixture(
        name="logcat_crash_java",
        args=["logcat", "-b", "crash", "-d", "-t", "200"],
        description=(
            "The dedicated crash buffer after a Java crash. This is the whole "
            "point of `-b crash`: the trace arrives already isolated, so triage "
            "does not mean grepping the main buffer for 'FATAL EXCEPTION' and "
            "hoping the surrounding lines belong to it. Note the shape a parser "
            "must handle -- a '--------- beginning of crash' separator, then one "
            "'FATAL EXCEPTION: <thread>' line, a 'Process: <pkg>, PID: <n>' line, "
            "the exception class and message, and an indented 'at ...' frame list "
            "that is the only multi-line part."
        ),
    ),
    Fixture(
        name="emu_help_sms",
        args=["emu", "help", "sms"],
        description=(
            "The console's authoritative `sms` sub-command list: `send` and "
            "`pdu`, and nothing else. Both simulate an INBOUND message, so there "
            "is no console path for testing an app's outgoing-SMS code, and no "
            "MMS sub-command. Recorded so a claim about `adb emu sms` can be "
            "checked against the console rather than assumed -- several scripts "
            "in this skill previously invented sub-commands outright."
        ),
    ),
    Fixture(
        name="logcat_crash_loop",
        args=["logcat", "-b", "crash", "-d", "-t", "200"],
        description=(
            "A real crash LOOP: the same app crashed three times. Recorded "
            "because the multi-block behaviour was previously exercised by "
            "cloning the single-crash fixture, which cannot prove how the "
            "device actually frames repeats. Measured here: three "
            "'FATAL EXCEPTION' blocks, exactly ONE "
            "'--------- beginning of crash' separator (the reader prints it "
            "once per dump, not once per crash), and a distinct PID per "
            "repeat. So blocks are delimited by 'FATAL EXCEPTION' and never by "
            "the separator, and PID/timestamp must stay out of any dedup key "
            "or one crash loop becomes N separate faults."
        ),
    ),
    Fixture(
        name="logcat_app_restart",
        emulator_only=True,
        args=["logcat", "-d", "-v", "threadtime"],
        command=(
            "adb shell am start -W (x3, ended by am crash, then am force-stop, then left "
            "running), then adb logcat -d -v threadtime, host-filtered to the lines that "
            "name the package or come from its pids"
        ),
        description=(
            "One app, three processes, one window: the fixture app launched, "
            "crashed (`am crash`), relaunched, force-stopped, relaunched. The "
            "manifest's `pids` are what `pidof` answered while each was alive, "
            "in order. This is the evidence that ties a pid to a package when "
            "the process is gone and `pidof` can no longer say: what "
            "system_server and the app itself write about a process. Note "
            "that most lines naming the package come from processes that are "
            "NOT the app (system_server, the launcher, the IME), so naming "
            "the package says nothing about which process wrote the line. "
            "Host-filtered to lines that name the package or carry one of its "
            "pids; other apps' free text is not recorded."
        ),
        catches=("D4",),
        capture_with=_app_restart_window_ms,
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
    ),
    Fixture(
        name="logcat_app_restart_usec",
        emulator_only=True,
        args=["logcat", "-d", "-v", "threadtime", "-v", "usec"],
        command=(
            "adb shell am start -W (x3, ended by am crash, then am force-stop, then left "
            "running), then adb logcat -d -v threadtime -v usec, host-filtered to the lines "
            "that name the package or come from its pids"
        ),
        description=(
            "One app, three processes, one window: the fixture app launched, "
            "crashed (`am crash`), relaunched, force-stopped, relaunched. The "
            "manifest's `pids` are what `pidof` answered while each was alive, "
            "in order. This is the evidence that ties a pid to a package when "
            "the process is gone and `pidof` can no longer say: what "
            "system_server and the app itself write about a process. Note "
            "that most lines naming the package come from processes that are "
            "NOT the app (system_server, the launcher, the IME), so naming "
            "the package says nothing about which process wrote the line. "
            "Host-filtered to lines that name the package or carry one of its "
            "pids; other apps' free text is not recorded. `system_server_pid` "
            "is `pidof system_server` read in the same run: the only process "
            "whose ActivityManager lines may say which pids were the app's, "
            "since any app can log under that tag. `package_uid_listing` and "
            "`ps_alive` are the uid oracles from the same run: `cmd package list "
            "packages -U` for the package, `ps -o PID,UID` for the live pid. "
            "Microsecond headers (`-v usec`): the lines of one multi-line message "
            "share their header to the microsecond; separate messages in one "
            "millisecond do not."
        ),
        catches=("D4",),
        capture_with=_app_restart_window,
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
    ),
    Fixture(
        name="logcat_app_restart_records",
        emulator_only=True,
        ext="bin",
        args=["logcat", "-d", "-B"],
        command=(
            "recorded in logcat_app_restart_usec's run, right after its text: adb logcat -d -B, "
            "host-filtered to the records that name the package or come from its pids"
        ),
        description=(
            "logcat_app_restart_usec's window as logd's records (`logcat -B`), dumped right after "
            "the text in the same run: what log_monitor and app_state_capture read. Each record "
            "is `struct logger_entry` (u16 payload length, u16 header size 28, i32 pid, u32 tid, "
            "sec, nsec, log id, uid) then `<priority><tag>NUL<message>NUL`; a multi-line message "
            "is ONE record. The pid and uid are logd's, from the socket's credentials: the "
            "metadata an app cannot write. Kept whole when the record names the package or comes from one of "
            "its pids, so it is a superset of the text half's lines. The manifest carries the "
            "same oracles as the text half, and `timezone`: the zone the device printed the text "
            "in, which a test printing these records must use."
        ),
        catches=("D4", "A1"),
        capture_bytes=_twin("logcat_app_restart_records", "logcat_app_restart_usec"),
    ),
    Fixture(
        name="logcat_tag_injection",
        emulator_only=True,
        args=["logcat", "-d", "-v", "threadtime", "-v", "usec"],
        command=(
            "adb shell am start -W (the fixture app); adb shell 'log -p e -t \"x:<newline>"
            '<system_server header> I ActivityManager" "Start proc $$:<package>/<uid> for '
            "w3l-tag-injection\"; exec log ...'; adb logcat -d -v threadtime -v usec, "
            "host-filtered to the package, its pid and the two injecting pids"
        ),
        description=(
            "A TAG an app chooses forges a whole line. The shell, standing in for any app, logs "
            "ONE record whose tag is `x:`, a newline, and a threadtime header claiming "
            "system_server's pid and the tag ActivityManager, with system_server's `Start proc "
            "<victim>:<package>/<package uid>` as its message; the victim (the logging shell, "
            "which `exec log` keeps at its pid) then logs one line. logcat prints a tag "
            "verbatim, so this text shows system_server naming the victim as the app's process "
            "-- byte reads and LF framing do not help. The manifest's `injection` names the "
            "forging and the victim pid; `pids` is the app's pid from `pidof`; "
            "`system_server_pid` the real one."
        ),
        catches=("A1",),
        capture_with=_tag_injection,
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
    ),
    Fixture(
        name="logcat_tag_injection_records",
        emulator_only=True,
        ext="bin",
        args=["logcat", "-d", "-B"],
        command="recorded in logcat_tag_injection's run, right after its text: adb logcat -d -B",
        description=(
            "logcat_tag_injection's window as logd's records: the forging record is ONE record, "
            "pid and uid the shell's (2000), its tag holding the newline and the fake header -- "
            "nothing in it is system_server's. Same filter and oracles as the text half."
        ),
        catches=("A1",),
        capture_bytes=_twin("logcat_tag_injection_records", "logcat_tag_injection"),
    ),
    Fixture(
        name="logcat_anr_broadcast_records",
        emulator_only=True,
        ext="bin",
        catches=("D5", "A1"),
        args=["logcat", "-d", "-B"],
        command=(
            "adb shell am broadcast -f 0x10000000 (the fixture app's AnrReceiver, which blocks "
            "40 s); 20 s later adb logcat -d -B, host-filtered to ActivityManager and "
            "WindowManager records naming the package, from the provoking broadcast on; where "
            "ActivityManager logs no `Broadcasting: Intent` record (API 33), from an "
            "`aes-recorder: anr-window` record logged just before provoking"
        ),
        description=(
            "One broadcast ANR as logd's records -- what anr_watcher reads (`-B`), the twin "
            "in kind of logcat_anr_broadcast_stream. ActivityManager's report is ONE record: "
            "its message is 'ANR in <package>' then '\\nPID: <app pid>', '\\nReason: Broadcast "
            "of Intent {...}' and the rest of the report, all one writer's. ~5 s later "
            "WindowManager's 'ANR in Window{...}' report and ActivityManager's 'Skipping "
            "duplicate ANR' verdict, one record each. Every record's pid is system_server's "
            "(manifest `system_server_pid`, `pidof` in the same run): the writer an ANR report "
            "is held to. That shape is API 35's; a level that differs lists what it lacks under "
            "the manifest's `expected_absent`, and `window_anchor` says where the window began."
        ),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.DefaultActivity"],
            ["shell", "sleep", "2"],
            _LOG_WINDOW_MARK,
            _PROVOKE_ANR,
            ["shell", "sleep", "20"],
        ],
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
        capture_bytes=_anr_records(
            requires=(f"ANR in {COMPOSE_PACKAGE}",),
            expects=(
                "\nPID: ",
                "\nReason: Broadcast of Intent",
                "ANR in Window{",
                "Skipping duplicate ANR",
            ),
        ),
    ),
    Fixture(
        name="logcat_anr_input_records",
        emulator_only=True,
        ext="bin",
        catches=("D5", "A1"),
        args=["logcat", "-d", "-B"],
        command=(
            "adb shell am broadcast --async (background, 60 s timeout), then input keyevent "
            "--async KEYCODE_SHIFT_LEFT to the blocked window (where `input` lists no --async, "
            "API 33: plain `input keyevent`, its adb call killed after 5 s); 15 s later adb "
            "logcat -d -B, host-filtered as logcat_anr_broadcast_records"
        ),
        description=(
            "An input-dispatch ANR as logd's records, the twin in kind of "
            "logcat_anr_input_stream: WindowManager's 'ANR in Window{...}. Reason:Input "
            "dispatching timed out' record first, then ActivityManager's report -- ONE record, "
            "'ANR in <package> (<component>)' with its PID and Reason lines. Every record's "
            "pid is system_server's (manifest `system_server_pid`). That shape is API 35's; a "
            "level that differs lists what it lacks under `expected_absent`, and `key_event` "
            "says how the key was sent."
        ),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.DefaultActivity"],
            ["shell", "sleep", "2"],
            _LOG_WINDOW_MARK,
            [
                "shell",
                "am",
                "broadcast",
                "--async",
                "-n",
                f"{COMPOSE_PACKAGE}/.AnrReceiver",
                "-a",
                f"{COMPOSE_PACKAGE}.PROVOKE_ANR",
            ],
            ["shell", "sleep", "2"],
        ],
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
        capture_bytes=_anr_records(
            requires=(f"ANR in {COMPOSE_PACKAGE}",),
            expects=(
                "ANR in Window{",
                f"ANR in {COMPOSE_PACKAGE} (",
                "\nReason: Input dispatching timed out",
            ),
            before=_key_then_wait,
        ),
    ),
    Fixture(
        name="logcat_anr_foreign_process_records",
        emulator_only=True,
        ext="bin",
        catches=("W4-N",),
        args=["logcat", "-d", "-B"],
        command=(
            "adb logcat -c; am start -W the fixture app; adb shell am broadcast --async "
            "-f 0x10000000 "
            "-n com.example.composefixture/.ForeignProcessAnrReceiver (android:process="
            "com.example.victim, AnrReceiver's 40 s block); once system_server logs its "
            "`Start proc <pid>:com.example.victim`, ps -o PID,UID -p <pid> and both packages' "
            "cmd package list packages -U; after `ANR in com.example.victim`, adb logcat -d -B, "
            "host-filtered to ActivityManager and WindowManager records naming the process or "
            "the provoking action, from the provoking broadcast on; where ActivityManager logs "
            "no `Broadcasting: Intent` record (API 33), from an `aes-recorder: anr-window` "
            "record logged just before the broadcast"
        ),
        description=(
            "An ANR ActivityManager files under ANOTHER package: the fixture app's own stall, "
            "in a process it named com.example.victim (android:process), under its own uid. "
            "ActivityManager prints the PROCESS name -- 'ANR in com.example.victim', its "
            "'\\nPID: <pid>' that process's -- and system_server's `Start proc <pid>:"
            "com.example.victim/u0a205` and `Killing <pid>:com.example.victim/u0a205 ... bg anr` "
            "carry the impostor's uid. Manifest: `stalled_pid`, `ps_alive` (ps -o PID,UID -p "
            "<pid> while it lived, ps_pid_uid's syntax), `package_uid_listing` (the fixture "
            "app's uid) and `claimed_package_uid_listing` (com.example.victim is not "
            "installed), `system_server_pid`. The `Start proc` and `ANR in` records are "
            "required on every level; what a level lacks of the rest of API 35's shape is "
            "listed under `expected_absent`."
        ),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["logcat", "-c"],
            ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.DefaultActivity"],
            ["shell", "sleep", "2"],
        ],
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
        capture_bytes=_foreign_process_anr,
    ),
    Fixture(
        name="logcat_crash_tail",
        emulator_only=True,
        args=["logcat", "-b", "crash", "-d", "-v", "threadtime", "-v", "usec", "-T", "1"],
        command=(
            "adb logcat -b crash -c; (am start -W, am crash) x2; adb logcat -b crash -d "
            "-v threadtime -v usec -T 1"
        ),
        description=(
            "`-T 1` against a crash buffer holding two crashes: the WHOLE last crash, every "
            "line under one header, after the buffer's separator -- not its last line. logd "
            "tails records, and a multi-line message is one record. The buffer itself is "
            "logcat_crash_buffer (text) and logcat_crash_buffer_records; this tail as records "
            "is logcat_crash_tail_records. All four in one run."
        ),
        catches=("A8",),
        capture_with=_crash_tail,
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
    ),
    Fixture(
        name="logcat_crash_buffer",
        emulator_only=True,
        args=["logcat", "-b", "crash", "-d", "-v", "threadtime", "-v", "usec"],
        command="recorded in logcat_crash_tail's run: adb logcat -b crash -d -v threadtime -v usec",
        description=(
            "The crash buffer logcat_crash_tail tails: two FATAL EXCEPTION records of the "
            "fixture app, as text."
        ),
        catches=("A8",),
        capture_bytes=_twin("logcat_crash_buffer", "logcat_crash_tail"),
    ),
    Fixture(
        name="logcat_crash_buffer_records",
        emulator_only=True,
        ext="bin",
        args=["logcat", "-b", "crash", "-d", "-B"],
        command="recorded in logcat_crash_tail's run: adb logcat -b crash -d -B",
        description="logcat_crash_buffer as logd's records: two records, one per crash.",
        catches=("A8",),
        capture_bytes=_twin("logcat_crash_buffer_records", "logcat_crash_tail"),
    ),
    Fixture(
        name="logcat_crash_tail_records",
        emulator_only=True,
        ext="bin",
        args=["logcat", "-b", "crash", "-d", "-B", "-T", "1"],
        command="recorded in logcat_crash_tail's run: adb logcat -b crash -d -B -T 1",
        description=(
            "`-T 1` as records: exactly the buffer's last record, byte for byte (checked at "
            "capture), its message many lines long."
        ),
        catches=("A8",),
        capture_bytes=_twin("logcat_crash_tail_records", "logcat_crash_tail"),
    ),
    Fixture(
        name="adb_get_state",
        args=["get-state"],
        description=(
            "`adb get-state` for an attached device: `device`, exit 0. log_monitor asks it before "
            "every read, and again when a live read ends by itself: a streaming `adb logcat` "
            "aimed at a device that is not there waits instead of failing "
            "(adb_waiting_for_device), so its silence cannot tell a quiet device from a missing "
            "one. For a serial that is not attached, see adb_device_not_found."
        ),
        catches=("A4",),
    ),
    Fixture(
        name="getprop_sdk",
        args=["shell", "getprop", "ro.build.version.sdk"],
        description=(
            "The device's API level, as log_monitor reads it once before a live read: "
            "Android 13 (API 33) is the one release whose logcat holds `-B` records back "
            "(logcat_stream_quiet_records), so there the read is made by dumps instead of "
            "a stream."
        ),
    ),
    Fixture(
        name="getprop_unset",
        args=["shell", "getprop", "ro.aes_recorder.not_a_property"],
        description=(
            "What getprop answers for a property the device does not have: an empty line, "
            "exit 0 -- no error at all. So a device that does not say its API level (one "
            "still booting, or a getprop that fails) answers ro.build.version.sdk this way, "
            "and log_monitor refuses the live read rather than guessing the level: a "
            "guessed stream on API 33 is the zero-line success it exists to prevent."
        ),
        record_both_streams=True,
    ),
    Fixture(
        name="logcat_tail_records",
        # It only reads, but it is a capture callable, which
        # tests/test_recorder_device_guard.py cannot see into: the conservative mark.
        emulator_only=True,
        ext="bin",
        args=[],
        command="adb -s <serial> logcat -d -B -T N, for N in 1, 5, 50, 500",
        description=(
            "Count-based tails of logd's order as records: `-T N` answers the newest N whole "
            "frames while the buffer holds N (manifest `measured`: bytes, frames, and the bytes "
            "of any cut record, per N). log_monitor's dump-made read on API 33 finds its cursor "
            "in such a tail, doubling N until the tail reaches it or comes back short: no "
            "timestamp positions the read. The file keeps the publishable records of the -T 50 "
            "answer."
        ),
        capture_bytes=_tail_records,
    ),
    Fixture(
        name="adb_waiting_for_device",
        args=[],
        command=(
            "ANDROID_SERIAL=no-such-serial-xyz adb logcat -B -T 1 (host adb), stopped after 3 s"
        ),
        description=(
            "What a streaming `adb logcat` prints while its device is not attached: the banner "
            "`- waiting for device -` on STDERR, nothing on stdout, and it does not exit "
            "(recorded exit status: terminated). The case log_monitor --duration used to "
            "report as a quiet device, and a banner that pushes adb's own error off the start "
            "of the stream's stderr (A5)."
        ),
        catches=("A4", "A5"),
        capture_with=_waiting_for_device,
    ),
    Fixture(
        name="package_list_uid",
        args=["shell", "cmd", "package", "list", "packages", "-U", COMPOSE_PACKAGE],
        description=(
            "`cmd package list packages -U <package>`: one line, "
            "'package:<package> uid:<uid>', exit 0. The filter is a SUBSTRING "
            "match (measured: asking for 'com.example.compose' lists "
            "com.example.composefixture too), and a package that is not "
            "installed prints nothing and still exits 0 -- so a reader must pick "
            "the line whose name is exactly the package, and treat none as "
            "unknown."
        ),
        post=_requires(f"package:{COMPOSE_PACKAGE} uid:"),
    ),
    Fixture(
        name="package_list_uid_two_users",
        emulator_only=True,
        args=[],
        command=(
            "adb shell pm create-user; pm install-existing --user <id> <package>; "
            "cmd package list packages -U <package>; pm remove-user <id>"
        ),
        description=(
            "`cmd package list packages -U <package>` with the package installed "
            "for two users: ONE row, the uids comma-separated "
            "('uid:10205,1110205' for user 11), not a row per user (measured, Android 15). A "
            "reader keeping the first number rejects the second user's processes. "
            "The throwaway user is removed before the capture is accepted, and the "
            "listing is read back until it names one uid again."
        ),
        capture_with=_package_uids_two_users,
    ),
    Fixture(
        name="package_list_uid_all",
        args=["shell", "cmd", "package", "list", "packages", "-U"],
        description=(
            "`cmd package list packages -U` with NO package filter: the whole table, one "
            "'package:<name> uid:<uid>' row per package. Ground truth for mapping a uid back "
            "to its packages -- several rows naming one uid is a sharedUserId (android.uid."
            "system's packages all read uid:1000) -- which a reader of the filtered form "
            "(package_list_uid) never sees. Non-AOSP names are aliased."
        ),
        post=_requires(f"package:{COMPOSE_PACKAGE} uid:", "package:android uid:1000"),
    ),
    Fixture(
        name="dumpsys_package_two_users",
        emulator_only=True,
        args=[],
        command=(
            "adb shell pm create-user v9-fixture-tmp; pm install-existing --user <id> "
            f"{COMPOSE_PACKAGE}; pm grant --user <id> {COMPOSE_PACKAGE} {_TWO_USER_GRANT} "
            f"(pm_grant_user); pm grant --user <id> {_SHARED_UID_PACKAGE} {_SHARED_UID_GRANT}; "
            f"dumpsys package {COMPOSE_PACKAGE}; dumpsys package {_SHARED_UID_PACKAGE} "
            "(dumpsys_package_shared_uid_two_users); pm remove-user <id>"
        ),
        description=(
            "`dumpsys package <fixture app>` with the app installed for two users and CAMERA "
            "granted for the second only: ground truth for where per-user runtime-permission "
            "state sits in the dump -- a `User 0:` and a `User <id>:` block under the package "
            "(the id is the manifest's `secondary_user`), each with its own install state and "
            "its own `runtime permissions:` section, where CAMERA reads granted=false for user "
            "0 and granted=true for user <id> (the recorder refuses a capture that does not). "
            "CAMERA is the one runtime permission the fixture app requests, added to its "
            "manifest for this fixture; emulator-api35's pm_grant_not_requested and "
            "dumpsys_package_after_silent_grant predate it and are unaffected (they use "
            "POST_NOTIFICATIONS, still not requested). The throwaway user is removed in the "
            "same run and its removal read back from `pm list users`."
        ),
        capture_with=_two_users_window,
        teardown_verify=_secondary_user_gone,
        post=_requires("User 0:"),
    ),
    Fixture(
        name="pm_grant_user",
        emulator_only=True,
        args=[],
        command=(
            f"adb shell 'pm grant --user <id> {COMPOSE_PACKAGE} {_TWO_USER_GRANT} 2>&1; "
            "echo exit=$?' (in dumpsys_package_two_users' run, while user <id> exists)"
        ),
        description=(
            "`pm grant --user <secondary user>` of CAMERA, a runtime permission the fixture app "
            "requests: what a per-user grant that WORKS prints and its exit status (stdout and "
            "stderr together, then the echoed `exit=`), ground truth for privacy_manager's "
            "--user handling. That it took effect for that user only is in "
            "dumpsys_package_two_users, recorded in the same run; manifest `secondary_user`, "
            "`permission`."
        ),
        capture_with=_text_twin("pm_grant_user", "dumpsys_package_two_users"),
        post=_requires("exit="),
    ),
    Fixture(
        name="dumpsys_package_shared_uid_two_users",
        emulator_only=True,
        args=[],
        command=(
            f"adb shell 'pm grant --user <id> {_SHARED_UID_PACKAGE} {_SHARED_UID_GRANT}'; "
            f"adb shell dumpsys package {_SHARED_UID_PACKAGE} (in dumpsys_package_two_users' "
            "run, while the secondary user exists)"
        ),
        description=(
            f"`dumpsys package {_SHARED_UID_PACKAGE}` (shared uid android.uid.shared) with a "
            f"second user on the device and {_SHARED_UID_GRANT.rsplit('.', 1)[1]} granted for "
            "that user only: ground truth for per-user runtime state in the shared-uid "
            "layout. A shared-uid package's runtime permissions are printed under the "
            "top-level `Shared users:` block (dumpsys_package_shared_uid), and here its "
            "`SharedUser [android.uid.shared]` block carries one `User <n>:` section per user, "
            "the permission granted=false under `User 0:` and granted=true under `User <id>:` "
            "(the recorder refuses a capture that does not). android.uid.system "
            "(com.android.localtransport, dumpsys_package_shared_uid) cannot show this: its "
            "grants are SYSTEM_FIXED and read the same for every user. Recorded with "
            "dumpsys_package_two_users; manifest `secondary_user`, `permission`, `grant` "
            "(the pm grant's command and output)."
        ),
        capture_with=_text_twin(
            "dumpsys_package_shared_uid_two_users", "dumpsys_package_two_users"
        ),
        post=_requires("Shared users:", "SharedUser [android.uid.shared]"),
    ),
    Fixture(
        name="pm_list_users",
        args=["shell", "pm", "list", "users"],
        description=(
            "`pm list users` on a device with only its system user: a `Users:` line, then one "
            "tab-indented `UserInfo{<id>:<name>:<flags>}` row per user, `running` after a "
            "started one. Ground truth for privacy_manager's check that a --user id exists "
            "before it grants (pm_grant_user_missing: a grant for a missing user exits 0)."
        ),
        post=_requires("UserInfo{0:"),
    ),
    Fixture(
        name="pm_grant_user_not_requested",
        emulator_only=True,
        args=[],
        command=(
            f"adb shell pm create-user {_TMP_USER_NAME}; pm list users "
            "(pm_list_users_two_users); pm grant --user <missing id> ... "
            "(pm_grant_user_missing); pm grant --user <id> "
            f"{COMPOSE_PACKAGE} {_TWO_USER_GRANT}; dumpsys package {COMPOSE_PACKAGE} "
            f"(dumpsys_package_user_not_installed); pm install-existing --user <id> "
            f"{COMPOSE_PACKAGE}; pm grant --user <id> {COMPOSE_PACKAGE} {_UNREQUESTED_GRANT}; "
            f"pm grant then pm revoke --user <id> {COMPOSE_PACKAGE} {_TWO_USER_GRANT}; "
            f"dumpsys package {COMPOSE_PACKAGE} (dumpsys_package_user_revoked); "
            "pm remove-user <id>"
        ),
        description=(
            "`pm grant --user <secondary user>` of a runtime permission the fixture app does "
            f"NOT request ({_UNREQUESTED_GRANT.rsplit('.', 1)[1]}), the app installed for that "
            "user: `java.lang.SecurityException: Package ... has not requested permission ...` "
            "and a stack trace on stderr, exit 255 (API 33). On API 35 the same grant for user "
            "0 is a silent exit 0 (emulator-api35 pm_grant_not_requested), so a reader must "
            "handle both. The throwaway user is removed in the same run and its removal read "
            "back; manifest `secondary_user`, `permission`."
        ),
        capture_with=_user_refusals_window,
        teardown_verify=_secondary_user_gone,
        post=_requires("SecurityException"),
    ),
    Fixture(
        name="pm_list_users_two_users",
        emulator_only=True,
        args=[],
        command="adb shell pm list users (in pm_grant_user_not_requested's run)",
        description=(
            "`pm list users` while a throwaway secondary user exists: `UserInfo{0:...}` and "
            "`UserInfo{<id>:...}` rows (the id is the manifest's `secondary_user`). Ground "
            "truth for parsing more than one user out of the listing."
        ),
        capture_with=_text_twin("pm_list_users_two_users", "pm_grant_user_not_requested"),
        post=_requires("UserInfo{0:"),
    ),
    Fixture(
        name="pm_grant_user_missing",
        emulator_only=True,
        args=[],
        command=(
            f"adb shell 'pm grant --user <id not in pm list users> {COMPOSE_PACKAGE} "
            f"{_TWO_USER_GRANT} 2>&1; echo exit=$?' (in pm_grant_user_not_requested's run)"
        ),
        description=(
            "`pm grant --user <id>` for a user the device does not have (manifest "
            "`missing_user`): nothing printed and exit 0 (measured, API 33). Neither the "
            "output nor the status says the user is missing, so privacy_manager checks the id "
            "against `pm list users` before granting."
        ),
        capture_with=_text_twin("pm_grant_user_missing", "pm_grant_user_not_requested"),
        post=_requires("exit="),
    ),
    Fixture(
        name="dumpsys_package_user_not_installed",
        emulator_only=True,
        args=[],
        command=(
            f"adb shell 'pm grant --user <id> {COMPOSE_PACKAGE} {_TWO_USER_GRANT}'; adb shell "
            f"dumpsys package {COMPOSE_PACKAGE} (in pm_grant_user_not_requested's run, the user "
            "created but the app not yet installed for it)"
        ),
        description=(
            "`dumpsys package <fixture app>` with a secondary user the app is NOT installed "
            "for, after `pm grant --user <id>` of CAMERA for that user (it exits 0; manifest "
            "`grant`): the dump still prints a `User <id>:` block, its first line saying "
            "`installed=false`, and a `runtime permissions:` section under it in which CAMERA "
            "reads granted=true (measured, API 33). A read-back that only looks for the "
            "permission would confirm a grant for an app that user does not have."
        ),
        capture_with=_text_twin(
            "dumpsys_package_user_not_installed", "pm_grant_user_not_requested"
        ),
        post=_requires("installed=false"),
    ),
    Fixture(
        name="dumpsys_package_user_revoked",
        emulator_only=True,
        args=[],
        command=(
            f"adb shell 'pm grant --user <id> {COMPOSE_PACKAGE} {_TWO_USER_GRANT}; pm revoke "
            f"--user <id> {COMPOSE_PACKAGE} {_TWO_USER_GRANT}'; adb shell dumpsys package "
            f"{COMPOSE_PACKAGE} (in pm_grant_user_not_requested's run, the app installed for "
            "the user)"
        ),
        description=(
            "`dumpsys package <fixture app>` after CAMERA was granted and then revoked for a "
            "secondary user (both exit 0; manifest `grant_revoke`): that user's `runtime "
            "permissions:` header is printed with NO entries -- a revoked permission with no "
            "flags is not listed at all, while `requested permissions:` still names CAMERA "
            "and user 0's section still lists it with its USER_SENSITIVE flags (measured, "
            "API 33). A reader taking a missing entry for 'not requested' misreports a "
            "plain revoke."
        ),
        capture_with=_text_twin("dumpsys_package_user_revoked", "pm_grant_user_not_requested"),
        post=_requires("runtime permissions:"),
    ),
    Fixture(
        name="ps_pid_uid",
        emulator_only=True,
        args=[],
        command="adb shell ps -o PID,UID -p <fixture app pid>,<system_server pid>",
        description=(
            "`ps -o PID,UID -p <pid>,<pid>`: a header, then one row per pid that "
            "exists, sorted by pid, with the NUMERIC uid (10205 for the fixture "
            "app, 1000 for system_server). Measured: `-p a -p b` answers the same; "
            "a pid that does not exist is left out, and with none existing only "
            "the header prints and the exit status is 1. The pids are under "
            "'pids' in this entry."
        ),
        capture_with=_ps_two_owners,
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
    ),
    Fixture(
        name="ps_all_pid_uid",
        args=["shell", "ps", "-A", "-o", "PID,UID"],
        description=(
            "`ps -A -o PID,UID`: every process, as the two numeric columns parse_ps_uids "
            "reads (a `PID UID` header, then one row per process). Ground truth for a "
            "whole-table pid -> uid read rather than ps_pid_uid's `-p` subset. Manifest "
            "`system_server_pid` (`pidof system_server` right after) names the row that must "
            "read uid 1000."
        ),
        metadata=_system_server_pid,
        post=_requires("PID", "UID"),
    ),
    Fixture(
        name="emu_sms_send",
        emulator_only=True,
        args=["emu", "sms", "send", "+15551234567", "Your code is 428193"],
        description=(
            "`adb emu sms send` reply: bare 'OK'. It says the console accepted "
            "the command, NOT that a message was delivered -- the same trap as "
            "`am broadcast` always printing 'result=0' (see "
            "am_broadcast_missing_receiver). Delivery is only provable by reading "
            "the inbox back; see content_query_sms_inbox."
        ),
    ),
    Fixture(
        name="content_query_sms_inbox",
        args=[
            "shell",
            "content query --uri content://sms/inbox --projection address:body:date",
        ],
        description=(
            "The SMS inbox read back through the content provider, which is what "
            "proves `adb emu sms send` actually delivered. Row shape is "
            "'Row: <n> address=..., body=..., date=...' -- comma-space separated "
            "pairs, so a body containing ', ' cannot be split naively."
        ),
    ),
    Fixture(
        name="content_query_sms_inbox_multi",
        emulator_only=True,
        args=[
            "shell",
            "content query --uri content://sms/inbox --projection address:body:date",
        ],
        setup=[
            ["emu", "sms", "send", "+15550002222", "Order 42, shipped today"],
            ["emu", "sms", "send", "+15550003333", "Verify 7781 now"],
            # `adb emu sms send` returns before the message reaches the inbox:
            # measured on API 35, absent immediately and present after ~2.1s.
            # Anything reading the inbox straight after a send must poll.
            ["shell", "sleep", "5"],
        ],
        description=(
            "The same query with several messages present, including one whose "
            "body contains ', ' -- 'Order 42, shipped today'. That row is the "
            "point of the fixture: the pair separator is also ', ', so splitting "
            "on it yields 'address=+1555...', 'body=Order 42', 'shipped today' "
            "and 'date=...'. A parser must bound each value by the NEXT KNOWN KEY "
            "instead. Note also that rows arrive newest first (the provider's own "
            "ORDER BY is date DESC, visible in the SQL echoed by "
            "content_query_sms_error), that dates are epoch milliseconds, and "
            "that two identical bodies can coexist -- which is why verifying a "
            "send means finding a message the inbox did not already contain. "
            "Re-recording appends: the setup sends again, so the file grows. "
            "That is harmless, since what it pins is row shape."
        ),
    ),
    Fixture(
        name="content_query_empty_result",
        args=[
            "shell",
            "content query --uri content://sms/sent --projection address:body:date",
        ],
        description=(
            "What `content query` prints for an empty cursor: 'No result found.' "
            "on stdout, exit 0, nothing on stderr. Recorded against sms/sent, "
            "which is empty unless something has sent from the device. This is "
            "the string that distinguishes an EMPTY inbox from an UNREADABLE one "
            "(content_query_sms_error) -- 'the message did not arrive' and "
            "'the inbox could not be read' are different answers and must not be "
            "reported as the same one."
        ),
    ),
    Fixture(
        name="content_query_sms_error",
        args=[
            "shell",
            "content query --uri content://sms/inbox --projection address:body:date "
            "--where address=nosuchcolumn",
        ],
        description=(
            "`content query` FAILING. It writes 'Error while accessing provider:"
            "<authority>' plus a Java stack trace to STDERR, prints nothing on "
            "stdout, and EXITS 0 -- the same shape of trap as `adb emu` and "
            "`am broadcast`. A caller checking only the exit status, or only "
            "stdout, reads this as an empty inbox. (The unquoted --where value "
            "reaches SQLite as an identifier, which is how the failure is "
            "provoked; the compiled SQL it echoes also documents the provider's "
            "default 'ORDER BY date DESC'.)"
        ),
    ),
    Fixture(
        name="emu_sms_send_missing_arg",
        emulator_only=True,
        args=["emu", "sms", "send", "+15551234567"],
        description=(
            "`adb emu sms send` with the body omitted. The console answers "
            "\"KO: missing argument, try 'sms send <phonenumber> <text "
            "message>'\" and adb still EXITS 0, so anything reading the exit "
            "status sees a successful send. Failure is visible only in the reply "
            "text; common/emu_console.run_emu is what turns it into an error."
        ),
    ),
    Fixture(
        name="content_query_sms_date_in_body",
        args=[
            "shell",
            "content query --uri content://sms/inbox --projection address:body:date",
        ],
        description=(
            "An inbox whose newest body contains a literal 'date=' -- the one "
            "case sms.py's parser claimed to survive with no ground truth "
            "behind the claim. Values are ', '-separated key=value pairs, so a "
            "body reading 'Meeting moved, date=2026-09-15, code 5521' can "
            "swallow the real trailing date= field if the parser splits "
            "greedily on the first match rather than bounding each value by "
            "the next projection key."
        ),
    ),
    Fixture(
        name="emu_avd_snapshot_list",
        args=["emu", "avd", "snapshot", "list"],
        description=(
            "`adb emu avd snapshot list`. A fixed-width table under two header "
            "lines, terminated by the console's 'OK'. The ID column is '--' for "
            "the boot snapshot, so a parser keying on ID must tolerate it."
        ),
    ),
    Fixture(
        name="emu_avd_snapshot_list_empty",
        args=["emu", "avd", "snapshot", "list"],
        description=(
            "`avd snapshot list` on an emulator with NO snapshots: a plain "
            "sentence, 'There is no snapshot available.', where the populated "
            "case is a table with two header lines. Found by CI on its first "
            "successful emulator run -- a fresh runner AVD has no snapshots, "
            "and the dev machine's did, so locally the empty case never "
            "occurred. The parser reported the sentence as an unreadable line "
            "rather than as an empty list."
        ),
    ),
    Fixture(
        name="emu_avd_snapshot_load_missing",
        emulator_only=True,
        args=["emu", "avd", "snapshot", "load", "no_such_snapshot_xyz"],
        description=(
            "A failed snapshot load. The console answers 'KO: <reason>' -- and "
            "`adb emu` still EXITS 0. Failure is only visible in the reply text, "
            "so anything checking returncode reports a successful load of a "
            "snapshot that does not exist, and the test that follows runs "
            "against whatever state the emulator happened to be in. Same class "
            "of trap as `am broadcast` always printing 'result=0'."
        ),
    ),
    # --- ANR / jank: the lines anr_pipeline claims to parse --------------
    #
    # Android will not produce an ANR on request, so these come from an app
    # built to earn one: tests/fixtures/scaffold/compose's AnrReceiver blocks
    # its main thread for 40s, and a FOREGROUND broadcast has a 10s dispatch
    # timeout. No touch input is involved, deliberately -- a recorder must
    # never be the thing that taps a screen.
    Fixture(
        name="logcat_anr_broadcast",
        emulator_only=True,
        args=["shell", "logcat -d -v threadtime | grep -A 3 'ActivityManager: ANR in '"],
        description=(
            "A real hard ANR as ActivityManager frames it, and the shape every "
            "hand-typed sample in this repo got wrong. The line is "
            "'ANR in com.example.composefixture' -- the package ALONE, with no "
            "parenthesised component. The invented samples all wrote "
            "'ANR in com.example.app (com.example.app/.MainActivity)', so the "
            "component group in _ANR_IN_RE was only ever exercised against "
            "output Android does not produce here. Note also what the following "
            "lines carry, each a separate logcat line under the same tag: "
            "'PID: <n>' is the ANRing app's pid, which is NOT the pid in the "
            "logcat column (that is system_server's), and 'Reason: Broadcast of "
            "Intent { ... }' holds the only statement of what actually timed "
            "out. parse_logcat_anr classifies lines one at a time, so it keeps "
            "the package and drops the pid and the reason."
        ),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.DefaultActivity"],
            ["shell", "sleep", "2"],
            ["logcat", "-c"],
            [
                "shell",
                "am",
                "broadcast",
                "-f",
                "0x10000000",
                "-n",
                f"{COMPOSE_PACKAGE}/.AnrReceiver",
                "-a",
                f"{COMPOSE_PACKAGE}.PROVOKE_ANR",
            ],
            # The foreground dispatch timeout is 10s; the input-dispatch ANR
            # follows ~5s later. Measured: both present by +16s.
            ["shell", "sleep", "20"],
        ],
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
        post=_anr_for(COMPOSE_PACKAGE),
    ),
    Fixture(
        name="logcat_anr_input_dispatch",
        emulator_only=True,
        args=["shell", "logcat -d -v threadtime | grep 'WindowManager: ANR in '"],
        description=(
            "The SAME stalled app, reported a second time by a different "
            "subsystem and in a completely different shape: WindowManager "
            "writes one line reading 'ANR in Window{<hash> u0 <pkg>/<Activity>}."
            " Reason:Input dispatching timed out (... Waited 5006ms for "
            "FocusEvent(hasFocus=false)).' Three things a guess gets wrong. "
            "The token after 'ANR in' is the literal word 'Window', not a "
            "package -- _ANR_IN_RE's '([\\w.]+)' happily reports a fault in a "
            "package called 'Window'. The component is inside the braces rather "
            "than in parentheses. And 'Reason:' has no space after the colon, "
            "unlike ActivityManager's. One stall therefore reaches a logcat "
            "reader as two events under two tags."
        ),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.DefaultActivity"],
            ["shell", "sleep", "2"],
            ["logcat", "-c"],
            [
                "shell",
                "am",
                "broadcast",
                "-f",
                "0x10000000",
                "-n",
                f"{COMPOSE_PACKAGE}/.AnrReceiver",
                "-a",
                f"{COMPOSE_PACKAGE}.PROVOKE_ANR",
            ],
            ["shell", "sleep", "20"],
        ],
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
        post=_anr_for(COMPOSE_PACKAGE),
    ),
    # The two fixtures above are single grepped lines. These two keep what a
    # LIVE reader sees of one stall -- every ActivityManager and WindowManager
    # line from the provocation on -- because "how many ANRs was that?" is a
    # question about the stream, not about any one line of it.
    Fixture(
        name="logcat_anr_broadcast_stream",
        emulator_only=True,
        catches=("D5",),
        args=["shell", "logcat -d -v threadtime | grep -E ' (ActivityManager|WindowManager): '"],
        description=(
            "One broadcast ANR, as `anr_watcher --start` reads it. ActivityManager "
            "logs the ANR as ONE record of many lines -- 'ANR in', 'PID:', "
            "'Reason:', 'ErrorId:', 'Frozen:', load, pressure, CPU usage -- every "
            "line under the identical timestamp/pid/tid/priority/tag header, which "
            "is the only thing that says they belong together. ~5 s later the ANR "
            "dialog takes focus, the blocked window cannot answer the focus event, "
            "and WindowManager writes 'ANR in Window{...}. Reason:Input dispatching "
            "timed out'; ActivityManager adjudicates that report a few ms later "
            "with 'Skipping duplicate ANR: ... Input dispatching timed out'. One "
            "stall, three places a line-at-a-time reader finds an ANR."
        ),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.DefaultActivity"],
            ["shell", "sleep", "2"],
            _PROVOKE_ANR,
            # Broadcast ANR at +10 s, WindowManager's report at ~+15 s.
            ["shell", "sleep", "20"],
        ],
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
        post=_anr_stream_for(
            COMPOSE_PACKAGE,
            requires=(
                "PID: ",
                "Reason: Broadcast of Intent",
                "ANR in Window{",
                "Skipping duplicate ANR",
            ),
        ),
    ),
    Fixture(
        name="logcat_anr_input_stream",
        emulator_only=True,
        catches=("D5",),
        args=["shell", "logcat -d -v threadtime | grep -E ' (ActivityManager|WindowManager): '"],
        description=(
            "An INPUT-dispatch ANR with no broadcast ANR in it, as the stream "
            "carries it. The receiver is sent as a BACKGROUND broadcast (60 s "
            "timeout, longer than its 40 s block) with --async, so am returns at "
            "once; the blocked window is then handed one key event. WindowManager "
            "reports first ('ANR in Window{...}. Reason:Input dispatching timed "
            "out ... for KeyEvent'), and ActivityManager follows with its own "
            "full record -- and here, unlike a broadcast ANR, 'ANR in <pkg> "
            "(<component>)' DOES carry the parenthesised component. Recorded to "
            "show that WindowManager's line is a report TO ActivityManager, which "
            "decides: a new ANR (this fixture) or a duplicate "
            "(logcat_anr_broadcast_stream). The key is SHIFT_LEFT: it has to "
            "reach the focused window, and it types nothing anywhere."
        ),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.DefaultActivity"],
            ["shell", "sleep", "2"],
            [
                "shell",
                "am",
                "broadcast",
                "--async",
                "-n",
                f"{COMPOSE_PACKAGE}/.AnrReceiver",
                "-a",
                f"{COMPOSE_PACKAGE}.PROVOKE_ANR",
            ],
            ["shell", "sleep", "2"],
            # --async: injection otherwise waits for the blocked window to
            # finish the key, and gives up only after 30 s (measured: "Input
            # event injection from pid N timed out" at +30 s).
            ["shell", "input", "keyevent", "--async", "KEYCODE_SHIFT_LEFT"],
            # Input dispatch times out after 5 s; ActivityManager's record follows.
            ["shell", "sleep", "15"],
        ],
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
        post=_anr_stream_for(
            COMPOSE_PACKAGE,
            requires=(
                "ANR in Window{",
                f"ANR in {COMPOSE_PACKAGE} (",
                "Reason: Input dispatching timed out",
            ),
        ),
    ),
    Fixture(
        name="logcat_choreographer_jank",
        emulator_only=True,
        args=["shell", "logcat -d -v threadtime | grep Choreographer"],
        description=(
            "Real Choreographer jank: "
            "'I Choreographer: Skipped N frames!  The application may be doing "
            "too much work on its main thread.' TWO spaces after the "
            "exclamation mark, and the tag carries the janking app's own pid in "
            "the logcat columns -- unlike the ActivityManager ANR lines, whose "
            "pid is system_server's. Recorded because every skipped-frame test "
            "in this repo built its input from an f-string, which proves only "
            "that the parser matches the f-string. Provoked by a cold start and "
            "then AnrReceiver's 40s block, and what lands differs by level. "
            "emulator-api35 recorded the block's own line, 'Skipped 2401 "
            "frames', and 2401 * 16.7ms is 40.1 seconds -- the first independent "
            "check this repo has that the FRAME_MS heuristic tracks wall-clock, "
            "on a stall whose real length is known. emulator-api33 recorded no "
            "line for the block, only the cold start's (tens of frames), plus a "
            "Looper 'Slow dispatch' line the grep keeps because it names "
            "Choreographer. A cold start janks only when the host happens to be "
            "busy, so how MANY lines land here is not fixed; the "
            "severity-boundary tests vary the frame count off this line rather "
            "than typing new ones."
        ),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["logcat", "-c"],
            ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.DefaultActivity"],
            # Two sources of jank, so the fixture carries both a small drop and
            # a catastrophic one. The cold start drops ~30 frames; the blocked
            # receiver then drops ~2400. Choreographer reports the second only
            # once the main thread runs again, so the wait must outlast
            # AnrReceiver's 40s sleep -- split in two because a single
            # `adb shell sleep 50` would exceed this recorder's own timeout.
            [
                "shell",
                "am",
                "broadcast",
                "-f",
                "0x10000000",
                "-n",
                f"{COMPOSE_PACKAGE}/.AnrReceiver",
                "-a",
                f"{COMPOSE_PACKAGE}.PROVOKE_ANR",
            ],
            ["shell", "sleep", "25"],
            ["shell", "sleep", "25"],
        ],
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
        post=_requires("Skipped"),
    ),
    Fixture(
        name="logcat_strictmode_violation",
        emulator_only=True,
        # Anchored on the tag column: a bare `grep StrictMode` also matches
        # every WindowManagerShell transition line naming StrictModeActivity,
        # which is ~40KB of unrelated dump.
        args=["shell", "logcat -d -v threadtime | grep -E '[VDIWEF] StrictMode: '"],
        description=(
            "What StrictMode actually logs, against what anr_pipeline was "
            "written for. The real header is "
            "'D StrictMode: StrictMode policy violation; ~duration=27 ms: "
            "android.os.strictmode.DiskReadViolation'; the invented sample was "
            "'D StrictMode: Slow operation detected on main thread', which "
            "StrictMode prints in no form at all (it borrows the wording of "
            "ActivityManager's own 'Slow operation:' bookkeeping, a different "
            "subsystem entirely). Two consequences visible in this file. The "
            "violation states its OWN duration, which _classify_anr discards in "
            "favour of a hard-coded WARN_FRAMES * FRAME_MS. And every frame of "
            "the stack trace that follows is logged under the same StrictMode "
            "tag, so a tag-only match turns one violation into one event per "
            "stack frame -- about thirty. Provoked by "
            "StrictModeActivity, which enables the policy and then reads and "
            "writes a file on the main thread."
        ),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["logcat", "-c"],
            ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.StrictModeActivity"],
            ["shell", "sleep", "3"],
        ],
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
        post=_requires("StrictMode policy violation"),
    ),
    Fixture(
        name="logcat_strictmode_records",
        emulator_only=True,
        ext="bin",
        args=["logcat", "-d", "-B"],
        command=(
            "adb shell am force-stop; adb logcat -c; adb shell am start -W -n "
            f"{COMPOSE_PACKAGE}/.StrictModeActivity; sleep 3; adb logcat -d -B, host-filtered "
            "to the records from the app's pid (pidof) or naming the package"
        ),
        description=(
            "logcat_strictmode_violation's provocation as logd's records (`-B`): ground truth "
            "for whether a StrictMode record carries the APP's uid, which is what lets a "
            "records reader attribute a violation to a package by uid instead of by text. "
            "Each record's pid and uid are logd's, from the writer's socket credentials. "
            "Manifest: `pids` (pidof in the same run), `package_uid_listing` (the app's uid "
            "to hold the records' uid against), `timezone`."
        ),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["logcat", "-c"],
            ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.StrictModeActivity"],
            ["shell", "sleep", "3"],
        ],
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
        capture_bytes=_strictmode_records,
    ),
    # --- host-side adb failures: the retry prompts an agent reads ---------
    Fixture(
        name="adb_shell_device_not_found",
        args=["-s", "no-such-serial-xyz", "shell", "getprop", "ro.build.version.sdk"],
        description=(
            "`adb -s <unknown> shell` -- the call shape the skill's scripts "
            "actually make. The prefix here is 'adb:', where the same failure "
            "from `get-state` (adb_device_not_found) says 'error:'. adb mixes "
            "the two, which is why adb_exec._classify matches the message body "
            "rather than the prefix; recorded so that stays a measured fact "
            "rather than a comment."
        ),
    ),
    Fixture(
        name="adb_more_than_one_device",
        args=[],
        command=" ".join(_NO_TARGET_ARGV),
        capture_with=_more_than_one_device,
        description=(
            "What adb says when two devices are attached and no -s was given: "
            "'adb: more than one device/emulator', exit 1, on stderr, and the "
            "command never reaches a device at all. Recorded as a HOST command "
            "precisely because it must run WITHOUT -s, which every other "
            "fixture here has. Requires two devices attached; with one, the "
            "capture is refused rather than written."
        ),
        post=_requires("more than one device"),
    ),
    Fixture(
        name="logcat_tail_zero",
        # It only reads, but it is a capture callable, which
        # tests/test_recorder_device_guard.py cannot see into: the conservative mark.
        emulator_only=True,
        args=[],
        capture_with=_logcat_tail_zero,
        command=(
            "adb -s <serial> logcat -v threadtime [-v usec] -T 0 "
            "(each ended by the recorder after 3 s)"
        ),
        description=(
            "Whether a live logcat read can start with nothing replayed: it "
            "cannot. logcat answers 'WARNING: -T 0 invalid, setting to 1' on "
            "stderr and starts on the newest entry already buffered, with or "
            "without -v usec (manifest `measured`: the first entry's timestamp "
            "against the device clock read before the calls). So anr_watcher's "
            "live reads keep `-T 1`, and the one entry they start on may "
            "predate the read; a restart skips only the entries it already "
            "consumed. The entries themselves are other processes' text and "
            "are not recorded."
        ),
        post=_requires("-T 0 invalid"),
    ),
    *_stream_fixtures(
        "logcat_stream_quiet",
        (
            "What a live `logcat -B -T 1` delivers in 5 s on a device left alone, before "
            "its reader stops it; the recorder writes nothing to the device. On API 33 "
            "(Android 13) it delivers nothing: logcat writes `-B` records through stdio and "
            "flushes only after a TEXT record (AOSP system/logging logcat.cpp, "
            "android13-release), so records wait in its 4 KiB buffer and are lost when the "
            "stream is terminated. On API 35 each record arrives as it is logged. Manifest: "
            "every chunk the stream delivered, timed (`stream`), and how many of the "
            "window's records it had delivered by the stop (`window`)."
        ),
    ),
    *_stream_fixtures(
        "logcat_stream_marks",
        (
            "As logcat_stream_quiet_records, with 5 marks (`aes-recorder: "
            "stream-mark-<run>-<n>`) logged half a second in. On API 33 the stream delivers "
            "none of them, and its window read back in dump mode holds all five. Also "
            "measured (`since_is_strict`): `-t <epoch>` leaves out a record stamped exactly "
            "then and includes it from one nanosecond earlier."
        ),
        marks=5,
    ),
    *_stream_fixtures(
        "logcat_stream_block",
        (
            "As logcat_stream_marks_records, each mark padded with 900 bytes so six of "
            "them pass 4096: on API 33 the stream delivers whole 4 KiB blocks only -- the "
            "last cut inside a record -- and the marks after the last block reach the "
            "reader only in the window read back in dump mode. The cut mark's visible "
            "bytes are kept when they are the recorder's (`kept.cut_record_kept`)."
        ),
        marks=6,
        padding=900,
    ),
    Fixture(
        name="pm_grant_not_changeable",
        emulator_only=True,
        args=[
            "shell",
            f"pm grant {COMPOSE_PACKAGE} android.permission.INTERNET",
        ],
        description=(
            '`pm grant` REFUSING. It writes "Exception occurred while '
            "executing 'grant':\" and a java.lang.SecurityException with a "
            "full stack trace to stderr, and exits 255. The invented sample "
            "said 'Operation not allowed: java.lang.SecurityException: not "
            "requested' at exit 0 -- neither the wording nor the exit status "
            "Android uses. INTERNET is used because it is an install-time "
            "permission and so is never grantable at runtime, which makes the "
            "refusal reproducible on any device."
        ),
        post=_requires("Exception occurred while executing"),
    ),
    Fixture(
        name="pm_grant_not_requested",
        emulator_only=True,
        args=[
            "shell",
            f"pm grant {COMPOSE_PACKAGE} android.permission.POST_NOTIFICATIONS "
            f"2>&1; echo exit=$?",
        ],
        description=(
            "`pm grant` doing NOTHING and saying so with silence. The fixture "
            "app does not request POST_NOTIFICATIONS, so the permission is not "
            "granted -- and pm prints nothing at all and exits 0. (The echo is "
            "part of the recorded command because an empty file cannot show an "
            "exit status; everything before 'exit=' is pm's own output, and "
            "there is none.) This is the failure push_notification cannot "
            "detect: its check is 'exit 0 and no output means granted', which "
            "is exactly what a no-op looks like."
        ),
        post=_requires("exit=0"),
    ),
    # --- agent text echoed by a failing command (see ECHO_TEXT) ---------------
    Fixture(
        name="pm_grant_permission_echo",
        emulator_only=True,
        record_both_streams=True,
        args=[
            "shell",
            "pm",
            "grant",
            "--user",
            "0",
            "com.android.settings",
            shlex.quote(f"android.permission.{ECHO_TEXT}"),
        ],
        command="adb shell pm grant --user 0 com.android.settings 'android.permission.<echo text>'",
        description=(
            "privacy_manager --grant with a permission name an agent typed. <echo text> is "
            "`com.example.echo`, a newline and `adb: device 'aes-spoof' not found`, quoted for "
            "the device shell as the script quotes it. `pm` exits 255 and its "
            "IllegalArgumentException ('Unknown permission: <name>') prints the name back "
            "on STDERR, newline and all, so the forged line starts a line of the stderr "
            "run_adb classifies (forged_line_starts_on)."
        ),
        capture_with=_echo_capture(
            [
                "shell",
                "pm",
                "grant",
                "--user",
                "0",
                "com.android.settings",
                shlex.quote(f"android.permission.{ECHO_TEXT}"),
            ],
            on=("stderr",),
        ),
        post=_requires("Unknown permission", ECHO_FORGED),
    ),
    Fixture(
        name="pm_revoke_permission_echo",
        emulator_only=True,
        record_both_streams=True,
        args=[
            "shell",
            "pm",
            "revoke",
            "--user",
            "0",
            "com.android.settings",
            shlex.quote(f"android.permission.{ECHO_TEXT}"),
        ],
        command="adb shell pm revoke --user 0 com.android.settings 'android.permission.<echo text>'",
        description=(
            "pm_grant_permission_echo for privacy_manager --revoke / --reset: the same "
            "exception, the name printed back on stderr, exit 255."
        ),
        capture_with=_echo_capture(
            [
                "shell",
                "pm",
                "revoke",
                "--user",
                "0",
                "com.android.settings",
                shlex.quote(f"android.permission.{ECHO_TEXT}"),
            ],
            on=("stderr",),
        ),
        post=_requires("Unknown permission", ECHO_FORGED),
    ),
    Fixture(
        name="dumpsys_package_echo",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "dumpsys", "package", shlex.quote(ECHO_TEXT)],
        command="adb shell dumpsys package '<echo text>'",
        description=(
            "privacy_manager's read-back and --list for a package named by <echo text> (see "
            "pm_grant_permission_echo): 'Unable to find package: <name>' on STDOUT, exit 0, "
            "stderr empty. Not classified: run_adb reads stderr, and only on a non-zero exit."
        ),
        capture_with=_echo_capture(
            ["shell", "dumpsys", "package", shlex.quote(ECHO_TEXT)], on=("stdout",)
        ),
        post=_requires("Unable to find package", ECHO_FORGED),
    ),
    Fixture(
        name="adb_install_missing_apk_echo",
        emulator_only=True,
        record_both_streams=True,
        args=["install", ECHO_APK],
        command=f"adb install {shlex.quote(ECHO_APK)}",
        description=(
            "app_launcher --install with a path that does not exist and that names a device "
            "problem itself -- no newline needed. Exit 1; stdout 'Performing Streamed "
            "Install' (this file); on stderr (adb_install_missing_apk_echo_stderr, same run) "
            "adb's OWN line, `adb: failed to stat <path>: No such file or directory`. It "
            "starts with `adb: ` and carries 'device' and 'not found' from the path, which "
            "is what run_adb's classifier matches."
        ),
        capture_with=_echo_capture(
            ["install", ECHO_APK],
            on=("stderr",),
            name="adb_install_missing_apk_echo",
            marker="aes-spoof",
        ),
        post=_requires("Performing Streamed Install"),
    ),
    Fixture(
        name="adb_install_missing_apk_echo_stderr",
        emulator_only=True,
        args=["install", ECHO_APK],
        command="recorded in adb_install_missing_apk_echo's run: its stderr",
        description="The stderr of adb_install_missing_apk_echo's one run, byte for byte.",
        capture_with=_text_twin(
            "adb_install_missing_apk_echo_stderr", "adb_install_missing_apk_echo"
        ),
        post=_requires("adb: failed to stat", "not found"),
    ),
    Fixture(
        name="am_start_component_echo",
        emulator_only=True,
        record_both_streams=True,
        args=[
            "shell",
            "am",
            "start",
            "-W",
            "-n",
            shlex.quote(f"{ECHO_TEXT}/.Main"),
            "-a",
            "android.intent.action.MAIN",
        ],
        command="adb shell am start -W -n '<echo text>/.Main' -a android.intent.action.MAIN",
        description=(
            "app_launcher --launch with a package named by <echo text> (see "
            "pm_grant_permission_echo). The component is printed back twice, newline and "
            "all, on STDOUT ('Starting: Intent { ... cmp=...' and 'Error: Activity class "
            "{...} does not exist.'); stderr empty. The exit status is in the manifest."
        ),
        capture_with=_echo_capture(
            [
                "shell",
                "am",
                "start",
                "-W",
                "-n",
                shlex.quote(f"{ECHO_TEXT}/.Main"),
                "-a",
                "android.intent.action.MAIN",
            ],
            on=("stdout",),
        ),
        post=_requires("does not exist", ECHO_FORGED),
    ),
    Fixture(
        name="am_start_view_echo",
        emulator_only=True,
        record_both_streams=True,
        args=[
            "shell",
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            shlex.quote(ECHO_TEXT),
        ],
        command="adb shell am start -W -a android.intent.action.VIEW -d '<echo text>'",
        description=(
            "app_launcher --open-url with <echo text> (see pm_grant_permission_echo) as the "
            "URL: 'unable to resolve Intent', the data cut short as am_start_wait_view_"
            "unresolved describes, on STDOUT; stderr empty."
        ),
        capture_with=_echo_capture(
            [
                "shell",
                "am",
                "start",
                "-W",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                shlex.quote(ECHO_TEXT),
            ],
            on=("stdout",),
        ),
        post=_requires("unable to resolve Intent"),
    ),
    Fixture(
        name="package_list_uid_echo",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "cmd", "package", "list", "packages", "-U", shlex.quote(ECHO_TEXT)],
        command="adb shell cmd package list packages -U '<echo text>'",
        description=(
            "device_utils.get_package_uids (log_monitor --app, app_state_capture, "
            "anr_watcher --package) for a package named by <echo text> (see "
            "pm_grant_permission_echo): nothing on either stream, exit 0."
        ),
        capture_with=_echo_capture(
            ["shell", "cmd", "package", "list", "packages", "-U", shlex.quote(ECHO_TEXT)],
            on=(),
        ),
        post=_requires_empty(),
    ),
    Fixture(
        name="pidof_echo",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "pidof", shlex.quote(ECHO_TEXT)],
        command="adb shell pidof '<echo text>'",
        description=(
            "`pidof` for a package named by <echo text> (see pm_grant_permission_echo) -- "
            "log_monitor --app's stream path, app_state_capture, app_launcher --state: "
            "nothing on either stream, exit 1, as pidof_not_running."
        ),
        capture_with=_echo_capture(["shell", "pidof", shlex.quote(ECHO_TEXT)], on=()),
        post=_requires_empty(),
    ),
    Fixture(
        name="dumpsys_package_versionname_echo",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "dumpsys", "package", shlex.quote(ECHO_TEXT), "|", "grep", "versionName"],
        command="adb shell dumpsys package '<echo text>' | grep versionName",
        description=(
            "app_state_capture's version probe for a package named by <echo text> (see "
            "pm_grant_permission_echo). The device-side grep drops dumpsys's 'Unable to find "
            "package' line: nothing on either stream, exit 1."
        ),
        capture_with=_echo_capture(
            ["shell", "dumpsys", "package", shlex.quote(ECHO_TEXT), "|", "grep", "versionName"],
            on=(),
        ),
        post=_requires_empty(),
    ),
    Fixture(
        name="setprop_locale_echo",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "setprop", "persist.sys.locale", shlex.quote(ECHO_TEXT)],
        command="adb shell setprop persist.sys.locale '<echo text>'",
        description=(
            "appearance --locale with <echo text> (see pm_grant_permission_echo): refused, "
            "exit 1, and `Failed to set property 'persist.sys.locale' to '<value>'.` prints "
            "the value back on STDERR, newline and all, so the forged line starts a line of "
            "the stderr run_adb classifies. The property reads `com.example.app1` in the "
            "file: the aliaser takes `persist.sys.locale` for a package name (departure 2)."
        ),
        capture_with=_echo_capture(
            ["shell", "setprop", "persist.sys.locale", shlex.quote(ECHO_TEXT)], on=("stderr",)
        ),
        post=_requires("Failed to set property", ECHO_FORGED),
    ),
    Fixture(
        name="am_broadcast_demo_datatype_echo",
        emulator_only=True,
        record_both_streams=True,
        args=[
            "shell",
            "am",
            "broadcast",
            "-a",
            "com.android.systemui.demo",
            "-e",
            "command",
            "network",
            "-e",
            "mobile",
            "show",
            "-e",
            "level",
            "4",
            "-e",
            "datatype",
            shlex.quote(ECHO_TEXT),
        ],
        command=(
            "adb shell am broadcast -a com.android.systemui.demo -e command network "
            "-e mobile show -e level 4 -e datatype '<echo text>'"
        ),
        description=(
            "status_bar --mobile-type with <echo text> (see pm_grant_permission_echo): the "
            "only free text status_bar sends. The broadcast is delivered whatever the extra "
            "says -- 'Broadcast completed: result=0' on stdout, exit 0 -- and the value is "
            "not printed back (`(has extras)`)."
        ),
        capture_with=_echo_capture(
            [
                "shell",
                "am",
                "broadcast",
                "-a",
                "com.android.systemui.demo",
                "-e",
                "command",
                "network",
                "-e",
                "mobile",
                "show",
                "-e",
                "level",
                "4",
                "-e",
                "datatype",
                shlex.quote(ECHO_TEXT),
            ],
            on=(),
        ),
        post=_requires("Broadcast completed"),
    ),
    Fixture(
        name="input_text_nonascii_echo",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "input", "text", shlex.quote(f"hé{ECHO_TEXT}".replace(" ", "%s"))],
        command="adb shell input text 'hé<echo text>' (spaces sent as %s)",
        description=(
            "keyboard --type / navigator --enter-text with <echo text> (see "
            "pm_grant_permission_echo) behind a non-ASCII letter, the one recorded way "
            "`input text` fails: exit 255 and a NullPointerException on stderr that does "
            "NOT print the text back. (ASCII text with a newline is typed: exit 0, "
            "silent.)"
        ),
        capture_with=_echo_capture(
            ["shell", "input", "text", shlex.quote(f"hé{ECHO_TEXT}".replace(" ", "%s"))],
            on=(),
        ),
        post=_requires("NullPointerException"),
    ),
    Fixture(
        name="am_force_stop_echo",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "am", "force-stop", shlex.quote(ECHO_TEXT)],
        command="adb shell am force-stop '<echo text>'",
        description=(
            "app_launcher --terminate for a package named by <echo text> (see "
            "pm_grant_permission_echo): nothing on either stream, exit 0 -- an unknown "
            "package is not an error to force-stop."
        ),
        capture_with=_echo_capture(["shell", "am", "force-stop", shlex.quote(ECHO_TEXT)], on=()),
        post=_requires_empty(),
    ),
    Fixture(
        name="adb_uninstall_echo",
        emulator_only=True,
        record_both_streams=True,
        args=["uninstall", ECHO_TEXT],
        command="adb uninstall '<echo text>'",
        description=(
            "app_launcher --uninstall with <echo text> (see pm_grant_permission_echo) as the "
            "package, passed as app_launcher passes it: 'Failure "
            "[DELETE_FAILED_INTERNAL_ERROR]' on STDOUT, exit 1, stderr empty; the name is "
            "not printed back."
        ),
        capture_with=_echo_capture(["uninstall", ECHO_TEXT], on=()),
        post=_requires("DELETE_FAILED"),
    ),
    Fixture(
        name="resolve_activity_echo",
        emulator_only=True,
        record_both_streams=True,
        args=[
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-c",
            "android.intent.category.LAUNCHER",
            shlex.quote(ECHO_TEXT),
        ],
        command=(
            "adb shell cmd package resolve-activity --brief -c "
            "android.intent.category.LAUNCHER '<echo text>'"
        ),
        description=(
            "app_launcher's launcher-activity lookup for a package named by <echo text> "
            "(see pm_grant_permission_echo): 'No activity found' on STDOUT, exit 0; the "
            "name is not printed back."
        ),
        capture_with=_echo_capture(
            [
                "shell",
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-c",
                "android.intent.category.LAUNCHER",
                shlex.quote(ECHO_TEXT),
            ],
            on=(),
        ),
        post=_requires("No activity found"),
    ),
    Fixture(
        name="pm_dump_echo",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "pm", "dump", shlex.quote(ECHO_TEXT), "|", "head", "-n", "3"],
        command="adb shell pm dump '<echo text>' | head -n 3",
        description=(
            "device_utils.get_package_info's `pm dump` for a package named by <echo text> "
            "(see pm_grant_permission_echo): 'Unable to find package: <name>' on STDOUT, "
            "exit 0. Only the first three lines are kept, by a device-side `head`: after "
            "them `pm dump` dumps every other service's state, which is not this fixture's "
            "to publish. The pipe takes stdout only, so stderr's size is pm's own (0)."
        ),
        capture_with=_echo_capture(
            ["shell", "pm", "dump", shlex.quote(ECHO_TEXT), "|", "head", "-n", "3"],
            on=("stdout",),
        ),
        post=_requires("Unable to find package", ECHO_FORGED),
    ),
    Fixture(
        name="cmd_notification_post_echo",
        emulator_only=True,
        record_both_streams=True,
        args=[
            "shell",
            "cmd",
            "notification",
            "post",
            shlex.quote(f"-{ECHO_TEXT}"),
            shlex.quote(ECHO_TEXT),
        ],
        command="adb shell cmd notification post '-<echo text>' '<echo text>'",
        description=(
            "push_notification --post with <echo text> (see pm_grant_permission_echo) as "
            "the text and, behind a `-`, as the tag -- the one way its two arguments made "
            "`cmd notification post` refuse. The refusal ('Error occurred. Check logcat for "
            'details. No argument expected after "<tag>"\') prints the tag back on STDOUT, '
            "exit 0, stderr empty; nothing is posted."
        ),
        capture_with=_echo_capture(
            [
                "shell",
                "cmd",
                "notification",
                "post",
                shlex.quote(f"-{ECHO_TEXT}"),
                shlex.quote(ECHO_TEXT),
            ],
            on=("stdout",),
        ),
        post=_requires("Error occurred", ECHO_FORGED),
    ),
    Fixture(
        name="dumpsys_package_permissions",
        args=["shell", "dumpsys", "package", "com.google.android.deskclock"],
        description=(
            "The permission state of an installed package, which is what "
            "`privacy_manager --list` parses and what proves a grant took "
            "effect. There is NO 'granted permissions:' section -- the header "
            "privacy_manager looked for, which is why --list always answered "
            "with two empty lists. The real headers, at four spaces of indent "
            "under `Package [<pkg>]`, are 'declared permissions:' "
            "(name: prot=...), 'requested permissions:' (bare names), "
            "'install permissions:' (name: granted=<bool>), and -- nested one "
            "level deeper, under 'User 0:' -- 'runtime permissions:' "
            "(name: granted=<bool>, flags=[...]).\n"
            "Deskclock is recorded rather than the fixture app because it is "
            "an UPDATED system app, so the dump repeats all four sections a "
            "second time under the top-level 'Hidden system packages:' header. "
            "Measured: that copy is NOT stale -- granting READ_CALENDAR flips "
            "`granted` in both copies -- so the cost of reading it is a "
            "doubled list rather than a wrong value, and taking the first "
            "occurrence of each section is what keeps it out. It also carries "
            "both outcomes: POST_NOTIFICATIONS granted=true, READ_CALENDAR and "
            "READ_MEDIA_AUDIO granted=false."
        ),
        catches=("S15",),
        timeout=60,
        post=_requires(
            "install permissions:",
            "runtime permissions:",
            "Hidden system packages:",
            "android.permission.POST_NOTIFICATIONS: granted=true",
            "android.permission.READ_CALENDAR: granted=false",
            absent=("granted permissions:",),
        ),
    ),
    Fixture(
        name="dumpsys_package_unknown",
        args=["shell", "dumpsys package com.example.not.installed; echo exit=$?"],
        description=(
            "`dumpsys package` asked about a package that is not installed: "
            "one line, 'Unable to find package: X', and exit status 0. So "
            "`--list` cannot tell 'not installed' from 'installed and holds "
            "nothing' by exit status, and an empty parse must not be printed "
            "as an answer. The structural signal is the absence of the "
            "top-level 'Packages:' block. (The echo is part of the recorded "
            "command because a file cannot show an exit status.)"
        ),
        post=_requires("Unable to find package", "exit=0"),
    ),
    Fixture(
        name="dumpsys_package_shared_uid",
        args=["shell", "dumpsys", "package", "com.android.localtransport"],
        description=(
            "The other place `install permissions:` shows up twice, and the "
            "one that costs data rather than adds it. A package in a shared "
            "uid has its runtime permission state tracked against the UID, so "
            "the dump prints it under the top-level 'Shared users:' header and "
            "NOT under 'Packages:' -- this package's Packages block has no "
            "'runtime permissions:' line at all, while the SharedUser "
            "[android.uid.system] block below it has twenty-three. A parser "
            "that reads only 'Packages:' (the obvious fix for the stale copy "
            "under 'Hidden system packages:') answers that a system app holds "
            "no runtime permissions whatsoever. com.android.settings has the "
            "same shape at five times the size."
        ),
        catches=("S15",),
        timeout=60,
        post=_requires(
            "Shared users:",
            "SharedUser [android.uid.system]",
            "runtime permissions:",
            "android.permission.CAMERA: granted=true",
        ),
    ),
    Fixture(
        name="dumpsys_package_after_silent_grant",
        emulator_only=True,
        args=["shell", "dumpsys", "package", COMPOSE_PACKAGE],
        setup=[
            ["shell", f"pm grant {COMPOSE_PACKAGE} android.permission.POST_NOTIFICATIONS"],
        ],
        description=(
            "The read-back that catches the no-op recorded as "
            "pm_grant_not_requested. Captured immediately AFTER that exact "
            "`pm grant` (exit 0, no output at all): the fixture app's "
            "'runtime permissions:' section is still empty and "
            "POST_NOTIFICATIONS appears nowhere in the dump. Exit status and "
            "printed output cannot tell a real grant from this one; the dump "
            "can, which is why grant/revoke now prove themselves by reading "
            "the state back."
        ),
        catches=("S16",),
        timeout=60,
        post=_requires(
            "runtime permissions:",
            "install permissions:",
            absent=("android.permission.POST_NOTIFICATIONS",),
        ),
    ),
    Fixture(
        name="cmd_notification_post_rejected",
        emulator_only=True,
        args=[
            "shell",
            "cmd notification post -i nonsense://x fixturetag hello 2>&1; echo exit=$?",
        ],
        description=(
            "`cmd notification post` REJECTING its arguments, and still exiting "
            "0. Probed every way it can fail -- unknown option, missing "
            "argument, bad icon, bad style, bad picture spec, no arguments at "
            "all -- and the exit status was 0 every time. So a non-zero exit "
            "from this command is not a case that exists, which is what "
            "push_notification's tests used to assert against. The real trap is "
            "this one: an error on the output stream at a successful exit "
            "status. (The trailing echo is part of the recorded command because "
            "a file cannot show an exit status; everything before 'exit=' is "
            "the command's own output.)"
        ),
        post=_requires("exit=0"),
    ),
    Fixture(
        name="cmd_notification_post_usage",
        emulator_only=True,
        args=["shell", "cmd notification post"],
        description=(
            "`cmd notification post` with its arguments omitted prints its "
            "usage block and EXITS 0 -- the same trap as `am broadcast` always "
            "printing 'result=0'. Anything deciding a post succeeded from the "
            "exit status cannot tell a posted notification from a rejected "
            "command line. The usage block is also the authoritative flag list: "
            "there is no --channel flag, so the channel a post lands on is not "
            "selectable here."
        ),
        post=_requires("usage: cmd notification post"),
    ),
    Fixture(
        name="uiautomator_dump_raw",
        args=["exec-out", "uiautomator", "dump", "/dev/tty"],
        # Declared "xml" to match the file already on disk, not because it is
        # one -- this capture is the raw stream WITH uiautomator's trailing
        # status line, so it is not a parseable document. It had declared
        # ext="txt" while being written as .xml, because _record_one derived
        # the extension from the name prefix and ignored this field entirely.
        # Honouring the field exposed the contradiction; renaming the fixture
        # to .txt is a separate change from the one this commit is making.
        ext="xml",
        description=(
            "The dump exactly as `exec-out` delivers it, status line included. "
            "The existing uiautomator_* fixtures hold only the XML, so the "
            "framing was never recorded and tests asserted it as an inline "
            "literal -- guessing tool output, the exact mistake this corpus "
            "exists to prevent. The measured shape: uiautomator appends "
            "'UI hierchary dumped to: /dev/tty' (Android's typo, not ours) "
            "directly onto the XML with NO separating newline, on stdout. A "
            "test that writes it with a '\\n' is testing something the device "
            "never produces."
        ),
    ),
    # --- am profile: the no-root sampling path, and the two it refuses ------
    Fixture(
        name="am_profile_hang",
        emulator_only=True,
        args=[],
        ext="trace",
        command=(
            f"adb {' '.join(_profile_start(TRACE_DEVICE_PATH))}; "
            f"adb {' '.join(_PROVOKE_ANR)}; "
            f"wait until {TRACE_STOP_AFTER_S}s after the broadcast; "
            f"adb {' '.join(_PROFILE_STOP)}; "
            f"adb exec-out cat {TRACE_DEVICE_PATH}"
        ),
        capture_bytes=_profile_hang_trace,
        description=(
            "An ART method trace of the fixture app, sampled every 1000 us by "
            "`am profile start --sampling` while AnrReceiver held its main "
            "thread in Thread.sleep for 40 s -- a hung app, seen from inside "
            "without root on a `user` build. The ground truth the v0.9.0 hang "
            "sampler is parsed against (vault: 'Sampling a Hung App Without "
            "Root'). BINARY, written byte for byte: a text header (`*version` "
            "/ `3` / key=value lines including `clock=dual` / `*threads` / "
            "`*methods` / `*end`), then a little-endian body starting with "
            "the magic `SLOW`. What a parser written from memory gets wrong: "
            "the records are TRANSITIONS, not samples -- a 40 s block is one "
            "enter and one exit 40 s apart, not 40,000 rows; the stack the "
            "thread already had when profiling began is replayed as enters at "
            "the first sample; and under clock=dual each record carries the "
            "thread-CPU clock BEFORE the wall clock, so across the sleep the "
            "CPU clock moves by milliseconds and only the wall clock shows the "
            "hang. Thread.sleep appears three times nested (three overloads), "
            "so 'the frame under the sleep' is the first frame that is not a "
            "Thread.sleep. Taken 10 s after launch, so that Jetpack "
            "ProfileInstaller's one-shot worker thread has come and gone "
            "first: in 1 of 3 recorder runs on 2026-10-16 the app aborted "
            "(ART thread.cc:2698 CHECK) as that worker exited under the "
            "sampler (see TRACE_QUIET_BEFORE_S). Note too that `am "
            "broadcast` did not return until the receiver timed out "
            "(broadcast_returned_after_s)."
        ),
        setup=[
            *_LAUNCH_COMPOSE,
            ["shell", "rm", "-f", TRACE_DEVICE_PATH],
            ["shell", "sleep", TRACE_QUIET_BEFORE_S],
        ],
        teardown=[
            _PROFILE_STOP,
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["shell", "rm", "-f", TRACE_DEVICE_PATH],
        ],
        teardown_verify=_device_path_absent(["shell", "ls", TRACE_DEVICE_PATH]),
    ),
    Fixture(
        name="am_profile_sdcard_denied",
        emulator_only=True,
        args=_profile_start("/sdcard/aes_denied.trace"),
        capture_with=_profile_refusal(
            "/sdcard/aes_denied.trace", ["shell", "ls", "-la", "/sdcard/aes_denied.trace"]
        ),
        record_both_streams=True,
        description=(
            "`am profile start` to a path on shared storage. Refused: exit "
            "**255**, stdout empty, and on stderr an SELinux denial "
            "(`avc:  denied  { write } ... scontext=u:r:system_server:s0 "
            "tcontext=u:object_r:fuse:s0`) RUN TOGETHER with the next sentence "
            "on the same line, no newline between them, then 'Error: Unable "
            "to open file: <path>' and 'Consider using a file under "
            "/data/local/tmp/'. It is system_server that opens the file, not "
            "the app and not the shell, which is why /sdcard -- writable from "
            "`adb shell` -- is still refused. The refusal LEAVES A 0-BYTE FILE "
            "behind (see left_behind in this entry), so a caller that takes "
            "'the trace file exists' as proof profiling started is fooled; "
            "the exit status is the only reliable signal."
        ),
        setup=[*_LAUNCH_COMPOSE, ["shell", "rm", "-f", "/sdcard/aes_denied.trace"]],
        teardown=[
            _PROFILE_STOP,
            ["shell", "rm", "-f", "/sdcard/aes_denied.trace"],
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
        ],
        teardown_verify=_device_path_absent(["shell", "ls", "/sdcard/aes_denied.trace"]),
        post=_requires("Unable to open file", "Consider using a file under /data/local/tmp/"),
    ),
    Fixture(
        name="am_profile_appdir_denied",
        emulator_only=True,
        args=_profile_start(f"/data/data/{COMPOSE_PACKAGE}/files/aes_denied.trace"),
        capture_with=_profile_refusal(
            f"/data/data/{COMPOSE_PACKAGE}/files/aes_denied.trace",
            ["shell", "run-as", COMPOSE_PACKAGE, "ls", "-la", "files/aes_denied.trace"],
        ),
        record_both_streams=True,
        description=(
            "`am profile start` to a path in the profiled app's OWN data "
            "directory -- the obvious place, since the app is the one writing "
            "the trace. Refused just the same: exit **255**, stdout empty, "
            "'Error: Unable to open file: <path>' and 'Consider using a file "
            "under /data/local/tmp/' on stderr, and no avc line this time. "
            "Unlike /sdcard it leaves nothing behind (left_behind: the app's "
            "own `run-as ls` reports No such file). Together with "
            "am_profile_sdcard_denied this pins both wrong destinations as "
            "refused, and the refusal text names the one that works."
        ),
        setup=[
            *_LAUNCH_COMPOSE,
            ["shell", "run-as", COMPOSE_PACKAGE, "rm", "-f", "files/aes_denied.trace"],
        ],
        teardown=[
            _PROFILE_STOP,
            ["shell", "run-as", COMPOSE_PACKAGE, "rm", "-f", "files/aes_denied.trace"],
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
        ],
        teardown_verify=_device_path_absent(
            ["shell", "run-as", COMPOSE_PACKAGE, "ls", "files/aes_denied.trace"]
        ),
        post=_requires("Unable to open file", "Consider using a file under /data/local/tmp/"),
    ),
    # --- run-as: the container/model-inspector ground truth -----------------
    Fixture(
        name="run_as_not_an_application",
        args=["shell", "run-as com.android.settings ls 2>&1"],
        description=(
            "run-as refused for a system package. The real wording is "
            "'run-as: package not an application: <pkg>'. container.py's denial "
            "markers listed 'is not an application' -- with an 'is' that the "
            "platform does not print -- so this denial fell through to a "
            "generic 'Command failed' instead of the hint telling the user the "
            "app must be debuggable. Written from imagination, wrong in exactly "
            "the way this corpus exists to catch."
        ),
    ),
    Fixture(
        name="run_as_unknown_package",
        args=["shell", "run-as com.nonexistent.app ls 2>&1"],
        description=(
            "run-as refused for a package that is not installed: "
            "'run-as: unknown package: <pkg>'. This one the marker list does "
            "match, which is why the actionable hint appears here and not for "
            "run_as_not_an_application -- the contrast is the point."
        ),
    ),
    Fixture(
        name="run_as_not_an_application_stderr",
        record_both_streams=True,
        args=["shell", "run-as", "com.android.settings", "ls"],
        description=(
            "run_as_not_an_application without its `2>&1`, so the manifest says "
            "where run-as puts a refusal: on STDERR, stdout empty, exit 1, one "
            "line starting `run-as: `. container.py now takes only such a line "
            "for a run-as denial. It used to scan every stream for markers, and "
            "`ls: <dir>: Permission denied` (run_as_ls_shared_prefs_unreadable) "
            "matched 'permission denied' and was given the debug-build hint."
        ),
        post=_requires("run-as: package not an application: com.android.settings"),
    ),
    Fixture(
        name="run_as_unknown_package_stderr",
        record_both_streams=True,
        args=["shell", "run-as", "com.nonexistent.app", "ls"],
        description=(
            "run_as_unknown_package without its `2>&1`: on STDERR, stdout empty, "
            "exit 1, `run-as: unknown package: <pkg>` (the package name aliased "
            "like any non-AOSP name)."
        ),
        post=_requires("run-as: unknown package: "),
    ),
    # `run-as <pkg> id`: the independent probe common.run_as asks before it
    # assigns the debug-build remedy. `id` takes no argument, so nothing in its
    # answer is a name an app chose -- unlike `cat <file>`, whose stderr carries
    # the file name, and a name can hold a newline and a forged `run-as:` line.
    Fixture(
        name="run_as_id",
        record_both_streams=True,
        args=["shell", "-T", "run-as", COMPOSE_PACKAGE, "id"],
        description=(
            "`adb shell -T run-as <pkg> id` for the debuggable fixture app: exit 0, "
            "stderr EMPTY, one line on stdout starting `uid=<n>(<name>)` -- the "
            "app's uid, run-as having switched to it. The probe's 'run-as works "
            "for this package' answer: a stderr line that only looks like a run-as "
            "refusal (a file name holding one) is overturned by it."
        ),
        post=_requires("uid=", " gid=", " context="),
    ),
    Fixture(
        name="run_as_id_not_an_application",
        record_both_streams=True,
        args=["shell", "-T", "run-as", "com.android.settings", "id"],
        description=(
            "The probe for a system package: exit 1, stdout EMPTY, the whole of "
            "stderr one line, `run-as: package not an application: <pkg>` -- the "
            "same refusal run_as_not_an_application_stderr recorded for `ls`, "
            "because run-as refuses before it runs any command."
        ),
        post=_requires("run-as: package not an application: com.android.settings\n"),
    ),
    Fixture(
        name="run_as_id_not_debuggable",
        record_both_streams=True,
        args=["shell", "-T", "run-as", "com.android.chrome", "id"],
        description=(
            "The probe for an installed APPLICATION (uid >= 10000) built without "
            "android:debuggable: exit 1, stdout EMPTY, `run-as: package not "
            "debuggable: <pkg>` on stderr. The refusal a release build of the "
            "user's own app gets, and the one no other recording held. "
            "com.android.chrome because it is on every Google APIs image and is "
            "not debuggable there."
        ),
        post=_requires("run-as: package not debuggable: com.android.chrome\n"),
    ),
    Fixture(
        name="run_as_id_unknown_package",
        record_both_streams=True,
        args=["shell", "-T", "run-as", "com.nonexistent.app", "id"],
        description=(
            "The probe for a package that is not installed: exit 1, stdout EMPTY, "
            "`run-as: unknown package: <pkg>` on stderr (the name aliased like any "
            "non-AOSP name)."
        ),
        post=_requires("run-as: unknown package: "),
    ),
    Fixture(
        name="run_as_ls_data_dir",
        args=["shell", "run-as com.example.composefixture ls -la"],
        description=(
            "A real `run-as ls -la` of an app's data dir, from the Compose "
            "fixture app. Note what a hand-written sample gets wrong: a "
            "'total N' header line, '.' and '..' entries, variable-width column "
            "padding, and a group that differs from the owner "
            "(u0_a205 vs u0_a205_cache on cache dirs)."
        ),
    ),
    Fixture(
        name="shared_prefs_settings_xml",
        args=[
            "shell",
            "run-as com.example.composefixture cat shared_prefs/fixture_settings.xml",
        ],
        description=(
            "Real SharedPreferences XML from the Compose fixture app, covering "
            "every type Android encodes differently: string, int, long, float, "
            "boolean and a string set. A parser that only ever saw <string> is "
            "a parser that has not been tested -- and the hand-written sample "
            "this replaces had exactly that shape. Note the set is nested "
            "<string> children rather than an attribute, and the entries are "
            "not in insertion order."
        ),
    ),
    Fixture(
        name="run_as_ls_databases",
        args=["shell", "run-as com.example.composefixture ls -la databases"],
        description=(
            "The databases directory. Carries the -journal file alongside the "
            "database, which anything listing databases has to not offer as a "
            "database."
        ),
    ),
    Fixture(
        name="run_as_ls_symlink_space",
        emulator_only=True,
        args=[
            "shell",
            f"run-as {COMPOSE_PACKAGE} sh -c 'mkdir -p files/aes_fx && cd files/aes_fx "
            f'&& echo x > "a b.txt" && ln -s "a b.txt" link && ls -la\'',
        ],
        description=(
            "`run-as ls -la` of a directory holding a filename with a space and "
            "a symlink to it, made for the purpose in the fixture app's files/ "
            "and removed after. The three things the `ls -la` parser documented "
            "and no other recording had (both inspectors list with find now: "
            "run_as_find_names). What the device actually prints: toybox's "
            "ISO layout ('YYYY-MM-DD HH:MM', two tokens) -- NOT the coreutils "
            "'Mon DD YYYY' layout the parser also claimed, which this device's "
            "`ls` does not produce and no caller of the parser fed it (all "
            "three ran `run-as ... ls -la` on the device); the symlink as "
            "'link -> a b.txt', size 7 (the "
            "length of the target string), mode lrwxrwxrwx; and the spaced "
            "name printed bare, unquoted and unescaped, so the name is "
            "everything after the time column. Note the entries are mode 0666 "
            "and the directory 0777: run-as's shell has a umask of 0."
        ),
        setup=[["shell", f"run-as {COMPOSE_PACKAGE} rm -rf files/aes_fx"]],
        teardown=[["shell", f"run-as {COMPOSE_PACKAGE} rm -rf files/aes_fx"]],
        teardown_verify=_device_path_absent(["shell", f"run-as {COMPOSE_PACKAGE} ls files/aes_fx"]),
        post=_requires("link -> a b.txt", "\n-rw"),
    ),
    Fixture(
        name="run_as_cat_missing",
        args=["shell", "-T", "run-as", COMPOSE_PACKAGE, "cat", "shared_prefs/aes_missing.xml"],
        record_both_streams=True,
        description=(
            "`adb shell -T run-as <pkg> cat` of a file that does not exist: exit "
            "1, stdout EMPTY, the reason on stderr. Over `adb exec-out` the same "
            "read exits 0 and the reason arrives on STDOUT -- as the file's "
            "content -- which is how container.py's --cat answered a missing "
            "file with the error text as its content, and export wrote it into "
            "a snapshot as a prefs file (measured on emulator-5554). `-T` (no "
            "pty) leaves a successful read's bytes as exec-out delivers them: "
            "fixture.db, 32768 bytes, cmp-identical over both."
        ),
    ),
    Fixture(
        name="run_as_cat_missing_exec_out",
        args=["exec-out", "run-as", COMPOSE_PACKAGE, "cat", "shared_prefs/aes_missing.xml"],
        record_both_streams=True,
        description=(
            "The same missing-file read as run_as_cat_missing, over `adb exec-out`: "
            "exit 0, stderr EMPTY, and the reason on STDOUT where the file's bytes "
            "would be. Nothing in the result says the read failed, so a caller that "
            "reads files this way takes the error text for the file -- the transport "
            "container.py's --cat, --shared-prefs NAME and --export used."
        ),
    ),
    Fixture(
        name="run_as_cat_utf8_cap",
        emulator_only=True,
        args=_read_file(UTF8_CAP_PATH),
        command=(
            f"adb push <{len(_utf8_cap_text())} bytes of UTF-8 text> {UTF8_CAP_STAGED}; "
            f"adb shell -T run-as {COMPOSE_PACKAGE} cp {UTF8_CAP_STAGED} {UTF8_CAP_PATH}; "
            f"adb {' '.join(_read_file(UTF8_CAP_PATH))}"
        ),
        prepare=_push_utf8_cap,
        capture_bytes=_utf8_cap_read,
        catches=("D10",),
        description=(
            "container.py's --cat read (`adb shell -T run-as <pkg> cat <absolute "
            "path>`) of a UTF-8 text file longer than its 65,536-byte cap, pushed "
            "into the fixture app's files/ for the purpose and removed after. "
            "Mixed-script lines (German, Japanese, accented Latin), with the header "
            "padded so the cap cuts a three-byte character after its second byte "
            "(manifest: cap_bytes, split_character_starts_at). Written byte for "
            "byte and refused unless it is exactly the file pushed. Slicing to the "
            "cap and strict-decoding the slice calls this file binary: the only "
            "invalid bytes are the incomplete character the cut itself made."
        ),
        setup=[
            ["shell", "run-as", COMPOSE_PACKAGE, "rm", "-f", UTF8_CAP_PATH],
            ["shell", "rm", "-f", UTF8_CAP_STAGED],
        ],
        teardown=[
            ["shell", "run-as", COMPOSE_PACKAGE, "rm", "-f", UTF8_CAP_PATH],
            ["shell", "rm", "-f", UTF8_CAP_STAGED],
        ],
        teardown_verify=_all_of(
            _device_path_absent(["shell", "run-as", COMPOSE_PACKAGE, "ls", UTF8_CAP_PATH]),
            _device_path_absent(["shell", "ls", UTF8_CAP_STAGED]),
        ),
    ),
    Fixture(
        name="run_as_cat_fixture_db",
        emulator_only=True,
        args=_read_file(f"{COMPOSE_DATA_DIR}/databases/fixture.db"),
        ext="db",
        capture_bytes=_sqlite_read,
        catches=("D10",),
        description=(
            "container.py's --cat read of the fixture app's databases/fixture.db: "
            "a binary file, read by the same argv as run_as_cat_utf8_cap and "
            "written byte for byte. Its first byte that is not valid UTF-8 is "
            "early in the SQLite header, so it is binary whole and binary when "
            "capped anywhere past that byte; forgiving the character a cap cuts "
            "must not forgive this. emulator_only only because a capture "
            "callable reads it; it changes nothing."
        ),
    ),
    Fixture(
        name="run_as_ls_data_dir_seeded",
        args=["shell", "run-as", COMPOSE_PACKAGE, "ls", "-la", COMPOSE_DATA_DIR],
        description=(
            "The fixture app's data dir listed as container.py listed it until it "
            "listed with find (run_as_find_data_dir), by its absolute path, after "
            "the app has run: shared_prefs/ and databases/ "
            "are in it. run_as_ls_data_dir was recorded before the app first ran "
            "and holds neither -- the listing of an app with no prefs and no "
            "databases, which --export reports as absent, not as a failure."
        ),
        post=_requires(" shared_prefs\n", " databases\n"),
    ),
    Fixture(
        name="run_as_ls_shared_prefs",
        args=["shell", "run-as", COMPOSE_PACKAGE, "ls", "-la", f"{COMPOSE_DATA_DIR}/shared_prefs"],
        description=(
            "`ls -la` of the fixture app's shared_prefs/, as --shared-prefs and "
            "--export listed it before find: one prefs file, fixture_settings.xml (the file "
            "shared_prefs_settings_xml holds)."
        ),
        post=_requires(" fixture_settings.xml\n"),
    ),
    Fixture(
        name="run_as_ls_shared_prefs_absent",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "run-as", COMPOSE_PACKAGE, "ls", "-la", f"{COMPOSE_DATA_DIR}/shared_prefs"],
        description=(
            "The same listing while shared_prefs/ is moved aside (renamed "
            "shared_prefs.aes_moved inside the data dir, and back after): exit 1, "
            "stdout EMPTY, `ls: <path>: No such file or directory` on stderr. "
            "--export took any failed sub-listing for an empty directory and "
            "reported success; a directory the data-dir listing names and this "
            "listing cannot find is a listing that failed."
        ),
        setup=[
            [
                "shell",
                "run-as",
                COMPOSE_PACKAGE,
                "mv",
                "-T",
                "shared_prefs",
                "shared_prefs.aes_moved",
            ]
        ],
        teardown=[
            [
                "shell",
                "run-as",
                COMPOSE_PACKAGE,
                "mv",
                "-T",
                "shared_prefs.aes_moved",
                "shared_prefs",
            ]
        ],
        teardown_verify=_app_dir_restored("shared_prefs", "fixture_settings.xml"),
        post=_requires("No such file or directory"),
    ),
    Fixture(
        name="run_as_ls_shared_prefs_unreadable",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "run-as", COMPOSE_PACKAGE, "ls", "-la", f"{COMPOSE_DATA_DIR}/shared_prefs"],
        description=(
            "The same listing with the owner's read bit taken off shared_prefs/ "
            "(`chmod u-r`, and `u+r` after; the app's uid is the owner, so the "
            "group's r does not apply): exit 1, stdout EMPTY, `ls: <path>: "
            "Permission denied` on stderr. The reviewer's reproduction of export "
            "reporting success with unread 0 over exactly this. Note the text "
            "matches container.py's run-as denial marker 'permission denied'."
        ),
        setup=[["shell", "run-as", COMPOSE_PACKAGE, "chmod", "u-r", "shared_prefs"]],
        teardown=[["shell", "run-as", COMPOSE_PACKAGE, "chmod", "u+r", "shared_prefs"]],
        teardown_verify=_app_dir_restored("shared_prefs", "fixture_settings.xml"),
        post=_requires("Permission denied"),
    ),
    Fixture(
        name="run_as_ls_databases_unreadable",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "run-as", COMPOSE_PACKAGE, "ls", "-la", f"{COMPOSE_DATA_DIR}/databases"],
        description=(
            "run_as_ls_shared_prefs_unreadable for databases/ (`chmod u-r`, then "
            "`u+r`). Taking only the read bit leaves the directory searchable, so "
            "an open database is undisturbed. --export wrote this failure into "
            "databases.json and still reported success."
        ),
        setup=[["shell", "run-as", COMPOSE_PACKAGE, "chmod", "u-r", "databases"]],
        teardown=[["shell", "run-as", COMPOSE_PACKAGE, "chmod", "u+r", "databases"]],
        teardown_verify=_app_dir_restored("databases", "fixture.db"),
        post=_requires("Permission denied"),
    ),
    Fixture(
        name="run_as_ls_1_databases",
        args=["shell", "run-as", COMPOSE_PACKAGE, "ls", "-1", f"{COMPOSE_DATA_DIR}/databases"],
        description=(
            "`ls -1` of databases/, as model_inspector's live mode first listed it: one "
            "name per line, no `.`/`..`, and the zero-byte fixture.db-journal "
            "beside fixture.db -- not the -wal/-shm pair a hand-written listing "
            "had in its place."
        ),
        post=_requires("fixture.db\n", "fixture.db-journal\n"),
    ),
    Fixture(
        name="run_as_ls_1_databases_unreadable",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "run-as", COMPOSE_PACKAGE, "ls", "-1", f"{COMPOSE_DATA_DIR}/databases"],
        description=(
            "model_inspector's `ls -1` of databases/ with the owner's read bit "
            "taken off it (`chmod u-r`, `u+r` after; run_as_ls_databases_unreadable "
            "is the `ls -la` container.py ran): exit 1, stdout EMPTY, `ls: <path>: "
            "Permission denied` on stderr -- which matched the run-as denial "
            "marker 'permission denied' and was answered with the debug-build hint."
        ),
        setup=[["shell", "run-as", COMPOSE_PACKAGE, "chmod", "u-r", "databases"]],
        teardown=[["shell", "run-as", COMPOSE_PACKAGE, "chmod", "u+r", "databases"]],
        teardown_verify=_app_dir_restored("databases", "fixture.db"),
        post=_requires("Permission denied"),
    ),
    # --- The listing both inspectors run now (common.run_as.listing_command) ---
    Fixture(
        name="run_as_find_databases",
        record_both_streams=True,
        args=_listing(f"{COMPOSE_DATA_DIR}/databases"),
        description=(
            "The fixture app's databases/ as container.py --databases and model_inspector's "
            "live mode list it: toybox find, six NUL-terminated fields per record (mode, "
            "owner, group, size, symlink target, name), the directory's own record first "
            "with an EMPTY name, then fixture.db and the zero-byte fixture.db-journal in "
            "directory order. Exit 0, stderr empty."
        ),
        post=_requires("\0\0\0", "\0fixture.db\0", "\0fixture.db-journal\0"),
    ),
    Fixture(
        name="run_as_find_data_dir",
        record_both_streams=True,
        args=_listing(COMPOSE_DATA_DIR),
        description=(
            "The fixture app's data dir as container.py --ls and --export list it, after "
            "the app has run: shared_prefs/ and databases/ are in it, and cache/ and "
            "code_cache/ have a group (u0_aNNN_cache) that is not their owner."
        ),
        post=_requires("\0shared_prefs\0", "\0databases\0", "\0cache\0", "_cache\0"),
    ),
    Fixture(
        name="run_as_find_shared_prefs",
        record_both_streams=True,
        args=_listing(f"{COMPOSE_DATA_DIR}/shared_prefs"),
        description=(
            "shared_prefs/ as --shared-prefs and --export list it: the directory's own "
            "record and fixture_settings.xml (the file shared_prefs_settings_xml holds)."
        ),
        post=_requires("\0fixture_settings.xml\0"),
    ),
    Fixture(
        name="run_as_find_data_dir_bare",
        emulator_only=True,
        record_both_streams=True,
        args=_listing(COMPOSE_DATA_DIR),
        description=(
            "The data-dir listing with shared_prefs/ AND databases/ moved aside (renamed "
            "<dir>.aes_moved, the app force-stopped first, and both moved back after): a "
            "real listing that names neither directory -- what --export reads for an app "
            "with no prefs and no databases, and reports as absent, not as a failure."
        ),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            [
                "shell",
                "run-as",
                COMPOSE_PACKAGE,
                "mv",
                "-T",
                "shared_prefs",
                "shared_prefs.aes_moved",
            ],
            ["shell", "run-as", COMPOSE_PACKAGE, "mv", "-T", "databases", "databases.aes_moved"],
        ],
        teardown=[
            [
                "shell",
                "run-as",
                COMPOSE_PACKAGE,
                "mv",
                "-T",
                "shared_prefs.aes_moved",
                "shared_prefs",
            ],
            ["shell", "run-as", COMPOSE_PACKAGE, "mv", "-T", "databases.aes_moved", "databases"],
        ],
        teardown_verify=_all_of(
            _app_dir_restored("shared_prefs", "fixture_settings.xml"),
            _app_dir_restored("databases", "fixture.db"),
        ),
        post=_requires(
            "\0shared_prefs.aes_moved\0",
            "\0databases.aes_moved\0",
            absent=("\0shared_prefs\0", "\0databases\0"),
        ),
    ),
    Fixture(
        name="run_as_find_names",
        emulator_only=True,
        record_both_streams=True,
        args=_listing(NAMES_DIR),
        description=(
            "The listing of a scratch directory in the fixture app's files/ (made for the "
            "purpose, removed after) holding the names a listing must keep whole: "
            "`x.db` + LF + a complete `ls -la` line for ghost.db, `c\\d.db`, `e f.db`, "
            "`link -> e f.db`, `lflink` whose target holds a LF, `sub/` and `dirlink -> "
            "sub`. Each is ONE "
            "record here, its name the last of six NUL-terminated fields; the forged line "
            "is inside x.db's name, and no record names ghost.db. Compare run_as_ls_names, "
            "the same directory through `ls -la`."
        ),
        setup=_NAMES_MADE,
        teardown=_NAMES_REMOVED,
        teardown_verify=_NAMES_GONE,
        post=_requires(
            "\0x.db\n-rw-rw---- 1 u0_a205 u0_a205 32768 2026-10-18 22:28 ghost.db\0",
            "\0c\\d.db\0",
            "\0e f.db\0link\0",
            "\0dangling\ntarget\0lflink\0",
            "\0sub\0",
        ),
    ),
    Fixture(
        name="run_as_find_names_dirlink",
        emulator_only=True,
        record_both_streams=True,
        args=_listing(f"{NAMES_DIR}/dirlink"),
        description=(
            "The listing aimed at run_as_find_names's `dirlink`, a symlink to its `sub/` "
            "(which holds `inner`): find's -H follows the named path, so the first record is "
            "the directory's own -- mode d, its %l still the link's target `sub` -- and "
            "`inner` follows, as `ls sub` would list. Without -H find printed the link's "
            "record alone (mode l), which would read as not a directory."
        ),
        setup=_NAMES_MADE,
        teardown=_NAMES_REMOVED,
        teardown_verify=_NAMES_GONE,
        post=_requires("\0sub\0\0", "\0inner\0"),
    ),
    Fixture(
        name="run_as_ls_names",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "run-as", COMPOSE_PACKAGE, "ls", "-la", NAMES_DIR],
        description=(
            "run_as_find_names's directory through `ls -la`, what both inspectors listed "
            "with until it was replaced: x.db's name prints as two lines, and the second "
            "is a complete, well-formed entry for ghost.db, a file that does not exist -- "
            "the forgery the final review of v0.8.0 reproduced (REQUIRED 4). lflink's "
            "target prints as two lines too."
        ),
        setup=_NAMES_MADE,
        teardown=_NAMES_REMOVED,
        teardown_verify=_NAMES_GONE,
        post=_requires(" x.db\n-rw-rw---- 1 u0_a205 u0_a205 32768 2026-10-18 22:28 ghost.db\n"),
    ),
    Fixture(
        name="run_as_find_shared_prefs_absent",
        emulator_only=True,
        record_both_streams=True,
        args=_listing(f"{COMPOSE_DATA_DIR}/shared_prefs"),
        description=(
            "The shared_prefs/ listing while the directory is moved aside (renamed "
            "shared_prefs.aes_moved, and back after): exit 1, stdout EMPTY, and on stderr "
            "`find: '<path>': No such file or directory` -- the path in single quotes, "
            "which ls never printed. The first line names the path the listing named."
        ),
        setup=[
            [
                "shell",
                "run-as",
                COMPOSE_PACKAGE,
                "mv",
                "-T",
                "shared_prefs",
                "shared_prefs.aes_moved",
            ]
        ],
        teardown=[
            [
                "shell",
                "run-as",
                COMPOSE_PACKAGE,
                "mv",
                "-T",
                "shared_prefs.aes_moved",
                "shared_prefs",
            ]
        ],
        teardown_verify=_app_dir_restored("shared_prefs", "fixture_settings.xml"),
        post=_requires("No such file or directory"),
    ),
    Fixture(
        name="run_as_find_shared_prefs_unreadable",
        emulator_only=True,
        record_both_streams=True,
        args=_listing(f"{COMPOSE_DATA_DIR}/shared_prefs"),
        description=(
            "The shared_prefs/ listing with the owner's read bit taken off it (`chmod u-r`, "
            "`u+r` after): exit 1, and find prints on BOTH streams -- the directory's own "
            "record on stdout (it could stat it), then `find: <path>: Permission denied`, "
            "unquoted, on stderr (run_as_find_shared_prefs_unreadable_stderr, same run)."
        ),
        setup=[["shell", "run-as", COMPOSE_PACKAGE, "chmod", "u-r", "shared_prefs"]],
        teardown=[["shell", "run-as", COMPOSE_PACKAGE, "chmod", "u+r", "shared_prefs"]],
        teardown_verify=_app_dir_restored("shared_prefs", "fixture_settings.xml"),
        capture_with=_both_streams(
            _listing(f"{COMPOSE_DATA_DIR}/shared_prefs"), "run_as_find_shared_prefs_unreadable"
        ),
        post=_requires("\0\0\0"),
    ),
    Fixture(
        name="run_as_find_shared_prefs_unreadable_stderr",
        emulator_only=True,
        args=_listing(f"{COMPOSE_DATA_DIR}/shared_prefs"),
        command="recorded in run_as_find_shared_prefs_unreadable's run: its stderr",
        description="The stderr of run_as_find_shared_prefs_unreadable's one run, byte for byte.",
        capture_bytes=_twin(
            "run_as_find_shared_prefs_unreadable_stderr", "run_as_find_shared_prefs_unreadable"
        ),
    ),
    Fixture(
        name="run_as_find_databases_unreadable",
        emulator_only=True,
        record_both_streams=True,
        args=_listing(f"{COMPOSE_DATA_DIR}/databases"),
        description=(
            "run_as_find_shared_prefs_unreadable for databases/ (`chmod u-r`, `u+r` after; "
            "the directory stays searchable, so an open database is undisturbed): exit 1, "
            "the directory's own record on stdout, `find: <path>: Permission denied` on "
            "stderr (run_as_find_databases_unreadable_stderr, same run)."
        ),
        setup=[["shell", "run-as", COMPOSE_PACKAGE, "chmod", "u-r", "databases"]],
        teardown=[["shell", "run-as", COMPOSE_PACKAGE, "chmod", "u+r", "databases"]],
        teardown_verify=_app_dir_restored("databases", "fixture.db"),
        capture_with=_both_streams(
            _listing(f"{COMPOSE_DATA_DIR}/databases"), "run_as_find_databases_unreadable"
        ),
        post=_requires("\0\0\0"),
    ),
    Fixture(
        name="run_as_find_databases_unreadable_stderr",
        emulator_only=True,
        args=_listing(f"{COMPOSE_DATA_DIR}/databases"),
        command="recorded in run_as_find_databases_unreadable's run: its stderr",
        description="The stderr of run_as_find_databases_unreadable's one run, byte for byte.",
        capture_bytes=_twin(
            "run_as_find_databases_unreadable_stderr", "run_as_find_databases_unreadable"
        ),
    ),
    Fixture(
        name="run_as_find_databases_unsearchable",
        emulator_only=True,
        record_both_streams=True,
        args=_listing(f"{COMPOSE_DATA_DIR}/databases"),
        description=(
            "The databases/ listing with the owner's SEARCH bit taken off it (`chmod u-x`, "
            "`u+x` after): find can read the directory's names but not stat them, so it "
            "exits 1 with the directory's own record on stdout and, on stderr "
            "(run_as_find_databases_unsearchable_stderr, same run), one line per entry, "
            "`find: '<dir>/<entry>': Permission denied` -- quoted, and naming the entry, "
            "not the directory. Its first line is the listing failing AT an entry."
        ),
        setup=[["shell", "run-as", COMPOSE_PACKAGE, "chmod", "u-x", "databases"]],
        teardown=[["shell", "run-as", COMPOSE_PACKAGE, "chmod", "u+x", "databases"]],
        teardown_verify=_app_dir_restored("databases", "fixture.db"),
        capture_with=_both_streams(
            _listing(f"{COMPOSE_DATA_DIR}/databases"), "run_as_find_databases_unsearchable"
        ),
        post=_requires("\0\0\0"),
    ),
    Fixture(
        name="run_as_find_databases_unsearchable_stderr",
        emulator_only=True,
        args=_listing(f"{COMPOSE_DATA_DIR}/databases"),
        command="recorded in run_as_find_databases_unsearchable's run: its stderr",
        description="The stderr of run_as_find_databases_unsearchable's one run, byte for byte.",
        capture_bytes=_twin(
            "run_as_find_databases_unsearchable_stderr", "run_as_find_databases_unsearchable"
        ),
    ),
    Fixture(
        name="run_as_find_not_a_directory",
        record_both_streams=True,
        args=_listing(f"{COMPOSE_DATA_DIR}/shared_prefs/fixture_settings.xml"),
        description=(
            "The listing aimed at a FILE (the fixture app's prefs file): exit 0, stderr "
            "empty, and one record -- the file's own, mode `-rw-...`, empty name. Without "
            "that record's mode to check, a listing of a file reads as an empty directory."
        ),
        post=_requires("\0\0\0"),
    ),
    Fixture(
        name="run_as_find_unknown_primary",
        record_both_streams=True,
        args=[
            "-printfx" if arg == "-printf" else arg
            for arg in _listing(f"{COMPOSE_DATA_DIR}/databases")
        ],
        description=(
            "The listing with `-printf` replaced by `-printfx`, a primary this find does "
            "not know -- the nearest measurable stand-in for a toybox without -printf: exit "
            "1, stdout EMPTY, `find: bad arg '<format>'` on stderr. It names no path; an "
            "older toybox's own wording is not measured."
        ),
        post=_requires("find: bad arg "),
    ),
    Fixture(
        name="run_as_find_unknown_escape",
        emulator_only=True,
        record_both_streams=True,
        args=_UNKNOWN_ESCAPE,
        description=(
            "The listing with its last escape, %P, replaced by %Q, which this find does not "
            "have -- the stand-in for a find lacking one of the six. find checks an escape "
            "only when it reaches it: exit 1, the directory's own record printed up to the "
            "bad escape (five fields) on stdout, then `find: bad -printf %Q` on stderr "
            "(run_as_find_unknown_escape_stderr, same run)."
        ),
        capture_with=_both_streams(_UNKNOWN_ESCAPE, "run_as_find_unknown_escape"),
        post=_requires("\0\0"),
    ),
    Fixture(
        name="run_as_find_unknown_escape_stderr",
        emulator_only=True,
        args=_UNKNOWN_ESCAPE,
        command="recorded in run_as_find_unknown_escape's run: its stderr",
        description="The stderr of run_as_find_unknown_escape's one run, byte for byte.",
        capture_bytes=_twin("run_as_find_unknown_escape_stderr", "run_as_find_unknown_escape"),
    ),
    Fixture(
        name="adb_version",
        args=[],
        host_argv=["adb", "version"],
        description=(
            "The HOST adb client's `adb version` banner; its first line is "
            "'Android Debug Bridge version X.Y.Z'. container.py reads app files "
            "with `adb shell -T`, which a client older than 1.0.36 (platform-tools "
            "24) does not refuse: it forwards `-T` to the device shell as command "
            "text and exits 0 after printing the shell's error, which a read "
            "deciding by exit status takes for the file. So the client is checked "
            "against this line before the first read. The install path is "
            "scrubbed of the recording account's home directory."
        ),
        post=_chain(_scrub_home_paths, _requires("Android Debug Bridge version ")),
    ),
    Fixture(
        name="sqlite_schema_host",
        args=[],
        prepare=_pull_app_database,
        host_argv=["sqlite3", "{tmp}/fx.db", ".schema"],
        description=(
            "`sqlite3 .schema` of a real Android app database. Recorded on the "
            "HOST because sqlite3 is absent from Android user builds -- "
            "verified missing on API 35 -- so model_inspector's working path is "
            "pull-then-host-sqlite3, and the adb call would capture the pull "
            "rather than the schema. Contains what a hand-written schema omits: "
            "android_metadata and sqlite_sequence, AUTOINCREMENT, a composite "
            "FOREIGN KEY ... ON DELETE CASCADE, and separate CREATE INDEX "
            "statements including a UNIQUE one."
        ),
    ),
    # --- emulator -list-avds: three scripts parse it, none had a fixture ----
    Fixture(
        name="emulator_list_avds",
        args=[],
        host_argv=["{emulator}", "-list-avds"],
        description=(
            "`emulator -list-avds`. Three scripts (device_list, emulator_boot, "
            "emulator_selector) parse this and none had a fixture, while two "
            "more scripts answer 'which AVDs exist?' from entirely different "
            "sources -- emulator_delete asks `avdmanager list avd -c`, "
            "emulator_erase scans $ANDROID_AVD_HOME for `*.avd` directories. "
            "Recording all three is what makes that divergence testable. The "
            "shape is deliberately boring: one bare AVD name per line, no "
            "header, no decoration -- but note it must be captured through the "
            "RESOLVED binary, because with the SDK root on PATH the bare name "
            "`emulator` is the <sdk>/emulator DIRECTORY and execve raises "
            "PermissionError, which is the defect this fixture pins."
        ),
    ),
    Fixture(
        name="avdmanager_list_avd_compact",
        args=[],
        host_argv=["{avdmanager}", "list", "avd", "-c"],
        description=(
            "`avdmanager list avd -c`. The compact form emulator_delete parses, "
            "as opposed to the block form in avdmanager_list_avd: one name per "
            "line, identical in shape to `emulator -list-avds`. Recorded as the "
            "second leg of the AVD-source agreement test."
        ),
    ),
    # --- avdmanager: seven scripts parse this and none had a fixture --------
    Fixture(
        name="avdmanager_list_avd",
        args=[],
        host_argv=["{avdmanager}", "list", "avd"],
        post=_scrub_home_paths,
        description=(
            "`avdmanager list avd`. Recorded only now, because until the SDK "
            "command-line tools were installed avdmanager could not run on the "
            "dev machine at all -- the legacy tools/bin copies use JAXB, removed "
            "in Java 11 -- so every script parsing AVD listings was written "
            "against imagined output. Note the shape: a header line, then "
            "indented `Key: value` pairs with INCONSISTENT leading whitespace "
            "(`    Name:` has four spaces, `  Device:` two), a `Based on:` "
            "continuation line belonging to `Target:`, and Tag/ABI riding on "
            "that same continuation rather than having its own key."
        ),
    ),
    Fixture(
        name="avdmanager_list_device",
        args=[],
        host_argv=["{avdmanager}", "list", "device"],
        post=_scrub_home_paths,
        description=(
            "`avdmanager list device` -- the device definitions emulator_create "
            'offers. Entries are numbered `id: N or "name"`, which is two '
            "identifiers for one device, and the human name is on a separate "
            "`Name:` line."
        ),
    ),
    # --- avdmanager create / delete: one throwaway AVD, made and removed ----
    # All four come out of one _ThrowawayAvd lifecycle (create -> config.ini
    # -> delete -> delete-missing -> read back), whichever of them --only asks
    # for. See that class for why the order is not left to FIXTURES.
    Fixture(
        name="avdmanager_create",
        args=[],
        command=" ".join(_AVD_ARGV["avdmanager_create"]) + "  (stdin: no)",
        capture_with=_THROWAWAY_AVD.capture("avdmanager_create"),
        record_both_streams=True,
        teardown_verify=_throwaway_avd_removed,
        post=_chain(_scrub_home_paths, _requires("Loading local repository")),
        description=(
            "`avdmanager create avd` exactly as emulator_create runs it (long "
            "options, --device given, `no` on stdin), for a throwaway AVD that "
            "the same run deletes. Exit 0, stdout only, and what it prints is "
            "progress, not a result: two 'Loading local repository...' "
            "repaints and 'Auto-selecting single ABI arm64-v8a'. The raw "
            "stream separates the repaints with BARE carriage returns; text-"
            "mode decoding, the recorder's and emulator_create's alike, turns "
            "each into a newline, which is what this file holds. With --device "
            "given there is NO 'Do you wish to create a custom hardware "
            "profile? [no]' prompt (measured: it is printed only when --device "
            "is omitted), so the `no` emulator_create sends is never read. "
            "Nothing here names the AVD or says it was created: success is "
            "the exit status alone."
        ),
    ),
    Fixture(
        name="avd_config_ini",
        args=[],
        command=(
            f"cat <avd home>/{THROWAWAY_AVD}.avd/config.ini  "
            f"(between avdmanager_create and avdmanager_delete)"
        ),
        capture_bytes=_THROWAWAY_AVD.config_ini,
        teardown_verify=_throwaway_avd_removed,
        description=(
            "The config.ini avdmanager writes for a new AVD, read from disk "
            "between the create and the delete -- what emulator_selector's "
            "read_avd_config / parse_config_ini consume. Bare `key=value` "
            "lines, sorted, no spaces around `=`, no comments and no blank "
            "lines. Three things a hand-written one gets wrong: there is no "
            "`AvdId` key and no `avd.ini.displayname` (this create wrote "
            "neither; the Studio-made Pixel_9 on the recording host has both), "
            "so emulator_selector's display_name for this AVD is empty; "
            "`avd.id` and `avd.name` hold the LITERAL placeholder '<build>', "
            "not the AVD's name; and the API level is only in "
            "`image.sysdir.1=system-images/android-34/...` and "
            "`target=android-34`. Recorded as bytes so the package aliaser "
            "never sees it -- its dotted keys (hw.camera.back) match the "
            "aliaser's pattern and would be rewritten."
        ),
    ),
    Fixture(
        name="avdmanager_delete",
        args=[],
        command=" ".join(_AVD_ARGV["avdmanager_delete"]),
        capture_with=_THROWAWAY_AVD.capture("avdmanager_delete"),
        record_both_streams=True,
        teardown_verify=_throwaway_avd_removed,
        post=_chain(_scrub_home_paths, _requires(f"AVD '{THROWAWAY_AVD}' deleted.")),
        description=(
            "`avdmanager delete avd` of an AVD that exists. Exit 0, stdout "
            "only: a leading blank line, then \"AVD '<name>' deleted.\" with "
            "NO trailing newline. It names no path and lists nothing it "
            "removed, so whether the .avd directory and the .ini beside it are "
            "gone is not something this output can say -- the recorder reads "
            "the disk back to know it."
        ),
    ),
    Fixture(
        name="avdmanager_delete_missing",
        args=[],
        command=" ".join(_AVD_ARGV["avdmanager_delete_missing"]),
        capture_with=_THROWAWAY_AVD.capture("avdmanager_delete_missing"),
        record_both_streams=True,
        teardown_verify=_throwaway_avd_removed,
        post=_chain(_scrub_home_paths, _requires("There is no Android Virtual Device named")),
        description=(
            "`avdmanager delete avd` of a name that does not exist. Exit **1**, "
            'stdout EMPTY, and on stderr "Error: There is no Android Virtual '
            "Device named '<name>'.\" followed by a second line reading just "
            "`null` -- a Java exception message printed without a null check. "
            "emulator_create.delete() passes stderr through verbatim, so its "
            "failure message ends in 'null'."
        ),
    ),
    Fixture(
        name="sdkmanager_list_installed",
        args=[],
        host_argv=["{sdkmanager}", "--list_installed"],
        post=_scrub_home_paths,
        description=(
            "`sdkmanager --list_installed` from cmdline-tools 23, whose "
            "`bin/sdkmanager` is a wrapper that hands the command to the "
            "Android CLI -- one of TWO formats (the Java tool's is "
            "sdkmanager_java_list_installed). emulator_create looked for lines "
            "starting `system-images;` and split them on `|`. This wrapper "
            "prints `  system-images/android-34/google_apis/arm64-v8a` -- "
            "SLASHES, and whitespace-padded columns with no pipe. So the installed "
            "list was always empty, every image reported as 'not installed', "
            "and `--api` inference had nothing to infer from. Note also that "
            "the *install* id uses semicolons, so the slashes have to be "
            "converted back to build the `sdkmanager` argument the error "
            "message tells the user to run."
        ),
    ),
    Fixture(
        name="sdkmanager_list",
        args=[],
        host_argv=["{sdkmanager}", "--list"],
        post=_scrub_home_paths,
        timeout=180,
        catches=("AVD-LIST-IMAGES",),
        description=(
            "`sdkmanager --list` from cmdline-tools 23's Android-CLI wrapper "
            "(the Java tool's is sdkmanager_java_list) -- what `--list-images` "
            "reads, and inert for the same reason its sibling was: it looked "
            "for `system-images;` lines split on `|`, and this wrapper prints "
            "neither. The rows are "
            "the same slash-separated paths in whitespace-padded columns as "
            "`--list_installed`, in TWO sections -- `Installed packages:` then "
            "`Available packages:` -- which is the only thing here that says "
            "whether an image is already on the machine or has to be "
            "downloaded. It is also the only recording that contains API "
            "tokens which are not bare integers (`android-34-ext12`, "
            "`android-36.1`, `android-37.2-beta1`, `android-CANARY`): they are "
            "76 of the 325 image rows, so a parser requiring `android-<int>` "
            "silently drops a quarter of the listing."
        ),
    ),
    Fixture(
        name="sdkmanager_java_list_installed",
        args=[],
        command=f"{_JAVA_SDKMANAGER_COMMAND} --list_installed",
        capture_with=_capture_java_sdkmanager("--list_installed", timeout=120),
        post=_scrub_home_paths,
        timeout=120,
        catches=("AVD-JAVA-SDKMANAGER",),
        description=(
            "`sdkmanager --list_installed` from the Java SdkManagerCli -- the "
            "sdkmanager the CI runner image and every cmdline-tools before 23 "
            "run. NOT the format of sdkmanager_list_installed, which is "
            "cmdline-tools 23's wrapper handing the command to the Android "
            "CLI: this is a pipe table, `Path | Version | Description | "
            "Location`, keyed by the SEMICOLON install id "
            "(`system-images;android-34;default;arm64-v8a | 4 | ... | "
            "system-images/android-34/default/arm64-v8a`), after progress "
            "lines (`Loading package information...`, a `[====   ] 25%` bar) "
            "that a terminal overwrites in place. A parser that took the wrapper's "
            "slash rows as the only format answered 'no system image "
            "installed' on the runner that had just booted from one. It also "
            "lists an image the wrapper does not "
            "(`android-UpsideDownCakePrivacySandbox`), whose API token is not "
            "an integer."
        ),
    ),
    Fixture(
        name="sdkmanager_java_list",
        args=[],
        command=f"{_JAVA_SDKMANAGER_COMMAND} --list",
        capture_with=_capture_java_sdkmanager("--list", timeout=240),
        post=_scrub_home_paths,
        timeout=240,
        catches=("AVD-JAVA-SDKMANAGER",),
        description=(
            "`sdkmanager --list` from the Java SdkManagerCli (see "
            "sdkmanager_java_list_installed). Three sections, not two: "
            "`Installed packages:` (with a Location column), `Available "
            "Packages:` (capital P, no Location), and `Available Updates:`, "
            "whose rows are `ID | Installed | Available` -- packages that are "
            "ALREADY installed, so an image listed there must not be read as "
            "one to download. Needs the network: it fetches the remote index."
        ),
    ),
    Fixture(
        name="emu_help",
        args=["emu", "help"],
        description=(
            "The emulator console's own command list, recorded so a claim that "
            "some `adb emu` subcommand exists can be checked against the console "
            "rather than assumed. Several scripts in this skill previously "
            "invented subcommands that do not exist."
        ),
    ),
    # --- emulator console + focused activity ------------------------------
    Fixture(
        name="emu_avd_name",
        args=["emu", "avd", "name"],
        description=(
            "`adb emu avd name` reply. The emulator console appends 'OK' to every "
            "response, so the raw bytes are 'Pixel_9\\r\\nOK\\r\\n'. "
            "emulator_boot compared .strip() of this against the AVD name, which "
            "yields 'Pixel_9\\nOK' and therefore never matches -- the "
            "already-booted short-circuit was dead and a second emulator was "
            "spawned for a running AVD."
        ),
        catches=("S5",),
    ),
    Fixture(
        name="dumpsys_window_focus",
        args=[
            "shell",
            "dumpsys window 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp'",
        ],
        description=(
            "The focused-window lines from `dumpsys window`. get_current_activity "
            "tried to build this pipeline as an argv list and run it with "
            "shell=True, which on POSIX executes only argv[0] -- bare `adb` -- so "
            "it always returned None. Recorded here as the text the parser must "
            "handle; the fix filters in Python rather than shelling out."
        ),
        catches=("S1",),
    ),
    # --- performance counters ---------------------------------------------
    Fixture(
        name="dumpsys_gfxinfo",
        timeout=90,
        args=["shell", "dumpsys", "gfxinfo", "com.android.settings"],
        description=(
            "Frame statistics: 'Total frames rendered', 'Janky frames', and the "
            "percentile block. Real jank accounting, versus anr_watcher's "
            "inference from Choreographer's 'Skipped N frames' log line."
        ),
    ),
    Fixture(
        name="dumpsys_meminfo",
        timeout=90,
        args=["shell", "dumpsys", "meminfo", "com.android.settings"],
        description=(
            "Memory breakdown including the Objects block (Views, ViewRootImpl, "
            "Activities, AppContexts). The Activities count is the Activity-leak "
            "test: navigate in and out N times and see whether it grew by N."
        ),
    ),
    # --- R11: a Jetpack Compose screen, which no system app renders ---------
    # Recorded from tests/fixtures/scaffold/compose. Build and install it
    # first; the post-processor refuses the capture if the app is not on top.
    Fixture(
        name="uiautomator_compose_default",
        emulator_only=True,
        ext="xml",
        args=["exec-out", "uiautomator", "dump", "/dev/tty"],
        description=(
            "A Jetpack Compose screen with NO testTagsAsResourceId -- what a new "
            "Android app looks like out of the box. Three things a class-name "
            "whitelist gets wrong here. (1) Every resource-id is EMPTY: Compose "
            "emits none, so any lookup keyed on resource-id finds nothing. (2) The "
            "nodes that actually carry clickable/checkable/scrollable are "
            "android.view.View -- the clickable Card, the Switch, the IconButton "
            "and the LazyColumn all report that class -- while the whitelisted "
            "widget names that DO appear (android.widget.Button, .TextView) sit on "
            "nodes that are not clickable. (3) uiautomator dumps the UNMERGED "
            "semantics tree, so mergeDescendants does not collapse anything: the "
            "clickable Card's own text is empty and its three Text children remain "
            "separate TextView nodes. A label has to be recovered from "
            "descendants, never read off the interactive node itself."
        ),
        catches=("R11",),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.DefaultActivity"],
            ["shell", "sleep", "1"],
        ],
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
        post=_hierarchy_of(COMPOSE_PACKAGE),
    ),
    Fixture(
        name="uiautomator_compose_testtags",
        emulator_only=True,
        ext="xml",
        args=["exec-out", "uiautomator", "dump", "/dev/tty"],
        description=(
            "The same Compose screen from an activity whose root adds "
            "Modifier.semantics { testTagsAsResourceId = true }, so Modifier.testTag "
            "surfaces as resource-id. This is the remediation the skill documents, "
            "and diffing it against uiautomator_compose_default shows exactly what "
            "it buys: one extra semantics wrapper node, and the resource-id column. "
            "Note the ids are the BARE testTag string ('submit_button'), not the "
            "'package:id/name' form every AOSP view uses -- code that splits on "
            "':id/' drops them. Only the nodes given a testTag get an id; the "
            "TextViews inside the Card and the list still have none."
        ),
        catches=("R11",),
        setup=[
            ["shell", "am", "force-stop", COMPOSE_PACKAGE],
            ["shell", "am", "start", "-W", "-n", f"{COMPOSE_PACKAGE}/.TestTagsActivity"],
            ["shell", "sleep", "1"],
        ],
        teardown=[["shell", "am", "force-stop", COMPOSE_PACKAGE]],
        post=_hierarchy_of(COMPOSE_PACKAGE),
    ),
    # --- below the fold: successive states of one scrolling list ------------
    Fixture(
        name="uiautomator_settings_top",
        emulator_only=True,
        ext="xml",
        args=["exec-out", "uiautomator", "dump", "/dev/tty"],
        description=(
            "Settings home as it opens. 'Notifications' is on it; 'About "
            "emulated device' is NOT -- it exists, one scroll below the fold. "
            'That pair is the whole navigator defect: `--find-text "About '
            "phone\"` answered 'Not found' and exited 1, which an agent reads "
            "as 'the item does not exist' and gives up. Two nodes carry "
            'scrollable="true" here (nested ScrollViews: '
            "settings_homepage_container and main_content_scrollable_container), "
            "so a screen is not limited to one scrolling region."
        ),
        setup=_OPEN_SETTINGS,
        post=_screen_of(
            SETTINGS_PACKAGE,
            require=("Notifications",),
            forbid=("About emulated device",),
            scrollable=True,
        ),
    ),
    Fixture(
        name="uiautomator_settings_scrolled",
        emulator_only=True,
        ext="xml",
        args=["exec-out", "uiautomator", "dump", "/dev/tty"],
        description=(
            "The same list after ONE scroll-down swipe. 'About emulated device' "
            "is now present and 'Notifications' has gone off the top, so the "
            "pair with uiautomator_settings_top is real evidence that one "
            "scroll changes what a text search can reach -- not a hand-edited "
            "variant of a single dump."
        ),
        setup=_OPEN_SETTINGS + [_SCROLL_DOWN, _SETTLE],
        post=_screen_of(
            SETTINGS_PACKAGE,
            require=("About emulated device",),
            forbid=("Notifications",),
            scrollable=True,
        ),
    ),
    Fixture(
        name="uiautomator_settings_half",
        emulator_only=True,
        ext="xml",
        args=["exec-out", "uiautomator", "dump", "/dev/tty"],
        description=(
            "The state BETWEEN settings_top and settings_scrolled, reached with "
            "a half-height swipe (80% -> 50% of the screen). It exists because "
            "this list is short: at the skill's own 80% -> 20% swipe it reaches "
            "its end in a single scroll, so the corpus had no three-state "
            "sequence to prove a search loop actually iterates. The traversal is "
            "real and was checked on the device -- a full scroll from this state "
            "produces bytes identical to settings_scrolled. Note what changed: "
            "'Network & internet' has gone off the top and 'Accessibility' has "
            "come in, while the target 'About emulated device' is still not "
            "there. So a scroll can move the list a long way without the search "
            "hitting, which is why 'no match' must not end the search and why "
            "bounds belong in the changed-screen comparison."
        ),
        setup=_OPEN_SETTINGS
        + [["shell", "input", "swipe", "540", "1939", "540", "1212", "300"], _SETTLE],
        post=_screen_of(
            SETTINGS_PACKAGE,
            require=("Notifications", "Accessibility"),
            forbid=("About emulated device", "Network"),
            scrollable=True,
        ),
    ),
    Fixture(
        name="uiautomator_settings_scrolled_again",
        emulator_only=True,
        ext="xml",
        args=["exec-out", "uiautomator", "dump", "/dev/tty"],
        description=(
            "A SECOND scroll-down swipe on a list already at its end, dumped "
            "again. Byte-identical to uiautomator_settings_scrolled -- measured: "
            "dumps taken after the 2nd through 6th swipe all hashed the same. "
            "This is the ground truth for navigator's early exit: at the end of "
            "a list the swipe still succeeds and still exits 0, so 'the screen "
            "did not change' is the only signal that scrolling further is "
            "pointless. A search without it reports having searched ten screens "
            "after searching one screen ten times."
        ),
        setup=_OPEN_SETTINGS + [_SCROLL_DOWN, _SETTLE, _SCROLL_DOWN, _SETTLE],
        post=_screen_of(SETTINGS_PACKAGE, require=("About emulated device",), scrollable=True),
    ),
    Fixture(
        name="uiautomator_dialer_keypad",
        emulator_only=True,
        ext="xml",
        args=["exec-out", "uiautomator", "dump", "/dev/tty"],
        description=(
            "A screen with NO scrollable node: the dialer keypad, opened on a "
            "dialled number. Recorded because every Settings page probed "
            "(home, date, wifi, input methods) and the launcher home screen all "
            "report at least one scrollable container, so 'the screen does not "
            "scroll' needed evidence rather than assumption. navigator must "
            "report that instead of swiping hopefully at a screen that cannot "
            "move."
        ),
        setup=[
            ["shell", "am", "start", "-a", "android.intent.action.DIAL", "-d", "tel:5551234"],
            ["shell", "sleep", "3"],
        ],
        teardown=[["shell", "am", "force-stop", DIALER_PACKAGE]],
        post=_screen_of(DIALER_PACKAGE, require=("555-1234",), scrollable=False),
    ),
    # --- am start -W: what a launch actually reports -----------------------
    # app_launcher --launch parses this and was written with no recording to
    # read; its own docstring says so. Both halves are captured because the
    # failure half is where the surprises are.
    Fixture(
        name="am_start_wait_settings",
        emulator_only=True,
        args=["shell", "am", "start", "-W", "-n", f"{SETTINGS_PACKAGE}/.Settings"],
        description=(
            "A successful `am start -W`, cold. Six lines on stdout, exit 0: "
            "'Starting: Intent { cmp=... }', 'Status: ok', "
            "'LaunchState: COLD', 'Activity: ...', 'TotalTime: <ms>', "
            "'WaitTime: <ms>', 'Complete'. Two traps for a parser written from "
            "memory. First, the **Activity: line does not name the component "
            "that was requested** -- asking for com.android.settings/.Settings "
            "reports com.android.settings/.homepage.SettingsHomepageActivity, "
            "because .Settings is an alias, so 'did the thing I asked for "
            "launch' cannot be answered by comparing those two strings. "
            "Second, when the activity is ALREADY foreground the command still "
            "exits 0 and still says 'Status: ok', but inserts "
            "'Warning: Activity not started, intent has been delivered to "
            "currently running top-most instance.' and reports TotalTime: 0 -- "
            "so a caller cannot tell a fresh launch from a no-op by the status "
            "line. The setup here force-stops first to make the capture a "
            "genuine cold start (LaunchState: COLD, TotalTime in the hundreds "
            "of ms)."
        ),
        catches=("E1",),
        record_both_streams=True,
        setup=[
            ["shell", "input", "keyevent", "KEYCODE_HOME"],
            ["shell", "am", "force-stop", SETTINGS_PACKAGE],
            ["shell", "am", "force-stop", "com.google.android.settings.intelligence"],
            ["shell", "sleep", "3"],
        ],
        teardown=[["shell", "am", "force-stop", SETTINGS_PACKAGE]],
        post=_requires("Status: ok", "LaunchState:", "TotalTime:", "Complete"),
    ),
    Fixture(
        name="am_start_wait_missing",
        emulator_only=True,
        args=["shell", "am", "start", "-W", "-n", f"{SETTINGS_PACKAGE}/.NoSuchActivity"],
        description=(
            "`am start -W` naming an activity class that does not exist. Exit "
            "**1**, and -- the part a parser has to know -- the whole "
            "diagnostic is on **stdout**, with stderr completely empty (see "
            "stderr_bytes). The shape: 'Starting: Intent { cmp=... }', then "
            "'Error type 3', then 'Error: Activity class {pkg/fully.qualified."
            "Class} does not exist.'. Note there is no 'Status:' line at all, "
            "so a parser looking for 'Status: ok' to decide success gets a "
            "correct answer here only by accident, and one keying on stderr to "
            "find the error finds nothing. Note too that the component in the "
            "message is EXPANDED to its fully-qualified form, which is not the "
            "string that was passed in."
        ),
        catches=("E1",),
        record_both_streams=True,
        post=_requires("Error type 3", "does not exist"),
    ),
    Fixture(
        name="am_start_wait_view_unresolved",
        emulator_only=True,
        args=[
            "shell",
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            "aes-nothing-handles://x",
        ],
        description=(
            "`am start -W -a VIEW -d <url>` for a deep link that nothing on the "
            "device handles -- the command app_launcher --open-url runs. Exit "
            "**1**, the diagnostic on **stdout** with stderr empty, like "
            "am_start_wait_missing, but a different shape: no 'Error type' "
            "line at all, just 'Starting: Intent { act=... dat=... }' and "
            "'Error: Activity not started, unable to resolve Intent { ... }'. "
            "Note the data URI is cut to scheme and host with '/...' "
            "appended, in both lines, though this URL had no path at all -- "
            "'aes-nothing-handles://x/...' -- so a path or query that failed "
            "cannot be read back out of the error, and a caller that wants to "
            "name the URL has to remember what it passed. A flags field "
            "(flg=0x10000000) "
            "appears in the error line that the Starting line does not carry."
        ),
        catches=("E1",),
        record_both_streams=True,
        post=_requires("unable to resolve Intent", absent=("Status:",)),
    ),
    # --- process liveness: the empty answer that is not an error -----------
    Fixture(
        name="pidof_not_running",
        record_both_streams=True,
        args=["shell", "pidof", "com.example.definitely.not.installed"],
        description=(
            "`pidof` for a package that is not running: **no output at all**, on "
            "either stream, and exit status 1. The file is deliberately zero "
            "bytes -- that emptiness IS the recording, and the exit_status field "
            "in this entry is what distinguishes it from a capture that failed. "
            "Any caller that decides liveness by parsing stdout sees the same "
            "empty string it would get from a device that is not responding at "
            "all; only the exit status separates the two."
        ),
        catches=("D4",),
        post=_requires_empty(),
    ),
    Fixture(
        name="pm_path_installed",
        record_both_streams=True,
        args=["shell", "pm", "path", COMPOSE_PACKAGE],
        description=(
            "`pm path` of an installed package, the emulator lane's check that "
            "the fixture app is there before it runs: exit 0, `package:<apk "
            "path>` on stdout, stderr empty. The lane reads only the status."
        ),
        post=_requires("package:/data/app/", f"/{COMPOSE_PACKAGE}-"),
    ),
    Fixture(
        name="pm_path_absent",
        record_both_streams=True,
        args=["shell", "pm", "path", "com.nonexistent.app"],
        description=(
            "`pm path` of a package that is not installed: nothing on either "
            "stream, and exit 1 -- the status is the whole answer, which is why "
            "the lane can send stdout to /dev/null."
        ),
        post=_requires_empty(),
    ),
    # --- IME visibility: which key actually exists ------------------------
    Fixture(
        name="dumpsys_input_method_shown",
        args=["shell", "dumpsys", "input_method"],
        description=(
            "IME visibility flags, filtered out of `dumpsys input_method` to the "
            "lines matching mInputShown|mIsInputViewShown|mImeWindowVis. "
            "Measured on API 33 and API 35: `mInputShown` and "
            "`mIsInputViewShown` both exist; **`mImeWindowVis` does not appear "
            "on either**, so code keying on it reads nothing. Note the second "
            "key shares its line with `mStatusIcon`, so a parser splitting the "
            "line on whitespace gets two fields, not one. "
            "The full dump is 7000+ lines and names the user's IME, keyboard app "
            "and open windows, so it is filtered BEFORE anything is written -- "
            "package redaction does not catch free text."
        ),
        catches=("C8",),
        post=_grep_lines(r"mInputShown|mIsInputViewShown|mImeWindowVis"),
    ),
    # --- `input text` is ASCII-only, and says so by crashing ---------------
    Fixture(
        name="input_text_nonascii",
        emulator_only=True,
        record_both_streams=True,
        args=["shell", "input", "text", "héllo"],
        description=(
            "`input text` with one non-ASCII character. It does not type a "
            "mangled string and it does not report a friendly error: it throws "
            "`java.lang.NullPointerException: Attempt to get length of null "
            "array` out of InputShellCommand.sendText, prints the Java stack "
            "trace to **stderr**, and exits **255**. Nothing is typed -- "
            "measured against a focused EditText that still held its previous "
            "ASCII value afterwards. The ASCII control (`input text hello`) "
            "exits 0 with both streams empty and the field updated. "
            "The crash happens in the shell command before any injection, so it "
            "does not depend on which field has focus."
        ),
        catches=("C12",),
        setup=[
            ["shell", "am", "force-stop", SETTINGS_PACKAGE],
            ["shell", "am", "start", "-n", f"{SETTINGS_PACKAGE}/.Settings"],
            ["shell", "sleep", "3"],
            # Centre of the Settings search bar on this profile's 1080x2424
            # screen; recompute before recording a differently sized device,
            # as with _SCROLL_DOWN above.
            ["shell", "input", "tap", "540", "673"],
            ["shell", "sleep", "2"],
        ],
        teardown=[["shell", "am", "force-stop", SETTINGS_PACKAGE]],
        post=_requires("NullPointerException", "InputShellCommand.sendText"),
    ),
    # --- demo mode clock: 3-digit hhmm is silently ignored -----------------
    # Both are recorded over Settings rather than the launcher: the home-screen
    # wallpaper is a gradient that PNG cannot compress, and it put the strip
    # over the size budget (measured 64KB vs 5KB).
    Fixture(
        name="demo_mode_clock_941",
        emulator_only=True,
        # What capture_bytes runs; the crop to the top strip happens on the host.
        args=["exec-out", "screencap", "-p"],
        ext="png",
        capture_bytes=_status_bar_strip_showing(demo_clock_applied=False),
        # clock_reads in MANIFEST.json is measured, not remembered.
        description=(
            "Status bar after `-e command clock -e hhmm 941`. The clock reads "
            "**the device's own wall-clock time, unchanged** -- not 9:41. (The "
            "exact time is whatever it was at capture; open the PNG and compare "
            "it with demo_mode_clock_0941.png, which is the same status bar one "
            "broadcast later.) SystemUI "
            "requires a 4-digit HHMM and silently drops anything else: no error, "
            "no log line, exit 0, `Broadcast completed: result=0`. A caller "
            "formatting '9:41' as '941' therefore gets a status bar that still "
            "shows the real time while every success check it can make passes. "
            "Demo mode is re-entered in setup precisely so this is unambiguous: "
            "a fresh `enter` resets the clock to real time, so 12:16 here proves "
            "941 did nothing rather than merely leaving an earlier value."
        ),
        catches=("L2",),
        setup=[
            ["shell", "am", "force-stop", SETTINGS_PACKAGE],
            ["shell", "am", "start", "-n", f"{SETTINGS_PACKAGE}/.Settings"],
            ["shell", "sleep", "3"],
            ["shell", "settings", "put", "global", "sysui_demo_allowed", "1"],
            ["shell", "am", "broadcast", "-a", DEMO_ACTION, "-e", "command", "exit"],
            ["shell", "sleep", "2"],
            ["shell", "am", "broadcast", "-a", DEMO_ACTION, "-e", "command", "enter"],
            ["shell", "sleep", "1"],
            *_demo_clock("941"),
        ],
        teardown=[["shell", "am", "broadcast", "-a", DEMO_ACTION, "-e", "command", "exit"]],
    ),
    Fixture(
        name="demo_mode_clock_0941",
        emulator_only=True,
        # What capture_bytes runs; the crop to the top strip happens on the host.
        args=["exec-out", "screencap", "-p"],
        ext="png",
        capture_bytes=_status_bar_strip_showing(demo_clock_applied=True),
        # clock_reads in MANIFEST.json is measured, not remembered.
        description=(
            "The same broadcast with the hour zero-padded: `-e hhmm 0941`. The "
            "clock reads **9:41**. This is the control that makes "
            "demo_mode_clock_941 mean something -- the mechanism works, the "
            "broadcast reaches SystemUI, and the only difference between the "
            "two captures is the width of the hhmm argument."
        ),
        catches=("L2",),
        setup=[
            ["shell", "am", "force-stop", SETTINGS_PACKAGE],
            ["shell", "am", "start", "-n", f"{SETTINGS_PACKAGE}/.Settings"],
            ["shell", "sleep", "3"],
            ["shell", "settings", "put", "global", "sysui_demo_allowed", "1"],
            ["shell", "am", "broadcast", "-a", DEMO_ACTION, "-e", "command", "exit"],
            ["shell", "sleep", "2"],
            ["shell", "am", "broadcast", "-a", DEMO_ACTION, "-e", "command", "enter"],
            ["shell", "sleep", "1"],
            *_demo_clock("0941"),
        ],
        teardown=[["shell", "am", "broadcast", "-a", DEMO_ACTION, "-e", "command", "exit"]],
    ),
]


def _command_of(fixture: Fixture) -> str:
    """What the manifest and ``--list`` say was run to produce ``fixture``."""
    if fixture.command is not None:
        return fixture.command
    if fixture.host_argv is not None:
        return " ".join(fixture.host_argv)
    return "adb " + " ".join(fixture.args)


def _known_names() -> set[str]:
    """Every fixture name this tool can record."""
    return {f.name for f in FIXTURES} | {HIERARCHY_FIXTURE, JAVA_VERSION_FIXTURE}


# macOS's JDK registry. `-V` lists every installed JDK on stderr, one per line,
# ending in its home: `    17.0.20 (arm64) "Amazon.com Inc." - "Amazon
# Corretto 17" /Users/<user>/Library/Java/JavaVirtualMachines/.../Contents/Home`.
_JAVA_HOME_TOOL = Path("/usr/libexec/java_home")
_JAVA_HOME_ENTRY = re.compile(r'"\s+(/.+)$')

# The major in a `java -version` banner's quoted version: "17.0.20.1" is 17,
# and before Java 9 the scheme was "1.8.0_392", whose major is 8. Used only to
# NAME the file; the health check's own parser is tested against what is in it.
_JAVA_BANNER_MAJOR = re.compile(r'version "(?:1\.)?(\d+)')


def _host_java_binaries() -> list[tuple[str, Path]]:
    """Every JDK on this host, as ``(how it was found, its java binary)``.

    The JDKs ``java_home -V`` registers, newest first as it lists them, then
    the ``java`` on PATH: that is the one the health check runs, and a
    Homebrew JDK is on PATH without being registered.
    """
    found: list[tuple[str, Path]] = []
    if _JAVA_HOME_TOOL.exists():
        listing = _run_host([str(_JAVA_HOME_TOOL), "-V"])
        for line in listing.stderr.splitlines():
            entry = _JAVA_HOME_ENTRY.search(line)
            if entry:
                found.append(("java_home -V", Path(entry.group(1)) / "bin" / "java"))
    on_path = shutil.which("java")
    if on_path:
        found.append(("PATH", Path(on_path)))
    return found


# How a banner line starts: Oracle prints `java version "`, every OpenJDK
# build `openjdk version "`.
_JAVA_BANNER_LINE = re.compile(r'(?:java|openjdk) version "')


def _java_version_line(text: str) -> str | None:
    """The banner line of ``java -version`` output, or None.

    The first line that STARTS ``java version "`` or ``openjdk version "``.
    Not simply the first line: with JAVA_TOOL_OPTIONS set the JVM prints
    ``Picked up JAVA_TOOL_OPTIONS: ...`` ahead of its banner. And not the first
    line that merely contains ``version "``: that notice echoes the options
    verbatim, so it can carry ``version "21"`` of its own. The same rule as
    android_health_check.sh's java check.
    """
    return next((line for line in text.splitlines() if _JAVA_BANNER_LINE.match(line)), None)


def _capture_java_versions() -> list[tuple[int, str, Path, Capture]]:
    """``java -version`` from one JDK per major installed on this host.

    The first JDK found for a major is the one recorded. The whole output is
    kept, all three lines: the health check picks the first line itself, and a
    fixture that did that for it would leave the selection untested.
    """
    by_major: dict[int, tuple[int, str, Path, Capture]] = {}
    for source, java in _host_java_binaries():
        capture = _run_host([str(java), "-version"])
        line = _java_version_line(capture.text)
        found = _JAVA_BANNER_MAJOR.search(line) if line is not None else None
        if found is None:
            raise CaptureError(f"{java} -version printed no version: {capture.text[:200]!r}")
        first = capture.text.splitlines()[0]
        if first != line:
            # Refused, not committed with the notice: a fixture is the banner a
            # plain `java -version` prints, and the notice case is derived
            # from it where it is tested.
            raise CaptureError(
                f"{java} -version printed {first!r} before its banner, a startup "
                f"notice; unset JAVA_TOOL_OPTIONS (and JDK_JAVA_OPTIONS, "
                f"_JAVA_OPTIONS) and record again"
            )
        major = int(found.group(1))
        by_major.setdefault(major, (major, source, java, capture))
    if not by_major:
        raise CaptureError("no JDK found: java_home -V listed none and no java is on PATH")
    return [by_major[major] for major in sorted(by_major)]


# `java_version_<major>`: the names the java_version selector writes.
_JAVA_VERSION_NAME = re.compile(rf"{JAVA_VERSION_FIXTURE}_(\d+)")


def _java_version_text(major: int) -> tuple[str, str]:
    """``(command, description)`` for ``java_version_<major>``.

    Neither names the JDK's path or how it was found: both depend on the host,
    and the manifest's text must be what :func:`recorder_text` says without
    one. The scrubbed path and its source go in the entry's ``java`` and
    ``found_by`` fields instead.
    """
    return (
        f"<JDK {major}>/bin/java -version",
        (
            f"`java -version` from a JDK {major} on the recording host, all of it. "
            f"android_health_check.sh reads its first line and fails a JDK below "
            f"17, AGP 8's floor. Host-only: no adb."
        ),
    )


def _capture_hierarchy(serial: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Dump the current UI hierarchy to stdout.

    Uses ``adb exec-out``, not ``adb shell``. Over ``shell`` the device
    allocates a pty and uiautomator writes only its status line
    ("UI hierchary dumped to: /dev/tty" — Android's typo, not ours), so the XML
    never reaches the host. ``exec-out`` gives a raw stream and the XML arrives.

    This also sidesteps the /sdcard round-trip entirely, which is the real fix
    for the fixed-path collision the skill's three hierarchy implementations
    currently share (R4): no device-side file means no file to collide on.
    """
    raw = _run(["exec-out", "uiautomator", "dump", "/dev/tty"], serial, timeout).text
    start = raw.find("<?xml")
    if start == -1:
        raise CaptureError(f"no XML in uiautomator output: {raw.strip()[:120]!r}")
    end = raw.rfind("</hierarchy>")
    if end == -1:
        raise CaptureError("uiautomator output truncated: no closing </hierarchy>")
    return raw[start : end + len("</hierarchy>")] + "\n"


def _device_metadata(serial: str) -> dict[str, str]:
    """Collect the device identity that gives a fixture its provenance."""
    props = {
        "model": "ro.product.model",
        "api_level": "ro.build.version.sdk",
        "android_release": "ro.build.version.release",
        "abi": "ro.product.cpu.abi",
        "build_fingerprint": "ro.build.fingerprint",
    }
    meta: dict[str, str] = {}
    for key, prop in props.items():
        meta[key] = _strip_cr(_run(["shell", "getprop", prop], serial, timeout=15).text).strip()
    return meta


def resolve_serial(serial: str | None) -> str | None:
    """The one serial a run addresses: ``--serial``, else $ANDROID_SERIAL, else None.

    Resolved once, at the start of :func:`record`, and passed to every adb call
    from there on -- each of which puts it after ``-s`` (:func:`_adb_argv`).
    adb's own fallbacks (the env var it reads itself, the only device
    attached) are never left to choose a target.
    """
    return serial or os.environ.get("ANDROID_SERIAL") or None


def _physical_target_refusal(target: str | None, selected: list[Fixture]) -> str | None:
    """Why this run must not start, or None when it may.

    Any selected ``emulator_only`` fixture needs a target that is provably an
    emulator. ``target`` is :func:`resolve_serial`'s answer; None -- no
    ``--serial`` and no $ANDROID_SERIAL -- is not proof and is refused like a
    phone's serial is.
    """
    mutating = sorted(f.name for f in selected if f.emulator_only)
    if not mutating:
        return None
    if target and target.startswith("emulator-"):
        return None
    shown = repr(target) if target else "unspecified (adb would pick for itself)"
    listed = ", ".join(mutating[:5]) + (
        f" and {len(mutating) - 5} more" if len(mutating) > 5 else ""
    )
    return (
        f"Refusing to record: the target is {shown}, not an emulator, and "
        f"{len(mutating)} selected fixture(s) change the device they run on "
        f"({listed}). Pass --serial emulator-NNNN, or select only fixtures that "
        f"read (--list shows which change the device)."
    )


def recorder_text(name: str) -> tuple[str, str]:
    """``(command, description)`` the recorder writes to the manifest for ``name``.

    Raises:
        KeyError: The recorder has no fixture by that name.
    """
    if name == HIERARCHY_FIXTURE:
        return HIERARCHY_COMMAND, HIERARCHY_DESCRIPTION
    java = _JAVA_VERSION_NAME.fullmatch(name)
    if java:
        return _java_version_text(int(java.group(1)))
    for fixture in FIXTURES:
        if fixture.name == name:
            return _command_of(fixture), fixture.description
    raise KeyError(name)


# Profile-level provenance a partial (--only) run leaves as it was. Each entry
# the run records carries its own `recorded_at`, and its own copy of any of the
# others that differ from the profile's.
_PROFILE_PROVENANCE = ("recorded_at", "recorded_on", "device")


def _merge_manifest(manifest: dict, previous: dict | None, *, partial: bool) -> dict:
    """The manifest to write: this run's, merged into ``previous`` when ``partial``.

    Every entry this run recorded is stamped with this run's ``recorded_at``.
    A full run replaces the manifest. A partial run keeps every previous entry
    it did not re-record and the profile's own ``recorded_at``,
    ``recorded_on`` and ``device`` -- a one-fixture re-record must not
    re-date fixtures it never touched -- and an entry it recorded carries its
    own ``recorded_on`` / ``device`` when they differ from the profile's
    (a build-fingerprint bump, say, which the drift refusal does not stop).
    """
    for entry in manifest["fixtures"]:
        entry["recorded_at"] = manifest["recorded_at"]
    if previous is None or not partial:
        return manifest
    recorded_now = {entry["name"] for entry in manifest["fixtures"]}
    kept = [e for e in previous.get("fixtures", []) if e["name"] not in recorded_now]
    merged = {**manifest, "fixtures": sorted(kept + manifest["fixtures"], key=lambda e: e["name"])}
    for key in _PROFILE_PROVENANCE:
        if key not in previous:
            continue
        merged[key] = previous[key]
        if key != "recorded_at" and manifest[key] != previous[key]:
            for entry in manifest["fixtures"]:
                entry[key] = manifest[key]
    return merged


def record(serial: str | None, only: set[str] | None, profile: str) -> int:
    """Record fixtures, write MANIFEST.json, and report what was written."""
    selected = [f for f in FIXTURES if only is None or f.name in only]
    if only:
        # The hierarchy dump and the host JDK banners are captured out-of-band
        # (exec-out; a file count that depends on the host), so neither is in
        # FIXTURES.
        unknown = only - _known_names()
        if unknown:
            print(f"Unknown fixture(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            print(f"Known: {', '.join(sorted(_known_names()))}", file=sys.stderr)
            return 2

    # Before any adb call and before anything is written: the getprop just
    # below already talks to the target.
    target = resolve_serial(serial)
    refusal = _physical_target_refusal(target, selected)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    if target is None:
        # Every run reads the device's identity (getprop) first, so every run
        # needs a device -- and an unnamed one is whatever adb picks.
        print(
            "Refusing to record: no target serial. Pass --serial <serial> or set "
            "ANDROID_SERIAL; adb is never left to pick a device for itself.",
            file=sys.stderr,
        )
        return 2
    serial = target

    recorded_dir = RECORDED_ROOT / profile
    recorded_dir.mkdir(parents=True, exist_ok=True)

    device = _device_metadata(serial)
    if not device.get("api_level"):
        print("No device responding. Boot an emulator first.", file=sys.stderr)
        return 1

    # Recording into an existing profile with a different device attached
    # rewrites that profile's provenance to describe the wrong hardware --
    # silently invalidating every fixture already in it.
    manifest_path = recorded_dir / "MANIFEST.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8")).get("device", {})
        drift = [
            f"{key}: profile has {existing[key]!r}, device reports {device.get(key)!r}"
            for key in ("model", "api_level")
            if existing.get(key) and existing[key] != device.get(key)
        ]
        if drift:
            print(
                f"Refusing to record into profile {profile!r}: the attached "
                f"device is not the one it was recorded from.",
                file=sys.stderr,
            )
            for line in drift:
                print(f"  {line}", file=sys.stderr)
            print(
                "Pass --profile naming this device, or attach the original one.",
                file=sys.stderr,
            )
            return 2

    entries: list[dict] = []
    failures: list[tuple[str, str]] = []
    # Failures to put the DEVICE back as it was found. Tracked separately from
    # capture failures and reported last, because a fixture that recorded fine
    # while leaving an override on somebody's phone is the worse outcome.
    restore_failures: list[str] = []
    redactions: dict[str, str] = {}

    def _record_one(
        name: str,
        command: str,
        description: str,
        catches: list[str],
        payload: str | bytes,
        *,
        ext: str = "txt",
        capture: Capture | None = None,
        both_streams: bool = False,
        extra: dict | None = None,
    ):
        path = recorded_dir / f"{name}.{ext}"
        if isinstance(payload, bytes):
            path.write_bytes(payload)
            size = len(payload)
        else:
            path.write_text(payload, encoding="utf-8")
            size = len(payload.encode("utf-8"))
        entry = {
            "name": name,
            "file": path.name,
            "command": command,
            "description": description,
            "catches": catches,
            "bytes": size,
        }
        if capture is not None:
            # The exit status lives here rather than in the file so the file
            # stays byte-for-byte what the tool emitted. For two fixtures the
            # status *is* the evidence: `dumpsys activity anr` exits 0 while
            # printing "Unknown command", and `pidof <not running>` exits 1
            # while printing nothing.
            entry["exit_status"] = capture.returncode
            entry["stream"] = capture.stream
            if both_streams:
                # The file holds one stream. These make the OTHER one a
                # recorded fact instead of an assumption -- "stderr was empty"
                # is a claim the file itself can never carry.
                entry["stdout_bytes"] = len(capture.stdout.encode("utf-8"))
                entry["stderr_bytes"] = len(capture.stderr.encode("utf-8"))
        if extra:
            entry.update(extra)
        entries.append(entry)
        print(f"  ok    {path.name} ({size} bytes)")

    scratch = Path(tempfile.mkdtemp(prefix="android_emu_record_"))

    for fixture in selected:
        capture: Capture | None = None
        payload: str | bytes
        extra: dict = {}
        # Setup runs INSIDE the try. It used to run before it, so a timeout in
        # `wm size 1080x2400` -- or in anything else between the override and
        # the capture -- skipped the `finally` entirely and left a real phone
        # at the overridden resolution.
        try:
            for cmd in fixture.setup:
                _run([a.format(tmp=scratch) for a in cmd], serial)
            if fixture.prepare is not None:
                fixture.prepare(scratch, serial)
            if fixture.capture_bytes is not None:
                payload = fixture.capture_bytes(serial, extra)
            elif fixture.capture_with is not None:
                capture = fixture.capture_with(scratch, serial, extra)
            elif fixture.host_argv is not None:
                capture = _run_host(
                    [
                        (
                            _emulator_tool()
                            if a == "{emulator}"
                            else (
                                _sdk_tool(a.strip("{}"))
                                if a in ("{avdmanager}", "{sdkmanager}")
                                else a.format(tmp=scratch)
                            )
                        )
                        for a in fixture.host_argv
                    ],
                    fixture.timeout,
                )
            else:
                capture = _run(fixture.args, serial, fixture.timeout)
            if capture is not None:
                payload = _redact_private_packages(_strip_cr(capture.text), redactions)
                if fixture.post is not None:
                    payload = fixture.post(payload)
        except CaptureError as exc:
            failures.append((fixture.name, str(exc)))
            print(f"  FAIL  {fixture.name}: {exc}", file=sys.stderr)
            continue
        finally:
            # A teardown that fails is not cosmetic: these restore a device
            # somebody owns. Its exit status is checked, and `teardown_verify`
            # reads the state back rather than trusting the command's word.
            for cmd in fixture.teardown:
                result = _run(cmd, serial)
                if result.returncode != 0:
                    restore_failures.append(
                        f"{fixture.name}: `adb {' '.join(cmd)}` exited "
                        f"{result.returncode}: {result.text.strip()[:200]!r}"
                    )
            if fixture.teardown_verify is not None:
                try:
                    fixture.teardown_verify(serial)
                except CaptureError as exc:
                    restore_failures.append(f"{fixture.name}: {exc}")
        if fixture.metadata is not None:
            try:
                extra.update(fixture.metadata(serial))
            except CaptureError as exc:
                failures.append((fixture.name, f"metadata: {exc}"))
                print(f"  FAIL  {fixture.name}: metadata: {exc}", file=sys.stderr)
                continue
        _record_one(
            fixture.name,
            _command_of(fixture),
            fixture.description,
            list(fixture.catches),
            payload,
            ext=fixture.ext,
            capture=capture,
            both_streams=fixture.record_both_streams,
            extra=extra,
        )

    if only is None or HIERARCHY_FIXTURE in only:
        try:
            hierarchy = _redact_private_packages(_strip_cr(_capture_hierarchy(serial)), redactions)
        except CaptureError as exc:
            failures.append((HIERARCHY_FIXTURE, str(exc)))
            print(f"  FAIL  {HIERARCHY_FIXTURE}: {exc}", file=sys.stderr)
        else:
            _record_one(
                HIERARCHY_FIXTURE,
                HIERARCHY_COMMAND,
                HIERARCHY_DESCRIPTION,
                ["S7"],
                hierarchy,
                ext="xml",
            )

    if only is None or JAVA_VERSION_FIXTURE in only:
        # Host-only: java_home and each JDK's java, never adb. The getprop
        # above is the run's provenance, not part of this capture.
        try:
            banners = _capture_java_versions()
        except CaptureError as exc:
            failures.append((JAVA_VERSION_FIXTURE, str(exc)))
            print(f"  FAIL  {JAVA_VERSION_FIXTURE}: {exc}", file=sys.stderr)
        else:
            for major, source, java, capture in banners:
                _record_one(
                    f"{JAVA_VERSION_FIXTURE}_{major}",
                    *_java_version_text(major),
                    [],
                    _redact_private_packages(_strip_cr(capture.text), redactions),
                    capture=capture,
                    extra={"java": _scrub_home_paths(str(java)), "found_by": source},
                )

    manifest = {
        "profile": profile,
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recorded_on": platform.platform(),
        "device": device,
        "redacted_package_count": _aliases_present(recorded_dir),
        "redaction_note": (
            "Non-AOSP package names were replaced with stable com.example.appN "
            "aliases so a developer's own apps are not published. Line shape is "
            "unchanged, so parsers exercise identical code paths. The count is of "
            "the distinct aliases this profile's fixtures hold; a partial re-record "
            "numbers its own from app1, so it is a lower bound on packages redacted."
        ),
        "fixtures": entries,
    }
    manifest_path = recorded_dir / "MANIFEST.json"
    previous = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    )
    manifest = _merge_manifest(manifest, previous, partial=only is not None)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"\n{len(entries)} fixture(s) recorded -> {recorded_dir}")
    print(f"Device: {device.get('model')} API {device.get('api_level')} ({device.get('abi')})")
    if restore_failures:
        print(
            f"\n{len(restore_failures)} DEVICE STATE NOT RESTORED -- fix this "
            f"before unplugging:",
            file=sys.stderr,
        )
        for line in restore_failures:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nA display override survives the recorder exiting. Reset it by "
            "hand:\n  adb -s <serial> shell wm size reset\n"
            "  adb -s <serial> shell wm density reset",
            file=sys.stderr,
        )
    if failures:
        print(f"\n{len(failures)} fixture(s) NOT recorded:", file=sys.stderr)
        for name, reason in failures:
            print(f"  {name}: {reason}", file=sys.stderr)
        print("\nNothing was written for these. Re-run with --only once the", file=sys.stderr)
        print("device is healthy; a placeholder fixture is worse than none.", file=sys.stderr)
        return 1
    return 1 if restore_failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record verbatim adb/tool output as test fixtures.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--serial",
        "-s",
        help="Device serial; $ANDROID_SERIAL if omitted. With neither, the run is refused.",
    )
    parser.add_argument("--only", nargs="+", metavar="NAME", help="Record only these fixtures")
    parser.add_argument("--list", action="store_true", help="List fixtures and exit")
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=(
            "Device profile directory under fixtures/recorded/ "
            f"(default: {DEFAULT_PROFILE}). Name it after the device and API "
            "level, e.g. pixel4xl-api33."
        ),
    )
    args = parser.parse_args()

    if args.list:
        width = max(len(n) for n in _known_names())
        for fixture in FIXTURES:
            catches = f"  [catches {', '.join(fixture.catches)}]" if fixture.catches else ""
            if fixture.emulator_only:
                catches += "  [emulator only]"
            print(f"{fixture.name:<{width}}  {_command_of(fixture)}{catches}")
        print(
            f"{HIERARCHY_FIXTURE:<{width}}  adb exec-out uiautomator dump /dev/tty" "  [catches S7]"
        )
        print(f"{JAVA_VERSION_FIXTURE:<{width}}  <each host JDK>/bin/java -version  [host]")
        return

    sys.exit(record(args.serial, set(args.only) if args.only else None, args.profile))


if __name__ == "__main__":
    main()
