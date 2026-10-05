"""
ui_mission.py — Settings → MISSION CONTROL (Phase 7).

The dashboard features the user supplied (the "dashboard features" folder):
a command uplink with slash-command buttons (/background, /personality,
/kanban …) and a MISSION CONTROL deck with tabs — Overview, Agents, Tasks,
Schedule, Content, Logs — plus two JARVIS-specific tabs: Permissions (every
"Allow always", revocable) and Learning (what JARVIS has learned about how
you like things, forgettable).

The panel only DISPLAYS core/mission.snapshot() and calls back into main.py;
every action it triggers goes through the same permission-checked paths as a
voice command. Colours come from ui.C, so it follows Friendly blue / Serious
red with the rest of the interface.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)

import ui as _ui
from ui import C

TABS = ("Overview", "Agents", "Tasks", "Schedule", "Content", "Logs", "Permissions", "Learning")
COMMANDS = (("/background", "RUN IN BACKGROUND"), ("/personality", "SET PERSONA"), ("/kanban", "WORK QUEUE"),
            ("/watch", "WATCH SCREEN"), ("/draw", "DRAW IN PAINT"), ("/reference", "KEEP REFERENCE"),
            ("/pocket", "PHONE LINE"))


def _mono(px: int, bold: bool = False) -> QFont:
    f = QFont("Consolas", px)
    if bold:
        f.setWeight(QFont.Weight.Bold)
    return f


def _sans(px: int, bold: bool = False) -> QFont:
    f = QFont("Segoe UI", px)
    if bold:
        f.setWeight(QFont.Weight.DemiBold)
    return f


def _lbl(text: str, font: QFont, color: str, wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    lb.setFont(font)
    lb.setWordWrap(wrap)
    lb.setStyleSheet(f"color: {color}; background: transparent;")
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lb


def _card(parent_layout, title: str = "") -> QVBoxLayout:
    box = QFrame()
    box.setStyleSheet(f"QFrame {{ background: rgba(255,255,255,8); border: 1px solid {C.BORDER}; "
                      f"border-radius: 10px; }} QLabel {{ border: none; }}")
    lay = QVBoxLayout(box)
    lay.setContentsMargins(14, 10, 14, 12)
    lay.setSpacing(4)
    if title:
        lay.addWidget(_lbl(title.upper(), _mono(8), C.TEXT_MED))
    parent_layout.addWidget(box)
    return lay


class _Bars(QWidget):
    """Seven little bars — actions per day for an agent, oldest → today."""

    def __init__(self, values: list[int], parent=None):
        super().__init__(parent)
        self._v = list(values or [])[-7:]
        self.setFixedHeight(42)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = max(1, len(self._v))
        top = max([1] + self._v)
        w = self.width() / n
        col = QColor(C.PRI)
        for i, v in enumerate(self._v):
            h = 4 + (self.height() - 6) * (v / top)
            r = QRectF(i * w + 3, self.height() - h, w - 6, h)
            c = QColor(col)
            c.setAlpha(110 + int(140 * (v / top)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(r, 3, 3)
        p.end()


class MissionControlOverlay(_ui._HudOverlay):
    _OW, _OH = 900, 640

    def __init__(self, snapshot_fn: Callable[[], dict], *, on_command: Callable[[str, str], str] | None = None,
                 on_revoke: Callable[[str], bool] | None = None,
                 on_forget_learning: Callable[[str], bool] | None = None,
                 on_learning_pause: Callable[[bool], str] | None = None,
                 on_forget_reference: Callable[[str], bool] | None = None, parent=None):
        super().__init__(parent)
        self._snap_fn = snapshot_fn
        self._on_command = on_command
        self._on_revoke = on_revoke
        self._on_forget_learning = on_forget_learning
        self._on_learning_pause = on_learning_pause
        self._on_forget_reference = on_forget_reference
        self._prefix = ""
        self._tab = "Overview"
        self._snap: dict = {}
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            MissionControlOverlay {{ background: rgba(4, 8, 14, 247); border: 1px solid {C.BORDER_B};
                                     border-radius: 14px; }}
            QScrollArea {{ background: transparent; border: none; }}
            QScrollArea > QWidget > QWidget {{ background: transparent; }}
        """)
        self.setFixedSize(self._OW, self._OH)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(10)

        # header
        hdr = QHBoxLayout()
        hdr.addWidget(_lbl("MISSION CONTROL", _mono(12, True), C.PRI))
        hdr.addStretch(1)
        self._synced = _lbl("", _mono(8), C.TEXT_DIM)
        hdr.addWidget(self._synced)
        close = QPushButton("✕")
        close.setFixedSize(26, 26)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"QPushButton {{ color: {C.TEXT_MED}; background: transparent; border: 1px solid "
                            f"{C.BORDER}; border-radius: 13px; }} QPushButton:hover {{ color: #ffffff; }}")
        close.clicked.connect(self.hide)
        hdr.addWidget(close)
        root.addLayout(hdr)

        # uplink: slash-command buttons + input
        up = QHBoxLayout()
        up.setSpacing(6)
        self._cmd_btns = {}
        for cmd, sub in COMMANDS:
            b = QPushButton(f"{cmd}\n{sub}")
            b.setFont(_mono(7))
            b.setFixedHeight(38)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, c=cmd: self._set_prefix(c))
            self._cmd_btns[cmd] = b
            up.addWidget(b)
        root.addLayout(up)
        row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type here. Press a command button first to prefix a slash command.")
        self._input.setFont(_mono(10))
        self._input.setStyleSheet(f"QLineEdit {{ color: {C.TEXT}; background: rgba(255,255,255,10); "
                                  f"border: 1px solid {C.BORDER}; border-radius: 8px; padding: 8px 10px; }} "
                                  f"QLineEdit:focus {{ border-color: {C.PRI}; }}")
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input, 1)
        send = QPushButton("SEND")
        send.setFont(_mono(9, True))
        send.setFixedHeight(36)
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"QPushButton {{ color: {C.PRI}; background: transparent; border: 1px solid {C.PRI}; "
                           f"border-radius: 8px; padding: 0 16px; }} QPushButton:hover {{ background: rgba(255,255,255,18); }}")
        send.clicked.connect(self._send)
        row.addWidget(send)
        root.addLayout(row)
        self._status = _lbl("", _mono(8), C.TEXT_MED, wrap=True)
        root.addWidget(self._status)

        # tabs
        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self._tab_btns = {}
        for t in TABS:
            b = QPushButton(t)
            b.setFont(_sans(9))
            b.setFixedHeight(30)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, name=t: self._show_tab(name))
            self._tab_btns[t] = b
            tabs.addWidget(b)
        tabs.addStretch(1)
        root.addLayout(tabs)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        root.addWidget(self._scroll, 1)

        self._style_buttons()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(3000)
        self.refresh()

    # ── chrome ─────────────────────────────────────────────────────────────
    def _style_buttons(self):
        for t, b in self._tab_btns.items():
            on = t == self._tab
            b.setStyleSheet(f"QPushButton {{ color: {'#ffffff' if on else C.TEXT_MED}; background: "
                            f"{'rgba(255,255,255,14)' if on else 'transparent'}; border: 1px solid "
                            f"{C.PRI if on else C.BORDER}; border-radius: 15px; padding: 0 14px; }}"
                            f"QPushButton:hover {{ color: #ffffff; }}")
        for c, b in self._cmd_btns.items():
            on = c == self._prefix
            b.setChecked(on)
            b.setStyleSheet(f"QPushButton {{ color: {C.WHITE if on else C.PRI}; background: rgba(255,255,255,"
                            f"{16 if on else 6}); border: 1px solid {C.PRI if on else C.BORDER}; border-radius: 8px;"
                            f" padding: 2px 8px; text-align: left; }}")

    def _set_prefix(self, cmd: str):
        self._prefix = "" if self._prefix == cmd else cmd
        if self._prefix == "/kanban":
            self._prefix = ""
            self._show_tab("Tasks")
        self._style_buttons()
        self._input.setFocus()

    def _send(self):
        text = self._input.text().strip()
        cmd = self._prefix
        if not text and cmd not in ("/watch", "/draw", "/reference", "/pocket"):
            return
        self._input.clear()
        self._prefix = ""
        self._style_buttons()
        if self._on_command is None:
            return
        try:
            msg = self._on_command(cmd, text) or "Sent."
        except Exception as e:
            msg = f"Couldn't send: {e}"
        self._status.setText(str(msg)[:220])
        QTimer.singleShot(400, self.refresh)

    def _show_tab(self, name: str):
        self._tab = name
        self._style_buttons()
        self._render()

    # ── data ───────────────────────────────────────────────────────────────
    def refresh(self):
        if not self.isVisible() and self._snap:
            return
        try:
            self._snap = self._snap_fn() or {}
        except Exception as e:
            self._snap = {"error": str(e)}
        self._synced.setText(f"SYNCED {self._snap.get('at', '')}")
        self._render()

    def _render(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(10)
        try:
            getattr(self, "_page_" + self._tab.lower())(lay, self._snap)
        except Exception as e:
            lay.addWidget(_lbl(f"(this tab could not be drawn: {e})", _mono(9), C.RED, wrap=True))
        lay.addStretch(1)
        old = self._scroll.takeWidget()
        self._scroll.setWidget(page)
        if old is not None:
            old.hide()
            old.deleteLater()

    # ── pages ──────────────────────────────────────────────────────────────
    def _stat(self, grid: QGridLayout, col: int, row: int, title: str, value: str, sub: str = ""):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        lay = _card(v, title)
        lay.addWidget(_lbl(value, _sans(20), C.PRI))
        if sub:
            lay.addWidget(_lbl(sub, _sans(8), C.TEXT_MED, wrap=True))
        grid.addWidget(w, row, col)

    def _page_overview(self, lay, s):
        ov = s.get("overview") or {}
        g = QGridLayout()
        g.setSpacing(10)
        lat = ov.get("latency") or {}
        heard = lat.get("median_heard_s")
        _b = (ov.get("brain") or "?").split(" · ")
        self._stat(g, 0, 0, "Brain", _b[0].split("/")[-1][:24], " · ".join(_b[1:]) or "")
        self._stat(g, 1, 0, "Voice latency", f"{heard:.2f} s" if heard else "—",
                   f"median speech-end → heard · {lat.get('turns', 0)} turns · buffer {lat.get('device_buffer_ms', 0)} ms")
        self._stat(g, 2, 0, "Events today", str(ov.get("events_today", 0)), f"{ov.get('events_logged', 0)} in the log")
        self._stat(g, 3, 0, "Open tasks", str(ov.get("open_tasks", 0)))
        self._stat(g, 0, 1, "Voice model", (ov.get("voice_model") or "?")[:28])
        self._stat(g, 1, 1, "Always allowed", str(ov.get("trusted", 0)), "revoke in Permissions")
        self._stat(g, 2, 1, "References", str(ov.get("references", 0)))
        self._stat(g, 3, 1, "Learned", str(ov.get("learned", 0)),
                   "learning paused" if ov.get("learning_paused") else "preferences + habits")
        lay.addLayout(g)

    def _page_agents(self, lay, s):
        g = QGridLayout()
        g.setSpacing(10)
        for i, a in enumerate(s.get("agents") or []):
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            c = _card(v)
            c.addWidget(_lbl(a.get("name", ""), _mono(11, True), C.WHITE))
            c.addWidget(_lbl(f"{a.get('total', 0)} logged actions · {a.get('today', 0)} today"
                             if a.get("total") else "Ready lane — no logged action yet", _sans(8), C.TEXT_MED))
            c.addWidget(_lbl(a.get("description", ""), _sans(8), C.TEXT_DIM, wrap=True))
            c.addWidget(_Bars(a.get("bars") or []))
            g.addWidget(w, i // 4, i % 4)
        lay.addLayout(g)

    def _page_tasks(self, lay, s):
        lanes = s.get("tasks") or {}
        row = QHBoxLayout()
        row.setSpacing(8)
        for name in ("queued", "running", "waiting", "done", "failed"):
            col = QWidget()
            v = QVBoxLayout(col)
            v.setContentsMargins(0, 0, 0, 0)
            c = _card(v, f"{name} · {len(lanes.get(name) or [])}")
            for t in (lanes.get(name) or [])[:8]:
                c.addWidget(_lbl(f"{t.get('id')} · {t.get('request')}", _sans(8), C.TEXT, wrap=True))
            if not lanes.get(name):
                c.addWidget(_lbl("—", _sans(8), C.TEXT_DIM))
            row.addWidget(col, 1)
        lay.addLayout(row)

    def _page_schedule(self, lay, s):
        sc = s.get("schedule") or {}
        c = _card(lay, "Saved routines (workflows)")
        for w in sc.get("workflows") or []:
            c.addWidget(_lbl(f"{w.get('name')} — {w.get('steps')} steps · run {w.get('runs', 0)}×", _sans(9), C.TEXT))
        if not sc.get("workflows"):
            c.addWidget(_lbl("None yet. Say \"save that as a routine\" after JARVIS suggests one, or create one "
                             "with /background.", _sans(9), C.TEXT_MED, wrap=True))
        h = _card(lay, "Learned habits (when you usually do things)")
        for x in sc.get("habits") or []:
            h.addWidget(_lbl(f"{x.get('text')}  ({x.get('count')}× over {x.get('days')} days)", _sans(9), C.TEXT))
        if not sc.get("habits"):
            h.addWidget(_lbl("Nothing yet — a habit needs 3 occurrences on 2 different days.", _sans(9),
                             C.TEXT_DIM, wrap=True))
        r = _card(lay, "Reminders")
        r.addWidget(_lbl("Reminders run as Windows scheduled tasks (they fire even with JARVIS closed). "
                         "Say \"remind me …\".", _sans(9), C.TEXT_DIM, wrap=True))

    def _page_content(self, lay, s):
        refs = s.get("references") or []
        c = _card(lay, f"References · {len(refs)}")
        for r in refs:
            line = QHBoxLayout()
            line.addWidget(_lbl(f"{r.get('title')}  ({r.get('kind')}{'' if r.get('active') else ', archived'})",
                                _sans(9), C.TEXT), 1)
            if self._on_forget_reference:
                line.addWidget(self._small_btn("FORGET", lambda _=False, i=r.get("id"): self._act(
                    self._on_forget_reference, i)))
            holder = QWidget()
            holder.setLayout(line)
            c.addWidget(holder)
        if not refs:
            c.addWidget(_lbl("Say \"keep this as a reference\" while something is on screen.", _sans(9),
                             C.TEXT_DIM))
        files = s.get("content") or []
        f = _card(lay, f"Made by JARVIS · {len(files)} recent files")
        for it in files:
            b = QPushButton(f"{it.get('name')}   ·   {it.get('folder')}   ·   {it.get('bytes', 0) / 1024:.1f} KB")
            b.setFont(_sans(9))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(f"Open {it.get('path')}")
            b.setStyleSheet(f"QPushButton {{ color: {C.TEXT}; background: transparent; border: none; "
                            f"text-align: left; padding: 3px 0; }} QPushButton:hover {{ color: {C.PRI}; }}")
            b.clicked.connect(lambda _=False, p=it.get("path"): self._open(p))
            f.addWidget(b)

    def _page_logs(self, lay, s):
        c = _card(lay, "Audit log — newest first (redacted)")
        for e in (s.get("logs") or [])[:40]:
            c.addWidget(_lbl(f"{e.get('ts')}  {str(e.get('event', '')).upper():<14} {e.get('text', '')}",
                             _mono(8), C.PRI if e.get("event") in ("permission", "learned") else C.TEXT, wrap=True))

    def _page_permissions(self, lay, s):
        tr = (s.get("permissions") or {}).get("trusted") or []
        c = _card(lay, "Always allowed (from ALLOW ALWAYS on a confirmation)")
        c.addWidget(_lbl("These run without asking. Critical actions (money, security, credentials), power "
                         "actions and plugin installs can never be here.", _sans(8), C.TEXT_DIM, wrap=True))
        for k in tr:
            line = QHBoxLayout()
            line.addWidget(_lbl(k, _mono(10), C.TEXT), 1)
            line.addWidget(self._small_btn("REVOKE", lambda _=False, key=k: self._act(self._on_revoke, key)))
            holder = QWidget()
            holder.setLayout(line)
            c.addWidget(holder)
        if not tr:
            c.addWidget(_lbl("Nothing is always-allowed. Every protected action asks first.", _sans(9), C.TEXT_MED))

    def _page_learning(self, lay, s):
        L = s.get("learning") or {}
        top = QHBoxLayout()
        paused = bool(L.get("paused"))
        top.addWidget(_lbl("Learning is PAUSED." if paused else "JARVIS learns from what you say and do — "
                           "nothing from web pages or documents, never secrets.", _sans(9), C.TEXT_MED, wrap=True), 1)
        if self._on_learning_pause:
            top.addWidget(self._small_btn("RESUME" if paused else "PAUSE",
                                          lambda _=False, p=paused: self._act(self._on_learning_pause, not p)))
        lay.addLayout(top)
        c = _card(lay, "How you like things")
        for p in L.get("preferences") or []:
            line = QHBoxLayout()
            line.addWidget(_lbl(f'"{p.get("text")}"  ·  {p.get("kind")}{"  ×" + str(p.get("count")) if p.get("count", 1) > 1 else ""}',
                                _sans(9), C.TEXT, wrap=True), 1)
            line.addWidget(self._small_btn("FORGET", lambda _=False, i=p.get("id"): self._act(
                self._on_forget_learning, i)))
            holder = QWidget()
            holder.setLayout(line)
            c.addWidget(holder)
        if not L.get("preferences"):
            c.addWidget(_lbl("Nothing yet. Tell JARVIS how you like things (\"always…\", \"next time…\", "
                             "\"I prefer…\") and it keeps it.", _sans(9), C.TEXT_DIM, wrap=True))
        h = _card(lay, "Habits")
        for x in L.get("habits") or []:
            line = QHBoxLayout()
            line.addWidget(_lbl(x.get("text", ""), _sans(9), C.TEXT), 1)
            line.addWidget(self._small_btn("FORGET", lambda _=False, a=x.get("action"): self._act(
                self._on_forget_learning, a)))
            holder = QWidget()
            holder.setLayout(line)
            h.addWidget(holder)
        if not L.get("habits"):
            h.addWidget(_lbl("No habits yet.", _sans(9), C.TEXT_DIM))

    # ── helpers ────────────────────────────────────────────────────────────
    def _small_btn(self, text: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setFont(_mono(8))
        b.setFixedHeight(24)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet(f"QPushButton {{ color: {C.TEXT_MED}; background: transparent; border: 1px solid "
                        f"{C.BORDER}; border-radius: 4px; padding: 0 10px; }} QPushButton:hover {{ color: {C.RED}; "
                        f"border-color: {C.RED}; }}")
        b.clicked.connect(slot)
        return b

    def _act(self, fn, arg):
        if fn is None:
            return
        try:
            res = fn(arg)
            self._status.setText(str(res) if isinstance(res, str) else ("Done." if res else "Nothing changed."))
        except Exception as e:
            self._status.setText(f"Failed: {e}")
        QTimer.singleShot(0, self.refresh)

    def _open(self, path: Optional[str]):
        try:
            if path and os.path.exists(path):
                os.startfile(path)                     # type: ignore[attr-defined]
        except Exception as e:
            self._status.setText(f"Couldn't open it: {e}")

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.refresh)
