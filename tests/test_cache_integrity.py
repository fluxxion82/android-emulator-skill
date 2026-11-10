"""D6/C10: the progressive-disclosure cache must not lose or misreport entries.

Three defects in ``common/cache_utils.py``, each of which lost an entry quietly:

- **Collision.** ``save()`` named an entry ``<prefix>-YYYYmmdd-HHMMSS``, so two
  saves of one type inside one second got the same id and the second
  overwrote the first -- the caller holding the first id read the second's data.
- **Non-atomic write.** ``save()`` opened the final path and ``json.dump``ed
  into it; a crash mid-write left a truncated ``<id>.json`` behind.
- **Corruption read as expiry.** ``_is_expired()`` answered True for anything
  that failed to decode, so ``get()`` deleted an unreadable entry and returned
  None as though it had aged out, and ``cleanup()`` deleted and counted it as
  expired. The truncated file from the second defect was therefore destroyed by
  the next read, and the caller was told "not found" instead of "corrupt".

The files written below are constructed cache state, not tool output.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from common import cache_utils
from common.cache_utils import ProgressiveCache


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "cache"


@pytest.fixture
def cache(cache_dir) -> ProgressiveCache:
    return ProgressiveCache(cache_dir=str(cache_dir))


def _outcome(cache: ProgressiveCache, cache_id: str):
    """What ``get()`` did: the value it returned, or the exception it raised."""
    try:
        return cache.get(cache_id)
    except Exception as error:  # the outcome, raised or returned, is what is asserted
        return error


# ---------------------------------------------------------------------------
# Collision
# ---------------------------------------------------------------------------


def test_two_saves_in_one_second_get_distinct_ids(cache, monkeypatch):
    """Pinned to one instant, two saves of one type must not share an id."""
    instant = datetime(2026, 1, 2, 3, 4, 5)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr(cache_utils, "datetime", _Frozen)

    first = cache.save({"n": 1}, "build-result")
    second = cache.save({"n": 2}, "build-result")

    assert first != second, f"two saves in one second both returned {first!r}"
    assert cache.get(first) == {"n": 1}
    assert cache.get(second) == {"n": 2}


def test_generated_id_keeps_its_readable_prefix_and_timestamp(cache):
    """The suffix is added, not substituted: the id still reads as type-and-time."""
    cache_id = cache.save({"x": 1}, "build-result")
    assert cache_id.startswith("build-")
    assert cache_utils._SAFE_CACHE_ID_RE.fullmatch(cache_id), cache_id


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def test_a_failed_write_leaves_no_entry_and_no_temp_file(cache, cache_dir, monkeypatch):
    """A write that dies halfway must not leave a truncated entry for get() to find."""
    real_dump = json.dump

    def dies_halfway(obj, fp, **kwargs):
        fp.write('{"cache_id": "build-')
        fp.flush()
        raise OSError("disk full")

    monkeypatch.setattr(cache_utils.json, "dump", dies_halfway)
    with pytest.raises(OSError, match="disk full"):
        cache.save({"x": 1}, "build-result")
    monkeypatch.setattr(cache_utils.json, "dump", real_dump)

    left = sorted(path.name for path in cache_dir.iterdir())
    assert not [
        name for name in left if name.endswith(".json")
    ], f"a failed save left a partial entry at its final path: {left}"
    assert not left, f"a failed save left a temp file in the cache dir: {left}"


def test_a_leftover_temp_file_is_neither_listed_nor_read(cache, cache_dir):
    """A temp file a crash left behind is not an entry, even when it is complete."""
    complete = {
        "cache_id": "build-20260101-000000-abcd1234",
        "cache_type": "build-result",
        "created_at": datetime.now().isoformat(),
        "data": {"x": 1},
    }
    leftover = cache_dir / ".build-20260101-000000-abcd1234.x1y2.json.tmp"
    leftover.write_text(json.dumps(complete), encoding="utf-8")

    assert cache.list_entries() == []
    assert cache.get("build-20260101-000000-abcd1234") is None
    assert cache.cleanup() == 0
    assert cache.corrupt_entries() == []
    assert leftover.exists()


# ---------------------------------------------------------------------------
# Corruption is not expiry
# ---------------------------------------------------------------------------

CORRUPT_ID = "build-20260101-000000-abcd1234"

CORRUPT_BODIES = {
    "truncated": '{"cache_id": "build-20260101-000000-abcd1234", "cache_',
    "no_created_at": json.dumps({"cache_id": CORRUPT_ID, "data": {"x": 1}}),
    "bad_created_at": json.dumps({"cache_id": CORRUPT_ID, "created_at": "soon", "data": {}}),
    "not_an_object": json.dumps(["build", "result"]),
    # Typed but undated: a typed clear must not take the readable type as leave
    # to delete an entry every other operation treats as corrupt.
    "typed_no_created_at": json.dumps({"cache_type": "build-result", "data": {}}),
    "typed_bad_created_at": json.dumps(
        {"cache_type": "build-result", "created_at": "yesterday", "data": {}}
    ),
    # Well-formed JSON Python's decoder still refuses: an integer longer than
    # sys.get_int_max_str_digits() (4300) raises a plain ValueError, not a
    # JSONDecodeError. Written as text, because json.dump of such an int would
    # hit the same limit.
    "oversized_int": (
        '{"cache_id": "build-20260101-000000-abcd1234", "cache_type": "build-result", '
        '"created_at": "2026-01-01T00:00:00", "data": {"n": ' + "9" * 4301 + "}}"
    ),
}


@pytest.fixture(params=sorted(CORRUPT_BODIES))
def corrupt_entry(request, cache_dir):
    path = cache_dir / f"{CORRUPT_ID}.json"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(CORRUPT_BODIES[request.param], encoding="utf-8")
    return path


def test_get_reports_a_corrupt_entry_and_keeps_it(cache, corrupt_entry):
    """Corrupt is not expired: the file survives and the caller is told which it is."""
    outcome = _outcome(cache, CORRUPT_ID)

    assert (
        corrupt_entry.exists()
    ), f"get() deleted a corrupt entry as if it had expired (it returned {outcome!r})"
    assert isinstance(
        outcome, cache_utils.CacheCorruptionError
    ), f"get() on a corrupt entry answered {outcome!r}, not a corruption error"
    assert isinstance(outcome, RuntimeError)
    assert str(corrupt_entry) in str(outcome)
    assert "corrupt" in str(outcome) and "not expired" in str(outcome)
    assert outcome.remedy and str(corrupt_entry) in outcome.remedy


def test_cleanup_neither_deletes_nor_counts_a_corrupt_entry(cache, corrupt_entry):
    """The expiry sweep is for aged entries; an unreadable one is set aside, not swept."""
    deleted = cache.cleanup()

    assert corrupt_entry.exists(), "cleanup() deleted a corrupt entry as if it had expired"
    assert deleted == 0, f"cleanup() counted a corrupt entry as expired ({deleted})"
    assert cache.corrupt_entries() == [corrupt_entry]


def test_listing_skips_a_corrupt_entry_without_deleting_it(cache, corrupt_entry):
    """A listing cannot attribute a corrupt entry to a type; it must not destroy it either."""
    assert cache.list_entries() == []
    assert corrupt_entry.exists(), "list_entries() deleted a corrupt entry"


def test_a_typed_clear_keeps_a_corrupt_entry_and_an_untyped_clear_removes_it(cache, corrupt_entry):
    """A corrupt entry has no readable type, so no typed clear may claim it."""
    cleared = cache.clear("build-result")

    assert corrupt_entry.exists(), "clear('build-result') deleted a corrupt entry"
    assert cleared == 0, f"clear('build-result') counted a corrupt entry ({cleared})"
    assert cache.clear() == 1
    assert not corrupt_entry.exists()


def test_an_expired_entry_beside_a_corrupt_one_is_still_swept(cache, cache_dir, corrupt_entry):
    """Setting corrupt entries aside must not stop the sweep for genuine old ones."""
    cache_id = cache.save({"x": 1}, "simulator-list")
    path = cache_dir / f"{cache_id}.json"
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["created_at"] = "2000-01-01T00:00:00"
    path.write_text(json.dumps(stale), encoding="utf-8")

    assert cache.cleanup() == 1
    assert not path.exists()
    assert corrupt_entry.exists()


@pytest.mark.parametrize("flag", ["--get-errors", "--get-warnings", "--get-log"])
@pytest.mark.parametrize("json_mode", [True, False])
def test_build_and_test_refuses_a_corrupt_build_as_corrupt(
    flag, json_mode, cache_dir, corrupt_entry, monkeypatch, capsys
):
    """The CLI boundary: one refusal naming the file and saying corrupt, not 'not found'."""
    import build_and_test as build_script

    from common import cli
    from gradle import BuildResultCache

    build_cache = BuildResultCache(cache_dir=str(cache_dir))
    monkeypatch.setattr(build_script, "BuildResultCache", lambda: build_cache)

    argv = [flag, CORRUPT_ID, *(["--json"] if json_mode else [])]
    with pytest.raises(SystemExit) as stopped:
        cli.run(build_script.main, argv)
    captured = capsys.readouterr()

    assert "Traceback" not in captured.err, captured.err
    assert stopped.value.code == 1
    if json_mode:
        payload = json.loads(captured.out)
        error, remedy = payload["error"], payload["remedy"]
        assert payload["success"] is False
        assert captured.out.count("\n") == 1, captured.out
    else:
        lines = captured.err.splitlines()
        assert len(lines) == 2 and lines[0].startswith("Error: "), captured.err
        assert lines[1].startswith("Remedy: "), captured.err
        error, remedy = lines[0], lines[1]
    assert "corrupt" in error and "not expired" in error, error
    assert str(corrupt_entry) in error, error
    assert str(corrupt_entry) in remedy, remedy
    assert corrupt_entry.exists()


@pytest.mark.parametrize("json_mode", [True, False])
def test_list_builds_skips_a_corrupt_build_and_still_answers(
    json_mode, cache_dir, corrupt_entry, monkeypatch, capsys
):
    """`--list-builds` is a scan: a corrupt file is skipped and kept, never a traceback."""
    import build_and_test as build_script

    from common import cli
    from gradle import BuildResultCache

    build_cache = BuildResultCache(cache_dir=str(cache_dir))
    monkeypatch.setattr(build_script, "BuildResultCache", lambda: build_cache)

    with pytest.raises(SystemExit) as stopped:
        cli.run(build_script.main, ["--list-builds", *(["--json"] if json_mode else [])])
    captured = capsys.readouterr()

    assert "Traceback" not in captured.err, captured.err
    assert stopped.value.code == 0, captured.err
    if json_mode:
        assert json.loads(captured.out) == []
    else:
        assert captured.out.strip() == "No cached builds found"
    assert corrupt_entry.exists()


# ---------------------------------------------------------------------------
# Typed reads
# ---------------------------------------------------------------------------


def test_a_typed_get_answers_only_an_entry_of_that_type(cache):
    """``get(id, cache_type=...)`` is None for another type's entry, and keeps it.

    A reader that knows what it cached (``container --get-cached`` reads
    ``container-cat`` entries) must not be handed another script's data because
    that data happens to carry the same keys.
    """
    foreign = cache.save({"content": "another script's text"}, "build-result")
    own = cache.save({"content": "cat text"}, "container-cat")

    assert cache.get(foreign, cache_type="container-cat") is None
    assert cache.get(own, cache_type="container-cat") == {"content": "cat text"}
    assert cache.get(foreign) == {"content": "another script's text"}, "the entry was removed"


def test_a_typed_get_still_reports_a_corrupt_entry(cache, corrupt_entry):
    """Asking for a type does not turn corruption into not-found, nor delete the file."""
    with pytest.raises(cache_utils.CacheCorruptionError):
        cache.get(CORRUPT_ID, cache_type="container-cat")
    assert corrupt_entry.exists()


@pytest.mark.parametrize("flag", ["--get-errors", "--get-warnings", "--get-log"])
@pytest.mark.parametrize("json_mode", [True, False])
def test_build_and_test_refuses_an_entry_another_script_cached(
    flag, json_mode, cache_dir, monkeypatch, capsys
):
    """A ``container-cat`` id is not a build: refused, not an empty list at exit 0.

    The entry carries ``stdout`` so that an untyped read has something to
    print for ``--get-log`` too.
    """
    import build_and_test as build_script

    from common import cli
    from gradle import BuildResultCache

    foreign_text = "another script's text"
    build_cache = BuildResultCache(cache_dir=str(cache_dir))
    monkeypatch.setattr(build_script, "BuildResultCache", lambda: build_cache)
    foreign = ProgressiveCache(cache_dir=str(cache_dir)).save(
        {"content": foreign_text, "stdout": foreign_text}, "container-cat"
    )

    argv = [flag, foreign, *(["--json"] if json_mode else [])]
    with pytest.raises(SystemExit) as stopped:
        cli.run(build_script.main, argv)
    captured = capsys.readouterr()

    assert stopped.value.code == 1, (stopped.value.code, captured.out, captured.err)
    assert foreign_text not in captured.out + captured.err
    said = json.loads(captured.out)["error"] if json_mode else captured.err
    assert foreign in said, said
    assert (cache_dir / f"{foreign}.json").exists(), "the other script's entry was removed"
