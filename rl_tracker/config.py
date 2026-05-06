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


def settings_file() -> Path:
    return appdata_dir() / "settings.json"


def history_db_path() -> Path:
    return appdata_dir() / "history.db"


def load_my_platform_id() -> str | None:
    import json

    try:
        data = json.loads(settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    val = data.get("my_platform_id")
    return val if isinstance(val, str) and val else None


def save_my_platform_id(platform_id: str) -> None:
    import json

    settings_file().parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data["my_platform_id"] = platform_id
    settings_file().write_text(json.dumps(data, indent=2), encoding="utf-8")
