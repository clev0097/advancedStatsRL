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


@dataclass
class SessionState:
    started_at: datetime = field(default_factory=_now)
    by_playlist: dict[str, PlaylistTally] = field(default_factory=dict)
    my_platform_id: str | None = None
    history: HistoryStore | None = None
    on_match_recorded: Callable[[MatchRecord], None] | None = None

    # match_guid -> latest roster snapshot
    _rosters: dict[str, _RosterSnapshot] = field(default_factory=dict, repr=False)
    # match_guids already persisted (don't double-count)
    _recorded: set[str] = field(default_factory=set, repr=False)

    def reset(self) -> None:
        self.started_at = _now()
        self.by_playlist.clear()
        self._rosters.clear()
        self._recorded.clear()

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
        snap = self._rosters.get(guid)
        if snap is None:
            self._rosters[guid] = _RosterSnapshot(started_at=_now(), players=players)
        else:
            snap.players = players

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

        # Identify self (if MY_PLATFORM_ID is configured and matches a roster entry).
        my_team: int | None = None
        player_records: list[PlayerRecord] = []
        for p in snap.players:
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

        # Derive playlist from roster sizes.
        team_sizes = (
            sum(1 for p in snap.players if p["TeamNum"] == 0),
            sum(1 for p in snap.players if p["TeamNum"] == 1),
        )
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
        )

        # Update session tally.
        tally = self.by_playlist.setdefault(playlist, PlaylistTally())
        tally.last_match_at = ended_at
        if won is True:
            tally.wins += 1
        elif won is False:
            tally.losses += 1

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
