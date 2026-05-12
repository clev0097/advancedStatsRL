from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from .config import load_session_gap_minutes
from .history import HistoryStore, MmrSnapshotRow
from .mmr_client import (
    MmrFailureReason,
    MmrFetchError,
    PlaylistRating,
    TrackerSnapshot,
    fetch_tracker_snapshot,
)
from .mmr_identity import PlayerIdentity, detect_player


class MmrStatus(str, Enum):
    DISABLED = "disabled"
    LOADING = "loading"
    READY = "ready"
    SYNCING = "syncing"
    SYNCED = "synced"
    FAILED = "failed"


@dataclass(frozen=True)
class PlaylistDelta:
    playlist_id: int
    name: str | None
    rating: int
    rating_delta: int
    matches_delta: int


@dataclass(frozen=True)
class MmrView:
    status: MmrStatus
    failure_reason: MmrFailureReason | None
    identity: PlayerIdentity | None
    total_delta: int
    total_matches_delta: int
    by_playlist: dict[int, PlaylistDelta] = field(default_factory=dict)


_FetchFn = Callable[[PlayerIdentity], TrackerSnapshot]
_OnChange = Callable[[], None]


class MmrSession:
    """Background worker that maintains baseline + current tracker.gg snapshots.

    Thread-safe: callers should use ``snapshot()`` to obtain an immutable view
    for rendering. The worker emits a callback on every state change so the
    UI can request a repaint.
    """

    WAIT_INTERVAL_S = 3.0
    STEADY_INTERVAL_S = 30.0
    RETRY_SCHEDULE_S: tuple[float, ...] = (5.0, 15.0, 60.0)

    def __init__(
        self,
        log_path: Path | None = None,
        history: HistoryStore | None = None,
        identity_override: PlayerIdentity | None = None,
        fetcher: _FetchFn | None = None,
        on_change: _OnChange | None = None,
        wait_interval_s: float | None = None,
        steady_interval_s: float | None = None,
    ) -> None:
        self._log_path = log_path
        self._history = history
        self._identity_override = identity_override
        self._fetcher: _FetchFn = fetcher or fetch_tracker_snapshot
        self._on_change = on_change
        self._wait_interval = wait_interval_s if wait_interval_s is not None else self.WAIT_INTERVAL_S
        self._steady_interval = steady_interval_s if steady_interval_s is not None else self.STEADY_INTERVAL_S

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._identity: PlayerIdentity | None = identity_override
        self._baseline: TrackerSnapshot | None = None
        self._current: TrackerSnapshot | None = None
        self._last_stable_delta: dict[int, PlaylistDelta] = {}
        self._status: MmrStatus = MmrStatus.LOADING
        self._failure_reason: MmrFailureReason | None = None

    # --- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        # Try to seed baseline from a recent persisted snapshot before the
        # worker fires; means the very first paint after restart has data.
        self._maybe_seed_baseline_from_history()
        self._thread = threading.Thread(target=self._run, name="rl-mmr-worker", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=join_timeout)

    def reset(self) -> None:
        with self._lock:
            self._baseline = None
            self._current = None
            self._last_stable_delta.clear()
            self._status = MmrStatus.LOADING
            self._failure_reason = None
        self._wake.set()
        self._notify()

    def nudge(self) -> None:
        """Ask the worker to fetch sooner (e.g. after a ranked match ends)."""
        self._wake.set()

    # --- read API ------------------------------------------------------
    def snapshot(self) -> MmrView:
        with self._lock:
            by = dict(self._last_stable_delta)
            total = sum(p.rating_delta for p in by.values())
            total_matches = sum(p.matches_delta for p in by.values())
            return MmrView(
                status=self._status,
                failure_reason=self._failure_reason,
                identity=self._identity,
                total_delta=total,
                total_matches_delta=total_matches,
                by_playlist=by,
            )

    # --- worker --------------------------------------------------------
    def _run(self) -> None:
        retry_idx = 0
        while not self._stop.is_set():
            identity = self._resolve_identity()
            if identity is None:
                self._set_failure(MmrFailureReason.PLAYER_NOT_DETECTED)
                self._sleep(self._wait_interval)
                continue

            with self._lock:
                self._identity = identity
                self._status = MmrStatus.SYNCING if self._baseline is not None else MmrStatus.LOADING

            try:
                snap = self._fetcher(identity)
            except MmrFetchError as e:
                self._set_failure(e.reason)
                delay = self.RETRY_SCHEDULE_S[min(retry_idx, len(self.RETRY_SCHEDULE_S) - 1)]
                retry_idx += 1
                self._sleep(delay)
                continue
            except Exception:
                self._set_failure(MmrFailureReason.UNKNOWN)
                delay = self.RETRY_SCHEDULE_S[min(retry_idx, len(self.RETRY_SCHEDULE_S) - 1)]
                retry_idx += 1
                self._sleep(delay)
                continue

            retry_idx = 0
            self._on_success(identity, snap)
            self._sleep(self._steady_interval)

    # ------------------------------------------------------------------
    def _resolve_identity(self) -> PlayerIdentity | None:
        if self._identity_override is not None:
            return self._identity_override
        try:
            return detect_player(self._log_path)
        except Exception:
            return None

    def _on_success(self, identity: PlayerIdentity, snap: TrackerSnapshot) -> None:
        with self._lock:
            if self._baseline is None:
                self._baseline = snap
                self._current = snap
                self._last_stable_delta = _compute_deltas(snap, snap)
                self._status = MmrStatus.READY
            else:
                self._current = snap
                self._last_stable_delta = _compute_deltas(self._baseline, snap)
                self._status = MmrStatus.SYNCED
            self._failure_reason = None
        self._persist(identity, snap)
        self._notify()

    def _set_failure(self, reason: MmrFailureReason) -> None:
        with self._lock:
            self._status = MmrStatus.FAILED
            self._failure_reason = reason
        self._notify()

    def _persist(self, identity: PlayerIdentity, snap: TrackerSnapshot) -> None:
        if self._history is None:
            return
        now = datetime.now(timezone.utc)
        rows = [
            MmrSnapshotRow(
                taken_at=now,
                platform=identity.platform,
                platform_id=identity.id_or_name,
                playlist_id=p.playlist_id,
                playlist_name=p.name,
                rating=p.rating,
                matches=p.matches,
                tier_name=p.tier_name,
            )
            for p in snap.playlists.values()
        ]
        try:
            self._history.insert_mmr_snapshot_rows(rows)
        except Exception:
            pass

    def _maybe_seed_baseline_from_history(self) -> None:
        if self._history is None:
            return
        identity = self._resolve_identity()
        if identity is None:
            return
        try:
            latest = self._history.latest_mmr_per_playlist(identity.id_or_name)
        except Exception:
            return
        if not latest:
            return
        gap = timedelta(minutes=load_session_gap_minutes())
        cutoff = datetime.now(timezone.utc) - gap
        recent = {
            pid: row for pid, row in latest.items()
            if row.taken_at >= cutoff
        }
        if not recent:
            return
        playlists = {
            pid: PlaylistRating(
                playlist_id=pid,
                rating=row.rating,
                matches=row.matches,
                name=row.playlist_name,
                tier_name=row.tier_name,
            )
            for pid, row in recent.items()
        }
        seeded = TrackerSnapshot(playlists=playlists)
        with self._lock:
            self._identity = identity
            self._baseline = seeded
            self._current = seeded
            self._last_stable_delta = _compute_deltas(seeded, seeded)
            self._status = MmrStatus.READY

    # ------------------------------------------------------------------
    def _sleep(self, secs: float) -> None:
        self._wake.clear()
        if self._stop.wait(timeout=secs):
            return
        # If _wake was set, we return early too.

    def _notify(self) -> None:
        cb = self._on_change
        if cb is None:
            return
        try:
            cb()
        except Exception:
            pass


def _compute_deltas(
    baseline: TrackerSnapshot,
    current: TrackerSnapshot,
) -> dict[int, PlaylistDelta]:
    out: dict[int, PlaylistDelta] = {}
    ids = set(baseline.playlists.keys()) | set(current.playlists.keys())
    for pid in ids:
        b = baseline.playlists.get(pid)
        c = current.playlists.get(pid)
        if c is None and b is None:
            continue
        if c is None:
            # Disappeared from response — keep the baseline rating but zero delta.
            out[pid] = PlaylistDelta(pid, b.name if b else None, b.rating if b else 0, 0, 0)
            continue
        b_rating = b.rating if b else c.rating
        b_matches = b.matches if b else c.matches
        out[pid] = PlaylistDelta(
            playlist_id=pid,
            name=c.name,
            rating=c.rating,
            rating_delta=c.rating - b_rating,
            matches_delta=c.matches - b_matches,
        )
    return out
