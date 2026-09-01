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
"""

import re
import shlex

from .adb_exec import AdbCommandError, AdbError, run_adb

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
        raise RuntimeError(
            "No devices connected. Start an emulator or connect a device:\n"
            "  emulator -avd <device-name>\n"
            "  adb devices"
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

        # Parse output
        # Format: Physical size: 1080x1920
        match = re.search(r"Physical size: (\d+)x(\d+)", result.stdout)
        if match:
            width = int(match.group(1))
            height = int(match.group(2))
            return (width, height)

        raise RuntimeError(
            f"Could not parse a screen size from `wm size`: {result.stdout.strip()!r}"
        )

    except AdbError:
        # Deliberately NOT a fallback. Callers derive tap and swipe coordinates
        # from this, so a guessed 1080x1920 on a tablet aims every gesture at
        # the wrong place and reports success -- a confident wrong answer, which
        # is worse than a failure the caller can see.
        raise


def get_ui_hierarchy(serial: str | None = None) -> dict:
    """
    Get UI hierarchy dump from device.

    Uses uiautomator dump to get XML UI hierarchy and converts to dict.

    Args:
        serial: Device serial (uses default if None)

    Returns:
        Dict representation of UI hierarchy

    Example:
        hierarchy = get_ui_hierarchy("emulator-5554")
        print(f"Found {len(hierarchy)} nodes")
    """
    try:
        # Dump UI hierarchy to device
        run_adb(
            "shell",
            serial,
            "uiautomator",
            "dump",
            "/sdcard/window_dump.xml",
            timeout=UI_DUMP_TIMEOUT,
            check=True,
        )

        # Pull XML file
        run_adb(
            "pull",
            serial,
            "/sdcard/window_dump.xml",
            "/tmp/window_dump.xml",
            timeout=UI_DUMP_TIMEOUT,
            check=True,
        )

        # Read and parse XML
        import xml.etree.ElementTree as ET

        tree = ET.parse("/tmp/window_dump.xml")
        root = tree.getroot()

        # Convert XML to dict structure
        return _xml_to_dict(root)

    except Exception as e:
        raise RuntimeError(f"Failed to get UI hierarchy: {e}") from e


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
