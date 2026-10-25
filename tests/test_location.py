"""Device-free tests for location.py pure logic.

These exercise the three pieces of pure logic without any emulator/adb:

* GPX parsing into ordered (lat, lng) waypoints (namespaces, rtept/wpt, errors),
* city-preset lookup (case/space-insensitive, aliases, unknowns), and
* 'emu geo fix' command construction — in particular the LON-before-LAT order,
  which is the reverse of the human-friendly lat/lng input.

The device-touching path (set_coordinate / replay_gpx) is also covered by
monkeypatching the module's subprocess.run so no real device is needed.

The GPX comes from a recording
------------------------------
GPX is not tool output this skill runs a command to get: it is a document the
*user* hands to ``location.py --gpx``, written by a GPS device, a mapping
service or a GPX library. Nothing in adb, the Android SDK, the emulator or
Android Studio writes one, so the recording comes from a real GPX writer:
``gpxpy`` (``tests/record_gpx_fixtures.py``, run through uvx; the command, the
gpxpy version and the route are in ``tests/fixtures/recorded/gpxpy-1.6.2/
MANIFEST.json``). The route is chosen -- three points around the emulator's
default location -- and the document is gpxpy's ``to_xml()`` output verbatim,
in GPX 1.1 and GPX 1.0. They reach the tests through the ``recorded_gpx``
fixture.

What the recordings carry that the old hand-written documents did not: the
GPX 1.1 and GPX 1.0 namespaces, ``xsi:schemaLocation``, a ``creator``, a track
``<name>``, and ``<ele>`` / ``<time>`` children inside a point.

Every other document here is DERIVED from a recording, by the documented string
edits in ``_without_namespace``, ``_with_point_tag``, ``_without_points`` and
``_crlf`` and the substitutions in the error tests, never written from memory.
What remains untested, said plainly: ``<metadata>``, ``<extensions>`` and a
watch's or a mapping service's own export have still never been through it.
"""

from __future__ import annotations

import importlib

import location
import pytest
from location import (
    CITY_PRESETS,
    LocationManager,
    build_geo_fix_args,
    list_cities,
    parse_gpx,
    resolve_city,
    validate_coordinate,
)

from common import adb_exec


class _FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess (success).

    ``stdout`` is the console's bare acknowledgement, which is what
    ``adb emu geo fix`` answers with; ``run_emu`` strips it.
    """

    returncode = 0
    stdout = "OK"
    stderr = ""


# === GPX parsing ===

GPX_11 = "gpxpy_route_gpx11.gpx"
GPX_10 = "gpxpy_route_gpx10.gpx"

# The recorded route, as gpxpy wrote its lat/lon attributes (MANIFEST "route").
ROUTE = [(37.422, -122.0841), (37.4226, -122.085), (37.4232, -122.0838)]

# The first point's attributes, exactly as the recording spells them.
FIRST_LAT = 'lat="37.422"'
FIRST_LON = ' lon="-122.0841"'


def _without_namespace(gpx: str, version: str = "1/1") -> str:
    """The recording with its default namespace declaration removed.

    Derivation: delete ``xmlns="http://www.topografix.com/GPX/<version>" `` from
    the root, nothing else touched (``xmlns:xsi`` stays). Not every writer
    declares one -- Xcode's GPX template does not -- so the parser must read
    the bare ``trkpt`` as well as ``{...GPX/1/1}trkpt``.
    """
    declaration = f'xmlns="http://www.topografix.com/GPX/{version}" '
    assert gpx.count(declaration) == 1, "the recording changed"
    return gpx.replace(declaration, "")


def _with_point_tag(gpx: str, tag: str) -> str:
    """The recording with its ``trkpt`` elements renamed to ``tag``.

    Derivation: ``<trkpt `` and ``</trkpt>`` renamed, attributes and children
    untouched. ``rtept`` and ``wpt`` are the GPX schema's other two point
    elements (all three are ``wptType``); the parser matches the local tag name
    wherever it sits, which is what this pins, not a route's nesting.
    """
    return gpx.replace("<trkpt ", f"<{tag} ").replace("</trkpt>", f"</{tag}>")


def _without_points(gpx: str) -> str:
    """The recording with every ``<trkpt>`` element cut out of its ``<trkseg>``.

    Derivation: delete from the first ``<trkpt `` to the end of the last
    ``</trkpt>``; the track, its name and its empty segment stay.
    """
    start = gpx.index("<trkpt ")
    end = gpx.rindex("</trkpt>") + len("</trkpt>")
    return gpx[:start] + gpx[end:]


def _crlf(gpx: str) -> str:
    """The recording with CRLF line endings.

    Derivation: every ``\\n`` becomes ``\\r\\n``, nothing else. A Windows
    writer, or a checkout with ``core.autocrlf``, hands the parser exactly
    this; gpxpy itself writes LF.
    """
    assert "\r" not in gpx, "the recording changed"
    return gpx.replace("\n", "\r\n")


@pytest.mark.parametrize("name", [GPX_11, GPX_10], ids=["gpx11", "gpx10"])
def test_parse_gpx_reads_the_recorded_route_in_document_order(recorded_gpx, name):
    """Namespaced, schemaLocation, creator, <name>, <ele>/<time> children: the real thing."""
    assert parse_gpx(recorded_gpx(name)) == ROUTE


def test_parse_gpx_preserves_document_order(recorded_gpx):
    pts = parse_gpx(recorded_gpx(GPX_11))
    assert pts[0] == (37.422, -122.0841)
    assert pts[-1] == (37.4232, -122.0838)


@pytest.mark.parametrize(
    ("name", "version"), [(GPX_11, "1/1"), (GPX_10, "1/0")], ids=["gpx11", "gpx10"]
)
def test_parse_gpx_reads_a_document_with_no_default_namespace(recorded_gpx, name, version):
    assert parse_gpx(_without_namespace(recorded_gpx(name), version)) == ROUTE


def test_parse_gpx_survives_crlf_line_endings(recorded_gpx):
    assert parse_gpx(_crlf(recorded_gpx(GPX_11))) == ROUTE


@pytest.mark.parametrize("tag", ["trkpt", "rtept", "wpt"])
def test_parse_gpx_accepts_every_point_element(recorded_gpx, tag):
    assert parse_gpx(_with_point_tag(recorded_gpx(GPX_11), tag)) == ROUTE


def test_parse_gpx_empty_returns_empty_list(recorded_gpx):
    assert parse_gpx(_without_points(recorded_gpx(GPX_11))) == []


def test_parse_gpx_missing_attr_raises(recorded_gpx):
    gpx = recorded_gpx(GPX_11)
    assert gpx.count(FIRST_LON) == 1, "the recording changed"
    with pytest.raises(ValueError, match="missing lat/lon"):
        parse_gpx(gpx.replace(FIRST_LON, ""))


def test_parse_gpx_non_numeric_raises(recorded_gpx):
    gpx = recorded_gpx(GPX_11).replace(FIRST_LAT, 'lat="north"')
    with pytest.raises(ValueError, match="non-numeric"):
        parse_gpx(gpx)


def test_parse_gpx_out_of_range_raises(recorded_gpx):
    gpx = recorded_gpx(GPX_11).replace(FIRST_LAT, 'lat="999"')
    with pytest.raises(ValueError, match="latitude"):
        parse_gpx(gpx)


def test_parse_gpx_malformed_xml_raises(recorded_gpx):
    """The recording cut off inside its first point's attribute, as a truncated download is."""
    gpx = recorded_gpx(GPX_11)
    with pytest.raises(ValueError, match="Malformed GPX"):
        parse_gpx(gpx[: gpx.index(FIRST_LAT) + len('lat="')])


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


# === geo fix console arguments (LON before LAT!) ===
#
# L7: the `adb ... emu` prefix is no longer built here. It belongs to
# common.emu_console.run_emu, which is the only place in the skill that speaks
# the console protocol; this module builds the console-side arguments only. The
# full argv is still asserted below, at the subprocess boundary under adb_exec,
# so the prefix is checked where it is now built rather than not at all.


def test_build_geo_fix_lon_before_lat():
    args = build_geo_fix_args(lat=51.5074, lng=-0.1278)
    assert args[:2] == ["geo", "fix"]
    lon_str, lat_str = args[2], args[3]
    assert float(lon_str) == -0.1278  # longitude FIRST
    assert float(lat_str) == 51.5074  # latitude SECOND


def test_build_geo_fix_builds_no_adb_argv():
    """The console prefix is emu_console's to build, and only its.

    Re-adding `adb`/`-s`/`emu` here would be the L7 defect coming back in the
    one file whose finding named a *builder* rather than a call site.
    """
    args = build_geo_fix_args(lat=10.0, lng=20.0)
    assert args == ["geo", "fix", repr(20.0), repr(10.0)]
    assert not {"adb", "-s", "emu"} & set(args)


def test_build_geo_fix_negative_coords_roundtrip():
    args = build_geo_fix_args(lat=-23.5505, lng=-46.6333)
    assert float(args[-2]) == -46.6333  # lon
    assert float(args[-1]) == -23.5505  # lat


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
    """Capture every argv issued, at the subprocess boundary under adb_exec.

    Patched one layer lower than before L7: the geo call goes out through
    ``common.emu_console.run_emu`` now, so ``location.subprocess`` is no longer
    on the path. Patching the boundary every adb caller shares keeps these
    assertions on the real argv -- `adb -s <serial> emu geo fix <lon> <lat>` --
    instead of on a mock of the wrapper.
    """
    cmds: list[list[str]] = []

    def fake_run(cmd, *_args, **_kwargs):
        cmds.append(list(cmd))
        return _FakeCompleted()

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
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


def test_replay_gpx_issues_one_call_per_point(monkeypatch, captured_cmds, recorded_gpx):
    monkeypatch.setattr(location.time, "sleep", lambda _s: None)
    pts = parse_gpx(recorded_gpx(GPX_11))
    ok, msg = LocationManager(serial="emulator-5554").replay_gpx(pts, interval_seconds=0.0)
    assert ok is True
    assert "3 points" in msg
    geo_calls = [c for c in captured_cmds if c[3:6] == ["emu", "geo", "fix"]]
    assert len(geo_calls) == 3
    # First call is the first point of the recorded route, lon-first.
    assert float(geo_calls[0][6]) == -122.0841
    assert float(geo_calls[0][7]) == 37.422


def test_replay_gpx_sleeps_between_points_only(monkeypatch, captured_cmds, recorded_gpx):
    sleeps: list[float] = []
    monkeypatch.setattr(location.time, "sleep", lambda s: sleeps.append(s))
    pts = parse_gpx(recorded_gpx(GPX_11))  # 3 points
    LocationManager(serial="emulator-5554").replay_gpx(pts, interval_seconds=0.5)
    # n-1 sleeps for n points.
    assert sleeps == [0.5, 0.5]


def test_replay_gpx_empty_makes_no_call(captured_cmds):
    ok, msg = LocationManager(serial="emulator-5554").replay_gpx([], interval_seconds=0.0)
    assert ok is False
    assert "no track points" in msg
    assert captured_cmds == []


def test_a_ko_reply_fails_the_geo_fix(monkeypatch):
    """L7: the console rejects at exit status 0, and this path must fail on it.

    `_run_geo_fix` used to test `"KO" in result.stdout` itself. Routing it
    through `run_emu` moves that check to the one module that owns the console
    protocol -- and the point of the routing is that the answer does not change.
    """

    class _Ko:
        returncode = 0
        stdout = "KO: bad coordinates\r\nOK\r\n"
        stderr = ""

    monkeypatch.setattr(adb_exec.subprocess, "run", lambda *_a, **_k: _Ko())

    ok, msg = LocationManager(serial="emulator-5554").set_coordinate(51.5074, -0.1278)
    assert ok is False
    assert "KO: bad coordinates" in msg


def test_a_failing_adb_exit_fails_the_geo_fix(monkeypatch):
    """The other half: adb itself reporting failure is not a fix that landed."""

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "some console complaint"

    monkeypatch.setattr(adb_exec.subprocess, "run", lambda *_a, **_k: _Failed())

    ok, msg = LocationManager(serial="emulator-5554").set_coordinate(51.5074, -0.1278)
    assert ok is False
    assert "some console complaint" in msg


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
        adb_exec.subprocess, "run", lambda cmd, *a, **k: calls.append(list(cmd)) or _FakeCompleted()
    )
    ok, msg = LocationManager(serial="ABC123").set_coordinate(1.0, 2.0)
    assert ok is False
    assert "physical device" in msg
    assert "mock-location" in msg
    assert calls == []
