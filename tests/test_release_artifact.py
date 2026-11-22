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
files. It is not a defence against a step written to tell the two apart;
that needs the check to run on the runner, on the file about to be uploaded.
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
JOB_KEYS = frozenset({"name", "runs-on", "needs", "steps"})
RUNNER = "ubuntu-latest"
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


def _is_junk(relative: str) -> bool:
    parts = relative.split("/")
    return "__pycache__" in parts or relative.endswith(".pyc") or parts[-1] == ".DS_Store"


@dataclasses.dataclass(frozen=True)
class Built:
    """One run of the package job: where it ran, the file it would upload, what that should hold."""

    checkout: Path
    archive: Path
    transcript: str
    # ``{relative path: sha256}`` of the plugin directory less its junk, taken
    # before any step of the job ran.
    wanted: dict[str, str]

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


def _package(text: str, root: Path) -> Built:
    """Run the package job of a release workflow on tag `TAG`, up to its upload step."""
    scripts, uploaded = _modelled(text)
    checkout, wanted = _checkout(root)
    transcript: list[str] = []
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
        assert ran.returncode == 0, "\n".join(transcript)
    return Built(checkout, checkout / uploaded, "\n".join(transcript), wanted)


def _extract(built: Built, destination: Path) -> Path:
    """Unpack the zip elsewhere, with the tool a person would use."""
    destination.mkdir(parents=True)
    subprocess.run(["unzip", "-q", str(built.archive), "-d", str(destination)], check=True)
    return destination


def _differences(built: Built, extracted: Path) -> list[str]:
    """How the extracted zip differs from the plugin directory less its junk. Empty: it does not."""
    wanted, have = built.wanted, verify_install.files_under(extracted)
    found = [f"missing: {name}" for name in sorted(wanted.keys() - have.keys())]
    found += [f"not the plugin's: {name}" for name in sorted(have.keys() - wanted.keys())]
    found += [
        f"other bytes: {name}"
        for name in sorted(wanted.keys() & have.keys())
        if wanted[name] != have[name]
    ]
    with zipfile.ZipFile(built.archive) as archive:
        found += [
            f"junk entry: {name}" for name in archive.namelist() if _is_junk(name.rstrip("/"))
        ]
    return found


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


@pytest.mark.parametrize("name", WRONG_PACKAGES)
def test_a_zip_step_that_packs_the_wrong_thing_is_told_apart(name, tmp_path):
    built = _package(WRONG_PACKAGES[name](RELEASE.read_text(encoding="utf-8")), tmp_path)
    assert _differences(built, _extract(built, tmp_path / "elsewhere" / "plugin")), name


_UPLOAD_STEP = "      - name: Upload package to release\n"
_LAST_UPLOAD_INPUT = "          fail_on_unmatched_files: true\n"
_ZIP_STEP = "      - name: Create skill package\n"
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
    directory that is not the skill's and an environment that is PATH alone,
    so the modules it imports are the ones extracted beside it.
    """
    _, extracted = release
    env = {"PATH": os.environ["PATH"]}
    ran, problems = verify_install.script_problems(_skill(extracted), tmp_path, env)
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
