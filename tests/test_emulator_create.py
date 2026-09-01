"""Device-free tests for emulator_create.

These exercise the pure logic added for the three feature deltas without any
SDK / avdmanager / sdkmanager binary:

  1. ``--api`` defaults to the latest *installed* system-image API level.
  2. ``--name`` is auto-generated from device + API when omitted.
  3. ``--device`` is fuzzy-matched against avdmanager device definitions.

Where a subprocess boundary exists, the module's ``subprocess`` is monkeypatched
so the arg->command mapping (and parsing) is asserted without a real toolchain.
"""

from __future__ import annotations

import importlib
import subprocess

import emulator_create

DEVICES = [
    {"id": "pixel_7", "name": "Pixel 7"},
    {"id": "pixel_7_pro", "name": "Pixel 7 Pro"},
    {"id": "pixel_tablet", "name": "Pixel Tablet"},
    {"id": "Nexus 6", "name": "Nexus 6"},
]


# --- Delta 3: fuzzy device matching ----------------------------------------


def test_fuzzy_exact_id():
    assert emulator_create.fuzzy_match_device("pixel_7", DEVICES) == "pixel_7"


def test_fuzzy_exact_id_case_insensitive():
    assert emulator_create.fuzzy_match_device("PIXEL_7", DEVICES) == "pixel_7"


def test_fuzzy_normalized_name_match():
    # "Pixel 7" (name) normalizes to the same token as id "pixel_7".
    assert emulator_create.fuzzy_match_device("Pixel 7", DEVICES) == "pixel_7"


def test_fuzzy_collapsed_spacing():
    # "pixel7" with no separator still resolves to pixel_7.
    assert emulator_create.fuzzy_match_device("pixel7", DEVICES) == "pixel_7"


def test_fuzzy_substring_prefers_specific():
    # "pixel 7 pro" should land on the pro variant, not the base.
    assert emulator_create.fuzzy_match_device("pixel 7 pro", DEVICES) == "pixel_7_pro"


def test_fuzzy_typo_within_cutoff():
    assert emulator_create.fuzzy_match_device("pixxel_tablet", DEVICES) == "pixel_tablet"


def test_fuzzy_no_match_returns_none():
    assert emulator_create.fuzzy_match_device("totally-unknown-device", DEVICES) is None


def test_fuzzy_empty_inputs():
    assert emulator_create.fuzzy_match_device("", DEVICES) is None
    assert emulator_create.fuzzy_match_device("pixel_7", []) is None


def test_suggest_devices_orders_by_closeness():
    suggestions = emulator_create.suggest_devices("pixel", DEVICES, limit=2)
    assert len(suggestions) == 2
    assert all(s in {d["id"] for d in DEVICES} for s in suggestions)
    assert suggestions[0].startswith("pixel")


# --- Delta 1: latest installed API default ---------------------------------


def test_latest_api_level_picks_max():
    images = [
        {"api_level": 31},
        {"api_level": 34},
        {"api_level": 33},
    ]
    assert emulator_create.latest_api_level(images) == 34


def test_latest_api_level_empty():
    assert emulator_create.latest_api_level([]) is None


def test_latest_api_level_ignores_non_int():
    assert emulator_create.latest_api_level([{"api_level": None}, {"foo": 1}]) is None


def test_resolve_api_level_honors_explicit(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    # Explicit value short-circuits; installed list never consulted.
    monkeypatch.setattr(
        creator, "list_installed_system_images", lambda: (_ for _ in ()).throw(AssertionError)
    )
    assert creator.resolve_api_level(33) == 33


def test_resolve_api_level_defaults_to_latest_installed(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(
        creator,
        "list_installed_system_images",
        lambda: [{"api_level": 30}, {"api_level": 34}],
    )
    assert creator.resolve_api_level(None) == 34


def test_resolve_api_level_none_when_nothing_installed(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "list_installed_system_images", lambda: [])
    assert creator.resolve_api_level(None) is None


def test_list_installed_system_images_parses_sdkmanager(monkeypatch, recorded):
    """Rewritten against recorded output; it used to assert an invented format.

    The old input was
    `system-images;android-34;google_apis;x86_64 | 7 | Google APIs` --
    semicolons AND pipe-delimited columns. sdkmanager prints neither: it emits
    `  system-images/android-34/google_apis/arm64-v8a` with whitespace-padded
    columns. The invented format and the parser agreed with each other, so this
    test passed while the parser matched nothing on a real machine and AVD
    creation was impossible.
    """
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["check"] = kwargs.get("check")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=recorded.text("sdkmanager_list_installed"), stderr=""
        )

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    images = creator.list_installed_system_images()

    assert captured["cmd"] == ["/fake/sdkmanager", "--list_installed"]
    assert captured["check"] is True
    assert images, "no images parsed from real sdkmanager output"
    # Only system-images lines; build-tools and platforms are ignored.
    assert all(img["id"].startswith("system-images;") for img in images)
    assert emulator_create.latest_api_level(images) == 35


# --- Delta 2: auto-generated AVD name --------------------------------------


def test_generate_avd_name_basic():
    assert emulator_create.generate_avd_name("pixel_7", 34) == "pixel_7_API_34"


def test_generate_avd_name_sanitizes_spaces():
    assert emulator_create.generate_avd_name("Pixel 7 Pro", 33) == "Pixel_7_Pro_API_33"


def test_generate_avd_name_collapses_separators():
    assert emulator_create.generate_avd_name("  Pixel--7  ", 31) == "Pixel_7_API_31"


def test_generate_avd_name_empty_base_falls_back():
    assert emulator_create.generate_avd_name("@@@", 30) == "AVD_API_30"


# --- create() arg->command mapping (subprocess mocked) ---------------------


def test_create_builds_expected_avdmanager_command(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")
    # No system-image pre-check (skips that subprocess call path).
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: None)

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["check"] = kwargs.get("check")
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    success, _message, avd_name = creator.create(
        device_id="pixel_7", api_level=34, name="MyAVD", abi="x86_64", variant="google_apis"
    )

    assert success is True
    assert avd_name == "MyAVD"
    assert captured["check"] is True
    assert captured["input"] == "no\n"
    assert captured["cmd"] == [
        "/fake/avdmanager",
        "create",
        "avd",
        "--name",
        "MyAVD",
        "--package",
        "system-images;android-34;google_apis;x86_64",
        "--device",
        "pixel_7",
    ]


def test_resolve_device_returns_suggestions_on_miss(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "list_device_definitions", lambda: DEVICES)
    device_id, suggestions = creator.resolve_device("zzzzzzzz")
    assert device_id is None
    assert suggestions  # non-empty close-match list for the error message


def test_resolve_device_matches(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "list_device_definitions", lambda: DEVICES)
    device_id, suggestions = creator.resolve_device("Pixel 7")
    assert device_id == "pixel_7"
    assert suggestions == []


# --- tunable env override ---------------------------------------------------


def test_tunables_env_override(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_DEVICE_MATCH_CUTOFF", "90")
    monkeypatch.setenv("ANDROID_EMU_DEVICE_MATCH_SUGGEST", "3")
    reloaded = importlib.reload(emulator_create)
    try:
        assert reloaded.DEVICE_MATCH_CUTOFF == 90
        assert reloaded.DEVICE_MATCH_SUGGEST == 3
    finally:
        monkeypatch.delenv("ANDROID_EMU_DEVICE_MATCH_CUTOFF", raising=False)
        monkeypatch.delenv("ANDROID_EMU_DEVICE_MATCH_SUGGEST", raising=False)
        importlib.reload(emulator_create)


def test_default_tunables():
    assert emulator_create.DEVICE_MATCH_CUTOFF == 60
    assert emulator_create.DEVICE_MATCH_SUGGEST == 5


# --- every SDK-tool call is bounded ----------------------------------------
# avdmanager/sdkmanager are Android SDK tools, not adb, so they do not route
# through common.adb_exec. That makes it easy for one to go out unbounded again,
# so every call site in the module is exercised below.


def test_every_sdk_tool_call_is_bounded(monkeypatch):
    """Six of the AST sweep's unbounded subprocess calls lived in this file."""
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")

    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        # create()'s pre-check looks for the image id in `sdkmanager --list`.
        return subprocess.CompletedProcess(
            cmd, 0, stdout="system-images;android-34;google_apis;x86_64\n", stderr=""
        )

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    creator.list_device_definitions()
    creator.list_system_images()
    creator.list_installed_system_images()
    creator.create(device_id="pixel_7", api_level=34, name="MyAVD")
    creator.delete("MyAVD")

    assert len(calls) == 6, "a call site was missed; update this test with it"
    unbounded = [c["cmd"] for c in calls if not c["kwargs"].get("timeout")]
    assert not unbounded, f"unbounded SDK-tool calls: {unbounded}"


def test_a_listing_timeout_degrades_to_an_empty_list(monkeypatch):
    """A bounded call raises TimeoutExpired; that must not reach the user raw."""
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")

    def fake_run(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=emulator_create.SDK_TOOL_TIMEOUT)

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    assert creator.list_device_definitions() == []


def test_create_reports_its_own_timeout(monkeypatch):
    """avdmanager waiting on stdin used to hang forever; now it is a message."""
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: None)

    def fake_run(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=emulator_create.SDK_TOOL_TIMEOUT)

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    success, message, avd_name = creator.create(device_id="pixel_7", api_level=34, name="MyAVD")

    assert success is False
    assert avd_name is None
    assert str(emulator_create.SDK_TOOL_TIMEOUT) in message
    assert "MyAVD" in message


# ---------------------------------------------------------------------------
# AVD creation could not work at all. Three reasons, all imagined output.
# ---------------------------------------------------------------------------


def _creator(monkeypatch, stdout: str):
    """An EmulatorCreator whose SDK tools return the given text."""
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")
    monkeypatch.setattr(
        emulator_create.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, stdout, ""),
    )
    return creator


def test_installed_images_are_found_in_sdkmanagers_real_format(monkeypatch, recorded):
    """`--list_installed` prints PATHS in whitespace columns, not `id | id | id`.

    The parser looked for lines starting `system-images;` and split them on
    `|`. sdkmanager prints `  system-images/android-34/google_apis/arm64-v8a`
    followed by space-padded columns, so nothing ever matched: the installed
    list was always empty, every image reported "not installed", and no AVD
    could be created -- while telling the user to install something they
    already had.
    """
    creator = _creator(monkeypatch, recorded.text("sdkmanager_list_installed"))
    images = creator.list_installed_system_images()

    assert images, "no installed images parsed from real sdkmanager output"
    ids = {image["id"] for image in images}
    # The *install* id uses semicolons even though the listing uses slashes.
    assert "system-images;android-34;google_apis;arm64-v8a" in ids, ids
    assert all(";" in image_id for image_id in ids), f"slashes left in ids: {ids}"
    assert all(isinstance(image["api_level"], int) for image in images)


def test_a_device_id_line_is_two_identifiers_not_one(monkeypatch, recorded):
    """`id: 53 or "pixel_9"` names one device twice.

    The whole tail was kept as the id and passed to `avdmanager --device`,
    which replied `No device found matching --device 53 or "pixel_9"` --
    echoing back the string it had been handed.
    """
    creator = _creator(monkeypatch, recorded.text("avdmanager_list_device"))
    devices = creator.list_device_definitions()

    assert devices, "no device definitions parsed"
    pixel = next((d for d in devices if d["id"] == "pixel_9"), None)
    assert (
        pixel is not None
    ), f"pixel_9 not resolvable; got ids like {[d['id'] for d in devices[:5]]}"
    assert pixel["index"] == "53"
    assert pixel["name"] == "Pixel 9"
    assert not any(
        " or " in device["id"] for device in devices
    ), 'a device id still carries the whole `N or "name"` line'
