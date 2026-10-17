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
import platform
import subprocess

import emulator_create
import pytest

from common.sdk_tools import SdkToolError, without_null_lines

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
    """Rewritten against recorded output; it used to assert a hand-written one.

    The old input was
    `system-images;android-34;google_apis;x86_64 | 7 | Google APIs` --
    semicolons AND pipe-delimited columns, written from memory. cmdline-tools
    23's wrapper prints neither: it emits
    `  system-images/android-34/google_apis/arm64-v8a` with whitespace-padded
    columns, and the parser matched nothing there. The pipe table is real too --
    it is the Java sdkmanager's (sdkmanager_java_list_installed, below) -- which
    is why neither format may be the only one the parser reads.
    """
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=recorded.text("sdkmanager_list_installed"), stderr=""
        )

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    images = creator.list_installed_system_images()

    assert captured["cmd"] == ["/fake/sdkmanager", "--list_installed"]
    assert captured["timeout"], "sdkmanager --list_installed went out unbounded"
    assert images, "no images parsed from real sdkmanager output"
    # Only system-images lines; build-tools and platforms are ignored.
    assert all(img["id"].startswith("system-images;") for img in images)
    assert emulator_create.latest_api_level(images) == 35


def test_list_system_images_reads_both_sections_of_sdkmanager_list(monkeypatch, recorded):
    """AVD-LIST-IMAGES: `--list-images` read a format `sdkmanager --list` never printed.

    It required `system-images;` lines split on `|`; sdkmanager prints
    slash-separated paths in whitespace-padded columns, so the listing was
    empty on every machine. `--list` also differs from `--list_installed` in the
    one way that matters here: it prints an `Available packages:` section after
    the installed one, and only the section boundary says which images are
    already on disk.
    """
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0, stdout=recorded.text("sdkmanager_list"), stderr=""
        )

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    images = creator.list_system_images()

    assert captured["cmd"] == ["/fake/sdkmanager", "--list"]
    assert images, "no images parsed from real sdkmanager output"
    assert all(image["id"].startswith("system-images;") for image in images)
    assert any(image["installed"] for image in images)
    assert any(not image["installed"] for image in images)
    # build-tools, platforms, ndk and add-ons share the listing and are not images.
    assert not any("build-tools" in image["id"] for image in images)


def test_the_installed_section_of_list_agrees_with_list_installed(monkeypatch, recorded):
    """Two recordings of the same machine, cross-checked.

    `sdkmanager --list` and `sdkmanager --list_installed` were captured minutes
    apart from one SDK, so the images `--list` files under `Installed packages:`
    must be exactly the ones `--list_installed` prints. This is what pins the
    section tracking: a parser that ignored the headers would mark all 325 rows
    installed and still satisfy every other assertion here.
    """
    creator = emulator_create.EmulatorCreator()

    from_list = {
        image["id"]
        for image in emulator_create.parse_system_images(recorded.text("sdkmanager_list"))
        if image["installed"]
    }
    from_list_installed = {
        image["id"]
        for image in emulator_create.parse_system_images(recorded.text("sdkmanager_list_installed"))
    }

    assert from_list == from_list_installed, from_list ^ from_list_installed
    assert len(from_list) < len(
        emulator_create.parse_system_images(recorded.text("sdkmanager_list"))
    ), "everything came back installed; the section headers were ignored"
    assert creator is not None


def test_an_api_token_is_not_always_an_integer(recorded):
    """`android-34-ext12`, `android-36.1`, `android-37.2-beta1`, `android-CANARY`.

    76 of the 325 image rows in the recording have an API token that is not a
    bare integer. A parser demanding `android-(\\d+)` drops a quarter of the
    listing, so the token is kept verbatim in `api` and `api_level` is left None
    when it is not an integer -- `create()` builds its package id as
    `system-images;android-{api_level};...`, so calling `34-ext12` "API 34"
    would name an image that is not installed.
    """
    images = emulator_create.parse_system_images(recorded.text("sdkmanager_list"))
    levels = {image["api"]: image["api_level"] for image in images}
    ids = {image["id"] for image in images}

    assert "34-ext12" in levels, sorted(levels)[:20]
    assert levels["34-ext12"] is None
    assert levels["CANARY"] is None
    assert levels["34"] == 34
    assert "system-images;android-34-ext12;google_apis_playstore;x86_64" in ids
    # And they are listed, not silently dropped.
    assert sum(1 for image in images if image["api_level"] is None) > 0


# ---------------------------------------------------------------------------
# The Java sdkmanager's format. cmdline-tools 23's `sdkmanager` is a wrapper
# around the Android CLI (the recordings above); every earlier release, and the
# CI runner image, run the Java SdkManagerCli, which prints a pipe table keyed
# by the semicolon id. The parser read only the wrapper's rows, so on the
# runner `--list_installed` answered "none installed" right after the runner
# had installed and booted an image -- and the lane's AVD round trip skipped.
# ---------------------------------------------------------------------------


def test_list_installed_system_images_reads_the_java_sdkmanager(monkeypatch, recorded):
    """AVD-JAVA-SDKMANAGER: the installed images, in the format CI's sdkmanager prints."""
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=recorded.text("sdkmanager_java_list_installed"), stderr=""
        )

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    images = creator.list_installed_system_images()

    assert images, "the Java sdkmanager's installed images parsed to nothing"
    assert all(image["installed"] for image in images)
    ids = {image["id"] for image in images}
    assert "system-images;android-35;google_apis_playstore_ps16k;arm64-v8a" in ids
    assert emulator_create.latest_api_level(images) == 35
    # Not a column of the row: the Location column repeats the path with
    # slashes, and must not become a second image.
    assert len(ids) == len(images)
    assert not any("/" in image["id"] or "|" in image["id"] for image in images)


def test_both_sdkmanager_formats_name_the_same_installed_images(recorded):
    """Two recordings of one SDK, one per sdkmanager, cross-checked.

    The wrapper lists every installed image the Java tool lists except the one
    the Android CLI leaves out -- a privacy-sandbox image whose API token is not
    an integer. Anything more than that difference means one format is being
    misread.
    """
    java = emulator_create.parse_system_images(recorded.text("sdkmanager_java_list_installed"))
    wrapper = emulator_create.parse_system_images(recorded.text("sdkmanager_list_installed"))
    java_ids = {image["id"] for image in java}
    wrapper_ids = {image["id"] for image in wrapper}

    assert wrapper_ids < java_ids, wrapper_ids - java_ids
    (only_java,) = java_ids - wrapper_ids
    sandbox = next(image for image in java if image["id"] == only_java)
    assert sandbox["api"] == "UpsideDownCakePrivacySandbox"
    assert sandbox["api_level"] is None


def test_the_java_list_has_three_sections_and_only_one_is_installed(recorded):
    """`--list` from the Java tool: Installed, Available Packages, Available Updates.

    Its installed section must agree with `--list_installed`, and images under
    `Available Packages:` are the downloadable ones.
    """
    images = emulator_create.parse_system_images(recorded.text("sdkmanager_java_list"))
    installed = {image["id"] for image in images if image["installed"]}
    from_list_installed = {
        image["id"]
        for image in emulator_create.parse_system_images(
            recorded.text("sdkmanager_java_list_installed")
        )
    }

    assert installed == from_list_installed, installed ^ from_list_installed
    assert any(not image["installed"] for image in images), "no downloadable image was read"
    levels = {image["api"]: image["api_level"] for image in images}
    assert levels["34-ext12"] is None and levels["34"] == 34


def test_an_image_under_available_updates_is_not_read_as_a_download(recorded):
    """The Java tool's `Available Updates:` rows are packages already installed.

    The recorded section has one row, `emulator`. The same row naming an
    installed image -- what the section says when that image has a newer
    revision -- must add nothing: no extra entry for `--list-images` to offer.
    """
    text = recorded.text("sdkmanager_java_list")
    image_id = "system-images;android-34;google_apis;arm64-v8a"
    derived = text.replace("  emulator | 37.1.11", "  " + image_id + " | 37.1.11")
    assert derived.count(image_id) == text.count(image_id) + 1, "the update row was not found"

    assert emulator_create.parse_system_images(derived) == emulator_create.parse_system_images(text)


def test_list_system_images_falls_back_to_the_local_listing_when_list_fails(monkeypatch, recorded):
    """`--list` refreshes the remote package index, so it fails offline.

    Returning [] there reads as "this machine has no system images", which is
    the same wrong answer the defect gave. The local `--list_installed` still
    works with no network, so a failed `--list` degrades to it -- and says so,
    so the installed-only answer does not pass for the whole catalogue.

    The failure is a bare non-zero exit: no offline sdkmanager output is
    recorded, so none is invented here.
    """
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")

    attempted: list = []

    def fake_run(cmd, **kwargs):
        attempted.append(cmd)
        if "--list" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=recorded.text("sdkmanager_list_installed"), stderr=""
        )

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    images = creator.list_system_images()

    assert [cmd[-1] for cmd in attempted] == ["--list", "--list_installed"]
    assert images, "an offline listing came back empty instead of local-only"
    assert all(image["installed"] for image in images)
    assert len(creator.warnings) == 1, creator.warnings
    assert "only the installed system images" in creator.warnings[0]


def test_a_listing_sdkmanager_cannot_answer_is_an_error_not_an_empty_list(monkeypatch):
    """X3: both listings failing is "I could not look", not "no images exist".

    The fallback above is for a `--list` that cannot reach the network. When
    the local listing fails as well there is no true short answer left, and
    `[]` at exit 0 was the lie. Exit 127 is the sweep's "not installed" double.
    """
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")
    monkeypatch.setattr(
        emulator_create.subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 127, stdout="", stderr=""),
    )

    with pytest.raises(SdkToolError, match="sdkmanager"):
        creator.list_system_images()


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


def test_create_builds_expected_avdmanager_command(monkeypatch, recorded):
    """The image check runs against the recorded installed set, then avdmanager.

    This test used to make sdkmanager unavailable in order to skip the check --
    and the check DID skip, which is N3: with no sdkmanager the create ran
    avdmanager against an image nobody had verified. The image here is one the
    recorded host has installed, so the check passes for the right reason.
    """
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")
    calls: list[dict] = []
    monkeypatch.setattr(emulator_create.subprocess, "run", _recorded_sdk(recorded, calls))

    success, _message, avd_name = creator.create(
        device_id="pixel_7", api_level=34, name="MyAVD", abi="arm64-v8a", variant="google_apis"
    )

    assert success is True
    assert avd_name == "MyAVD"
    assert calls[0]["cmd"][1:] == ["--list_installed"], "the image check did not run first"
    create_call = calls[-1]
    assert create_call["kwargs"]["check"] is True
    assert create_call["kwargs"]["input"] == "no\n"
    assert create_call["cmd"] == [
        "/fake/avdmanager",
        "create",
        "avd",
        "--name",
        "MyAVD",
        "--package",
        "system-images;android-34;google_apis;arm64-v8a",
        "--device",
        "pixel_7",
    ]


def test_create_refuses_without_sdkmanager_and_runs_nothing(monkeypatch):
    """N3, at the method: no sdkmanager means no create, not an unchecked one."""
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: None)
    calls: list = []
    monkeypatch.setattr(emulator_create.subprocess, "run", lambda cmd, **_kw: calls.append(cmd))

    with pytest.raises(SdkToolError, match=r"sdkmanager not found\. Looked in: PATH"):
        creator.create(device_id="pixel_7", api_level=34, name="MyAVD", abi="arm64-v8a")

    assert calls == [], "a tool was run although the image could not be checked"


@pytest.mark.parametrize(
    ("machine", "abi"),
    [
        ("arm64", "arm64-v8a"),  # macOS on Apple silicon
        ("aarch64", "arm64-v8a"),  # Linux on ARM
        ("x86_64", "x86_64"),  # macOS on Intel, Linux
        ("AMD64", "x86_64"),  # Windows
    ],
)
def test_the_default_abi_follows_the_host(monkeypatch, machine, abi):
    """L11: the default ABI is read from platform.machine() at call time, not fixed."""
    monkeypatch.setattr(platform, "machine", lambda: machine)
    assert emulator_create.host_abi() == abi


def test_the_help_states_this_hosts_default_abi(monkeypatch, capsys):
    """L11: `--help` names the ABI this host will get, not a constant."""
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    with pytest.raises(SystemExit):
        emulator_create.main(["--help"])
    assert "default: arm64-v8a" in capsys.readouterr().out


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


def _manifest_entry(recorded, name: str) -> dict:
    for entry in recorded.manifest["fixtures"]:
        if entry["name"] == name:
            return entry
    pytest.fail(f"no manifest entry for {name!r}")


def _replay(recorded, name: str, cmd: list, kwargs: dict) -> subprocess.CompletedProcess:
    """Answer ``cmd`` the way the recorded tool answered it.

    The fixture file holds the stream the manifest names, and the exit status
    is the manifest's, so a non-zero recording raises under ``check=True``
    exactly as `subprocess.run` would.
    """
    entry = _manifest_entry(recorded, name)
    text = recorded.text(name)
    stdout, stderr = (text, "") if entry.get("stream", "stdout") == "stdout" else ("", text)
    status = entry.get("exit_status", 0)
    if kwargs.get("check") and status != 0:
        raise subprocess.CalledProcessError(status, cmd, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, status, stdout=stdout, stderr=stderr)


# The recordings come from one throwaway AVD: `avdmanager_create` made it from
# this image and device, and `avdmanager_delete` removed it.
RECORDED_AVD = "aes_fixture_tmp"
RECORDED_CREATE = {
    "device_id": "pixel_9",
    "api_level": 34,
    "name": RECORDED_AVD,
    "abi": "arm64-v8a",
    "variant": "google_apis",
}


def _recorded_sdk(recorded, calls: list[dict]):
    """A fake `subprocess.run` answering every SDK-tool call from the corpus."""

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        args = cmd[1:]
        if args == ["list", "device"]:
            return _replay(recorded, "avdmanager_list_device", cmd, kwargs)
        if args == ["--list"]:
            return _replay(recorded, "sdkmanager_list", cmd, kwargs)
        if args == ["--list_installed"]:
            return _replay(recorded, "sdkmanager_list_installed", cmd, kwargs)
        if args[:2] == ["create", "avd"]:
            return _replay(recorded, "avdmanager_create", cmd, kwargs)
        if args[:2] == ["delete", "avd"]:
            missing = args[-1] != RECORDED_AVD
            name = "avdmanager_delete_missing" if missing else "avdmanager_delete"
            return _replay(recorded, name, cmd, kwargs)
        pytest.fail(f"no recording answers {cmd}")

    return fake_run


def test_every_sdk_tool_call_is_bounded(monkeypatch, recorded):
    """Six of the AST sweep's unbounded subprocess calls lived in this file.

    Every call is answered from a recording -- including create() and delete(),
    which used to get an invented `system-images;...` line on stdout because no
    avdmanager create/delete output existed to replay.
    """
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")

    calls: list[dict] = []
    monkeypatch.setattr(emulator_create.subprocess, "run", _recorded_sdk(recorded, calls))

    assert creator.list_device_definitions()
    assert creator.list_system_images()
    assert creator.list_installed_system_images()
    created, message, _name = creator.create(**RECORDED_CREATE)
    assert created, message
    deleted, message = creator.delete(RECORDED_AVD)
    assert deleted, message

    assert len(calls) == 6, "a call site was missed; update this test with it"
    unbounded = [c["cmd"] for c in calls if not c["kwargs"].get("timeout")]
    assert not unbounded, f"unbounded SDK-tool calls: {unbounded}"


def test_create_runs_the_command_that_was_recorded(monkeypatch, recorded):
    """The recording is of the script's own argv, so replaying it means something.

    `avdmanager_create` was captured with the argv in its manifest entry; the
    script, given the same image and device, must build exactly that argv after
    the tool path -- long options, `--device` given -- and feed `no` on stdin.
    With `--device` the custom-hardware prompt never appears (see the fixture's
    description), and the recorded stdout bears that out.
    """
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")
    calls: list[dict] = []
    monkeypatch.setattr(emulator_create.subprocess, "run", _recorded_sdk(recorded, calls))

    success, message, avd_name = creator.create(**RECORDED_CREATE)

    assert success is True, message
    assert avd_name == RECORDED_AVD
    create_call = calls[-1]
    recorded_argv = _manifest_entry(recorded, "avdmanager_create")["argv"]
    assert create_call["cmd"][1:] == recorded_argv[1:]
    assert create_call["kwargs"]["input"] == "no\n"
    assert "custom hardware profile" not in recorded.text("avdmanager_create")


def test_delete_runs_the_command_that_was_recorded(monkeypatch, recorded):
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")
    calls: list[dict] = []
    monkeypatch.setattr(emulator_create.subprocess, "run", _recorded_sdk(recorded, calls))

    success, message = creator.delete(RECORDED_AVD)

    assert success is True, message
    assert calls[0]["cmd"][1:] == _manifest_entry(recorded, "avdmanager_delete")["argv"][1:]
    assert f"AVD '{RECORDED_AVD}' deleted." in recorded.text("avdmanager_delete")


def _delete_missing(monkeypatch, recorded) -> tuple[str, bool, str]:
    """emulator_create.delete() of the recorded missing name: (name, success, message)."""
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")
    calls: list[dict] = []
    monkeypatch.setattr(emulator_create.subprocess, "run", _recorded_sdk(recorded, calls))
    missing = _manifest_entry(recorded, "avdmanager_delete_missing")["argv"][-1]
    success, message = creator.delete(missing)
    return missing, success, message


def test_deleting_an_avd_that_does_not_exist_reports_avdmanagers_reason(monkeypatch, recorded):
    """Exit 1, stdout empty, and avdmanager's reason on stderr -- which reaches the caller.

    What the recording says about the TOOL: the reason is followed by a second
    stderr line reading just `null`. Whether delete() should pass that line on
    is not asserted here -- it should not, and that is pinned separately as N13.
    """
    entry = _manifest_entry(recorded, "avdmanager_delete_missing")
    assert entry["exit_status"] == 1
    assert entry["stdout_bytes"] == 0
    assert (
        recorded.lines("avdmanager_delete_missing")[-1] == "null"
    ), "avdmanager stopped printing it"

    missing, success, message = _delete_missing(monkeypatch, recorded)

    assert success is False
    assert f"There is no Android Virtual Device named '{missing}'." in message, message


def test_a_failed_delete_does_not_hand_on_avdmanagers_stray_null_line(monkeypatch, recorded):
    """The desired behaviour: the reason, without a line that says nothing."""
    _missing, _success, message = _delete_missing(monkeypatch, recorded)
    assert "null" not in [line.strip() for line in message.splitlines()], message


def test_only_the_bare_null_line_goes(recorded):
    """N13's helper on the recording: the reason survives whole, the `null` line does not."""
    text = recorded.text("avdmanager_delete_missing")
    cleaned = without_null_lines(text)

    assert cleaned == recorded.lines("avdmanager_delete_missing")[0]
    assert cleaned.startswith("Error: There is no Android Virtual Device named")
    assert without_null_lines(recorded.lines("avdmanager_delete_missing")[-1]) == ""


def test_a_listing_timeout_is_an_error_not_an_empty_list(monkeypatch):
    """A bounded call raises TimeoutExpired; it reaches the user as a remedy, not raw.

    It used to degrade to `[]`, and `--list-devices` printed nothing at exit 0
    -- "no device definitions" for "avdmanager never answered" (X3).
    """
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")

    def fake_run(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=emulator_create.SDK_TOOL_TIMEOUT)

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    with pytest.raises(SdkToolError) as excinfo:
        creator.list_device_definitions()

    assert str(emulator_create.SDK_TOOL_TIMEOUT) in str(excinfo.value)
    assert excinfo.value.remedy, "the timeout carries no remedy"


def test_create_reports_its_own_timeout(monkeypatch, recorded):
    """avdmanager waiting on stdin used to hang forever; now it is a message."""
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")
    replay = _recorded_sdk(recorded, [])

    def fake_run(cmd, **kwargs):
        if cmd[1:3] == ["create", "avd"]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=emulator_create.SDK_TOOL_TIMEOUT)
        return replay(cmd, **kwargs)

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    success, message, avd_name = creator.create(
        device_id="pixel_7", api_level=34, name="MyAVD", abi="arm64-v8a"
    )

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
