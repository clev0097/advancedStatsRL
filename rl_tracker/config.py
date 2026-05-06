from __future__ import annotations

import os
from pathlib import Path

STATS_API_HOST = "127.0.0.1"
STATS_API_PORT = 49123
PACKET_SEND_RATE = 30

RECONNECT_BACKOFF_START_S = 1.0
RECONNECT_BACKOFF_MAX_S = 5.0


def user_documents() -> Path:
    return Path(os.path.expandvars(r"%USERPROFILE%\Documents"))


def stats_api_ini_path() -> Path:
    return user_documents() / "My Games" / "Rocket League" / "TAGame" / "Config" / "TAStatsAPI.ini"


def appdata_dir() -> Path:
    base = Path(os.path.expandvars(r"%APPDATA%"))
    return base / "rl_tracker"


def state_file() -> Path:
    return appdata_dir() / "state.json"
