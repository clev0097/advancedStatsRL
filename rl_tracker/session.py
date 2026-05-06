from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PlaylistTally:
    wins: int = 0
    losses: int = 0
    last_match_at: datetime | None = None

    @property
    def total(self) -> int:
        return self.wins + self.losses


@dataclass
class SessionState:
    started_at: datetime = field(default_factory=_now)
    by_playlist: dict[str, PlaylistTally] = field(default_factory=dict)
    # Tracks team assignment seen at MatchCreated, keyed by match_guid.
    _local_team_by_match: dict[str, int] = field(default_factory=dict, repr=False)

    def reset(self) -> None:
        self.started_at = _now()
        self.by_playlist.clear()
        self._local_team_by_match.clear()

    def totals(self) -> PlaylistTally:
        out = PlaylistTally()
        for t in self.by_playlist.values():
            out.wins += t.wins
            out.losses += t.losses
        return out

    def apply(self, event: dict[str, Any]) -> None:
        """Route a Stats API event into the session aggregator.

        Schema is best-effort: field names are normalized at parse time and we
        accept either snake_case or PascalCase keys to survive minor variations.
        """
        name = (event.get("event") or event.get("Event") or "").lower()
        data = event.get("data") or event.get("Data") or event

        if name in ("matchcreated", "match_created"):
            self._on_match_created(data)
        elif name in ("matchended", "match_ended"):
            self._on_match_ended(data)

    @staticmethod
    def _get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
        for k in keys:
            if k in d:
                return d[k]
        return default

    def _on_match_created(self, data: dict[str, Any]) -> None:
        guid = self._get(data, "match_guid", "MatchGuid", "matchGuid")
        local_team = self._get(data, "local_team", "LocalTeam", "localTeam")
        if guid is not None and local_team is not None:
            self._local_team_by_match[str(guid)] = int(local_team)

    def _on_match_ended(self, data: dict[str, Any]) -> None:
        playlist = str(self._get(data, "playlist", "Playlist", "playlist_id", default="unknown"))
        winning_team = self._get(data, "winning_team", "WinningTeam", "winner")
        local_team = self._get(data, "local_team", "LocalTeam", "localTeam")
        if local_team is None:
            guid = self._get(data, "match_guid", "MatchGuid", "matchGuid")
            if guid is not None:
                local_team = self._local_team_by_match.get(str(guid))

        tally = self.by_playlist.setdefault(playlist, PlaylistTally())
        tally.last_match_at = _now()

        if winning_team is None or local_team is None:
            # Schema didn't surface enough info; count as a played match without W/L.
            return

        if int(winning_team) == int(local_team):
            tally.wins += 1
        else:
            tally.losses += 1
