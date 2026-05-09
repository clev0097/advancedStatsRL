from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


# Known Rocket League playlist IDs as they appear in Launch.log. Unknown IDs
# are surfaced as "playlist_<n>" so the value still round-trips through the DB.
PLAYLIST_ID_LABELS: dict[int, str] = {
    0: "Unranked",
    1: "Casual Duel",
    2: "Casual Doubles",
    3: "Casual Standard",
    4: "Casual Chaos",
    6: "Private",
    7: "Season",
    8: "Offline",
    10: "Ranked Duel",
    11: "Ranked Doubles",
    12: "Ranked Solo Standard",
    13: "Ranked Standard",
    15: "Mutator Mayhem",
    16: "Tournament (legacy)",
    17: "Dropshot",
    18: "Snow Day",
    19: "Rocket Labs",
    21: "Hoops",
    22: "Rumble",
    23: "Workshop",
    24: "Custom Tournament",
    27: "Hoops",
    28: "Rumble",
    29: "Dropshot",
    30: "Snow Day",
    34: "Tournament",
    37: "Heatseeker",
    38: "Boomer",
}


def label_for_playlist_id(pid: int) -> str:
    return PLAYLIST_ID_LABELS.get(pid, f"playlist_{pid}")


def default_log_path() -> Path:
    return (
        Path.home()
        / "Documents"
        / "My Games"
        / "Rocket League"
        / "TAGame"
        / "Logs"
        / "Launch.log"
    )


@dataclass(frozen=True)
class PlaylistSnapshot:
    playlist_id: int | None
    playlist_label: str | None
    seen_at: datetime | None


# Pre-compiled in priority order. The first regex to match a line wins.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Playlist[_ ]?ID[=:\s]+(\d+)", re.IGNORECASE),
    re.compile(r"PlaylistId[=:\s]+(\d+)", re.IGNORECASE),
    re.compile(r"\bPlaylist[=:\s]+(\d+)\b", re.IGNORECASE),
)


def parse_playlist_id(line: str) -> int | None:
    for pat in _PATTERNS:
        m = pat.search(line)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


class LogPlaylistWatcher:
    """Tails Rocket League's Launch.log to surface the most recent playlist ID.

    The Stats API doesn't expose the queue type (ranked vs casual, tournament,
    extra mode, ...). Rocket League itself logs ``Playlist=<id>`` style lines
    around match join. We tail the file in a background thread and expose the
    latest seen ID via :meth:`snapshot`. EAC-safe: read-only file watch.
    """

    POLL_INTERVAL_S = 0.25
    BACKOFF_MAX_S = 5.0

    def __init__(
        self,
        log_path: Path | None = None,
        on_change: Callable[[PlaylistSnapshot], None] | None = None,
    ) -> None:
        self._log_path = log_path or default_log_path()
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._snapshot = PlaylistSnapshot(None, None, None)

    @property
    def log_path(self) -> Path:
        return self._log_path

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="rl-log-watcher", daemon=True
        )
        self._thread.start()

    def stop(self, join_timeout: float = 1.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=join_timeout)

    def snapshot(self) -> PlaylistSnapshot:
        with self._lock:
            return self._snapshot

    def _set_snapshot(self, pid: int) -> None:
        snap = PlaylistSnapshot(
            playlist_id=pid,
            playlist_label=label_for_playlist_id(pid),
            seen_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._snapshot = snap
        if self._on_change is not None:
            try:
                self._on_change(snap)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                if not self._log_path.exists():
                    self._wait(backoff)
                    backoff = min(backoff * 2, self.BACKOFF_MAX_S)
                    continue
                backoff = 0.5
                self._tail_once()
            except Exception:
                # Never let a transient IO error crash the watcher thread.
                self._wait(backoff)
                backoff = min(backoff * 2, self.BACKOFF_MAX_S)

    def _wait(self, secs: float) -> None:
        # Use the stop event so shutdown is prompt.
        self._stop.wait(timeout=secs)

    def _tail_once(self) -> None:
        path = self._log_path
        with path.open("r", encoding="utf-8", errors="replace") as fp:
            # Replay from the start so we capture a playlist line written
            # before the watcher started (e.g. RL was already in a match).
            buf = ""
            last_mtime: float | None = None
            last_size: int | None = None
            while not self._stop.is_set():
                chunk = fp.read()
                if chunk:
                    buf += chunk
                    while True:
                        nl = buf.find("\n")
                        if nl < 0:
                            break
                        line, buf = buf[:nl], buf[nl + 1 :]
                        pid = parse_playlist_id(line)
                        if pid is not None:
                            self._set_snapshot(pid)
                try:
                    st = path.stat()
                    size = st.st_size
                    mtime = st.st_mtime
                    pos = fp.tell()
                except OSError:
                    return
                # Rotation / truncation: file is shorter than where we are,
                # or its size shrank, or its mtime changed without yielding
                # any new bytes (same-size in-place replacement).
                if size < pos:
                    return
                if last_size is not None and size < last_size:
                    return
                if (
                    not chunk
                    and last_mtime is not None
                    and mtime != last_mtime
                    and size <= pos
                ):
                    return
                last_size = size
                last_mtime = mtime
                self._stop.wait(timeout=self.POLL_INTERVAL_S)
