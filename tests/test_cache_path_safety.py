"""Cache id handling in the progressive-disclosure cache.

A4. `ProgressiveCache.get()` built its path as::

    cache_file = self.cache_dir / f"{cache_id}.json"

from an unsanitised, caller-supplied id, and `_is_expired()` returns True for
anything that fails to parse as JSON with a valid ``created_at``. So::

    if self._is_expired(cache_file):
        cache_file.unlink()

reads an arbitrary path and then *deletes* it. The id reaches this from the CLI
as ``build_and_test.py --get-log <ID>``.

Impact is bounded -- it needs a caller-supplied id and only removes files whose
name ends in ``.json`` -- but this ships inside a plugin and there is no reason
for a cache id to address anything outside the cache directory.
"""

from __future__ import annotations

import json

import pytest

from common.cache_utils import ProgressiveCache


@pytest.fixture
def cache(tmp_path) -> ProgressiveCache:
    return ProgressiveCache(cache_dir=tmp_path / "cache")


@pytest.fixture
def outsider(tmp_path):
    """A file outside the cache directory that must survive every lookup."""
    victim = tmp_path / "important.json"
    victim.write_text("not a cache entry, and not valid cache JSON", encoding="utf-8")
    return victim


TRAVERSAL_IDS = [
    "../important",
    "../../important",
    "subdir/../../important",
    "./../important",
]


@pytest.mark.parametrize("cache_id", TRAVERSAL_IDS)
def test_traversing_id_does_not_delete_files_outside_the_cache(cache, outsider, cache_id):
    """The unlink path is the dangerous half: reading is bad, deleting is worse."""
    cache.get(cache_id)
    assert outsider.exists(), f"cache id {cache_id!r} deleted a file outside the cache directory"


@pytest.mark.parametrize("cache_id", TRAVERSAL_IDS)
def test_traversing_id_returns_nothing(cache, outsider, cache_id):
    """A rejected id must not yield content either."""
    assert cache.get(cache_id) is None


def test_absolute_path_id_is_rejected(cache, outsider):
    """An absolute id would otherwise discard the cache_dir prefix entirely."""
    cache.get(str(outsider.with_suffix("")))
    assert outsider.exists()


def test_id_with_separator_is_rejected(cache, tmp_path):
    """Even without '..', a separator escapes the flat cache namespace."""
    nested = tmp_path / "cache" / "nested"
    nested.mkdir(parents=True)
    (nested / "entry.json").write_text("garbage", encoding="utf-8")

    assert cache.get("nested/entry") is None
    assert (nested / "entry.json").exists()


# ---------------------------------------------------------------------------
# The legitimate path must keep working.
# ---------------------------------------------------------------------------


def test_normal_round_trip_still_works(cache):
    """Guard against over-tightening into rejecting real ids."""
    cache_id = cache.save({"devices": ["emulator-5554"]}, "simulator-list")
    assert cache.get(cache_id) == {"devices": ["emulator-5554"]}


def test_generated_ids_pass_validation(cache):
    """Whatever save() produces must be accepted by get()."""
    for cache_type in ("simulator-list", "build-result", "anr"):
        cache_id = cache.save({"x": 1}, cache_type)
        assert cache.get(cache_id) is not None, f"save() produced a rejected id: {cache_id!r}"


def test_expired_entry_inside_the_cache_is_still_cleaned(cache, tmp_path):
    """The expiry sweep must keep working for genuine entries."""
    cache_id = cache.save({"x": 1}, "simulator-list")
    path = tmp_path / "cache" / f"{cache_id}.json"

    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["created_at"] = "2000-01-01T00:00:00"
    path.write_text(json.dumps(stale), encoding="utf-8")

    assert cache.get(cache_id) is None
    assert not path.exists(), "expired entries inside the cache should still be removed"


def test_unknown_but_wellformed_id_is_a_miss_not_an_error(cache):
    """A plausible id that simply does not exist returns None."""
    assert cache.get("sim-20260101-000000") is None
