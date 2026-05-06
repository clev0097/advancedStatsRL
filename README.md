# RL Tracker

Barebones Rocket League session tracker. Counts wins/losses per playlist for the current play session and shows a small movable overlay on top of the game. EAC-safe: uses the sanctioned local Stats API that ships with Rocket League — no injection.

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

## Out of scope (v1)

MMR tracking, per-match history, OBS overlay mode. Planned for later.
