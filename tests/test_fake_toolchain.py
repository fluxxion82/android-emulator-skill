"""The harness the red specs stand on, checked directly.

``tests/fake_toolchain.py`` holds two things every Inc 1 spec depends on: the
stub tools, and ``assert_refusal``, the refusal contract of the shared CLI
boundary (PR #18). A contract checker that accepted a traceback, a bare
``null`` or a prompt glued to the JSON would make every refusal spec vacuous
in the same direction at once -- the review of PR #20 found the first version
of these specs did exactly that by checking "non-zero, and the tool's name
somewhere". So the checker is exercised here against each shape it must
accept and each it must reject.

The documents are built with ``json.dumps`` and ``"\\n".join`` from short
values: they are this skill's OWN output shape, not a tool's, and nothing here
is handed to a parser.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from fake_toolchain import assert_refusal

REFUSAL = {"success": False, "error": "`sdkmanager` was not found", "remedy": "Install it."}


def _run(returncode: int = 1, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["x"], returncode, stdout, stderr)


def _text_refusal() -> str:
    return "\n".join(["Error: `sdkmanager` was not found", "Remedy: Install it."]) + "\n"


def test_the_contract_shape_is_accepted_in_both_modes():
    payload = assert_refusal(
        _run(stdout=json.dumps(REFUSAL) + "\n"),
        json_mode=True,
        naming=("sdkmanager",),
        status=1,
    )
    assert payload == REFUSAL
    assert (
        assert_refusal(
            _run(stderr=_text_refusal()), json_mode=False, naming=("sdkmanager",), status=1
        )
        is None
    )


def test_a_streaming_refusal_is_the_last_line():
    record = json.dumps({"kind": "anr"})
    stdout = "\n".join([record, json.dumps(REFUSAL)]) + "\n"
    assert_refusal(
        _run(stdout=stdout), json_mode=True, naming=("sdkmanager",), status=1, streaming=True
    )
    with pytest.raises(AssertionError):
        assert_refusal(_run(stdout=stdout), json_mode=True, naming=("sdkmanager",), status=1)


REJECTED_JSON = {
    "a traceback": _run(stdout=json.dumps(REFUSAL), stderr="Traceback (most recent call last):"),
    "the wrong status": _run(returncode=3, stdout=json.dumps(REFUSAL)),
    "a bare null": _run(stdout="null"),
    "a prompt before the object": _run(stdout="Delete? " + json.dumps(REFUSAL)),
    "success true": _run(stdout=json.dumps({**REFUSAL, "success": True})),
    "no remedy": _run(stdout=json.dumps({"success": False, "error": "sdkmanager"})),
    "an error naming nothing": _run(stdout=json.dumps({**REFUSAL, "error": "it failed"})),
    "the legacy error-only shape": _run(stdout=json.dumps({"error": "sdkmanager missing"})),
    "an empty stdout": _run(stderr=_text_refusal()),
}


@pytest.mark.parametrize("case", list(REJECTED_JSON))
def test_json_mode_rejects(case):
    with pytest.raises(AssertionError):
        assert_refusal(REJECTED_JSON[case], json_mode=True, naming=("sdkmanager",), status=1)


REJECTED_TEXT = {
    "no Error line": _run(stderr="sdkmanager is missing\n"),
    "no Remedy line": _run(stderr="Error: sdkmanager was not found\n"),
    "JSON on stdout": _run(stdout=json.dumps(REFUSAL) + "\n", stderr=_text_refusal()),
    "an Error line naming nothing": _run(stderr="\n".join(["Error: it failed", "Remedy: retry"])),
}


@pytest.mark.parametrize("case", list(REJECTED_TEXT))
def test_text_mode_rejects(case):
    with pytest.raises(AssertionError):
        assert_refusal(REJECTED_TEXT[case], json_mode=False, naming=("sdkmanager",), status=1)


def test_a_malformed_object_is_loud_not_excused():
    """Looks like an object, is not one: a broken fix, so not an AssertionError."""
    with pytest.raises(json.JSONDecodeError):
        assert_refusal(_run(stdout='{"success": false,}'), json_mode=True, naming=("x",), status=1)
