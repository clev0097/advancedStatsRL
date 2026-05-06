from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


@dataclass
class PlayerRecord:
    platform_id: str
    name: str
    team: int
    is_me: bool = False


@dataclass
class MatchRecord:
    match_guid: str
    playlist: str
    started_at: datetime
    ended_at: datetime
    won: bool | None  # None when self isn't on roster (MY_PLATFORM_ID unset/wrong)
    my_team: int | None
    winner_team: int | None
    players: list[PlayerRecord] = field(default_factory=list)


@dataclass
class TeammateAggregate:
    teammates: tuple[str, ...]  # sorted platform IDs
    teammate_names: tuple[str, ...]
    playlist: str
    wins: int
    losses: int
    unknown: int  # matches where won is None

    @property
    def total(self) -> int:
        return self.wins + self.losses + self.unknown


_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_guid    TEXT NOT NULL UNIQUE,
    playlist      TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    ended_at      TEXT NOT NULL,
    won           INTEGER,
    my_team       INTEGER,
    winner_team   INTEGER
);

CREATE TABLE IF NOT EXISTS match_players (
    match_id     INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    platform_id  TEXT NOT NULL,
    name         TEXT NOT NULL,
    team         INTEGER NOT NULL,
    is_me        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (match_id, platform_id)
);

CREATE INDEX IF NOT EXISTS idx_match_players_platform ON match_players(platform_id);
"""


class HistoryStore:
    """Persistent SQLite store for completed matches and rosters."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def record(self, match: MatchRecord) -> bool:
        """Insert a match. Returns False if match_guid already exists (idempotent)."""
        cur = self._conn.cursor()
        try:
            cur.execute(
                "INSERT INTO matches (match_guid, playlist, started_at, ended_at, won, my_team, winner_team) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    match.match_guid,
                    match.playlist,
                    match.started_at.isoformat(),
                    match.ended_at.isoformat(),
                    None if match.won is None else int(match.won),
                    match.my_team,
                    match.winner_team,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        match_id = cur.lastrowid
        cur.executemany(
            "INSERT OR IGNORE INTO match_players (match_id, platform_id, name, team, is_me) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (match_id, p.platform_id, p.name, p.team, int(p.is_me))
                for p in match.players
            ],
        )
        self._conn.commit()
        return True

    def teammate_breakdown(
        self, my_platform_id: str | None, playlist: str | None = None
    ) -> list[TeammateAggregate]:
        """Group matches by exact teammate set (excluding self). Requires MY_PLATFORM_ID
        to identify self; without it, returns []."""
        if not my_platform_id:
            return []

        sql = (
            "SELECT m.id, m.playlist, m.won FROM matches m "
            "JOIN match_players mp ON mp.match_id = m.id AND mp.platform_id = ? "
        )
        params: list[object] = [my_platform_id]
        if playlist:
            sql += "WHERE m.playlist = ? "
            params.append(playlist)

        cur = self._conn.cursor()
        cur.execute(sql, params)
        my_matches = cur.fetchall()
        if not my_matches:
            return []

        # For each match, fetch teammates (same team, excluding self).
        groups: dict[tuple[tuple[str, ...], str], dict] = {}
        for match_id, m_playlist, won in my_matches:
            cur.execute(
                "SELECT mp2.platform_id, mp2.name FROM match_players mp1 "
                "JOIN match_players mp2 ON mp1.match_id = mp2.match_id AND mp1.team = mp2.team "
                "WHERE mp1.match_id = ? AND mp1.platform_id = ? AND mp2.platform_id != ?",
                (match_id, my_platform_id, my_platform_id),
            )
            tm = sorted(cur.fetchall(), key=lambda r: r[0])
            tm_ids = tuple(r[0] for r in tm)
            tm_names = tuple(r[1] for r in tm)
            key = (tm_ids, m_playlist)
            agg = groups.setdefault(
                key, {"names": tm_names, "wins": 0, "losses": 0, "unknown": 0}
            )
            if won is None:
                agg["unknown"] += 1
            elif won:
                agg["wins"] += 1
            else:
                agg["losses"] += 1

        out: list[TeammateAggregate] = []
        for (tm_ids, pl), agg in groups.items():
            out.append(
                TeammateAggregate(
                    teammates=tm_ids,
                    teammate_names=agg["names"],
                    playlist=pl,
                    wins=agg["wins"],
                    losses=agg["losses"],
                    unknown=agg["unknown"],
                )
            )
        out.sort(key=lambda a: (-a.total, a.playlist, a.teammate_names))
        return out

    def recent(self, limit: int = 50) -> list[dict]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id, match_guid, playlist, ended_at, won, my_team, winner_team "
            "FROM matches ORDER BY ended_at DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        out: list[dict] = []
        for mid, guid, playlist, ended_at, won, my_team, winner_team in rows:
            cur.execute(
                "SELECT platform_id, name, team, is_me FROM match_players WHERE match_id = ?",
                (mid,),
            )
            players = [
                {"platform_id": pid, "name": n, "team": t, "is_me": bool(im)}
                for pid, n, t, im in cur.fetchall()
            ]
            out.append(
                {
                    "match_guid": guid,
                    "playlist": playlist,
                    "ended_at": ended_at,
                    "won": None if won is None else bool(won),
                    "my_team": my_team,
                    "winner_team": winner_team,
                    "players": players,
                }
            )
        return out
