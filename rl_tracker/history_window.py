from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from datetime import datetime

from .config import (
    load_my_platform_id,
    load_session_gap_minutes,
    save_my_platform_id,
)
from .history import HistoryStore


def _format_local(dt_or_iso) -> str:
    """Format an ISO string or datetime as 'Mon May 5, 7:32 PM' in local time."""
    if dt_or_iso is None:
        return ""
    if isinstance(dt_or_iso, str):
        try:
            dt = datetime.fromisoformat(dt_or_iso)
        except ValueError:
            return dt_or_iso
    else:
        dt = dt_or_iso
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    # %#d / %#I are the Windows equivalents of %-d / %-I (no leading zero).
    return dt.strftime("%a %b %#d, %#I:%M %p")


class HistoryWindow(QWidget):
    def __init__(
        self,
        store: HistoryStore,
        on_set_platform_id: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(None)
        self.setWindowTitle("RL Tracker — Match history")
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.resize(720, 520)
        self._store = store
        self._on_set_platform_id = on_set_platform_id

        layout = QVBoxLayout(self)

        # Banner area: prompt for platform ID if missing. Replaced dynamically
        # with player-picker buttons when matches exist but ID isn't set.
        self._banner = QFrame()
        self._banner.setStyleSheet(
            "QFrame { background:#553; color:#ffd; border-radius:4px; } "
            "QLabel { color:#ffd; } "
            "QPushButton { padding:4px 10px; }"
        )
        self._banner_layout = QVBoxLayout(self._banner)
        self._banner_layout.setContentsMargins(8, 6, 8, 6)
        layout.addWidget(self._banner)

        # Filters row.
        filt_row = QHBoxLayout()
        filt_row.addWidget(QLabel("Playlist:"))
        self._playlist_filter = QComboBox()
        self._playlist_filter.currentIndexChanged.connect(self._refresh)
        filt_row.addWidget(self._playlist_filter)
        filt_row.addStretch(1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh)
        filt_row.addWidget(refresh_btn)
        layout.addLayout(filt_row)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, 1)

        # Tab 1: teammate breakdown (per session).
        self._teammate_table = QTableWidget(0, 6)
        self._teammate_table.setHorizontalHeaderLabels(
            ["Session", "Teammates", "Playlist", "W", "L", "Win %"]
        )
        self._teammate_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self._teammate_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tabs.addTab(self._teammate_table, "By teammate")

        # Tab 2: recent matches.
        self._recent_table = QTableWidget(0, 5)
        self._recent_table.setHorizontalHeaderLabels(
            ["Ended", "Playlist", "Result", "My team", "Players"]
        )
        self._recent_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self._recent_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tabs.addTab(self._recent_table, "Recent matches")

        self._refresh()

    # ------------------------------------------------------------------
    def _current_playlist(self) -> str | None:
        if self._playlist_filter.currentIndex() <= 0:
            return None
        return self._playlist_filter.currentText()

    def _update_playlist_options(self, recents: list[dict]) -> None:
        playlists = sorted({r["playlist"] for r in recents})
        current = self._playlist_filter.currentText()
        self._playlist_filter.blockSignals(True)
        self._playlist_filter.clear()
        self._playlist_filter.addItem("(all)")
        for p in playlists:
            self._playlist_filter.addItem(p)
        idx = self._playlist_filter.findText(current)
        if idx >= 0:
            self._playlist_filter.setCurrentIndex(idx)
        self._playlist_filter.blockSignals(False)

    def _clear_banner(self) -> None:
        while self._banner_layout.count():
            item = self._banner_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _show_picker_banner(self, latest_players: list[dict]) -> None:
        self._clear_banner()
        self._banner_layout.addWidget(
            QLabel("Which one of these is you? (click once — saved for next time)")
        )
        row = QHBoxLayout()
        for p in latest_players:
            btn = QPushButton(f"{p['name']}  ({p['platform_id'].split('|')[0]})")
            pid = p["platform_id"]
            btn.clicked.connect(lambda _checked=False, x=pid: self._pick_self(x))
            row.addWidget(btn)
        row.addStretch(1)
        self._banner_layout.addLayout(row)
        self._banner.show()

    def _show_no_matches_banner(self) -> None:
        self._clear_banner()
        self._banner_layout.addWidget(
            QLabel(
                "Play one match — afterward you'll be able to pick which player is "
                "you, and your W/L history will be attributed automatically."
            )
        )
        self._banner.show()

    def _pick_self(self, platform_id: str) -> None:
        save_my_platform_id(platform_id)
        if self._on_set_platform_id is not None:
            self._on_set_platform_id(platform_id)
        self._refresh()

    def _refresh(self) -> None:
        my_id = load_my_platform_id()
        recent = self._store.recent(limit=200)
        if not my_id:
            if recent:
                self._show_picker_banner(recent[0]["players"])
            else:
                self._show_no_matches_banner()
        else:
            self._banner.hide()
        self._update_playlist_options(recent)
        playlist = self._current_playlist()
        if playlist:
            recent = [r for r in recent if r["playlist"] == playlist]

        # Teammate breakdown (per session).
        breakdown = self._store.teammate_breakdown(
            my_id, playlist, gap_minutes=load_session_gap_minutes()
        )
        self._teammate_table.setRowCount(len(breakdown))
        for row, agg in enumerate(breakdown):
            label = ", ".join(agg.teammate_names) if agg.teammate_names else "(solo)"
            played = agg.wins + agg.losses
            pct = f"{(agg.wins / played * 100):.0f}%" if played else "—"
            session_label = _format_local(agg.session_started_at)
            cells = [
                session_label,
                label,
                agg.playlist,
                str(agg.wins),
                str(agg.losses),
                pct,
            ]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                if col >= 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._teammate_table.setItem(row, col, item)

        # Recent matches.
        self._recent_table.setRowCount(len(recent))
        for row, r in enumerate(recent):
            won = r["won"]
            result = "—" if won is None else ("W" if won else "L")
            my_team = "?" if r["my_team"] is None else str(r["my_team"])
            roster = ", ".join(
                ("[me] " if p["is_me"] else "") + f"{p['name']} (T{p['team']})"
                for p in r["players"]
            )
            cells = [
                _format_local(r["ended_at"]),
                r["playlist"],
                result,
                my_team,
                roster,
            ]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                if col == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._recent_table.setItem(row, col, item)

    # Public hook so the overlay can ping us when a new match is recorded.
    def notify_new_match(self) -> None:
        self._refresh()
