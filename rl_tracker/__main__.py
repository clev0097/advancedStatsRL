from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import history_db_path, load_my_platform_id
from .history import HistoryStore
from .ini_helper import ensure_stats_api_enabled
from .log_watcher import LogPlaylistWatcher, default_log_path
from .mmr_identity import PlayerIdentity
from .mmr_session import MmrSession
from .session import SessionState
from .stats_client import StatsClient


def main() -> int:
    parser = argparse.ArgumentParser(prog="rl-tracker")
    parser.add_argument(
        "--dump-events",
        type=Path,
        metavar="PATH",
        help="Append every raw event line received from the Stats API to PATH (debug).",
    )
    parser.add_argument(
        "--no-ini",
        action="store_true",
        help="Skip writing/verifying TAStatsAPI.ini on startup.",
    )
    parser.add_argument(
        "--launch-log",
        type=Path,
        metavar="PATH",
        default=None,
        help=(
            "Override the path to Rocket League's Launch.log. "
            f"Defaults to {default_log_path()}."
        ),
    )
    parser.add_argument(
        "--no-launch-log",
        action="store_true",
        help="Disable the Launch.log tail (no ranked-vs-casual detection).",
    )
    parser.add_argument(
        "--no-mmr",
        action="store_true",
        help="Disable tracker.gg MMR polling.",
    )
    parser.add_argument(
        "--mmr-platform",
        choices=("steam", "epic"),
        default=None,
        help="Override MMR identity platform (skips Launch.log auto-detect).",
    )
    parser.add_argument(
        "--mmr-id",
        default=None,
        help="Override MMR identity id (Steam ID64 or Epic display name).",
    )
    args = parser.parse_args()

    if not args.no_ini:
        try:
            changed = ensure_stats_api_enabled()
            if changed:
                print(
                    "TAStatsAPI.ini was created/updated. Restart Rocket League once "
                    "for the change to take effect.",
                    file=sys.stderr,
                )
        except OSError as e:
            print(f"Could not write TAStatsAPI.ini: {e}", file=sys.stderr)

    from PyQt6.QtWidgets import QApplication

    from .overlay import Overlay

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    db_path = history_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    history = HistoryStore(db_path)

    log_watcher: LogPlaylistWatcher | None = None
    if not args.no_launch_log:
        log_watcher = LogPlaylistWatcher(log_path=args.launch_log)
        log_watcher.start()

    session = SessionState(
        history=history,
        my_platform_id=load_my_platform_id(),
        playlist_watcher=log_watcher,
    )
    client = StatsClient(dump_path=args.dump_events)

    mmr_session: MmrSession | None = None
    if not args.no_mmr:
        identity_override: PlayerIdentity | None = None
        if args.mmr_platform and args.mmr_id:
            identity_override = PlayerIdentity(args.mmr_platform, args.mmr_id)
        mmr_session = MmrSession(
            log_path=(log_watcher.log_path if log_watcher is not None else None),
            history=history,
            identity_override=identity_override,
        )

    overlay = Overlay(session, client, history, mmr_session=mmr_session)
    client._on_status = overlay.set_status  # bind status updates to the overlay
    if mmr_session is not None:
        mmr_session._on_change = overlay.request_repaint  # type: ignore[attr-defined]
        mmr_session.start()
        prev_recorded = session.on_match_recorded
        def _on_match(m, _prev=prev_recorded, _mmr=mmr_session):
            if _prev is not None:
                try:
                    _prev(m)
                except Exception:
                    pass
            _mmr.nudge()
        session.on_match_recorded = _on_match
    client.start()
    overlay.show()

    if log_watcher is not None:
        app.aboutToQuit.connect(log_watcher.stop)
    if mmr_session is not None:
        app.aboutToQuit.connect(mmr_session.stop)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
