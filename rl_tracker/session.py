from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .history import HistoryStore, MatchRecord, PlayerRecord


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Map roster-derived team sizes to playlist labels. The Stats API doesn't tell
# us the playlist directly; we infer from the final roster.
_PLAYLIST_BY_SIZE = {
    (1, 1): "duels",
    (2, 2): "doubles",
    (3, 3): "standard",
    (4, 4): "chaos",
}


@dataclass
class PlaylistTally:
    wins: int = 0
    losses: int = 0
    last_match_at: datetime | None = None

    @property
    def total(self) -> int:
        return self.wins + self.losses


@dataclass
class _RosterSnapshot:
    started_at: datetime
    players: list[dict]  # raw roster entries (Name, PrimaryId, TeamNum)
    # Largest team sizes ever seen during the match. Used to derive the
    # playlist label so a mid-match ragequit doesn't downgrade 1v1 -> 1v0.
    max_team_sizes: tuple[int, int] = (0, 0)
    # Roster captured on the first UpdateState. Used to backfill players who
    # left before MatchEnded and weren't replaced, so e.g. a teammate who
    # ragequits still appears in the recorded match.
    initial_players: list[dict] = field(default_factory=list)
    team0_score: int = 0
    team1_score: int = 0
    # Sticky: once true, stays true even if a later frame flips it back.
    overtime: bool = False


@dataclass
class SessionState:
    started_at: datetime = field(default_factory=_now)
    by_playlist: dict[str, PlaylistTally] = field(default_factory=dict)
    my_platform_id: str | None = None
    history: HistoryStore | None = None
    on_match_recorded: Callable[[MatchRecord], None] | None = None
    streak_count: int = 0
    streak_kind: str | None = None  # "W", "L", or None

    # match_guid -> latest roster snapshot
    _rosters: dict[str, _RosterSnapshot] = field(default_factory=dict, repr=False)
    # match_guids already persisted (don't double-count)
    _recorded: set[str] = field(default_factory=set, repr=False)

    def reset(self) -> None:
        self.started_at = _now()
        self.by_playlist.clear()
        self._rosters.clear()
        self._recorded.clear()
        self.streak_count = 0
        self.streak_kind = None

    def totals(self) -> PlaylistTally:
        out = PlaylistTally()
        for t in self.by_playlist.values():
            out.wins += t.wins
            out.losses += t.losses
        return out

    # ------------------------------------------------------------------
    def apply(self, event: dict[str, Any]) -> None:
        name = (event.get("Event") or event.get("event") or "").lower()
        data = event.get("Data") if isinstance(event.get("Data"), dict) else event.get("data")
        if not isinstance(data, dict):
            data = event

        if name == "updatestate":
            self._on_update_state(data)
        elif name in ("matchended", "match_ended"):
            self._on_match_ended(data)

    @staticmethod
    def _get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
        for k in keys:
            if k in d:
                return d[k]
        return default

    # ------------------------------------------------------------------
    def _on_update_state(self, data: dict[str, Any]) -> None:
        guid = self._get(data, "MatchGuid", "match_guid")
        if not guid:
            return
        players_raw = self._get(data, "Players", "players")
        if not isinstance(players_raw, list):
            return
        players: list[dict] = []
        for p in players_raw:
            if not isinstance(p, dict):
                continue
            pid = self._get(p, "PrimaryId", "primary_id")
            name = self._get(p, "Name", "name")
            team = self._get(p, "TeamNum", "team_num")
            if pid is None or name is None or team is None:
                continue
            players.append({"PrimaryId": str(pid), "Name": str(name), "TeamNum": int(team)})
        if not players:
            return
        sizes = (
            sum(1 for p in players if p["TeamNum"] == 0),
            sum(1 for p in players if p["TeamNum"] == 1),
        )
        snap = self._rosters.get(guid)
        if snap is None:
            self._rosters[guid] = _RosterSnapshot(
                started_at=_now(),
                players=players,
                max_team_sizes=sizes,
                initial_players=list(players),
            )
        else:
            snap.players = players
            snap.max_team_sizes = (
                max(snap.max_team_sizes[0], sizes[0]),
                max(snap.max_team_sizes[1], sizes[1]),
            )

        snap = self._rosters[guid]
        game = self._get(data, "Game", "game")
        if isinstance(game, dict):
            teams = self._get(game, "Teams", "teams")
            if isinstance(teams, list):
                for t in teams:
                    if not isinstance(t, dict):
                        continue
                    team_num = self._get(t, "TeamNum", "team_num")
                    score = self._get(t, "Score", "score")
                    try:
                        team_num_i = int(team_num)
                        score_i = int(score)
                    except (TypeError, ValueError):
                        continue
                    if team_num_i == 0:
                        snap.team0_score = score_i
                    elif team_num_i == 1:
                        snap.team1_score = score_i
            if bool(self._get(game, "bOvertime", "b_overtime", default=False)):
                snap.overtime = True

    def _on_match_ended(self, data: dict[str, Any]) -> None:
        guid = str(self._get(data, "MatchGuid", "match_guid", "guid", default=""))
        if not guid or guid in self._recorded:
            return
        winner_team = self._get(data, "WinnerTeamNum", "winning_team", "winner")
        try:
            winner_team_int = int(winner_team) if winner_team is not None else None
        except (TypeError, ValueError):
            winner_team_int = None

        snap = self._rosters.get(guid)
        if snap is None:
            return  # No roster captured; can't attribute teammates.

        # Backfill players who were in the initial roster but left before the
        # match ended and weren't replaced. If a slot was filled by a late
        # joiner the final roster meets the max team size and nothing is added.
        roster: list[dict] = list(snap.players)
        present_ids = {p["PrimaryId"] for p in roster}
        for team in (0, 1):
            current = sum(1 for p in roster if p["TeamNum"] == team)
            gap = snap.max_team_sizes[team] - current
            if gap <= 0:
                continue
            for p in snap.initial_players:
                if gap <= 0:
                    break
                if p["TeamNum"] != team or p["PrimaryId"] in present_ids:
                    continue
                roster.append(p)
                present_ids.add(p["PrimaryId"])
                gap -= 1

        # Identify self (if MY_PLATFORM_ID is configured and matches a roster entry).
        my_team: int | None = None
        player_records: list[PlayerRecord] = []
        for p in roster:
            is_me = bool(self.my_platform_id) and p["PrimaryId"] == self.my_platform_id
            if is_me:
                my_team = p["TeamNum"]
            player_records.append(
                PlayerRecord(
                    platform_id=p["PrimaryId"],
                    name=p["Name"],
                    team=p["TeamNum"],
                    is_me=is_me,
                )
            )

        # Derive playlist from the largest roster seen during the match so
        # that a mid-match ragequit doesn't reclassify e.g. duels as 1v0.
        team_sizes = snap.max_team_sizes
        playlist = _PLAYLIST_BY_SIZE.get(team_sizes, f"{team_sizes[0]}v{team_sizes[1]}")

        won: bool | None
        if my_team is not None and winner_team_int is not None:
            won = my_team == winner_team_int
        else:
            won = None

        ended_at = _now()
        record = MatchRecord(
            match_guid=guid,
            playlist=playlist,
            started_at=snap.started_at,
            ended_at=ended_at,
            won=won,
            my_team=my_team,
            winner_team=winner_team_int,
            players=player_records,
            team0_score=snap.team0_score,
            team1_score=snap.team1_score,
            overtime=snap.overtime,
        )

        # Update session tally.
        tally = self.by_playlist.setdefault(playlist, PlaylistTally())
        tally.last_match_at = ended_at
        if won is True:
            tally.wins += 1
            if self.streak_kind == "W":
                self.streak_count += 1
            else:
                self.streak_kind = "W"
                self.streak_count = 1
        elif won is False:
            tally.losses += 1
            if self.streak_kind == "L":
                self.streak_count += 1
            else:
                self.streak_kind = "L"
                self.streak_count = 1

        self._recorded.add(guid)
        self._rosters.pop(guid, None)

        if self.history is not None:
            try:
                self.history.record(record)
            except Exception:
                pass
        if self.on_match_recorded is not None:
            try:
                self.on_match_recorded(record)
            except Exception:
                pass
