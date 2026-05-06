from __future__ import annotations

import pytest

from rl_tracker.history import HistoryStore
from rl_tracker.session import SessionState

ME = "Steam|76561198000487482|0"
MATE = "Epic|51ed0115a80a4f958cf03a430611ba6e|0"
OPP1 = "XboxOne|2535418161062515|0"
OPP2 = "Unknown|0|0"


def update_state(guid: str, players: list[tuple[str, str, int]]) -> dict:
    return {
        "Event": "UpdateState",
        "Data": {
            "MatchGuid": guid,
            "Players": [
                {"PrimaryId": pid, "Name": name, "TeamNum": team}
                for pid, name, team in players
            ],
        },
    }


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


def test_reset_clears_session_but_not_history(store: HistoryStore):
    s = SessionState(history=store, my_platform_id=ME)
    s.apply(update_state("g1", [(ME, "Me", 0), (MATE, "F", 0), (OPP1, "X", 1), (OPP2, "Y", 1)]))
    s.apply(match_ended("g1", winner_team=0))
    s.reset()
    assert s.by_playlist == {}
    assert len(store.recent()) == 1  # persistence survives session reset
