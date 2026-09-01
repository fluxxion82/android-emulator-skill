#!/usr/bin/env python3
"""
Android Target Listing with Progressive Disclosure

Lists Android targets (connected devices + defined AVDs) with token-efficient
summaries. Full detail is available on demand via --get-details / --json.

This is the Android counterpart of the iOS ``sim_list.py``:
- Connected devices come from ``adb devices -l`` (serial, model, state).
- Defined AVDs come from ``emulator -list-avds`` (names) enriched, when
  available, with ``avdmanager list avd`` metadata (target/API, device, ABI).

Usage Examples:
    # Concise summary (default)
    python scripts/device_list.py

    # Full list of every target
    python scripts/device_list.py --get-details

    # Machine-readable
    python scripts/device_list.py --json

    # Filter by name/type substring (matches serial, model, or AVD name)
    python scripts/device_list.py --device-type Pixel
    python scripts/device_list.py --name emulator --get-details

Output (default):
    Android Targets
    ├─ Total: 5
    ├─ Online: 1
    ├─ Offline: 1
    └─ AVDs: 3

    ✓ emulator-5554 (sdk_gphone64_x86_64) [online]

    Use --get-details for the full list

Technical Details:
- Connected devices: ``adb devices -l``
- Defined AVDs: ``emulator -list-avds`` (+ ``avdmanager list avd`` metadata)
- Default summary is a handful of lines; --get-details / --json expand it.
"""

import argparse
import json
import subprocess
import sys

from common.env_config import env_int
from common.sdk_tools import get_emulator_path

# Tunable defaults (override via the ANDROID_EMU_ prefix).
# How many online devices to echo inline under the concise summary.
SUMMARY_PREVIEW_COUNT = env_int("ANDROID_EMU_LIST_PREVIEW_COUNT", 3, min_value=0)
# Per-command timeout (seconds) for the discovery subprocess calls.
LIST_COMMAND_TIMEOUT = env_int("ANDROID_EMU_LIST_TIMEOUT", 15, min_value=1)


def parse_adb_devices(output: str) -> list[dict]:
    """
    Parse ``adb devices -l`` output into structured device records.

    Pure logic: takes the raw stdout string and returns a list of dicts. No
    subprocess or device access, so it is trivially unit-testable.

    Args:
        output: Raw stdout from ``adb devices -l``.

    Returns:
        List of dicts with keys:
        - "kind": always "device"
        - "serial": device serial (e.g. "emulator-5554", "ABC123")
        - "state": adb state ("device", "offline", "unauthorized", ...)
        - "online": bool, True only when state == "device"
        - "type": "emulator" or "device"
        - "model": model from the ``model:`` tag, or "" if absent

    Example input lines::

        List of devices attached
        emulator-5554   device product:sdk_gphone64 model:sdk_gphone64 device:emu64x
        ABC123DEF456    offline
    """
    devices: list[dict] = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Skip the header and any informational daemon lines.
        if line.startswith("List of devices"):
            continue
        if line.startswith("*"):  # e.g. "* daemon started successfully *"
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        serial = parts[0]
        state = parts[1]

        # Extract key:value tags (model:, product:, device:, transport_id:, ...).
        model = ""
        for token in parts[2:]:
            if token.startswith("model:"):
                model = token.split(":", 1)[1]
                break

        device_type = "emulator" if serial.startswith("emulator-") else "device"

        devices.append(
            {
                "kind": "device",
                "serial": serial,
                "state": state,
                "online": state == "device",
                "type": device_type,
                "model": model,
            }
        )

    return devices


def parse_emulator_avds(output: str) -> list[dict]:
    """
    Parse ``emulator -list-avds`` output into structured AVD records.

    Pure logic: each non-empty line is an AVD name. Some emulator builds emit a
    leading "INFO" banner line on stderr; defensively skip lines that contain
    whitespace (real AVD names never do) so a stray banner cannot pollute output.

    Args:
        output: Raw stdout from ``emulator -list-avds``.

    Returns:
        List of dicts with keys:
        - "kind": always "avd"
        - "name": AVD name
        - "online": always False (a defined AVD is not a running device)
    """
    avds: list[dict] = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Real AVD names are single tokens; banner/info lines contain spaces.
        if " " in line:
            continue
        avds.append({"kind": "avd", "name": line, "online": False})

    return avds


def parse_avdmanager_avds(output: str) -> dict[str, dict]:
    """
    Parse ``avdmanager list avd`` output into a name -> metadata mapping.

    Pure logic. The block format looks like::

        Available Android Virtual Devices:
            Name: Pixel_5_API_33
          Device: pixel_5 (Google)
            Path: /Users/me/.android/avd/Pixel_5_API_33.avd
          Target: Google APIs (Google Inc.)
                  Based on: Android 13 (Tiramisu) Tag/ABI: google_apis/x86_64

    Args:
        output: Raw stdout from ``avdmanager list avd``.

    Returns:
        Dict keyed by AVD name. Each value carries any of:
        - "device": device profile (e.g. "pixel_5 (Google)")
        - "target": target/API string (e.g. "Google APIs (Google Inc.)")
        - "based_on": "Based on" line value (e.g. "Android 13 (Tiramisu) ...")
        - "abi": Tag/ABI value (e.g. "google_apis/x86_64")
    """
    metadata: dict[str, dict] = {}
    current: dict | None = None

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # A new device block always starts with a "Name:" line.
        if line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
            current = {}
            metadata[name] = current
            continue
        if current is None:
            continue

        if line.startswith("Device:"):
            current["device"] = line.split(":", 1)[1].strip()
        elif line.startswith("Target:"):
            current["target"] = line.split(":", 1)[1].strip()
        elif "Tag/ABI:" in line:
            # This line may also carry "Based on:" before "Tag/ABI:".
            if "Based on:" in line:
                based = line.split("Based on:", 1)[1]
                based = based.split("Tag/ABI:", 1)[0].strip()
                if based:
                    current["based_on"] = based
            current["abi"] = line.split("Tag/ABI:", 1)[1].strip()
        elif line.startswith("Based on:"):
            current["based_on"] = line.split(":", 1)[1].strip()

    return metadata


def merge_avds(names: list[dict], metadata: dict[str, dict]) -> list[dict]:
    """
    Enrich emulator AVD records with avdmanager metadata where names match.

    Pure logic: ``emulator -list-avds`` is the source of truth for which AVDs
    exist; avdmanager only adds optional detail. AVDs without metadata are kept
    as-is so the listing never silently drops a defined AVD.

    Args:
        names: AVD records from :func:`parse_emulator_avds`.
        metadata: Name -> metadata map from :func:`parse_avdmanager_avds`.

    Returns:
        New list of AVD dicts, each merged with its metadata (if any).
    """
    merged: list[dict] = []
    for avd in names:
        record = dict(avd)
        extra = metadata.get(avd["name"])
        if extra:
            record.update(extra)
        merged.append(record)
    return merged


def matches_filter(record: dict, needle: str) -> bool:
    """
    Case-insensitive substring match across a target's identifying fields.

    Pure logic. A device matches on serial, model, or type; an AVD matches on
    name, device, target, or abi. Used by both the device and AVD filters.

    Args:
        record: A device or AVD record.
        needle: Substring to look for.

    Returns:
        True if ``needle`` (case-insensitive) appears in any searchable field.
    """
    if not needle:
        return True

    needle_lc = needle.lower()
    fields = [
        record.get("serial", ""),
        record.get("model", ""),
        record.get("type", ""),
        record.get("name", ""),
        record.get("device", ""),
        record.get("target", ""),
        record.get("abi", ""),
    ]
    return any(needle_lc in str(field).lower() for field in fields)


class DeviceLister:
    """Lists Android targets (devices + AVDs) with progressive disclosure."""

    def __init__(self, name_filter: str | None = None):
        """
        Initialize lister.

        Args:
            name_filter: Optional case-insensitive substring filter applied to
                both connected devices and defined AVDs.
        """
        self.name_filter = name_filter

    def _run(self, cmd: list[str]) -> str | None:
        """
        Run a discovery command, returning stdout or None if unavailable.

        Unusable tools and nonzero exits are treated as "nothing to report"
        rather than hard failures, so the listing degrades gracefully (e.g. AVDs
        still show if adb is absent). The catch is ``OSError``, not just
        ``FileNotFoundError``: when PATH holds the Android SDK root, a bare tool
        name can resolve to a *directory* and execve raises ``PermissionError``.
        """
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=LIST_COMMAND_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def get_devices(self) -> list[dict]:
        """Get connected devices via ``adb devices -l`` (filtered)."""
        output = self._run(["adb", "devices", "-l"])
        devices = parse_adb_devices(output) if output else []
        if self.name_filter:
            devices = [d for d in devices if matches_filter(d, self.name_filter)]
        return devices

    def get_avds(self) -> list[dict]:
        """
        Get defined AVDs via ``emulator -list-avds`` + avdmanager (filtered).

        The emulator binary is resolved explicitly rather than exec'd by bare
        name, which is unsafe when PATH holds the SDK root (see
        :mod:`common.sdk_tools`). An unresolvable emulator yields no names.
        """
        emulator = get_emulator_path()
        names_output = self._run([emulator, "-list-avds"]) if emulator else None
        names = parse_emulator_avds(names_output) if names_output else []

        meta_output = self._run(["avdmanager", "list", "avd"])
        metadata = parse_avdmanager_avds(meta_output) if meta_output else {}

        avds = merge_avds(names, metadata)
        if self.name_filter:
            avds = [a for a in avds if matches_filter(a, self.name_filter)]
        return avds

    def collect(self) -> dict:
        """
        Collect devices + AVDs and compute the summary counts.

        Returns:
            Dict with "devices", "avds", and a "summary" of counts
            (total / online / offline / avds).
        """
        devices = self.get_devices()
        avds = self.get_avds()

        online = [d for d in devices if d["online"]]
        offline = [d for d in devices if not d["online"]]

        return {
            "devices": devices,
            "avds": avds,
            "summary": {
                "total": len(devices) + len(avds),
                "online": len(online),
                "offline": len(offline),
                "avds": len(avds),
            },
        }


def format_device(device: dict) -> str:
    """Format a single connected-device record for human output."""
    icon = "✓" if device["online"] else " "
    model = f" ({device['model']})" if device.get("model") else ""
    return f"{icon} {device['serial']}{model} [{device['state']}]"


def format_avd(avd: dict) -> str:
    """Format a single AVD record for human output."""
    detail_parts = []
    if avd.get("based_on"):
        detail_parts.append(avd["based_on"])
    elif avd.get("target"):
        detail_parts.append(avd["target"])
    if avd.get("abi"):
        detail_parts.append(avd["abi"])
    detail = f" ({', '.join(detail_parts)})" if detail_parts else ""
    return f"  {avd['name']}{detail}"


def _print_details(data: dict) -> None:
    """Print the full device + AVD list (human form)."""
    devices = data["devices"]
    avds = data["avds"]

    print(f"Connected devices ({len(devices)}):")
    if devices:
        for device in devices:
            print(f"  {format_device(device)}")
    else:
        print("  (none)")

    print()
    print(f"Defined AVDs ({len(avds)}):")
    if avds:
        for avd in avds:
            print(format_avd(avd))
    else:
        print("  (none)")


def _print_summary(data: dict) -> None:
    """Print the concise, token-efficient summary (human form)."""
    s = data["summary"]
    print("Android Targets")
    print(f"├─ Total: {s['total']}")
    print(f"├─ Online: {s['online']}")
    print(f"├─ Offline: {s['offline']}")
    print(f"└─ AVDs: {s['avds']}")

    online_devices = [d for d in data["devices"] if d["online"]]
    preview = online_devices[:SUMMARY_PREVIEW_COUNT]
    if preview:
        print()
        for device in preview:
            print(f"  {format_device(device)}")

    print()
    print("Use --get-details for the full list")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="List Android targets (connected devices + defined AVDs)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Concise summary (default)
  python scripts/device_list.py

  # Full list of every target
  python scripts/device_list.py --get-details

  # Machine-readable output
  python scripts/device_list.py --json

  # Filter by name/type substring (serial, model, or AVD name)
  python scripts/device_list.py --device-type Pixel
  python scripts/device_list.py --name emulator --get-details
        """,
    )
    parser.add_argument(
        "--get-details",
        action="store_true",
        help="Show the full list of devices and AVDs (default is a summary)",
    )
    parser.add_argument(
        "--device-type",
        dest="name_filter",
        help="Filter targets by case-insensitive substring (serial/model/name)",
    )
    parser.add_argument(
        "--name",
        dest="name_filter",
        help="Alias for --device-type (substring filter)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    lister = DeviceLister(name_filter=args.name_filter)
    data = lister.collect()

    if args.json:
        print(json.dumps(data, indent=2))
    elif args.get_details:
        _print_details(data)
    else:
        _print_summary(data)

    sys.exit(0)


if __name__ == "__main__":
    main()
