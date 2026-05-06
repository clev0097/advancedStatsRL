from __future__ import annotations

import argparse
import sys
from pathlib import Path

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

    session = SessionState()
    client = StatsClient(dump_path=args.dump_events)

    overlay = Overlay(session, client)
    client._on_status = overlay.set_status  # bind status updates to the overlay
    client.start()
    overlay.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
