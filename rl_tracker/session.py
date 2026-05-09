from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from .history import HistoryStore, MatchRecord, PlayerRecord


class _PlaylistSnapshotProvider(Protocol):
    def snapshot(self) -> Any: ...


# Sample 1 in N UpdateState frames into match_ticks. Stats API runs ~30 Hz, so
# N=15 -> ~2 Hz, plenty for boost / speed / possession aggregates.
TICK_DECIMATION = 15


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
    # Advanced-stats buffers
    update_count: int = 0
    next_seq: int = 0
    next_event_seq: int = 0
    last_t_seconds: float = 0.0
    events: list[tuple] = field(default_factory=list)
    ticks: list[tuple] = field(default_factory=list)
    name_to_team: dict[str, int] = field(default_factory=dict)


@dataclass
class SessionState:
    started_at: datetime = field(default_factory=_now)
    by_playlist: dict[str, PlaylistTally] = field(default_factory=dict)
    my_platform_id: str | None = None
    history: HistoryStore | None = None
    on_match_recorded: Callable[[MatchRecord], None] | None = None
    streak_count: int = 0
    streak_kind: str | None = None  # "W", "L", or None
    playlist_watcher: _PlaylistSnapshotProvider | None = None

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
        elif name == "ballhit":
            self._on_ball_hit(data)
        elif name == "goalscored":
            self._on_goal_scored(data)
        elif name == "crossbarhit":
            self._on_crossbar(data)
        elif name == "statfeedevent":
            self._on_statfeed(data)
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
        for p in players:
            snap.name_to_team[p["Name"]] = p["TeamNum"]
        game = self._get(data, "Game", "game")
        t_seconds = 0.0
        ball_speed: float | None = None
        ball_team: int | None = None
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
            try:
                t_seconds = float(self._get(game, "TimeSeconds", "time_seconds", default=0) or 0)
            except (TypeError, ValueError):
                t_seconds = 0.0
            ball = self._get(game, "Ball", "ball")
            if isinstance(ball, dict):
                try:
                    ball_speed = float(self._get(ball, "Speed", "speed", default=0) or 0)
                except (TypeError, ValueError):
                    ball_speed = None
                bt = self._get(ball, "TeamNum", "team_num")
                try:
                    ball_team = int(bt) if bt is not None else None
                except (TypeError, ValueError):
                    ball_team = None
        snap.last_t_seconds = t_seconds

        # Decimated tick samples for advanced stats.
        snap.update_count += 1
        if snap.update_count % TICK_DECIMATION == 0:
            seq = snap.next_seq
            snap.next_seq += 1
            for p_raw in players_raw:
                if not isinstance(p_raw, dict):
                    continue
                name = self._get(p_raw, "Name", "name")
                team = self._get(p_raw, "TeamNum", "team_num")
                if name is None or team is None:
                    continue
                boost = self._get(p_raw, "Boost", "boost")
                speed = self._get(p_raw, "Speed", "speed")
                b_boosting = self._get(p_raw, "bBoosting", "b_boosting", default=False)
                b_on_ground = self._get(p_raw, "bOnGround", "b_on_ground", default=False)
                snap.ticks.append((
                    seq, t_seconds, str(name), int(team),
                    None if boost is None else float(boost),
                    None if speed is None else float(speed),
                    1 if b_boosting else 0,
                    1 if b_on_ground else 0,
                    ball_speed,
                    ball_team,
                ))

    def _snap_for(self, data: dict[str, Any]) -> _RosterSnapshot | None:
        guid = self._get(data, "MatchGuid", "match_guid")
        if not guid:
            return None
        return self._rosters.get(guid)

    def _next_event_seq(self, snap: _RosterSnapshot) -> int:
        n = snap.next_event_seq
        snap.next_event_seq += 1
        return n

    def _player_team(self, snap: _RosterSnapshot, p: dict[str, Any]) -> int | None:
        t = self._get(p, "TeamNum", "team_num")
        if t is not None:
            try:
                return int(t)
            except (TypeError, ValueError):
                pass
        name = self._get(p, "Name", "name")
        if name is not None:
            return snap.name_to_team.get(str(name))
        return None

    def _on_ball_hit(self, data: dict[str, Any]) -> None:
        snap = self._snap_for(data)
        if snap is None:
            return
        ball = self._get(data, "Ball", "ball") or {}
        loc = ball.get("Location") or ball.get("location") or {}
        try:
            pre = float(ball.get("PreHitSpeed", ball.get("pre_hit_speed", 0)) or 0)
            post = float(ball.get("PostHitSpeed", ball.get("post_hit_speed", 0)) or 0)
        except (TypeError, ValueError):
            pre = post = 0.0
        bx = loc.get("X", loc.get("x"))
        by = loc.get("Y", loc.get("y"))
        bz = loc.get("Z", loc.get("z"))
        players = self._get(data, "Players", "players") or []
        if not isinstance(players, list) or not players:
            return
        # Single touch -> kind 'ball_hit'; two players -> '50_50' (both get a row).
        kind = "fifty" if len(players) >= 2 else "ball_hit"
        for p in players:
            if not isinstance(p, dict):
                continue
            name = self._get(p, "Name", "name")
            team = self._player_team(snap, p)
            seq = self._next_event_seq(snap)
            snap.events.append((
                seq, snap.last_t_seconds, kind,
                None if name is None else str(name),
                team,
                None, None,
                _f(bx), _f(by), _f(bz),
                pre, post,
                None, None, None, None,
            ))

    def _on_goal_scored(self, data: dict[str, Any]) -> None:
        snap = self._snap_for(data)
        if snap is None:
            return
        scorer = self._get(data, "Scorer", "scorer") or {}
        assister = self._get(data, "Assister", "assister") or {}
        last_touch = self._get(data, "BallLastTouch", "ball_last_touch") or {}
        last_touch_player = last_touch.get("Player") or last_touch.get("player") or {}
        try:
            last_speed = float(last_touch.get("Speed", last_touch.get("speed", 0)) or 0)
        except (TypeError, ValueError):
            last_speed = 0.0
        try:
            goal_speed = float(self._get(data, "GoalSpeed", "goal_speed", default=0) or 0)
            goal_time = float(self._get(data, "GoalTime", "goal_time", default=0) or 0)
        except (TypeError, ValueError):
            goal_speed = goal_time = 0.0
        impact = self._get(data, "ImpactLocation", "impact_location") or {}
        scorer_name = self._get(scorer, "Name", "name") if isinstance(scorer, dict) else None
        assister_name = (
            self._get(assister, "Name", "name") if isinstance(assister, dict) else None
        )
        last_touch_name = (
            self._get(last_touch_player, "Name", "name")
            if isinstance(last_touch_player, dict)
            else None
        )
        if not scorer_name:
            # Empty Scorer rows fire on round end resets — skip.
            if not last_touch_name:
                return
        scorer_team = self._player_team(snap, scorer) if isinstance(scorer, dict) else None
        seq = self._next_event_seq(snap)
        extra = json.dumps({
            "assister": assister_name,
            "last_touch": last_touch_name,
            "last_touch_speed": last_speed,
        })
        snap.events.append((
            seq, snap.last_t_seconds, "goal",
            None if not scorer_name else str(scorer_name),
            scorer_team,
            None if not assister_name else str(assister_name),
            self._player_team(snap, assister) if isinstance(assister, dict) else None,
            _f(impact.get("X", impact.get("x"))),
            _f(impact.get("Y", impact.get("y"))),
            _f(impact.get("Z", impact.get("z"))),
            None, None,
            goal_speed, goal_time, None,
            extra,
        ))

    def _on_crossbar(self, data: dict[str, Any]) -> None:
        snap = self._snap_for(data)
        if snap is None:
            return
        loc = self._get(data, "BallLocation", "ball_location") or {}
        try:
            ball_speed = float(self._get(data, "BallSpeed", "ball_speed", default=0) or 0)
            impact_force = float(self._get(data, "ImpactForce", "impact_force", default=0) or 0)
        except (TypeError, ValueError):
            ball_speed = impact_force = 0.0
        last_touch = self._get(data, "BallLastTouch", "ball_last_touch") or {}
        last_player = last_touch.get("Player") or last_touch.get("player") or {}
        last_name = self._get(last_player, "Name", "name") if isinstance(last_player, dict) else None
        last_team = self._player_team(snap, last_player) if isinstance(last_player, dict) else None
        seq = self._next_event_seq(snap)
        snap.events.append((
            seq, snap.last_t_seconds, "crossbar",
            None if not last_name else str(last_name),
            last_team,
            None, None,
            _f(loc.get("X", loc.get("x"))),
            _f(loc.get("Y", loc.get("y"))),
            _f(loc.get("Z", loc.get("z"))),
            None, ball_speed,
            None, None, impact_force,
            None,
        ))

    def _on_statfeed(self, data: dict[str, Any]) -> None:
        snap = self._snap_for(data)
        if snap is None:
            return
        ev_name = str(self._get(data, "EventName", "event_name", default="") or "")
        if not ev_name:
            return
        kind_map = {
            "Demolish": "demo",
            "Shot": "shot",
            "EpicSave": "epic_save",
            "Save": "save",
            "Goal": "sf_goal",
            "Assist": "sf_assist",
        }
        kind = kind_map.get(ev_name)
        if kind is None:
            return  # ignore Win, MVP, etc.
        main = self._get(data, "MainTarget", "main_target") or {}
        sec = self._get(data, "SecondaryTarget", "secondary_target") or {}
        main_name = self._get(main, "Name", "name") if isinstance(main, dict) else None
        sec_name = self._get(sec, "Name", "name") if isinstance(sec, dict) else None
        seq = self._next_event_seq(snap)
        snap.events.append((
            seq, snap.last_t_seconds, kind,
            None if not main_name else str(main_name),
            self._player_team(snap, main) if isinstance(main, dict) else None,
            None if not sec_name else str(sec_name),
            self._player_team(snap, sec) if isinstance(sec, dict) else None,
            None, None, None,
            None, None,
            None, None, None,
            None,
        ))

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
        playlist_id: int | None = None
        if self.playlist_watcher is not None:
            try:
                pl_snap = self.playlist_watcher.snapshot()
            except Exception:
                pl_snap = None
            pid = getattr(pl_snap, "playlist_id", None)
            seen_at = getattr(pl_snap, "seen_at", None)
            label = getattr(pl_snap, "playlist_label", None)
            if pid is not None and seen_at is not None:
                # Reject stale snapshots from a prior match.
                if seen_at >= snap.started_at - timedelta(seconds=60):
                    playlist_id = int(pid)
                    if label:
                        playlist = str(label)

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
            playlist_id=playlist_id,
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

        if self.history is not None:
            try:
                match_id = self.history.record_with_id(record)
            except Exception:
                match_id = None
            if match_id is not None:
                try:
                    if snap.events:
                        self.history.insert_events(match_id, snap.events)
                    if snap.ticks:
                        self.history.insert_ticks(match_id, snap.ticks)
                    from . import advanced_stats
                    advanced_stats.compute_match_stats(self.history, match_id, record)
                except Exception:
                    pass
        self._rosters.pop(guid, None)
        if self.on_match_recorded is not None:
            try:
                self.on_match_recorded(record)
            except Exception:
                pass
