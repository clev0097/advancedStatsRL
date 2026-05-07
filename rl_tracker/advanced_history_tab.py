from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Callable, Hashable

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import (
    load_advanced_view,
    load_my_platform_id,
    load_session_gap_minutes,
    save_advanced_view,
)
from .history import HistoryStore, assign_sessions


# --------------------------------------------------------------------------
# Per-match helpers
# --------------------------------------------------------------------------
def _parse_dt(val) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val
    else:
        try:
            dt = datetime.fromisoformat(val)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone()
        dt = dt.replace(tzinfo=None)
    return dt


def _my_team(m: dict) -> int | None:
    mt = m.get("my_team")
    return mt if mt in (0, 1) else None


def _teammates(m: dict) -> list[dict]:
    mt = _my_team(m)
    if mt is None:
        return []
    return [p for p in m["players"] if not p["is_me"] and p["team"] == mt]


def _opponents(m: dict) -> list[dict]:
    mt = _my_team(m)
    if mt is None:
        return []
    return [p for p in m["players"] if p["team"] != mt]


def _score_diff(m: dict) -> int:
    mt = _my_team(m)
    t0, t1 = m.get("team0_score", 0), m.get("team1_score", 0)
    if mt == 1:
        return t1 - t0
    return t0 - t1


def _format_local(dt_or_iso) -> str:
    dt = _parse_dt(dt_or_iso)
    if dt is None:
        return ""
    return dt.strftime("%a %b %#d, %#I:%M %p")


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------
def apply_filters(matches: list[dict], f: dict, gap_minutes: int) -> list[dict]:
    pl_inc = set(f.get("playlist_include") or [])
    pl_exc = set(f.get("playlist_exclude") or [])
    tm_inc = set(f.get("teammate_include") or [])
    tm_exc = set(f.get("teammate_exclude") or [])
    op_inc = set(f.get("opponent_include") or [])
    op_exc = set(f.get("opponent_exclude") or [])
    result = f.get("result") or "any"
    ot = f.get("overtime") or "any"
    date_from = _parse_dt(f.get("date_from"))
    date_to = _parse_dt(f.get("date_to"))
    diff_min = f.get("diff_min")
    diff_max = f.get("diff_max")
    min_session = int(f.get("min_session_size") or 0)

    out: list[dict] = []
    for m in matches:
        if pl_inc and m["playlist"] not in pl_inc:
            continue
        if pl_exc and m["playlist"] in pl_exc:
            continue

        tm_ids = {p["platform_id"] for p in _teammates(m)}
        if tm_inc and tm_inc.isdisjoint(tm_ids):
            continue
        if tm_exc and not tm_exc.isdisjoint(tm_ids):
            continue

        op_ids = {p["platform_id"] for p in _opponents(m)}
        if op_inc and op_inc.isdisjoint(op_ids):
            continue
        if op_exc and not op_exc.isdisjoint(op_ids):
            continue

        won = m.get("won")
        if result == "win" and won is not True:
            continue
        if result == "loss" and won is not False:
            continue

        is_ot = bool(m.get("overtime"))
        if ot == "ot" and not is_ot:
            continue
        if ot == "no_ot" and is_ot:
            continue

        end = _parse_dt(m.get("ended_at"))
        if date_from is not None and (end is None or end < date_from):
            continue
        if date_to is not None and (end is None or end > date_to):
            continue

        if diff_min is not None and _score_diff(m) < diff_min:
            continue
        if diff_max is not None and _score_diff(m) > diff_max:
            continue

        out.append(m)

    if min_session > 1 and out:
        # Sort ascending by end time, assign sessions, count, drop short ones.
        sorted_asc = sorted(out, key=lambda x: _parse_dt(x["ended_at"]) or datetime.min)
        sess = assign_sessions(
            [_parse_dt(x["ended_at"]) or datetime.min for x in sorted_asc],
            gap_minutes,
        )
        counts = Counter(sess)
        keep_guids = {
            sorted_asc[i]["match_guid"]
            for i, s in enumerate(sess)
            if counts[s] >= min_session
        }
        out = [m for m in out if m["match_guid"] in keep_guids]

    return out


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------
def _teammate_set_key(m: dict) -> tuple:
    return tuple(sorted(p["platform_id"] for p in _teammates(m)))


def _teammate_set_label(m: dict) -> str:
    names = sorted(p["name"] for p in _teammates(m))
    return ", ".join(names) if names else "(solo)"


def _opponent_set_key(m: dict) -> tuple:
    return tuple(sorted(p["platform_id"] for p in _opponents(m)))


def _opponent_set_label(m: dict) -> str:
    names = sorted(p["name"] for p in _opponents(m))
    return ", ".join(names) if names else "(none)"


def _result_label(m: dict) -> str:
    won = m.get("won")
    if won is True:
        return "W"
    if won is False:
        return "L"
    return "—"


def _date_key(m: dict) -> str:
    dt = _parse_dt(m.get("ended_at"))
    return dt.date().isoformat() if dt else ""


def _date_label(m: dict) -> str:
    dt = _parse_dt(m.get("ended_at"))
    return dt.strftime("%a %b %#d, %Y") if dt else ""


GROUP_FIELDS: list[tuple[str, str]] = [
    ("playlist", "Playlist"),
    ("teammate_set", "Teammate set"),
    ("opponent_set", "Opponent set"),
    ("result", "Result (W/L)"),
    ("session", "Session"),
    ("date", "Date (day)"),
    ("my_team", "My team #"),
    ("overtime", "Overtime"),
]

# Map: field_id -> (key_func(match) -> hashable, label_func(match) -> str)
GROUP_EXTRACTORS: dict[str, tuple[Callable[[dict], Hashable], Callable[[dict], str]]] = {
    "playlist": (lambda m: m["playlist"], lambda m: m["playlist"]),
    "teammate_set": (_teammate_set_key, _teammate_set_label),
    "opponent_set": (_opponent_set_key, _opponent_set_label),
    "result": (lambda m: m.get("won"), _result_label),
    "date": (_date_key, _date_label),
    "my_team": (
        lambda m: m.get("my_team"),
        lambda m: "?" if m.get("my_team") is None else f"T{m['my_team']}",
    ),
    "overtime": (
        lambda m: bool(m.get("overtime")),
        lambda m: "OT" if m.get("overtime") else "Reg",
    ),
}


def group_and_aggregate(
    matches: list[dict], group_fields: list[str], gap_minutes: int
) -> list[dict]:
    if not group_fields:
        return []

    # Pre-compute session ids for the whole filtered set if needed.
    session_id: dict[str, int] = {}
    if "session" in group_fields:
        sorted_asc = sorted(
            matches, key=lambda x: _parse_dt(x["ended_at"]) or datetime.min
        )
        sess = assign_sessions(
            [_parse_dt(x["ended_at"]) or datetime.min for x in sorted_asc],
            gap_minutes,
        )
        session_id = {sorted_asc[i]["match_guid"]: s for i, s in enumerate(sess)}

    def key_for(m: dict) -> tuple:
        parts = []
        for f in group_fields:
            if f == "session":
                parts.append(session_id.get(m["match_guid"], -1))
            else:
                parts.append(GROUP_EXTRACTORS[f][0](m))
        return tuple(parts)

    def label_for(m: dict, f: str) -> str:
        if f == "session":
            sid = session_id.get(m["match_guid"], -1)
            # Label session by its earliest end time.
            return f"Session #{sid + 1}"
        return GROUP_EXTRACTORS[f][1](m)

    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for m in matches:
        buckets[key_for(m)].append(m)

    rows: list[dict] = []
    for key, ms in buckets.items():
        wins = sum(1 for m in ms if m.get("won") is True)
        losses = sum(1 for m in ms if m.get("won") is False)
        unknown = sum(1 for m in ms if m.get("won") is None)
        decided = wins + losses
        win_pct = (wins / decided * 100) if decided else None
        diffs = [_score_diff(m) for m in ms]
        avg_diff = sum(diffs) / len(diffs) if diffs else 0.0
        ends = [_parse_dt(m["ended_at"]) for m in ms if _parse_dt(m["ended_at"])]
        last_played = max(ends) if ends else None

        sample = ms[0]
        labels = [label_for(sample, f) for f in group_fields]

        rows.append(
            {
                "_key": key,
                "labels": labels,
                "games": len(ms),
                "wins": wins,
                "losses": losses,
                "unknown": unknown,
                "win_pct": win_pct,
                "avg_diff": avg_diff,
                "last_played": last_played,
            }
        )
    return rows


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------
ORDER_FIELDS: list[tuple[str, str]] = [
    ("games", "Games"),
    ("wins", "Wins"),
    ("losses", "Losses"),
    ("win_pct", "Win %"),
    ("avg_diff", "Avg score Δ"),
    ("last_played", "Last played"),
]


def _order_value(row: dict, field: str) -> Any:
    v = row.get(field)
    if v is None:
        # Push None to the end regardless of direction.
        return (1, 0)
    if isinstance(v, datetime):
        return (0, v.timestamp())
    return (0, v)


def order_rows(rows: list[dict], spec: list[tuple[str, str]]) -> list[dict]:
    if not spec:
        return rows
    out = list(rows)
    for field, direction in reversed(spec):
        out.sort(
            key=lambda r: _order_value(r, field),
            reverse=(direction == "desc"),
        )
    return out


# --------------------------------------------------------------------------
# Widget
# --------------------------------------------------------------------------
class AdvancedHistoryTab(QWidget):
    def __init__(self, store: HistoryStore) -> None:
        super().__init__()
        self._store = store
        self._matches: list[dict] = []
        self._dirty = True

        root = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        # Left side: filter / group / order panel inside a scroll area.
        left_holder = QScrollArea()
        left_holder.setWidgetResizable(True)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_holder.setWidget(left)
        splitter.addWidget(left_holder)

        self._build_filters_panel(left_layout)
        self._build_group_order_panel(left_layout)

        btn_row = QHBoxLayout()
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply_clicked)
        reset_btn = QPushButton("Reset")
        reset_btn.clicked.connect(self._reset_clicked)
        btn_row.addWidget(apply_btn)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch(1)
        left_layout.addLayout(btn_row)
        left_layout.addStretch(1)

        # Right side: results table.
        self._table = QTableWidget(0, 0)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        splitter.addWidget(self._table)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 760])

        self._load_state()

    # ---------------- panel construction ----------------
    def _build_filters_panel(self, parent: QVBoxLayout) -> None:
        box = QGroupBox("Filters")
        grid = QGridLayout(box)

        self._playlist_inc = self._make_list()
        self._playlist_exc = self._make_list()
        self._teammate_inc = self._make_list()
        self._teammate_exc = self._make_list()
        self._opponent_inc = self._make_list()
        self._opponent_exc = self._make_list()

        self._result_combo = QComboBox()
        for k, lbl in (("any", "Any"), ("win", "Wins only"), ("loss", "Losses only")):
            self._result_combo.addItem(lbl, k)

        self._ot_combo = QComboBox()
        for k, lbl in (("any", "Any"), ("ot", "OT only"), ("no_ot", "Non-OT only")):
            self._ot_combo.addItem(lbl, k)

        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setSpecialValueText(" ")
        self._date_from.setMinimumDate(QDate(2000, 1, 1))
        self._date_from.setDate(self._date_from.minimumDate())

        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setSpecialValueText(" ")
        self._date_to.setMinimumDate(QDate(2000, 1, 1))
        self._date_to.setDate(self._date_to.minimumDate())

        clear_dates = QPushButton("Clear dates")
        clear_dates.clicked.connect(self._clear_dates)

        self._diff_min = QSpinBox()
        self._diff_min.setRange(-99, 0)
        self._diff_min.setValue(-99)
        self._diff_max = QSpinBox()
        self._diff_max.setRange(0, 99)
        self._diff_max.setValue(99)

        self._min_session = QSpinBox()
        self._min_session.setRange(1, 99)
        self._min_session.setValue(1)

        # Layout
        r = 0
        grid.addWidget(QLabel("Playlist include"), r, 0)
        grid.addWidget(QLabel("Playlist exclude"), r, 1)
        r += 1
        grid.addWidget(self._playlist_inc, r, 0)
        grid.addWidget(self._playlist_exc, r, 1)
        r += 1
        grid.addWidget(QLabel("Teammate include"), r, 0)
        grid.addWidget(QLabel("Teammate exclude"), r, 1)
        r += 1
        grid.addWidget(self._teammate_inc, r, 0)
        grid.addWidget(self._teammate_exc, r, 1)
        r += 1
        grid.addWidget(QLabel("Opponent include"), r, 0)
        grid.addWidget(QLabel("Opponent exclude"), r, 1)
        r += 1
        grid.addWidget(self._opponent_inc, r, 0)
        grid.addWidget(self._opponent_exc, r, 1)
        r += 1
        grid.addWidget(QLabel("Result"), r, 0)
        grid.addWidget(self._result_combo, r, 1)
        r += 1
        grid.addWidget(QLabel("Overtime"), r, 0)
        grid.addWidget(self._ot_combo, r, 1)
        r += 1
        grid.addWidget(QLabel("Date from"), r, 0)
        grid.addWidget(self._date_from, r, 1)
        r += 1
        grid.addWidget(QLabel("Date to"), r, 0)
        grid.addWidget(self._date_to, r, 1)
        r += 1
        grid.addWidget(clear_dates, r, 1)
        r += 1
        grid.addWidget(QLabel("Score Δ min"), r, 0)
        grid.addWidget(self._diff_min, r, 1)
        r += 1
        grid.addWidget(QLabel("Score Δ max"), r, 0)
        grid.addWidget(self._diff_max, r, 1)
        r += 1
        grid.addWidget(QLabel("Session size ≥"), r, 0)
        grid.addWidget(self._min_session, r, 1)

        parent.addWidget(box)

    def _make_list(self) -> QListWidget:
        w = QListWidget()
        w.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        w.setMaximumHeight(110)
        return w

    def _build_group_order_panel(self, parent: QVBoxLayout) -> None:
        box = QGroupBox("Group && order")
        v = QVBoxLayout(box)

        v.addWidget(QLabel("Group by (check in desired order — top first):"))
        self._group_list = QListWidget()
        self._group_list.setMaximumHeight(170)
        self._group_list.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove
        )
        for fid, label in GROUP_FIELDS:
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, fid)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            self._group_list.addItem(it)
        v.addWidget(self._group_list)

        v.addWidget(QLabel("Order by:"))
        self._order_rows: list[tuple[QComboBox, QComboBox]] = []
        self._order_holder = QVBoxLayout()
        v.addLayout(self._order_holder)

        order_btns = QHBoxLayout()
        add_o = QPushButton("+ order key")
        add_o.clicked.connect(lambda: self._add_order_row())
        clear_o = QPushButton("Clear")
        clear_o.clicked.connect(self._clear_order_rows)
        order_btns.addWidget(add_o)
        order_btns.addWidget(clear_o)
        order_btns.addStretch(1)
        v.addLayout(order_btns)

        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("Row limit:"))
        self._row_limit = QSpinBox()
        self._row_limit.setRange(1, 10000)
        self._row_limit.setValue(500)
        limit_row.addWidget(self._row_limit)
        limit_row.addStretch(1)
        v.addLayout(limit_row)

        parent.addWidget(box)

    def _add_order_row(self, field: str | None = None, direction: str = "desc") -> None:
        field_box = QComboBox()
        for fid, label in ORDER_FIELDS:
            field_box.addItem(label, fid)
        if field is not None:
            i = field_box.findData(field)
            if i >= 0:
                field_box.setCurrentIndex(i)
        dir_box = QComboBox()
        dir_box.addItem("desc", "desc")
        dir_box.addItem("asc", "asc")
        if direction == "asc":
            dir_box.setCurrentIndex(1)

        row = QHBoxLayout()
        row.addWidget(field_box, 1)
        row.addWidget(dir_box)
        rm = QPushButton("✕")
        rm.setMaximumWidth(28)
        row.addWidget(rm)
        container = QWidget()
        container.setLayout(row)
        self._order_holder.addWidget(container)
        entry = (field_box, dir_box)
        self._order_rows.append(entry)

        def remove() -> None:
            if entry in self._order_rows:
                self._order_rows.remove(entry)
            container.setParent(None)
            container.deleteLater()

        rm.clicked.connect(remove)

    def _clear_order_rows(self) -> None:
        for fb, db in list(self._order_rows):
            parent = fb.parentWidget()
            if parent is not None:
                parent.setParent(None)
                parent.deleteLater()
        self._order_rows.clear()

    def _clear_dates(self) -> None:
        self._date_from.setDate(self._date_from.minimumDate())
        self._date_to.setDate(self._date_to.minimumDate())

    # ---------------- data + UI plumbing ----------------
    def refresh_dataset(self) -> None:
        self._dirty = False
        self._matches = self._store.all_matches()
        self._populate_filter_options()
        self._apply_pending_selections()
        self._render()

    def _apply_pending_selections(self) -> None:
        pending = getattr(self, "_pending_selections", None)
        if not pending:
            return
        self._select_values(self._playlist_inc, pending["playlist_include"])
        self._select_values(self._playlist_exc, pending["playlist_exclude"])
        self._select_values(self._teammate_inc, pending["teammate_include"])
        self._select_values(self._teammate_exc, pending["teammate_exclude"])
        self._select_values(self._opponent_inc, pending["opponent_include"])
        self._select_values(self._opponent_exc, pending["opponent_exclude"])
        self._pending_selections = None

    def _populate_filter_options(self) -> None:
        playlists = sorted({m["playlist"] for m in self._matches})

        teammates: dict[str, str] = {}
        opponents: dict[str, str] = {}
        for m in self._matches:
            for p in _teammates(m):
                teammates.setdefault(p["platform_id"], p["name"])
            for p in _opponents(m):
                opponents.setdefault(p["platform_id"], p["name"])

        self._fill_list(self._playlist_inc, [(p, p) for p in playlists])
        self._fill_list(self._playlist_exc, [(p, p) for p in playlists])

        tm_items = sorted(teammates.items(), key=lambda kv: kv[1].lower())
        self._fill_list(
            self._teammate_inc,
            [(pid, f"{name} ({pid.split('|')[0]})") for pid, name in tm_items],
        )
        self._fill_list(
            self._teammate_exc,
            [(pid, f"{name} ({pid.split('|')[0]})") for pid, name in tm_items],
        )

        op_items = sorted(opponents.items(), key=lambda kv: kv[1].lower())
        self._fill_list(
            self._opponent_inc,
            [(pid, f"{name} ({pid.split('|')[0]})") for pid, name in op_items],
        )
        self._fill_list(
            self._opponent_exc,
            [(pid, f"{name} ({pid.split('|')[0]})") for pid, name in op_items],
        )

    def _fill_list(self, w: QListWidget, items: list[tuple[str, str]]) -> None:
        prev_selected = {
            w.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(w.count())
            if w.item(i).isSelected()
        }
        w.blockSignals(True)
        w.clear()
        for value, label in items:
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, value)
            w.addItem(it)
            if value in prev_selected:
                it.setSelected(True)
        w.blockSignals(False)

    def _selected_values(self, w: QListWidget) -> list[str]:
        return [
            w.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(w.count())
            if w.item(i).isSelected()
        ]

    def _select_values(self, w: QListWidget, values: list[str]) -> None:
        vs = set(values or [])
        for i in range(w.count()):
            it = w.item(i)
            it.setSelected(it.data(Qt.ItemDataRole.UserRole) in vs)

    def _collect_filters(self) -> dict:
        df = self._date_from.date()
        dt_from = (
            datetime.combine(df.toPyDate(), datetime.min.time())
            if df != self._date_from.minimumDate()
            else None
        )
        dt = self._date_to.date()
        dt_to = (
            datetime.combine(dt.toPyDate(), datetime.max.time())
            if dt != self._date_to.minimumDate()
            else None
        )
        diff_min = self._diff_min.value() if self._diff_min.value() != -99 else None
        diff_max = self._diff_max.value() if self._diff_max.value() != 99 else None
        return {
            "playlist_include": self._selected_values(self._playlist_inc),
            "playlist_exclude": self._selected_values(self._playlist_exc),
            "teammate_include": self._selected_values(self._teammate_inc),
            "teammate_exclude": self._selected_values(self._teammate_exc),
            "opponent_include": self._selected_values(self._opponent_inc),
            "opponent_exclude": self._selected_values(self._opponent_exc),
            "result": self._result_combo.currentData(),
            "overtime": self._ot_combo.currentData(),
            "date_from": dt_from.isoformat() if dt_from else None,
            "date_to": dt_to.isoformat() if dt_to else None,
            "diff_min": diff_min,
            "diff_max": diff_max,
            "min_session_size": self._min_session.value(),
        }

    def _collect_group_fields(self) -> list[str]:
        out: list[str] = []
        for i in range(self._group_list.count()):
            it = self._group_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def _collect_order_spec(self) -> list[tuple[str, str]]:
        return [(fb.currentData(), db.currentData()) for fb, db in self._order_rows]

    # ---------------- actions ----------------
    def _apply_clicked(self) -> None:
        self._render()
        self._save_state()

    def _reset_clicked(self) -> None:
        for w in (
            self._playlist_inc,
            self._playlist_exc,
            self._teammate_inc,
            self._teammate_exc,
            self._opponent_inc,
            self._opponent_exc,
        ):
            w.clearSelection()
        self._result_combo.setCurrentIndex(0)
        self._ot_combo.setCurrentIndex(0)
        self._clear_dates()
        self._diff_min.setValue(-99)
        self._diff_max.setValue(99)
        self._min_session.setValue(1)
        for i in range(self._group_list.count()):
            self._group_list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self._clear_order_rows()
        self._row_limit.setValue(500)
        self._render()
        self._save_state()

    def _render(self) -> None:
        gap = load_session_gap_minutes()
        filters = self._collect_filters()
        filtered = apply_filters(self._matches, filters, gap)
        groups = self._collect_group_fields()
        spec = self._collect_order_spec()
        limit = self._row_limit.value()

        if groups:
            rows = group_and_aggregate(filtered, groups, gap)
            rows = order_rows(rows, spec)
            self._render_grouped(groups, rows[:limit])
        else:
            # Flat per-match view, ordered by ended_at desc by default.
            sorted_matches = sorted(
                filtered,
                key=lambda m: _parse_dt(m["ended_at"]) or datetime.min,
                reverse=True,
            )
            self._render_flat(sorted_matches[:limit])

    def _render_grouped(self, group_fields: list[str], rows: list[dict]) -> None:
        labels_for = {fid: lbl for fid, lbl in GROUP_FIELDS}
        headers = [labels_for[f] for f in group_fields] + [
            "Games", "W", "L", "Win %", "Avg Δ", "Last played",
        ]
        self._table.clear()
        self._table.setColumnCount(len(headers))
        self._table.setHorizontalHeaderLabels(headers)
        self._table.setRowCount(len(rows))
        for r_idx, row in enumerate(rows):
            cols: list[str] = list(row["labels"])
            cols.append(str(row["games"]))
            cols.append(str(row["wins"]))
            cols.append(str(row["losses"]))
            cols.append(
                "—" if row["win_pct"] is None else f"{row['win_pct']:.0f}%"
            )
            cols.append(f"{row['avg_diff']:+.1f}")
            cols.append(
                _format_local(row["last_played"]) if row["last_played"] else ""
            )
            for c_idx, txt in enumerate(cols):
                it = QTableWidgetItem(txt)
                if c_idx >= len(group_fields):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table.setItem(r_idx, c_idx, it)
        self._stretch_text_columns(len(group_fields))

    def _render_flat(self, matches: list[dict]) -> None:
        headers = [
            "Ended", "Playlist", "Result", "Score", "Δ", "OT",
            "Teammates", "Opponents",
        ]
        self._table.clear()
        self._table.setColumnCount(len(headers))
        self._table.setHorizontalHeaderLabels(headers)
        self._table.setRowCount(len(matches))
        for r_idx, m in enumerate(matches):
            mt = _my_team(m)
            t0, t1 = m.get("team0_score", 0), m.get("team1_score", 0)
            if mt == 1:
                score = f"{t1}–{t0}"
            else:
                score = f"{t0}–{t1}"
            tmates = ", ".join(p["name"] for p in _teammates(m)) or "(solo)"
            opps = ", ".join(p["name"] for p in _opponents(m)) or ""
            cols = [
                _format_local(m["ended_at"]),
                m["playlist"],
                _result_label(m),
                score,
                f"{_score_diff(m):+d}",
                "OT" if m.get("overtime") else "",
                tmates,
                opps,
            ]
            for c_idx, txt in enumerate(cols):
                it = QTableWidgetItem(txt)
                if c_idx in (2, 3, 4, 5):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table.setItem(r_idx, c_idx, it)
        # Stretch teammates + opponents columns.
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)

    def _stretch_text_columns(self, group_count: int) -> None:
        header = self._table.horizontalHeader()
        # Stretch the last group column (typically text-heavy: teammate set).
        if group_count > 0:
            header.setSectionResizeMode(
                group_count - 1, QHeaderView.ResizeMode.Stretch
            )

    # ---------------- persistence ----------------
    def _state_dict(self) -> dict:
        return {
            "filters": self._collect_filters(),
            "groups": self._collect_group_fields(),
            "order": self._collect_order_spec(),
            "row_limit": self._row_limit.value(),
        }

    def _save_state(self) -> None:
        try:
            save_advanced_view(self._state_dict())
        except Exception:
            pass

    def _load_state(self) -> None:
        st = load_advanced_view()
        if not st:
            return
        f = st.get("filters") or {}
        # Filter list selections are restored after dataset load.
        self._pending_selections = {
            "playlist_include": f.get("playlist_include") or [],
            "playlist_exclude": f.get("playlist_exclude") or [],
            "teammate_include": f.get("teammate_include") or [],
            "teammate_exclude": f.get("teammate_exclude") or [],
            "opponent_include": f.get("opponent_include") or [],
            "opponent_exclude": f.get("opponent_exclude") or [],
        }
        rk = f.get("result") or "any"
        i = self._result_combo.findData(rk)
        if i >= 0:
            self._result_combo.setCurrentIndex(i)
        ok = f.get("overtime") or "any"
        i = self._ot_combo.findData(ok)
        if i >= 0:
            self._ot_combo.setCurrentIndex(i)
        df = _parse_dt(f.get("date_from"))
        if df:
            self._date_from.setDate(QDate(df.year, df.month, df.day))
        dt = _parse_dt(f.get("date_to"))
        if dt:
            self._date_to.setDate(QDate(dt.year, dt.month, dt.day))
        if f.get("diff_min") is not None:
            self._diff_min.setValue(int(f["diff_min"]))
        if f.get("diff_max") is not None:
            self._diff_max.setValue(int(f["diff_max"]))
        if f.get("min_session_size"):
            self._min_session.setValue(int(f["min_session_size"]))

        groups = set(st.get("groups") or [])
        for i in range(self._group_list.count()):
            it = self._group_list.item(i)
            it.setCheckState(
                Qt.CheckState.Checked
                if it.data(Qt.ItemDataRole.UserRole) in groups
                else Qt.CheckState.Unchecked
            )
        for entry in st.get("order") or []:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                self._add_order_row(entry[0], entry[1])
        if st.get("row_limit"):
            self._row_limit.setValue(int(st["row_limit"]))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self.refresh_dataset()
