"""emulator_create against the host it runs on, and the SDK it actually has.

Three findings, all the same shape -- an answer the tools did not give:

* **L11** (``emulator_create.py:476,678``, remedy at ``:737-740``): the default
  ABI is ``x86_64`` and ``platform.machine()`` is never consulted. On an arm64
  Mac -- the machine this skill is developed on, and the first command of the
  documented CI flow (SKILL.md:503) -- every create without ``--abi`` asks for
  an image the host cannot run, and both remedies recommend installing one.

* **N3** (``:511-512``): the "is this image installed?" check runs only
  ``if installed``, so when ``sdkmanager`` is missing the list is empty and
  the check is skipped -- ``avdmanager create`` runs against an image nobody
  verified.

* **X3, the remainder** (``:326,369,393,404,421,432``): with no
  ``sdkmanager``, ``--list-images --json`` answers ``{"system_images": []}``
  at exit 0 (measured); with no ``avdmanager``, ``--list-devices --json``
  answers ``{"devices": []}`` and a create reports the device "not found".

The SDK tools are stubs from ``tests/fake_toolchain.py`` answering with the
recorded ``avdmanager`` / ``sdkmanager`` output of the emulator-api35 host. Two
kinds of "no tool" are covered, because they fail differently: **absent** (not
on PATH, ``ANDROID_HOME`` empty -- the absent-toolchain case the sweep does not
yet have, N5) and **broken** (on PATH, exits 127 -- the sweep's double).

``avdmanager create avd`` is answered with exit 0 and no output: its output is
not recorded on any profile (KNOWN_VIOLATIONS carries that debt for
test_emulator_create.py), and the script does not read it on success, so
silence is the one answer that invents nothing.

NOTE for the Inc 1 implementer of N3: ``tests/test_emulator_create.py:291-295``
relies on the skipped check this file pins as a defect. That file is being
rewritten by another unit this wave and was deliberately not edited here.

L11 runs ``main()`` in-process, because the host has to be patched.
``platform.machine`` is replaced BEFORE the script is imported fresh, so the
fix may read the host however it likes -- per call, at import time, or
through a ``from platform import machine`` binding. Which flag carries the
image to ``avdmanager create`` (``-k`` or ``--package``) is not pinned either:
the image id is found in the call by its own shape.
"""

from __future__ import annotations

import importlib
import json
import platform
import re
import sys

import pytest
from fake_toolchain import FakeToolchain, Rule, assert_refusal

SCRIPT = "emulator_create.py"
DEVICE = "pixel_7"

# An image id as the script prints it: system-images;android-<api>;<tag>;<abi>.
IMAGE_ID = re.compile(r"system-images;android-[\w.]+;[\w-]+;(?P<abi>[\w-]+)")


@pytest.fixture
def toolchain(tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    return chain


def _avdmanager(toolchain: FakeToolchain, recorded) -> None:
    toolchain.install(
        "avdmanager",
        [
            Rule(("list", "device"), stdout=recorded.text("avdmanager_list_device")),
            Rule(("create", "avd"), exit=0),
        ],
    )


def _sdkmanager(toolchain: FakeToolchain, installed: str, everything: str) -> None:
    toolchain.install(
        "sdkmanager",
        [
            Rule(("--list_installed",), stdout=installed),
            Rule(("--list",), stdout=everything),
        ],
    )


def _sdk(toolchain: FakeToolchain, recorded) -> None:
    _avdmanager(toolchain, recorded)
    _sdkmanager(
        toolchain,
        recorded.text("sdkmanager_list_installed"),
        recorded.text("sdkmanager_list"),
    )


def _import_on_host(monkeypatch, name: str, machine: str):
    """Import ``name`` afresh on a host whose ``platform.machine()`` is ``machine``."""
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module(name)


def test_the_host_patch_reaches_an_import_time_binding(monkeypatch, tmp_path):
    """Control, unmarked: the L11 harness patches before the import, not after.

    A module that binds ``from platform import machine`` and reads it at
    import time -- the shape the review said the first harness would miss --
    sees the patched host.
    """
    module = tmp_path / "host_probe_l11.py"
    module.write_text("from platform import machine\nHOST = machine()\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert _import_on_host(monkeypatch, "host_probe_l11", "aarch64").HOST == "aarch64"


def _main(monkeypatch, capsys, toolchain: FakeToolchain, machine: str, *argv: str):
    """Run ``emulator_create.main()`` on a host reporting ``machine``.

    The host is patched BEFORE the script is imported -- the module is dropped
    from ``sys.modules`` and imported fresh -- so a fix that reads the machine
    at import time, or binds ``from platform import machine``, sees the patch
    as well as one that calls ``platform.machine()`` per run (review of PR
    #20: patching after import constrained the fix to the latter).
    ``monkeypatch`` puts the original module back afterwards.
    """
    toolchain.apply(monkeypatch)
    emulator_create = _import_on_host(monkeypatch, "emulator_create", machine)
    monkeypatch.setattr(sys, "argv", ["emulator_create.py", *argv])
    with pytest.raises(SystemExit) as exited:
        emulator_create.main()
    captured = capsys.readouterr()
    return exited.value.code, captured.out + captured.err


def _created_images(toolchain: FakeToolchain) -> list[str]:
    """The system image of every ``avdmanager create avd`` the script ran.

    Read off the stub's argv log by the image id's own shape, whichever flag
    carries it -- ``--package`` and ``-k`` are both avdmanager's spelling.
    """
    images = []
    for call in toolchain.calls("avdmanager"):
        if "create" in call and "avd" in call:
            images += [arg for arg in call if IMAGE_ID.fullmatch(arg)]
    return images


# --- L11 --------------------------------------------------------------------

ARM_HOSTS = ["arm64", "aarch64"]  # macOS spells it one way, Linux the other


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="L11: emulator_create.py:476,678 default --abi is x86_64 on any host",
)
@pytest.mark.parametrize("machine", ARM_HOSTS)
def test_l11_the_default_abi_is_the_hosts(machine, monkeypatch, capsys, toolchain, recorded):
    """L11: with no ``--abi`` an arm64 host creates an arm64-v8a AVD.

    The recorded host has ``system-images;android-34;google_apis;arm64-v8a``
    installed, so ``--api 34`` must simply work there. Today it asks for the
    x86_64 image, finds it missing, and tells the user to install it.
    """
    _sdk(toolchain, recorded)
    code, output = _main(
        monkeypatch, capsys, toolchain, machine, "--device", DEVICE, "--api", "34", "--json"
    )

    created = _created_images(toolchain)
    assert len(created) == 1, f"expected one AVD created, got {created}:\n{output}"
    assert "android-34;" in created[0] and created[0].endswith(";arm64-v8a"), created
    assert code == 0, output


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="L11: emulator_create.py:511-519 remedy names an x86_64 image on arm64",
)
@pytest.mark.parametrize("machine", ARM_HOSTS)
def test_l11_the_not_installed_remedy_names_an_image_the_host_can_run(
    machine, monkeypatch, capsys, toolchain, recorded
):
    """L11: an image that is not installed is named for THIS host's ABI.

    API 30 has no system image on the recorded host, so the create refuses --
    correctly -- and its "Install with: sdkmanager '...'" must name an image an
    arm64 machine can boot.
    """
    _sdk(toolchain, recorded)
    code, output = _main(
        monkeypatch, capsys, toolchain, machine, "--device", DEVICE, "--api", "30", "--json"
    )

    assert code != 0, output
    abis = {m["abi"] for m in IMAGE_ID.finditer(output) if "android-30;" in m[0]}
    assert abis == {"arm64-v8a"}, f"the remedy for API 30 names {sorted(abis)}:\n{output}"
    assert not _created_images(toolchain)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="L11: emulator_create.py:737-740 API-level remedy hard-codes an x86_64 image",
)
def test_l11_the_no_api_level_remedy_names_an_image_the_host_can_run(
    monkeypatch, capsys, toolchain, recorded
):
    """L11: ``:737-740``, the remedy when no system image is installed at all.

    The installed listing is the recorded one with its system-image rows
    removed -- sdkmanager ran and reported none. The suggested image must be
    one an arm64 host can run.
    """
    _avdmanager(toolchain, recorded)
    no_images = "\n".join(
        line for line in recorded.lines("sdkmanager_list_installed") if "system-images" not in line
    )
    _sdkmanager(toolchain, no_images, recorded.text("sdkmanager_list"))
    code, output = _main(monkeypatch, capsys, toolchain, "arm64", "--device", DEVICE, "--json")

    assert code != 0, output
    abis = {m["abi"] for m in IMAGE_ID.finditer(output)}
    assert abis, f"the remedy names no system image at all:\n{output}"
    assert abis == {"arm64-v8a"}, f"the remedy recommends {sorted(abis)} to an arm64 host"


def test_l11_an_x86_64_host_still_defaults_to_x86_64(monkeypatch, capsys, toolchain, recorded):
    """Control, unmarked: the fix follows the host; it does not hard-code arm64.

    Green today (x86_64 is the constant) and must stay green after: CI's
    ubuntu runners are x86_64.
    """
    _sdk(toolchain, recorded)
    code, output = _main(
        monkeypatch, capsys, toolchain, "x86_64", "--device", DEVICE, "--api", "30", "--json"
    )

    assert code != 0, output
    abis = {m["abi"] for m in IMAGE_ID.finditer(output) if "android-30;" in m[0]}
    assert abis == {"x86_64"}, output


def test_the_recorded_sdk_is_what_these_specs_assume(recorded):
    """Anti-vacuity, unmarked: API 34 arm64 is installed and API 30 is not."""
    installed = recorded.text("sdkmanager_list_installed")
    assert "system-images/android-34/google_apis/arm64-v8a" in installed
    assert "system-images/android-30/" not in installed
    assert f'"{DEVICE}"' in recorded.text("avdmanager_list_device")


# --- N3 and the X3 remainder: a missing tool is an error, not an empty list --

NO_TOOL = ["absent", "broken"]


def _without(toolchain: FakeToolchain, tool: str, how: str) -> None:
    """Leave ``tool`` absent (nowhere), or install it broken (exit 127)."""
    if how == "broken":
        toolchain.install_broken(tool)


# --json, or text. Every refusal below is checked in both, against the shared
# boundary's contract (fake_toolchain.assert_refusal): one refusal object on
# stdout, or `Error:`/`Remedy:` on stderr with no JSON on stdout.
OUTPUT = {"json": ("--json",), "text": ()}


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="N3: emulator_create.py:511-512 skips the image check when sdkmanager is missing",
)
@pytest.mark.parametrize("output", list(OUTPUT))
@pytest.mark.parametrize("how", NO_TOOL)
def test_n3_create_does_not_skip_the_image_check_without_sdkmanager(
    how, output, toolchain, recorded
):
    """N3: no sdkmanager -> no installed list -> ``if installed`` skips the check.

    ``avdmanager create`` then runs against an image nobody verified. Without
    sdkmanager the create cannot know the image is there, so it must refuse,
    naming sdkmanager -- and must not have created anything.
    """
    _avdmanager(toolchain, recorded)
    _without(toolchain, "sdkmanager", how)
    result = toolchain.run(
        SCRIPT, "--device", DEVICE, "--api", "34", "--abi", "arm64-v8a", *OUTPUT[output]
    )

    assert not _created_images(toolchain), "an AVD was created with the image check skipped"
    assert_refusal(result, json_mode=output == "json", naming=("sdkmanager",), status=1)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="X3: emulator_create.py:393,404,421,432 answer {'system_images': []} at exit 0 with no sdkmanager",
)
@pytest.mark.parametrize("output", list(OUTPUT))
@pytest.mark.parametrize("how", NO_TOOL)
def test_x3_list_images_without_sdkmanager_is_an_error(how, output, toolchain):
    """X3 remainder: ``--list-images`` with no sdkmanager.

    Measured: ``{"system_images": []}``, exit 0 -- "this host has no images"
    and "I could not ask" in one answer.
    """
    _without(toolchain, "sdkmanager", how)
    result = toolchain.run(SCRIPT, "--list-images", *OUTPUT[output])

    assert_refusal(result, json_mode=output == "json", naming=("sdkmanager",), status=1)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="X3: emulator_create.py:326,369 answer {'devices': []} at exit 0 with no avdmanager",
)
@pytest.mark.parametrize("output", list(OUTPUT))
@pytest.mark.parametrize("how", NO_TOOL)
def test_x3_list_devices_without_avdmanager_is_an_error(how, output, toolchain):
    """X3 remainder: ``--list-devices`` with no avdmanager."""
    _without(toolchain, "avdmanager", how)
    result = toolchain.run(SCRIPT, "--list-devices", *OUTPUT[output])

    assert_refusal(result, json_mode=output == "json", naming=("avdmanager",), status=1)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="X3: emulator_create.py:326 -- no avdmanager reads as 'device not found'",
)
@pytest.mark.parametrize("output", list(OUTPUT))
@pytest.mark.parametrize("how", NO_TOOL)
def test_x3_create_without_avdmanager_names_the_missing_tool(how, output, toolchain, recorded):
    """X3 remainder: a create with no avdmanager blames the device name.

    The empty device-definition list makes every ``--device`` "not found",
    with a suggestion to run ``--list-devices`` -- which then answers ``[]``.
    The missing tool is the thing to name.
    """
    _without(toolchain, "avdmanager", how)
    _sdkmanager(
        toolchain,
        recorded.text("sdkmanager_list_installed"),
        recorded.text("sdkmanager_list"),
    )
    result = toolchain.run(SCRIPT, "--device", DEVICE, "--api", "34", *OUTPUT[output])

    assert_refusal(result, json_mode=output == "json", naming=("avdmanager",), status=1)


def test_a_broken_tool_is_really_reached(toolchain):
    """Anti-vacuity, unmarked: in the "broken" case the stub IS what was run."""
    toolchain.install_broken("sdkmanager")
    toolchain.run(SCRIPT, "--list-images", "--json")
    assert toolchain.calls("sdkmanager"), "the broken sdkmanager was never invoked"


def test_create_output_is_parsed_as_json_when_it_succeeds(toolchain, recorded):
    """Control, unmarked: with the whole recorded SDK present, a create succeeds.

    ``--abi`` is explicit, so this holds on any host today and after L11. It
    is the baseline the N3/X3 specs differ from by exactly one missing tool.
    """
    _sdk(toolchain, recorded)
    result = toolchain.run(
        SCRIPT, "--device", DEVICE, "--api", "34", "--abi", "arm64-v8a", "--json"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["success"] is True
    assert _created_images(toolchain) == ["system-images;android-34;google_apis;arm64-v8a"]
