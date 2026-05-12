from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .log_watcher import default_log_path


@dataclass(frozen=True)
class PlayerIdentity:
    platform: str  # "steam" | "epic"
    id_or_name: str


_STEAM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"OnlineSubsystemSteam.*?(\d{17})"),
    re.compile(r"\bSteam\|(\d{17})\b"),
    re.compile(r"PrimaryId[^0-9]{0,8}(\d{17})"),
    re.compile(r"\bSteamID(?:64)?[=:\s]+(\d{17})"),
)

_EPIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"OnlineSubsystemEpic.*?DisplayName[=:\s\"]+([^\"\r\n,]+)"),
    re.compile(r"\bEpic\|([^|\s\"]+)"),
    re.compile(r"\bEpicAccount(?:Id)?[=:\s\"]+([0-9a-fA-F]{32})"),
)


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    start = max(0, size - max_bytes)
    try:
        with path.open("rb") as fp:
            if start:
                fp.seek(start)
                fp.readline()
            data = fp.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def detect_player(
    log_path: Path | None = None,
    tail_bytes: int = 2_000_000,
) -> PlayerIdentity | None:
    """Scan the tail of Rocket League's Launch.log for the current player.

    Returns ``None`` when no identity can be recovered. The most recent
    matching line wins (we scan tail → head).
    """

    path = log_path or default_log_path()
    if not path.exists():
        return None
    lines = _tail_lines(path, tail_bytes)
    for line in reversed(lines):
        for pat in _STEAM_PATTERNS:
            m = pat.search(line)
            if m:
                return PlayerIdentity("steam", m.group(1))
        for pat in _EPIC_PATTERNS:
            m = pat.search(line)
            if m:
                name = m.group(1).strip()
                if name:
                    return PlayerIdentity("epic", name)
    return None


def is_launch_log_current(
    log_path: Path | None = None,
    max_age_seconds: float = 1800.0,
) -> bool:
    """Return True if Launch.log was modified within the last ``max_age_seconds``."""

    path = log_path or default_log_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) <= max_age_seconds
