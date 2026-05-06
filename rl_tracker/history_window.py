from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox,
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

from .config import load_my_platform_id, save_my_platform_id, settings_file
from .history import HistoryStore


class HistoryWindow(QWidget):
    def __init__(self, store: HistoryStore) -> None:
        super().__init__(None)
        self.setWindowTitle("RL Tracker — Match history")
        self.resize(720, 520)
        self._store = store

        layout = QVBoxLayout(self)

        # Banner: prompt for platform ID if missing.
        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(
            "background:#553; color:#ffd; padding:6px; border-radius:4px;"
        )
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

        # Tab 1: teammate breakdown.
        self._teammate_table = QTableWidget(0, 5)
        self._teammate_table.setHorizontalHeaderLabels(
            ["Teammates", "Playlist", "W", "L", "Win %"]
        )
        self._teammate_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
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

    def _refresh(self) -> None:
        my_id = load_my_platform_id()
        if not my_id:
            self._banner.setText(
                f"MY_PLATFORM_ID is not set. Edit {settings_file()} and add "
                f'\'"my_platform_id": "Steam|76561198…|0"\' (find your PrimaryId in '
                "events.log). Until then, matches are recorded but not attributed to you."
            )
            self._banner.show()
        else:
            self._banner.hide()

        recent = self._store.recent(limit=200)
        self._update_playlist_options(recent)
        playlist = self._current_playlist()
        if playlist:
            recent = [r for r in recent if r["playlist"] == playlist]

        # Teammate breakdown.
        breakdown = self._store.teammate_breakdown(my_id, playlist)
        self._teammate_table.setRowCount(len(breakdown))
        for row, agg in enumerate(breakdown):
            label = ", ".join(agg.teammate_names) if agg.teammate_names else "(solo)"
            played = agg.wins + agg.losses
            pct = f"{(agg.wins / played * 100):.0f}%" if played else "—"
            cells = [label, agg.playlist, str(agg.wins), str(agg.losses), pct]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                if col >= 2:
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
            cells = [r["ended_at"], r["playlist"], result, my_team, roster]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                if col == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._recent_table.setItem(row, col, item)

    # Public hook so the overlay can ping us when a new match is recorded.
    def notify_new_match(self) -> None:
        self._refresh()
