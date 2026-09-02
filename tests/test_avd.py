"""The unified AVD entry point: routing, pass-through, and what it must not break.

``avd.py`` is a router, not a reimplementation, for the same reason ``logs.py``
is. That choice is what makes the consolidation non-breaking, and it is also
what these tests have to hold it to:

- the verb selects a module and hands over the argument vector **verbatim**, so
  no second copy of any flag set exists here to drift out of step -- and in
  particular no second opinion about which flag means "yes, really destroy it"
  (``delete`` takes ``--yes``, ``reset`` takes ``--force``);
- the delegate's exit status reaches the shell unchanged;
- every script it routes to is still standalone-executable with the flags it
  always had, pinned by name so a removal fails loudly.

One hazard is specific to this router and gets its own tests: ``reset`` and
``delete`` both destroy something, and they destroy different things. A
"did you mean" that resolved a near-miss between them into a single answer
would be a wrong answer that costs an AVD, so the suggestion logic is required
to ask rather than guess.

Parser assertions read ``tests/fixtures/recorded/`` through the ``recorded``
fixture. Nothing here inlines invented tool output: the three defects that made
AVD creation impossible until 19325db are pinned against real ``avdmanager`` and
``sdkmanager`` captures, or against the argument vector actually built.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import avd
import device_list
import emulator_create
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"

# The seven scripts this router covers. Listed here as well as in avd.ROUTES so
# that dropping a route fails rather than silently shrinking the surface.
LIFECYCLE_SCRIPTS = (
    "device_list.py",
    "emulator_selector.py",
    "emulator_create.py",
    "emulator_boot.py",
    "emulator_shutdown.py",
    "emulator_erase.py",
    "emulator_delete.py",
)


def _run_help(script: str) -> str:
    """`--help` for one script, stdout and stderr together."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Routing: one verb per question, and the argv passes through untouched.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", avd.ROUTES, ids=lambda r: r.name)
def test_every_route_names_a_module_that_exists_and_exposes_main(route):
    """A route pointing at nothing would fail only at the moment of use."""
    module = __import__(route.module)
    assert callable(module.main), f"{route.module}.main is not callable"
    assert (SCRIPTS_DIR / route.script).exists(), f"{route.script} is missing"


def test_every_lifecycle_script_has_exactly_one_verb():
    """The consolidation is only useful if it covers the set it claims to."""
    routed = [route.script for route in avd.ROUTES]
    assert sorted(routed) == sorted(LIFECYCLE_SCRIPTS)
    assert len(set(routed)) == len(routed), "two verbs delegate to the same script"


@pytest.mark.parametrize("route", avd.ROUTES, ids=lambda r: r.name)
def test_verb_dispatches_to_its_own_module(route, monkeypatch):
    """Every verb, not just a sampled one: a mis-wired route is a wrong tool run."""
    seen: dict = {}
    module = __import__(route.module)
    monkeypatch.setattr(module, "main", lambda: seen.setdefault("argv", list(sys.argv)) and 0)

    assert avd.main([route.name, "--json"]) == 0
    assert seen["argv"][1:] == ["--json"]
    assert seen["argv"][0] == f"avd.py {route.name}"


def test_argv_passes_through_verbatim_including_flags_the_router_also_defines(monkeypatch):
    """``--json`` belongs to the verb once a verb has been named.

    The router defines ``--json`` too. If it parsed the tail itself, ``avd.py
    list --json`` would print a routing table instead of the devices -- a silent
    wrong answer, which is this repo's characteristic failure. Dispatch
    therefore happens before the router's own parser ever runs.
    """
    seen: dict = {}
    monkeypatch.setattr(device_list, "main", lambda: seen.setdefault("argv", list(sys.argv)) and 0)
    avd.main(["list", "--json", "--get-details", "--device-type", "Pixel"])
    assert seen["argv"][1:] == ["--json", "--get-details", "--device-type", "Pixel"]


def test_the_confirmation_flags_are_passed_through_not_normalised(monkeypatch):
    """`delete` takes `--yes`; `reset` takes `--force`. The router owns neither.

    Normalising them here would mean a second copy of two destructive scripts'
    flag sets, and the copy would be the thing that decides whether a wipe is
    confirmed. Pass-through means the delegate's own answer is the only answer.
    """
    import emulator_delete
    import emulator_erase

    seen: dict = {}
    monkeypatch.setattr(
        emulator_delete, "main", lambda: seen.setdefault("delete", list(sys.argv[1:])) and 0
    )
    monkeypatch.setattr(
        emulator_erase, "main", lambda: seen.setdefault("reset", list(sys.argv[1:])) and 0
    )

    avd.main(["delete", "--name", "aes_probe", "--yes"])
    avd.main(["reset", "--name", "aes_probe", "--force"])

    assert seen["delete"] == ["--name", "aes_probe", "--yes"]
    assert seen["reset"] == ["--name", "aes_probe", "--force"]


def test_prog_name_reflects_how_the_agent_invoked_it(monkeypatch):
    """The delegate's own errors should name the command that was typed."""
    seen: dict = {}
    import emulator_boot

    monkeypatch.setattr(emulator_boot, "main", lambda: seen.setdefault("prog", sys.argv[0]))
    avd.main(["start", "--avd", "Pixel_9"])
    assert seen["prog"] == "avd.py start"


def test_sys_argv_is_restored_after_dispatch(monkeypatch):
    monkeypatch.setattr(device_list, "main", lambda: 0)
    before = list(sys.argv)
    avd.main(["list", "--json"])
    assert sys.argv == before


# ---------------------------------------------------------------------------
# Exit status. A swallowed failure here means "the wipe worked" when it did not.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ending", "expected"),
    [
        ("return-zero", 0),
        ("return-two", 2),
        ("return-none", 0),
        ("exit-zero", 0),
        ("exit-one", 1),
        ("exit-message", 1),
    ],
)
def test_exit_status_survives_the_router(monkeypatch, ending, expected, capsys):
    """A router that swallowed a non-zero status would report failure as success."""
    endings = {
        "return-zero": lambda: 0,
        "return-two": lambda: 2,
        "return-none": lambda: None,
        "exit-zero": lambda: sys.exit(0),
        "exit-one": lambda: sys.exit(1),
        "exit-message": lambda: sys.exit("device is offline"),
    }
    import emulator_shutdown

    monkeypatch.setattr(emulator_shutdown, "main", endings[ending])
    assert avd.main(["stop", "--serial", "emulator-5554"]) == expected
    if ending == "exit-message":
        assert "device is offline" in capsys.readouterr().err


@pytest.mark.parametrize("verb", ["reset", "delete"])
def test_a_failed_destructive_run_still_exits_non_zero(monkeypatch, verb):
    """A failure reported as success matters most for the two verbs that destroy."""
    module = __import__(avd.ROUTES_BY_NAME[verb].module)
    monkeypatch.setattr(module, "main", lambda: sys.exit(1))
    assert avd.main([verb, "--name", "aes_probe"]) == 1


# ---------------------------------------------------------------------------
# The router's own surface.
# ---------------------------------------------------------------------------


def test_help_lists_every_verb_with_the_question_it_answers():
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "avd.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    for route in avd.ROUTES:
        assert route.name in result.stdout
        assert route.question in result.stdout


def test_help_says_which_verbs_can_lose_work_and_what_each_loses():
    """The whole reason for two verbs is that they destroy different things."""
    text = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "avd.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).stdout
    assert "DESTRUCTIVE" in text
    assert "reset and delete are not synonyms" in text
    destructive = {route.name for route in avd.ROUTES if route.destructive}
    assert destructive == {"reset", "delete"}


def test_json_emits_the_routing_table():
    table = avd.routes_json()
    assert {entry["verb"] for entry in table["routes"]} == {r.name for r in avd.ROUTES}
    for entry in table["routes"]:
        assert entry["question"] and entry["effect"] and entry["delegates_to"]
    destructive = {entry["verb"] for entry in table["routes"] if entry["destructive"]}
    assert destructive == {"reset", "delete"}


def test_bare_invocation_is_a_usage_error_that_still_explains_the_verbs(capsys):
    assert avd.main([]) == 2
    err = capsys.readouterr().err
    assert all(route.name in err for route in avd.ROUTES)


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("boot", "start"),
        ("shutdown", "stop"),
        ("devices", "list"),
        ("ls", "list"),
        ("suggest", "pick"),
        ("crete", "create"),
        ("emulator_boot", "start"),
    ],
)
def test_unknown_verb_suggests_the_nearest_one(typed, expected, capsys):
    assert avd.main([typed]) == 2
    assert f"avd.py {expected}" in capsys.readouterr().err.rsplit("Did you mean", 1)[-1]


@pytest.mark.parametrize("typed", ["erase", "wipe", "clean", "clear"])
def test_a_word_meaning_destroy_is_never_resolved_for_the_caller(typed, capsys):
    """`erase` is the old script name for `reset` AND ordinary English for `delete`.

    difflib scores it closest to `reset` and would answer confidently. The
    answer is a coin flip between "wipe the data" and "remove the AVD", so the
    router names both and lets the caller choose.
    """
    assert avd.main([typed]) == 2
    hint = capsys.readouterr().err.rsplit("Did you mean", 1)[-1]
    assert "avd.py reset" in hint and "avd.py delete" in hint


@pytest.mark.parametrize(
    "typed",
    ["delet", "deleet", "reste", "resett", "device", "devices", "avd", "dele"],
)
def test_a_fuzzy_guess_never_names_one_destructive_verb_on_its_own(typed):
    """The failure this guards is `avd.py devices` being answered with `delete`.

    difflib really does rank `delete` first for `devices` -- same prefix,
    similar length -- and an agent that trusts a "did you mean" would then
    remove an AVD while trying to list them. Any suggestion that lands on a
    destructive verb must present both of them, so the caller has to read.
    """
    hint = avd.suggest_verb(typed)
    named = {name for name in avd.ROUTES_BY_NAME if f"avd.py {name}" in hint}
    destructive = {name for name in named if avd.ROUTES_BY_NAME[name].destructive}
    both_or_neither = destructive in ({"reset", "delete"}, set())
    assert both_or_neither, f"{typed!r} was answered with one destructive verb: {hint!r}"


def test_verb_help_reaches_the_delegate_not_the_router():
    """`avd.py delete --help` must document delete's flags, not the router's."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "avd.py"), "delete", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "avd.py delete" in result.stdout
    assert "--yes" in result.stdout and "--old" in result.stdout


# ---------------------------------------------------------------------------
# Nothing was taken away. The seven scripts are a published surface.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", LIFECYCLE_SCRIPTS)
def test_the_delegated_scripts_remain_standalone(script):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage" in (result.stdout + result.stderr).lower()


@pytest.mark.parametrize(
    ("script", "flags"),
    [
        ("device_list.py", ["--get-details", "--device-type", "--name", "--json"]),
        (
            "emulator_selector.py",
            ["--suggest", "--list", "--boot", "--headless", "--count", "--json", "--verbose"],
        ),
        (
            "emulator_create.py",
            [
                "--list-devices",
                "--list-images",
                "--device",
                "--api",
                "--name",
                "--abi",
                "--variant",
                "--json",
            ],
        ),
        (
            "emulator_boot.py",
            ["--avd", "--all", "--wait-ready", "--timeout", "--headless", "--list-avds", "--json"],
        ),
        (
            "emulator_shutdown.py",
            ["--serial", "--name", "--verify", "--no-verify", "--timeout", "--all", "--json"],
        ),
        (
            "emulator_erase.py",
            [
                "--name",
                "--list",
                "--all",
                "--force",
                "--verify",
                "--timeout",
                "--json",
                "--verbose",
            ],
        ),
        (
            "emulator_delete.py",
            ["--name", "--all", "--old", "--yes", "--list", "--json", "--verbose"],
        ),
    ],
)
def test_no_documented_flag_disappeared(script, flags):
    """The published flag set is the compatibility contract, so pin it by name."""
    text = _run_help(script)
    missing = [flag for flag in flags if flag not in text]
    assert not missing, f"{script} no longer offers {missing}"


def test_the_two_destructive_scripts_still_disagree_about_their_confirm_flag():
    """Pinned as it is, not as it should be.

    `emulator_delete.py` confirms with `--yes` and `emulator_erase.py` with
    `--force`. That is a wart, and the router does not fix it -- fixing it would
    change a published surface. It is pinned so that if someone does unify them
    later, this test says so out loud rather than the router quietly gaining a
    flag it does not own.
    """
    assert "--yes" in _run_help("emulator_delete.py")
    assert "--force" in _run_help("emulator_erase.py")
    assert "--force" not in _run_help("emulator_delete.py")


# ---------------------------------------------------------------------------
# The defects this consolidation must not undo. All three made AVD creation
# impossible until 19325db, and all three were imagined tool output.
# ---------------------------------------------------------------------------


def _sdk_double(monkeypatch, recorded):
    """Point emulator_create's SDK tools at recorded output; record the argv.

    Returns:
        The list of commands the module tried to run, so what it *sent* can be
        asserted as well as what it parsed.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "list" in cmd and "device" in cmd:
            text = recorded.text("avdmanager_list_device")
        elif "--list_installed" in cmd:
            text = recorded.text("sdkmanager_list_installed")
        elif "--list" in cmd:
            # Both are recorded now, and they are not interchangeable: `--list`
            # adds an `Available packages:` section after the installed one, and
            # that section is the only place the non-integer API tokens live.
            # Serving the installed recording for both would hide whether the
            # section boundary is honoured at all.
            text = recorded.text("sdkmanager_list")
        else:
            text = ""
        return subprocess.CompletedProcess(cmd, 0, text, "")

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)
    monkeypatch.setattr(
        emulator_create.EmulatorCreator, "get_sdkmanager_path", lambda self: "/fake/sdkmanager"
    )
    monkeypatch.setattr(
        emulator_create.EmulatorCreator, "get_avdmanager_path", lambda self: "/fake/avdmanager"
    )
    return calls


def test_routed_create_sends_the_resolved_device_id_not_the_whole_id_line(monkeypatch, recorded):
    """`id: 53 or "pixel_9"` is two identifiers for one device.

    Keeping the tail whole made `avdmanager` answer `No device found matching
    --device 53 or "pixel_9"`, echoing back what it had been handed. Asserted
    on the argv the create path actually builds, after routing, so the router
    cannot re-introduce it by re-parsing `--device` itself.
    """
    calls = _sdk_double(monkeypatch, recorded)

    assert (
        avd.main(
            [
                "create",
                "--device",
                "Pixel 9",
                "--api",
                "34",
                "--variant",
                "google_apis",
                "--abi",
                "arm64-v8a",
                "--name",
                "aes_probe_unit",
                "--json",
            ]
        )
        == 0
    )

    create_cmd = next(cmd for cmd in calls if "create" in cmd and "avd" in cmd)
    assert create_cmd[create_cmd.index("--device") + 1] == "pixel_9"
    assert not any(" or " in token for token in create_cmd), create_cmd


def test_routed_create_infers_the_api_level_from_what_is_installed(monkeypatch, recorded, capsys):
    """`--api` inference had nothing to infer from while the parser matched nothing.

    35 is the highest API level in the recorded `--list_installed` capture, so
    the inference is asserted against the machine's real package set rather
    than against a number someone expected to see.
    """
    _sdk_double(monkeypatch, recorded)

    assert (
        avd.main(
            [
                "create",
                "--device",
                "Pixel 9",
                "--variant",
                "google_apis_playstore_ps16k",
                "--abi",
                "arm64-v8a",
                "--name",
                "aes_probe_unit",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["api_level"] == 35, payload


def test_routed_create_refuses_an_image_it_cannot_see_and_names_the_ones_it_can(
    monkeypatch, recorded, capsys
):
    """The old check asked `sdkmanager --list` whether `system-images;...` appeared.

    It never does -- the listing uses slashes -- so every image looked missing
    and the error told the user to install what they already had. The fix is
    only worth anything if the refusal now carries the installed set, which is
    what makes it actionable.
    """
    _sdk_double(monkeypatch, recorded)

    assert (
        avd.main(
            [
                "create",
                "--device",
                "Pixel 9",
                "--api",
                "34",
                "--abi",
                "x86_64",
                "--name",
                "aes_probe_unit",
                "--json",
            ]
        )
        == 1
    )
    message = json.loads(capsys.readouterr().out)["message"]
    assert "system-images;android-34;google_apis;arm64-v8a" in message, message


def test_no_recorded_sdkmanager_output_uses_the_pipe_delimited_id_format(recorded):
    """The ground truth behind the two tests above, stated once.

    `system-images;` and `|` are what the original parsers required. Neither
    appears anywhere in what sdkmanager actually printed on a real machine.
    """
    for name in ("sdkmanager_list_installed", "sdkmanager_list"):
        text = recorded.text(name)
        assert "system-images/" in text, name
        assert "system-images;" not in text, name
        assert "|" not in text, name


def test_routed_list_images_reads_the_format_sdkmanager_actually_prints(
    monkeypatch, recorded, capsys
):
    """AVD-LIST-IMAGES: `--list-images` was advertised in `--help` and answered nothing.

    It looked for `system-images;<id> | <rev> | <desc>` in `sdkmanager --list`
    -- the same invented format 19325db removed from
    `list_installed_system_images`, left behind in the sibling method that
    commit did not touch. Both now go through one parser, so the two cannot
    disagree about what sdkmanager prints again.
    """
    calls = _sdk_double(monkeypatch, recorded)

    assert avd.main(["create", "--list-images", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    images = payload["system_images"]

    assert images, "no images parsed from real sdkmanager output"
    assert [cmd for cmd in calls if "--list" in cmd], calls
    # The listing prints paths; the id an agent has to hand back to sdkmanager
    # or avdmanager uses semicolons.
    assert all(image["id"].startswith("system-images;") for image in images)
    # `--list` is the only listing that answers "and which do I already have".
    assert any(image["installed"] for image in images)
    assert any(not image["installed"] for image in images)


def test_routed_list_reads_avdmanagers_real_block_shape(recorded):
    """`avdmanager list avd` indents inconsistently and folds Tag/ABI into a continuation.

    `Name:` has four leading spaces and `Device:` two, and `Tag/ABI:` rides on
    the `Based on:` continuation line rather than having a key of its own -- so
    a parser that split on the first colon of a consistently-indented block
    would lose the ABI entirely. Asserted against the recording, not against a
    tidied-up version of it.
    """
    metadata = device_list.parse_avdmanager_avds(recorded.text("avdmanager_list_avd"))

    assert metadata, "no AVD metadata parsed from real avdmanager output"
    entry = metadata["Pixel_9"]
    assert entry["device"] == "pixel_9 (Google)"
    assert entry["abi"] == "page_size_16kb/arm64-v8a"
    assert entry["based_on"].startswith("Android 15.0")
    assert "Tag/ABI" not in entry["based_on"], "the continuation line was not split"


def test_routed_pick_and_list_do_not_share_a_source_of_truth(recorded):
    """Documented, not fixed: the seven disagree about what "the AVDs" means.

    `device_list` (and so `avd.py list`) enriches `emulator -list-avds` with
    `avdmanager list avd` metadata; `emulator_delete` asks `avdmanager list avd
    -c`; `emulator_erase` scans the AVD home for `*.avd` directories. This test
    pins the one thing the router relies on -- that the *name* is the join key
    across those sources -- so that a change to either listing format shows up
    here rather than as an AVD silently missing from a listing.
    """
    metadata = device_list.parse_avdmanager_avds(recorded.text("avdmanager_list_avd"))
    merged = device_list.merge_avds(
        [{"kind": "avd", "name": name, "online": False} for name in metadata], metadata
    )
    assert [record["name"] for record in merged] == list(metadata)
    assert all(record.get("abi") for record in merged), "metadata did not join by name"


# ---------------------------------------------------------------------------
# Device-backed. Semantic floors: did the agent get a usable answer, and did
# the round trip leave the machine as it found it.
# ---------------------------------------------------------------------------


def _defined_avd_names() -> list[str]:
    """AVD names from `avdmanager list avd -c`, the listing that works everywhere.

    Deliberately not `emulator -list-avds`: on a host whose PATH carries the SDK
    root rather than `<sdk>/emulator`, the name `emulator` resolves to a
    directory and that listing returns nothing.
    """
    import emulator_delete

    return sorted(emulator_delete.EmulatorDeleter().list_avds())


@pytest.mark.emulator
def test_routing_list_gives_the_same_answer_as_calling_the_script(emulator_only_device):
    """The router is transparent or it is a liability."""
    direct = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "device_list.py"), "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    routed = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "avd.py"), "list", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert direct.returncode == routed.returncode == 0, direct.stderr + routed.stderr
    assert json.loads(direct.stdout) == json.loads(routed.stdout)
    assert emulator_only_device in {
        device["serial"] for device in json.loads(routed.stdout)["devices"]
    }


@pytest.mark.emulator
def test_create_then_delete_leaves_the_avd_store_as_it_found_it(emulator_only_device, capsys):
    """The lifecycle's one genuinely destructive round trip, on a throwaway only.

    Named with the pid so a stale probe from an interrupted run is never the
    target, and the AVD list is compared before and after: the point is not only
    that the probe was created and removed, but that nothing else moved.
    """
    creator = emulator_create.EmulatorCreator()
    if not creator.get_avdmanager_path():
        pytest.skip("avdmanager is not installed; nothing to create with")
    images = creator.list_installed_system_images()
    if not images:
        pytest.skip("no system image installed; no AVD can be created")
    if not creator.resolve_device("pixel_7")[0]:
        pytest.skip("no pixel_7 device definition on this SDK")

    image = max(images, key=lambda entry: entry["api_level"])
    probe = f"aes_probe_avd_router_{os.getpid()}"

    before = _defined_avd_names()
    assert probe not in before, f"{probe} already exists; refusing to touch it"

    created = avd.main(
        [
            "create",
            "--device",
            "pixel_7",
            "--api",
            str(image["api_level"]),
            "--variant",
            image["variant"],
            "--abi",
            image["abi"],
            "--name",
            probe,
            "--json",
        ]
    )
    capsys.readouterr()
    assert created == 0, f"avd.py create failed for {probe}"

    try:
        assert set(_defined_avd_names()) - set(before) == {probe}
    finally:
        removed = avd.main(["delete", "--name", probe, "--yes", "--json"])
        capsys.readouterr()

    assert removed == 0, f"avd.py delete failed for {probe}; it is still on disk"
    assert _defined_avd_names() == before, "the round trip did not restore the AVD store"
