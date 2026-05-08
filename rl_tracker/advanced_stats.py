"""Compute per-player advanced stats from raw match_events / match_ticks rows
and write them into player_match_stats. Run once per match at MatchEnded.

All metrics are best-effort: if the underlying raw rows are missing or empty,
the corresponding fields are set to None / 0 rather than raising.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .history import HistoryStore, MatchRecord


# Empirical thresholds tuned to the values seen in events.log. The Stats API
# reports speeds in a normalised scale where ~22 looks like supersonic and
# touch PostHitSpeed of ~80+ tracks community "big hit" intuition.
SUPERSONIC = 22.0
SLOW = 5.0
BIG_HIT_POST = 80.0
GOAL_LINE_Y = 5120.0  # absolute Y of goal mouth in field-axis units
OFF_THIRD_FRAC = 1.0 / 3.0


@dataclass
class _Touch:
    seq: int
    t: float
    name: str
    team: int | None
    pre: float | None
    post: float | None
    by: float | None
    kind: str  # 'ball_hit' or 'fifty'


def compute_match_stats(
    store: HistoryStore, match_id: int, record: MatchRecord
) -> None:
    conn = store.connection()
    cur = conn.cursor()

    # ---- roster ----
    roster = [(p.name, p.team) for p in record.players]
    if not roster:
        return
    name_to_team = {n: t for n, t in roster}
    team_to_names = {0: [n for n, t in roster if t == 0], 1: [n for n, t in roster if t == 1]}

    # ---- ticks ----
    cur.execute(
        "SELECT seq, player_name, player_team, boost, speed, b_boosting, "
        "b_on_ground, ball_speed, ball_team FROM match_ticks WHERE match_id = ? "
        "ORDER BY seq",
        (match_id,),
    )
    ticks = cur.fetchall()

    # Group ticks by player.
    per_player_ticks: dict[str, list[tuple]] = {n: [] for n, _ in roster}
    seqs_seen: set[int] = set()
    seq_ball_team: dict[int, int | None] = {}
    for row in ticks:
        seq, pname, _pteam, boost, speed, bboost, bground, bspeed, bteam = row
        seqs_seen.add(seq)
        seq_ball_team[seq] = bteam
        per_player_ticks.setdefault(pname, []).append(row)

    total_ticks = len(seqs_seen)

    # Per-team possession (counted once per seq, not per player).
    team0_poss = sum(1 for bt in seq_ball_team.values() if bt == 0)
    team1_poss = sum(1 for bt in seq_ball_team.values() if bt == 1)
    team_poss_pct = {
        0: (team0_poss / total_ticks) if total_ticks else None,
        1: (team1_poss / total_ticks) if total_ticks else None,
    }

    # ---- events ----
    cur.execute(
        "SELECT seq, t_seconds, kind, player_name, player_team, secondary_name, "
        "secondary_team, ball_x, ball_y, ball_z, pre_speed, post_speed, "
        "goal_speed, goal_time, impact_force, extra "
        "FROM match_events WHERE match_id = ? ORDER BY seq",
        (match_id,),
    )
    events = cur.fetchall()

    # Build chronological touch list (ball_hit + fifty). Used for fifty win rate.
    touches: list[_Touch] = []
    for ev in events:
        kind = ev[2]
        if kind in ("ball_hit", "fifty"):
            touches.append(_Touch(
                seq=ev[0], t=ev[1], name=ev[3] or "",
                team=ev[4], pre=ev[10], post=ev[11], by=ev[8],
                kind=kind,
            ))

    # Index touches by player.
    touches_by_player: dict[str, list[_Touch]] = {n: [] for n, _ in roster}
    for tch in touches:
        touches_by_player.setdefault(tch.name, []).append(tch)

    # Team totals for touch share.
    team_touch_count = {0: 0, 1: 0}
    for tch in touches:
        if tch.team in (0, 1):
            team_touch_count[tch.team] += 1

    # 50/50 win attribution: a fifty is "won" by the team of whichever player
    # makes the next non-fifty contact.
    fifty_groups: dict[int, list[_Touch]] = {}
    for tch in touches:
        if tch.kind == "fifty":
            fifty_groups.setdefault(tch.seq, []).append(tch)
    fifty_wins_by_player: dict[str, int] = {n: 0 for n, _ in roster}
    fifty_attempts_by_player: dict[str, int] = {n: 0 for n, _ in roster}
    for seq, contestants in fifty_groups.items():
        for c in contestants:
            fifty_attempts_by_player[c.name] = fifty_attempts_by_player.get(c.name, 0) + 1
        # Find the next ball_hit (single-touch) after this seq.
        winner_team = None
        for tch in touches:
            if tch.seq <= seq:
                continue
            if tch.kind == "ball_hit":
                winner_team = tch.team
                break
        if winner_team is None:
            continue
        for c in contestants:
            if c.team == winner_team:
                fifty_wins_by_player[c.name] = fifty_wins_by_player.get(c.name, 0) + 1

    # Statfeed-driven counters.
    shots_by: dict[str, int] = {}
    sf_goals_by: dict[str, int] = {}
    sf_assists_by: dict[str, int] = {}
    saves_by: dict[str, int] = {}
    epic_saves_by: dict[str, int] = {}
    demos_dealt: dict[str, int] = {}
    demos_taken: dict[str, int] = {}
    # For demo assists: list of (t, demoer_name, victim_team)
    demo_log: list[tuple[float, str, int | None]] = []
    # Goals chronologically: (t, scorer_team, scorer_name, assister_name)
    goal_log: list[tuple[float, int | None, str | None, str | None, float | None, float | None]] = []
    crossbars_by: dict[str, int] = {}

    for ev in events:
        seq, t, kind, pname, pteam, sname, steam, bx, by, bz, pre, post, gs, gt, ifc, extra = ev
        if kind == "shot":
            if pname:
                shots_by[pname] = shots_by.get(pname, 0) + 1
        elif kind == "sf_goal":
            if pname:
                sf_goals_by[pname] = sf_goals_by.get(pname, 0) + 1
        elif kind == "sf_assist":
            if pname:
                sf_assists_by[pname] = sf_assists_by.get(pname, 0) + 1
        elif kind == "save":
            if pname:
                saves_by[pname] = saves_by.get(pname, 0) + 1
        elif kind == "epic_save":
            if pname:
                epic_saves_by[pname] = epic_saves_by.get(pname, 0) + 1
        elif kind == "demo":
            # MainTarget = demoer, SecondaryTarget = victim (matches log shape).
            if pname:
                demos_dealt[pname] = demos_dealt.get(pname, 0) + 1
            if sname:
                demos_taken[sname] = demos_taken.get(sname, 0) + 1
            victim_team = name_to_team.get(sname) if sname else None
            demo_log.append((t, pname or "", victim_team))
        elif kind == "goal":
            # Parse extra json for assister + last touch.
            assister = None
            if extra:
                import json
                try:
                    e = json.loads(extra)
                    assister = e.get("assister")
                except Exception:
                    pass
            goal_log.append((t, pteam, pname, assister, gs, gt))
        elif kind == "crossbar":
            if pname:
                crossbars_by[pname] = crossbars_by.get(pname, 0) + 1

    # Compute average shot speed and goal speed per scorer from goal_log.
    scorer_goal_speeds: dict[str, list[float]] = {}
    for _t, _team, scorer, _ass, gs, _gt in goal_log:
        if scorer and gs is not None:
            scorer_goal_speeds.setdefault(scorer, []).append(float(gs))

    # Demo assists: a demo by player X within 3s before a teammate goal counts.
    demo_assists_by: dict[str, int] = {}
    for gt, gteam, scorer, _a, _gs, _gtm in goal_log:
        if gteam is None:
            continue
        for dt, demoer, victim_team in demo_log:
            if demoer and 0 <= (gt - dt) <= 3.0 and name_to_team.get(demoer) == gteam:
                # Demo on the *opposing* team that helped this goal.
                if victim_team is not None and victim_team != gteam:
                    demo_assists_by[demoer] = demo_assists_by.get(demoer, 0) + 1

    # Average save speed: nearest prior ball_hit/fifty by enemy team before each save.
    save_speed_samples: dict[str, list[float]] = {}
    save_events_chrono = [ev for ev in events if ev[2] in ("save", "epic_save")]
    for ev in save_events_chrono:
        seq, _t, _k, pname, pteam, *_rest = ev
        if not pname or pteam is None:
            continue
        # Find nearest preceding touch by enemy team.
        for tch in reversed(touches):
            if tch.seq >= seq:
                continue
            if tch.team is not None and tch.team != pteam and tch.post is not None:
                save_speed_samples.setdefault(pname, []).append(float(tch.post))
                break

    # Shots faced per goalie-side: count enemy 'shot' statfeeds.
    shots_faced_by_team = {0: 0, 1: 0}
    for ev in events:
        if ev[2] == "shot":
            t = ev[4]
            if t in (0, 1):
                # Shot by team t means shot faced by team 1-t.
                shots_faced_by_team[1 - t] += 1

    # Goals attribution into player records (use roster Score from updatestate is
    # not stored per-player here; use sf_goals + sf_assists as the source of
    # truth for per-player goals/assists at match end).
    # ---- assemble per-player rows ----
    out_rows: list[dict] = []
    for name, team in roster:
        pticks = per_player_ticks.get(name, [])
        n = len(pticks)
        if n:
            boosts = [r[3] for r in pticks if r[3] is not None]
            speeds = [r[4] for r in pticks if r[4] is not None]
            on_ground = [r[6] for r in pticks if r[6] is not None]
            ball_teams_when_present = [r[8] for r in pticks if r[8] is not None]
            boost_avg = sum(boosts) / len(boosts) if boosts else None
            time_zero_boost_pct = (
                sum(1 for b in boosts if b <= 0.5) / len(boosts) if boosts else None
            )
            time_full_boost_pct = (
                sum(1 for b in boosts if b >= 99.5) / len(boosts) if boosts else None
            )
            boost_starved_pct = (
                sum(1 for b in boosts if b <= 12.0) / len(boosts) if boosts else None
            )
            # Boost used: sum of negative deltas while bBoosting.
            boost_used = 0.0
            prev = None
            for r in pticks:
                b = r[3]
                bb = r[5]
                if b is not None and prev is not None and bb:
                    if b < prev:
                        boost_used += prev - b
                prev = b
            avg_speed = sum(speeds) / len(speeds) if speeds else None
            supersonic_pct = (
                sum(1 for s in speeds if s >= SUPERSONIC) / len(speeds) if speeds else None
            )
            slow_pct = (
                sum(1 for s in speeds if s < SLOW) / len(speeds) if speeds else None
            )
            aerial_pct = (
                sum(1 for g in on_ground if not g) / len(on_ground) if on_ground else None
            )
        else:
            boost_avg = time_zero_boost_pct = time_full_boost_pct = None
            boost_starved_pct = boost_used = None
            avg_speed = supersonic_pct = slow_pct = aerial_pct = None

        # Possession is a team-level stat surfaced on each player.
        possession_pct_team = team_poss_pct.get(team)

        # Touches.
        ptouches = touches_by_player.get(name, [])
        touches_n = len(ptouches)
        team_total = team_touch_count.get(team, 0)
        touch_share = (touches_n / team_total) if team_total else None
        pace_added = [
            (tch.post - tch.pre)
            for tch in ptouches
            if tch.post is not None and tch.pre is not None
        ]
        avg_touch_pace_added = (
            sum(pace_added) / len(pace_added) if pace_added else None
        )
        big_hits = sum(1 for tch in ptouches if tch.post is not None and tch.post >= BIG_HIT_POST)
        ys = [tch.by for tch in ptouches if tch.by is not None]
        avg_touch_y = sum(ys) / len(ys) if ys else None
        # "Offensive third" depends on which goal the player is attacking.
        # team 0 attacks +Y goal (Y=+5120), team 1 attacks -Y goal (Y=-5120).
        if ys:
            attacking_sign = 1 if team == 0 else -1
            in_off = sum(1 for y in ys if attacking_sign * y > GOAL_LINE_Y * OFF_THIRD_FRAC)
            time_in_off_third_pct = in_off / len(ys)
        else:
            time_in_off_third_pct = None

        # Shooting / scoring (from statfeed counters).
        shots = shots_by.get(name, 0)
        goals = sf_goals_by.get(name, 0)
        assists = sf_assists_by.get(name, 0)
        avg_goal_speed = (
            sum(scorer_goal_speeds.get(name, [])) / len(scorer_goal_speeds[name])
            if scorer_goal_speeds.get(name) else None
        )
        # avg_shot_speed approximation: average post-hit speed of this player's
        # touches that registered as shots (fall back to all touches if shots=0).
        candidate_post = [tch.post for tch in ptouches if tch.post is not None]
        avg_shot_speed = (
            (sum(candidate_post) / len(candidate_post)) if candidate_post else None
        )
        # xG-lite: for each goal scored by player, score = clamp(speed/maxSpeed, 0..1) *
        # clamp(1 - dist/maxDist, 0..1). Aggregate to a per-match expected goals number.
        # (Distance from opposite-corner of attacking goal scaled by 5120 in Y.)
        xg = 0.0
        for ev in events:
            if ev[2] != "goal":
                continue
            scorer_name = ev[3]
            if scorer_name != name:
                continue
            gs = ev[12] or 0.0
            ix = ev[7] or 0.0
            iy = ev[8] or 0.0
            iz = ev[9] or 0.0
            attacking_sign = 1 if team == 0 else -1
            # Last touch X/Y not stored on goal event directly; use impact location
            # to penalise tight-angle goals (large |X|) and reward central impacts.
            angle_factor = max(0.0, 1.0 - abs(ix) / 1200.0)  # 1200 ~ goal half-width
            speed_factor = min(1.0, gs / 120.0)  # 120 ~ very fast goal
            xg += 0.4 + 0.4 * speed_factor + 0.2 * angle_factor
        xg_lite = xg if (goals or xg) else None

        out_rows.append({
            "player_name": name,
            "player_team": team,
            "boost_avg": boost_avg,
            "time_zero_boost_pct": time_zero_boost_pct,
            "time_full_boost_pct": time_full_boost_pct,
            "boost_starved_pct": boost_starved_pct,
            "boost_used": boost_used,
            "avg_speed": avg_speed,
            "supersonic_pct": supersonic_pct,
            "slow_pct": slow_pct,
            "aerial_pct": aerial_pct,
            "possession_pct_team": possession_pct_team,
            "touch_share": touch_share,
            "time_in_off_third_pct": time_in_off_third_pct,
            "touches": touches_n,
            "avg_touch_pace_added": avg_touch_pace_added,
            "big_hits": big_hits,
            "fifty_attempts": fifty_attempts_by_player.get(name, 0),
            "fifty_wins": fifty_wins_by_player.get(name, 0),
            "avg_touch_y": avg_touch_y,
            "shots": shots,
            "goals": goals,
            "assists": assists,
            "avg_shot_speed": avg_shot_speed,
            "avg_goal_speed": avg_goal_speed,
            "xg_lite": xg_lite,
            "crossbars": crossbars_by.get(name, 0),
            "saves": saves_by.get(name, 0),
            "epic_saves": epic_saves_by.get(name, 0),
            "avg_save_speed": (
                sum(save_speed_samples[name]) / len(save_speed_samples[name])
                if save_speed_samples.get(name) else None
            ),
            "shots_faced": shots_faced_by_team.get(team, 0),
            "demos_dealt": demos_dealt.get(name, 0),
            "demos_taken": demos_taken.get(name, 0),
            "demo_assists": demo_assists_by.get(name, 0),
        })

    store.upsert_player_stats(match_id, out_rows)
