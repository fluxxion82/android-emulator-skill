#!/usr/bin/env python3
"""Record the GPX documents ``location.py --gpx`` is tested against.

GPX is not output this skill runs a command to get: it is a document a GPS
device, a mapping service or a GPX library WRITES and a user hands to
``location.py --gpx``. Nothing in adb, the Android SDK, the emulator or Android
Studio writes one, so the recording comes from a real GPX writer instead:
``gpxpy`` (MIT), the common Python GPX library. The route is ours -- three
points around the emulator's default location -- and the document is gpxpy's
``to_xml()`` output, written verbatim, in GPX 1.1 and GPX 1.0.

Run with uvx, so nothing is installed into the repo or the system::

    uvx --python 3.12 --with gpxpy==1.6.2 python tests/record_gpx_fixtures.py

Writes ``tests/fixtures/recorded/gpxpy-<version>/`` and its ``MANIFEST.json``.
Not a device profile (the manifest has no ``device`` block).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import platform
from pathlib import Path

import gpxpy
import gpxpy.gpx

RECORDED_ROOT = Path(__file__).resolve().parent / "fixtures" / "recorded"

COMMAND = "uvx --python 3.12 --with gpxpy==1.6.2 python tests/record_gpx_fixtures.py"

# (lat, lon, elevation metres or None, UTC time or None). Around the emulator's
# default location (37.4220, -122.0841). The first two carry <ele> and <time>,
# the third carries neither, so both shapes of a point are in the recording.
ROUTE = [
    (37.4220, -122.0841, 12.5, "2026-10-25T10:00:00+00:00"),
    (37.4226, -122.0850, 13.0, "2026-10-25T10:00:30+00:00"),
    (37.4232, -122.0838, None, None),
]

FIXTURES = [
    ("gpxpy_route_gpx11", "1.1"),
    ("gpxpy_route_gpx10", "1.0"),
]


def build_route() -> gpxpy.gpx.GPX:
    """One track, one segment, the ROUTE's points in order."""
    gpx = gpxpy.gpx.GPX()
    track = gpxpy.gpx.GPXTrack(name="emulator default location loop")
    segment = gpxpy.gpx.GPXTrackSegment()
    for lat, lon, elevation, when in ROUTE:
        segment.points.append(
            gpxpy.gpx.GPXTrackPoint(
                lat,
                lon,
                elevation=elevation,
                time=datetime.datetime.fromisoformat(when) if when else None,
            )
        )
    track.segments.append(segment)
    gpx.tracks.append(track)
    return gpx


def main() -> None:
    """Write each fixture verbatim and the manifest describing them."""
    out_dir = RECORDED_ROOT / f"gpxpy-{gpxpy.__version__}"
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, version in FIXTURES:
        xml = build_route().to_xml(version=version)
        data = xml.encode("utf-8")
        path = out_dir / f"{name}.gpx"
        path.write_bytes(data)
        entries.append(
            {
                "name": name,
                "file": path.name,
                "call": f"build_route().to_xml(version={version!r})",
                "gpx_version": version,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    manifest = {
        "profile": out_dir.name,
        "recorded_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gpxpy_version": gpxpy.__version__,
        "python_version": platform.python_version(),
        "command": COMMAND,
        "route": [
            {"lat": lat, "lon": lon, "ele": elevation, "time": when}
            for lat, lon, elevation, when in ROUTE
        ],
        "note": (
            "GPX is a document a GPS tool writes and a user hands to `location.py --gpx`; "
            "nothing in the Android toolchain writes one. These are gpxpy's to_xml() "
            "output for the route above, verbatim (one <trk>/<trkseg>, three <trkpt>, "
            "the first two with <ele> and <time>), in GPX 1.1 and GPX 1.0. The route is "
            "chosen, the document's form is gpxpy's."
        ),
        "fixtures": entries,
    }
    (out_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    for entry in entries:
        print(f"{out_dir.name}/{entry['file']}: {entry['bytes']} bytes")


if __name__ == "__main__":
    main()
