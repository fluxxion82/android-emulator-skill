"""A live test that changes a device runs on an emulator, or not at all.

`adb devices` lists a physical phone before `emulator-5554`. Two live tests
reached past that: one took ``live_device`` (which falls back to a phone when
it is the only thing attached) and then force-stopped, launched and crashed an
app; the other listed the devices itself and drove the first one that had the
fixture app installed, which a developer's handset can.

Nothing here touches a device. The "phone" is a made-up serial in the shape of
a USB one, substituted into the recorded two-device listing; no real device's
serial appears in this file.

The source check below is a ratchet over the spellings these tests use today
(the fixtures they take, the helpers they call), not a proof that no live
test can reach a phone: a new way of picking a device is a new case here.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest
from conftest import booted_serials, emulator_serials, emulator_with_package

TESTS = Path(__file__).resolve().parent
PHONE = "98ZZ1FFBA00000"
APP = "com.example.composefixture"

# The live tests that may run on a phone when a phone is all there is: each
# only READS (a getprop, a hierarchy dump, a logcat dump, `wm size`, an echo,
# the crash buffer). Anything that launches, stops, taps, types, grants or
# writes takes ``emulator_only_device`` instead. Adding a name here is a claim
# that the test changes nothing on the device; check it before making it.
READS_ONLY: frozenset[str] = frozenset(
    {
        "conftest.py::emulator_only_device",
        "test_crash_triage.py::test_crash_buffer_triage_on_a_live_device",
        "test_emulator_smoke.py::test_device_reports_an_api_level",
        "test_emulator_smoke.py::test_effective_display_geometry_is_reported",
        "test_emulator_smoke.py::test_hierarchy_dump_returns_parseable_xml",
        "test_emulator_smoke.py::test_logcat_produces_lines_the_monitor_can_parse",
        "test_emulator_smoke.py::test_quoted_argument_survives_the_device_shell",
        "test_emulator_smoke.py::test_screen_mapper_returns_a_usable_answer",
    }
)

# The places outside conftest that run `adb devices` themselves, and why none
# of them can end up driving a phone.
LISTS_DEVICES: dict[str, str] = {
    "test_agent_task_e2e.py::compose_device": (
        "hands the listing to emulator_with_package, which asks emulators only"
    ),
    "test_emulator_smoke.py::test_omitting_serial_fails_loudly_when_multiple_devices_attached": (
        "counts the devices and names none: its one command is the echo adb must refuse"
    ),
}


def _functions():
    for path in sorted(TESTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                yield f"{path.name}::{node.name}", node


def _runs_adb_devices(function: ast.AST) -> bool:
    """True when the function builds the argv ``[adb, "devices", ...]``."""
    for node in ast.walk(function):
        if not isinstance(node, ast.List | ast.Tuple) or len(node.elts) < 2:
            continue
        first, second = node.elts[0], node.elts[1]
        if (
            isinstance(first, ast.Name)
            and first.id == "adb"
            and isinstance(second, ast.Constant)
            and second.value == "devices"
        ):
            return True
    return False


def test_only_tests_that_read_take_the_device_that_may_be_a_phone():
    takers = {
        name
        for name, node in _functions()
        if any(arg.arg == "live_device" for arg in node.args.args + node.args.kwonlyargs)
    }
    # conftest's own definition of the fixture takes `adb`, not itself.
    assert takers, "found no user of live_device; the comparison below would be vacuous"
    assert takers == READS_ONLY, (
        f"takes live_device but is not listed as read-only: {sorted(takers - READS_ONLY)} "
        f"(use emulator_only_device if it changes the device); "
        f"listed but no longer takes it: {sorted(READS_ONLY - takers)}"
    )


def test_only_the_compose_fixture_lists_devices_for_itself():
    listers = {
        name
        for name, node in _functions()
        if not name.startswith("conftest.py::") and _runs_adb_devices(node)
    }
    known = set(LISTS_DEVICES)
    assert listers == known, (
        f"runs `adb devices` and picks its own device: {sorted(listers - known)} "
        f"(take live_device or emulator_only_device); stale: {sorted(known - listers)}"
    )


def test_a_test_that_lists_devices_never_addresses_one_itself():
    """Listing is allowed above; choosing is not. No `-s <serial>` beside the listing.

    A function that has every attached serial in hand and then writes its own
    `adb -s` is the defect again, whatever it does with the answer.
    """
    addressing = sorted(
        name
        for name, node in _functions()
        if name in LISTS_DEVICES
        and any(isinstance(n, ast.Constant) and n.value == "-s" for n in ast.walk(node))
    )
    assert not addressing, f"lists the devices and then names one to adb: {addressing}"


class _Device:
    """Stands in for adb: every device asked answers with the recorded listing."""

    def __init__(self, installed: str) -> None:
        self.installed = installed
        self.argvs: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.argvs.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=self.installed, stderr="")


@pytest.fixture
def installed(recorded_anywhere) -> str:
    """What `pm list packages` printed for the fixture app on a device that has it."""
    listing = recorded_anywhere("package_list_uid")
    assert APP in listing, listing
    return listing


@pytest.fixture
def two_devices(recorded_anywhere) -> str:
    listing = recorded_anywhere("adb_devices_multiple")
    assert booted_serials(listing) == ["emulator-5554", "emulator-5556"], listing
    return listing


def test_a_phone_listed_first_with_the_app_is_neither_asked_nor_chosen(two_devices, installed):
    listing = two_devices.replace("emulator-5554", PHONE)
    assert booted_serials(listing) == [PHONE, "emulator-5556"]

    device = _Device(installed)
    assert emulator_with_package("adb", listing, APP, run=device) == "emulator-5556"
    assert device.argvs, "nothing was asked; the next assertion would be vacuous"
    assert not [argv for argv in device.argvs if PHONE in argv], device.argvs


def test_with_only_phones_attached_there_is_no_device_and_no_adb_call(two_devices, installed):
    listing = two_devices.replace("emulator-5554", PHONE).replace("emulator-5556", PHONE[::-1])
    assert len(booted_serials(listing)) == 2
    assert emulator_serials(booted_serials(listing)) == []

    device = _Device(installed)
    assert emulator_with_package("adb", listing, APP, run=device) is None
    assert device.argvs == []


def test_the_first_emulator_with_the_app_wins(two_devices, installed):
    """The reason the fixture searches at all: the app is often on the second."""

    def only_on_the_second(argv, **_kwargs):
        answer = installed if "emulator-5556" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout=answer, stderr="")

    assert emulator_with_package("adb", two_devices, APP, run=only_on_the_second) == "emulator-5556"
