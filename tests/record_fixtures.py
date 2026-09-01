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

Fixtures are written byte-for-byte, with no header or annotation, so a parser
under test sees exactly what it would see from the real tool. Provenance lives
alongside in ``MANIFEST.json``.

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
import platform
import re
import subprocess
import sys
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


def _hierarchy_of(package: str) -> Callable[[str], str]:
    """Trim an ``exec-out uiautomator dump`` stream to just its XML document.

    uiautomator writes its status line ("UI hierchary dumped to: /dev/tty" --
    Android's typo, not ours) onto the same stream, so the raw bytes are not a
    parseable document.

    The returned callable also asserts the dump is of ``package``. A dump always
    succeeds: if the activity never launched, uiautomator happily describes the
    launcher instead, and the result would be a file that looks like a Compose
    hierarchy and is not -- the exact failure this recorder exists to prevent.
    """

    def trim(raw: str) -> str:
        start = raw.find("<?xml")
        if start == -1:
            raise CaptureError(f"no XML in uiautomator output: {raw.strip()[:120]!r}")
        end = raw.rfind("</hierarchy>")
        if end == -1:
            raise CaptureError("uiautomator output truncated: no closing </hierarchy>")
        xml = raw[start : end + len("</hierarchy>")] + "\n"
        if f'package="{package}"' not in xml:
            raise CaptureError(
                f"dump contains no {package!r} node: the activity did not launch. "
                f"Build and install tests/fixtures/scaffold/compose first."
            )
        return xml

    return trim


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

    for fixture in selected:
        for cmd in fixture.setup:
            _run(cmd, serial)
        try:
            text = _redact_private_packages(
                _strip_cr(_run(fixture.args, serial, fixture.timeout)), redactions
            )
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
            "adb " + " ".join(fixture.args),
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
