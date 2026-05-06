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
        self._decoder = json.JSONDecoder()
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
        # The Stats API emits a stream of concatenated JSON objects with no
        # delimiter (`}{`). Use raw_decode to peel one object at a time.
        buf = ""
        while not self._stop.is_set():
            chunk = sock.recv(4096)
            if not chunk:
                return  # peer closed
            buf += chunk.decode("utf-8", errors="replace")
            while True:
                stripped = buf.lstrip()
                if not stripped:
                    buf = stripped
                    break
                try:
                    obj, end = self._decoder.raw_decode(stripped)
                except json.JSONDecodeError:
                    # Incomplete object; wait for more bytes.
                    buf = stripped
                    break
                buf = stripped[end:]
                self._handle_event(obj)

    def _handle_event(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        # `Data` arrives as a JSON-encoded string; unwrap it so consumers see
        # a real dict.
        data = event.get("Data") if "Data" in event else event.get("data")
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                if "Data" in event:
                    event["Data"] = parsed
                else:
                    event["data"] = parsed
        if self._dump_fp is not None:
            try:
                self._dump_fp.write(json.dumps(event) + "\n")
                self._dump_fp.flush()
            except Exception:
                pass
        self.events.put(event)
