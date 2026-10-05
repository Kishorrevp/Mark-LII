"""
tools/preview_ui.py — run the JARVIS interface WITHOUT the voice backend.

Useful for checking the look after a UI change: no Gemini session, no
microphone, no API calls (the weather panel may still fetch wttr.in).

    python tools/preview_ui.py                      # interactive, cycles states
    python tools/preview_ui.py --shot out.png --seconds 6 --size 1600x900
    python tools/preview_ui.py --mode friendly --state LISTENING --offscreen --shot a.png

--shot    save a screenshot of the window after --seconds and exit
--offscreen  render without showing a window (Qt offscreen platform)
"""
import argparse
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

ap = argparse.ArgumentParser()
ap.add_argument("--shot", default="")
ap.add_argument("--seconds", type=float, default=5.0)
ap.add_argument("--size", default="")
ap.add_argument("--mode", default="")
ap.add_argument("--state", default="")
ap.add_argument("--offscreen", action="store_true")
ap.add_argument("--ambient", action="store_true")
ap.add_argument("--layout", default="")
ap.add_argument("--content", action="store_true", help="show the content panel")
ap.add_argument("--mission", action="store_true", help="open Mission Control (preview data)")
ap.add_argument("--confirm", action="store_true", help="show the ALLOW ONCE / ALWAYS / DENY banner")
ap.add_argument("--tab", default="", help="Mission Control tab to show")
args = ap.parse_args()
import ui  # noqa: E402
from PyQt6.QtCore import QTimer, Qt  # noqa: E402

if args.offscreen:
    # Render normally (real fonts, real DPI) but never put the window on screen.
    def _hidden(cls):
        orig = cls.show

        def show(self):
            self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            orig(self)
        cls.show = show
    _hidden(ui.MainWindow)
    try:
        import ui_command_center as _ucc
        _hidden(_ucc.AmbientOrb)
    except Exception:
        pass

if args.layout:
    _orig = ui._read_full_config
    ui._read_full_config = lambda: {**_orig(), "ui_layout": args.layout}

jui = ui.JarvisUI("face.png")
win = jui._win
win._ready = True
if args.size:
    w, h = (int(x) for x in args.size.lower().split("x"))
    win.showNormal()
    win.resize(w, h)
if args.mode:
    jui.set_personality_mode(args.mode)
for line in ["SYS: JARVIS online.", "You: find my resume PDF and move it to Documents",
             "JARVIS: On it, sir. Task accepted.", "SYS: Plugin discovery complete: 0 active.",
             "JARVIS: Done — the resume is in Documents, verified."]:
    jui.write_log(line)
if args.content:
    jui.show_content("Preview", "This is the content panel (search results, news, briefings).")

if args.mission:
    _days = [0, 2, 1, 4, 3, 5, 6]
    PREVIEW = {
        "at": time.strftime("%H:%M:%S"),
        "overview": {"brain": "gemini-flash-latest · google", "voice_model": "gemini-3.8-live",
                     "latency": {"median_heard_s": 0.82, "turns": 14, "device_buffer_ms": 40},
                     "events_today": 37, "events_logged": 214, "open_tasks": 1, "trusted": 1, "references": 2,
                     "learned": 3, "learning_paused": False},
        "agents": [{"name": n, "title": n, "today": t, "total": t * 6, "bars": _days,
                    "description": d} for n, t, d in (
            ("laptop", 6, "Applications, windows, keyboard/mouse control"),
            ("browser", 3, "Opening sites, tabs, downloads, YouTube"),
            ("file", 2, "Finding, moving, reading files"), ("research", 1, "Web research and reports"),
            ("ui", 1, "Designs and drawings"), ("coding", 0, "Code changes, verified"),
            ("vision", 4, "Looking at the screen"), ("memory", 2, "Remembering, references, learning"))],
        "tasks": {"queued": [], "running": [{"id": "T1A2B3C", "request": "make me a website for my project"}],
                  "waiting": [], "done": [{"id": "T0FFEE1", "request": "draw something you like"}], "failed": []},
        "schedule": {"workflows": [{"name": "morning setup", "steps": 3, "runs": 4}],
                     "habits": [{"text": "usually open vs code around 09:00 on weekdays", "count": 6, "days": 4}]},
        "content": [{"path": "", "name": "index.html", "folder": "Nova AI", "bytes": 18432},
                    {"path": "", "name": "cat 2026-10-01 2112.svg", "folder": "drawing cat", "bytes": 5210}],
        "logs": [{"ts": "21:12:04", "event": "permission", "text": "draw.paint · CONFIRM · approved"},
                 {"ts": "21:11:40", "event": "learned", "text": "next time put the report in Documents"}],
        "permissions": {"trusted": ["draw.paint"]},
        "learning": {"paused": False, "preferences": [
            {"id": "pref-1", "text": "next time put the report in Documents", "kind": "preference", "count": 2}],
            "habits": [{"action": "open vs code", "text": "usually open vs code around 09:00 on weekdays"}]},
        "references": [{"id": "ref-1", "title": "Linear landing page", "kind": "screen", "active": True}],
    }
    jui.set_mission_handlers(snapshot=lambda: PREVIEW, command=lambda c, t: f"(preview) {c} {t}",
                             revoke=lambda k: True, forget_learning=lambda i: True, learning_pause=lambda p: "ok",
                             forget_reference=lambda i: True)

    def _open_mc():
        win._open_mission_control()
        if args.tab and win._mission_overlay is not None:
            win._mission_overlay._show_tab(args.tab)
    QTimer.singleShot(700, _open_mc)
if args.confirm:
    QTimer.singleShot(700, lambda: jui.show_confirm("use the mouse to draw a cat in Paint",
                                                    "Task T1A2B3C · High Risk · This tool always asks first.", True))

t0 = time.time()
states = ["LISTENING", "THINKING", "SPEAKING", "EXECUTING", "LISTENING"]


def tick():
    el = time.time() - t0
    if args.state:
        st = args.state
    else:
        st = states[int(el / 3) % len(states)]
    jui.set_state(st)
    if st in ("SPEAKING", "LISTENING"):
        jui.set_audio_level(0.35 + 0.35 * abs(math.sin(el * 5.3)) * abs(math.sin(el * 1.7)))


timer = QTimer()
timer.timeout.connect(tick)
timer.start(40)

if args.ambient and getattr(win, "_cc", None) is not None:
    QTimer.singleShot(500, win._cc.enter_ambient)

if args.shot:
    def shoot():
        target = win
        if args.ambient and getattr(win, "_cc", None) is not None and win._cc._ambient is not None:
            target = win._cc._ambient
        target.grab().save(args.shot)
        print("saved", args.shot, target.width(), "x", target.height())
        jui._app.quit()
    QTimer.singleShot(int(args.seconds * 1000), shoot)

jui.root.mainloop()
