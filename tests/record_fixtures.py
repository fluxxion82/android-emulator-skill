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

Three documented departures from verbatim, all visible in the code below:

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

Binary fixtures are the exception to (1) and (2): the status-bar PNGs and
``am_profile_hang.trace`` are written byte for byte, with no newline
translation and no aliasing, because either would corrupt them -- the trace's
record stream is little-endian integers, and a 0x0d byte in a clock value is
not a line ending. Aliasing never sees their bytes, so each carries its own
content check instead: the PNGs are cropped to the status bar, and the trace
is refused unless every class it names is AOSP, Jetpack or the fixture app
(``_check_hang_trace``).

Usage
-----
    python tests/record_fixtures.py --list
    python tests/record_fixtures.py                 # record everything
    python tests/record_fixtures.py --only logcat_threadtime wm_size_override
    python tests/record_fixtures.py --serial emulator-5554

Requires a booted device or emulator. Re-run after an Android version bump and
commit the diff: a fixture that changes shape is exactly the signal you want.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
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
    prepare: Callable[[Path, str | None], None] | None = None
    host_argv: list[str] | None = None
    capture_bytes: Callable[[str | None, dict], bytes] | None = None
    record_both_streams: bool = False
    teardown_verify: Callable[[str | None], None] | None = None
    metadata: Callable[[str | None], dict] | None = None
    capture_with: Callable[[Path, str | None, dict], Capture] | None = None
    command: str | None = None


class CaptureError(RuntimeError):
    """Raised when a fixture could not be captured.

    Deliberately fatal for that fixture: a half-captured or timed-out artifact
    must never reach disk. Writing a placeholder would recreate precisely the
    defect this whole exercise exists to eliminate — a file that looks like
    ground truth and is not.
    """


def _run(args: list[str], serial: str | None, timeout: int = DEFAULT_TIMEOUT) -> Capture:
    """Run one adb command and return both streams plus its exit status.

    Raises:
        CaptureError: on timeout, or when the device reports a dump timeout.

    A non-zero exit is *not* an error here: "what this prints when it goes
    wrong" is itself worth recording (see am_broadcast_missing_receiver, whose
    entire point is that a doomed broadcast still exits 0).
    """
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"timed out after {timeout}s") from exc

    capture = Capture(result.stdout, result.stderr, result.returncode)

    # dumpsys reports its own internal timeout in-band and still exits 0.
    if "DUMP TIMEOUT" in capture.text:
        raise CaptureError("device-side dumpsys timeout (device is loaded or unhealthy)")
    return capture


def _run_bytes(args: list[str], serial: str | None, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """Run one adb command and return its stdout as raw bytes.

    ``_run`` decodes with ``text=True``, which mangles a PNG beyond repair.
    Used only by the screencap fixtures.
    """
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"timed out after {timeout}s") from exc
    if result.returncode != 0 or not result.stdout:
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


def _pull_app_database(scratch: Path, serial: str | None) -> None:
    """Copy the fixture app's database to the host, byte for byte.

    `adb exec-out` rather than a device-side redirect: `run-as ... cat > /sdcard/x`
    runs the redirect as the shell uid while `cat` runs as the app uid, and the
    measured result is a 0-byte file. exec-out streams the bytes straight to the
    host with no pty and no intermediate file.
    """
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += ["exec-out", "run-as", "com.example.composefixture", "cat", "databases/fixture.db"]

    result = subprocess.run(cmd, capture_output=True, timeout=DEFAULT_TIMEOUT, check=False)
    if result.returncode != 0 or not result.stdout:
        raise CaptureError(
            "could not read the fixture app's database. Install it first:\n"
            "  cd tests/fixtures/scaffold/compose && gradle :app:installDebug\n"
            "then launch it once so DefaultActivity seeds the data dir."
        )
    (scratch / "fx.db").write_bytes(result.stdout)


def _run_host(
    argv: list[str], timeout: int = DEFAULT_TIMEOUT, *, stdin: str | None = None
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
    """
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            input=stdin,
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


# The Compose scaffold under tests/fixtures/scaffold/compose. Two exported
# activities render the same screen; only the root Modifier differs. See that
# directory's AndroidManifest.xml for the build-and-install commands.
COMPOSE_PACKAGE = "com.example.composefixture"


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


def _status_bar_strip(serial: str | None, extra: dict) -> bytes:
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


def _display_restored(*, size: bool = False, density: bool = False) -> Callable[[str | None], None]:
    """Read the display state back and refuse to believe an unverified reset.

    `wm size reset` prints nothing whether it worked or not, so the recorder
    used to take its silence as success. It is the only thing standing between
    a recording run and leaving somebody's phone at 1080x2400 -- the override
    survives the process exiting, and no other check in this file would notice.

    So the reset is verified the way a person would verify it: read `wm size`
    back and require the Override line to be gone.
    """

    def verify(serial: str | None) -> None:
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


def _device_clock(serial: str | None) -> str:
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


def _status_bar_strip_showing(demo_clock_applied: bool) -> Callable[[str | None, dict], bytes]:
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

    def capture(serial: str | None, extra: dict) -> bytes:
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


def _device_path_absent(listing: list[str]) -> Callable[[str | None], None]:
    """Read a teardown's deletion back instead of trusting ``rm``'s silence.

    ``rm -f`` exits 0 whether or not there was anything to remove, so its
    status says nothing. The listing must fail AND say why: a listing refused
    for permission would also exit non-zero, and would prove nothing.
    """

    def verify(serial: str | None) -> None:
        result = _run(listing, serial, timeout=15)
        if result.returncode == 0 or "No such file" not in result.text:
            raise CaptureError(
                f"`adb {' '.join(listing)}` does not show the path gone after "
                f"teardown (exit {result.returncode}): {result.text.strip()[:200]!r}"
            )

    return verify


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
# on a pool thread with no keep-alive -- to start AND exit. A thread exiting
# while `am profile --sampling` runs can kill the app: measured once in three
# runs here, an ART `thread.cc:2698 Check failed: tlsPtr_.method_trace_buffer ==
# nullptr` abort in that worker 57 ms after it started, and the trace was lost
# with the process. (The ANR held the worker's message behind the blocked main
# thread, so it ran the moment main unblocked, mid-trace.)
TRACE_QUIET_BEFORE_S = "10"

# Every class a committed trace may name. The trace is binary, so the package
# aliaser never sees it; this is its privacy check instead. The four added
# here are public runtime code a method trace reaches and text output so far
# has not: the JDK internals ART ships (jdk.internal.misc.Unsafe,
# sun.misc.Cleaner), libcore's DDM server (org.apache.harmony.dalvik.ddmc --
# a debuggable app answers the host's debugger polling on it), and
# kotlinx.coroutines, which the Compose fixture app runs on. Each was added
# because a real capture naming it was refused, which is the check working.
_TRACE_PUBLIC_PREFIXES = (
    *_PUBLIC_PACKAGE_PREFIXES,
    "jdk.",
    "sun.",
    "kotlinx.",
    "org.apache.harmony.",
)

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
    if not any(line.split("\t")[-1] == "main" for line in threads):
        raise CaptureError("no `main` thread in *threads")

    classes = {line.split("\t")[1] for line in methods if line.count("\t") >= 2}
    if f"{COMPOSE_PACKAGE}.AnrReceiver" not in classes:
        raise CaptureError(
            "AnrReceiver is not in the trace: the broadcast did not land while the "
            "profiler was running"
        )
    private = sorted(name for name in classes if not name.startswith(_TRACE_PUBLIC_PREFIXES))
    if private:
        raise CaptureError(
            f"the trace names classes outside AOSP, Jetpack and the fixture app "
            f"({private[:5]}); refusing to commit it"
        )


def _wait_for_complete_file(serial: str | None, path: str, *, within_s: int = 20) -> int:
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


def _profile_hang_trace(serial: str | None, extra: dict) -> bytes:
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


def _profile_refusal(
    destination: str, listing: list[str]
) -> Callable[[Path, str | None, dict], Capture]:
    """`am profile start` to a destination it refuses, and what the refusal leaves.

    The listing is taken here, not in teardown, because teardown deletes what it
    would show. For /sdcard that is the point: the refusal still leaves a 0-byte
    file behind, so "the trace file exists" is no evidence profiling started.
    """

    def capture(_scratch: Path, serial: str | None, extra: dict) -> Capture:
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

# Named so that nobody has one. The run refuses to start if somebody does,
# because the lifecycle below DELETES an AVD by each of these names.
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


def _throwaway_avd_removed(_serial: str | None) -> None:
    """Teardown check for the four avdmanager fixtures: nothing left on disk."""
    home = _avd_home()
    if _avd_present(home, THROWAWAY_AVD):
        raise CaptureError(
            f"the throwaway AVD is still on this host under {_scrub_home_paths(str(home))}. "
            f"Remove it: avdmanager delete avd --name {THROWAWAY_AVD}"
        )


class _ThrowawayAvd:
    """Create an AVD, read its config.ini, delete it, and prove it is gone.

    Four fixtures come out of one lifecycle, and the order is the point:
    config.ini exists only between the create and the delete. So the lifecycle
    runs once per recorder run, the first time any of the four is asked for,
    and each fixture takes its piece -- `--only avdmanager_delete` still
    creates first, and `--only avdmanager_create` still deletes after. The
    delete is in a ``finally``, and the end state is read back from disk and
    from `avdmanager list avd -c` rather than trusted: the outcome this must
    never have is an AVD left on somebody's machine.
    """

    def __init__(self) -> None:
        self._captures: dict[str, Capture] | None = None
        self._config_ini = ""
        self._error: CaptureError | None = None

    def capture(self, name: str) -> Callable[[Path, str | None, dict], Capture]:
        """A ``capture_with`` for one of the three avdmanager commands."""

        def get(_scratch: Path, _serial: str | None, extra: dict) -> Capture:
            captures = self._run_once()
            extra["argv"] = _AVD_ARGV[name]
            return captures[name]

        return get

    def config_ini(self, _serial: str | None, extra: dict) -> bytes:
        """A ``capture_bytes`` for the config.ini, read between create and delete.

        Bytes rather than text so the package aliaser never sees it: its keys
        are dotted lowercase names (`hw.camera.back`, `image.sysdir.1`) that the
        aliaser's pattern takes for package names, and it would rewrite the
        keys a parser looks up. Nothing in it is private -- avdmanager writes it
        from the system image and the device definition -- and the home
        directory is scrubbed all the same.
        """
        self._run_once()
        extra["source"] = (
            f"<avd home>/{THROWAWAY_AVD}.avd/config.ini, read after avdmanager_create "
            f"and before avdmanager_delete"
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
        home = _avd_home()
        for name in (THROWAWAY_AVD, MISSING_AVD):
            if _avd_present(home, name):
                raise CaptureError(
                    f"an AVD named {name!r} already exists on this host. This "
                    f"recorder deletes AVDs by that name and will not touch one it "
                    f"did not create; if it is a leftover from an earlier run, "
                    f"remove it with: avdmanager delete avd --name {name}"
                )

        def run(name: str, stdin: str | None = None) -> Capture:
            argv = [tool if arg == "{avdmanager}" else arg for arg in _AVD_ARGV[name]]
            return _run_host(argv, AVD_TOOL_TIMEOUT, stdin=stdin)

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
                self._config_ini = config_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise CaptureError(
                    f"the AVD was created but its config.ini is unreadable: {exc}"
                ) from exc
        finally:
            if _avd_present(home, THROWAWAY_AVD):
                captures["avdmanager_delete"] = run("avdmanager_delete")

        captures["avdmanager_delete_missing"] = run("avdmanager_delete_missing")

        # Read back both ways: the files on disk, and what avdmanager itself
        # still lists. Not through _run_host, which refuses empty output -- a
        # host whose only AVD was this one lists nothing, and that is success.
        try:
            listing = subprocess.run(
                [tool, "list", "avd", "-c"],
                capture_output=True,
                text=True,
                timeout=AVD_TOOL_TIMEOUT,
                check=False,
            ).stdout.split()
        except subprocess.TimeoutExpired as exc:
            raise CaptureError("could not list AVDs to confirm the delete") from exc
        if _avd_present(home, THROWAWAY_AVD) or THROWAWAY_AVD in listing:
            raise CaptureError(
                f"{THROWAWAY_AVD} is still present after `avdmanager delete`. Remove "
                f"it by hand: avdmanager delete avd --name {THROWAWAY_AVD}"
            )
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


FIXTURES: list[Fixture] = [
    # --- device discovery -------------------------------------------------
    Fixture(
        name="adb_devices_single",
        args=["devices", "-l"],
        description="`adb devices -l` with exactly one device attached.",
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
        args=["shell", "wm", "size"],
        description="`wm size` with no override set — only a 'Physical size:' line.",
        setup=[["shell", "wm", "size", "reset"]],
    ),
    Fixture(
        name="wm_density_physical",
        args=["shell", "wm", "density"],
        description="`wm density` with no override set — only a 'Physical density:' line.",
        setup=[["shell", "wm", "density", "reset"]],
    ),
    Fixture(
        name="wm_size_override",
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
            "Correct launcher-activity resolution. app_launcher instead greps "
            "`pm dump` for the first line containing 'Activity' and the package, "
            "which readily returns an ActivityRecord or a provider."
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
        name="emu_sms_send",
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
    Fixture(
        name="logcat_choreographer_jank",
        args=["shell", "logcat -d -v threadtime | grep Choreographer"],
        description=(
            "Real Choreographer jank: "
            "'I Choreographer: Skipped N frames!  The application may be doing "
            "too much work on its main thread.' TWO spaces after the "
            "exclamation mark, and the tag carries the janking app's own pid in "
            "the logcat columns -- unlike the ActivityManager ANR lines, whose "
            "pid is system_server's. Recorded because every skipped-frame test "
            "in this repo built its input from an f-string, which proves only "
            "that the parser matches the f-string. Provoked by AnrReceiver's "
            "40s block: the recorded line says 'Skipped 2401 frames', and "
            "2401 * 16.7ms is 40.1 seconds -- the first independent check this "
            "repo has that the FRAME_MS heuristic tracks wall-clock, on a stall "
            "whose real length is known. A cold start alone also janks, but "
            "only when the host happens to be busy, so how MANY lines land here "
            "is not fixed; the severity-boundary tests vary the frame count off "
            "this line rather than typing new ones."
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
        host_argv=["adb", "shell", "getprop", "ro.build.version.sdk"],
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
        name="pm_grant_not_changeable",
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
            "first: a thread exiting under the sampler killed the app once in "
            "three runs (see TRACE_QUIET_BEFORE_S). Note too that `am "
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
        args=[
            "shell",
            f"run-as {COMPOSE_PACKAGE} sh -c 'mkdir -p files/aes_fx && cd files/aes_fx "
            f'&& echo x > "a b.txt" && ln -s "a b.txt" link && ls -la\'',
        ],
        description=(
            "`run-as ls -la` of a directory holding a filename with a space and "
            "a symlink to it, made for the purpose in the fixture app's files/ "
            "and removed after. The three things parse_ls_output documents and "
            "no other recording had. What the device actually prints: toybox's "
            "ISO layout ('YYYY-MM-DD HH:MM', two tokens) -- NOT the coreutils "
            "'Mon DD YYYY' layout the parser also claims, which this device's "
            "`ls` does not produce and no caller of the parser feeds it (all "
            "three run `run-as ... ls -la` on the device); the symlink as "
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
            "`AvdId` key and no `avd.ini.displayname` (Android Studio writes "
            "both; the command-line tool writes neither), so a display name "
            "read from it is empty for every AVD emulator_create makes; "
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
            "`sdkmanager --list_installed`, and the reason AVD creation could "
            "never work. emulator_create looked for lines starting "
            "`system-images;` and split them on `|`. sdkmanager prints "
            "`  system-images/android-34/google_apis/arm64-v8a` -- SLASHES, and "
            "whitespace-padded columns with no pipe anywhere. So the installed "
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
            "`sdkmanager --list` -- what `--list-images` reads, and inert for "
            "the same reason its sibling was: it looked for `system-images;` "
            "lines split on `|`, and sdkmanager prints neither. The rows are "
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
    return {f.name for f in FIXTURES} | {HIERARCHY_FIXTURE}


def _capture_hierarchy(serial: str | None, timeout: int = DEFAULT_TIMEOUT) -> str:
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


def _device_metadata(serial: str | None) -> dict[str, str]:
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


def record(serial: str | None, only: set[str] | None, profile: str) -> int:
    """Record fixtures, write MANIFEST.json, and report what was written."""
    recorded_dir = RECORDED_ROOT / profile
    recorded_dir.mkdir(parents=True, exist_ok=True)

    selected = [f for f in FIXTURES if only is None or f.name in only]
    if only:
        # The hierarchy dump is captured out-of-band (it needs exec-out, not the
        # plain adb call every other fixture uses), so it is not in FIXTURES.
        unknown = only - {f.name for f in FIXTURES} - {HIERARCHY_FIXTURE}
        if unknown:
            print(f"Unknown fixture(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            print(f"Known: {', '.join(sorted(_known_names()))}", file=sys.stderr)
            return 2

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
                "adb shell uiautomator dump /dev/tty",
                (
                    "A real uiautomator hierarchy. Note bounds are in physical "
                    "PIXELS, which is what makes accessibility_audit's comparison "
                    "against a 48 'dp' constant wrong."
                ),
                ["S7"],
                hierarchy,
                ext="xml",
            )

    manifest = {
        "profile": profile,
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recorded_on": platform.platform(),
        "device": device,
        "redacted_package_count": len(redactions),
        "redaction_note": (
            "Non-AOSP package names were replaced with stable com.example.appN "
            "aliases so a developer's own apps are not published. Line shape is "
            "unchanged, so parsers exercise identical code paths."
        ),
        "fixtures": entries,
    }
    manifest_path = recorded_dir / "MANIFEST.json"
    # Merge with any previous run so a partial re-record keeps earlier provenance.
    if manifest_path.exists() and only is not None:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        recorded_now = {entry["name"] for entry in entries}
        entries = [
            e for e in previous.get("fixtures", []) if e["name"] not in recorded_now
        ] + entries
        manifest["fixtures"] = sorted(entries, key=lambda e: e["name"])
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"\n{len(entries)} fixture(s) -> {recorded_dir}")
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
    parser.add_argument("--serial", "-s", help="Device serial (auto-detects if omitted)")
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
            print(f"{fixture.name:<{width}}  {_command_of(fixture)}{catches}")
        print(
            f"{HIERARCHY_FIXTURE:<{width}}  adb exec-out uiautomator dump /dev/tty" "  [catches S7]"
        )
        return

    sys.exit(record(args.serial, set(args.only) if args.only else None, args.profile))


if __name__ == "__main__":
    main()
