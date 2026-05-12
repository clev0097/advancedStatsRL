from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from rl_tracker.history import HistoryStore, MmrSnapshotRow


def _row(taken_at: datetime, pid: int, rating: int, matches: int) -> MmrSnapshotRow:
    return MmrSnapshotRow(
        taken_at=taken_at,
        platform="steam",
        platform_id="76561198000000001",
        playlist_id=pid,
        playlist_name=f"P{pid}",
        rating=rating,
        matches=matches,
        tier_name="Gold I",
    )


def test_insert_and_read_back(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    inserted = store.insert_mmr_snapshot_rows(
        [_row(t0, 11, 900, 50), _row(t0, 13, 1100, 80)]
    )
    assert inserted == 2
    latest = store.latest_mmr_per_playlist("76561198000000001")
    assert set(latest.keys()) == {11, 13}
    assert latest[11].rating == 900
    assert latest[13].matches == 80


def test_dedupe_on_unchanged(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=30)
    store.insert_mmr_snapshot_rows([_row(t0, 11, 900, 50)])
    inserted = store.insert_mmr_snapshot_rows([_row(t1, 11, 900, 50)])
    assert inserted == 0  # unchanged tuple → skipped

    inserted = store.insert_mmr_snapshot_rows([_row(t1, 11, 912, 51)])
    assert inserted == 1

    hist = store.mmr_history("76561198000000001", 11)
    assert [r.rating for r in hist] == [900, 912]


def test_mmr_history_filter_since(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    store.insert_mmr_snapshot_rows([_row(t0, 11, 900, 50)])
    store.insert_mmr_snapshot_rows([_row(t0 + timedelta(hours=1), 11, 910, 51)])
    store.insert_mmr_snapshot_rows([_row(t0 + timedelta(hours=2), 11, 920, 52)])

    recent = store.mmr_history(
        "76561198000000001", 11, since=t0 + timedelta(minutes=90)
    )
    assert [r.rating for r in recent] == [920]
