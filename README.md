# RL Tracker

Rocket League session tracker. Counts wins/losses per playlist for the current play session, shows a small movable overlay on top of the game, persists per-match history, and computes pro-sports-style **advanced stats** for every match (boost economy, possession, xG-lite, demo combat, etc.). EAC-safe: uses the sanctioned local Stats API that ships with Rocket League — no injection.

## Requirements

- Windows
- Rocket League running in **borderless** fullscreen (the default). Exclusive fullscreen will hide the overlay.

## For players (no Python needed)

1. Download `RLTracker.exe` from the [latest release](../../releases/latest).
2. Double-click it to run. Windows SmartScreen may warn since the exe is unsigned — click **More info → Run anyway**.
3. If the overlay doesn't appear in-game, **restart Rocket League once** so it picks up the config file the tracker writes (see "First run" below).

## For developers

Requires Python 3.11+.

### Install

```
pip install -e .[dev]
```

### First run

```
python -m rl_tracker
```

On first launch the app writes/verifies:

```
%USERPROFILE%\Documents\My Games\Rocket League\TAGame\Config\TAStatsAPI.ini
```

If that file was missing or wrong, **restart Rocket League once** so the game picks up the new config and starts broadcasting on `127.0.0.1:49123`.

## Schema-discovery / debug mode

If W/L counters look wrong, dump raw events:

```
python -m rl_tracker --dump-events events.log
```

Play one match, close the app, and inspect `events.log` to see exactly what fields the Stats API emits in your version of the game.

### Building the distributable exe

```
./build_exe.ps1
```

Produces `dist/RLTracker.exe` (single-file, ~40–60 MB). Upload that to a GitHub Release for friends to download.

## Controls

- Drag the overlay with the left mouse button to move it.
- Right-click for the menu: Reset session / Toggle click-through / Quit.

## Advanced stats

Every match the tracker sees is broken down into per-player advanced stats and stored alongside the result. Open the **History → Advanced** tab and **double-click any match** to open the per-match drill-down.

Metrics computed per player per match:

| Group | Metrics |
|---|---|
| Boost economy | avg boost, % time at 0, % time at 100, % time starved (≤12), total boost used |
| Movement | avg speed, supersonic %, slow %, aerial % |
| Possession | team possession %, touch share, time in offensive third |
| Touch quality | touches, avg pace added per touch, big hits, 50/50 wins/attempts, avg touch Y |
| Shooting | shots, goals, assists, avg shot speed, avg goal speed, xG-lite, crossbars |
| Defense | saves, epic saves, avg save speed, shots faced |
| Combat | demos dealt, demos taken, demo assists |

How it works:
- During a match, every `BallHit`, `GoalScored`, `CrossbarHit`, and `StatfeedEvent` is buffered in memory along with a ~2 Hz decimated sample of `UpdateState` (boost, speed, on-ground, ball speed/team).
- On `MatchEnded`, the raw events flush to SQLite (`match_events`, `match_ticks`) and `advanced_stats.compute_match_stats()` derives the per-player numbers into `player_match_stats`. UI reads only the derived rows.

Data limitations worth knowing:
- The Stats API does not provide per-player X/Y/Z, so heatmaps are touch-based only — true rotation / "last man back" analysis isn't possible from this feed.
- Roster members who never appear in a sampled tick get `None` for tick-derived stats.
- xG-lite is a rough heuristic; it'll calibrate as you accumulate matches.

## MMR tracking

The overlay shows your per-session MMR change while you play, polled from
[tracker.gg](https://tracker.gg). It identifies you automatically by reading
your Steam ID / Epic display name out of Rocket League's `Launch.log`, then
fetches the public ranked stats endpoint every ~30 s.

- Total session delta and per-playlist breakdown render under the W/L lines.
- Snapshots are persisted to `history.db` (`mmr_snapshots` table) so the
  baseline survives a brief app restart inside the same session.
- Right-click the overlay → **Reset MMR baseline** to start the delta from
  the current rating.
- Disable polling entirely with `--no-mmr`, or override the detected identity
  with `--mmr-platform steam|epic --mmr-id <id>`.

If tracker.gg starts blocking the request you'll see a `(tracker_blocked)`
status line; the previous delta is preserved across transient failures so
the UI doesn't flicker back to 0.

## Out of scope (v1)

OBS overlay mode. Planned for later.
