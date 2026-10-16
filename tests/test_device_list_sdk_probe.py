"""N2 -- device_list says it looked for avdmanager where it never looked.

``device_list.py:366`` runs ``["avdmanager", "list", "avd"]`` -- a BARE name,
which ``subprocess`` resolves through PATH and nothing else. When that fails,
the warning at ``:368-372`` reports::

    Looked in: PATH, $ANDROID_HOME/cmdline-tools/latest/bin/avdmanager,
    $ANDROID_HOME/tools/bin/avdmanager

-- the locations ``common.sdk_tools.resolve_sdk_tool`` WOULD probe
(``searched_locations('avdmanager', CMDLINE_TOOLS_SUBDIRS)``), had anything
called it. So with the command-line tools installed exactly where the SDK puts
them and not on PATH -- Android Studio's default -- the listing drops every
AVD's target and ABI and blames a location it never checked.

The spec puts the stub at ``$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager``
(the first location the warning names) and nowhere on PATH. Every answer is
recorded on the emulator-api35 host: ``adb devices -l``, ``emulator
-list-avds``, ``avdmanager list avd``.
"""

from __future__ import annotations

import json

import pytest
from fake_toolchain import FakeToolchain, Rule

SCRIPT = "device_list.py"


@pytest.fixture
def sdk_layout_toolchain(tmp_path, recorded) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    chain.install("adb", [Rule(("devices",), stdout=recorded.text("adb_devices_single"))])
    chain.install("emulator", [Rule(("-list-avds",), stdout=recorded.text("emulator_list_avds"))])
    chain.install(
        "avdmanager",
        [Rule(("list", "avd"), stdout=recorded.text("avdmanager_list_avd"))],
        directory=chain.sdk / "cmdline-tools" / "latest" / "bin",
    )
    return chain


def test_the_stub_sits_where_the_warning_says_it_looked(sdk_layout_toolchain, monkeypatch):
    """Anti-vacuity, unmarked: the shared resolver finds the stub there.

    So the location is one ``resolve_sdk_tool`` -- whose candidates the warning
    prints -- really does probe, and the stub is not also on PATH.
    """
    from common.sdk_tools import CMDLINE_TOOLS_SUBDIRS, resolve_sdk_tool

    chain = sdk_layout_toolchain
    stub = chain.sdk / "cmdline-tools" / "latest" / "bin" / "avdmanager"
    assert not (chain.bin / "avdmanager").exists(), "the stub must not be on PATH"

    chain.apply(monkeypatch)
    assert resolve_sdk_tool("avdmanager", CMDLINE_TOOLS_SUBDIRS) == str(stub)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="N2: device_list.py:366 runs a bare 'avdmanager' and never probes the SDK layout it names",
)
def test_n2_avdmanager_is_found_where_the_warning_says_it_was_looked_for(
    sdk_layout_toolchain, recorded
):
    """N2: an avdmanager in ``cmdline-tools/latest/bin`` is used, not reported missing.

    The listing must carry the avdmanager metadata (the recorded AVD's ABI is
    arm64-v8a) and must not warn that avdmanager could not be found.
    """
    chain = sdk_layout_toolchain
    result = chain.run(SCRIPT, "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)

    avdmanager_warnings = [w for w in payload.get("warnings", []) if "avdmanager" in w]
    assert not avdmanager_warnings, avdmanager_warnings

    names = [line.strip() for line in recorded.lines("emulator_list_avds") if line.strip()]
    avds = {avd["name"]: avd for avd in payload["avds"]}
    assert set(names) <= set(avds), payload["avds"]
    assert all("arm64-v8a" in str(avds[name].get("abi", "")) for name in names), payload["avds"]
    assert chain.calls("avdmanager"), "the SDK-layout avdmanager was never run"
