"""The instrumented results root: where AGP writes connected-test XML, and nothing else.

``gradle/results.py`` and ``gradle/builder.py`` were hard-wired to the unit
root, ``<module>/build/test-results/<task>/``. AGP 8.13.2 writes a connected
run's JUnit XML under ``<module>/build/outputs/androidTest-results/connected/``
instead (tests/test_instrumented_ground_truth.py pins that), so adding the
task alone would find no report. Every layout below is a RECORDED tree:

* ``instrumented_results_tree`` -- an unflavored variant, ``connected/debug/``;
* ``instrumented_flavored_results_tree`` -- variant ``proDebug``, written to
  ``connected/debug/flavors/pro/``;
* ``instrumented_no_match_tree`` -- a class filter that matched nothing.

The trees are rebuilt on disk from those listings: directories as listed,
each XML the run's recorded report, every other file empty (only its name and
place matter to discovery and cleanup).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import gradle.builder as builder_mod
from common import cli
from gradle import BuildRunner
from gradle.results import (
    INSTRUMENTED_RESULTS,
    UNIT_RESULTS,
    find_test_result_files,
)

RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded" / "gradle-8.13"
CONNECTED = Path("app", "build", "outputs", "androidTest-results", "connected")
# Any serial: Gradle is mocked here, and the device is the CLI's concern.
SERIAL = "emulator-5554"

# run -> (its tree listing, its recorded report)
RUNS = {
    "connected_android_test_success": (
        "instrumented_results_tree",
        "junit_xml_instrumented_passing",
    ),
    "connected_android_test_flavored": (
        "instrumented_flavored_results_tree",
        "junit_xml_instrumented_flavored",
    ),
    "connected_android_test_no_match": (
        "instrumented_no_match_tree",
        "junit_xml_instrumented_no_match",
    ),
}


def _entry(name: str) -> dict:
    fixtures = json.loads((RECORDED / "MANIFEST.json").read_text(encoding="utf-8"))["fixtures"]
    (entry,) = [entry for entry in fixtures if entry["name"] == name]
    return entry


def _task(run: str) -> str:
    """The connected task the run's recorded command named, unqualified."""
    (task,) = [arg for arg in _entry(run)["command"].split() if arg.startswith(":app:connected")]
    return task.removeprefix(":app:")


def _variant(run: str) -> str:
    return _task(run).removeprefix("connected").removesuffix("AndroidTest")


def _materialize(project: Path, run: str, recorded_gradle, device: str | None = None) -> Path:
    """Rebuild ``run``'s recorded results tree under ``project``; return its report.

    ``device`` renames the recorded device (an AVD name) throughout, as a run
    on another emulator would name its files.
    """
    tree, xml = RUNS[run]
    results_path = _entry(run)["results_path"]
    report = None
    for line in recorded_gradle(tree).splitlines():
        relative = line if device is None else _rename(line, results_path, device)
        target = project / relative
        if line.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if line == results_path:
            target.write_text(recorded_gradle(xml), encoding="utf-8")
            report = target
        else:
            target.touch()
    assert report is not None, f"{tree} does not list {results_path}"
    return report


def _recorded_device(results_path: str) -> str:
    """The device in AGP's report name: ``TEST-<device>-_app-<flavor>.xml``."""
    name = Path(results_path).name
    return name.removeprefix("TEST-").rsplit("-_app-", 1)[0]


def _rename(line: str, results_path: str, device: str) -> str:
    return line.replace(_recorded_device(results_path), device)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "app").mkdir(parents=True)
    (project / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    return project


@pytest.fixture(autouse=True)
def _gradle_answers_the_conventional_project_dir(monkeypatch, recorded_gradle):
    """A --module run first asks Gradle where the module is: the recorded answer, app/."""
    answer = recorded_gradle("gradle_properties_project_dir")

    def query(command, cwd, timeout):
        return subprocess.CompletedProcess(
            command, 0, stdout=answer.replace("/scaffold", str(cwd)), stderr=""
        )

    monkeypatch.setattr(builder_mod, "_gradle_query", query)


def _gradle(monkeypatch, returncode: int, log: str, writes=(), seen=None) -> list:
    """Mock gradlew: note what is on disk when it starts, write ``writes``, exit.

    Returns the list of commands it was started with.
    """
    started: list = []

    def fake_run(cmd, cwd, **kwargs):
        started.append((cmd, kwargs))
        if seen is not None:
            seen.update(p for p in Path(cwd).rglob("*") if p.is_file())
        for path, text in writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, stdout=log, stderr="")

    monkeypatch.setattr(builder_mod.subprocess, "run", fake_run)
    return started


def _run(project: Path, variant: str = "debug", module: str | None = ":app"):
    """One instrumented run through the builder's verdict path."""
    runner = BuildRunner(str(project), module=module, variant=variant)
    return runner.instrumented_test(SERIAL)


# --- discovery --------------------------------------------------------------


@pytest.mark.parametrize("run", list(RUNS))
@pytest.mark.parametrize("scoped", [True, False], ids=["module", "project"])
def test_the_instrumented_root_finds_the_report_where_agp_wrote_it(
    run, scoped, tmp_path: Path, recorded_gradle
):
    report = _materialize(tmp_path, run, recorded_gradle)
    search = tmp_path / "app" if scoped else tmp_path

    found = find_test_result_files(
        search, task=_task(run), recursive=not scoped, root=INSTRUMENTED_RESULTS
    )

    # The report only: not the per-device directory's logcat, .pb or textproto.
    assert found == [report]


@pytest.mark.parametrize("run", list(RUNS))
def test_the_unit_root_is_unchanged_and_still_misses_it(run, tmp_path: Path, recorded_gradle):
    _materialize(tmp_path, run, recorded_gradle)

    assert find_test_result_files(tmp_path, task=_task(run)) == []
    assert find_test_result_files(tmp_path, task=_task(run), root=UNIT_RESULTS) == []


def test_the_instrumented_root_does_not_read_the_unit_roots_directory(
    tmp_path: Path, recorded_gradle
):
    """A report in build/test-results/<task>/ is not an instrumented result."""
    unit = tmp_path / "app" / "build" / "test-results" / "connectedDebugAndroidTest"
    unit.mkdir(parents=True)
    (unit / "TEST-com.example.PassingTest.xml").write_text(
        recorded_gradle("junit_xml_instrumented_passing"), encoding="utf-8"
    )

    for scoped in (True, False):
        search = tmp_path / "app" if scoped else tmp_path
        assert (
            find_test_result_files(
                search,
                task="connectedDebugAndroidTest",
                recursive=not scoped,
                root=INSTRUMENTED_RESULTS,
            )
            == []
        )


def test_an_unflavored_task_does_not_read_a_flavored_report_below_its_name(
    tmp_path: Path, recorded_gradle
):
    """connected/debug/ is also the flavored layout's parent: only its own reports are debug's."""
    _materialize(tmp_path, "connected_android_test_flavored", recorded_gradle)

    assert (
        find_test_result_files(
            tmp_path / "app",
            task="connectedDebugAndroidTest",
            recursive=False,
            root=INSTRUMENTED_RESULTS,
        )
        == []
    )


def test_a_flavored_task_does_not_read_its_sibling_flavor(tmp_path: Path, recorded_gradle):
    """proDebug reads flavors/pro/ only; the same recorded tree under flavors/free/ is not its."""
    report = _materialize(tmp_path, "connected_android_test_flavored", recorded_gradle)
    free = report.parent.parent / "free"
    free.mkdir()
    (free / report.name.replace("-pro.xml", "-free.xml")).write_text(
        recorded_gradle("junit_xml_instrumented_failing"), encoding="utf-8"
    )

    found = find_test_result_files(
        tmp_path / "app",
        task="connectedProDebugAndroidTest",
        recursive=False,
        root=INSTRUMENTED_RESULTS,
    )
    assert found == [report]


def test_a_miscased_variant_finds_agps_spelling(tmp_path: Path, recorded_gradle):
    """Gradle resolves a miscased task; AGP writes the variant's real spelling."""
    report = _materialize(tmp_path, "connected_android_test_flavored", recorded_gradle)

    found = find_test_result_files(
        tmp_path / "app",
        task="connectedPRODEBUGAndroidTest",
        recursive=False,
        root=INSTRUMENTED_RESULTS,
    )
    assert found == [report]


# --- the verdict, through the builder ---------------------------------------


@pytest.mark.parametrize(
    ("run", "log", "returncode", "success"),
    [
        ("connected_android_test_success", "connected_android_test_success", 0, True),
        ("connected_android_test_flavored", "connected_android_test_flavored", 0, True),
    ],
)
def test_a_green_connected_run_passes_on_the_report_it_wrote(
    run, log, returncode, success, tmp_path: Path, monkeypatch, recorded_gradle
):
    project = _project(tmp_path)
    scratch = tmp_path / "scratch"
    written = _materialize(scratch, run, recorded_gradle)
    target = project / written.relative_to(scratch)
    _gradle(monkeypatch, returncode, recorded_gradle(log), writes=[(target, written.read_text())])

    result = _run(project, variant=_variant(run))

    assert result.success is success
    assert result.test_results["passed"] == result.test_results["total"] >= 1
    assert result.results_dir == str(target.parent)


def test_a_red_connected_run_names_the_failing_test(tmp_path: Path, monkeypatch, recorded_gradle):
    project = _project(tmp_path)
    target = project / _entry("connected_android_test_failure")["results_path"]
    _gradle(
        monkeypatch,
        1,
        recorded_gradle("connected_android_test_failure"),
        writes=[(target, recorded_gradle("junit_xml_instrumented_failing"))],
    )

    result = _run(project)

    assert result.success is False
    assert [t["test_name"] for t in result.test_results["failed_tests"]] == [
        "com.example.composefixture.FailingInstrumentedTest.failsOnPurpose"
    ]


@pytest.mark.parametrize("module", [":app", None], ids=["module", "project"])
def test_a_previous_runs_passing_report_is_not_this_runs_result(
    module, tmp_path: Path, monkeypatch, recorded_gradle
):
    """The stale-report deletion, on the instrumented root.

    A previous green run's tree is on disk; this run exits 0 and writes no
    report. Read without the deletion, the old report is "1/1 passed".
    """
    project = _project(tmp_path)
    stale = _materialize(project, "connected_android_test_success", recorded_gradle)
    _gradle(monkeypatch, 0, recorded_gradle("connected_android_test_success"))

    result = _run(project, module=module)

    assert (
        result.success is False
    ), f"a previous run's report passed this one: {result.test_results}"
    assert result.test_results is None
    assert result.no_test_results is True
    assert not stale.exists(), "the previous run's report survived the pre-run delete"


def test_the_pre_run_delete_touches_only_the_variants_reports(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """Gone when Gradle starts: the variant's TEST-*.xml. Kept: everything else.

    Deleted: the variant's report, one from a device this run is not on, and
    those under its flavors/<f>/ (a ``debug`` run in a flavored project writes
    there, so whatever is there afterwards must be this run's). Kept: the
    per-device UTP directory and every .pb beside a report, another variant's
    report, and the same variant in another module.
    """
    project = _project(tmp_path)
    (project / "lib").mkdir()
    previous = _materialize(project, "connected_android_test_success", recorded_gradle)
    other_device = _materialize(
        project, "connected_android_test_success", recorded_gradle, device="Other(AVD) - 14"
    )
    flavored = _materialize(project, "connected_android_test_flavored", recorded_gradle)
    release = project / CONNECTED / "release" / previous.name
    release.parent.mkdir(parents=True)
    release.write_text(previous.read_text(), encoding="utf-8")
    lib = project / "lib" / previous.relative_to(project / "app")
    lib.parent.mkdir(parents=True)
    lib.write_text(previous.read_text(), encoding="utf-8")
    deleted = (previous, other_device, flavored)
    kept_before = {p for p in project.rglob("*") if p.is_file() and p not in deleted}
    seen: set[Path] = set()
    _gradle(monkeypatch, 1, recorded_gradle("connected_android_test_failure"), seen=seen)

    _run(project)

    assert not set(deleted) & seen, "a stale report reached the run"
    assert kept_before <= seen, sorted(map(str, kept_before - seen))
    assert release in seen and lib in seen
    assert any(p.suffix == ".pb" for p in seen) and any(p.name == "test-result.pb" for p in seen)


def test_a_flavored_run_leaves_its_sibling_flavor_and_its_build_type_alone(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """proDebug clears debug/flavors/pro/ only: not free's, not debug/'s own."""
    project = _project(tmp_path)
    pro = _materialize(project, "connected_android_test_flavored", recorded_gradle)
    free = pro.parent.parent / "free" / pro.name.replace("-pro.xml", "-free.xml")
    free.parent.mkdir()
    free.write_text(pro.read_text(), encoding="utf-8")
    unflavored = project / CONNECTED / "debug" / pro.name
    unflavored.write_text(pro.read_text(), encoding="utf-8")
    seen: set[Path] = set()
    _gradle(monkeypatch, 0, recorded_gradle("connected_android_test_flavored"), seen=seen)

    _run(project, variant="proDebug")

    assert pro not in seen
    assert free in seen and unflavored in seen


@pytest.mark.parametrize("module", [":app", None], ids=["module", "project"])
def test_no_report_names_the_connected_directory(
    module, tmp_path: Path, monkeypatch, recorded_gradle
):
    project = _project(tmp_path)
    _gradle(monkeypatch, 0, recorded_gradle("connected_android_test_success"))

    result = _run(project, module=module)

    assert result.no_test_results is True
    connected = project / CONNECTED
    expected = connected if module else project / "**" / CONNECTED.relative_to("app")
    assert result.results_dir == str(expected)


# --- the refusals, on the instrumented tree ---------------------------------

UNFLAVORED_COMPONENTS = [
    "build",
    "build/outputs",
    "build/outputs/androidTest-results",
    "build/outputs/androidTest-results/connected",
    "build/outputs/androidTest-results/connected/debug",
]
FLAVORED_COMPONENTS = [
    "build/outputs/androidTest-results/connected/debug/flavors",
    "build/outputs/androidTest-results/connected/debug/flavors/pro",
]


def _redirect(project: Path, run: str, component: str, recorded_gradle, outside: bool) -> Path:
    """Put ``run``'s recorded tree behind a symlinked ``app/<component>``; return its report.

    The real directory lives elsewhere -- outside the project, or in an
    in-project decoy -- and ``app/<component>`` points at it, so the report is
    reachable only through the link.
    """
    scratch = project.parent / "scratch"
    report = _materialize(scratch, run, recorded_gradle)
    real_root = (project.parent / "outside") if outside else (project / "app" / "decoy")
    source = scratch / "app" / component
    target = real_root / component
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    link = project / "app" / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    return target / report.relative_to(source)


@pytest.mark.parametrize("outside", [True, False], ids=["outside", "in-project"])
@pytest.mark.parametrize(
    ("run", "component"),
    [("connected_android_test_success", c) for c in UNFLAVORED_COMPONENTS]
    + [("connected_android_test_flavored", c) for c in FLAVORED_COMPONENTS],
)
def test_a_symlinked_component_is_refused_before_gradle_and_deletes_nothing(
    run, component, outside, tmp_path: Path, monkeypatch, recorded_gradle
):
    project = _project(tmp_path)
    sentinel = _redirect(project, run, component, recorded_gradle, outside)
    started = _gradle(monkeypatch, 0, recorded_gradle("connected_android_test_success"))

    with pytest.raises(cli.Failure) as refused:
        _run(project, variant=_variant(run))

    assert sentinel.exists(), "cleanup deleted a report it reached through a symlink"
    assert "symlinked JUnit report path component" in str(refused.value)
    assert str(project / "app" / component) in str(refused.value)
    assert started == [], "Gradle started although the results tree was refused"


def test_a_symlinked_report_outside_the_project_is_refused(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    project = _project(tmp_path)
    report = _materialize(project, "connected_android_test_success", recorded_gradle)
    outside = tmp_path / "outside" / report.name
    outside.parent.mkdir()
    report.rename(outside)
    report.symlink_to(outside)
    started = _gradle(monkeypatch, 0, recorded_gradle("connected_android_test_success"))

    with pytest.raises(cli.Failure) as refused:
        _run(project)

    assert outside.exists()
    assert "outside its project task directory" in str(refused.value)
    assert started == []


def test_case_colliding_variant_directories_are_refused(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    project = _project(tmp_path)
    report = _materialize(project, "connected_android_test_success", recorded_gradle)
    connected = project / CONNECTED
    exact, colliding = connected / "debug", connected / "DEBUG"
    # On a case-sensitive filesystem (Linux CI) this is a second, real
    # directory; on a case-insensitive one (macOS's temp volume) it is the same
    # directory as `debug`, so exist_ok. Discovery drops a name that is not a
    # directory, so the colliding one has to exist wherever it can.
    colliding.mkdir(exist_ok=True)
    original = Path.iterdir

    def listing(path):
        # A case-insensitive volume lists one name; this is what a
        # case-sensitive filesystem lists.
        if path == connected:
            return iter((exact, colliding))
        return original(path)

    monkeypatch.setattr(Path, "iterdir", listing)
    started = _gradle(monkeypatch, 0, recorded_gradle("connected_android_test_success"))

    with pytest.raises(cli.Failure) as refused:
        _run(project)

    assert report.exists()
    assert "ambiguous" in str(refused.value).lower()
    assert started == []


def test_the_connected_task_runs_pinned_rerun_and_filtered(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """ANDROID_SERIAL in Gradle's environment, --rerun bound to the task, the runner's filter."""
    project = _project(tmp_path)
    started = _gradle(monkeypatch, 0, recorded_gradle("connected_android_test_success"))

    BuildRunner(str(project), module=":app", variant="proDebug").instrumented_test(
        SERIAL, suite="com.example.composefixture.PassingInstrumentedTest"
    )

    ((cmd, kwargs),) = started
    task = cmd.index(":app:connectedProDebugAndroidTest")
    assert cmd[task + 1 :] == [
        "--rerun",
        "-Pandroid.testInstrumentationRunnerArguments.class="
        "com.example.composefixture.PassingInstrumentedTest",
    ]
    assert "--tests" not in cmd
    assert kwargs["env"]["ANDROID_SERIAL"] == SERIAL
    assert kwargs["timeout"] == builder_mod.INSTRUMENTED_TEST_TIMEOUT > builder_mod.TEST_TIMEOUT


def test_two_layouts_reading_back_as_one_variant_are_refused_in_their_own_words(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """connected/proDebug/ beside connected/debug/flavors/pro/: not a case collision."""
    project = _project(tmp_path)
    flavored = _materialize(project, "connected_android_test_flavored", recorded_gradle)
    unflavored = project / CONNECTED / "proDebug" / flavored.name
    unflavored.parent.mkdir(parents=True)
    unflavored.write_text(flavored.read_text(), encoding="utf-8")
    started = _gradle(monkeypatch, 0, recorded_gradle("connected_android_test_flavored"))

    with pytest.raises(cli.Failure) as refused:
        _run(project, variant="proDebug")

    message = f"{refused.value} {refused.value.remedy}"
    assert "read back as variant proDebug" in message, message
    assert "case-colliding" not in message, message
    assert flavored.exists() and unflavored.exists()
    assert started == []


@pytest.mark.parametrize("outside", [True, False], ids=["outside", "in-project"])
@pytest.mark.parametrize("component", FLAVORED_COMPONENTS)
def test_a_symlinked_flavor_directory_is_refused_for_its_build_type_too(
    component, outside, tmp_path: Path, monkeypatch, recorded_gradle
):
    """A `debug` run clears debug/flavors/<f>/, so a symlink there is refused like any other."""
    project = _project(tmp_path)
    sentinel = _redirect(
        project, "connected_android_test_flavored", component, recorded_gradle, outside
    )
    started = _gradle(monkeypatch, 0, recorded_gradle("connected_android_test_flavored"))

    with pytest.raises(cli.Failure) as refused:
        _run(project, variant="debug")

    assert sentinel.exists(), "cleanup deleted a report it reached through a symlink"
    assert "symlinked JUnit report path component" in str(refused.value)
    assert started == []
