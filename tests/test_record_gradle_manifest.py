"""The Gradle recorder's manifest merge: a partial run re-dates only what it recorded.

No Gradle runs here. The merge is exercised directly against the checked-in
gradle-8.13 manifest, and once through ``record()`` with the Gradle run faked,
writing into a temporary copy of the corpus.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import record_gradle_fixtures as recorder

PROFILE = Path(__file__).resolve().parent / "fixtures" / "recorded" / "gradle-8.13"
NOW = "2099-01-01T00:00:00Z"


def _checked_in() -> tuple[str, dict]:
    text = (PROFILE / "MANIFEST.json").read_text(encoding="utf-8")
    return text, json.loads(text)


def _entry_texts(manifest: dict) -> dict[str, str]:
    return {e["name"]: json.dumps(e, indent=2) for e in manifest["fixtures"]}


def test_a_partial_run_that_recorded_nothing_rewrites_the_manifest_byte_for_byte():
    text, previous = _checked_in()

    merged = recorder._merged_manifest(previous, [], version="8.13", now=NOW)

    assert recorder._manifest_text(merged) == text


def test_a_partial_run_keeps_the_profile_stamp_and_every_untouched_entry():
    _, previous = _checked_in()
    replaced = {"name": "gradle_build_successful", "file": "x.txt", "recorded_at": NOW}

    merged = recorder._merged_manifest(previous, [replaced], version="8.13", now=NOW)

    assert merged["recorded_at"] == previous["recorded_at"] != NOW
    for key in ("profile", "gradle_version", "scaffolds", "note"):
        assert merged[key] == previous[key], key
    before, after = _entry_texts(previous), _entry_texts(merged)
    assert before.keys() == after.keys()
    for name in before.keys() - {"gradle_build_successful"}:
        assert after[name] == before[name], name
    assert after["gradle_build_successful"] == json.dumps(replaced, indent=2)
    # Same position, too: the merge keeps FIXTURES order.
    assert [e["name"] for e in merged["fixtures"]] == [e["name"] for e in previous["fixtures"]]


def test_a_full_run_dates_the_profile_now():
    merged = recorder._merged_manifest(None, [], version="8.13", now=NOW)

    assert merged["recorded_at"] == NOW


def test_record_with_only_re_dates_nothing_it_did_not_record(tmp_path: Path, monkeypatch):
    """Through record() itself, with the Gradle invocation faked."""
    corpus = tmp_path / "recorded"
    shutil.copytree(PROFILE, corpus / "gradle-8.13")
    text, previous = _checked_in()

    def fake_run(_gradle, _fixture, cwd=None, env=None):
        assert env is None, "a flavored fixture must not be given a device environment"
        return "BUILD SUCCESSFUL in 1s\n", 0

    monkeypatch.setattr(recorder, "RECORDED_ROOT", corpus)
    monkeypatch.setattr(recorder, "_gradle_version", lambda _gradle: "8.13")
    monkeypatch.setattr(recorder, "_run", fake_run)
    monkeypatch.setattr(recorder, "_utc_stamp", lambda: NOW)

    assert recorder.record("gradle", only={"gradle_build_successful"}) == 0

    merged = json.loads((corpus / "gradle-8.13" / "MANIFEST.json").read_text(encoding="utf-8"))
    assert merged["recorded_at"] == previous["recorded_at"]
    (entry,) = [e for e in merged["fixtures"] if e["name"] == "gradle_build_successful"]
    assert entry["recorded_at"] == NOW
    before, after = _entry_texts(previous), _entry_texts(merged)
    for name in before.keys() - {"gradle_build_successful"}:
        assert after[name] == before[name], name
    # The checked-in corpus itself was not touched.
    assert (PROFILE / "MANIFEST.json").read_text(encoding="utf-8") == text
