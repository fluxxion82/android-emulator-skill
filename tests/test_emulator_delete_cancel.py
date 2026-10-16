"""N4 -- a declined batch delete exited 0; a declined single delete exited 1.

Until the fix, ``emulator_delete --name X`` that the user declined answered
"Deletion cancelled by user" and exited 1 (main @ 6e4e646:
``emulator_delete.py:251-252``, ``:449``). The batch paths asked the same
question and treated "no" as "nothing to do": ``_delete_many`` returned
``(0, 0, [])`` (``:320-321``), ``_report_batch`` printed "No AVDs deleted",
and ``--all`` / ``--old`` exited 0 (``:420``, ``:440``, ``:503-504``). Two
answers to one question, and the batch one said the request succeeded when it
was refused. Every mode now refuses the same way, through common.cli.

The stub ``avdmanager`` answers ``list avd -c`` with the recorded compact
listing (one AVD, Pixel_9), so there is something to delete and the prompt is
reached; it answers ``delete avd`` with silence -- and every spec asserts that
``delete`` was never called, because "no" was typed.
"""

from __future__ import annotations

import pytest
from fake_toolchain import FakeToolchain, Rule, assert_refusal

SCRIPT = "emulator_delete.py"


@pytest.fixture
def one_avd(tmp_path, recorded) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    chain.install(
        "avdmanager",
        [
            Rule(("list", "avd", "-c"), stdout=recorded.text("avdmanager_list_avd_compact")),
            Rule(("list", "avd"), stdout=recorded.text("avdmanager_list_avd")),
            Rule(("delete", "avd"), exit=0),
        ],
    )
    return chain


def _deleted(chain: FakeToolchain) -> list[list[str]]:
    return [call for call in chain.calls("avdmanager") if call[:2] == ["delete", "avd"]]


def _declined(chain: FakeToolchain, *argv: str):
    """Run with "no" on stdin. A deletion after "no" fails loudly, never as N4.

    ``pytest.fail`` rather than an assertion: that would be a far worse defect
    than the one pinned here, and ``raises=AssertionError`` must not be able
    to excuse it.
    """
    result = chain.run(SCRIPT, *argv, stdin="no\n")
    if _deleted(chain):
        pytest.fail(f"an AVD was deleted after the user typed no: {_deleted(chain)}")
    return result


def test_a_declined_single_delete_exits_one(one_avd, recorded):
    """The reference behaviour, unmarked and green today."""
    name = recorded.text("avdmanager_list_avd_compact").split()[0]
    result = _declined(one_avd, "--name", name)
    assert "Traceback" not in result.stderr + result.stdout, result.stderr
    assert result.returncode == 1, result.stdout


@pytest.mark.parametrize("argv", [("--all",), ("--all", "--json")], ids=" ".join)
def test_n4_a_declined_batch_delete_is_a_cancelled_request(argv, one_avd):
    """N4: refusing ``--all`` is a cancelled request -- a refusal, exit 1.

    In the shared boundary's shape, naming the cancellation: one refusal
    object under ``--json``, ``Error:``/``Remedy:`` on stderr otherwise. Under
    ``--json`` that means stdout is ONLY the object -- before the fix the
    confirmation prompt (``input()`` at ``emulator_delete.py:198`` on main @
    6e4e646) was written to stdout first, so a caller parsing the JSON got
    ``Permanently delete ALL 1 AVDs? (type 'yes' to confirm): {...`` instead.
    """
    result = _declined(one_avd, *argv)
    if ["list", "avd", "-c"] not in one_avd.calls("avdmanager"):
        pytest.fail(f"the batch never listed AVDs, so no prompt was reached: {one_avd.calls()}")
    assert_refusal(result, json_mode="--json" in argv, naming=("cancel",), status=1)
