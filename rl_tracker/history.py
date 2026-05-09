from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    team0_score: int = 0
    team1_score: int = 0
    overtime: bool = False
    playlist_id: int | None = None  # numeric queue ID from Launch.log, when known


@dataclass
class TeammateAggregate:
    teammates: tuple[str, ...]  # sorted platform IDs
    teammate_names: tuple[str, ...]
    playlist: str
    wins: int
    losses: int
    unknown: int  # matches where won is None
    session_started_at: datetime | None = None
    session_ended_at: datetime | None = None

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
    winner_team   INTEGER,
    team0_score   INTEGER NOT NULL DEFAULT 0,
    team1_score   INTEGER NOT NULL DEFAULT 0,
    overtime      INTEGER NOT NULL DEFAULT 0,
    playlist_id   INTEGER
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

CREATE TABLE IF NOT EXISTS match_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    t_seconds       REAL NOT NULL,
    kind            TEXT NOT NULL,
    player_name     TEXT,
    player_team     INTEGER,
    secondary_name  TEXT,
    secondary_team  INTEGER,
    ball_x          REAL,
    ball_y          REAL,
    ball_z          REAL,
    pre_speed       REAL,
    post_speed      REAL,
    goal_speed      REAL,
    goal_time       REAL,
    impact_force    REAL,
    extra           TEXT
);
CREATE INDEX IF NOT EXISTS idx_match_events_match ON match_events(match_id, seq);
CREATE INDEX IF NOT EXISTS idx_match_events_kind ON match_events(match_id, kind);

CREATE TABLE IF NOT EXISTS match_ticks (
    match_id        INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    t_seconds       REAL NOT NULL,
    player_name     TEXT NOT NULL,
    player_team     INTEGER NOT NULL,
    boost           REAL,
    speed           REAL,
    b_boosting      INTEGER,
    b_on_ground     INTEGER,
    ball_speed      REAL,
    ball_team       INTEGER,
    PRIMARY KEY (match_id, seq, player_name)
);
CREATE INDEX IF NOT EXISTS idx_match_ticks_player ON match_ticks(match_id, player_name);

CREATE TABLE IF NOT EXISTS player_match_stats (
    match_id              INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    player_name           TEXT NOT NULL,
    player_team           INTEGER NOT NULL,
    -- Boost economy
    boost_avg             REAL,
    time_zero_boost_pct   REAL,
    time_full_boost_pct   REAL,
    boost_starved_pct     REAL,
    boost_used            REAL,
    -- Movement
    avg_speed             REAL,
    supersonic_pct        REAL,
    slow_pct              REAL,
    aerial_pct            REAL,
    -- Possession / tempo
    possession_pct_team   REAL,
    touch_share           REAL,
    time_in_off_third_pct REAL,
    -- Touch quality
    touches               INTEGER,
    avg_touch_pace_added  REAL,
    big_hits              INTEGER,
    fifty_attempts        INTEGER,
    fifty_wins            INTEGER,
    avg_touch_y           REAL,
    -- Shooting / finishing
    shots                 INTEGER,
    goals                 INTEGER,
    assists               INTEGER,
    avg_shot_speed        REAL,
    avg_goal_speed        REAL,
    xg_lite               REAL,
    crossbars             INTEGER,
    -- Defense
    saves                 INTEGER,
    epic_saves            INTEGER,
    avg_save_speed        REAL,
    shots_faced           INTEGER,
    -- Combat
    demos_dealt           INTEGER,
    demos_taken           INTEGER,
    demo_assists          INTEGER,
    PRIMARY KEY (match_id, player_name)
);
"""


def assign_sessions(
    ended_ats: list[datetime], gap_minutes: int
) -> list[int]:
    """Given a chronologically-ascending list of end times, return per-item
    session indices. A new session begins when the gap to the previous match
    exceeds ``gap_minutes``."""
    gap = timedelta(minutes=gap_minutes)
    out: list[int] = []
    idx = -1
    prev: datetime | None = None
    for ended_at in ended_ats:
        if prev is None or (ended_at - prev) > gap:
            idx += 1
        out.append(idx)
        prev = ended_at
    return out


class HistoryStore:
    """Persistent SQLite store for completed matches and rosters."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(matches)")
        existing = {row[1] for row in cur.fetchall()}
        for col, ddl in (
            ("team0_score", "INTEGER NOT NULL DEFAULT 0"),
            ("team1_score", "INTEGER NOT NULL DEFAULT 0"),
            ("overtime", "INTEGER NOT NULL DEFAULT 0"),
            ("playlist_id", "INTEGER"),
        ):
            if col not in existing:
                cur.execute(f"ALTER TABLE matches ADD COLUMN {col} {ddl}")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def record(self, match: MatchRecord) -> bool:
        """Insert a match. Returns False if match_guid already exists (idempotent)."""
        return self.record_with_id(match) is not None

    def record_with_id(self, match: MatchRecord) -> int | None:
        """Insert a match and return its rowid, or None if duplicate."""
        cur = self._conn.cursor()
        try:
            cur.execute(
                "INSERT INTO matches (match_guid, playlist, started_at, ended_at, won, my_team, winner_team, team0_score, team1_score, overtime, playlist_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    match.match_guid,
                    match.playlist,
                    match.started_at.isoformat(),
                    match.ended_at.isoformat(),
                    None if match.won is None else int(match.won),
                    match.my_team,
                    match.winner_team,
                    int(match.team0_score),
                    int(match.team1_score),
                    int(match.overtime),
                    match.playlist_id,
                ),
            )
        except sqlite3.IntegrityError:
            return None
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
        return match_id

    def insert_events(self, match_id: int, rows: Iterable[tuple]) -> None:
        cur = self._conn.cursor()
        cur.executemany(
            "INSERT INTO match_events (match_id, seq, t_seconds, kind, "
            "player_name, player_team, secondary_name, secondary_team, "
            "ball_x, ball_y, ball_z, pre_speed, post_speed, goal_speed, goal_time, "
            "impact_force, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(match_id, *r) for r in rows],
        )
        self._conn.commit()

    def insert_ticks(self, match_id: int, rows: Iterable[tuple]) -> None:
        cur = self._conn.cursor()
        cur.executemany(
            "INSERT OR IGNORE INTO match_ticks (match_id, seq, t_seconds, "
            "player_name, player_team, boost, speed, b_boosting, b_on_ground, "
            "ball_speed, ball_team) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(match_id, *r) for r in rows],
        )
        self._conn.commit()

    def upsert_player_stats(self, match_id: int, rows: Iterable[dict]) -> None:
        cur = self._conn.cursor()
        cols = [
            "player_name", "player_team",
            "boost_avg", "time_zero_boost_pct", "time_full_boost_pct",
            "boost_starved_pct", "boost_used",
            "avg_speed", "supersonic_pct", "slow_pct", "aerial_pct",
            "possession_pct_team", "touch_share", "time_in_off_third_pct",
            "touches", "avg_touch_pace_added", "big_hits",
            "fifty_attempts", "fifty_wins", "avg_touch_y",
            "shots", "goals", "assists", "avg_shot_speed", "avg_goal_speed",
            "xg_lite", "crossbars",
            "saves", "epic_saves", "avg_save_speed", "shots_faced",
            "demos_dealt", "demos_taken", "demo_assists",
        ]
        placeholders = ",".join(["?"] * (len(cols) + 1))  # + match_id
        sql = (
            f"INSERT OR REPLACE INTO player_match_stats (match_id, {', '.join(cols)}) "
            f"VALUES ({placeholders})"
        )
        cur.executemany(
            sql,
            [tuple([match_id] + [r.get(c) for c in cols]) for r in rows],
        )
        self._conn.commit()

    def player_stats_for_match(self, match_guid: str) -> list[dict]:
        cur = self._conn.cursor()
        cur.execute("SELECT id FROM matches WHERE match_guid = ?", (match_guid,))
        row = cur.fetchone()
        if row is None:
            return []
        match_id = row[0]
        cur.execute("SELECT * FROM player_match_stats WHERE match_id = ?", (match_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def connection(self) -> sqlite3.Connection:
        return self._conn

    def teammate_breakdown(
        self,
        my_platform_id: str | None,
        playlist: str | None = None,
        gap_minutes: int = 60,
    ) -> list[TeammateAggregate]:
        """Group matches by (session, exact teammate set). Sessions are derived
        from gaps between consecutive ``ended_at`` values exceeding
        ``gap_minutes``. Requires MY_PLATFORM_ID to identify self; without it,
        returns []."""
        if not my_platform_id:
            return []

        sql = (
            "SELECT m.id, m.playlist, m.won, m.ended_at FROM matches m "
            "JOIN match_players mp ON mp.match_id = m.id AND mp.platform_id = ? "
        )
        params: list[object] = [my_platform_id]
        if playlist:
            sql += "WHERE m.playlist = ? "
            params.append(playlist)
        sql += "ORDER BY m.ended_at ASC"

        cur = self._conn.cursor()
        cur.execute(sql, params)
        my_matches = cur.fetchall()
        if not my_matches:
            return []

        gap = timedelta(minutes=gap_minutes)
        session_idx = -1
        prev_end: datetime | None = None
        session_bounds: dict[int, list[datetime]] = {}  # idx -> [start, end]
        match_session: dict[int, int] = {}
        for match_id, _pl, _won, ended_at_str in my_matches:
            ended_at = datetime.fromisoformat(ended_at_str)
            if prev_end is None or (ended_at - prev_end) > gap:
                session_idx += 1
                session_bounds[session_idx] = [ended_at, ended_at]
            else:
                session_bounds[session_idx][1] = ended_at
            match_session[match_id] = session_idx
            prev_end = ended_at

        # For each match, fetch teammates (same team, excluding self).
        groups: dict[tuple[int, tuple[str, ...], str], dict] = {}
        for match_id, m_playlist, won, _ended_at in my_matches:
            cur.execute(
                "SELECT mp2.platform_id, mp2.name FROM match_players mp1 "
                "JOIN match_players mp2 ON mp1.match_id = mp2.match_id AND mp1.team = mp2.team "
                "WHERE mp1.match_id = ? AND mp1.platform_id = ? AND mp2.platform_id != ?",
                (match_id, my_platform_id, my_platform_id),
            )
            tm = sorted(cur.fetchall(), key=lambda r: r[0])
            tm_ids = tuple(r[0] for r in tm)
            tm_names = tuple(r[1] for r in tm)
            sidx = match_session[match_id]
            key = (sidx, tm_ids, m_playlist)
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
        for (sidx, tm_ids, pl), agg in groups.items():
            start, end = session_bounds[sidx]
            out.append(
                TeammateAggregate(
                    teammates=tm_ids,
                    teammate_names=agg["names"],
                    playlist=pl,
                    wins=agg["wins"],
                    losses=agg["losses"],
                    unknown=agg["unknown"],
                    session_started_at=start,
                    session_ended_at=end,
                )
            )
        # Newest session first; within a session, biggest groups first.
        out.sort(
            key=lambda a: (
                -(a.session_started_at.timestamp() if a.session_started_at else 0),
                -a.total,
                a.playlist,
                a.teammate_names,
            )
        )
        return out

    def all_matches(self) -> list[dict]:
        return self._fetch_matches(limit=None)

    def recent(self, limit: int = 50) -> list[dict]:
        return self._fetch_matches(limit=limit)

    def _fetch_matches(self, limit: int | None) -> list[dict]:
        cur = self._conn.cursor()
        sql = (
            "SELECT id, match_guid, playlist, started_at, ended_at, won, my_team, winner_team, "
            "team0_score, team1_score, overtime, playlist_id "
            "FROM matches ORDER BY ended_at DESC"
        )
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        cur.execute(sql, params)
        rows = cur.fetchall()
        out: list[dict] = []
        for (
            mid,
            guid,
            playlist,
            started_at,
            ended_at,
            won,
            my_team,
            winner_team,
            team0_score,
            team1_score,
            overtime,
            playlist_id,
        ) in rows:
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
                    "playlist_id": None if playlist_id is None else int(playlist_id),
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "won": None if won is None else bool(won),
                    "my_team": my_team,
                    "winner_team": winner_team,
                    "team0_score": int(team0_score),
                    "team1_score": int(team1_score),
                    "overtime": bool(overtime),
                    "players": players,
                }
            )
        return out
