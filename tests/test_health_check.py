"""H1 -- the health check must not report a failed probe as an empty answer.

``android_health_check.sh:254-263`` lists devices with

    DEVICE_LINES=$(adb devices -l 2>/dev/null | sed '1d' | grep -v '^$' || true)

so an ``adb devices -l`` that FAILS -- server will not start, permissions, a
broken install -- is indistinguishable from one that ran and found nothing: the
script warns "No devices or emulators connected" and exits 0 (``:303``). And
``--json`` is not an option the script has; it prints the same glyph-decorated
text (first byte 0xe2), so a caller that asked for JSON cannot parse the answer.

The stub ``adb`` here answers ``--version`` (exit 0, and prints nothing -- the
check only displays the line, and no ``adb --version`` banner is recorded) and
fails ``devices -l`` with exit 1, also silently. Silence keeps the double free
of invented text: the failure is carried by the exit status alone, which is
exactly what the script throws away. Same fake-tool arrangement as
``test_exit_code_sweep.py``: a PATH holding the stub bin, ``/usr/bin`` and
``/bin``, and nothing else.

``tests/test_android_health_check.py`` covers the script's other checks; this
file is only the failed-probe contract, pinned red until Inc 1 fixes it.
"""

from __future__ import annotations

import json
import re

import pytest
from fake_toolchain import FakeToolchain, Rule

SCRIPT = "android_health_check.sh"

# How the fixed script may phrase the failed probe. Several spellings are
# accepted so the fix is free to word it; what is not accepted is silence, or
# "No devices", which is the claim this finding is about.
FAILED_PROBE = re.compile(r"(could not|couldn't|cannot|can't|failed to) list devices", re.I)


@pytest.fixture
def broken_adb(tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    chain.install("adb", [Rule(("--version",), exit=0), Rule(("devices",), exit=1)])
    return chain


def test_the_stub_really_fails_the_device_probe(broken_adb):
    """Anti-vacuity, unmarked: the script reaches ``adb devices -l`` and it fails.

    If the probe were never made, the specs below could not distinguish a
    script that reports the failure from one that never looked.
    """
    result = broken_adb.run(SCRIPT)
    assert ["devices", "-l"] in broken_adb.calls("adb"), broken_adb.calls("adb")
    assert ["--version"] in broken_adb.calls("adb")
    assert "Traceback" not in result.stderr


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H1: android_health_check.sh:254-263 turns a failed adb devices -l into 'No devices', exit 0",
)
def test_h1_a_failed_device_probe_is_named_and_fails_the_check(broken_adb):
    """H1: ``android_health_check.sh:254-263,303``.

    The probe failed, so the check failed: say which probe, exit non-zero, and
    do not tell the reader that nothing is connected -- nobody looked.
    """
    result = broken_adb.run(SCRIPT)
    output = result.stdout + result.stderr

    assert result.returncode != 0, f"a failed device probe exited 0:\n{output[-800:]}"
    assert FAILED_PROBE.search(output), f"the failed probe is not named:\n{output[-800:]}"
    assert (
        "No devices or emulators connected" not in output
    ), "a probe that failed is reported as an empty answer"


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="H1: android_health_check.sh ignores --json"
)
def test_h1_json_is_json_and_fails_too(broken_adb):
    """H1: ``--json`` is a flag the script does not have; it must be real.

    Asked for JSON, the script must print a JSON document on stdout -- and the
    failed probe fails it in that mode as well.
    """
    result = broken_adb.run(SCRIPT, "--json")

    try:
        json.loads(result.stdout)
    except json.JSONDecodeError as error:
        # An AssertionError, not pytest.fail: the xfail accepts only that, so
        # a harness that breaks some other way fails loudly instead of being
        # excused as "H1, still red".
        raise AssertionError(
            f"--json stdout is not JSON ({error}); it begins {result.stdout[:80]!r}"
        ) from error
    assert result.returncode != 0, "under --json a failed device probe exited 0"
