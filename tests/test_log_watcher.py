from __future__ import annotations

import time
from pathlib import Path

import pytest

from rl_tracker.log_watcher import (
    LogPlaylistWatcher,
    label_for_playlist_id,
    parse_playlist_id,
)


def test_parse_known_forms():
    assert parse_playlist_id("Foo Playlist=11 bar") == 11
    assert parse_playlist_id("PlaylistId: 27") == 27
    assert parse_playlist_id("Playlist_ID = 34") == 34
    assert parse_playlist_id("nothing here") is None


def test_label_lookup_and_unknown_fallback():
    assert label_for_playlist_id(11) == "Ranked Doubles"
    assert label_for_playlist_id(2) == "Casual Doubles"
    assert label_for_playlist_id(99999) == "playlist_99999"


def _wait_for_pid(watcher: LogPlaylistWatcher, expected: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = watcher.snapshot()
        if snap.playlist_id == expected:
            return
        time.sleep(0.05)
    pytest.fail(f"watcher never saw playlist_id={expected}; latest={watcher.snapshot()}")


def test_watcher_picks_up_appended_lines(tmp_path: Path):
    log = tmp_path / "Launch.log"
    log.write_text("startup line with no playlist\n", encoding="utf-8")

    watcher = LogPlaylistWatcher(log_path=log)
    watcher.POLL_INTERVAL_S = 0.02
    watcher.start()
    try:
        with log.open("a", encoding="utf-8") as fp:
            fp.write("OnlineGame: PlaylistId=11 joining\n")
            fp.flush()
        _wait_for_pid(watcher, 11)

        snap = watcher.snapshot()
        assert snap.playlist_label == "Ranked Doubles"
        assert snap.seen_at is not None

        with log.open("a", encoding="utf-8") as fp:
            fp.write("Loading match Playlist=2\n")
            fp.flush()
        _wait_for_pid(watcher, 2)
        assert watcher.snapshot().playlist_label == "Casual Doubles"
    finally:
        watcher.stop()


def test_watcher_handles_unknown_id(tmp_path: Path):
    log = tmp_path / "Launch.log"
    log.write_text("Playlist=12345\n", encoding="utf-8")
    watcher = LogPlaylistWatcher(log_path=log)
    watcher.POLL_INTERVAL_S = 0.02
    watcher.start()
    try:
        _wait_for_pid(watcher, 12345)
        assert watcher.snapshot().playlist_label == "playlist_12345"
    finally:
        watcher.stop()


def test_watcher_recovers_from_truncation(tmp_path: Path):
    log = tmp_path / "Launch.log"
    log.write_text("Playlist=10\n", encoding="utf-8")
    watcher = LogPlaylistWatcher(log_path=log)
    watcher.POLL_INTERVAL_S = 0.02
    watcher.start()
    try:
        _wait_for_pid(watcher, 10)
        # Simulate game restart: truncate and write a different ID.
        log.write_text("Playlist=11\n", encoding="utf-8")
        _wait_for_pid(watcher, 11)
    finally:
        watcher.stop()


def test_watcher_tolerates_missing_log(tmp_path: Path):
    log = tmp_path / "missing.log"
    watcher = LogPlaylistWatcher(log_path=log)
    watcher.POLL_INTERVAL_S = 0.02
    watcher.start()
    try:
        # Initially no snapshot.
        assert watcher.snapshot().playlist_id is None
        # Create the file later; watcher should pick it up.
        log.write_text("Playlist=27\n", encoding="utf-8")
        _wait_for_pid(watcher, 27)
    finally:
        watcher.stop()


def test_stop_returns_promptly(tmp_path: Path):
    log = tmp_path / "Launch.log"
    log.write_text("", encoding="utf-8")
    watcher = LogPlaylistWatcher(log_path=log)
    watcher.POLL_INTERVAL_S = 0.05
    watcher.start()
    t0 = time.monotonic()
    watcher.stop(join_timeout=2.0)
    assert time.monotonic() - t0 < 1.5
