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
from fake_toolchain import FakeToolchain, Rule

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
    result = chain.run(SCRIPT, *argv, stdin="no\n")
    assert not _deleted(chain), f"an AVD was deleted after the user typed no: {_deleted(chain)}"
    assert "Traceback" not in result.stderr, result.stderr
    return result


def test_a_declined_single_delete_exits_one(one_avd, recorded):
    """The reference behaviour, unmarked and green today."""
    name = recorded.text("avdmanager_list_avd_compact").split()[0]
    result = _declined(one_avd, "--name", name)
    assert result.returncode == 1, result.stdout


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="N4: emulator_delete.py:320-321,420 -- a declined --all reports success, exit 0",
)
@pytest.mark.parametrize("argv", [("--all",), ("--all", "--json")], ids=" ".join)
def test_n4_a_declined_batch_delete_exits_one_like_a_declined_single_one(argv, one_avd):
    """N4: refusing ``--all`` is a cancelled request, exit 1, not "nothing to do"."""
    result = _declined(one_avd, *argv)
    assert ["list", "avd", "-c"] in one_avd.calls("avdmanager"), "the batch never listed AVDs"
    assert (
        result.returncode == 1
    ), f"{' '.join(argv)} declined, exit {result.returncode}:\n{result.stdout}"
