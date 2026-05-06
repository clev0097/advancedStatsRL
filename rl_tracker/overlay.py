from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QFont, QPainter
from PyQt6.QtWidgets import QMenu, QWidget

from .config import state_file
from .history import HistoryStore
from .session import SessionState
from .stats_client import StatsClient


class Overlay(QWidget):
    def __init__(
        self,
        session: SessionState,
        client: StatsClient,
        history: HistoryStore | None = None,
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMinimumSize(220, 80)

        self._session = session
        self._client = client
        self._history = history
        self._history_window = None  # lazy
        self._status = "starting"
        self._click_through = False
        self._drag_offset: QPoint | None = None

        if history is not None:
            session.on_match_recorded = lambda _m: self._notify_history_window()

        self._restore_position()

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._drain)
        self._tick.start(100)

    # --- persistence -------------------------------------------------------
    def _restore_position(self) -> None:
        try:
            data = json.loads(state_file().read_text(encoding="utf-8"))
            self.move(int(data["x"]), int(data["y"]))
        except Exception:
            self.move(40, 40)

    def _save_position(self) -> None:
        try:
            state_file().parent.mkdir(parents=True, exist_ok=True)
            state_file().write_text(
                json.dumps({"x": self.x(), "y": self.y()}), encoding="utf-8"
            )
        except Exception:
            pass

    # --- public API for the rest of the app --------------------------------
    def set_status(self, status: str) -> None:
        self._status = status
        self.update()

    # --- event drain -------------------------------------------------------
    def _drain(self) -> None:
        changed = False
        while True:
            try:
                event = self._client.events.get_nowait()
            except Exception:
                break
            self._session.apply(event)
            changed = True
        if changed:
            self.update()

    # --- painting ----------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(0, 0, 0, 170))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(self.rect(), 8, 8)

        p.setPen(QColor(235, 235, 235))
        p.setFont(QFont("Consolas", 10))

        totals = self._session.totals()
        lines = [f"Session  W {totals.wins}  L {totals.losses}   [{self._status}]"]
        if self._session.by_playlist:
            for name, t in sorted(self._session.by_playlist.items()):
                lines.append(f"  {name:<14} {t.wins}-{t.losses}")
        else:
            lines.append("  (no matches yet)")

        y = 18
        for line in lines:
            p.drawText(10, y, line)
            y += 16

        # auto-size to content
        needed_h = y + 2
        needed_w = max(220, max((p.fontMetrics().horizontalAdvance(l) for l in lines), default=200) + 24)
        if self.height() != needed_h or self.width() != needed_w:
            self.resize(needed_w, needed_h)

    # --- interaction -------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            self._save_position()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        reset_act = QAction("Reset session", self)
        reset_act.triggered.connect(self._reset)
        menu.addAction(reset_act)

        ct_act = QAction(
            "Disable click-through" if self._click_through else "Enable click-through",
            self,
        )
        ct_act.triggered.connect(self._toggle_click_through)
        menu.addAction(ct_act)

        if self._history is not None:
            menu.addSeparator()
            hist_act = QAction("Match history…", self)
            hist_act.triggered.connect(self._open_history)
            menu.addAction(hist_act)

        menu.addSeparator()
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self._quit)
        menu.addAction(quit_act)

        menu.exec(event.globalPos())

    def _reset(self) -> None:
        self._session.reset()
        self.update()

    def _open_history(self) -> None:
        if self._history is None:
            return
        if self._history_window is None:
            from .history_window import HistoryWindow

            self._history_window = HistoryWindow(self._history)
        self._history_window.show()
        self._history_window.raise_()
        self._history_window.activateWindow()

    def _notify_history_window(self) -> None:
        if self._history_window is not None:
            self._history_window.notify_new_match()

    def _toggle_click_through(self) -> None:
        self._click_through = not self._click_through
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, self._click_through
        )
        # Re-show so the flag takes effect on Windows
        self.hide()
        self.show()

    def _quit(self) -> None:
        self._save_position()
        self._client.stop()
        from PyQt6.QtWidgets import QApplication

        QApplication.quit()

    def closeEvent(self, event) -> None:
        self._save_position()
        self._client.stop()
        super().closeEvent(event)
