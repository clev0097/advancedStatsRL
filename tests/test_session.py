from __future__ import annotations

import pytest

from rl_tracker.history import HistoryStore
from rl_tracker.session import SessionState

ME = "Steam|76561198000487482|0"
MATE = "Epic|51ed0115a80a4f958cf03a430611ba6e|0"
OPP1 = "XboxOne|2535418161062515|0"
OPP2 = "Unknown|0|0"


def update_state(
    guid: str,
    players: list[tuple[str, str, int]],
    team_scores: tuple[int, int] | None = None,
    overtime: bool = False,
) -> dict:
    data: dict = {
        "MatchGuid": guid,
        "Players": [
            {"PrimaryId": pid, "Name": name, "TeamNum": team}
            for pid, name, team in players
        ],
    }
    if team_scores is not None or overtime:
        t0, t1 = team_scores if team_scores is not None else (0, 0)
        data["Game"] = {
            "Teams": [
                {"TeamNum": 0, "Score": t0},
                {"TeamNum": 1, "Score": t1},
            ],
            "bOvertime": overtime,
        }
    return {"Event": "UpdateState", "Data": data}


def match_ended(guid: str, winner_team: int) -> dict:
    return {"Event": "MatchEnded", "Data": {"MatchGuid": guid, "WinnerTeamNum": winner_team}}


@pytest.fixture
def store() -> HistoryStore:
    return HistoryStore(":memory:")


def test_doubles_win_attributed_to_self(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 1), (MATE, "Friend", 1), (OPP1, "Opp1", 0), (OPP2, "Opp2", 0)]))
    s.apply(match_ended("g1", winner_team=1))

    assert s.by_playlist["doubles"].wins == 1
    assert s.by_playlist["doubles"].losses == 0

    recent = store.recent()
    assert len(recent) == 1
    r = recent[0]
    assert r["playlist"] == "doubles"
    assert r["won"] is True
    assert r["my_team"] == 1
    me_rec = next(p for p in r["players"] if p["is_me"])
    assert me_rec["platform_id"] == ME


def test_loss_when_other_team_wins(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (MATE, "Friend", 0), (OPP1, "O1", 1), (OPP2, "O2", 1)]))
    s.apply(match_ended("g1", winner_team=1))
    assert s.by_playlist["doubles"].losses == 1


def test_playlist_inferred_from_team_sizes(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    # 3v3 = standard
    roster = [(ME, "Me", 0), (MATE, "F", 0), (OPP2, "F2", 0), (OPP1, "X", 1), ("p4", "Y", 1), ("p5", "Z", 1)]
    s.apply(update_state("g3", roster))
    s.apply(match_ended("g3", winner_team=0))
    assert "standard" in s.by_playlist
    # 1v1 = duels
    s.apply(update_state("g4", [(ME, "Me", 0), (OPP1, "X", 1)]))
    s.apply(match_ended("g4", winner_team=1))
    assert "duels" in s.by_playlist


def test_no_my_platform_id_records_match_with_unknown_result(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=None)
    s.apply(update_state("g1", [(ME, "Me", 0), (MATE, "F", 0), (OPP1, "X", 1), (OPP2, "Y", 1)]))
    s.apply(match_ended("g1", winner_team=0))
    # Tally not incremented (can't attribute), but match is persisted.
    assert s.by_playlist["doubles"].wins == 0
    assert s.by_playlist["doubles"].losses == 0
    recent = store.recent()
    assert len(recent) == 1
    assert recent[0]["won"] is None


def test_match_ended_without_roster_is_skipped(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(match_ended("orphan", winner_team=0))
    assert store.recent() == []
    assert "doubles" not in s.by_playlist


def test_duplicate_match_ended_is_idempotent(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 1), (MATE, "F", 1), (OPP1, "X", 0), (OPP2, "Y", 0)]))
    s.apply(match_ended("g1", winner_team=1))
    s.apply(match_ended("g1", winner_team=1))  # replay
    assert s.by_playlist["doubles"].wins == 1
    assert len(store.recent()) == 1


def test_roster_snapshot_reflects_latest_update(store: HistoryStore):
    """Mid-match a player drops, joiner replaces them; final roster is what we record."""
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (MATE, "F", 0), (OPP1, "X", 1), (OPP2, "Y", 1)]))
    # Late-join replaces OPP2 with new player.
    s.apply(update_state("g1", [(ME, "Me", 0), (MATE, "F", 0), (OPP1, "X", 1), ("Steam|999|0", "Joined", 1)]))
    s.apply(match_ended("g1", winner_team=0))
    recent = store.recent()
    pids = {p["platform_id"] for p in recent[0]["players"]}
    assert "Steam|999|0" in pids
    assert OPP2 not in pids


def test_teammate_ragequit_falls_back_to_initial_roster(store: HistoryStore):
    """Teammate leaves before MatchEnded with no replacement; original teammate is still recorded."""
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (MATE, "Friend", 0), (OPP1, "X", 1), (OPP2, "Y", 1)]))
    # Teammate quits; opponent OPP2 also drops.
    s.apply(update_state("g1", [(ME, "Me", 0), (OPP1, "X", 1)]))
    s.apply(match_ended("g1", winner_team=1))

    recent = store.recent()
    assert recent[0]["playlist"] == "doubles"
    pids = {p["platform_id"] for p in recent[0]["players"]}
    assert MATE in pids  # teammate from initial roster preserved
    assert OPP2 in pids  # opponent from initial roster preserved
    assert OPP1 in pids


def test_late_join_replacement_overrides_initial(store: HistoryStore):
    """A replacement filling the slot wins over the initial roster fallback."""
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (MATE, "Friend", 0), (OPP1, "X", 1), (OPP2, "Y", 1)]))
    # MATE leaves and is replaced by a new teammate.
    new_mate = "Steam|777|0"
    s.apply(update_state("g1", [(ME, "Me", 0), (new_mate, "NewMate", 0), (OPP1, "X", 1), (OPP2, "Y", 1)]))
    s.apply(match_ended("g1", winner_team=0))

    pids = {p["platform_id"] for p in store.recent()[0]["players"]}
    assert new_mate in pids
    assert MATE not in pids  # replaced, so original teammate is dropped


def test_duel_ragequit_still_recorded_as_duels(store: HistoryStore):
    """Opponent leaves before MatchEnded; playlist must stay 'duels', not '1v0'."""
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (OPP1, "X", 1)]))
    # Opponent quits; only self remains in the final UpdateState.
    s.apply(update_state("g1", [(ME, "Me", 0)]))
    s.apply(match_ended("g1", winner_team=0))

    assert "duels" in s.by_playlist
    assert "1v0" not in s.by_playlist
    assert s.by_playlist["duels"].wins == 1
    assert store.recent()[0]["playlist"] == "duels"


def test_teammate_breakdown_groups_by_exact_set(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    # 2v2 with Friend: win
    s.apply(update_state("a", [(ME, "Me", 0), (MATE, "Friend", 0), (OPP1, "X", 1), (OPP2, "Y", 1)]))
    s.apply(match_ended("a", winner_team=0))
    # 2v2 with Friend: loss
    s.apply(update_state("b", [(ME, "Me", 1), (MATE, "Friend", 1), (OPP1, "X", 0), (OPP2, "Y", 0)]))
    s.apply(match_ended("b", winner_team=0))
    # 3v3 with Friend + OtherFriend: win
    other = "Steam|11111|0"
    s.apply(update_state("c", [
        (ME, "Me", 0), (MATE, "Friend", 0), (other, "Other", 0),
        (OPP1, "X", 1), (OPP2, "Y", 1), ("p", "Z", 1),
    ]))
    s.apply(match_ended("c", winner_team=0))

    breakdown = store.teammate_breakdown(ME)
    by_key = {(a.teammates, a.playlist): a for a in breakdown}
    assert by_key[((MATE,), "doubles")].wins == 1
    assert by_key[((MATE,), "doubles")].losses == 1
    triple_key = (tuple(sorted([MATE, other])), "standard")
    assert by_key[triple_key].wins == 1
    assert by_key[triple_key].losses == 0


def test_streak_increments_on_consecutive_wins(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (OPP1, "X", 1)]))
    s.apply(match_ended("g1", winner_team=0))
    s.apply(update_state("g2", [(ME, "Me", 0), (OPP1, "X", 1)]))
    s.apply(match_ended("g2", winner_team=0))
    assert s.streak_kind == "W"
    assert s.streak_count == 2


def test_streak_resets_on_loss_after_win(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (OPP1, "X", 1)]))
    s.apply(match_ended("g1", winner_team=0))
    s.apply(update_state("g2", [(ME, "Me", 0), (OPP1, "X", 1)]))
    s.apply(match_ended("g2", winner_team=1))
    assert s.streak_kind == "L"
    assert s.streak_count == 1


def test_streak_flips_on_win_after_loss(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (OPP1, "X", 1)]))
    s.apply(match_ended("g1", winner_team=1))
    s.apply(update_state("g2", [(ME, "Me", 0), (OPP1, "X", 1)]))
    s.apply(match_ended("g2", winner_team=0))
    assert s.streak_kind == "W"
    assert s.streak_count == 1


def test_streak_unchanged_when_result_unknown(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (OPP1, "X", 1)]))
    s.apply(match_ended("g1", winner_team=0))
    # Second match without my_platform_id means won is None.
    s.my_platform_id = None
    s.apply(update_state("g2", [(ME, "Me", 0), (OPP1, "X", 1)]))
    s.apply(match_ended("g2", winner_team=1))
    assert s.streak_kind == "W"
    assert s.streak_count == 1


def test_reset_clears_streak(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (OPP1, "X", 1)]))
    s.apply(match_ended("g1", winner_team=0))
    s.reset()
    assert s.streak_kind is None
    assert s.streak_count == 0


def test_reset_clears_session_but_not_history(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (MATE, "F", 0), (OPP1, "X", 1), (OPP2, "Y", 1)]))
    s.apply(match_ended("g1", winner_team=0))
    s.reset()
    assert s.by_playlist == {}
    assert len(store.recent()) == 1  # persistence survives session reset


def test_team_scores_and_overtime_recorded(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    roster = [(ME, "Me", 0), (MATE, "F", 0), (OPP1, "X", 1), (OPP2, "Y", 1)]
    s.apply(update_state("g1", roster, team_scores=(1, 1)))
    # OT flag flips to true mid-match, then a later frame happens to omit it.
    s.apply(update_state("g1", roster, team_scores=(2, 2), overtime=True))
    s.apply(update_state("g1", roster, team_scores=(3, 2), overtime=False))
    s.apply(match_ended("g1", winner_team=0))

    r = store.recent()[0]
    assert r["team0_score"] == 3
    assert r["team1_score"] == 2
    assert r["overtime"] is True  # sticky once set


def test_match_without_game_payload_defaults_zero(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (MATE, "F", 0), (OPP1, "X", 1), (OPP2, "Y", 1)]))
    s.apply(match_ended("g1", winner_team=0))
    r = store.recent()[0]
    assert r["team0_score"] == 0
    assert r["team1_score"] == 0
    assert r["overtime"] is False


class _StubWatcher:
    def __init__(self, snap):
        self._snap = snap

    def snapshot(self):
        return self._snap


class _StubSnap:
    def __init__(self, playlist_id, playlist_label, seen_at):
        self.playlist_id = playlist_id
        self.playlist_label = playlist_label
        self.seen_at = seen_at


def test_log_watcher_label_overrides_size_derived_playlist(store: HistoryStore):
    from datetime import datetime, timezone
    snap = _StubSnap(11, "Ranked Doubles", datetime.now(timezone.utc))
    s = SessionState(history=store, my_platform_id=ME, playlist_watcher=_StubWatcher(snap))
    s.apply(update_state("g1", [(ME, "Me", 1), (MATE, "F", 1), (OPP1, "X", 0), (OPP2, "Y", 0)]))
    s.apply(match_ended("g1", winner_team=1))

    assert s.by_playlist["Ranked Doubles"].wins == 1
    assert "doubles" not in s.by_playlist
    r = store.recent()[0]
    assert r["playlist"] == "Ranked Doubles"
    assert r["playlist_id"] == 11


def test_log_watcher_absent_falls_back_to_size_label(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME, playlist_watcher=None)
    s.apply(update_state("g1", [(ME, "Me", 1), (MATE, "F", 1), (OPP1, "X", 0), (OPP2, "Y", 0)]))
    s.apply(match_ended("g1", winner_team=1))
    r = store.recent()[0]
    assert r["playlist"] == "doubles"
    assert r["playlist_id"] is None


def test_log_watcher_with_no_snapshot_falls_back(store: HistoryStore):
    snap = _StubSnap(None, None, None)
    s = SessionState(history=store, my_platform_id=ME, playlist_watcher=_StubWatcher(snap))
    s.apply(update_state("g1", [(ME, "Me", 1), (MATE, "F", 1), (OPP1, "X", 0), (OPP2, "Y", 0)]))
    s.apply(match_ended("g1", winner_team=1))
    r = store.recent()[0]
    assert r["playlist"] == "doubles"
    assert r["playlist_id"] is None


def test_log_watcher_stale_snapshot_is_ignored(store: HistoryStore):
    """A snapshot from before the match started (>60s prior) should not be applied."""
    from datetime import datetime, timedelta, timezone
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    snap = _StubSnap(34, "Tournament", stale)
    s = SessionState(history=store, my_platform_id=ME, playlist_watcher=_StubWatcher(snap))
    s.apply(update_state("g1", [(ME, "Me", 1), (MATE, "F", 1), (OPP1, "X", 0), (OPP2, "Y", 0)]))
    s.apply(match_ended("g1", winner_team=1))
    r = store.recent()[0]
    assert r["playlist"] == "doubles"
    assert r["playlist_id"] is None


def test_history_store_migrates_legacy_schema(tmp_path):
    import sqlite3
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE matches (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            match_guid    TEXT NOT NULL UNIQUE,
            playlist      TEXT NOT NULL,
            started_at    TEXT NOT NULL,
            ended_at      TEXT NOT NULL,
            won           INTEGER,
            my_team       INTEGER,
            winner_team   INTEGER
        );
        CREATE TABLE match_players (
            match_id     INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
            platform_id  TEXT NOT NULL,
            name         TEXT NOT NULL,
            team         INTEGER NOT NULL,
            is_me        INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (match_id, platform_id)
        );
        """
    )
    conn.execute(
        "INSERT INTO matches (match_guid, playlist, started_at, ended_at, won, my_team, winner_team) "
        "VALUES ('old', 'doubles', '2024-01-01T00:00:00+00:00', '2024-01-01T00:05:00+00:00', 1, 0, 0)"
    )
    conn.commit()
    conn.close()

    store2 = HistoryStore(str(db))
    r = store2.recent()
    assert len(r) == 1
    assert r[0]["team0_score"] == 0
    assert r[0]["team1_score"] == 0
    assert r[0]["overtime"] is False
    assert r[0]["playlist_id"] is None
    store2.close()
