"""The Gradle recorder's ``earlier_runs``: a file only a project's history produces.

Room keeps one exported schema per database version, each written by the build
of its day, so a schemas directory holding two versions takes two builds of two
sources in one project. No Gradle runs here: ``_run`` is faked and writes what a
build would, into the copy the recorder made.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import record_gradle_fixtures as recorder

NOW = "2099-01-01T00:00:00Z"
EARLIER = ("-ProomSchemaVersion=1", ":db:kspDebugKotlin")
LATER = ["-ProomSchemaVersion=2", ":db:kspDebugKotlin"]


def _probe(**overrides) -> recorder.GradleFixture:
    fields = {
        "name": "probe",
        "args": LATER,
        "description": "probe",
        "earlier_runs": (EARLIER,),
        "expect_success": True,
        "results_glob": "out/**/2.json",
        "results_fixture": "probe_result",
        "results_description": "What the probe wrote.",
        "tree_root": "out",
        "tree_fixture": "probe_tree",
        "tree_description": "Where the probe wrote.",
    }
    return recorder.GradleFixture(**{**fields, **overrides})


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(recorder, "RECORDED_ROOT", tmp_path)
    monkeypatch.setattr(recorder, "_gradle_version", lambda _gradle: "8.13")
    monkeypatch.setattr(recorder, "_utc_stamp", lambda: NOW)
    return tmp_path / "gradle-8.13"


def _fake_gradle(monkeypatch, exits: dict[str, int] | None = None) -> list[tuple[Path, list[str]]]:
    """A Gradle that writes ``out/db/<N>.json`` for ``-ProomSchemaVersion=<N>``."""
    calls: list[tuple[Path, list[str]]] = []

    def fake_run(_gradle, fixture, cwd=None, env=None, args=None):
        argv = list(fixture.args if args is None else args)
        calls.append((cwd, argv))
        version = argv[0].rpartition("=")[2]
        exit_code = (exits or {}).get(version, 0)
        if exit_code == 0:
            (cwd / "out" / "db").mkdir(parents=True, exist_ok=True)
            (cwd / "out" / "db" / f"{version}.json").write_text(f'{{"version": {version}}}')
        return f"> Task :db:kspDebugKotlin (version {version})\n", exit_code

    monkeypatch.setattr(recorder, "_run", fake_run)
    return calls


def test_the_schema_fixtures_run_only_enumerated_args_and_reach_no_device():
    by_name = {f.name: f for f in recorder.FIXTURES}
    first, second = by_name["room_schema_export_v1"], by_name["room_schema_export_v2"]

    assert recorder._args_refusals([first, second]) == []
    # Version 2 is recorded beside the version 1 an earlier build exported.
    assert second.earlier_runs == (tuple(first.args),)
    for run in (first.args, *second.earlier_runs, second.args):
        assert not set(run) & recorder.DEVICE_TASKS
    assert first.android_serial is None and second.android_serial is None


def test_an_earlier_run_is_enumerated_like_the_run_itself():
    why = recorder._args_refusal(_probe(earlier_runs=((":app:installDebug",),)))

    assert why is not None and "':app:installDebug'" in why


def test_an_earlier_run_reaching_a_device_needs_a_pinned_serial():
    why = recorder._args_refusal(_probe(earlier_runs=((":app:connectedDebugAndroidTest",),)))

    assert why is not None and "no android_serial" in why


def test_earlier_runs_come_first_in_the_same_copy(corpus: Path, monkeypatch):
    calls = _fake_gradle(monkeypatch)
    monkeypatch.setattr(recorder, "FIXTURES", [*recorder.FIXTURES, _probe()])

    assert recorder.record("gradle", only={"probe"}) == 0

    assert [argv for _, argv in calls] == [list(EARLIER), LATER]
    (copy,) = {cwd for cwd, _ in calls}
    assert copy != recorder.FLAVORED, "the checked-in scaffold was built in"
    # The log recorded is the LATER run's; the tree shows both runs' files.
    assert (corpus / "probe.txt").read_text() == "> Task :db:kspDebugKotlin (version 2)\n"
    assert (corpus / "probe_tree.txt").read_text() == (
        "out/\nout/db/\nout/db/1.json\nout/db/2.json\n"
    )
    manifest = json.loads((corpus / "MANIFEST.json").read_text())
    (entry,) = [e for e in manifest["fixtures"] if e["name"] == "probe"]
    assert "gradle --console=plain -ProomSchemaVersion=1 :db:kspDebugKotlin" in entry["setup"]


def test_a_results_file_keeps_its_own_extension_and_description(corpus: Path, monkeypatch):
    _fake_gradle(monkeypatch)
    monkeypatch.setattr(recorder, "FIXTURES", [*recorder.FIXTURES, _probe()])

    assert recorder.record("gradle", only={"probe"}) == 0

    assert (corpus / "probe_result.json").read_text() == '{"version": 2}'
    assert not (corpus / "probe_result.xml").exists()
    manifest = json.loads((corpus / "MANIFEST.json").read_text())
    (entry,) = [e for e in manifest["fixtures"] if e["name"] == "probe_result"]
    assert entry["file"] == "probe_result.json"
    assert entry["results_path"] == "out/db/2.json"
    assert entry["description"] == "What the probe wrote."
    (tree,) = [e for e in manifest["fixtures"] if e["name"] == "probe_tree"]
    assert tree["description"] == "Where the probe wrote."


def test_a_failed_earlier_run_records_nothing(corpus: Path, monkeypatch, capsys):
    calls = _fake_gradle(monkeypatch, exits={"1": 1})
    monkeypatch.setattr(recorder, "FIXTURES", [*recorder.FIXTURES, _probe()])

    assert recorder.record("gradle", only={"probe"}) == 1

    assert [argv for _, argv in calls] == [list(EARLIER)], "the later run still happened"
    assert not list(corpus.glob("probe*"))
    assert "earlier run" in capsys.readouterr().err


def test_a_copy_starts_without_the_schemas_a_build_by_hand_left(tmp_path: Path):
    scaffold = tmp_path / "room"
    (scaffold / "db" / "schemas" / "com.example.Db").mkdir(parents=True)
    (scaffold / "db" / "schemas" / "com.example.Db" / "9.json").write_text("{}")
    (scaffold / "db" / "build.gradle").write_text("")

    copy = recorder._prepare_copy(_probe(scaffold=scaffold), tmp_path / "work")

    assert (copy / "db" / "build.gradle").exists()
    assert not (copy / "db" / "schemas").exists()
