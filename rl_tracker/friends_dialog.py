from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from .config import load_friends, save_friends
from .history import HistoryStore


class ManageFriendsDialog(QDialog):
    """Searchable, multi-select dialog for picking friend platform IDs from
    everyone seen in match history."""

    def __init__(self, parent, store: HistoryStore) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manage Friends")
        self.resize(480, 560)
        self._store = store
        self._friends: set[str] = set(load_friends())
        # platform_id -> display name (most recently seen wins)
        self._teammates: dict[str, str] = {}
        self._opponents: dict[str, str] = {}

        self._collect_players()
        self._build_ui()
        self._populate()

    def _collect_players(self) -> None:
        for m in self._store.all_matches():
            mt = m.get("my_team")
            for p in m.get("players", []):
                if p.get("is_me"):
                    continue
                pid = p.get("platform_id")
                name = p.get("name") or ""
                if not pid:
                    continue
                if mt in (0, 1) and p.get("team") == mt:
                    self._teammates[pid] = name
                else:
                    self._opponents[pid] = name

    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Select players to register as friends. Friends are matched by platform ID, so name changes won't break the list."))

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search by name or platform…")
        self._search.textChanged.connect(self._apply_search)
        v.addWidget(self._search)

        self._show_opponents = QCheckBox("Include players seen only as opponents")
        self._show_opponents.toggled.connect(self._populate)
        v.addWidget(self._show_opponents)

        self._list = QListWidget()
        self._list.itemChanged.connect(self._on_item_changed)
        v.addWidget(self._list, 1)

        info = QLabel()
        info.setStyleSheet("color: gray;")
        self._info = info
        v.addWidget(info)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def _populate(self) -> None:
        # Capture current checks so toggling "include opponents" preserves them.
        self._sync_checks_to_friends()

        candidates: dict[str, str] = dict(self._teammates)
        if self._show_opponents.isChecked():
            for pid, name in self._opponents.items():
                candidates.setdefault(pid, name)

        # Always include any currently-saved friends, even if not in candidates.
        for pid in self._friends:
            candidates.setdefault(pid, pid.split("|", 1)[-1])

        items = sorted(
            candidates.items(),
            key=lambda kv: ((kv[1] or "").lower(), kv[0]),
        )

        self._list.blockSignals(True)
        self._list.clear()
        for pid, name in items:
            label = f"{name}  ({pid.split('|', 1)[0]})"
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, pid)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(
                Qt.CheckState.Checked if pid in self._friends else Qt.CheckState.Unchecked
            )
            self._list.addItem(it)
        self._list.blockSignals(False)
        self._apply_search(self._search.text())
        self._update_info()

    def _apply_search(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self._list.count()):
            it = self._list.item(i)
            if not needle:
                it.setHidden(False)
                continue
            label = it.text().lower()
            pid = (it.data(Qt.ItemDataRole.UserRole) or "").lower()
            it.setHidden(needle not in label and needle not in pid)

    def _sync_checks_to_friends(self) -> None:
        for i in range(self._list.count()):
            it = self._list.item(i)
            pid = it.data(Qt.ItemDataRole.UserRole)
            if not pid:
                continue
            if it.checkState() == Qt.CheckState.Checked:
                self._friends.add(pid)
            else:
                self._friends.discard(pid)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        pid = item.data(Qt.ItemDataRole.UserRole)
        if not pid:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._friends.add(pid)
        else:
            self._friends.discard(pid)
        self._update_info()

    def _update_info(self) -> None:
        self._info.setText(f"{len(self._friends)} friend(s) selected")

    def _on_save(self) -> None:
        self._sync_checks_to_friends()
        save_friends(sorted(self._friends))
        self.accept()

    def selected_friends(self) -> list[str]:
        return sorted(self._friends)
