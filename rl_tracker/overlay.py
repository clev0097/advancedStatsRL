from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QFont, QPainter
from PyQt6.QtWidgets import QMenu, QWidget

from .config import state_file
from .history import HistoryStore
from .mmr_session import MmrSession, MmrStatus
from .session import SessionState
from .stats_client import StatsClient


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


class Overlay(QWidget):
    def __init__(
        self,
        session: SessionState,
        client: StatsClient,
        history: HistoryStore | None = None,
        mmr_session: MmrSession | None = None,
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
        self._mmr = mmr_session
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

    def request_repaint(self) -> None:
        """Thread-safe-ish repaint trigger callable from worker threads.

        Qt requires UI updates from the main thread; we rely on the existing
        100 ms ``QTimer`` drain in ``_drain`` to pick up state changes. This
        method just marks the widget dirty so the next paint reads MMR state.
        """
        try:
            self.update()
        except Exception:
            pass

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
        streak = ""
        if self._session.streak_kind and self._session.streak_count:
            streak = f"  Streak {self._session.streak_kind}{self._session.streak_count}"
        lines = [f"Session  W {totals.wins}  L {totals.losses}{streak}   [{self._status}]"]
        if self._session.by_playlist:
            for name, t in sorted(self._session.by_playlist.items()):
                lines.append(f"  {name:<14} {t.wins}-{t.losses}")
        else:
            lines.append("  (no matches yet)")

        if self._mmr is not None:
            view = self._mmr.snapshot()
            if view.status == MmrStatus.FAILED and view.failure_reason is not None:
                lines.append(f"MMR  ({view.failure_reason.value})")
            elif view.status in (MmrStatus.LOADING,) and not view.by_playlist:
                lines.append("MMR  …")
            else:
                ranked_played = view.total_matches_delta
                lines.append(
                    f"MMR  {_signed(view.total_delta)}   (ranked {ranked_played})"
                )
                for pid in sorted(view.by_playlist.keys()):
                    pd = view.by_playlist[pid]
                    if pd.matches_delta <= 0 and pd.rating_delta == 0:
                        continue
                    name = pd.name or f"playlist_{pid}"
                    lines.append(f"  {name:<14} {_signed(pd.rating_delta)}")

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

        if self._mmr is not None:
            mmr_reset = QAction("Reset MMR baseline", self)
            mmr_reset.triggered.connect(self._mmr.reset)
            menu.addAction(mmr_reset)

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
        if self._mmr is not None:
            self._mmr.reset()
        self.update()

    def _open_history(self) -> None:
        if self._history is None:
            return
        if self._history_window is None:
            from .history_window import HistoryWindow

            def on_set_platform_id(pid: str) -> None:
                self._session.my_platform_id = pid

            self._history_window = HistoryWindow(
                self._history, on_set_platform_id=on_set_platform_id
            )
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
