#!/usr/bin/env python3
"""Check a release zip against the plugin directory it was made from. Standard library only.

    python3 tests/check_release_zip.py <zip> <plugin-dir>

The `package` job of release.yml runs this on the runner, on the file its next
step uploads, and the job fails unless it exits 0. In order, stopping at the
first that fails:

1. `unzip` extracts the zip to a temporary directory.
2. The extracted files are the plugin directory's files, name for name and
   byte for byte, less what a machine adds to a directory and a release must
   not carry: any path with a `__pycache__` part, a `.pyc` suffix or the name
   `.DS_Store`. The zip holds no entry of that kind either, an entry for a
   directory included.
3. Every Python script under `skills/android-emulator-skill/scripts/` of the
   extracted copy answers `--help`, started by its absolute path from another
   directory. Of this environment it is given PATH and nothing else;
   `verify_install.script_problems` adds `PYTHONDONTWRITEBYTECODE=1`.

Exit status: 0 when all three hold, 1 when one does not, 2 for bad arguments
or a Python older than the scripts need.

Limits. The plugin directory is read when this runs, after the steps before
it: a step that rewrote a file and then packed it leaves a directory and a
zip that agree, and this passes. tests/test_release_artifact.py records the
directory before the first step and tells that apart, on the test machine.
Files are compared, by name and by bytes. A directory with nothing in it
that the zip holds and the plugin does not is not seen, and neither is a
file whose mode changed, a lost executable bit included.
The scripts are started by the Python running this, which on the runner is
the image's `python3` with whatever packages the image has. And the zip is
the one that exists when this runs: the job keeps this the last step before
the upload, and a guard in that test module holds it there.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import verify_install

EXTRACT_TIMEOUT = 120


def is_junk(relative: str) -> bool:
    """Whether a path, written with `/`, is one a machine adds and a release must not carry."""
    parts = relative.split("/")
    return "__pycache__" in parts or relative.endswith(".pyc") or parts[-1] == ".DS_Store"


def wanted_from(plugin: Path) -> dict[str, str]:
    """``{relative path: sha256}`` of what a zip of a plugin directory should hold.

    Raises OSError for a directory or a file it cannot read, the root included.
    """
    held = verify_install.files_under(plugin)
    return {name: digest for name, digest in held.items() if not is_junk(name)}


def extract(archive: Path, destination: Path) -> Path:
    """Unpack a zip into a new directory, with the tool a person would use.

    Raises OSError when there is no `unzip`, subprocess.CalledProcessError
    when it exits non-zero and subprocess.TimeoutExpired when it does not end.
    """
    destination.mkdir(parents=True)
    subprocess.run(
        ["unzip", "-q", str(archive), "-d", str(destination)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=EXTRACT_TIMEOUT,
        check=True,
    )
    return destination


def differences(wanted: dict[str, str], extracted: Path, archive: Path) -> list[str]:
    """How an extracted zip differs from what it should hold. Empty: it does not."""
    have = verify_install.files_under(extracted)
    found = [f"missing: {name}" for name in sorted(wanted.keys() - have.keys())]
    found += [f"not the plugin's: {name}" for name in sorted(have.keys() - wanted.keys())]
    found += [
        f"other bytes: {name}"
        for name in sorted(wanted.keys() & have.keys())
        if wanted[name] != have[name]
    ]
    with zipfile.ZipFile(archive) as held:
        found += [f"junk entry: {name}" for name in held.namelist() if is_junk(name.rstrip("/"))]
    return found


def check(archive: Path, plugin: Path, scratch: Path) -> tuple[int, int, list[str]]:
    """``(files compared, scripts started, what is wrong)`` for one zip. Nothing wrong is a pass.

    `scratch` is an empty directory, this check's to fill.
    """
    try:
        wanted = wanted_from(plugin)
    except OSError as error:
        return 0, 0, [f"the plugin directory could not all be read: {error}"]
    if not wanted:
        return 0, 0, [f"{plugin} holds no file to compare the zip with"]
    try:
        extracted = extract(archive, scratch / "plugin")
    except subprocess.CalledProcessError as error:
        said = (error.stdout + error.stderr).strip()[-400:]
        return 0, 0, [f"`unzip` exited {error.returncode} on {archive.name}: {said}"]
    except (OSError, subprocess.TimeoutExpired) as error:
        return 0, 0, [f"`unzip` did not extract {archive.name}: {error}"]
    try:
        found = differences(wanted, extracted, archive)
    except (OSError, zipfile.BadZipFile) as error:
        return 0, 0, [f"what {archive.name} holds could not all be read: {error}"]
    if found:
        return len(wanted), 0, found
    cwd = scratch / "cwd"
    cwd.mkdir()
    scripts, problems = verify_install.script_problems(
        extracted / "skills" / verify_install.SKILL,
        cwd,
        {"PATH": os.environ.get("PATH", "")},
        "extracted",
    )
    return len(wanted), scripts, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a release zip against the plugin directory it was made from.",
        epilog="Exit 0: the zip is right. 1: it is not. 2: bad arguments or an old Python.",
    )
    parser.add_argument("zip", type=Path, help="The zip a release would upload.")
    parser.add_argument("plugin_dir", type=Path, help="The plugin directory it was made from.")
    arguments = parser.parse_args(argv)
    if sys.version_info < verify_install.MINIMUM_PYTHON:
        floor = ".".join(map(str, verify_install.MINIMUM_PYTHON))
        print(
            f"The scripts this starts need Python {floor}+, and this is {sys.version.split()[0]}."
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="check-release-zip-") as scratch:
        files, scripts, problems = check(
            arguments.zip.resolve(), arguments.plugin_dir.resolve(), Path(scratch).resolve()
        )
    if problems:
        print(f"FAILED: {arguments.zip.name}, checked against {arguments.plugin_dir}:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(
        f"ok: {arguments.zip.name} holds the {files} files of {arguments.plugin_dir} and no other "
        f"file; {scripts} scripts answer --help from the extracted copy."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
