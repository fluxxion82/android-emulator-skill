"""N11 -- an audit of nothing is not a clean audit.

``localization_audit.py:443-445`` refuses a ``--res`` that does not exist, but
an EXISTING directory with no ``values*/strings.xml`` in it -- a wrong path one
level off, ``app/src/main`` instead of ``app/src/main/res`` -- audits zero keys
across zero locales and prints "No issues found." at exit 0. ``--strict``
exits only on findings (``:468-469``), and there are none, so it passes too.
Measured on ``main``: ``Resources: 0 keys, 0 translation locale(s). / Missing
keys: none. / No issues found.``, exit 0; under ``--json`` a report with
``"total_keys": 0`` and every list empty, exit 0.

Same class as L3 (an accessibility audit of a screen it could not read): a
check that inspected nothing must not report that everything passed.
"""

from __future__ import annotations

import json

import pytest
from fake_toolchain import SCRIPTS

SCRIPT = SCRIPTS / "localization_audit.py"


def _clean_report(stdout: str) -> bool:
    """Whether stdout is a successful audit with no findings, in either mode."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return "No issues found" in stdout
    return isinstance(payload, dict) and "error" not in payload and "total_keys" in payload


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="N11: localization_audit.py:443-445,468-469 -- an empty --res audits clean, exit 0",
)
@pytest.mark.parametrize(
    "flags", [(), ("--json",), ("--strict",)], ids=lambda f: " ".join(f) or "text"
)
def test_n11_an_empty_res_dir_is_an_error(flags, tmp_path):
    """N11: a ``--res`` holding no string resources fails, and says why."""
    import subprocess
    import sys

    res = tmp_path / "res"
    res.mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--res", str(res), *flags],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert "Traceback" not in result.stderr, result.stderr
    assert not _clean_report(result.stdout), f"an empty --res audited clean:\n{result.stdout}"
    assert result.returncode != 0, result.stdout
    assert (result.stdout + result.stderr).strip(), "failed without saying why"
