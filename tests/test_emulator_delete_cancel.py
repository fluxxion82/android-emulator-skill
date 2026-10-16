"""N4 -- a declined batch delete exits 0; a declined single delete exits 1.

``emulator_delete --name X`` that the user declines answers "Deletion
cancelled by user" and exits 1 (``emulator_delete.py:251-252``, ``:449``).
The batch paths ask the same question and treat "no" as "nothing to do":
``_delete_many`` returns ``(0, 0, [])`` (``:320-321``), ``_report_batch``
prints "No AVDs deleted", and ``--all`` / ``--old`` exit 0 (``:420``,
``:440``, ``:503-504``). Two answers to one question, and the batch one says
the request succeeded when it was refused.

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
    assert result.returncode == 1, result.stdout


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "N4: emulator_delete.py:320-321,420 -- a declined --all exits 0; and the input() "
        "prompt at :198 lands on stdout, ahead of the --json document"
    ),
)
@pytest.mark.parametrize("argv", [("--all",), ("--all", "--json")], ids=" ".join)
def test_n4_a_declined_batch_delete_is_a_cancelled_request(argv, one_avd):
    """N4: refusing ``--all`` is a cancelled request -- a refusal, exit 1.

    In the shared boundary's shape, naming the cancellation: one refusal
    object under ``--json``, ``Error:``/``Remedy:`` on stderr otherwise. Under
    ``--json`` that means stdout is ONLY the object -- today the confirmation
    prompt (``input()`` at ``emulator_delete.py:198``) is written to stdout
    first, so a caller parsing the JSON gets
    ``Permanently delete ALL 1 AVDs? (type 'yes' to confirm): {...`` instead.
    """
    result = _declined(one_avd, *argv)
    if ["list", "avd", "-c"] not in one_avd.calls("avdmanager"):
        pytest.fail(f"the batch never listed AVDs, so no prompt was reached: {one_avd.calls()}")
    assert_refusal(result, json_mode="--json" in argv, naming=("cancel",), status=1)
