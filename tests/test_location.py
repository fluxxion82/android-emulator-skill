"""Device-free tests for location.py pure logic.

These exercise the three pieces of pure logic without any emulator/adb:

* GPX parsing into ordered (lat, lng) waypoints (namespaces, rtept/wpt, errors),
* city-preset lookup (case/space-insensitive, aliases, unknowns), and
* 'emu geo fix' command construction — in particular the LON-before-LAT order,
  which is the reverse of the human-friendly lat/lng input.

The device-touching path (set_coordinate / replay_gpx) is also covered by
monkeypatching the module's subprocess.run so no real device is needed.
"""

from __future__ import annotations

import importlib

import location
import pytest
from location import (
    CITY_PRESETS,
    LocationManager,
    build_geo_fix_command,
    list_cities,
    parse_gpx,
    resolve_city,
    validate_coordinate,
)


class _FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess (success)."""

    returncode = 0
    stdout = "OK"
    stderr = ""


# === GPX parsing ===

_GPX_BASIC = """<?xml version="1.0"?>
<gpx version="1.1">
  <trk><trkseg>
    <trkpt lat="51.5074" lon="-0.1278"/>
    <trkpt lat="48.8566" lon="2.3522"/>
    <trkpt lat="40.7128" lon="-74.0060"/>
  </trkseg></trk>
</gpx>
"""

_GPX_NAMESPACED = """<?xml version="1.0"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
  <trk><trkseg>
    <trkpt lat="35.6762" lon="139.6503"/>
    <trkpt lat="-33.8688" lon="151.2093"/>
  </trkseg></trk>
</gpx>
"""


def test_parse_gpx_basic_order_and_values():
    pts = parse_gpx(_GPX_BASIC)
    assert pts == [
        (51.5074, -0.1278),
        (48.8566, 2.3522),
        (40.7128, -74.0060),
    ]


def test_parse_gpx_preserves_document_order():
    pts = parse_gpx(_GPX_BASIC)
    # First point must be London, last must be New York (insertion order).
    assert pts[0] == (51.5074, -0.1278)
    assert pts[-1] == (40.7128, -74.0060)


def test_parse_gpx_handles_default_namespace():
    pts = parse_gpx(_GPX_NAMESPACED)
    assert pts == [(35.6762, 139.6503), (-33.8688, 151.2093)]


def test_parse_gpx_accepts_rtept_and_wpt():
    xml = """<gpx>
      <wpt lat="1.0" lon="2.0"/>
      <rte><rtept lat="3.0" lon="4.0"/></rte>
    </gpx>"""
    assert parse_gpx(xml) == [(1.0, 2.0), (3.0, 4.0)]


def test_parse_gpx_empty_returns_empty_list():
    assert parse_gpx("<gpx></gpx>") == []


def test_parse_gpx_missing_attr_raises():
    with pytest.raises(ValueError, match="missing lat/lon"):
        parse_gpx('<gpx><trkpt lat="1.0"/></gpx>')


def test_parse_gpx_non_numeric_raises():
    with pytest.raises(ValueError, match="non-numeric"):
        parse_gpx('<gpx><trkpt lat="north" lon="2.0"/></gpx>')


def test_parse_gpx_out_of_range_raises():
    with pytest.raises(ValueError, match="latitude"):
        parse_gpx('<gpx><trkpt lat="999" lon="0"/></gpx>')


def test_parse_gpx_malformed_xml_raises():
    with pytest.raises(ValueError, match="Malformed GPX"):
        parse_gpx("<gpx><trkpt lat=")


# === City preset lookup ===


def test_resolve_city_exact():
    assert resolve_city("london") == (51.5074, -0.1278)


def test_resolve_city_case_and_space_insensitive():
    assert resolve_city("New York") == resolve_city("newyork")
    assert resolve_city("SAN FRANCISCO") == resolve_city("sanfrancisco")


def test_resolve_city_aliases_match_canonical():
    assert resolve_city("nyc") == resolve_city("newyork")
    assert resolve_city("sf") == resolve_city("sanfrancisco")
    assert resolve_city("la") == resolve_city("losangeles")


def test_resolve_city_underscore_and_hyphen():
    assert resolve_city("san_francisco") == resolve_city("sanfrancisco")
    assert resolve_city("new-york") == resolve_city("newyork")


def test_resolve_city_unknown_returns_none():
    assert resolve_city("atlantis") is None


def test_list_cities_excludes_aliases():
    cities = list_cities()
    assert "nyc" not in cities
    assert "sf" not in cities
    assert "la" not in cities
    assert "london" in cities
    assert "newyork" in cities


def test_list_cities_sorted():
    assert list_cities() == sorted(list_cities())


# === emu geo fix command construction (LON before LAT!) ===


def test_build_geo_fix_lon_before_lat():
    cmd = build_geo_fix_command("emulator-5554", lat=51.5074, lng=-0.1278)
    # Structure: adb -s <serial> emu geo fix <LON> <LAT>
    assert cmd[:6] == ["adb", "-s", "emulator-5554", "emu", "geo", "fix"]
    lon_str, lat_str = cmd[6], cmd[7]
    assert float(lon_str) == -0.1278  # longitude FIRST
    assert float(lat_str) == 51.5074  # latitude SECOND


def test_build_geo_fix_no_serial_omits_flag():
    cmd = build_geo_fix_command(None, lat=10.0, lng=20.0)
    assert cmd[:4] == ["adb", "emu", "geo", "fix"]
    assert "-s" not in cmd
    assert float(cmd[4]) == 20.0  # lon
    assert float(cmd[5]) == 10.0  # lat


def test_build_geo_fix_negative_coords_roundtrip():
    cmd = build_geo_fix_command("emulator-5556", lat=-23.5505, lng=-46.6333)
    assert float(cmd[-2]) == -46.6333  # lon
    assert float(cmd[-1]) == -23.5505  # lat


# === validation helper ===


@pytest.mark.parametrize("lat,lng", [(0, 0), (90, 180), (-90, -180), (51.5, -0.1)])
def test_validate_coordinate_accepts_valid(lat, lng):
    assert validate_coordinate(lat, lng) is None


@pytest.mark.parametrize("lat,lng", [(91, 0), (-91, 0)])
def test_validate_coordinate_rejects_bad_lat(lat, lng):
    assert "latitude" in validate_coordinate(lat, lng)


@pytest.mark.parametrize("lat,lng", [(0, 181), (0, -181)])
def test_validate_coordinate_rejects_bad_lng(lat, lng):
    assert "longitude" in validate_coordinate(lat, lng)


# === tunables ===


def test_default_geo_interval():
    assert location.DEFAULT_GEO_INTERVAL == 1.0


def test_geo_interval_env_override(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_GEO_INTERVAL", "0.25")
    reloaded = importlib.reload(location)
    try:
        assert reloaded.DEFAULT_GEO_INTERVAL == 0.25
    finally:
        monkeypatch.delenv("ANDROID_EMU_GEO_INTERVAL", raising=False)
        importlib.reload(location)


# === device-touching path (subprocess mocked) ===


@pytest.fixture
def captured_cmds(monkeypatch):
    """Capture every command passed to the module's subprocess.run."""
    cmds: list[list[str]] = []

    def fake_run(cmd, *_args, **_kwargs):
        cmds.append(list(cmd))
        return _FakeCompleted()

    monkeypatch.setattr(location.subprocess, "run", fake_run)
    return cmds


def test_set_coordinate_issues_geo_fix(captured_cmds):
    ok, msg = LocationManager(serial="emulator-5554").set_coordinate(51.5074, -0.1278)
    assert ok is True
    assert "51.5074" in msg
    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert cmd[3:6] == ["emu", "geo", "fix"]
    assert float(cmd[6]) == -0.1278  # lon first
    assert float(cmd[7]) == 51.5074


def test_set_city_resolves_and_fixes(captured_cmds):
    ok, msg = LocationManager(serial="emulator-5554").set_city("tokyo")
    assert ok is True
    assert "Tokyo" in msg
    assert float(captured_cmds[0][6]) == CITY_PRESETS["tokyo"][1]  # lon
    assert float(captured_cmds[0][7]) == CITY_PRESETS["tokyo"][0]  # lat


def test_set_city_unknown_makes_no_call(captured_cmds):
    ok, msg = LocationManager(serial="emulator-5554").set_city("atlantis")
    assert ok is False
    assert "Unknown city" in msg
    assert captured_cmds == []


def test_replay_gpx_issues_one_call_per_point(monkeypatch, captured_cmds):
    monkeypatch.setattr(location.time, "sleep", lambda _s: None)
    pts = parse_gpx(_GPX_BASIC)
    ok, msg = LocationManager(serial="emulator-5554").replay_gpx(pts, interval_seconds=0.0)
    assert ok is True
    assert "3 points" in msg
    geo_calls = [c for c in captured_cmds if c[3:6] == ["emu", "geo", "fix"]]
    assert len(geo_calls) == 3
    # First call is the first waypoint, lon-first.
    assert float(geo_calls[0][6]) == -0.1278
    assert float(geo_calls[0][7]) == 51.5074


def test_replay_gpx_sleeps_between_points_only(monkeypatch, captured_cmds):
    sleeps: list[float] = []
    monkeypatch.setattr(location.time, "sleep", lambda s: sleeps.append(s))
    pts = parse_gpx(_GPX_BASIC)  # 3 points
    LocationManager(serial="emulator-5554").replay_gpx(pts, interval_seconds=0.5)
    # n-1 sleeps for n points.
    assert sleeps == [0.5, 0.5]


def test_replay_gpx_empty_makes_no_call(captured_cmds):
    ok, msg = LocationManager(serial="emulator-5554").replay_gpx([], interval_seconds=0.0)
    assert ok is False
    assert "no track points" in msg
    assert captured_cmds == []


def test_physical_device_is_refused(monkeypatch):
    # A non-emulator serial that resolves to a physical device must be refused
    # before any geo fix call, not silently no-op'd.
    monkeypatch.setattr(
        location,
        "get_connected_devices",
        lambda: [{"serial": "ABC123", "state": "device", "type": "device"}],
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        location.subprocess, "run", lambda cmd, *a, **k: calls.append(list(cmd)) or _FakeCompleted()
    )
    ok, msg = LocationManager(serial="ABC123").set_coordinate(1.0, 2.0)
    assert ok is False
    assert "physical device" in msg
    assert "mock-location" in msg
    assert calls == []
