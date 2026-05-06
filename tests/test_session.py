from rl_tracker.session import SessionState


def created(guid: str, local_team: int) -> dict:
    return {"event": "MatchCreated", "data": {"match_guid": guid, "local_team": local_team}}


def ended(guid: str, playlist: str, winning_team: int, local_team: int | None = None) -> dict:
    data = {"match_guid": guid, "playlist": playlist, "winning_team": winning_team}
    if local_team is not None:
        data["local_team"] = local_team
    return {"event": "MatchEnded", "data": data}


def test_win_increments():
    s = SessionState()
    s.apply(created("a", 0))
    s.apply(ended("a", "ranked-duels", winning_team=0))
    assert s.by_playlist["ranked-duels"].wins == 1
    assert s.by_playlist["ranked-duels"].losses == 0


def test_loss_increments():
    s = SessionState()
    s.apply(created("a", 1))
    s.apply(ended("a", "ranked-doubles", winning_team=0))
    assert s.by_playlist["ranked-doubles"].losses == 1


def test_local_team_inline_overrides_match_guid_lookup():
    s = SessionState()
    s.apply(ended("only-end", "casual", winning_team=1, local_team=1))
    assert s.by_playlist["casual"].wins == 1


def test_multiple_playlists_tracked_separately():
    s = SessionState()
    s.apply(created("1", 0))
    s.apply(ended("1", "duels", winning_team=0))
    s.apply(created("2", 0))
    s.apply(ended("2", "doubles", winning_team=1))
    assert s.by_playlist["duels"].wins == 1
    assert s.by_playlist["doubles"].losses == 1


def test_pascal_case_keys_accepted():
    s = SessionState()
    s.apply({"Event": "MatchCreated", "Data": {"MatchGuid": "x", "LocalTeam": 0}})
    s.apply({"Event": "MatchEnded", "Data": {"MatchGuid": "x", "Playlist": "ranked", "WinningTeam": 0}})
    assert s.by_playlist["ranked"].wins == 1


def test_reset_clears_everything():
    s = SessionState()
    started = s.started_at
    s.apply(created("a", 0))
    s.apply(ended("a", "ranked", winning_team=0))
    s.reset()
    assert s.by_playlist == {}
    assert s.started_at >= started


def test_missing_team_info_counts_match_but_not_wl():
    s = SessionState()
    s.apply({"event": "MatchEnded", "data": {"playlist": "x", "winning_team": 0}})
    tally = s.by_playlist["x"]
    assert tally.wins == 0 and tally.losses == 0
    assert tally.last_match_at is not None


def test_totals_aggregates_across_playlists():
    s = SessionState()
    s.apply(created("1", 0))
    s.apply(ended("1", "a", winning_team=0))
    s.apply(created("2", 0))
    s.apply(ended("2", "b", winning_team=1))
    t = s.totals()
    assert t.wins == 1 and t.losses == 1
