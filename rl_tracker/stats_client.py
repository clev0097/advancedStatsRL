from __future__ import annotations

import json
import queue
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from .config import (
    RECONNECT_BACKOFF_MAX_S,
    RECONNECT_BACKOFF_START_S,
    STATS_API_HOST,
    STATS_API_PORT,
)


class StatsClient:
    """Background TCP client for the Rocket League Stats API.

    Connects to 127.0.0.1:49123, reads newline-delimited JSON, and pushes
    parsed events onto a thread-safe queue. The Qt main thread drains the
    queue via a QTimer.
    """

    def __init__(
        self,
        host: str = STATS_API_HOST,
        port: int = STATS_API_PORT,
        on_status: Callable[[str], None] | None = None,
        dump_path: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._on_status = on_status or (lambda _: None)
        self._dump_path = dump_path
        self._dump_fp = None
        self.events: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._dump_path is not None:
            self._dump_fp = self._dump_path.open("a", encoding="utf-8")
        self._thread = threading.Thread(target=self._run, name="rl-stats-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._dump_fp is not None:
            try:
                self._dump_fp.close()
            except Exception:
                pass
            self._dump_fp = None

    def _run(self) -> None:
        backoff = RECONNECT_BACKOFF_START_S
        while not self._stop.is_set():
            try:
                self._on_status("connecting")
                with socket.create_connection((self.host, self.port), timeout=5) as sock:
                    sock.settimeout(None)
                    self._on_status("connected")
                    backoff = RECONNECT_BACKOFF_START_S
                    self._read_loop(sock)
            except (OSError, socket.timeout):
                self._on_status("waiting for game")
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_S)
        self._on_status("stopped")

    def _read_loop(self, sock: socket.socket) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            chunk = sock.recv(4096)
            if not chunk:
                return  # peer closed
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl]).strip()
                del buf[: nl + 1]
                if not line:
                    continue
                self._handle_line(line)

    def _handle_line(self, line: bytes) -> None:
        if self._dump_fp is not None:
            try:
                self._dump_fp.write(line.decode("utf-8", errors="replace") + "\n")
                self._dump_fp.flush()
            except Exception:
                pass
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if isinstance(event, dict):
            self.events.put(event)
