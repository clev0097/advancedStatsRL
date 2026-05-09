"""Diagnose boost_used asymmetry between teams.

For each match in history.db, report per-player:
  - total ticks
  - ticks where b_boosting=1 (% of ticks)
  - count of negative boost deltas (any)
  - count of negative deltas where b_boosting=1 at later tick (what we currently count)
  - sum of negative deltas (any) -- "ungated boost_used"
  - sum of negative deltas where b_boosting=1 (current boost_used)

Usage:
    python scripts/diagnose_boost.py            # latest match
    python scripts/diagnose_boost.py --all      # all matches
    python scripts/diagnose_boost.py --match-id 12
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def db_path() -> Path:
    return Path(os.path.expandvars(r"%APPDATA%")) / "rl_tracker" / "history.db"


def list_matches(conn: sqlite3.Connection) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, started_at, my_team, winner_team FROM matches ORDER BY id"
    )
    return cur.fetchall()


def diagnose_match(conn: sqlite3.Connection, match_id: int) -> None:
    cur = conn.cursor()
    cur.execute(
        "SELECT started_at, my_team, winner_team FROM matches WHERE id = ?",
        (match_id,),
    )
    meta = cur.fetchone()
    if not meta:
        print(f"match {match_id}: not found")
        return
    started, my_team, winner = meta
    print(f"\n=== match {match_id}  started={started}  my_team={my_team} winner={winner} ===")

    cur.execute(
        "SELECT seq, player_name, player_team, boost, b_boosting "
        "FROM match_ticks WHERE match_id = ? ORDER BY player_name, seq",
        (match_id,),
    )
    rows = cur.fetchall()
    if not rows:
        print("  (no ticks)")
        return

    by_player: dict[str, list] = {}
    for r in rows:
        by_player.setdefault(r[1], []).append(r)

    print(f"  {'player':<28} {'team':>4} {'ticks':>6} "
          f"{'bb%':>6} {'drops':>6} {'gateD':>6} "
          f"{'sumAll':>8} {'sumGated':>8}")
    print("  " + "-" * 80)

    rows_out = []
    for name, ticks in by_player.items():
        team = ticks[0][2]
        n = len(ticks)
        bb_count = sum(1 for r in ticks if r[4])
        drops_any = 0
        drops_gated = 0
        sum_any = 0.0
        sum_gated = 0.0
        prev = None
        for r in ticks:
            b = r[3]
            bb = r[4]
            if b is not None and prev is not None and b < prev:
                d = prev - b
                drops_any += 1
                sum_any += d
                if bb:
                    drops_gated += 1
                    sum_gated += d
            prev = b
        rows_out.append((team, name, n, bb_count, drops_any, drops_gated,
                         sum_any, sum_gated))

    rows_out.sort(key=lambda x: (x[0], x[1]))
    for team, name, n, bb_count, drops_any, drops_gated, sum_any, sum_gated in rows_out:
        bb_pct = (bb_count / n * 100) if n else 0
        print(f"  {name[:28]:<28} {team:>4} {n:>6} "
              f"{bb_pct:>5.1f}% {drops_any:>6} {drops_gated:>6} "
              f"{sum_any:>8.1f} {sum_gated:>8.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--match-id", type=int)
    args = ap.parse_args()

    p = db_path()
    if not p.exists():
        print(f"history.db not found at {p}")
        return
    conn = sqlite3.connect(p)

    matches = list_matches(conn)
    if not matches:
        print("no matches in db")
        return

    if args.match_id is not None:
        diagnose_match(conn, args.match_id)
    elif args.all:
        for m in matches:
            diagnose_match(conn, m[0])
    else:
        diagnose_match(conn, matches[-1][0])

    print("\nLegend:")
    print("  bb%      = % of ticks where b_boosting flag was true")
    print("  drops    = # adjacent-tick intervals where boost decreased")
    print("  gateD    = subset of drops where b_boosting=1 at later tick (what we count)")
    print("  sumAll   = sum of all negative deltas  (gate-free 'boost_used')")
    print("  sumGated = sum of gated negative deltas (current 'boost_used')")


if __name__ == "__main__":
    main()
