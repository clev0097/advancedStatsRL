from __future__ import annotations

import pytest

from rl_tracker.mmr_client import (
    MmrFailureReason,
    MmrFetchError,
    PlaylistRating,
    TrackerSnapshot,
)
from rl_tracker.mmr_identity import PlayerIdentity
from rl_tracker.mmr_session import MmrSession, MmrStatus


def _snap(playlists: dict[int, tuple[int, int]]) -> TrackerSnapshot:
    return TrackerSnapshot(
        playlists={
            pid: PlaylistRating(playlist_id=pid, rating=r, matches=m, name=f"P{pid}")
            for pid, (r, m) in playlists.items()
        }
    )


def _make(fetcher) -> MmrSession:
    ident = PlayerIdentity("steam", "76561198000000001")
    return MmrSession(
        identity_override=ident,
        fetcher=fetcher,
        wait_interval_s=0.001,
        steady_interval_s=0.001,
    )


def test_baseline_set_on_first_fetch_then_deltas() -> None:
    snaps = [
        _snap({11: (900, 50), 13: (1100, 80)}),
        _snap({11: (912, 53), 13: (1095, 81)}),
    ]
    calls = {"i": 0}

    def fetcher(identity):
        i = calls["i"]
        calls["i"] += 1
        return snaps[min(i, len(snaps) - 1)]

    sess = _make(fetcher)
    # Manually drive one cycle of the loop logic.
    sess._on_success(sess._identity, fetcher(sess._identity))
    view = sess.snapshot()
    assert view.status == MmrStatus.READY
    assert view.total_delta == 0

    sess._on_success(sess._identity, fetcher(sess._identity))
    view = sess.snapshot()
    assert view.status == MmrStatus.SYNCED
    # 912-900 + 1095-1100 = 12 - 5 = 7
    assert view.total_delta == 7
    assert view.total_matches_delta == (53 - 50) + (81 - 80)


def test_last_stable_delta_survives_transient_failure() -> None:
    sess = _make(lambda _i: _snap({11: (900, 50)}))
    sess._on_success(sess._identity, _snap({11: (900, 50)}))
    sess._on_success(sess._identity, _snap({11: (925, 53)}))
    before = sess.snapshot()
    assert before.total_delta == 25

    # Simulate a failure path.
    sess._set_failure(MmrFailureReason.RATE_LIMITED)
    after = sess.snapshot()
    assert after.status == MmrStatus.FAILED
    assert after.failure_reason == MmrFailureReason.RATE_LIMITED
    # Deltas preserved.
    assert after.total_delta == 25


def test_reset_rearms() -> None:
    sess = _make(lambda _i: _snap({11: (900, 50)}))
    sess._on_success(sess._identity, _snap({11: (900, 50)}))
    sess._on_success(sess._identity, _snap({11: (950, 55)}))
    assert sess.snapshot().total_delta == 50

    sess.reset()
    view = sess.snapshot()
    assert view.status == MmrStatus.LOADING
    assert view.total_delta == 0
    # Next success becomes the new baseline.
    sess._on_success(sess._identity, _snap({11: (950, 55)}))
    assert sess.snapshot().total_delta == 0
