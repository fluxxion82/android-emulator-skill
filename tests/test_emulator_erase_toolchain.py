"""N1 -- emulator_erase cannot tell "no AVDs" from "could not look".

``emulator_erase.py:92-93``::

    avd_home = self.get_avd_home()
    if not avd_home.exists():
        return []

so on a host whose AVD home is not where the script looked -- or a host with
no SDK at all -- ``--list --json`` answers ``{"avds": []}`` and ``--all``
answers "No AVDs found", both at exit 0. SKILL.md:716-719 claims the opposite:
that emulator_erase routes through ``common/sdk_tools.py`` and "gives the
remedy instead of returning an empty list". It does not consult any SDK tool;
it reads one directory and treats that directory's absence as an answer.

The scenario is the absent-toolchain one the exit-code sweep lacks (N5): no
``emulator``/``avdmanager``/``sdkmanager`` reachable ("absent"), or each on
PATH and exiting 127 ("broken", the sweep's double); ``ANDROID_HOME`` empty;
and the AVD home ``ANDROID_SDK_HOME`` points at holds no ``.android/avd``.
Whether Inc 1 fixes this by listing through ``avdmanager`` (as
emulator_delete does) or by refusing a missing AVD home, nothing here can be
answered with an empty list -- which is all these specs assert.
"""

from __future__ import annotations

import json

import pytest
from fake_toolchain import ANDROID_TOOLS, FakeToolchain, assert_refusal

SCRIPT = "emulator_erase.py"

MODES = [
    ("--list", "--json"),
    ("--list",),
    ("--all", "--json"),
    ("--all",),
]


@pytest.fixture(params=["absent", "broken"])
def no_sdk(request, tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    if request.param == "broken":
        for tool in ANDROID_TOOLS:
            chain.install_broken(tool)
    return chain


def _answered_empty(stdout: str) -> bool:
    """Whether stdout is an empty-but-successful listing, in either output mode."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return "No AVDs found" in stdout
    if not isinstance(payload, dict) or "error" in payload:
        return False
    return payload.get("avds") == [] or payload.get("total") == 0


def test_the_avd_home_really_is_missing(no_sdk):
    """Anti-vacuity, unmarked: there is no AVD home for the script to read."""
    assert not (no_sdk.home / ".android" / "avd").exists()
    assert no_sdk.env()["ANDROID_SDK_HOME"] == str(no_sdk.home)


# What the refusal may name: the tool it could not run, or the AVD home it
# could not find (by path, or by the variables that relocate it). Any one will
# do -- the fix chooses whether to list through avdmanager, as emulator_delete
# does, or to refuse a missing AVD home; it may not answer with nothing.
N1_NAMES = (
    "avdmanager",
    "emulator",
    ".android/avd",
    "avd home",
    "android_avd_home",
    "android_sdk_home",
)


@pytest.mark.parametrize("argv", MODES, ids=" ".join)
def test_n1_no_sdk_and_no_avd_home_is_an_error_not_an_empty_list(argv, no_sdk):
    """N1: with nothing to look with and nowhere to look, refuse, and say what.

    A refusal in the shared boundary's shape (exit 1; one refusal object under
    ``--json``, ``Error:``/``Remedy:`` otherwise) whose error names the AVD
    home or the tool -- never the empty listing that means "this host has no
    AVDs".
    """
    result = no_sdk.run(SCRIPT, *argv)

    assert not _answered_empty(
        result.stdout
    ), f"{' '.join(argv)} answered an empty listing:\n{result.stdout}"
    assert_refusal(result, json_mode="--json" in argv, naming=N1_NAMES, status=1)
