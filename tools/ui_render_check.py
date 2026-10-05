"""
tools/ui_render_check.py - Phase 8 UI check, run on the PC without touching
the assistant (no voice session): builds the real JARVIS window offscreen,
switches modes and states, and checks / measures / photographs it.

    Friendly is blue in every state; Serious keeps its colours; red stays red
    the log no longer paints "Interrupted" as an error
    the sky rotates at the configured speed; the heartbeat beats at the
    state's tempo and eases between tempos
    Phase 9 (ALIVE): only the core beats (the sphere is pixel-identical at
    rest, on the pulse and with the voice), the core is the brightest area,
    every beat feeds the veins, the veins get busier per state and in
    Serious mode, speaking keeps the heartbeat, no memory growth
    paint cost of the core and of the sky; CPU of the whole UI while animating

Frames go to _to_delete/ui_check/*.png, results to tools/ui_render_check.log.
"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
OUT = ROOT / "_to_delete" / "ui_check"
OUT.mkdir(parents=True, exist_ok=True)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LINES: list[str] = []
FAILS: list[str] = []


def log(s: str) -> None:
    LINES.append(s)
    print(s, flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    log(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    import psutil
    import ui
    import ui_command_center as cc
    from PyQt6.QtCore import QEventLoop
    from PyQt6.QtGui import QColor, QImage, QPainter

    jui = ui.JarvisUI("face.png")
    app = QApplication = jui._app
    win = jui._win
    win.showNormal()
    win.resize(1440, 900)

    def pump(sec: float) -> None:
        end = time.monotonic() + sec
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 16)
            time.sleep(0.004)

    pump(1.5)
    hud = win.hud
    root = win._cc.root

    def shot(name: str) -> None:
        win.grab().save(str(OUT / f"{name}.png"))

    def blue(rgb) -> bool:
        r, g, b = rgb
        return b >= 200 and b > r + 60 and b >= g

    # ── Friendly: all blue ────────────────────────────────────────────────────
    jui.set_personality_mode("friendly")
    pump(2.0)
    th = ui.HUD_THEME["friendly"]
    for st in ("idle", "listening", "thinking", "executing", "speaking", "warning"):
        check(f"friendly '{st}' colour is blue", blue(th[st]["color"]), str(th[st]["color"]))
    check("friendly 'error' stays red", th["error"]["color"][0] > 200 and th["error"]["color"][2] < 120,
          str(th["error"]["color"]))
    for k in ("ACC", "ACC2", "GREEN", "GREEN_D", "MUTED_C"):
        c = QColor(getattr(ui.C, k))
        check(f"friendly C.{k} is blue", c.blue() > c.red() + 60, getattr(ui.C, k))
    check("C.RED stays red", QColor(ui.C.RED).red() > 200, ui.C.RED)
    tl = win._cc.task_list
    for st in ("COMPLETED", "WAITING_FOR_INPUT", "RUNNING"):
        c = QColor(tl._state_col(st))
        check(f"friendly task state {st} is blue", c.blue() > c.red() + 60, c.name())
    check("friendly task FAILED is red", QColor(tl._state_col("FAILED")).red() > 200, tl._state_col("FAILED"))
    ib = win._interrupt_btn.styleSheet()
    check("friendly INTERRUPT button has no pink-red", "255,51,102" not in ib and "#ff6688" not in ib)
    check("STOP button stays red", "255,34,34" in win._stop_btn.styleSheet())
    check("friendly uptime label not green", "#00ff88" not in win._uptime_lbl.styleSheet(),
          win._uptime_lbl.styleSheet())
    jui.set_state("LISTENING")
    pump(2.5)
    r, g, b = hud._cur_color
    check("friendly LISTENING core colour is blue (not green)", b > g and b > r + 60,
          f"({r:.0f}, {g:.0f}, {b:.0f})")
    shot("friendly_listening")
    jui.set_state("THINKING")
    pump(2.0)
    shot("friendly_thinking")
    jui.set_state("SPEAKING")
    for _ in range(90):
        hud.set_audio_level(0.35 + 0.3 * math.sin(time.monotonic() * 9))
        pump(0.02)
    shot("friendly_speaking")
    jui.set_state("LISTENING")
    pump(1.0)

    # ── log colours ──────────────────────────────────────────────────────────
    lw = win._log
    for text, want in (("SYS: Interrupted (ESC) - listening...", "sys"), ("ERR: API key invalid", "err"),
                       ("SYS: an error happened", "err"), ("SYS: Emergency stop engaged", "sys"),
                       ("You: hello", "you")):
        lw._queue.clear()
        lw._typing = False
        lw._enqueue(text)
        check(f"log tag for {text[:28]!r} = {want}", lw._tag == want, lw._tag)
        pump(0.6)

    # ── Serious keeps its colours ────────────────────────────────────────────
    jui.set_personality_mode("serious")
    pump(2.0)
    check("serious C.ACC2 back to default", ui.C.ACC2.lower() == "#ffcc00", ui.C.ACC2)
    check("serious C.GREEN back to default", ui.C.GREEN.lower() == "#00ff88", ui.C.GREEN)
    check("serious INTERRUPT back to its colour", "255,51,102" in win._interrupt_btn.styleSheet())
    shot("serious_listening")
    jui.set_personality_mode("friendly")
    pump(2.0)
    check("friendly again: C.ACC2 blue", QColor(ui.C.ACC2).blue() > QColor(ui.C.ACC2).red() + 60, ui.C.ACC2)

    # ── the rotating sky ─────────────────────────────────────────────────────
    sky = root.sky
    check("sky exists (space background)", sky is not None)
    if sky is not None:
        per = 2 * math.pi / sky.omega if sky.omega else 0
        check("sky turns (seconds per turn)", sky.omega > 0, f"{per:.0f} s per turn")
        a0 = sky.angle(time.monotonic())
        cx, cy = root.sky_center()
        hc = hud.mapTo(root, hud.rect().center())
        check("sky turns around the core's centre", abs(cx - hc.x()) <= 1 and abs(cy - hc.y()) <= 1,
              f"sky centre ({cx:.0f},{cy:.0f}) core ({hc.x()},{hc.y()})")
        img = QImage(1440, 900, QImage.Format.Format_ARGB32_Premultiplied)
        ts = []
        for _ in range(30):
            img.fill(QColor(0, 0, 2))
            p = QPainter(img)
            t0 = time.perf_counter()
            sky.paint(p, 1440, 900, cx, cy)
            ts.append((time.perf_counter() - t0) * 1000)
            p.end()
        check("full-window sky paint < 8 ms", sorted(ts)[len(ts) // 2] < 8.0, f"median {sorted(ts)[15]:.2f} ms")
        img.save(str(OUT / "sky_only.png"))
        shot("rotation_t0")
        pump(15.0)
        shot("rotation_t15s")
        a1 = sky.angle(time.monotonic())
        log(f"      sky angle moved {math.degrees((a1 - a0) % (2 * math.pi)):.1f} deg in ~16.5 s")

    # ── heartbeat ────────────────────────────────────────────────────────────
    jui.set_state("LISTENING")
    pump(3.0)
    hbs, beats0 = [], hud._beat
    t_end = time.monotonic() + 4.0
    while time.monotonic() < t_end:
        pump(0.016)
        hbs.append(hud._hb)
    beats = hud._beat - beats0
    check("heartbeat beats (LISTENING ~66 bpm)", 3.6 <= beats <= 5.2, f"{beats:.2f} beats in 4 s, {hud._bpm:.1f} bpm")
    check("heartbeat envelope rises and falls", max(hbs) > 0.85 and min(hbs) < 0.05,
          f"min {min(hbs):.3f} max {max(hbs):.3f}")
    jui.set_state("THINKING")
    pump(0.5)
    mid = hud._bpm
    pump(4.0)
    check("tempo eases toward THINKING (84 bpm), no jump", 66 < mid < 84 and abs(hud._bpm - 84) < 3,
          f"after 0.5 s {mid:.1f} bpm, after 4.5 s {hud._bpm:.1f} bpm")
    # ── Phase 9: ALIVE — the heartbeat belongs to the core, the veins carry it ──
    import numpy as np
    import tracemalloc
    import ui_holo as holo
    from PyQt6.QtGui import QPixmap
    from PyQt6.QtCore import Qt as _Qt
    check("no shell-wide waves or flashes any more",
          not hasattr(hud, "_paint_waves") and not hasattr(hud, "_paint_brain_flash"))
    check("the heart and the circulation are running",
          isinstance(hud._heart, holo.HeartClock) and isinstance(hud._flow, holo.VeinFlow))

    def to_np(pm):
        # premultiplied = the light each pixel adds (everything here is drawn additively)
        im = pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
        ptr = im.constBits()
        ptr.setsize(im.sizeInBytes())
        a = np.frombuffer(ptr, np.uint8).reshape(im.height(), im.bytesPerLine() // 4, 4)[:, :im.width(), :3]
        return a.astype(np.float32).sum(axis=2)

    def painted(h, fn):
        dpr = h.devicePixelRatioF()
        pm = QPixmap(int(h.width() * dpr), int(h.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(_Qt.GlobalColor.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        q.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        fn(q)
        q.end()
        return pm

    jui.set_state("IDLE")
    pump(2.5)
    W_, H_ = hud.width(), hud.height()
    cx_, cy_ = W_ / 2.0, H_ / 2.0
    keep_beat = hud._core_beat
    # (1) the shell never beats and never follows the voice (state frozen: no events pumped)
    hud._core_beat = holo.CoreBeat()
    hud._amp_disp = 0.0
    l0 = to_np(hud._render_layer(W_, H_))
    hud._core_beat = holo.CoreBeat(size=1.0, light=1.0, impulse=0.1, voice=0.9)
    hud._amp_disp = 0.9
    l1 = to_np(hud._render_layer(W_, H_))
    check("the sphere itself is pixel-identical at rest, on the strong pulse and with a loud voice",
          np.array_equal(l0, l1), f"{int((l0 != l1).sum())} pixels differ")
    hud._amp_disp = 0.0

    # (2) the core beats: brighter and bigger on the strong pulse, smaller in the contraction
    Rs = hud._layer_R * 0.82 * hud.devicePixelRatioF()

    def core_light(beat):
        hud._core_beat = beat
        img = to_np(painted(hud, lambda q: hud._paint_core(q, cx_, cy_)))
        hh, ww = img.shape
        yy, xx = np.mgrid[0:hh, 0:ww]
        rad = np.hypot(xx - ww / 2.0, yy - hh / 2.0) / Rs
        tot = img.sum()
        order = np.argsort(rad.ravel())
        cum = np.cumsum(img.ravel()[order])
        r80 = rad.ravel()[order][np.searchsorted(cum, 0.8 * tot)]
        return tot, r80, img

    t0_, r0_, img0 = core_light(holo.CoreBeat())
    t1_, r1_, img1 = core_light(holo.CoreBeat(size=1.0, light=1.0))
    t2_, r2_, _ = core_light(holo.CoreBeat(size=-0.45, light=-0.2))
    check("the core beats: brighter and wider on the strong pulse, smaller in the contraction",
          t1_ > t0_ * 1.4 and r1_ > r0_ and t2_ < t0_ and r2_ <= r0_ + 0.005,
          f"light x{t1_ / t0_:.2f} / x{t2_ / t0_:.2f}, 80 % of it within r {r2_:.3f} < {r0_:.3f} < {r1_:.3f}")
    _, _, imgv = core_light(holo.CoreBeat(voice=0.8))
    check("the voice is felt around the core", imgv.sum() > t0_ * 1.1, f"x{imgv.sum() / t0_:.2f}")
    _, _, imgi = core_light(holo.CoreBeat(impulse=0.2))
    hh_, ww_ = img0.shape
    yy_, xx_ = np.mgrid[0:hh_, 0:ww_]
    rad_ = np.hypot(xx_ - ww_ / 2.0, yy_ - hh_ / 2.0) / Rs
    ring = (imgi - img0).clip(0)
    check("each strong pulse sends a small energy impulse, inside the core region only",
          ring[(rad_ > 0.2) & (rad_ < 0.55)].sum() > 0 and ring[rad_ > 0.62].sum() < 1e-6 * max(1.0, ring.sum()) + 1,
          f"impulse light {ring.sum():.0f}, beyond r 0.62: {ring[rad_ > 0.62].sum():.0f}")
    hud._core_beat = keep_beat

    # (3) the core is the brightest area of the whole sphere
    pump(0.5)
    full = to_np(hud._layer) + to_np(painted(hud, lambda q: (hud._paint_flow(q, cx_, cy_), hud._paint_core(q, cx_, cy_))))
    hh_, ww_ = full.shape
    yy_, xx_ = np.mgrid[0:hh_, 0:ww_]
    rad_ = np.hypot(xx_ - ww_ / 2.0, yy_ - hh_ / 2.0) / Rs
    top = full >= np.percentile(full[rad_ < 1.3], 99.8)
    check("the core is the brightest area", (rad_[top] < 0.3).mean() > 0.95,
          f"{(rad_[top] < 0.3).mean():.0%} of the brightest pixels are in the core")

    # (4) per state × mode: tempo and how busy the veins are (live, event loop running)
    def watch(state, secs=6.0, speak=False):
        jui.set_state(state)
        pump(2.0)
        n0, a = hud._flow.launches, []
        t_end = time.monotonic() + secs
        while time.monotonic() < t_end:
            if speak:
                hud.set_audio_level(0.35 + 0.3 * math.sin(time.monotonic() * 9))
            pump(0.05)
            a.append(int((hud._flow.on & ~hud._flow.drift).sum()))
        return float(np.mean(a)), hud._flow.launches - n0, hud._bpm

    rows = {}
    for mode in ("friendly", "serious"):
        jui.set_personality_mode(mode)
        for st in ("IDLE", "LISTENING", "THINKING", "EXECUTING", "SPEAKING"):
            rows[(mode, st)] = watch(st, speak=(st == "SPEAKING"))
            a_, n_, b_ = rows[(mode, st)]
            log(f"      {mode:8s} {st:9s} {b_:5.1f} bpm, {n_:2d} launches in 6 s, {a_:4.1f} pulses in the veins")
    jui.set_personality_mode("friendly")
    for mode in ("friendly", "serious"):
        acts = [rows[(mode, s)][0] for s in ("IDLE", "LISTENING", "THINKING", "EXECUTING")]
        check(f"{mode}: the veins get busier idle < listening < thinking < executing",
              all(x < y for x, y in zip(acts, acts[1:])), " < ".join(f"{x:.1f}" for x in acts))
    check("serious: a faster heart and busier veins than friendly (idle, thinking)",
          all(rows[("serious", s)][2] > rows[("friendly", s)][2] and rows[("serious", s)][0] > rows[("friendly", s)][0]
              for s in ("IDLE", "THINKING")))
    check("every beat feeds the veins (launches ≈ 2 per beat: the strong pulse and its echo)",
          all(abs(rows[k][1] - 2 * 6.0 * rows[k][2] / 60.0) <= 3 for k in rows),
          ", ".join(f"{k[1][:4]}:{rows[k][1]}" for k in rows if k[0] == "friendly"))

    # (5) speaking: the heart keeps beating, the sphere doesn't follow the voice
    jui.set_state("SPEAKING")
    pump(2.0)
    rm, gk, vc, hb = [], [], [], []
    t_end = time.monotonic() + 4.0
    while time.monotonic() < t_end:
        hud.set_audio_level(0.2 + 0.6 * abs(math.sin(time.monotonic() * 7)))
        pump(0.02)
        rm.append(hud._r_mult)
        gk.append(hud._glowk)
        vc.append(hud._voice)
        hb.append(hud._hb)
    check("speaking: the sphere's size and glow ignore the voice; the core follows it; the heart keeps beating",
          np.ptp(rm) < 0.01 and np.ptp(gk) < 0.02 and np.ptp(vc) > 0.15 and max(hb) > 0.85,
          f"size range {np.ptp(rm):.4f}, glow range {np.ptp(gk):.4f}, core voice range {np.ptp(vc):.2f}, "
          f"beat peak {max(hb):.2f}")

    # (6) photographs: rest, strong pulse, pulses in flight (thinking)
    jui.set_state("THINKING")
    pump(3.0)
    got = set()
    t_end = time.monotonic() + 6
    while time.monotonic() < t_end and len(got) < 3:
        pump(0.008)
        tau = hud._beat_tau
        if "strong" not in got and hud._hb > 0.95:
            shot("alive_strong_pulse")
            got.add("strong")
        elif "flow" not in got and 0.40 < tau < 0.55:
            shot("alive_veins_carrying")
            got.add("flow")
        elif "rest" not in got and hud._hb < 0.02 and tau > 0.6:
            shot("alive_rest")
            got.add("rest")
    check("photographed the strong pulse, the veins carrying it and the rest", len(got) == 3, str(sorted(got)))

    # (7) no leaks while it lives: the same 36 s tour of every state and both modes,
    # twice; the second tour must not need more memory than the first left behind
    proc_ = psutil.Process()
    seq = ("IDLE", "LISTENING", "THINKING", "EXECUTING", "SPEAKING", "IDLE")

    def tour():
        for i in range(12):
            jui.set_personality_mode("serious" if i % 4 == 3 else "friendly")
            jui.set_state(seq[i % len(seq)])
            pump(3.0)

    tour()
    rss1 = proc_.memory_info().rss
    tracemalloc.start()
    snap0 = tracemalloc.take_snapshot()
    tour()
    grow = sum(s.size_diff for s in tracemalloc.take_snapshot().compare_to(snap0, "lineno"))
    tracemalloc.stop()
    rss2 = proc_.memory_info().rss
    jui.set_personality_mode("friendly")
    check("no memory growth: a second tour of every state and both modes needs nothing more",
          grow < 512 * 1024 and (rss2 - rss1) < 10 * 1024 * 1024 and hud._flow.on.shape == (holo.VeinFlow.POOL,),
          f"Python heap {grow / 1024:+.0f} KB, process {(rss2 - rss1) / 1048576:+.1f} MB, "
          f"pool {hud._flow.active()}/{holo.VeinFlow.POOL}")

    # (8) cost of the live parts every frame
    jui.set_state("THINKING")
    pump(2.0)
    ts_ = []
    for _ in range(60):
        t0 = time.perf_counter()
        painted(hud, lambda q: (hud._paint_flow(q, cx_, cy_), hud._paint_core(q, cx_, cy_)))
        ts_.append((time.perf_counter() - t0) * 1000)
        pump(0.016)
    ts_.sort()
    log(f"      live core + veins per frame (thinking): median {ts_[30]:.2f} ms, p95 {ts_[57]:.2f} ms "
        f"(includes a {W_}x{H_} pixmap clear the real paint doesn't need)")
    check("the live core + veins cost well under a 60 fps frame", ts_[30] < 8.0, f"median {ts_[30]:.2f} ms")
    jui.set_state("LISTENING")
    pump(1.0)

    # photograph a beat peak and a trough
    jui.set_state("IDLE")
    pump(2.0)
    got = set()
    t_end = time.monotonic() + 6
    while time.monotonic() < t_end and len(got) < 2:
        pump(0.008)
        if hud._hb > 0.9 and "peak" not in got:
            shot("heartbeat_peak")
            got.add("peak")
        elif hud._hb < 0.02 and "rest" not in got and (hud._beat % 1.0) > 0.6:
            shot("heartbeat_rest")
            got.add("rest")
    check("photographed a beat peak and a rest", got == {"peak", "rest"}, str(sorted(got)))

    # ── smooth transitions: radius / spin / glow ease instead of jumping ──────
    jui.set_state("LISTENING")
    pump(3.0)
    r0, g0 = hud._r_mult, hud._glow_s
    jui.set_state("THINKING")
    pump(0.1)
    r1, g1 = hud._r_mult, hud._glow_s
    pump(3.0)
    r2 = hud._r_mult
    check("radius eases (no jump in 0.1 s)", abs(r1 - r0) < 0.25 * abs(r2 - r0) + 1e-6,
          f"{r0:.3f} -> {r1:.3f} after 0.1 s -> {r2:.3f} after 3 s")

    # ── cost ─────────────────────────────────────────────────────────────────
    ts = []
    for _ in range(40):
        t0 = time.perf_counter()
        hud.grab()
        ts.append((time.perf_counter() - t0) * 1000)
    log(f"      core widget paint (grab): median {sorted(ts)[20]:.1f} ms, layer render EMA "
        f"{getattr(hud, '_render_ms', 0):.1f} ms, re-render every {getattr(hud, '_every', 2)} frames")
    proc = psutil.Process()
    proc.cpu_percent(None)
    pump(10.0)
    cpu = proc.cpu_percent(None)
    log(f"      whole UI animating for 10 s: {cpu:.0f} % of one core "
        f"({cpu / max(1, psutil.cpu_count()):.1f} % of the machine, {psutil.cpu_count()} logical cores)")
    log(f"RESULT: {'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAIL: ' + ', '.join(FAILS)}")
    return 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    except Exception:
        import traceback
        log(traceback.format_exc())
    finally:
        (ROOT / "tools" / "ui_render_check.log").write_text("\n".join(LINES) + "\n", encoding="utf-8")
        os._exit(rc)
