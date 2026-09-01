"""`--all` / `--top` cluster retention in anr_watcher.

A3, two defects in three lines:

    top_n = None if args.all_clusters else args.top_n     # main()
    effective_top_n = top_n or env_int(..., 3)            # stop()
    summary.clusters = summary.clusters[:effective_top_n]
    self.store.stop(session_id, summary)

1. `top_n or default` is a falsy-None bug: `--all`, whose whole purpose is "no
   cap", passes None and therefore gets exactly 3. `--top 0` likewise.
2. The truncation happens **before** the summary is persisted, so every cluster
   past N is destroyed on disk. `--get-details --cluster 4` can never resolve,
   and `--diff` compares two truncated sets and reports clusters as newly
   appearing or resolved purely because they fell outside the cap.

Display truncation is fine. Storage truncation is data loss.
"""

from __future__ import annotations

import json

import pytest
from anr_watcher import AnrBuster

from common import anr_sessions

CLUSTER_COUNT = 8


@pytest.fixture
def store(tmp_path) -> anr_sessions.SessionStore:
    return anr_sessions.SessionStore(base_dir=tmp_path / "anr-sessions")


@pytest.fixture
def session_with_many_clusters(store) -> str:
    """A stopped-ready session holding CLUSTER_COUNT distinct fingerprints."""
    meta = store.create({"package": "com.example.app"})
    with open(store.events_path(meta.session_id), "w") as handle:
        for index in range(CLUSTER_COUNT):
            # Distinct fingerprint per event => distinct cluster.
            handle.write(
                json.dumps(
                    {
                        "delta_ms": index * 1000,
                        "process": f"proc{index}",
                        "pid": 1000 + index,
                        "duration_ms": 500.0 + index,
                        "severity": "warn",
                        "symbol": None,
                        "message_prefix": f"Skipped frames variant {index}",
                        "fingerprint": f"fp{index:03d}",
                        "raw_message": f"Skipped {30 + index} frames!",
                        "frames": 30 + index,
                        "kind": "jank",
                    }
                )
                + "\n"
            )
    return meta.session_id


def _stop(store, session_id: str, **kwargs) -> str:
    return AnrBuster(store=store).stop(session_id, **kwargs)


# ---------------------------------------------------------------------------
# Guard the guard.
# ---------------------------------------------------------------------------


def test_fixture_really_produces_more_clusters_than_the_default_cap(
    store, session_with_many_clusters
):
    """Without more clusters than the cap, none of this proves anything."""
    summary = store.build_summary(session_with_many_clusters, 0, 0, 0)
    assert len(summary.clusters) == CLUSTER_COUNT
    assert CLUSTER_COUNT > 3, "fixture must exceed the default top-N of 3"


# ---------------------------------------------------------------------------
# Storage must never be truncated.
# ---------------------------------------------------------------------------


def test_default_stop_persists_every_cluster(store, session_with_many_clusters):
    """The default cap is a display concern; the stored summary must be whole."""
    _stop(store, session_with_many_clusters)

    persisted = store.load_summary(session_with_many_clusters)
    assert len(persisted.clusters) == CLUSTER_COUNT, (
        f"stored only {len(persisted.clusters)} of {CLUSTER_COUNT} clusters; "
        f"the rest are gone from disk and --get-details can never reach them"
    )


def test_details_can_reach_a_cluster_beyond_the_display_cap(store, session_with_many_clusters):
    """`--get-details --cluster N` reads the persisted summary."""
    _stop(store, session_with_many_clusters)

    out = AnrBuster(store=store).get_details(session_with_many_clusters, cluster=CLUSTER_COUNT)
    assert "out of range" not in out, f"cluster {CLUSTER_COUNT} unreachable after --stop: {out!r}"


def test_explicit_top_n_still_does_not_truncate_storage(store, session_with_many_clusters):
    """Even an explicit --top is a view, not a delete."""
    _stop(store, session_with_many_clusters, top_n=2)

    persisted = store.load_summary(session_with_many_clusters)
    assert len(persisted.clusters) == CLUSTER_COUNT


# ---------------------------------------------------------------------------
# --all must mean all.
# ---------------------------------------------------------------------------


def test_all_clusters_keeps_every_cluster_in_output(store, session_with_many_clusters):
    """`--all` passes top_n=0 meaning "no cap"; `x or default` swallowed it."""
    out = _stop(store, session_with_many_clusters, top_n=0, json_mode=True)

    payload = json.loads(out)
    assert len(payload["clusters"]) == CLUSTER_COUNT, (
        f"--all returned {len(payload['clusters'])} clusters, expected "
        f"{CLUSTER_COUNT}; the no-cap request was silently replaced by the default"
    )


def test_explicit_top_n_limits_the_view(store, session_with_many_clusters):
    """Guard against over-correcting into never capping."""
    out = _stop(store, session_with_many_clusters, top_n=2, json_mode=True)
    assert len(json.loads(out)["clusters"]) == 2


def test_default_view_uses_the_configured_cap(store, session_with_many_clusters, monkeypatch):
    """With no flag, the environment default still applies to the view."""
    out = _stop(store, session_with_many_clusters, json_mode=True)
    assert len(json.loads(out)["clusters"]) == 3


# ---------------------------------------------------------------------------
# --diff must not invent deltas from a display cap.
# ---------------------------------------------------------------------------


def _write_session(store, durations: dict[str, float]) -> str:
    """Create a session whose clusters rank by the given per-fingerprint duration."""
    meta = store.create({"package": "com.example.app"})
    with open(store.events_path(meta.session_id), "w") as handle:
        for index, (fingerprint, duration) in enumerate(durations.items()):
            handle.write(
                json.dumps(
                    {
                        "delta_ms": index * 1000,
                        "process": f"proc{index}",
                        "pid": 1000 + index,
                        "duration_ms": duration,
                        "severity": "warn",
                        "symbol": None,
                        "message_prefix": f"Skipped frames variant {index}",
                        "fingerprint": fingerprint,
                        "raw_message": f"Skipped {30 + index} frames!",
                        "frames": 30 + index,
                        "kind": "jank",
                    }
                )
                + "\n"
            )
    return meta.session_id


def test_diff_does_not_invent_deltas_from_a_display_cap(store):
    """A cluster that merely drops in rank must not read as resolved.

    Both sessions contain the same eight fingerprints. Only their severity
    ordering differs, so `slow_screen` sits inside the top 3 in the first
    session and outside it in the second. While storage was truncated to the
    top N, that cluster vanished from the second stored summary entirely and
    --diff reported it as resolved -- a regression that never happened.
    """
    names = [f"fp{i:03d}" for i in range(CLUSTER_COUNT - 1)]

    first = _write_session(store, {"slow_screen": 900.0, **dict.fromkeys(names, 100.0)})
    second = _write_session(store, {"slow_screen": 100.0, **dict.fromkeys(names, 900.0)})

    AnrBuster(store=store).stop(first)
    AnrBuster(store=store).stop(second)

    stored_second = store.load_summary(second)
    assert "slow_screen" in {c.fingerprint for c in stored_second.clusters}, (
        "a cluster that only dropped in rank is missing from storage, so --diff "
        "would report it as resolved when it is still occurring"
    )

    from common.anr_pipeline import diff_sessions

    delta = diff_sessions(store.load_summary(first), stored_second)
    resolved = {c.get("fingerprint") for c in delta.get("resolved", [])}
    assert "slow_screen" not in resolved, f"phantom 'resolved' cluster in diff: {delta}"
