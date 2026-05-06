from __future__ import annotations

import configparser
from pathlib import Path

from .config import PACKET_SEND_RATE, STATS_API_PORT, stats_api_ini_path

SECTION = "TAGame.MatchStatsExporter_TA"


def ensure_stats_api_enabled(path: Path | None = None) -> bool:
    """Write/verify TAStatsAPI.ini. Returns True if a change was made (caller should
    tell the user to restart Rocket League)."""
    path = path or stats_api_ini_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    parser = configparser.ConfigParser()
    parser.optionxform = str  # preserve case
    if path.exists():
        parser.read(path, encoding="utf-8")

    desired = {"Port": str(STATS_API_PORT), "PacketSendRate": str(PACKET_SEND_RATE)}
    changed = False
    if not parser.has_section(SECTION):
        parser.add_section(SECTION)
        changed = True
    for k, v in desired.items():
        if parser.get(SECTION, k, fallback=None) != v:
            parser.set(SECTION, k, v)
            changed = True

    if changed:
        with path.open("w", encoding="utf-8") as f:
            parser.write(f)
    return changed
