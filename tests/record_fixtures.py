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

Two documented departures from verbatim, both visible in the code below:
CRLF is normalised to LF (``_strip_cr``), and non-AOSP package names are
redacted. The CRLF one has a cost worth knowing: the emulator console really
does answer ``Pixel_9\r\nOK\r\n``, and that CR is what caused defect S5, so
no fixture can reproduce S5. ``tests/test_emu_console.py`` covers the CRLF
framing with constructed input and records the measured bytes in its docstring.

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
class Fixture:
    """One recorded artifact.

    Attributes:
        name: Basename written under ``recorded/`` (extension appended).
        args: Arguments after ``adb [-s SERIAL]``.
        description: What a parser test uses this for.
        ext: File extension; ``xml`` for hierarchy dumps, else ``txt``.
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


class CaptureError(RuntimeError):
    """Raised when a fixture could not be captured.

    Deliberately fatal for that fixture: a half-captured or timed-out artifact
    must never reach disk. Writing a placeholder would recreate precisely the
    defect this whole exercise exists to eliminate — a file that looks like
    ground truth and is not.
    """


def _run(args: list[str], serial: str | None, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run one adb command and return its output.

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

    # Some tools (am broadcast, avdmanager) write the interesting part to stderr.
    text = result.stdout if result.stdout.strip() else result.stderr

    # dumpsys reports its own internal timeout in-band and still exits 0.
    if "DUMP TIMEOUT" in text:
        raise CaptureError("device-side dumpsys timeout (device is loaded or unhealthy)")
    return text


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
    scripts = (
        Path(__file__).resolve().parents[1]
        / "android-emulator-skill"
        / "skills"
        / "android-emulator-skill"
        / "scripts"
    )
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from common.sdk_tools import EMULATOR_NOT_FOUND_MESSAGE, get_emulator_path

    resolved = get_emulator_path()
    if not resolved:
        raise CaptureError(EMULATOR_NOT_FOUND_MESSAGE)
    return resolved


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


def _run_host(argv: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a command on the HOST and return its output.

    For the one case where the text a parser consumes is not produced by adb:
    `sqlite3` is absent from Android user builds (verified missing on API 35),
    so `model_inspector`'s working path pulls the database and runs the host's
    sqlite3 against it. Recording the adb call instead would capture the pull,
    not the schema the parser actually reads.
    """
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise CaptureError(f"{argv[0]} is not installed on this host") from exc
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"timed out after {timeout}s") from exc

    text = result.stdout if result.stdout.strip() else result.stderr
    if not text.strip():
        raise CaptureError(f"{' '.join(argv)} produced no output")
    return text


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
    ),
    Fixture(
        name="wm_density_override",
        args=["shell", "wm", "density"],
        description=(
            "`wm density` with an override active — emits BOTH 'Physical density:' "
            "and 'Override density:'. Needed for a correct px->dp conversion."
        ),
        catches=("S7", "S9"),
        setup=[["shell", "wm", "density", "560"]],
        teardown=[["shell", "wm", "density", "reset"]],
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
        ext="txt",
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
]


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
    raw = _run(["exec-out", "uiautomator", "dump", "/dev/tty"], serial, timeout)
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
        meta[key] = _strip_cr(_run(["shell", "getprop", prop], serial, timeout=15)).strip()
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
    redactions: dict[str, str] = {}

    def _record_one(name: str, command: str, description: str, catches: list[str], text: str):
        ext = "xml" if name.startswith("uiautomator") else "txt"
        path = recorded_dir / f"{name}.{ext}"
        path.write_text(text, encoding="utf-8")
        size = len(text.encode("utf-8"))
        entries.append(
            {
                "name": name,
                "file": path.name,
                "command": command,
                "description": description,
                "catches": catches,
                "bytes": size,
            }
        )
        print(f"  ok    {path.name} ({size} bytes)")

    scratch = Path(tempfile.mkdtemp(prefix="android_emu_record_"))

    for fixture in selected:
        for cmd in fixture.setup:
            _run([a.format(tmp=scratch) for a in cmd], serial)
        if fixture.prepare is not None:
            fixture.prepare(scratch, serial)
        try:
            if fixture.host_argv is not None:
                raw = _run_host(
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
                raw = _run(fixture.args, serial, fixture.timeout)
            text = _redact_private_packages(_strip_cr(raw), redactions)
            if fixture.post is not None:
                text = fixture.post(text)
        except CaptureError as exc:
            failures.append((fixture.name, str(exc)))
            print(f"  FAIL  {fixture.name}: {exc}", file=sys.stderr)
            continue
        finally:
            for cmd in fixture.teardown:
                _run(cmd, serial)
        _record_one(
            fixture.name,
            (
                " ".join(fixture.host_argv)
                if fixture.host_argv is not None
                else "adb " + " ".join(fixture.args)
            ),
            fixture.description,
            list(fixture.catches),
            text,
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
    if failures:
        print(f"\n{len(failures)} fixture(s) NOT recorded:", file=sys.stderr)
        for name, reason in failures:
            print(f"  {name}: {reason}", file=sys.stderr)
        print("\nNothing was written for these. Re-run with --only once the", file=sys.stderr)
        print("device is healthy; a placeholder fixture is worse than none.", file=sys.stderr)
        return 1
    return 0


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
            print(f"{fixture.name:<{width}}  adb {' '.join(fixture.args)}{catches}")
        print(
            f"{HIERARCHY_FIXTURE:<{width}}  adb exec-out uiautomator dump /dev/tty" "  [catches S7]"
        )
        return

    sys.exit(record(args.serial, set(args.only) if args.only else None, args.profile))


if __name__ == "__main__":
    main()
