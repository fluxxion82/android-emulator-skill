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

import pytest
from fake_toolchain import FakeToolchain, Rule, assert_refusal

SCRIPT = "android_health_check.sh"

# The probe the output must name. Not a sentence: "adb devices -l exited 1",
# "could not run adb devices" and any other wording that names it all pass
# (review of PR #20 rejected the first version's fixed phrase).
PROBE = "adb devices"

# The claim this finding is about. With it gone, so is its "  adb devices"
# hint line -- which today would otherwise satisfy PROBE on its own.
EMPTY_ANSWER = "No devices or emulators connected"


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


def test_h1_a_failed_device_probe_is_named_and_fails_the_check(broken_adb):
    """H1: ``android_health_check.sh:254-263,303``.

    The probe failed, so the check failed: exit non-zero, name the probe, and
    do not tell the reader that nothing is connected -- nobody looked.
    """
    result = broken_adb.run(SCRIPT)
    output = result.stdout + result.stderr

    assert result.returncode != 0, f"a failed device probe exited 0:\n{output[-800:]}"
    assert EMPTY_ANSWER not in output, "a probe that failed is reported as an empty answer"
    assert PROBE in output, f"the failed probe is not named:\n{output[-800:]}"


def test_h1_json_is_an_object_saying_the_check_failed(broken_adb):
    """H1: ``--json`` is a flag the script does not have; it must be real.

    Asked for JSON, stdout must be a JSON OBJECT whose ``success`` (or
    ``ok``) is false, and the run must exit non-zero. A bare ``null`` with
    exit 1 is not that (review of PR #20).

    Whether stdout is an object at all is the pinned behaviour, asserted on
    its first character. Decoding happens only after that, unwrapped: a
    document that starts like an object but does not parse is a broken fix,
    and ``raises=AssertionError`` must not excuse it as "H1, still red".
    """
    result = broken_adb.run(SCRIPT, "--json")

    document = result.stdout.strip()
    assert document.startswith("{"), f"--json stdout is not a JSON object: {document[:80]!r}"
    payload = json.loads(document)
    assert isinstance(payload, dict), payload
    verdict = payload["success"] if "success" in payload else payload.get("ok")
    assert verdict is False, f"the JSON does not say the check failed: {payload!r}"
    assert result.returncode != 0, "under --json a failed device probe exited 0"


def test_h1_json_escapes_control_bytes_in_device_probe_error(tmp_path):
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    diagnostic = "server startup failed: \x1b[31mcontrol\x01\n"
    chain.install(
        "adb",
        [
            Rule(("--version",), exit=0),
            Rule(("devices", "-l"), stderr=diagnostic, exit=1),
        ],
    )

    result = chain.run(SCRIPT, "--json")

    payload = assert_refusal(
        result,
        json_mode=True,
        naming=("adb devices -l",),
        status=1,
    )
    assert payload is not None
    assert payload["error"] == (
        "could not list devices (adb devices -l): server startup failed: \x1b[31mcontrol\x01"
    )
    assert "\x1b" in payload["error"]
    assert "\x01" in payload["error"]
    assert "\\u001b" in result.stdout
    assert "\\u0001" in result.stdout
