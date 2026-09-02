#!/usr/bin/env python3
"""
Shared device and emulator utilities for Android.

Common patterns for interacting with Android devices and emulators via adb.
Standardizes command building and device targeting to prevent errors.

Android equivalents:
- xcrun simctl -> adb
- IDB -> adb shell uiautomator / input
- iOS Simulator -> Android Emulator or real device

Used by:
- app_launcher.py - App lifecycle commands
- Multiple scripts - ADB command building
- navigator.py, gesture.py - Touch simulation
- test_recorder.py, app_state_capture.py - Auto-device detection
- privacy_manager.py, push_notification.py - Permission state from dumpsys

Permission parsing note
-----------------------
``dumpsys package <pkg>`` has NO ``granted permissions:`` section.
``privacy_manager --list`` looked for exactly that header, never found it, and
so answered every query with two empty lists while exiting 0. The sections that
do exist, recorded in ``tests/fixtures/recorded/*/dumpsys_package_*.txt``, sit
at four spaces of indent under ``Package [<pkg>]``::

    declared permissions:
      NAME: prot=signature|privileged
    requested permissions:
      NAME
    install permissions:
      NAME: granted=true
    User 0: ceDataInode=...
      runtime permissions:
        NAME: granted=false, flags=[ USER_SENSITIVE_WHEN_GRANTED|... ]

Two distinctions the old code did not make, and both matter to a caller.
*Install vs runtime*: an install permission is fixed at install time and
``pm grant`` on one raises a SecurityException, so a runtime permission is the
only kind a permission-flow test can move. *Granted vs merely requested*:
``requested permissions:`` is the manifest's ask, carrying no state at all, so
reading it as "granted" reports every denied permission as held.

Every section can also appear more than once, and the two repeats pull in
opposite directions -- which is why "find the header" is not enough on its own:

* An **updated system app** repeats all four sections under the top-level
  ``Hidden system packages:`` header. ``com.google.android.deskclock`` does
  this. Measured on API 35, that copy is not stale -- granting READ_CALENDAR
  flips ``granted`` in both copies -- so the cost of reading it is a doubled
  list, not a wrong value.
* A package in a **shared uid** has its runtime state tracked against the uid,
  not the package, so it is printed under ``Shared users:`` and NOT under
  ``Packages:``. ``com.android.settings`` does this, and so does the recorded
  ``com.android.localtransport``: their ``Packages:`` block has no
  ``runtime permissions:`` line at all, so a parser confined to ``Packages:``
  reports a system app as holding no runtime permissions whatsoever.

So the parser reads the whole dump and keeps the FIRST occurrence of each
section. That one rule covers both: the live copy is printed before the
``Hidden system packages:`` repeat, and user 0 -- the user ``pm grant``
targets without ``--user`` -- is printed before any secondary user. A
top-level allow-list of blocks was tried first and dropped: it was redundant
with this rule, and no recorded dump could tell the two apart, which makes it
a guard nothing could check.
"""

import re
import shlex

from .adb_exec import AdbCommandError, AdbError, run_adb
from .hierarchy import capture_hierarchy_dict

# Every adb call is bounded. An unbounded one wedges the adb connection for
# whatever runs next, which is a hang with no diagnosis rather than an error.
CURRENT_ACTIVITY_TIMEOUT = 15
PACKAGE_INFO_TIMEOUT = 20
# uiautomator has to wait for the UI to go idle before it can dump, which on
# an animating screen takes longer than an ordinary command.
UI_DUMP_TIMEOUT = 60


def quote_for_device_shell(value: str) -> str:
    """Quote a single argument for the shell running **on the device**.

    ``build_adb_command`` keeps the host safe by never using ``shell=True``, but
    that is only half the story: ``adb shell a b c`` concatenates the arguments
    and the device's own ``sh -c`` re-parses the result. An argument carrying
    ``;``, ``&``, backticks or ``$(...)`` therefore executes on the device --
    for ``run-as`` calls, as the target app's uid.

    Args:
        value: Raw text destined for a device-side command.

    Returns:
        The value quoted so the device shell treats it as one literal argument.

    Example:
        >>> quote_for_device_shell("x;id")
        "'x;id'"
        >>> quote_for_device_shell("com.example.app")
        'com.example.app'
    """
    return shlex.quote(value)


def build_adb_command(
    operation: str,
    serial: str | None = None,
    *args,
) -> list:
    """
    Build adb command with proper device handling.

    Standardizes command building to prevent device targeting bugs.
    Automatically omits -s flag if no serial provided (uses default device).

    Args:
        operation: adb operation (shell, install, uninstall, etc.)
        serial: Device serial number (omits -s if None)
        *args: Additional command arguments

    Returns:
        Complete command list ready for subprocess.run()

    Examples:
        # Start activity on default device
        cmd = build_adb_command("shell", None, "am", "start", "-n", "com.app/.MainActivity")
        # Returns: ["adb", "shell", "am", "start", "-n", "com.app/.MainActivity"]

        # Start on specific device
        cmd = build_adb_command("shell", "emulator-5554", "am", "start", "-n", "com.app/.MainActivity")
        # Returns: ["adb", "-s", "emulator-5554", "shell", "am", "start", "-n", "com.app/.MainActivity"]

        # Install APK
        cmd = build_adb_command("install", "emulator-5554", "-r", "/path/to/app.apk")
        # Returns: ["adb", "-s", "emulator-5554", "install", "-r", "/path/to/app.apk"]
    """
    cmd = ["adb"]

    # Add device targeting if specified
    if serial:
        cmd.extend(["-s", serial])

    # Add operation
    cmd.append(operation)

    # Add remaining arguments
    cmd.extend(str(arg) for arg in args)

    return cmd


def get_connected_devices() -> list:
    """
    Get list of connected Android devices and emulators.

    Queries adb devices and returns structured list.

    Returns:
        List of device dicts with keys:
        - "serial": Device serial (e.g., "emulator-5554", "ABC123")
        - "state": Device state ("device", "offline", "unauthorized")
        - "type": Device type ("emulator" or "device")

    Example:
        devices = get_connected_devices()
        for dev in devices:
            print(f"{dev['serial']} ({dev['type']}) - {dev['state']}")

        # Output:
        # emulator-5554 (emulator) - device
        # ABC123DEF456 (device) - device
    """
    try:
        result = run_adb("devices", None, "-l", check=True)

        devices = []
        # Parse output
        # Format:
        # List of devices attached
        # emulator-5554          device product:sdk_gphone64_x86_64 model:sdk_gphone64_x86_64 device:emu64x transport_id:1
        # ABC123DEF456           device product:redfin model:Pixel_5 device:redfin transport_id:2

        for line in result.stdout.split("\n")[1:]:  # Skip header
            line = line.strip()
            if not line:
                continue

            parts = line.split()
            if len(parts) >= 2:
                serial = parts[0]
                state = parts[1]

                # Determine device type
                device_type = "emulator" if serial.startswith("emulator-") else "device"

                devices.append({"serial": serial, "state": state, "type": device_type})

        return devices

    except AdbCommandError as e:
        raise RuntimeError(f"Failed to list devices: {e}") from e


def get_default_device() -> str | None:
    """
    Get default device serial (first available device).

    Returns:
        Device serial, or None if no devices connected

    Example:
        serial = get_default_device()
        if serial:
            print(f"Using device: {serial}")
        else:
            print("No devices connected")
    """
    devices = get_connected_devices()
    available = [d for d in devices if d["state"] == "device"]
    return available[0]["serial"] if available else None


def resolve_device_identifier(identifier: str | None) -> str | None:
    """
    Resolve device identifier to serial number.

    Supports multiple identifier formats:
    - Full serial: "emulator-5554" or "ABC123DEF456"
    - Partial match: "emulator" (matches first emulator)
    - Device type: "emulator" or "device" (matches first of that type)
    - None: Uses default device (first available)

    Args:
        identifier: Device serial, type, or None

    Returns:
        Full device serial, or None if should use default device

    Raises:
        RuntimeError: If identifier cannot be resolved

    Example:
        serial = resolve_device_identifier("emulator")
        # Returns: "emulator-5554" (first emulator)

        serial = resolve_device_identifier(None)
        # Returns: None (will use default device)

        serial = resolve_device_identifier("ABC123")
        # Returns: "ABC123DEF456" (partial match)
    """
    # If None, return None (caller will use default device)
    if identifier is None:
        return None

    devices = get_connected_devices()
    available = [d for d in devices if d["state"] == "device"]

    if not available:
        # Name what the caller asked for. The "not found" branch below does,
        # but only when something is attached -- so asking for a specific
        # serial with nothing plugged in used to get a generic "No devices
        # connected", dropping the one detail the caller supplied. For an
        # agent, whose next move depends on whether it named the wrong device
        # or has no device at all, that distinction is the whole answer.
        raise RuntimeError(
            f"Device '{identifier}' was requested, but no devices are connected. "
            f"Start an emulator or connect a device:\n"
            f"  emulator -avd <device-name>\n"
            f"  adb devices"
        )

    # Exact match
    exact = [d for d in available if d["serial"] == identifier]
    if exact:
        return exact[0]["serial"]

    # Type match (emulator/device)
    if identifier.lower() in ["emulator", "device"]:
        type_match = [d for d in available if d["type"] == identifier.lower()]
        if type_match:
            return type_match[0]["serial"]

    # Partial match
    partial = [d for d in available if identifier in d["serial"]]
    if partial:
        return partial[0]["serial"]

    # No match found
    raise RuntimeError(
        f"Device '{identifier}' not found. Available devices:\n"
        + "\n".join(f"  - {d['serial']} ({d['type']})" for d in available)
    )


def list_devices(device_type: str | None = None, state: str | None = None) -> list:
    """
    List Android devices with optional filtering.

    Queries adb and returns structured list of devices.
    Optionally filters by type (emulator/device) or state.

    Args:
        device_type: Optional filter - "emulator" or "device"
        state: Optional filter - "device" (ready), "offline", "unauthorized"

    Returns:
        List of device dicts with keys:
        - "serial": Device serial
        - "state": Device state
        - "type": Device type

    Example:
        # List all devices
        all_devs = list_devices()
        print(f"Total devices: {len(all_devs)}")

        # List only emulators
        emulators = list_devices(device_type="emulator")
        for emu in emulators:
            print(f"{emu['serial']} - {emu['state']}")

        # List only ready devices
        ready = list_devices(state="device")
        for dev in ready:
            print(f"Ready: {dev['serial']}")
    """
    devices = get_connected_devices()

    # Apply filters
    if device_type:
        devices = [d for d in devices if d["type"] == device_type]
    if state:
        devices = [d for d in devices if d["state"] == state]

    return devices


# `wm size` / `wm density` report a Physical line always, and an Override line
# only when one is set. The override is the EFFECTIVE value: uiautomator reports
# element bounds in it, so a parser that reads only Physical scales every tap by
# the wrong ratio (S9).
_PHYSICAL_SIZE_RE = re.compile(r"Physical size:\s*(\d+)x(\d+)")
_OVERRIDE_SIZE_RE = re.compile(r"Override size:\s*(\d+)x(\d+)")
_PHYSICAL_DENSITY_RE = re.compile(r"Physical density:\s*(\d+)")
_OVERRIDE_DENSITY_RE = re.compile(r"Override density:\s*(\d+)")


def parse_display_size(output: str) -> tuple[int, int]:
    """Return the effective (width, height) from ``wm size`` output.

    Prefers ``Override size:`` when present, because that is the resolution the
    device is actually rendering -- and therefore the one uiautomator reports
    bounds in.

    Args:
        output: Raw stdout of ``adb shell wm size``.

    Returns:
        (width, height) in pixels.

    Raises:
        RuntimeError: If neither line is present.

    Example:
        >>> parse_display_size("Physical size: 1080x2424\nOverride size: 1080x2400")
        (1080, 2400)
    """
    match = _OVERRIDE_SIZE_RE.search(output) or _PHYSICAL_SIZE_RE.search(output)
    if not match:
        raise RuntimeError(f"Could not parse a screen size from `wm size`: {output.strip()!r}")
    return (int(match.group(1)), int(match.group(2)))


def parse_display_density(output: str) -> int:
    """Return the effective dpi from ``wm density`` output.

    Prefers ``Override density:`` for the same reason as :func:`parse_display_size`.

    Args:
        output: Raw stdout of ``adb shell wm density``.

    Returns:
        Dots per inch.

    Raises:
        RuntimeError: If neither line is present.
    """
    match = _OVERRIDE_DENSITY_RE.search(output) or _PHYSICAL_DENSITY_RE.search(output)
    if not match:
        raise RuntimeError(f"Could not parse a density from `wm density`: {output.strip()!r}")
    return int(match.group(1))


def get_device_density(serial: str | None = None) -> int:
    """Query the device's effective screen density in dpi.

    Args:
        serial: Device serial (uses the default device if None).

    Returns:
        Dots per inch, honouring an active override.
    """
    result = run_adb("shell", serial, "wm", "density", check=True)
    return parse_display_density(result.stdout)


def get_device_screen_size(serial: str | None = None) -> tuple:
    """
    Get actual screen dimensions for device.

    Queries device via adb shell wm size.

    Args:
        serial: Device serial (uses default if None)

    Returns:
        Tuple of (width, height) in pixels

    Example:
        width, height = get_device_screen_size("emulator-5554")
        print(f"Device screen: {width}x{height}")
    """
    try:
        result = run_adb("shell", serial, "wm", "size", check=True)

        return parse_display_size(result.stdout)

    except AdbError:
        # Deliberately NOT a fallback. Callers derive tap and swipe coordinates
        # from this, so a guessed 1080x1920 on a tablet aims every gesture at
        # the wrong place and reports success -- a confident wrong answer, which
        # is worse than a failure the caller can see.
        raise


def get_ui_hierarchy(serial: str | None = None) -> dict:
    """
    Get the UI hierarchy as nested dicts.

    Thin wrapper over :func:`common.hierarchy.capture_hierarchy_dict`, kept
    because several scripts import this name. The capture itself now writes no
    temp file on either the device or the host; this used to dump to
    ``/sdcard/window_dump.xml`` and pull to ``/tmp/window_dump.xml``, a path
    shared with two other implementations (R4).

    Args:
        serial: Device serial (uses the default device if None).

    Returns:
        ``{"tag": str, "attributes": {...}, "children": [...]}``, with every
        attribute value left as the string uiautomator emitted.

    Raises:
        HierarchyError: If the hierarchy could not be captured or parsed.
    """
    return capture_hierarchy_dict(serial)


def _xml_to_dict(element) -> dict:
    """
    Convert XML element to dictionary.

    Args:
        element: XML element

    Returns:
        Dict representation of element
    """
    result = {"tag": element.tag, "attributes": dict(element.attrib), "children": []}

    for child in element:
        result["children"].append(_xml_to_dict(child))

    return result


def get_package_info(package_name: str, serial: str | None = None) -> dict:
    """
    Get package information for an app.

    Args:
        package_name: App package name (e.g., "com.example.app")
        serial: Device serial (uses default if None)

    Returns:
        Dict with package info

    Example:
        info = get_package_info("com.android.settings")
        print(f"Package: {info['package']}")
    """
    try:
        # The package name is re-parsed by the device shell, so it is quoted
        # like every other argument crossing that boundary. Bounded, too: an
        # unbounded adb call wedges the connection for whatever runs next.
        result = run_adb(
            "shell",
            serial,
            "pm",
            "dump",
            quote_for_device_shell(package_name),
            timeout=PACKAGE_INFO_TIMEOUT,
            check=True,
        )

        # Parse relevant info from pm dump output
        info = {"package": package_name, "installed": True}

        # Extract version code
        version_match = re.search(r"versionCode=(\d+)", result.stdout)
        if version_match:
            info["version_code"] = int(version_match.group(1))

        # Extract version name
        version_name_match = re.search(r"versionName=([^\s]+)", result.stdout)
        if version_name_match:
            info["version_name"] = version_name_match.group(1)

        return info

    except AdbCommandError:
        # `pm dump` exits non-zero for a package that is not installed, which is
        # an answer rather than a failure.
        return {"package": package_name, "installed": False}


# A section header, at whatever indent the dump chose. The indent is captured
# because it -- not a blank line or a heuristic -- is what ends the section:
# entries are strictly more indented than their header.
_SECTION_RE = re.compile(r"^(\s*)(declared|requested|install|runtime) permissions:\s*$")

# "NAME: granted=true, flags=[ A|B ]" -- the flags clause is present on runtime
# entries and absent on install ones.
_STATE_RE = re.compile(
    r"^(?P<name>\S+?):\s*granted=(?P<granted>true|false)"
    r"(?:,\s*flags=\[\s*(?P<flags>[^\]]*?)\s*\])?\s*$"
)

# Every real package has one. Its absence is how "not installed" is detected,
# because `dumpsys package <unknown>` exits 0 like everything else here.
_PACKAGES_HEADER = "Packages:"


def parse_package_permissions(dump: str) -> dict:
    """Parse ``adb shell dumpsys package <pkg>`` into permission state.

    Args:
        dump: Verbatim stdout of ``dumpsys package <pkg>``.

    Returns:
        A dict with:

        * ``found`` -- whether the dump described an installed package at all.
          ``dumpsys package <unknown>`` prints one line, ``Unable to find
          package: X``, and exits 0, so the exit status cannot be used and an
          empty result would otherwise read as "this app has no permissions".
        * ``declared`` -- permissions the app defines, as ``{name: protection}``.
        * ``requested`` -- names from the manifest, in dump order. Requested is
          not held; see ``granted``.
        * ``install`` -- ``[{"permission", "granted"}]`` for install-time
          permissions.
        * ``runtime`` -- ``[{"permission", "granted", "flags"}]`` for runtime
          permissions, for user 0.
        * ``granted`` -- names actually held, install and runtime together.
        * ``denied`` -- names the dump reports with ``granted=false``.

        A dump with no ``Packages:`` block yields the same keys, all empty,
        and ``found`` False.
    """
    declared: dict[str, str] = {}
    requested: list[str] = []
    install: list[dict] = []
    runtime: list[dict] = []
    seen_sections: set[str] = set()

    section: str | None = None
    section_indent = 0
    found = False

    for line in dump.splitlines():
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip())
        if indent == 0:
            found = found or line.strip() == _PACKAGES_HEADER
            section = None
            continue

        if section is not None and indent <= section_indent:
            section = None

        header = _SECTION_RE.match(line)
        if header is not None:
            name = header.group(2)
            # First occurrence wins, and it is the only thing keeping the
            # repeats out. See the module docstring: a multi-user device
            # prints one "runtime permissions:" per "User N:" and user 0 --
            # what `pm grant` without --user targets -- comes first, and an
            # updated system app repeats every section under "Hidden system
            # packages:" AFTER the live copy.
            section = None if name in seen_sections else name
            seen_sections.add(name)
            section_indent = len(header.group(1))
            continue

        if section is None:
            continue

        entry: str = line.strip()
        if section == "requested":
            requested.append(entry)
            continue

        name, _, detail = entry.partition(":")
        if section == "declared":
            declared[name] = detail.strip().removeprefix("prot=")
            continue

        state = _STATE_RE.match(entry)
        if state is None:
            # Never guess. An unparsed line is a format change, and inventing a
            # value for it is how this script came to report an empty list as
            # the truth.
            continue
        record: dict = {
            "permission": state.group("name"),
            "granted": state.group("granted") == "true",
        }
        if section == "install":
            install.append(record)
        else:
            flags = state.group("flags") or ""
            record["flags"] = [f for f in flags.split("|") if f]
            runtime.append(record)

    held = [e["permission"] for e in install + runtime if e["granted"]]
    denied = [e["permission"] for e in install + runtime if not e["granted"]]
    return {
        "found": found,
        "declared": declared,
        "requested": requested,
        "install": install,
        "runtime": runtime,
        "granted": held,
        "denied": denied,
    }


def permission_state(dump: str, permission: str) -> bool | None:
    """Whether ``permission`` is held, according to the dump itself.

    Args:
        dump: Verbatim stdout of ``dumpsys package <pkg>``.
        permission: Full permission name, e.g. ``android.permission.CAMERA``.

    Returns:
        ``True`` or ``False`` as the dump reports it, or ``None`` when the
        package lists no state for it at all -- which is what a ``pm grant``
        of an unrequested permission leaves behind. ``None`` is deliberately
        not ``False``: "the app does not ask for this" and "the user said no"
        are different answers, and only the first means the caller's command
        could never have worked.
    """
    parsed = parse_package_permissions(dump)
    for entry in parsed["install"] + parsed["runtime"]:
        if entry["permission"] == permission:
            return bool(entry["granted"])
    return None


def list_installed_packages(serial: str | None = None) -> list:
    """
    List all installed packages on device.

    Args:
        serial: Device serial (uses default if None)

    Returns:
        List of package names

    Example:
        packages = list_installed_packages("emulator-5554")
        print(f"Found {len(packages)} packages")
    """
    try:
        result = run_adb("shell", serial, "pm", "list", "packages", check=True)

        # Parse output
        # Format: package:com.android.settings
        packages = []
        for line in result.stdout.split("\n"):
            if line.startswith("package:"):
                packages.append(line.replace("package:", "").strip())

        return packages

    except AdbCommandError as e:
        raise RuntimeError(f"Failed to list packages: {e}") from e


# Focused-window lines from `dumpsys window`, e.g.
#   mCurrentFocus=Window{c63b9b5 u0 com.pkg/com.pkg.MainActivity}
#   mFocusedApp=ActivityRecord{ba1d946 u0 com.pkg/.MainActivity t615}
_FOCUS_LINE_RE = re.compile(r"^\s*(?:mCurrentFocus|mFocusedApp)=(?P<body>.*)$", re.MULTILINE)
_COMPONENT_RE = re.compile(r"([A-Za-z][A-Za-z0-9_.]*/[A-Za-z0-9_.]+)")


def parse_focused_activity(dumpsys_output: str) -> str | None:
    """Extract the focused ``package/activity`` component from `dumpsys window`.

    Pure function so it can be tested against recorded device output; the adb
    call lives in :func:`get_current_activity`.

    Args:
        dumpsys_output: Text from ``adb shell dumpsys window``.

    Returns:
        The component, or None when nothing is focused.

    Example:
        >>> parse_focused_activity("  mCurrentFocus=Window{a u0 com.x/com.x.Main}")
        'com.x/com.x.Main'
    """
    for match in _FOCUS_LINE_RE.finditer(dumpsys_output):
        body = match.group("body")
        if "null" in body:
            continue
        component = _COMPONENT_RE.search(body)
        if component:
            return component.group(1)
    return None


def get_current_activity(serial: str | None = None) -> str | None:
    """
    Get currently focused activity.

    Args:
        serial: Device serial (uses default if None)

    Returns:
        Activity name (e.g., "com.example.app/.MainActivity"), or None if not found

    Example:
        activity = get_current_activity("emulator-5554")
        if activity:
            print(f"Current activity: {activity}")
    """
    try:
        # Filtering happens in Python, not via a device-side pipeline. The
        # previous version built an argv list containing a literal "|" and
        # "grep" and ran it with shell=True -- which on POSIX executes only
        # argv[0] (bare `adb`) and passes the rest as $0, $1, ... So stdout was
        # always empty and this always returned None.
        result = run_adb("shell", serial, "dumpsys", "window", timeout=CURRENT_ACTIVITY_TIMEOUT)
        return parse_focused_activity(result.stdout)

    except (AdbError, OSError):
        return None


def transform_screenshot_coords(
    x: float,
    y: float,
    screenshot_width: int,
    screenshot_height: int,
    device_width: int,
    device_height: int,
) -> tuple:
    """
    Transform screenshot coordinates to device coordinates.

    Handles the case where a screenshot was downscaled (e.g., to 'half' size)
    and needs to be transformed back to actual device pixel coordinates
    for accurate tapping.

    The transformation is linear:
    device_x = (screenshot_x / screenshot_width) * device_width
    device_y = (screenshot_y / screenshot_height) * device_height

    Args:
        x, y: Coordinates in the screenshot
        screenshot_width, screenshot_height: Screenshot dimensions (e.g., 540, 960)
        device_width, device_height: Actual device dimensions (e.g., 1080, 1920)

    Returns:
        Tuple of (device_x, device_y) in device pixels

    Example:
        # Screenshot taken at 'half' size: 540x960 (from 1080x1920 device)
        device_x, device_y = transform_screenshot_coords(
            100, 200,  # Tap point in screenshot
            540, 960,  # Screenshot dimensions
            1080, 1920  # Device dimensions
        )
        print(f"Tap at device coords: ({device_x}, {device_y})")
        # Output: Tap at device coords: (200, 400)
    """
    device_x = int((x / screenshot_width) * device_width)
    device_y = int((y / screenshot_height) * device_height)
    return (device_x, device_y)
