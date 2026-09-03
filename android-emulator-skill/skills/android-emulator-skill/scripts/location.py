#!/usr/bin/env python3
"""
Simulate GPS location on an Android emulator.

Set a fixed coordinate, use a named city preset, or replay a GPX route through
the emulator console. This is the Android-native counterpart of the iOS
``location.py`` (which wraps ``simctl location``); here we drive the emulator's
built-in console command:

    adb -s <serial> emu geo fix <LON> <LAT>

IMPORTANT: longitude comes FIRST in ``emu geo fix`` (lon, lat), which is the
reverse of the more common "lat, lng" ordering. This script always accepts
human-friendly ``--lat`` / ``--lng`` and handles the reordering internally.

EMULATOR-ONLY
-------------
``emu geo fix`` only works on Android *emulators* (AVDs), because it talks to
the emulator console, not to the device. Physical devices have no such console:
to mock GPS on real hardware you must install a *mock-location provider* app and
enable it under Settings -> Developer options -> "Select mock location app".
This script does NOT silently no-op on a physical device; it refuses up front
with a clear message so you are never misled into thinking GPS was mocked.

Key features:
- Fixed coordinate via --lat / --lng
- Built-in city presets (--city london|tokyo|sf|nyc|...)
- GPX route replay (--gpx FILE): sequential "emu geo fix" calls at a
  configurable interval (--interval / ANDROID_EMU_GEO_INTERVAL)
- Best-effort --clear, --list-cities
- Standard --serial, --json, and --verbose flags

Usage examples:
    python scripts/location.py --lat 51.5074 --lng -0.1278
    python scripts/location.py --city london
    python scripts/location.py --gpx route.gpx --interval 0.5
    python scripts/location.py --list-cities
    python scripts/location.py --clear
"""

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET

from common.adb_exec import AdbError
from common.device_utils import get_connected_devices, resolve_device_identifier
from common.emu_console import run_emu
from common.env_config import env_float

# Tunable defaults (override via the ANDROID_EMU_ prefix).
# Seconds to wait between successive "emu geo fix" calls during GPX replay.
DEFAULT_GEO_INTERVAL = env_float("ANDROID_EMU_GEO_INTERVAL", 1.0, min_value=0.0)

# Per-call timeout for an individual emulator-console invocation.
DEFAULT_GEO_TIMEOUT = env_float("ANDROID_EMU_GEO_TIMEOUT", 15.0, min_value=1.0)

# === PRESETS ===

# Coordinates are stored human-friendly as (lat, lng); command construction
# reorders them to the emulator's (lon, lat) convention. Aliases share values
# with their canonical city so --list-cities can suppress duplicates.
_CITY_ALIASES: set[str] = {"nyc", "sf", "la"}

CITY_PRESETS: dict[str, tuple[float, float]] = {
    "london": (51.5074, -0.1278),
    "tokyo": (35.6762, 139.6503),
    "newyork": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060),
    "sanfrancisco": (37.7749, -122.4194),
    "sf": (37.7749, -122.4194),
    "losangeles": (34.0522, -118.2437),
    "la": (34.0522, -118.2437),
    "paris": (48.8566, 2.3522),
    "berlin": (52.5200, 13.4050),
    "dublin": (53.3498, -6.2603),
    "sydney": (-33.8688, 151.2093),
    "beijing": (39.9042, 116.4074),
    "mumbai": (19.0760, 72.8777),
    "cairo": (30.0444, 31.2357),
    "saopaulo": (-23.5505, -46.6333),
}


# === PURE LOGIC (device-free; unit-tested) ===


def normalize_city_key(name: str) -> str:
    """Normalize a user-supplied city name to a preset lookup key."""
    return name.lower().replace(" ", "").replace("_", "").replace("-", "")


def resolve_city(name: str) -> tuple[float, float] | None:
    """
    Look up a city preset by (case/space-insensitive) name.

    Args:
        name: City name or alias (e.g. "London", "new york", "SF").

    Returns:
        (lat, lng) tuple, or None if the city is unknown.
    """
    return CITY_PRESETS.get(normalize_city_key(name))


def list_cities() -> list[str]:
    """Return canonical city names (aliases suppressed), sorted."""
    return sorted(k for k in CITY_PRESETS if k not in _CITY_ALIASES)


def validate_coordinate(lat: float, lng: float) -> str | None:
    """
    Validate a lat/lng pair.

    Returns:
        None if valid, otherwise an error message describing the problem.
    """
    if not (-90 <= lat <= 90):
        return f"Invalid latitude {lat}: must be between -90 and 90"
    if not (-180 <= lng <= 180):
        return f"Invalid longitude {lng}: must be between -180 and 180"
    return None


def build_geo_fix_args(lat: float, lng: float) -> list[str]:
    """
    Build the ``geo fix`` console command for a coordinate.

    The emulator console expects LONGITUDE FIRST, then latitude:
        adb [-s <serial>] emu geo fix <LON> <LAT>

    We accept human-friendly (lat, lng) and emit them in the console's
    (lon, lat) order here so callers never have to remember the reversal.

    Only the console-side arguments are built here. The ``adb ... emu`` prefix
    belongs to :func:`common.emu_console.run_emu`, which is the one place in
    this skill that speaks the console protocol -- it strips the ``OK`` framing
    and raises on a ``KO``, which this module used to check for by hand and
    only in one of its two failure branches.

    Args:
        lat: Latitude in decimal degrees.
        lng: Longitude in decimal degrees.

    Returns:
        Console command arguments, ready for ``run_emu(*args)``.
    """
    # NOTE: longitude precedes latitude — this ordering is load-bearing.
    return ["geo", "fix", repr(lng), repr(lat)]


def parse_gpx(xml_text: str) -> list[tuple[float, float]]:
    """
    Parse GPX track points into an ordered list of (lat, lng) waypoints.

    Reads ``<trkpt lat=".." lon="..">`` elements (the standard GPX track-point
    element). Namespaces are tolerated: GPX files commonly declare a default
    namespace, so we match on the local tag name rather than a fixed prefix.
    Route points (``<rtept>``) and standalone waypoints (``<wpt>``) are also
    accepted as a convenience, scanned in document order.

    Args:
        xml_text: Raw GPX XML content.

    Returns:
        Ordered list of (lat, lng) float tuples.

    Raises:
        ValueError: If the XML is malformed or a point has bad coordinates.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"Malformed GPX XML: {e}") from e

    waypoints: list[tuple[float, float]] = []
    point_tags = {"trkpt", "rtept", "wpt"}

    for element in root.iter():
        # Strip any "{namespace}" prefix to compare the local tag name.
        local = element.tag.rsplit("}", 1)[-1]
        if local not in point_tags:
            continue

        lat_raw = element.get("lat")
        lon_raw = element.get("lon")
        if lat_raw is None or lon_raw is None:
            raise ValueError(f"GPX <{local}> missing lat/lon attribute")

        try:
            lat = float(lat_raw)
            lng = float(lon_raw)
        except ValueError:
            raise ValueError(
                f"GPX <{local}> has non-numeric coordinate: lat={lat_raw!r} lon={lon_raw!r}"
            ) from None

        error = validate_coordinate(lat, lng)
        if error:
            raise ValueError(f"GPX <{local}>: {error}")

        waypoints.append((lat, lng))

    return waypoints


# === MAIN CLASS ===


class LocationManager:
    """Manage simulated GPS location on an Android emulator."""

    def __init__(self, serial: str | None = None):
        """
        Initialize the location manager.

        Args:
            serial: Target emulator serial (auto-detected by main() when None).
        """
        self.serial = serial

    def _ensure_emulator(self) -> str | None:
        """
        Confirm the target serial is an emulator (not a physical device).

        ``emu geo fix`` only works against the emulator console, so refusing a
        physical device here keeps us from silently doing nothing.

        Returns:
            None if the target is an emulator (or cannot be confirmed offline),
            otherwise an error message explaining the physical-device path.
        """
        if self.serial and self.serial.startswith("emulator-"):
            return None

        try:
            devices = get_connected_devices()
        except Exception:
            # If we cannot enumerate devices, let the actual adb call surface
            # the error rather than blocking on an unverifiable guess.
            return None

        # If no serial was supplied, main() resolved one; treat an explicit
        # serial that is not in the emulator list as a physical device.
        match = next((d for d in devices if d["serial"] == self.serial), None)
        if match is not None and match["type"] != "emulator":
            return (
                f"Target '{self.serial}' is a physical device, not an emulator.\n"
                "  'emu geo fix' only works on emulators. To mock GPS on a real device,\n"
                "  install a mock-location provider app and enable it under\n"
                "  Settings -> Developer options -> 'Select mock location app'."
            )
        return None

    def _run_geo_fix(self, lat: float, lng: float) -> tuple[bool, str]:
        """
        Issue a single ``emu geo fix`` call. Returns (success, error_message)."""
        args = build_geo_fix_args(lat, lng)
        try:
            run_emu(*args, serial=self.serial, timeout=int(DEFAULT_GEO_TIMEOUT))
        except AdbError as error:
            # Covers what the three hand-rolled branches here used to cover, and
            # one they did not: a timeout, adb missing, a non-zero adb exit, and
            # a `KO` reply -- which the console delivers at exit status 0, and
            # which EmuConsoleError carries with the rejection text in it.
            # Every one of these messages names its own remedy.
            return False, str(error)
        except Exception as e:  # top-level safety net
            return False, str(e)

        return True, ""

    def set_coordinate(self, lat: float, lng: float, verbose: bool = False) -> tuple[bool, str]:
        """
        Set emulator GPS to a fixed coordinate.

        Args:
            lat: Latitude in decimal degrees (-90 to 90).
            lng: Longitude in decimal degrees (-180 to 180).
            verbose: Include extra context in the returned message.

        Returns:
            (success, message) tuple.
        """
        error = validate_coordinate(lat, lng)
        if error:
            return False, error

        guard = self._ensure_emulator()
        if guard:
            return False, guard

        ok, err = self._run_geo_fix(lat, lng)
        if not ok:
            return False, f"emu geo fix failed: {err}"

        if verbose:
            return True, (
                f"Location set\n"
                f"  Latitude:  {lat}\n"
                f"  Longitude: {lng}\n"
                f"  Device:    {self.serial or '(default)'}"
            )
        return True, f"Location set: {lat}, {lng}"

    def set_city(self, city_name: str, verbose: bool = False) -> tuple[bool, str]:
        """
        Set location to a named city preset.

        Args:
            city_name: Case-insensitive city name or alias from CITY_PRESETS.
            verbose: Include coordinate detail in the returned message.

        Returns:
            (success, message) tuple.
        """
        coords = resolve_city(city_name)
        if coords is None:
            available = ", ".join(list_cities())
            return False, f"Unknown city '{city_name}'. Available: {available}"

        lat, lng = coords
        success, message = self.set_coordinate(lat, lng, verbose=verbose)
        if success and not verbose:
            return True, f"Location set: {city_name.title()} ({lat}, {lng})"
        return success, message

    def replay_gpx(
        self,
        waypoints: list[tuple[float, float]],
        interval_seconds: float = DEFAULT_GEO_INTERVAL,
        verbose: bool = False,
    ) -> tuple[bool, str]:
        """
        Replay a GPX route by issuing sequential ``emu geo fix`` calls.

        Each waypoint is pushed in order, sleeping ``interval_seconds`` between
        successive points (no sleep after the final point). The emulator does not
        interpolate, so the effective "speed" is determined by point spacing and
        the interval.

        Args:
            waypoints: Ordered (lat, lng) pairs (at least one).
            interval_seconds: Delay between points in seconds.
            verbose: Include per-point detail in the returned message.

        Returns:
            (success, message) tuple.
        """
        if not waypoints:
            return False, "GPX route contains no track points"

        guard = self._ensure_emulator()
        if guard:
            return False, guard

        total = len(waypoints)
        for index, (lat, lng) in enumerate(waypoints):
            ok, err = self._run_geo_fix(lat, lng)
            if not ok:
                return False, f"GPX replay failed at point {index + 1}/{total}: {err}"
            if index < total - 1 and interval_seconds > 0:
                time.sleep(interval_seconds)

        if verbose:
            first, last = waypoints[0], waypoints[-1]
            return True, (
                f"GPX route replayed\n"
                f"  Points:    {total}\n"
                f"  Interval:  {interval_seconds}s\n"
                f"  Start:     {first[0]}, {first[1]}\n"
                f"  End:       {last[0]}, {last[1]}\n"
                f"  Device:    {self.serial or '(default)'}"
            )
        return True, f"GPX route replayed: {total} points at {interval_seconds}s interval"

    def clear(self, verbose: bool = False) -> tuple[bool, str]:
        """
        Best-effort clear of any GPS override.

        The emulator console has no dedicated "clear location" command, so there
        is no true reset to real GPS (the emulator has none). As a pragmatic
        best effort we re-fix the coordinate to (0, 0) so a stale override does
        not linger; this is documented as approximate.

        Args:
            verbose: Include extra context in the returned message.

        Returns:
            (success, message) tuple.
        """
        guard = self._ensure_emulator()
        if guard:
            return False, guard

        ok, err = self._run_geo_fix(0.0, 0.0)
        if not ok:
            return False, f"Location clear failed: {err}"

        note = "(best-effort: reset to 0,0; emulator has no native clear)"
        if verbose:
            return True, f"Location cleared {note}\n  Device: {self.serial or '(default)'}"
        return True, f"Location cleared {note}"


# === CLI ===


def _read_gpx_file(path: str) -> list[tuple[float, float]]:
    """
    Read and parse a GPX file into waypoints.

    Args:
        path: Filesystem path to a .gpx file.

    Returns:
        Ordered list of (lat, lng) waypoints.

    Raises:
        ValueError: If the file cannot be read or parsed.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            xml_text = handle.read()
    except OSError as e:
        raise ValueError(f"Cannot read GPX file '{path}': {e}") from e
    return parse_gpx(xml_text)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Simulate GPS location on an Android emulator (emu geo fix)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/location.py --lat 51.5074 --lng -0.1278
  python scripts/location.py --city london
  python scripts/location.py --gpx route.gpx --interval 0.5
  python scripts/location.py --list-cities
  python scripts/location.py --clear

Notes:
  EMULATOR-ONLY. 'emu geo fix' drives the emulator console and does not work on
  physical devices. To mock GPS on real hardware, install a mock-location
  provider app and select it under Developer options.
""",
    )

    # Location actions (mutually exclusive)
    action_group = parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument("--lat", type=float, help="Latitude (requires --lng)")
    action_group.add_argument("--city", metavar="NAME", help="Named city preset")
    action_group.add_argument("--gpx", metavar="FILE", help="Replay a GPX route file")
    action_group.add_argument(
        "--clear", action="store_true", help="Best-effort clear of GPS override (resets to 0,0)"
    )
    action_group.add_argument(
        "--list-cities", action="store_true", help="List available city presets"
    )

    # Coordinate companion
    parser.add_argument("--lng", type=float, help="Longitude (used with --lat)")

    # GPX replay pacing. --speed is an alias for --interval kept for parity with
    # the iOS script's vocabulary; both set the inter-point delay in seconds.
    pace_group = parser.add_mutually_exclusive_group()
    pace_group.add_argument(
        "--interval",
        type=float,
        metavar="SECONDS",
        help=(
            f"Seconds between GPX points "
            f"(default: {DEFAULT_GEO_INTERVAL}, override via ANDROID_EMU_GEO_INTERVAL)"
        ),
    )
    pace_group.add_argument(
        "--speed",
        type=float,
        metavar="SECONDS",
        help="Alias for --interval (seconds between GPX points)",
    )

    # Device selection
    parser.add_argument(
        "--serial", dest="device_serial", help="Target emulator serial (auto-detects if omitted)"
    )

    # Output flags
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    # --list-cities needs no device; handle it before any resolution.
    if args.list_cities:
        cities = list_cities()
        if args.json:
            print(
                json.dumps(
                    {
                        "action": "list_cities",
                        "cities": [
                            {"name": c, "lat": CITY_PRESETS[c][0], "lng": CITY_PRESETS[c][1]}
                            for c in cities
                        ],
                    },
                    indent=2,
                )
            )
        else:
            print(f"Available cities ({len(cities)}):")
            for city in cities:
                lat, lng = CITY_PRESETS[city]
                print(f"  - {city} ({lat}, {lng})")
        sys.exit(0)

    # --lat and --lng must be provided together.
    if (args.lat is None) != (args.lng is None):
        parser.error("--lat and --lng must be provided together")

    interval = args.interval if args.interval is not None else args.speed
    if interval is None:
        interval = DEFAULT_GEO_INTERVAL

    # Resolve device serial (None means "use default device").
    try:
        serial = resolve_device_identifier(args.device_serial)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    manager = LocationManager(serial=serial)

    # === Dispatch ===

    if args.lat is not None:
        success, message = manager.set_coordinate(args.lat, args.lng, verbose=args.verbose)
        action = "set_coordinate"
        extra: dict = {"lat": args.lat, "lng": args.lng}

    elif args.city:
        success, message = manager.set_city(args.city, verbose=args.verbose)
        action = "set_city"
        coords = resolve_city(args.city)
        extra = {
            "city": args.city,
            "lat": coords[0] if coords else None,
            "lng": coords[1] if coords else None,
        }

    elif args.gpx:
        try:
            waypoints = _read_gpx_file(args.gpx)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        success, message = manager.replay_gpx(
            waypoints, interval_seconds=interval, verbose=args.verbose
        )
        action = "replay_gpx"
        extra = {
            "gpx": args.gpx,
            "points": len(waypoints),
            "interval_seconds": interval,
        }

    else:  # --clear
        success, message = manager.clear(verbose=args.verbose)
        action = "clear"
        extra = {}

    # === Output ===

    if args.json:
        print(
            json.dumps(
                {
                    "action": action,
                    "serial": serial,
                    "success": success,
                    "message": message,
                    **extra,
                },
                indent=2,
            )
        )
    else:
        print(message)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
