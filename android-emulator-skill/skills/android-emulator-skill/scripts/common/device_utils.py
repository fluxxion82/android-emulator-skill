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
section: the live copy is printed before the ``Hidden system packages:``
repeat. Runtime sections are the exception, because they are per user: one
``runtime permissions:`` per ``User N:`` section, in both layouts (under the
package, and under the ``SharedUser`` in ``Shared users:``). Those are chosen
by their ``User N:`` label -- user 0, what ``pm grant`` targets without
``--user``, unless the caller names another -- and a runtime section under
any other user is never read in its place. Position used to pick user 0, and
that is what let a ``--user 11`` grant be "verified" against user 0. The
``User N:`` labels are only read under ``Packages:``, ``Hidden system
packages:`` and ``Shared users:``: ``Queries:`` prints ``User 0:`` lines at
the same indent with a different meaning. And a ``SharedUser [<name>]`` block
is read only when ``<name>`` is the package's own ``sharedUser=`` value; any
other shared user's block is skipped whole.
"""

import re
import shlex
from collections.abc import Iterable

from .adb_exec import AdbCommandError, AdbError, DeviceNotFoundError, run_adb
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
        DeviceNotFoundError: If identifier cannot be resolved. A RuntimeError,
            so callers that catch that keep working; typed so that "the serial
            you named is not attached" exits 3 at the CLI boundary whether this
            notices it or adb does one call later.

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
        # The message is the one this has always raised; only the type and the
        # one-line remedy are new.
        raise DeviceNotFoundError(
            f"Device '{identifier}' was requested, but no devices are connected. "
            f"Start an emulator or connect a device:\n"
            f"  emulator -avd <device-name>\n"
            f"  adb devices",
            remedy=(
                "Start an emulator (`emulator -avd <device-name>`) or connect a device; "
                "`adb devices` lists what is attached."
            ),
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
    remedy = "Pass --serial with one of the available devices listed."
    raise DeviceNotFoundError(
        f"Device '{identifier}' not found. Available devices:\n"
        + "\n".join(f"  - {d['serial']} ({d['type']})" for d in available),
        remedy=remedy,
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

# The top-level blocks whose "User N:" sections are per-user package state.
# `Queries:` prints "    User 0:" at the same indent and means something else
# (who may see the package), so a user section is only one under these.
_USER_STATE_BLOCKS = frozenset({_PACKAGES_HEADER, "Hidden system packages:", "Shared users:"})

# "    User 11: ceDataInode=0 installed=true ..." under a package, or
# "    User 11: " (nothing after it) under a SharedUser.
_USER_SECTION_RE = re.compile(r"^(\s+)User (\d+):(?:\s(.*))?$")
_INSTALLED_RE = re.compile(r"\binstalled=(true|false)\b")

# The package's shared uid, "    sharedUser=SharedUserSetting{ae1ae0a android.uid.shared/10074}",
# and a block header under "Shared users:", "  SharedUser [android.uid.shared] (ae1ae0a):".
# A SharedUser block is read only when its name is the package's own.
_SHARED_USER_FIELD_RE = re.compile(r"^\s+sharedUser=SharedUserSetting\{\S+ ([^/}\s]+)/")
_SHARED_USER_BLOCK_RE = re.compile(r"^\s+SharedUser \[([^\]]+)\]")


def parse_package_permissions(dump: str, user: int = 0) -> dict:
    """Parse ``adb shell dumpsys package <pkg>`` into permission state.

    Runtime permissions are per user: each sits in a ``User N:`` section,
    under the package in ``Packages:`` or, for a package in a shared uid, under
    its ``SharedUser [...]`` in ``Shared users:`` (both layouts are the same on
    API 33 and API 35). Only ``user``'s section is read, found by its label,
    never by position -- and when it is absent nothing is read in its place.

    Args:
        dump: Verbatim stdout of ``dumpsys package <pkg>``.
        user: The user whose runtime state to read. 0, the system user, is
            what ``pm grant`` targets without ``--user``.

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
          permissions, for ``user`` only. A requested permission another
          user's section lists as runtime, but ``user``'s section does not
          list at all, is added as ``{"granted": False, "flags": [],
          "unlisted": True}``: a revoked permission left with no flags is not
          printed (recorded: ``dumpsys_package_user_revoked``). A requested
          permission no section lists stays out -- nothing says it is a
          runtime one (deskclock requests eight such).
        * ``granted`` -- names actually held, install and runtime together.
        * ``denied`` -- names the dump reports with ``granted=false``.
        * ``user`` -- the user asked about.
        * ``user_found`` -- whether the dump has a ``User <user>:`` section
          for the package at all. False means ``runtime`` is empty because
          nothing was read, not because the app requests nothing: a caller
          verifying a grant must refuse rather than read on.
        * ``user_installed`` -- the ``installed=`` flag of the package's
          ``User <user>:`` line, or None when there is none. The dump prints
          a section for every user on the device, installed or not, and a
          ``pm grant --user`` for a user without the app exits 0 and then
          reads granted=true there (recorded:
          ``dumpsys_package_user_not_installed``).

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
    block = ""
    # The "User N:" section the current line is inside, if any.
    in_user: int | None = None
    user_indent = 0
    user_found = False
    user_installed: bool | None = None
    # Runtime permission names seen under any OTHER user's section.
    elsewhere_runtime: set[str] = set()
    shared_user: str | None = None
    # Lines more indented than this belong to a SharedUser block that is not
    # the package's, and are skipped whole.
    skip_indent: int | None = None

    for line in dump.splitlines():
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip())
        if indent == 0:
            block = line.strip()
            found = found or block == _PACKAGES_HEADER
            section = None
            in_user = None
            skip_indent = None
            continue

        if skip_indent is not None:
            if indent > skip_indent:
                continue
            skip_indent = None
        if section is not None and indent <= section_indent:
            section = None
        if in_user is not None and indent <= user_indent:
            in_user = None

        if block == _PACKAGES_HEADER and shared_user is None:
            field = _SHARED_USER_FIELD_RE.match(line)
            if field is not None:
                shared_user = field.group(1)
                continue
        if block == "Shared users:":
            owner = _SHARED_USER_BLOCK_RE.match(line)
            if owner is not None:
                section = None
                in_user = None
                if owner.group(1) != shared_user:
                    skip_indent = indent
                continue

        user_line = _USER_SECTION_RE.match(line) if block in _USER_STATE_BLOCKS else None
        if user_line is not None:
            in_user = int(user_line.group(2))
            user_indent = len(user_line.group(1))
            section = None
            if in_user == user:
                user_found = True
                installed = _INSTALLED_RE.search(user_line.group(3) or "")
                if block == _PACKAGES_HEADER and installed and user_installed is None:
                    user_installed = installed.group(1) == "true"
            continue

        header = _SECTION_RE.match(line)
        if header is not None:
            name = header.group(2)
            if name == "runtime" and in_user != user:
                # Another user's section, or one outside any user: never read
                # in place of the one asked for. Another user's names are kept
                # only as evidence of which permissions are runtime ones.
                section = "elsewhere" if in_user is not None else None
                section_indent = len(header.group(1))
                continue
            # First occurrence wins, and it is what keeps the repeats out: an
            # updated system app repeats every section under "Hidden system
            # packages:" AFTER the live copy. Runtime sections are chosen by
            # their "User N:" label above, so position no longer picks a user.
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
        if section == "elsewhere":
            if state is not None:
                elsewhere_runtime.add(state.group("name"))
            continue
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

    if user_found:
        listed = {e["permission"] for e in install + runtime}
        for name in requested:
            if name in elsewhere_runtime and name not in listed:
                runtime.append(
                    {"permission": name, "granted": False, "flags": [], "unlisted": True}
                )

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
        "user": user,
        "user_found": user_found,
        "user_installed": user_installed,
    }


class UserStateUnreadableError(LookupError):
    """The dump describes the package but cannot answer for ``user``.

    Raised by :func:`permission_state` and returned by :func:`user_state_error`,
    which is the one rule every read-back goes through. Never answered from
    another user's section instead.
    """

    def __init__(self, user: int, message: str) -> None:
        super().__init__(message)
        self.user = user

    def describe(self, package: str) -> str:
        """Why the dump cannot answer for :attr:`user`, naming ``package``."""
        raise NotImplementedError

    def install_command(self, package: str) -> str:
        """The command that makes ``package`` exist for :attr:`user`."""
        return f"adb shell pm install-existing --user {self.user} {package}"

    def remedy(self, package: str) -> str:
        """What to run before retrying."""
        raise NotImplementedError


class UserSectionMissingError(UserStateUnreadableError):
    """The dump describes the package but has no ``User N:`` section for the user."""

    def __init__(self, user: int) -> None:
        super().__init__(user, f"the dump has no `User {user}:` section")

    def describe(self, package: str) -> str:
        return (
            f"the dump of {package} has no `User {self.user}:` section, so its state for "
            f"user {self.user} could not be read"
        )

    def remedy(self, package: str) -> str:
        return (
            f"Check `adb shell pm list users` and `adb shell dumpsys package {package}`; "
            f"install the app for user {self.user} with `{self.install_command(package)}`, "
            f"then retry."
        )


class UserNotInstalledError(UserStateUnreadableError):
    """The user's ``User N:`` section reads ``installed=false``.

    Its permission lines are still there and can read ``granted=true``: ``pm grant
    --user N`` for a user the app is not installed for exits 0 and records the
    grant (recorded as ``dumpsys_package_user_not_installed``). No app exists for
    that user to hold it, so that state is never reported as held.
    """

    def __init__(self, user: int) -> None:
        super().__init__(user, f"`User {user}:` reads installed=false")

    def describe(self, package: str) -> str:
        return (
            f"{package} is not installed for user {self.user} (`User {self.user}:` reads "
            f"installed=false)"
        )

    def remedy(self, package: str) -> str:
        return (
            f"Install the app for user {self.user} with `{self.install_command(package)}`, "
            f"then retry."
        )


def user_state_error(parsed: dict) -> UserStateUnreadableError | None:
    """Why a :func:`parse_package_permissions` result cannot answer for its user.

    The single rule behind every permission read-back: a package with no
    ``User N:`` section for the user, or one whose section reads
    ``installed=false``, has no state for that user worth reporting. ``None``
    when the dump can answer, including when it does not describe the package
    at all (``found`` is False), which each caller words for itself.
    """
    if not parsed["found"]:
        return None
    if not parsed["user_found"]:
        return UserSectionMissingError(parsed["user"])
    if parsed["user_installed"] is False:
        return UserNotInstalledError(parsed["user"])
    return None


def permission_state(dump: str, permission: str, user: int = 0) -> bool | None:
    """Whether ``permission`` is held for ``user``, according to the dump itself.

    Args:
        dump: Verbatim stdout of ``dumpsys package <pkg>``.
        permission: Full permission name, e.g. ``android.permission.CAMERA``.
        user: Whose runtime state to read; 0 by default, as ``pm grant``.

    Returns:
        ``True`` or ``False`` as the dump reports it, or ``None`` when the
        package lists no state for it at all -- which is what a ``pm grant``
        of an unrequested permission leaves behind. ``None`` is deliberately
        not ``False``: "the app does not ask for this" and "the user said no"
        are different answers, and only the first means the caller's command
        could never have worked.

    Raises:
        UserSectionMissingError: the package is in the dump but ``user`` has
            no section in it. Answering ``None`` there would read as "not
            requested", which a revoke counts as done.
        UserNotInstalledError: ``user``'s section reads ``installed=false``.
            Its ``granted=true`` is a grant no installed app holds.
    """
    parsed = parse_package_permissions(dump, user=user)
    error = user_state_error(parsed)
    if error is not None:
        raise error
    for entry in parsed["install"] + parsed["runtime"]:
        if entry["permission"] == permission:
            return bool(entry["granted"])
    return None


_USER_INFO_RE = re.compile(r"^\s*UserInfo\{(\d+):", re.MULTILINE)


def parse_user_ids(output: str) -> frozenset[int]:
    """The user ids in ``adb shell pm list users``.

    Recorded (``pm_list_users``, ``pm_list_users_two_users``): a ``Users:``
    line, then one ``UserInfo{<id>:<name>:<flags>}`` row per user, ``running``
    after a started one. Empty when no row parses, which a caller must treat
    as "could not tell", not as "no such user".
    """
    return frozenset(int(match) for match in _USER_INFO_RE.findall(output))


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


def get_current_activity(serial: str | None = None, strict: bool = False) -> str | None:
    """
    Get currently focused activity.

    Two failures look identical in the return value and are not the same
    thing: "nothing is focused" and "the device could not be asked". By
    default this collapses both to None, which is what a best-effort caller
    labelling a recording wants. A caller whose whole answer depends on the
    lookup -- ``app_launcher.get_state`` reports ``foreground`` -- passes
    ``strict=True`` and gets the adb failure, with its remedy, instead of a
    plausible-looking None (C6).

    A dump that simply names no focused window still returns None under
    ``strict``: that is an answer, not a failure.

    Args:
        serial: Device serial (uses default if None)
        strict: Propagate an adb failure instead of returning None.

    Returns:
        Activity name (e.g., "com.example.app/.MainActivity"), or None if not found

    Raises:
        AdbError: only when ``strict`` is True and adb failed.
        OSError: only when ``strict`` is True and the binary could not be run.

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
        if strict:
            raise
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


class UnknownSdkLevelError(RuntimeError):
    """The device did not say its API level: getprop failed, or answered no number."""

    remedy = (
        "Check the device has finished booting and answers `adb shell getprop "
        "ro.build.version.sdk` with a number, then retry."
    )


def get_sdk_level(serial: str | None = None, *, timeout: int | None = None) -> int:
    """The device's API level (``getprop ro.build.version.sdk``).

    Recorded: getprop_sdk (``33`` on the API 33 emulator, ``35`` on API 35).
    A property a device does not have answers an empty line at exit 0
    (getprop_unset), so "no number" is the failure, whatever the status.

    Args:
        serial: Device serial.
        timeout: Seconds to allow the call; adb_exec's default when None.

    Returns:
        The level.

    Raises:
        UnknownSdkLevelError: getprop exited non-zero or answered anything but
            digits; the message quotes its answer.
        adb_exec.DeviceError: The device could not be reached.
        adb_exec.AdbTimeoutError: The call took longer than ``timeout``.
    """
    result = run_adb("shell", serial, "getprop", "ro.build.version.sdk", timeout=timeout)
    value = result.stdout.strip()
    if result.ok and value.isdigit():
        return int(value)
    said = " ".join(result.stderr.split())[:200]
    raise UnknownSdkLevelError(
        f"`adb shell getprop ro.build.version.sdk` answered {value[:80]!r} at exit "
        f"{result.returncode}" + (f" ({said})" if said else "") + ", which is no API level."
    )


def get_system_server_pid(
    serial: str | None = None, *, not_one_of: Iterable[str] = (), timeout: int | None = None
) -> str | None:
    """system_server's pid, or None when the device does not name exactly one.

    The process every ``ActivityManager`` sentence about an app's processes must
    come from (``common.logcat.parse_package_pids``): any app may log under that
    tag, but only system_server writes it with this pid. Measured on the API 35
    emulator: ``pidof system_server`` answered 598, the PID column of every
    ActivityManager line in the same window.

    Args:
        serial: Device serial.
        not_one_of: Pids already known to be other processes (the app's own,
            from ``pidof <package>``). A pid is one process, so an answer naming
            one of these is not system_server's, and None is returned.
        timeout: Seconds to allow the call; adb_exec's default when None. The
            ANR session worker asks with a short bound, because --stop waits
            for a call under way.

    Returns:
        The pid, or None when ``pidof`` fails or names none, several, or one of
        ``not_one_of``.

    Raises:
        adb_exec.DeviceError: The device could not be reached.
        adb_exec.AdbTimeoutError: The call took longer than ``timeout``.
    """
    result = run_adb("shell", serial, "pidof", "system_server", timeout=timeout)
    pids = result.stdout.split() if result.ok else []
    if len(pids) != 1 or not pids[0].isdigit() or pids[0] in set(not_one_of):
        return None
    return pids[0]


def parse_package_uids(output: str, package: str) -> frozenset[int] | None:
    """Every uid ``cmd package list packages -U <package>`` gives for exactly ``package``.

    Recorded: ``package:<package> uid:10205`` with one user (package_list_uid),
    and ONE row with the uids comma-separated, ``uid:10205,1110205``, with the
    package installed for two (package_list_uid_two_users) -- not a row per
    user. Each is the package's uid in one user, so all of them count. The
    command filters by SUBSTRING, so other packages whose names contain
    ``package`` are listed too; the row whose name is exactly ``package`` is
    the one read.

    Returns:
        The uids, or None when no row names exactly ``package``.
    """
    for line in output.splitlines():
        match = re.match(r"package:(\S+) uid:(\d+(?:,\d+)*)", line.strip())
        if match and match.group(1) == package:
            return frozenset(int(uid) for uid in match.group(2).split(","))
    return None


def parse_uid_table(output: str) -> dict[str, frozenset[int]]:
    """``{package: its uids}`` for every row of ``cmd package list packages -U``.

    The whole table, unfiltered (package_list_uid_all, recorded on API 33 and
    35): one ``package:<name> uid:<uids>`` row per package, the uids
    comma-separated when it is installed for several users
    (package_list_uid_two_users). Several packages may share a uid
    (sharedUserId): the recorded tables put ``android`` and ten others on
    1000, and a handful of providers on one app uid.
    """
    table: dict[str, frozenset[int]] = {}
    for line in output.splitlines():
        match = re.match(r"package:(\S+) uid:(\d+(?:,\d+)*)", line.strip())
        if match:
            table[match.group(1)] = frozenset(int(uid) for uid in match.group(2).split(","))
    return table


def get_package_uids(
    serial: str | None, package: str, *, timeout: int | None = None
) -> frozenset[int] | None:
    """The package's uids (one per user it is installed for), asked of the device.

    One bounded adb call -- ``timeout`` seconds, adb_exec's default when None.
    Returns None when the device does not name the package (not installed, or
    the command failed).

    Raises:
        adb_exec.DeviceError: The device could not be reached.
        adb_exec.AdbTimeoutError: The call took longer than ``timeout``.
    """
    result = run_adb(
        "shell",
        serial,
        "cmd",
        "package",
        "list",
        "packages",
        "-U",
        quote_for_device_shell(package),
        timeout=timeout,
    )
    return parse_package_uids(result.stdout, package) if result.ok else None


def parse_ps_uids(output: str) -> dict[str, int]:
    """``{pid: uid}`` from ``ps -o PID,UID``.

    Recorded (ps_pid_uid): a ``  PID   UID`` header, then one row per pid that
    exists, with the numeric uid. A pid that does not exist has no row.
    """
    uids: dict[str, int] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            uids[fields[0]] = int(fields[1])
    return uids


def get_pid_uids(serial: str | None, pids: Iterable[str]) -> dict[str, int]:
    """``{pid: uid}`` for ``pids`` that exist, from one bounded ``ps`` call.

    Measured: with none of the pids existing ``ps`` prints only its header and
    exits 1, which is an answer (no rows), not a failure.

    Raises:
        adb_exec.DeviceError: The device could not be reached.
    """
    wanted = sorted({pid for pid in pids if pid.isdigit()}, key=int)
    if not wanted:
        return {}
    result = run_adb(
        "shell", serial, "ps", "-o", "PID,UID", "-p", quote_for_device_shell(",".join(wanted))
    )
    return parse_ps_uids(result.stdout)
