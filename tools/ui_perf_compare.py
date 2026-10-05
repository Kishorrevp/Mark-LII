"""tools/ui_perf_compare.py - CPU of the JARVIS window animating offscreen, for
the UI before Phase 8 (--base: the copies in _to_delete/perf_base) and now.
Same machine, same window size, same state sequence, 15 s each."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
BASE = "--base" in sys.argv
NOGAP = "--no-gap-timer" in sys.argv      # isolate: the backdrop's gap refresh
NOBEAT = "--no-heartbeat" in sys.argv     # isolate: the heartbeat
sys.path.insert(0, str(ROOT))
if BASE:
    sys.path.insert(0, str(ROOT / "_to_delete" / "perf_base"))
os.chdir(ROOT)


def main() -> None:
    import psutil
    import ui
    from PyQt6.QtCore import QEventLoop
    jui = ui.JarvisUI("face.png")
    app, win = jui._app, jui._win
    win.showNormal()
    win.resize(1440, 900)

    def pump(sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 16)
            time.sleep(0.004)
    jui.set_personality_mode("friendly")
    pump(3.0)
    if NOGAP and getattr(win._cc.root, "_sky_timer", None) is not None:
        win._cc.root._sky_timer.stop()
    if NOBEAT and hasattr(win.hud, "_heartbeat"):
        win.hud._heartbeat = False
    proc = psutil.Process()
    res = {}
    for st in ("LISTENING", "SPEAKING", "THINKING"):
        jui.set_state(st)
        pump(1.0)
        proc.cpu_percent(None)
        t_end = time.monotonic() + 5.0
        while time.monotonic() < t_end:
            if st == "SPEAKING":
                win.hud.set_audio_level(0.4)
            pump(0.05)
        res[st] = proc.cpu_percent(None)
    ts = []
    for _ in range(40):
        t0 = time.perf_counter()
        win.hud.grab()
        ts.append((time.perf_counter() - t0) * 1000)
    tag = "BEFORE (pre-Phase 8 UI)" if BASE else "AFTER  (Phase 8 UI)   "
    tag += " [gap timer off]" if NOGAP else ""
    tag += " [heartbeat off]" if NOBEAT else ""
    line = (f"{tag}  ui.py from "
            f"{Path(ui.__file__).parent.name}: CPU % of one core  listening {res['LISTENING']:.0f} · speaking "
            f"{res['SPEAKING']:.0f} · thinking {res['THINKING']:.0f}  | core paint median {sorted(ts)[20]:.1f} ms"
            f" · layer EMA {getattr(win.hud, '_render_ms', 0):.1f} ms")
    print(line, flush=True)
    with open(ROOT / "tools" / "ui_perf_compare.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    try:
        main()
    finally:
        os._exit(0)
