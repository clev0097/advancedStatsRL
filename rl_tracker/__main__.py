from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import history_db_path, load_my_platform_id
from .history import HistoryStore
from .ini_helper import ensure_stats_api_enabled
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

    session = SessionState(history=history, my_platform_id=load_my_platform_id())
    client = StatsClient(dump_path=args.dump_events)

    overlay = Overlay(session, client, history)
    client._on_status = overlay.set_status  # bind status updates to the overlay
    client.start()
    overlay.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
