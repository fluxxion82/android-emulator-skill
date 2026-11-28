"""T9: the zip a release uploads, built by the release workflow's own steps and then used.

Every other test of the package reads this checkout's `android-emulator-skill/`
directory and takes it that the zip holds the same. This module builds the
zip instead: the `package` job's `run` steps, in order, by `bash -e`, as far as
the step that uploads. The file that step names is extracted somewhere else
with `unzip`, and what is asked of it is what a person who downloaded it would
ask: are the files all there and nothing besides, does a script start from
where it now sits, does every relative link in its documents name a file it
holds (a heading after `#` is not looked for, and a URL is not fetched), can
an agent read it as a plugin.

The limit, stated: the steps run in a copy of the plugin directory, not on a
runner. Only the scripts of the job's `run` steps are run. Everything else
about the job is compared, whole, with what is written out below: the
checkout and the upload are each one literal (`CHECKOUT`, `UPLOAD`), the keys
of the workflow, the job and a `run` step are lists of what is allowed, and
the workflow is read with the reader that refuses what GitHub reads another
way. Nothing about an action's inputs is interpreted here, because three
attempts to say which inputs were harmless each missed one; an edit to either
step is an edit to its literal.

Every `run` step is run, in order, in one directory, so what a step leaves in
the directory for a later one is seen. What a step hands a later one by
another road is not: the runner's files of variables and paths (`$GITHUB_ENV`,
`$GITHUB_PATH`, `$GITHUB_OUTPUT`) are refused by name, each step is given an
empty summary file of its own as on a runner, and a script that reached a
runner file by a name it built, or changed the machine instead of the
directory, is past this. What the zip should hold is recorded before the
first step runs, so a step that changes the directory cannot move the
comparison with it. And a `zip` or `unzip` that differs from Ubuntu's can
differ in what it packs; the suite runs on Ubuntu in CI.

One limit is not a detail of the harness and no edit to it removes it: a
script can ask where it is running. Each step is started as a runner starts
it (`bash -e` on a file, in the checkout, with an empty summary file), but
the variables a runner sets, the tools it has installed and the user it runs
as are this machine's. This harness is for a zip step that packs the wrong
files. It is not a defence against a step written to tell the two apart, and
nothing in the job is: a step that knows a runner when it sees one can
rewrite `tests/check_release_zip.py` there as easily as the zip.

What the job's own last step before the upload, `CHECK`, sees is a runner
that differs from this machine by accident: another `zip`, another `unzip`,
a checkout with other files in it. On the runner,
`tests/check_release_zip.py` extracts the file about to be uploaded, compares
it with the plugin directory less the same junk, and starts every Python
script from the extracted copy. The comparison is that module's, and this
one uses it too. The step is held where it is by a guard below, and it is
run here like any other `run` step, from a checkout that holds the two files
it needs; here it is started by whichever `python3` is first on PATH.

What is on the runner and what is only here, then. On the runner: the files
of the real zip against the directory, no junk entry, `--help` from every
Python script. Only here: the directory as it was before the first step (the
step reads it after, so a file rewritten and then packed agrees with its
zip; `STEP_CANNOT_SEE` names the case), the shell scripts, the relative
links, the two manifests, and every refusal of a job in another shape.

And who may write the asset at all. The check is of the file the `package`
job uploads; a job after it, or another workflow on the same trigger, could
put another file of that name in its place. `_asset_writer_problems` holds
the jobs of release.yml to the ones written out here, and lets the token of
no job of any workflow write the repository's contents, the `package`
job's apart.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import zipfile
from collections.abc import Callable
from pathlib import Path

import check_release_zip
import pytest
import verify_install
from test_distribution_manifests import CLAUDE, CODEX, SHIPPED, SKILL, _path_problems
from test_packaging_contract import (
    PACKAGE_ROOT,
    WORKFLOWS,
    _filled,
    _links_that_do_not_ship,
    _literal_label,
    _packaged_markdown,
    _scripts_on_disk,
    _unreadable_links,
    _written_alike,
    _yaml_with_the_keys_github_reads,
)

RELEASE = WORKFLOWS / "release.yml"
PACKAGE_JOB = "package"
TAG = "v99.98.97"
REPOSITORY = "owner/repository"
WORKFLOW_KEYS = frozenset({"name", "on", "permissions", "jobs"})
JOB_KEYS = frozenset({"name", "runs-on", "needs", "permissions", "steps"})
RUNNER = "ubuntu-latest"
# Every job of release.yml. A job beside these is one nothing here examined.
RELEASE_JOBS = ("verify", "emulator", "install", "version", "package")
# What the token of the package job may do, whole. No other job's may write contents.
PACKAGE_PERMISSIONS = {"contents": "write"}
# The job's first and last steps, less their `name`, exactly. The upload's
# `files` is one path as the action reads it: no comma, newline or pattern.
CHECKOUT = {"uses": "actions/checkout@v4"}
UPLOAD = {
    "uses": "softprops/action-gh-release@3bb12739c298aeb8a4eeaf626c5b8d85266b0e65",
    "with": {
        "files": "android-emulator-skill-${{ github.ref_name }}.zip",
        "generate_release_notes": True,
        "fail_on_unmatched_files": True,
    },
}
# The step before the upload, whole, its name included: the job's own check of
# the file the upload names, against the directory it was zipped from.
CHECK = {
    "name": "Check the zip against the plugin directory",
    "run": f"python3 tests/check_release_zip.py {UPLOAD['with']['files']} {PACKAGE_ROOT.name}",
}
# What that step runs, where a checkout of the repository has it.
CHECKER_FILES = ("tests/check_release_zip.py", "tests/verify_install.py")
# The one runner file a step may name: what is written to it is shown, not run.
RUNNER_FILE = re.compile(r"GITHUB_[A-Z_]+")
RUNNER_FILES_ALLOWED = frozenset({"GITHUB_STEP_SUMMARY"})

# What a developer's machine adds to the plugin directory and a release must
# not carry: bytecode at each depth, a stray `.pyc`, Finder's files, and a
# `__pycache__` with nothing in it (zip stores a directory as an entry too).
JUNK_FILES = (
    "__pycache__/top.cpython-312.pyc",
    f"skills/{SKILL}/scripts/__pycache__/navigator.cpython-312.pyc",
    f"skills/{SKILL}/scripts/common/__pycache__/hierarchy.cpython-312.pyc",
    f"skills/{SKILL}/scripts/stray.pyc",
    ".DS_Store",
    f"skills/{SKILL}/references/.DS_Store",
)
JUNK_DIRECTORIES = (f"skills/{SKILL}/examples/__pycache__",)


_is_junk = check_release_zip.is_junk


@dataclasses.dataclass(frozen=True)
class Built:
    """One run of the package job: where it ran, the file it would upload, what that should hold."""

    checkout: Path
    archive: Path
    transcript: str
    # ``{relative path: sha256}`` of the plugin directory less its junk, taken
    # before any step of the job ran.
    wanted: dict[str, str]
    # The step that exited non-zero, where a job stops. None: every step ran.
    stopped_at: str | None = None

    @property
    def source(self) -> Path:
        return self.checkout / PACKAGE_ROOT.name


def _checkout(root: Path) -> tuple[Path, dict[str, str]]:
    """The plugin directory where a release's checkout has it, with a developer's junk added.

    And what a zip of it should hold, read now: the job's steps run in this
    directory, and one that rewrote a file would otherwise rewrite the answer.
    """
    checkout = root / "checkout"
    source = checkout / PACKAGE_ROOT.name
    shutil.copytree(PACKAGE_ROOT, source, symlinks=True)
    for relative in JUNK_FILES:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00junk")
    for relative in JUNK_DIRECTORIES:
        (source / relative).mkdir(parents=True, exist_ok=True)
    for relative in CHECKER_FILES:
        (checkout / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PACKAGE_ROOT.parent / relative, checkout / relative)
    held = verify_install.files_under(source)
    assert set(JUNK_FILES) <= set(held), "the junk this harness plants is not in the directory"
    return checkout, {name: digest for name, digest in held.items() if not _is_junk(name)}


def _modelled(text: str) -> tuple[list[tuple[str, str]], str]:
    """``([(step name, its script as the shell gets it)], the file the upload names)``.

    For a job in the one shape this harness runs, checked whole before
    anything is run: a job that differs in any way from that shape is one
    whose zip this harness would only be guessing at.
    """
    workflow = _yaml_with_the_keys_github_reads(text)
    job = workflow["jobs"][PACKAGE_JOB]
    wrong = [f"workflow key `{key}`" for key in workflow if key not in WORKFLOW_KEYS]
    wrong += [f"job key `{key}`" for key in job if key not in JOB_KEYS]
    if job.get("runs-on") != RUNNER:
        wrong.append(f"`runs-on: {job.get('runs-on')}`, not `{RUNNER}`")
    steps = job["steps"]
    for index, step in enumerate(steps):
        if not _literal_label(step.get("name")):
            wrong.append(f"step {index} with keys {sorted(step)} and no name that is plain text")
    first, *middle, last = [
        {key: value for key, value in step.items() if key != "name"} for step in steps
    ]
    if not _written_alike(first, CHECKOUT):
        wrong.append(f"a first step that is not the checkout written out in `CHECKOUT`: {first}")
    if not _written_alike(last, UPLOAD):
        wrong.append(f"a last step that is not the upload written out in `UPLOAD`: {last}")
    for step, written in zip(steps[1:-1], middle, strict=True):
        named = step.get("name")
        if set(written) != {"run"} or not isinstance(written["run"], str):
            wrong.append(
                f"step {named!r} with keys {sorted(step)}, where a step between the checkout "
                "and the upload has `name` and `run` only"
            )
            continue
        files = sorted(set(RUNNER_FILE.findall(written["run"])) - RUNNER_FILES_ALLOWED)
        if files:
            wrong.append(f"{files} in the script of step {named!r}")
    assert not wrong, (
        "the tests cannot say what the package job uploads, because of "
        f"{'; '.join(wrong)}. The harness runs the scripts of plain `run` steps between a "
        "checkout and an upload it knows whole."
    )
    known = {"github.ref_name": TAG, "github.repository": REPOSITORY}
    scripts = [
        (step["name"], _filled(step["run"], known, f"release.yml, step {step['name']!r}"))
        for step in steps[1:-1]
    ]
    return scripts, _filled(UPLOAD["with"]["files"], known, "release.yml, the upload")


def _package(text: str, root: Path, *, may_stop: bool = False) -> Built:
    """Run the package job of a release workflow on tag `TAG`, up to its upload step.

    A step that exits non-zero stops the job, as on a runner. That fails the
    caller unless it said the job may stop; then the step is named in
    `stopped_at` and no later step is run.
    """
    scripts, uploaded = _modelled(text)
    checkout, wanted = _checkout(root)
    transcript: list[str] = []
    stopped_at = None
    for index, (name, script) in enumerate(scripts):
        # A summary file of its own, there and empty when the step starts, as
        # each step has on a runner. One file for the job, or none until a
        # step wrote one, would each be a road between steps that GitHub lacks.
        summary = root / f"github-summary-{index}"
        summary.write_text("", encoding="utf-8")
        # From a file, as a runner starts a step that names no shell (`bash -e
        # {0}`): to `bash -c` a script has no source file, and can tell.
        source = root / f"step-{index}.sh"
        source.write_text(script, encoding="utf-8")
        ran = subprocess.run(
            ["bash", "-e", str(source)],
            cwd=checkout,
            env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary)},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        transcript.append(f"## step {name!r}: exit {ran.returncode}\n{ran.stdout}{ran.stderr}")
        if ran.returncode != 0:
            assert may_stop, "\n".join(transcript)
            stopped_at = name
            break
    return Built(checkout, checkout / uploaded, "\n".join(transcript), wanted, stopped_at)


def _extract(built: Built, destination: Path) -> Path:
    """Unpack the zip elsewhere, with the tool a person would use."""
    return check_release_zip.extract(built.archive, destination)


def _differences(built: Built, extracted: Path) -> list[str]:
    """How the extracted zip differs from the plugin directory less its junk. Empty: it does not.

    The comparison the job's own step makes, against the directory as it was
    before the first step ran, where the step has it as the steps left it.
    """
    return check_release_zip.differences(built.wanted, extracted, built.archive)


@pytest.fixture(scope="module")
def release(tmp_path_factory) -> tuple[Built, Path]:
    """The real workflow's zip and its extraction. Shared: no test may change either."""
    root = tmp_path_factory.mktemp("release")
    built = _package(RELEASE.read_text(encoding="utf-8"), root)
    return built, _extract(built, root / "elsewhere" / "plugin")


def _skill(extracted: Path) -> Path:
    return extracted / "skills" / SKILL


def test_the_zip_tested_is_the_file_the_release_uploads(release):
    built, _ = release
    assert built.archive.name == f"android-emulator-skill-{TAG}.zip"
    assert built.archive.is_file(), built.transcript


def test_the_zip_holds_the_plugin_directory_and_none_of_a_machines_junk(release):
    """File for file and byte for byte, less bytecode and Finder's files, at any depth.

    The junk is planted, so the exclusions are seen to act: a clean checkout
    has none, and a test on one would pass with every `-x` deleted.
    """
    built, extracted = release
    assert all((built.source / name).is_file() for name in JUNK_FILES)
    assert all((built.source / name).is_dir() for name in JUNK_DIRECTORIES)
    assert len(built.wanted) > 70 and not any(_is_junk(name) for name in built.wanted)

    assert _differences(built, extracted) == []
    assert (_skill(extracted) / "SKILL.md").is_file()


def _edit_release(old: str, new: str, *more: str) -> Callable[[str], str]:
    """One replacement, or several given as further old, new pairs; each anchor exactly once."""
    pairs = [(old, new), *zip(more[::2], more[1::2], strict=True)]

    def change(text: str) -> str:
        for before, after in pairs:
            # Counted in the text as it now stands: an earlier replacement can
            # write a later anchor a second time.
            assert text.count(before) == 1, f"the text does not hold {before!r} exactly once"
            text = text.replace(before, after)
        return text

    return change


# Each is a zip step that packs the wrong thing. The comparison above has to
# say so of every one, or it is a comparison that cannot fail.
WRONG_PACKAGES = {
    "nested bytecode directories are packed": _edit_release(
        '            -x "*/__pycache__/*" \\\n', ""
    ),
    "a stray .pyc is packed": _edit_release('            -x "*.pyc" \\\n', ""),
    "Finder's files are packed": _edit_release('-x "*.DS_Store"', '-x "*.nothing"'),
    "the documents are left out": _edit_release('-x "*.DS_Store"', '-x "*.DS_Store" -x "*.md"'),
    "the skill alone is packed, without the manifests": _edit_release(
        ".zip . \\\n", ".zip skills \\\n"
    ),
    "the references are left out": _edit_release(
        '-x "*.DS_Store"', '-x "*.DS_Store" -x "*/references/*"'
    ),
    # The directory and the zip then agree with each other, and not with the plugin.
    # On a runner each step has its own summary file, so the zip step never
    # sees `ready` and leaves the documents out. A harness with one file for
    # the job would see it and pack them, and call that the release's zip.
    "the zip step decides by what an earlier step wrote to the summary": _edit_release(
        '          echo "✅ Skill structure validated successfully"\n',
        '          echo "✅ Skill structure validated successfully"\n'
        '          echo ready >> "$GITHUB_STEP_SUMMARY"\n',
        "          cd android-emulator-skill\n          zip -r",
        "          exclude='*.md'\n"
        "          if grep -q '^ready$' \"$GITHUB_STEP_SUMMARY\" 2>/dev/null; then "
        "exclude='*.nothing'; fi\n"
        "          cd android-emulator-skill\n          zip -r",
        '-x "*.DS_Store"',
        '-x "*.DS_Store" -x "$exclude"',
    ),
    # On a runner the file is there before the step starts, so the documents
    # are left out. A harness that made no file would pack them.
    "the zip step decides by whether its summary file is there": _edit_release(
        "          cd android-emulator-skill\n          zip -r",
        "          exclude='*.nothing'\n"
        "          if [ -f \"$GITHUB_STEP_SUMMARY\" ]; then exclude='*.md'; fi\n"
        "          cd android-emulator-skill\n          zip -r",
        '-x "*.DS_Store"',
        '-x "*.DS_Store" -x "$exclude"',
    ),
    # A step started with `bash -c` has no source file, and one on a runner has.
    "the zip step decides by whether it was started from a file": _edit_release(
        "          cd android-emulator-skill\n          zip -r",
        "          exclude='*.nothing'\n"
        "          if [[ -n ${BASH_SOURCE[0]:-} ]]; then exclude='*.md'; fi\n"
        "          cd android-emulator-skill\n          zip -r",
        '-x "*.DS_Store"',
        '-x "*.DS_Store" -x "$exclude"',
    ),
    "a file is rewritten before it is packed": _edit_release(
        "          cd android-emulator-skill\n          zip -r",
        "          cd android-emulator-skill\n          printf changed > assets/icon.svg\n"
        "          zip -r",
    ),
}


# The wrong packages the job's own step passes, because it reads the plugin
# directory after the steps before it: here the directory and the zip agree.
# Pinned, so that the limit is this set and no larger.
STEP_CANNOT_SEE = frozenset({"a file is rewritten before it is packed"})


@pytest.mark.parametrize("name", WRONG_PACKAGES)
def test_a_zip_step_that_packs_the_wrong_thing_is_told_apart(name, tmp_path, capsys):
    """By this harness, by the script given the plugin as committed, and by the job's own step."""
    text = WRONG_PACKAGES[name](RELEASE.read_text(encoding="utf-8"))
    built = _package(text, tmp_path, may_stop=True)
    assert _differences(built, _extract(built, tmp_path / "elsewhere" / "plugin")), name

    assert check_release_zip.main([str(built.archive), str(PACKAGE_ROOT)]) == 1
    assert f"FAILED: {built.archive.name}" in capsys.readouterr().out

    stopped_at = None if name in STEP_CANNOT_SEE else CHECK["name"]
    assert built.stopped_at == stopped_at, built.transcript


def test_the_jobs_own_check_passed_on_the_zip_and_says_what_it_compared(release):
    """The counts are the plugin's: a check that compared nothing would pass as well."""
    built, _ = release
    scripts = len([name for name in _scripts_on_disk() if name.endswith(".py")])
    assert built.stopped_at is None
    assert (
        f"ok: {built.archive.name} holds the {len(built.wanted)} files of {PACKAGE_ROOT.name} "
        f"and no other file; {scripts} scripts answer --help from the extracted copy."
    ) in built.transcript


def test_a_job_stops_at_the_step_that_fails_and_runs_none_after_it(tmp_path):
    """As on a runner. The step that fails here is before the zip step, which would leave a zip."""
    text = _edit_release(
        '          echo "Validating android-emulator-skill/ package structure..."\n',
        "          exit 3\n",
    )(RELEASE.read_text(encoding="utf-8"))
    built = _package(text, tmp_path / "may", may_stop=True)
    assert built.stopped_at == "Validate skill structure"
    assert built.transcript.count("## step ") == 1 and ": exit 3\n" in built.transcript
    assert not built.archive.exists(), "a step after the one that failed was run"

    with pytest.raises(AssertionError, match="Validate skill structure"):
        _package(text, tmp_path / "may-not")


def _check_step_problems(text: str) -> list[str]:
    """Why a package job does not check its zip as its last act before uploading it. Empty: it does.

    The two last steps are compared whole with what is written out above, so
    nothing runs between the check and the upload, and nothing after.
    """
    steps = _yaml_with_the_keys_github_reads(text)["jobs"][PACKAGE_JOB]["steps"]
    found = []
    if len(steps) < 2 or not _written_alike(steps[-2], CHECK):
        found.append("the step before the last is not the check written out in `CHECK`")
    last = {key: value for key, value in steps[-1].items() if key != "name"}
    if not _written_alike(last, UPLOAD):
        found.append("the last step is not the upload written out in `UPLOAD`")
    return found


def test_the_package_job_checks_the_zip_as_its_last_step_before_the_upload():
    assert _check_step_problems(RELEASE.read_text(encoding="utf-8")) == []
    name, script = _modelled(RELEASE.read_text(encoding="utf-8"))[0][-1]
    assert name == CHECK["name"]
    assert script.split() == [
        "python3",
        CHECKER_FILES[0],
        f"android-emulator-skill-{TAG}.zip",
        PACKAGE_ROOT.name,
    ]


_CHECK_STEP = f"      - name: {CHECK['name']}\n"
_CHECK_RUN = f"        run: {CHECK['run']}\n"
_ZIP_STEP = "      - name: Create skill package\n"
# Each is a job whose last two steps are not the check and the upload as they
# are written out. All but the last would upload a zip the check did not pass,
# or did not see. The last changes nothing that runs: the step is compared
# whole, and its name is how a transcript and `stopped_at` say which step it was.
UNCHECKED_UPLOADS = {
    "the check is deleted": _edit_release(f"{_CHECK_STEP}{_CHECK_RUN}", ""),
    "the check runs before the zip is made": _edit_release(
        f"{_CHECK_STEP}{_CHECK_RUN}", "", _ZIP_STEP, f"{_CHECK_STEP}{_CHECK_RUN}\n{_ZIP_STEP}"
    ),
    "a step runs between the check and the upload": _edit_release(
        f"{_CHECK_STEP}{_CHECK_RUN}",
        f"{_CHECK_STEP}{_CHECK_RUN}\n"
        "      - name: Pack again\n        run: zip -r again.zip android-emulator-skill\n",
    ),
    "the check reads another file": _edit_release(
        _CHECK_RUN, _CHECK_RUN.replace("android-emulator-skill-${{", "other-${{")
    ),
    "the check reads another directory": _edit_release(
        _CHECK_RUN, _CHECK_RUN.replace(".zip android-emulator-skill\n", ".zip tests\n")
    ),
    "the check's exit status is thrown away": _edit_release(
        _CHECK_RUN, _CHECK_RUN.replace("\n", " || true\n")
    ),
    "the job goes on when the check fails": _edit_release(
        _CHECK_STEP, f"{_CHECK_STEP}        continue-on-error: true\n"
    ),
    "the check is conditional": _edit_release(_CHECK_STEP, f"{_CHECK_STEP}        if: false\n"),
    "the check is given another name": _edit_release(_CHECK_STEP, "      - name: Check\n"),
}


@pytest.mark.parametrize("name", UNCHECKED_UPLOADS)
def test_a_job_whose_last_steps_are_not_the_check_and_the_upload_is_told_apart(name):
    text = UNCHECKED_UPLOADS[name](RELEASE.read_text(encoding="utf-8"))
    assert _check_step_problems(text), name


def _workflow_texts() -> dict[str, str]:
    """``{file name: text}`` of every workflow GitHub reads from this checkout."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(WORKFLOWS.iterdir())
        if path.suffix in (".yml", ".yaml")
    }


def _asset_writer_problems(workflows: dict[str, str]) -> list[str]:
    """Why something besides the package job could write a release's asset. Empty: nothing could.

    Two rules. The names of release.yml's jobs are the ones written out in
    `RELEASE_JOBS`. And the token GitHub makes for a job may not write the
    repository's contents, in any workflow, the package job's apart: the
    workflow lists its permissions, `contents` among them is not `write`,
    and no job sets permissions of its own but the package job, whose are
    `PACKAGE_PERMISSIONS` and nothing more. A workflow that lists none gets
    the repository's default, which is a setting and in no file, so it is
    refused.

    `contents` is the one scope held. Any other that a workflow lists for
    all its jobs is not read: pages.yml's token may write Pages.

    The limit: that token is all this reads. A step handed another one, as a
    secret, writes whatever that one may, and a person who may write the
    repository needs no workflow.
    """
    found = []
    for name, text in sorted(workflows.items()):
        workflow = _yaml_with_the_keys_github_reads(text)
        granted = workflow.get("permissions")
        if type(granted) is not dict:
            found.append(
                f"{name} does not list what its token may do: `permissions` is {granted!r}"
            )
        elif granted.get("contents", "none") not in ("read", "none"):
            found.append(f"{name} lets the token of every job write the repository's contents")
        for job, body in workflow["jobs"].items():
            own = body.get("permissions")
            if (name, job) == (RELEASE.name, PACKAGE_JOB):
                if not _written_alike(own, PACKAGE_PERMISSIONS):
                    found.append(
                        f"{name}, job `{job}`: `permissions` is {own!r}, not {PACKAGE_PERMISSIONS}"
                    )
            elif "permissions" in body:
                found.append(f"{name}, job `{job}`, sets permissions of its own: {own!r}")
        if name == RELEASE.name and sorted(workflow["jobs"]) != sorted(RELEASE_JOBS):
            found.append(
                f"the jobs of {name} are {sorted(workflow['jobs'])}, not {sorted(RELEASE_JOBS)}"
            )
    return found


def test_nothing_but_the_package_job_may_write_a_releases_asset():
    workflows = _workflow_texts()
    assert sorted(workflows) == sorted(
        path.name for path in WORKFLOWS.iterdir()
    ), "a file in the workflows directory that this guard did not read"
    assert RELEASE.name in workflows
    assert _asset_writer_problems(workflows) == []


def _in(name: str, change: Callable[[str], str]) -> Callable[[dict[str, str]], dict[str, str]]:
    """An edit to one workflow among them all."""
    return lambda workflows: {**workflows, name: change(workflows[name])}


_TOKEN = "\npermissions:\n  contents: read\n"
_PACKAGE_TOKEN = "    permissions:\n      contents: write\n"
# What the review of this module appended to release.yml, with every guard
# then green: the upload action puts a file over an asset of the same name.
# The workflow's token no longer writes contents, so here this job is the
# control of the job-set rule alone.
_REPLACING_JOB = (
    "\n  replace_asset:\n"
    "    name: Replace the asset\n"
    "    needs: package\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - name: Fetch\n"
    "        uses: actions/checkout@v4\n"
    "      - name: Pack an incomplete replacement\n"
    "        run: zip android-emulator-skill-${{ github.ref_name }}.zip README.md\n"
    "      - name: Replace\n"
    f"        uses: {UPLOAD['uses']}\n"
    "        with:\n"
    "          files: android-emulator-skill-${{ github.ref_name }}.zip\n"
)
_ANOTHER_WORKFLOW = (
    "name: After the release\n\non:\n  release:\n    types: [published]\n\n"
    "permissions:\n  contents: write\n\n"
    "jobs:\n  replace:\n    runs-on: ubuntu-latest\n    steps:\n"
    '      - run: gh release upload "$GITHUB_REF_NAME" other.zip --clobber\n'
)
# Each with the words of the one rule that refuses it, and it is the only
# thing found: a control refused for something else holds nothing.
OTHER_WRITERS = {
    "a job is added to the release, after the package job": (
        _in(RELEASE.name, lambda text: text + _REPLACING_JOB),
        f"the jobs of {RELEASE.name} are ",
    ),
    "the token of every job of the release may write, as it once could": (
        _in(RELEASE.name, _edit_release(_TOKEN, "\npermissions:\n  contents: write\n")),
        f"{RELEASE.name} lets the token of every job write",
    ),
    "the release grants every permission in one word": (
        _in(RELEASE.name, _edit_release(_TOKEN, "\npermissions: write-all\n")),
        f"{RELEASE.name} does not list what its token may do: `permissions` is 'write-all'",
    ),
    "a workflow says nothing of its token": (
        _in("test.yml", _edit_release(_TOKEN, "\n")),
        "test.yml does not list what its token may do: `permissions` is None",
    ),
    "a gate of the release is given the write": (
        _in(
            RELEASE.name,
            _edit_release(
                "    name: Version consistency\n",
                f"    name: Version consistency\n{_PACKAGE_TOKEN}",
            ),
        ),
        f"{RELEASE.name}, job `version`, sets permissions of its own",
    ),
    "the package job may write more than the repository's contents": (
        _in(
            RELEASE.name,
            _edit_release(_PACKAGE_TOKEN, f"{_PACKAGE_TOKEN}      actions: write\n"),
        ),
        f"{RELEASE.name}, job `package`: `permissions` is {{'contents': 'write', 'actions'",
    ),
    "the package job cannot upload": (
        _in(RELEASE.name, _edit_release(_PACKAGE_TOKEN, "")),
        f"{RELEASE.name}, job `package`: `permissions` is None",
    ),
    "a second workflow on the same trigger may write": (
        lambda workflows: {**workflows, "after-release.yml": _ANOTHER_WORKFLOW},
        "after-release.yml lets the token of every job write",
    ),
    "a job of another workflow is given the write": (
        _in(
            "validate-version.yml",
            _edit_release(
                "    name: Validate version consistency\n",
                f"    name: Validate version consistency\n{_PACKAGE_TOKEN}",
            ),
        ),
        "validate-version.yml, job `validate`, sets permissions of its own",
    ),
}


@pytest.mark.parametrize("name", OTHER_WRITERS)
def test_another_writer_of_the_asset_is_refused_by_the_rule_it_breaks(name):
    change, said = OTHER_WRITERS[name]
    found = _asset_writer_problems(change(_workflow_texts()))
    assert len(found) == 1 and said in found[0], found


_UPLOAD_STEP = "      - name: Upload package to release\n"
_LAST_UPLOAD_INPUT = "          fail_on_unmatched_files: true\n"
_UPLOAD_USES = f"        uses: {UPLOAD['uses']}"
_UPLOAD_FILES = "          files: android-emulator-skill-${{ github.ref_name }}.zip\n"
_PACKAGE_CHECKOUT = (
    "      - name: Checkout repository\n        uses: actions/checkout@v4\n\n"
    "      - name: Validate skill structure\n"
)
NOT_THE_UPLOAD = "a last step that is not the upload written out in `UPLOAD`"
NOT_THE_CHECKOUT = "a first step that is not the checkout written out in `CHECKOUT`"
# Each with the words the refusal must say, so that it is refused for this.
UNMODELLED_PACKAGE_JOBS = {
    "the workflow sets variables for every step": (
        _edit_release("\njobs:\n", "\nenv:\n  PACKAGE_EXCLUDE: '*.md'\n\njobs:\n"),
        "workflow key `env`",
    ),
    "the triggers are under a key GitHub does not read": (
        _edit_release("\non:\n", "\ntrue:\n"),
        "workflow key `True`",
    ),
    "the job runs in a container": (
        _edit_release(
            "    needs: [verify, emulator, version, install]\n",
            "    needs: [verify, emulator, version, install]\n    container: alpine:3\n",
        ),
        "job key `container`",
    ),
    "the job runs on another system": (
        _edit_release(
            "    runs-on: ubuntu-latest\n    needs: [verify, emulator, version, install]\n",
            "    runs-on: macos-latest\n    needs: [verify, emulator, version, install]\n",
        ),
        "`runs-on: macos-latest`",
    ),
    "the checkout is of another ref": (
        _edit_release(
            _PACKAGE_CHECKOUT,
            _PACKAGE_CHECKOUT.replace("@v4\n", "@v4\n        with:\n          ref: main\n"),
        ),
        NOT_THE_CHECKOUT,
    ),
    "the checkout is written as a merge": (
        _edit_release(
            _PACKAGE_CHECKOUT,
            "      - <<: {name: Checkout repository, uses: actions/checkout@v4}\n\n"
            "      - name: Validate skill structure\n",
        ),
        "a merge key (`<<`)",
    ),
    "the zip step runs in another directory": (
        _edit_release(_ZIP_STEP, f"{_ZIP_STEP}        working-directory: tests\n"),
        "step 'Create skill package' with keys ['name', 'run', 'working-directory']",
    ),
    "the zip step is conditional": (
        _edit_release(_ZIP_STEP, f"{_ZIP_STEP}        if: false\n"),
        "step 'Create skill package' with keys ['if', 'name', 'run']",
    ),
    "a step hands the zip step a variable through the runner's file": (
        _edit_release(
            _ZIP_STEP,
            "      - name: Choose what to leave out\n"
            "        run: printf 'PACKAGE_EXCLUDE=*.md\\n' >> \"${GITHUB_ENV:-/dev/null}\"\n\n"
            f"{_ZIP_STEP}",
        ),
        "['GITHUB_ENV'] in the script of step 'Choose what to leave out'",
    ),
    "a step puts another zip first on the path": (
        _edit_release(
            _ZIP_STEP,
            f'      - name: Add tools\n        run: echo "$PWD/tools" >> $GITHUB_PATH\n\n{_ZIP_STEP}',
        ),
        "['GITHUB_PATH'] in the script of step 'Add tools'",
    ),
    "another action runs before the upload": (
        _edit_release(_ZIP_STEP, f"      - uses: actions/download-artifact@v4\n\n{_ZIP_STEP}"),
        "with keys ['uses'] and no name that is plain text",
    ),
    "the upload may match nothing": (
        _edit_release("fail_on_unmatched_files: true", "fail_on_unmatched_files: false"),
        NOT_THE_UPLOAD,
    ),
    "an upload input is `yes`, which the action does not read as true": (
        _edit_release("fail_on_unmatched_files: true", "fail_on_unmatched_files: yes"),
        NOT_THE_UPLOAD,
    ),
    "an upload input is `1`, which Python calls equal to true": (
        _edit_release("fail_on_unmatched_files: true", "fail_on_unmatched_files: 1"),
        NOT_THE_UPLOAD,
    ),
    "a step's name is an expression nobody can fill": (
        _edit_release(_ZIP_STEP, "      - name: ${{ missing.value }}\n"),
        "with keys ['name', 'run'] and no name that is plain text",
    ),
    "an upload input is an expression nobody filled": (
        _edit_release(
            "generate_release_notes: true", "generate_release_notes: ${{ missing.value }}"
        ),
        NOT_THE_UPLOAD,
    ),
    "the upload names two files, as the action splits a comma": (
        _edit_release(
            _UPLOAD_FILES,
            "          files: dist,final/android-emulator-skill-${{ github.ref_name }}.zip\n",
        ),
        NOT_THE_UPLOAD,
    ),
    "the upload names a pattern": (
        _edit_release(_UPLOAD_FILES, "          files: '*.zip'\n"),
        NOT_THE_UPLOAD,
    ),
    "the upload looks for the file somewhere else": (
        _edit_release(
            _LAST_UPLOAD_INPUT, f"{_LAST_UPLOAD_INPUT}          working_directory: alternate\n"
        ),
        NOT_THE_UPLOAD,
    ),
    "the upload is conditional": (
        _edit_release(_UPLOAD_STEP, f"{_UPLOAD_STEP}        if: false\n"),
        NOT_THE_UPLOAD,
    ),
    "the upload is another version of the action": (
        _edit_release(_UPLOAD_USES, f"        uses: softprops/action-gh-release@{'a' * 40}"),
        NOT_THE_UPLOAD,
    ),
    "the upload has no name": (
        _edit_release(f"{_UPLOAD_STEP}{_UPLOAD_USES}", f"      - uses: {UPLOAD['uses']}"),
        "with keys ['uses', 'with'] and no name that is plain text",
    ),
    "a second upload follows the first": (
        _edit_release(
            _LAST_UPLOAD_INPUT,
            f"{_LAST_UPLOAD_INPUT}\n      - name: Upload another\n"
            f"{_UPLOAD_USES}\n        with:\n"
            f"          files: other.zip\n          generate_release_notes: false\n"
            f"{_LAST_UPLOAD_INPUT}",
        ),
        "step 'Upload package to release' with keys ['name', 'uses', 'with']",
    ),
    "a step after the upload replaces the asset": (
        _edit_release(
            _LAST_UPLOAD_INPUT,
            f"{_LAST_UPLOAD_INPUT}\n      - name: Upload by hand\n"
            "        run: gh release upload v1 other.zip --clobber\n",
        ),
        NOT_THE_UPLOAD,
    ),
    "a script uses an expression the harness has no value for": (
        _edit_release("ls -lh android-emulator-skill-", "ls -lh ${{ github.sha }}-"),
        "the harness has no value for ${{ github.sha }}",
    ),
}


def test_an_edit_is_made_where_its_anchor_stands_once_when_it_is_made():
    assert _edit_release("a", "b")("xay") == "xby"
    with pytest.raises(AssertionError, match="exactly once"):
        _edit_release("a", "b", "b", "c")("ab")
    with pytest.raises(AssertionError, match="exactly once"):
        _edit_release("a", "b")("aa")
    with pytest.raises(ValueError, match="zip"):
        _edit_release("a", "b", "c")


@pytest.mark.parametrize("name", UNMODELLED_PACKAGE_JOBS)
def test_a_package_job_the_harness_does_not_model_is_refused(name, tmp_path):
    """Rather than run a job it would run differently from GitHub and call the result the zip."""
    change, said = UNMODELLED_PACKAGE_JOBS[name]
    text = change(RELEASE.read_text(encoding="utf-8"))
    with pytest.raises(AssertionError, match=re.escape(said)):
        _package(text, tmp_path)
    assert not (tmp_path / "checkout").exists(), "the job was refused after something had run"


def test_every_packaged_script_starts_by_its_absolute_path_from_another_directory(
    release, tmp_path
):
    """The invocation SKILL.md documents, against the files the zip holds.

    Each Python script is asked for `--help` by absolute path, with a working
    directory that is not the skill's. Of this environment it is given PATH and
    nothing else (`script_problems` adds `PYTHONDONTWRITEBYTECODE=1`), so the
    modules it imports are the ones extracted beside it.
    """
    _, extracted = release
    env = {"PATH": os.environ["PATH"]}
    ran, problems = verify_install.script_problems(_skill(extracted), tmp_path, env, "extracted")
    assert problems == []
    assert ran == len([name for name in _scripts_on_disk() if name.endswith(".py")])

    for script in sorted((_skill(extracted) / "scripts").glob("*.sh")):
        helped = subprocess.run(
            ["bash", str(script), "--help"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert helped.returncode == 0 and helped.stdout.strip(), (script.name, helped.stderr)


def test_a_script_extracted_without_its_shared_modules_does_not_start(release, tmp_path):
    """The control for the test above: it is the extracted copy that runs, not this checkout's."""
    _, extracted = release
    partial = tmp_path / "plugin"
    shutil.copytree(extracted, partial)
    shutil.rmtree(_skill(partial) / "scripts" / "common")
    _, problems = verify_install.script_problems(
        _skill(partial), tmp_path, {"PATH": os.environ["PATH"]}
    )
    assert problems, "every script answered --help with scripts/common deleted from its copy"


def _installed_root(extracted: Path, doc: Path) -> Path:
    """What arrives with a document: the skill directory for one inside it, else the plugin."""
    return _skill(extracted) if _skill(extracted) in doc.parents else extracted


def _broken_links(extracted: Path) -> dict[str, list[str]]:
    broken = {}
    for doc in sorted(extracted.rglob("*.md")):
        text = doc.read_text(encoding="utf-8")
        wrong = _unreadable_links(text) + _links_that_do_not_ship(
            doc, text, _installed_root(extracted, doc)
        )
        if wrong:
            broken[doc.relative_to(extracted).as_posix()] = wrong
    return broken


def test_every_relative_link_in_the_extracted_documents_names_a_file_the_zip_holds(
    release, tmp_path
):
    _, extracted = release
    documents = sorted(doc.relative_to(extracted).as_posix() for doc in extracted.rglob("*.md"))
    assert documents == sorted(
        doc.relative_to(PACKAGE_ROOT).as_posix() for doc in _packaged_markdown()
    )
    assert _broken_links(extracted) == {}

    # The control: a file the documents link, taken out of a copy, is missed.
    partial = tmp_path / "plugin"
    shutil.copytree(extracted, partial)
    (_skill(partial) / "references" / "known-gaps.md").unlink()
    assert "references/known-gaps.md" in _broken_links(partial)[f"skills/{SKILL}/SKILL.md"]


@pytest.mark.parametrize("surface", [CLAUDE, CODEX], ids=lambda surface: surface.agent)
def test_the_extracted_zip_is_a_plugin_the_agent_can_read(release, surface):
    """Its manifest parses, every path the manifest writes is in the zip, and the skill is whole."""
    _, extracted = release
    manifest = json.loads((extracted / surface.manifest).read_text(encoding="utf-8"))
    assert manifest["name"] == PACKAGE_ROOT.name
    assert _path_problems(manifest, extracted) == []
    missing = [name for name, kind in SHIPPED if not kind(_skill(extracted) / name)]
    assert not missing, missing


def _zipped(root: Path, files: dict[str, str], beside: tuple[str, ...] = ()) -> tuple[Path, Path]:
    """``(a zip, the directory it was made from)`` for a plugin of a few files.

    `beside` names files the directory has and the zip has not.
    """
    plugin = root / "plugin"
    plugin.mkdir(parents=True)
    archive = root / "plugin.zip"
    with zipfile.ZipFile(archive, "w") as held:
        for relative, text in files.items():
            (plugin / relative).parent.mkdir(parents=True, exist_ok=True)
            (plugin / relative).write_text(text, encoding="utf-8")
            held.write(plugin / relative, relative)
    for relative in beside:
        (plugin / relative).parent.mkdir(parents=True, exist_ok=True)
        (plugin / relative).write_bytes(b"\x00junk")
    return archive, plugin


_SCRIPT = f"skills/{SKILL}/scripts/answers.py"
_FINDERS = f"skills/{SKILL}/scripts/.DS_Store"
# Answers `--help` unless Finder's file is beside it. A plugin directory can
# have that file and its zip cannot, so the script tells the two copies apart.
_ANSWERS = (
    "import argparse\nimport sys\nfrom pathlib import Path\n\n"
    "if (Path(__file__).parent / '.DS_Store').exists():\n    sys.exit(7)\n"
    "argparse.ArgumentParser().parse_args()\n"
)


def test_the_check_starts_the_scripts_of_the_extracted_copy(tmp_path, capsys):
    """Not the directory's, where the one script exits 7 and the files of the two agree."""
    files = {_SCRIPT: _ANSWERS, "README.md": "text\n"}
    archive, plugin = _zipped(tmp_path / "starts", files, beside=(_FINDERS,))
    _, in_the_directory = verify_install.script_problems(
        plugin / "skills" / SKILL, tmp_path, {"PATH": os.environ["PATH"]}
    )
    assert len(in_the_directory) == 1 and "--help exited 7" in in_the_directory[0]
    assert check_release_zip.main([str(archive), str(plugin)]) == 0
    assert "holds the 2 files" in capsys.readouterr().out

    archive, plugin = _zipped(tmp_path / "raises", {_SCRIPT: "raise SystemExit(3)\n"})
    assert check_release_zip.main([str(archive), str(plugin)]) == 1
    assert "answers.py --help exited 3 from the extracted copy" in capsys.readouterr().out

    archive, plugin = _zipped(tmp_path / "none", {"README.md": "text\n"})
    assert check_release_zip.main([str(archive), str(plugin)]) == 1
    assert "holds no Python script" in capsys.readouterr().out


def test_the_check_fails_where_it_could_not_look(release, tmp_path, monkeypatch, capsys):
    """A zip or a directory it cannot read is a failure, and so is a Python the scripts refuse."""
    built, extracted = release
    not_a_zip = tmp_path / "not-a.zip"
    not_a_zip.write_text("text", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    for archive, plugin, said in (
        (tmp_path / "no-such.zip", extracted, "`unzip` exited"),
        (not_a_zip, extracted, "`unzip` exited"),
        (built.archive, tmp_path / "no-such-directory", "could not all be read"),
        (built.archive, tmp_path / "empty", "holds no file"),
    ):
        assert check_release_zip.main([str(archive), str(plugin)]) == 1
        assert said in capsys.readouterr().out

    with pytest.raises(SystemExit) as stopped:
        check_release_zip.main([str(built.archive)])
    assert stopped.value.code == 2
    monkeypatch.setattr(check_release_zip.sys, "version_info", (3, 11, 9))
    assert check_release_zip.main([str(built.archive), str(extracted)]) == 2
    assert "need Python 3.12+" in capsys.readouterr().out
