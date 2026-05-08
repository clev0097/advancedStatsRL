from __future__ import annotations

import json
from pathlib import Path

import pytest

from rl_tracker.history import HistoryStore
from rl_tracker.session import SessionState


EVENTS_LOG = Path(__file__).resolve().parents[1] / "events.log"
TARGET_GUID = "BD582D5411F149901775DCB16D034FCF"


def _replay(history: HistoryStore) -> None:
    session = SessionState(history=history, my_platform_id=None)
    with EVENTS_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            session.apply(ev)


@pytest.fixture
def replayed(tmp_path) -> HistoryStore:
    if not EVENTS_LOG.exists():
        pytest.skip("events.log not present")
    db = HistoryStore(tmp_path / "history.db")
    _replay(db)
    return db


def test_replay_creates_match(replayed: HistoryStore):
    matches = replayed.all_matches()
    guids = [m["match_guid"] for m in matches]
    assert TARGET_GUID in guids


def test_replay_writes_events_and_ticks(replayed: HistoryStore):
    cur = replayed.connection().cursor()
    cur.execute("SELECT id FROM matches WHERE match_guid = ?", (TARGET_GUID,))
    row = cur.fetchone()
    assert row is not None
    match_id = row[0]
    cur.execute("SELECT COUNT(*) FROM match_events WHERE match_id = ?", (match_id,))
    assert cur.fetchone()[0] > 0
    cur.execute("SELECT COUNT(*) FROM match_ticks WHERE match_id = ?", (match_id,))
    assert cur.fetchone()[0] > 0
    cur.execute(
        "SELECT COUNT(*) FROM match_events WHERE match_id = ? AND kind IN ('ball_hit','fifty')",
        (match_id,),
    )
    assert cur.fetchone()[0] > 0


def test_replay_player_stats_plausible(replayed: HistoryStore):
    rows = replayed.player_stats_for_match(TARGET_GUID)
    assert rows, "expected per-player stats rows"
    names = {r["player_name"] for r in rows}
    assert "Bill Cement" in names

    for r in rows:
        if r["boost_avg"] is not None:
            assert 0.0 <= r["boost_avg"] <= 100.0
        if r["possession_pct_team"] is not None:
            assert 0.0 <= r["possession_pct_team"] <= 1.0
        if r["aerial_pct"] is not None:
            assert 0.0 <= r["aerial_pct"] <= 1.0
        if r["supersonic_pct"] is not None:
            assert 0.0 <= r["supersonic_pct"] <= 1.0
        assert r["touches"] >= 0
        assert r["shots"] >= 0
        assert r["goals"] >= 0


def test_replay_team_possession_sums_reasonable(replayed: HistoryStore):
    rows = replayed.player_stats_for_match(TARGET_GUID)
    by_team = {}
    for r in rows:
        by_team.setdefault(r["player_team"], r["possession_pct_team"])
    # Team possession excludes neutral ticks, so sum should be in (0, 1].
    total = sum(v for v in by_team.values() if v is not None)
    assert 0.0 < total <= 1.0 + 1e-9
