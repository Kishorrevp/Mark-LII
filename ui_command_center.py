"""
ui_command_center.py — the "Mark LIII" Command Center interface.

A full-screen, glass-panel HUD layout inspired by the supplied
"JARVIS neural dashboard" design: a live holographic core in the middle
(Mark LIII.2: a particle sphere with fragmented 3-D orbit rings, modelled on
the film's JARVIS — geometry in ui_holo.py), telemetry and protocol panels
on the left, activity / atmospherics / uplink on the right, a slim status
strip across the top (clock, volume, power) and a command dock at the
bottom with the Friendly / Serious / Ambient buttons. Every figure is shown
once: CPU/RAM/GPU/temperature in Diagnostics, network in Uplink, disks in
Quick Launch. Plus an Ambient mode — a small floating reactor that stays on top
of other windows (F10), the Phase-3 "Ambient / Command-Center windowing"
item that had been deferred.

WHAT IS PRESERVED FROM THE PREVIOUS HUD (ui.py)
    * HudCanvas._step() is inherited unchanged: Friendly/Serious colour
      identity (HUD_THEME), the eased cross-fades, state colours
      (listening green / brighter red, warning, error), voice reactivity
      (_live_amp → _amp_disp), motion profiles and pulses. Only the frame
      *rendering* is new (ArcReactorHud.paintEvent).
    * Every MainWindow attribute the rest of the app touches (_log, _input,
      _mute_btn, _interrupt_btn, _drop_zone, _file_hint, _title_lbl,
      _clock_lbl, _bar_cpu …, hud, _content_panel, _hud_cam_stack …) is
      created here with the same name and behaviour, so main.py, the
      confirm banner, overlays and camera feed work unchanged.
    * The classic layout is still available: set "ui_layout": "classic" in
      config/api_keys.json (or use the CLASSIC LAYOUT button in the ⚙ drawer).

GLASS
    Qt has no CSS backdrop-filter, so the frosted-glass look is built for
    real: the backdrop (gradient, grid, stars, accent glow) is rendered once
    per size/colour into a pixmap, a blurred copy is made by multi-pass
    down/up-sampling, and every GlassPanel paints the blurred region behind
    itself, then a tinted gradient, a light top edge, a soft accent border
    and HUD corner brackets. Cheap enough to repaint at 60 fps.

DATA (all real, all local unless noted)
    psutil (CPU, RAM, disks, battery, network rates, uptime), socket (LAN
    IP), netsh / nmcli (Wi-Fi SSID + signal), pycaw (volume, Windows),
    memory/tasks/tasks.json (JARVIS task system), wttr.in (weather — the one
    network call; set "weather_enabled": false to turn it off, or
    "weather_city": "Coimbatore" to pin a city instead of IP location).
"""
from __future__ import annotations

import json
import math
import os
import platform
import random
import re
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import psutil

from PyQt6.QtCore import QLineF, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDesktopServices, QFont, QFontDatabase, QFontMetrics,
    QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient, QRegion,
)
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizeGrip,
    QSizePolicy, QSplitter, QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

import ui as _ui
from ui import C, HudCanvas, qcol
import ui_holo as _holo

_OS = platform.system()
if _OS == "Windows":
    _NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _NOWIN = {}


# ═════════════════════════════════════════════════════════════════════════════
# Fonts & accent
# ═════════════════════════════════════════════════════════════════════════════
_FAMILIES: dict[str, str] = {}


def _pick(cands: list[str], key: str) -> str:
    if key in _FAMILIES:
        return _FAMILIES[key]
    try:
        have = set(QFontDatabase.families())
    except Exception:
        have = set()
    for c in cands:
        if c in have:
            _FAMILIES[key] = c
            return c
    _FAMILIES[key] = cands[-1]
    return cands[-1]


def hfont(px: float, weight=QFont.Weight.Normal, mono: bool = False, spacing: float = 0.0) -> QFont:
    """Display font: Bahnschrift (Windows 10+, a DIN-style technical face) →
    Segoe UI → sans. Mono: Cascadia Mono → Consolas → Courier New."""
    fam = (_pick(["Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New"], "mono") if mono
           else _pick(["Bahnschrift", "Segoe UI", "Rajdhani", "DejaVu Sans", "Arial"], "disp"))
    f = QFont(fam)
    f.setPixelSize(max(6, int(round(px))))
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


class _Accent:
    """The panels' accent colour = the personality MODE colour (Friendly blue,
    Serious red), cross-faded over ~1 s. The interaction-state shades stay on
    the reactor itself (Phase 8: in Friendly every state is a shade of blue;
    only an error is red), so the dashboard keeps its identity while the core
    reports what is happening."""
    hud: "ArcReactorHud | None" = None
    _fallback = (62, 166, 255)
    _cur: list | None = None
    _t = 0.0

    @classmethod
    def rgb(cls) -> tuple[int, int, int]:
        h = cls.hud
        tgt = cls._fallback
        if h is not None:
            try:
                tgt = _ui.HUD_THEME[h._mode_name]["idle"]["color"]
            except Exception:
                pass
        now = time.monotonic()
        if cls._cur is None:
            cls._cur = [float(v) for v in tgt]
        else:
            k = min(1.0, (now - cls._t) * 3.0)          # ~1 s cross-fade
            for i in range(3):
                cls._cur[i] += (tgt[i] - cls._cur[i]) * k
        cls._t = now
        return (int(cls._cur[0]), int(cls._cur[1]), int(cls._cur[2]))

    @classmethod
    def c(cls, a: int = 255, lift: float = 0.0) -> QColor:
        r, g, b = cls.rgb()
        if lift:
            r, g, b = (int(v + (255 - v) * lift) for v in (r, g, b))
        return QColor(r, g, b, max(0, min(255, int(a))))

    @classmethod
    def key(cls) -> tuple[int, int, int]:
        r, g, b = cls.rgb()
        return (r // 12, g // 12, b // 12)

    @classmethod
    def hex(cls, lift: float = 0.0) -> str:
        c = cls.c(255, lift)
        return f"#{c.red():02x}{c.green():02x}{c.blue():02x}"


TEXT_HI = QColor(226, 241, 248)
TEXT_MID = QColor(148, 163, 184)
TEXT_LO = QColor(100, 116, 139)


def _friendly() -> bool:
    """Phase 8: Friendly mode is blue everywhere (red only for errors / STOP)."""
    return getattr(_Accent.hud, "_mode_name", "") == "friendly"


# ═════════════════════════════════════════════════════════════════════════════
# Live data services (background threads; UI polls snapshots)
# ═════════════════════════════════════════════════════════════════════════════

def _read_cfg() -> dict:
    try:
        return _ui._read_full_config()
    except Exception:
        return {}


class SysInfo:
    """Everything the status strip and panels show, refreshed off the Qt thread."""

    def __init__(self):
        self.data: dict = {"down": 0.0, "up": 0.0, "lan": "--", "ssid": "", "signal": None,
                           "battery": None, "plugged": None, "volume": None, "disks": [],
                           "uptime": "", "procs": 0, "iface": ""}
        self._lock = threading.Lock()
        self._last = psutil.net_io_counters()
        self._last_t = time.time()
        self._wifi_t = 0.0
        self._vol_t = 0.0
        self._disk_t = 0.0
        threading.Thread(target=self._loop, daemon=True, name="cc-sysinfo").start()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.data)

    def _loop(self):
        if _OS == "Windows":
            try:
                import comtypes
                comtypes.CoInitialize()
            except Exception:
                pass
        while True:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(1.5)

    def _tick(self):
        now = time.time()
        nc = psutil.net_io_counters()
        dt = max(0.001, now - self._last_t)
        down = (nc.bytes_recv - self._last.bytes_recv) / dt
        up = (nc.bytes_sent - self._last.bytes_sent) / dt
        self._last, self._last_t = nc, now
        upd = {"down": down, "up": up, "lan": _lan_ip()}
        try:
            b = psutil.sensors_battery()
            if b is not None:
                upd["battery"] = b.percent
                upd["plugged"] = b.power_plugged
        except Exception:
            pass
        try:
            el = now - psutil.boot_time()
            upd["uptime"] = f"{int(el // 86400)}d {int(el % 86400 // 3600):02d}h {int(el % 3600 // 60):02d}m"
            upd["procs"] = len(psutil.pids())
        except Exception:
            pass
        if now - self._wifi_t > 15:
            self._wifi_t = now
            ssid, sig = _wifi_info()
            upd["ssid"], upd["signal"] = ssid, sig
        if now - self._vol_t > 3:
            self._vol_t = now
            upd["volume"] = _volume()
        if now - self._disk_t > 30:
            self._disk_t = now
            upd["disks"] = _disks()
        with self._lock:
            self.data.update(upd)


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 1))        # no packet is sent for UDP connect
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "offline"


def _wifi_info() -> tuple[str, int | None]:
    try:
        if _OS == "Windows":
            r = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True,
                               timeout=4, encoding="utf-8", errors="ignore", **_NOWIN)
            ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", r.stdout, re.M)
            sig = re.search(r"^\s*Signal\s*:\s*(\d+)%", r.stdout, re.M)
            return ((ssid.group(1).strip() if ssid else ""), (int(sig.group(1)) if sig else None))
        if _OS == "Linux":
            r = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
                               capture_output=True, text=True, timeout=4)
            for line in r.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 3 and parts[0] == "yes":
                    return parts[1], int(parts[2] or 0)
    except Exception:
        pass
    return "", None


def _volume() -> int | None:
    if _OS != "Windows":
        return None
    try:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        dev = AudioUtilities.GetSpeakers()
        vol = getattr(dev, "EndpointVolume", None)          # pycaw ≥ 2024 returns an AudioDevice
        if vol is None:
            itf = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            vol = cast(itf, POINTER(IAudioEndpointVolume))
        if vol.GetMute():
            return 0
        return int(round(vol.GetMasterVolumeLevelScalar() * 100))
    except Exception:
        return None


def _disks() -> list[dict]:
    out = []
    try:
        for part in psutil.disk_partitions(all=False):
            if _OS == "Windows" and ("cdrom" in part.opts or not part.fstype):
                continue
            if _OS != "Windows" and not (part.mountpoint in ("/", "/home") or part.mountpoint.startswith("/media")):
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            label = part.device[:1].upper() if _OS == "Windows" else ("/" if part.mountpoint == "/" else
                                                                        Path(part.mountpoint).name[:1].upper())
            out.append({"label": label, "pct": u.percent, "free_gb": u.free / 1024 ** 3,
                        "total_gb": u.total / 1024 ** 3})
    except Exception:
        pass
    return out[:3]


class WeatherService:
    """wttr.in JSON (free, no key). Refreshes every 30 min; never blocks the UI."""

    def __init__(self):
        self.data: dict | None = None
        self.error = ""
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True, name="cc-weather").start()

    def snapshot(self):
        with self._lock:
            return self.data, self.error

    def _city(self, cfg: dict) -> str:
        city = str(cfg.get("weather_city") or "").strip()
        if city:
            return city
        try:
            from memory.memory_manager import load_memory
            ent = (load_memory().get("identity") or {}).get("city")
            v = ent.get("value") if isinstance(ent, dict) else ent
            return str(v or "").strip()
        except Exception:
            return ""

    def _loop(self):
        time.sleep(2)
        while True:
            cfg = _read_cfg()
            if cfg.get("weather_enabled", True) is False:
                with self._lock:
                    self.data, self.error = None, "Weather feed disabled in settings."
                time.sleep(300)
                continue
            try:
                import requests
                city = self._city(cfg)
                url = f"https://wttr.in/{requests.utils.quote(city)}?format=j1" if city else \
                    "https://wttr.in/?format=j1"
                r = requests.get(url, timeout=10, headers={"User-Agent": "JARVIS-MarkLIII"})
                r.raise_for_status()
                d = self._parse(r.json())
                with self._lock:
                    self.data, self.error = d, ""
                time.sleep(1800)
            except Exception as e:
                with self._lock:
                    self.error = f"Weather unavailable ({type(e).__name__})."
                time.sleep(300)

    @staticmethod
    def _parse(j: dict) -> dict:
        cur = (j.get("current_condition") or [{}])[0]
        area = (j.get("nearest_area") or [{}])[0]

        def v(x, k):
            try:
                return x[k][0]["value"]
            except Exception:
                return ""
        days = []
        for d in (j.get("weather") or [])[:3]:
            hourly = d.get("hourly") or []
            desc = ""
            if hourly:
                desc = v(hourly[min(4, len(hourly) - 1)], "weatherDesc")
            days.append({"date": d.get("date", ""), "max": d.get("maxtempC", ""), "min": d.get("mintempC", ""),
                         "desc": desc})
        astro = ((j.get("weather") or [{}])[0].get("astronomy") or [{}])[0]
        return {
            "city": ", ".join(x for x in (v(area, "areaName"), v(area, "region")) if x) or "Local",
            "temp": cur.get("temp_C", "--"), "feels": cur.get("FeelsLikeC", "--"),
            "desc": v(cur, "weatherDesc") or "--", "humidity": cur.get("humidity", "--"),
            "wind": f"{cur.get('windspeedKmph', '--')} km/h {cur.get('winddir16Point', '')}".strip(),
            "pressure": cur.get("pressure", "--"), "precip": cur.get("precipMM", "--"),
            "visibility": cur.get("visibility", "--"), "uv": cur.get("uvIndex", "--"),
            "sunrise": astro.get("sunrise", "--"), "sunset": astro.get("sunset", "--"),
            "moon": astro.get("moon_phase", "--"), "days": days,
            "updated": datetime.now().strftime("%H:%M"),
        }


class TaskFeed:
    """Reads JARVIS's task store (memory/tasks/tasks.json) — no coupling to
    main.py; the file is written atomically by core/tasks/state.py."""

    def __init__(self):
        self.path = _ui.BASE_DIR / "memory" / "tasks" / "tasks.json"
        self._mtime = -1.0
        self.tasks: list[dict] = []

    def poll(self) -> bool:
        try:
            m = self.path.stat().st_mtime
        except Exception:
            return False
        if m == self._mtime:
            return False
        self._mtime = m
        try:
            self.tasks = json.loads(self.path.read_text(encoding="utf-8")).get("tasks", [])[-12:]
        except Exception:
            return False
        return True


def _fmt_rate(bps: float) -> tuple[str, str]:
    kb = bps / 1024
    if kb < 1000:
        return f"{kb:.0f}", "K"
    return f"{kb / 1024:.1f}", "M"


# ═════════════════════════════════════════════════════════════════════════════
# Backdrop + glass
# ═════════════════════════════════════════════════════════════════════════════

class StarField:
    """Phase 8 — the rotating sky (the user's reference picture: pure black,
    many pin-point stars in white, ice-blue and warm amber, the brighter ones
    wrapped in a wide soft halo). The whole sky turns slowly around the JARVIS
    core, as if the core were a planet with space revolving around it.

    One instance is shared by the Backdrop (the gaps between panels) and the
    core widget (the large centre), so both always show the same sky. Stars
    live in polar coordinates around the core and the angle comes from the
    clock, so the two painters agree without talking to each other.

    Cost: the stars are pre-built into one QPolygonF per (tint, size,
    brightness) group in the sky's own frame; a frame is a translate + rotate
    and one drawPoints per group, all in C++. Twinkling stars are grouped by
    twinkle phase, so they cost the same. Only the ~6 % haloed stars are
    placed one by one (a rotated sprite would cost more than placing it).

    config/api_keys.json: "space_rotation_s" = seconds per full turn (default
    240; 0 = still sky)."""

    TINTS = ((255, 255, 255), (214, 232, 255), (168, 210, 255), (140, 222, 255),
             (255, 216, 176), (255, 172, 112))
    _TINT_W = (0.34, 0.22, 0.16, 0.10, 0.10, 0.08)
    _TW_BUCKETS = 8

    def __init__(self, count: int = 3000, seed: int = 2026):
        rnd = random.Random(seed)
        self.n = count
        self.r = np.sqrt(np.array([rnd.random() for _ in range(count)]))      # uniform by area
        self.th = np.array([rnd.random() * 2.0 * math.pi for _ in range(count)])
        self.b = np.array([rnd.random() ** 2.2 for _ in range(count)])         # mostly faint dust
        cum = np.cumsum(self._TINT_W)
        self.tint = np.array([int(np.searchsorted(cum, rnd.random() * cum[-1])) for _ in range(count)])
        self.size = np.select([self.b > 0.80, self.b > 0.45, self.b > 0.15], [2.2, 1.6, 1.2], 0.9)
        self.lv = (np.clip(40 + self.b * 215, 0, 255) // 32).astype(int)       # brightness level 1..7
        self.halo_idx = np.nonzero(self.b > 0.88)[0]                          # ~6 % wear a soft halo
        self.halo_rad = (10 + 10 * self.b).astype(int)
        twinkle = np.array([rnd.random() < 0.40 for _ in range(count)]) & (self.b > 0.25)
        bucket = np.array([rnd.randrange(self._TW_BUCKETS) for _ in range(count)])
        self.tw_f = [rnd.uniform(0.25, 1.1) * 2.0 * math.pi for _ in range(self._TW_BUCKETS)]
        self.tw_ph = [rnd.random() * 2.0 * math.pi for _ in range(self._TW_BUCKETS)]
        # group key: (tint, size, level, twinkle bucket or -1) -> star indices
        groups: dict = {}
        for i in range(count):
            k = (int(self.tint[i]), float(self.size[i]), int(self.lv[i]), int(bucket[i]) if twinkle[i] else -1)
            groups.setdefault(k, []).append(i)
        self._groups = {k: np.array(v) for k, v in groups.items()}
        self._polys: dict = {}
        self._poly_R = None
        try:
            per = float(_read_cfg().get("space_rotation_s", 240))
        except (TypeError, ValueError):
            per = 240.0
        self.omega = (2.0 * math.pi / per) if per > 0 else 0.0
        self._t0 = time.monotonic()
        self._sprites: dict = {}
        self._pens: dict = {}

    def angle(self, now: float) -> float:
        return ((now - self._t0) * self.omega) % (2.0 * math.pi)

    def _build(self, R: float) -> None:
        """Star positions at angle 0, in the sky's own frame (centre = 0,0)."""
        xs = self.r * R * np.cos(self.th)
        ys = self.r * R * np.sin(self.th)
        self._polys = {k: QPolygonF([QPointF(u, v) for u, v in zip(xs[idx].tolist(), ys[idx].tolist())])
                       for k, idx in self._groups.items()}
        self._poly_R = R

    def _sprite(self, tint: int, rad: int, dpr: float) -> QPixmap:
        key = (tint, rad, round(dpr, 2))
        pm = self._sprites.get(key)
        if pm is None:
            n = max(2, int(math.ceil(rad * 2 * dpr)))
            pm = QPixmap(n, n)
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            q = QPainter(pm)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            r, g, b = self.TINTS[tint]
            rg = QRadialGradient(QPointF(rad, rad), rad)
            rg.setColorAt(0.00, QColor(r, g, b, 135))
            rg.setColorAt(0.10, QColor(r, g, b, 70))
            rg.setColorAt(0.35, QColor(r, g, b, 22))
            rg.setColorAt(1.00, QColor(r, g, b, 0))
            q.setPen(Qt.PenStyle.NoPen)
            q.setBrush(QBrush(rg))
            q.drawEllipse(QPointF(rad, rad), rad, rad)
            q.end()
            self._sprites[key] = pm
        return pm

    def _pen(self, tint: int, size: float, alpha: int) -> QPen:
        key = (tint, size, alpha)
        pen = self._pens.get(key)
        if pen is None:
            r, g, b = self.TINTS[tint]
            pen = QPen(QColor(r, g, b, alpha), size)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            self._pens[key] = pen
        return pen

    def paint(self, p: QPainter, W: float, H: float, cx: float, cy: float, ox: float = 0.0, oy: float = 0.0,
              w: float | None = None, h: float | None = None, dpr: float = 1.0, now: float | None = None,
              region: QRegion | None = None) -> None:
        """Draw the sky of a W x H backdrop turning around (cx, cy) into painter `p`,
        whose origin is the point (ox, oy) of that backdrop and whose area is w x h.
        `region` (painter coordinates), when given, limits the haloed stars to it."""
        now = time.monotonic() if now is None else now
        w = W if w is None else w
        h = H if h is None else h
        a = self.angle(now)
        R = max(math.hypot(cx, cy), math.hypot(W - cx, cy), math.hypot(cx, H - cy), math.hypot(W - cx, H - cy)) + 24.0
        R = math.ceil(R / 16.0) * 16.0                     # rebuild the polygons only on a real resize
        if self._poly_R != R:
            self._build(R)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # halos (soft, wide) - placed one by one, unrotated sprites
        hi = self.halo_idx
        hx = cx + self.r[hi] * R * np.cos(self.th[hi] + a) - ox
        hy = cy + self.r[hi] * R * np.sin(self.th[hi] + a) - oy
        for j in np.nonzero((hx > -24.0) & (hx < w + 24.0) & (hy > -24.0) & (hy < h + 24.0))[0].tolist():
            i = int(hi[j])
            rad = int(self.halo_rad[i])
            if region is not None and not region.intersects(QRect(int(hx[j]) - rad, int(hy[j]) - rad,
                                                                  2 * rad, 2 * rad)):
                continue
            p.setOpacity(0.55 + 0.45 * float(self.b[i]))
            p.drawPixmap(QPointF(hx[j] - rad, hy[j] - rad), self._sprite(int(self.tint[i]), rad, dpr))
        p.setOpacity(1.0)
        # the pin-point cores: one rotated drawPoints per group
        p.translate(cx - ox, cy - oy)
        p.rotate(math.degrees(a))
        tw = [0.55 + 0.45 * math.sin(now * f + ph) for f, ph in zip(self.tw_f, self.tw_ph)]
        for (t, sz, lv, bk), poly in self._polys.items():
            alpha = 31 + lv * 32
            if bk >= 0:
                alpha = max(18, int(alpha * tw[bk]) // 24 * 24)
            p.setPen(self._pen(t, sz, min(255, alpha)))
            p.drawPoints(poly)
        p.restore()


class Backdrop(QWidget):
    """Central widget. Paints the deep-space field and serves blurred regions
    of it to GlassPanels.

    Phase 7 (the user's brief: "like it is in space — stars and nothing
    else"): the default background is a black starfield only — no grid, no
    nebula, no light streaks. config/api_keys.json "background": "hud" brings
    the Mark LIII grid back."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._sharp: QPixmap | None = None
        self._blur: QPixmap | None = None
        self._key = None
        self.sky: StarField | None = None
        self._sky_region: QRegion | None = None
        self._sky_region_t = 0.0
        rnd = random.Random(1977)
        self._stars = [(rnd.random(), rnd.random(), rnd.random()) for _ in range(160)]
        # starfield: (x, y, size, brightness, tint) — mostly faint dust, a few bright stars
        self._field = []
        for _ in range(1100):
            b = rnd.random() ** 3.2
            self._field.append((rnd.random(), rnd.random(), 0.6 + b * 1.6, b,
                                rnd.choice(((255, 255, 255), (205, 225, 255), (170, 200, 255),
                                            (255, 240, 220), (255, 255, 255)))))
        self._style = str(_read_cfg().get("background", "space") or "space").lower()
        if self._style != "hud":
            # Phase 8: the sky turns. The base pixmap (and the glass blur made from
            # it) is plain black; the stars are drawn live, here for the gaps
            # between panels (20 fps, those thin gaps only) and by the core widget
            # for the centre (~30 fps, blitted every frame).
            self.sky = StarField()
            self._sky_timer = QTimer(self)
            self._sky_timer.timeout.connect(self._tick_sky)
            if self.sky.omega > 0:                   # "space_rotation_s": 0 = a still sky, no refresh cost
                self._sky_timer.start(50)

    # -- Phase 8: the rotating sky ------------------------------------------------
    def sky_center(self) -> tuple[float, float]:
        """The core's centre in backdrop coordinates (the sky turns around it)."""
        h = _Accent.hud
        try:
            if h is not None and h.isVisible() and h.window() is self.window():
                c = h.mapTo(self, QPoint(h.width() // 2, h.height() // 2))
                self._last_center = (float(c.x()), float(c.y()))
        except Exception:
            pass
        return getattr(self, "_last_center", (self.width() / 2.0, self.height() / 2.0))

    def paint_sky(self, p: QPainter, w: QWidget) -> None:
        """Draw the live sky into widget `w`'s painter (its own coordinates)."""
        if self.sky is None:
            return
        off = w.mapTo(self, QPoint(0, 0))
        cx, cy = self.sky_center()
        self.sky.paint(p, self.width(), self.height(), cx, cy, off.x(), off.y(), w.width(), w.height(),
                       w.devicePixelRatioF())

    def _covered(self) -> QRegion:
        """The backdrop area NOT hidden by self-painting children (glass panels,
        the top bar, the core / camera stack) — the only part the sky timer
        repaints, so no panel is redrawn because the stars moved."""
        now = time.monotonic()
        if self._sky_region is not None and now - self._sky_region_t < 0.5:
            return self._sky_region
        reg = QRegion(self.rect())
        for wdg in self.findChildren(QWidget):
            if not wdg.isVisible():
                continue
            if isinstance(wdg, (GlassPanel, TopBar, QStackedWidget)) or wdg is _Accent.hud:
                tl = wdg.mapTo(self, QPoint(0, 0))
                reg = reg.subtracted(QRegion(QRect(tl, wdg.size())))
        self._sky_region, self._sky_region_t = reg, now
        return reg

    def _tick_sky(self) -> None:
        if not self.isVisible() or self.window().isMinimized():
            return
        reg = self._covered()
        if not reg.isEmpty():
            self.update(reg)

    def _paint_space(self, p: QPainter, W: int, H: int) -> None:
        p.fillRect(QRectF(0, 0, W, H), QColor(0, 0, 2))
        for sx, sy, size, b, tint in self._field:
            x, y = sx * W, sy * H
            a = int(26 + b * 229)
            if b > 0.55:                             # the few bright ones get a soft halo
                rg = QRadialGradient(QPointF(x, y), 2.5 + size * 3.2)
                rg.setColorAt(0.0, QColor(tint[0], tint[1], tint[2], int(70 * b)))
                rg.setColorAt(1.0, QColor(0, 0, 0, 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(rg))
                p.drawEllipse(QPointF(x, y), 2.5 + size * 3.2, 2.5 + size * 3.2)
            pen = QPen(QColor(tint[0], tint[1], tint[2], a), size)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawPoint(QPointF(x, y))

    def _ensure(self):
        W, H = max(1, self.width()), max(1, self.height())
        key = (W, H, _Accent.key() if self._style == "hud" else 0)
        if key == self._key and self._sharp is not None:
            return
        self._key = key
        dpr = self.devicePixelRatioF()
        img = QImage(int(W * dpr), int(H * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(QColor(3, 6, 10))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._style != "hud":
            if self.sky is None:                     # (not reached: sky exists for every non-"hud" style)
                self._paint_space(p, W, H)
            else:
                p.fillRect(QRectF(0, 0, W, H), QColor(0, 0, 2))
            p.end()
            self._finish(img, dpr)
            return
        g = QLinearGradient(0, 0, 0, H)
        g.setColorAt(0.0, QColor(4, 9, 15))
        g.setColorAt(0.55, QColor(3, 7, 12))
        g.setColorAt(1.0, QColor(2, 4, 8))
        p.fillRect(QRectF(0, 0, W, H), g)
        # accent nebula behind the reactor and two faint side glows
        for (fx, fy, fr, fa) in ((0.5, 0.48, 0.52, 52), (0.1, 0.25, 0.28, 34), (0.1, 0.8, 0.3, 26),
                                 (0.9, 0.22, 0.28, 34), (0.9, 0.78, 0.3, 24)):
            rg = QRadialGradient(QPointF(W * fx, H * fy), max(W, H) * fr)
            rg.setColorAt(0.0, _Accent.c(fa))
            rg.setColorAt(0.5, _Accent.c(fa // 3))
            rg.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(QRectF(0, 0, W, H), rg)
        # warm counter-light (the reactor-orange of the suit) — very low, for depth
        for (fx, fy, fr, fa) in ((0.78, 0.95, 0.35, 20), (0.2, 0.02, 0.25, 12)):
            rg = QRadialGradient(QPointF(W * fx, H * fy), max(W, H) * fr)
            rg.setColorAt(0.0, QColor(255, 140, 60, fa))
            rg.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(QRectF(0, 0, W, H), rg)
        # soft light streaks (what the frosted glass diffuses)
        for k in range(7):
            y = H * (0.08 + 0.13 * k)
            lg = QLinearGradient(0, y, W, y + H * 0.08)
            lg.setColorAt(0.0, QColor(0, 0, 0, 0))
            lg.setColorAt(0.5, _Accent.c(9 if k % 2 else 5))
            lg.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(QRectF(0, y - 30, W, 60), lg)
        # fine grid
        p.setPen(QPen(_Accent.c(10), 1))
        step = 40
        for x in range(0, W, step):
            p.drawLine(x, 0, x, H)
        for y in range(0, H, step):
            p.drawLine(0, y, W, y)
        # grid nodes
        p.setPen(QPen(_Accent.c(26), 1.6))
        for x in range(0, W, step * 4):
            for y in range(0, H, step * 4):
                p.drawPoint(x, y)
        # stars
        for sx, sy, sb in self._stars:
            a = int(40 + sb * 120)
            p.setPen(QPen(QColor(200, 230, 255, a), 1.0 + sb * 0.8))
            p.drawPoint(QPointF(sx * W, sy * H))
        # vignette
        vg = QRadialGradient(QPointF(W / 2, H / 2), max(W, H) * 0.75)
        vg.setColorAt(0.6, QColor(0, 0, 0, 0))
        vg.setColorAt(1.0, QColor(0, 0, 0, 170))
        p.fillRect(QRectF(0, 0, W, H), vg)
        p.end()
        self._finish(img, dpr)

    def _finish(self, img: QImage, dpr: float) -> None:
        self._sharp = QPixmap.fromImage(img)
        # blur: 3 passes of down/up-sampling ≈ gaussian
        small = img
        for f in (0.25, 0.25, 0.5):
            small = small.scaled(max(1, int(small.width() * f)), max(1, int(small.height() * f)),
                                 Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        blurred = small.scaled(img.width(), img.height(), Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        blurred.setDevicePixelRatio(dpr)
        self._blur = QPixmap.fromImage(blurred)

    def paintEvent(self, e):
        self._ensure()
        p = QPainter(self)
        if p.isActive():
            p.drawPixmap(0, 0, self._sharp)
            if self.sky is not None:
                cx, cy = self.sky_center()
                self.sky.paint(p, self.width(), self.height(), cx, cy, dpr=self.devicePixelRatioF(),
                               region=e.region())
            p.end()

    def paint_under(self, p: QPainter, w: QWidget, blurred: bool, rect: QRectF | None = None):
        """Paint the backdrop region that lies under widget `w` into p."""
        self._ensure()
        src = self._blur if blurred else self._sharp
        if src is None:
            return
        off = w.mapTo(self, QPoint(0, 0))
        dpr = src.devicePixelRatio()
        r = rect or QRectF(0, 0, w.width(), w.height())
        p.drawPixmap(r, src, QRectF((off.x() + r.x()) * dpr, (off.y() + r.y()) * dpr,
                                    r.width() * dpr, r.height() * dpr))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._key = None
        self._sky_region = None


def _backdrop_of(w: QWidget) -> Backdrop | None:
    p = w.parentWidget()
    while p is not None:
        if isinstance(p, Backdrop):
            return p
        p = p.parentWidget()
    return None


def paint_glass(p: QPainter, w: QWidget, rect: QRectF, radius: float = 10.0, strength: float = 1.0,
                brackets: bool = True):
    """The glass material: blurred backdrop + tint + sheen + edge light + border."""
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    p.save()
    p.setClipPath(path)
    bd = _backdrop_of(w)
    if bd is not None:
        bd.paint_under(p, w, True, rect)
    tint = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    tint.setColorAt(0.0, QColor(30, 52, 74, int(96 * strength)))
    tint.setColorAt(0.4, QColor(12, 24, 38, int(118 * strength)))
    tint.setColorAt(1.0, QColor(4, 10, 18, int(160 * strength)))
    p.fillRect(rect, tint)
    sheen = QRadialGradient(rect.topLeft() + QPointF(rect.width() * 0.15, 0), rect.width() * 0.9)
    sheen.setColorAt(0.0, _Accent.c(int(38 * strength)))
    sheen.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(rect, sheen)
    # diagonal glare band
    glare = QLinearGradient(rect.topLeft(), rect.bottomRight())
    glare.setColorAt(0.0, QColor(255, 255, 255, 0))
    glare.setColorAt(0.18, QColor(255, 255, 255, int(10 * strength)))
    glare.setColorAt(0.26, QColor(255, 255, 255, 0))
    p.fillRect(rect, glare)
    p.restore()
    # edge: bright top highlight fading down, accent border
    p.setBrush(Qt.BrushStyle.NoBrush)
    edge = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    edge.setColorAt(0.0, QColor(255, 255, 255, 70))
    edge.setColorAt(0.08, _Accent.c(90))
    edge.setColorAt(1.0, _Accent.c(38))
    p.setPen(QPen(QBrush(edge), 1.0))
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    if brackets:
        L = min(16.0, rect.width() / 5)
        p.setPen(QPen(_Accent.c(210), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        x0, y0, x1, y1 = rect.left() + 1, rect.top() + 1, rect.right() - 1, rect.bottom() - 1
        for (x, y, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            p.drawLine(QPointF(x, y + dy * radius * 0.6), QPointF(x, y + dy * (radius * 0.6 + L)))
            p.drawLine(QPointF(x + dx * radius * 0.6, y), QPointF(x + dx * (radius * 0.6 + L), y))


def _sky_opaque(w: QWidget, p: QPainter) -> None:
    """Phase 8: over the rotating sky a panel paints its whole rectangle (black
    under the rounded corners) and is marked opaque, so the stars moving behind
    it never force it to repaint."""
    bd = _backdrop_of(w)
    if bd is None or bd.sky is None:
        return
    if not w.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent):
        w.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
    p.fillRect(w.rect(), QColor(0, 0, 2))


class GlassPanel(QWidget):
    """A frosted-glass HUD panel with an optional icon + title header."""

    def __init__(self, title: str = "", icon: str = "", parent=None, margins=(14, 12, 14, 12)):
        super().__init__(parent)
        self._title = title
        self._icon = icon
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        top = margins[1] + (30 if title else 0)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(margins[0], top, margins[2], margins[3])
        self.body.setSpacing(6)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        _sky_opaque(self, p)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        paint_glass(p, self, r)
        if self._title:
            x = r.left() + 14
            if self._icon:
                _paint_icon(p, self._icon, QRectF(x, r.top() + 11, 16, 16))
                x += 24
            p.setFont(hfont(13, QFont.Weight.DemiBold, spacing=1.4))
            p.setPen(TEXT_HI)
            p.drawText(QRectF(x, r.top() + 8, r.width() - x, 22),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._title.upper())
            y = r.top() + 34
            g = QLinearGradient(r.left() + 12, y, r.right() - 12, y)
            g.setColorAt(0.0, _Accent.c(150))
            g.setColorAt(0.5, _Accent.c(50))
            g.setColorAt(1.0, _Accent.c(0))
            p.setPen(QPen(QBrush(g), 1))
            p.drawLine(QPointF(r.left() + 12, y), QPointF(r.right() - 12, y))
        p.end()


def _paint_icon(p: QPainter, name: str, r: QRectF):
    """Tiny line icons drawn with QPainter (no image assets needed)."""
    p.save()
    p.setPen(QPen(_Accent.c(235), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.setBrush(Qt.BrushStyle.NoBrush)
    cx, cy, s = r.center().x(), r.center().y(), r.width() / 2
    if name == "wifi":
        for k in (1.0, 0.66, 0.33):
            rr = s * k * 1.2
            p.drawArc(QRectF(cx - rr, cy - rr + s * 0.5, rr * 2, rr * 2), 45 * 16, 90 * 16)
        p.drawPoint(QPointF(cx, cy + s * 0.55))
    elif name == "tasks":
        p.drawRoundedRect(QRectF(cx - s * 0.75, cy - s, s * 1.5, s * 2), 2, 2)
        for k in (-0.4, 0.0, 0.4):
            p.drawLine(QPointF(cx - s * 0.4, cy + s * k), QPointF(cx + s * 0.4, cy + s * k))
    elif name == "pulse":
        pts = [(-1, 0), (-0.45, 0), (-0.2, -0.8), (0.15, 0.8), (0.4, 0), (1, 0)]
        path = QPainterPath(QPointF(cx + pts[0][0] * s, cy + pts[0][1] * s))
        for x, y in pts[1:]:
            path.lineTo(QPointF(cx + x * s, cy + y * s))
        p.drawPath(path)
    elif name == "link":
        p.drawEllipse(QPointF(cx, cy), s * 0.9, s * 0.9)
        p.drawLine(QPointF(cx - s * 0.9, cy), QPointF(cx + s * 0.9, cy))
        p.drawEllipse(QPointF(cx, cy), s * 0.4, s * 0.9)
    elif name == "log":
        p.drawRoundedRect(QRectF(cx - s * 0.85, cy - s * 0.7, s * 1.7, s * 1.4), 2, 2)
        p.drawLine(QPointF(cx - s * 0.5, cy - s * 0.2), QPointF(cx - s * 0.1, cy + s * 0.1))
        p.drawLine(QPointF(cx - s * 0.1, cy + s * 0.1), QPointF(cx - s * 0.5, cy + s * 0.4))
    elif name == "cloud":
        path = QPainterPath()
        path.addEllipse(QPointF(cx - s * 0.35, cy + s * 0.1), s * 0.45, s * 0.45)
        path.addEllipse(QPointF(cx + s * 0.2, cy - s * 0.1), s * 0.6, s * 0.6)
        path.addRoundedRect(QRectF(cx - s * 0.8, cy + s * 0.05, s * 1.6, s * 0.55), 3, 3)
        p.drawPath(path.simplified())
    elif name == "uplink":
        p.drawLine(QPointF(cx - s * 0.4, cy + s * 0.8), QPointF(cx - s * 0.4, cy - s * 0.8))
        p.drawLine(QPointF(cx - s * 0.75, cy - s * 0.45), QPointF(cx - s * 0.4, cy - s * 0.8))
        p.drawLine(QPointF(cx - s * 0.05, cy - s * 0.45), QPointF(cx - s * 0.4, cy - s * 0.8))
        p.drawLine(QPointF(cx + s * 0.4, cy - s * 0.8), QPointF(cx + s * 0.4, cy + s * 0.8))
        p.drawLine(QPointF(cx + s * 0.05, cy + s * 0.45), QPointF(cx + s * 0.4, cy + s * 0.8))
        p.drawLine(QPointF(cx + s * 0.75, cy + s * 0.45), QPointF(cx + s * 0.4, cy + s * 0.8))
    elif name == "file":
        path = QPainterPath(QPointF(cx - s * 0.6, cy - s))
        path.lineTo(QPointF(cx + s * 0.25, cy - s))
        path.lineTo(QPointF(cx + s * 0.65, cy - s * 0.6))
        path.lineTo(QPointF(cx + s * 0.65, cy + s))
        path.lineTo(QPointF(cx - s * 0.6, cy + s))
        path.closeSubpath()
        p.drawPath(path)
    elif name == "drive":
        p.drawRoundedRect(QRectF(cx - s, cy - s * 0.5, s * 2, s), 2, 2)
        p.drawPoint(QPointF(cx + s * 0.6, cy))
    else:
        p.drawEllipse(QPointF(cx, cy), s * 0.6, s * 0.6)
    p.restore()


# ═════════════════════════════════════════════════════════════════════════════
# The holographic core
# ═════════════════════════════════════════════════════════════════════════════

class ArcReactorHud(HudCanvas):
    """The JARVIS core — a holographic particle sphere (Mark LIII.2).

    Modelled on the reference the user supplied (the film's JARVIS "mind"
    sphere): a glowing nebula core inside a cloud of particles, wrapped in
    fragmented 3-D orbit rings, turning slowly. Nothing else is drawn on it —
    no chips, gauges, clock or text.

    Colour and state come from the inherited HudCanvas._step(): Friendly →
    blue in every state (listening is a brighter blue), Serious → red; an
    error is red; muted → grey. The sphere swells a little while listening,
    tightens and spins faster while thinking or executing.

    Phase 9 — ALIVE. The heartbeat belongs to the core; the veins carry it.
      * Shell (veins, hairs, orbits, the steady nebula glow): a cached layer
        (~30 fps, adaptive). It never beats and never follows the voice.
      * Brain (its tangle of veins + the dust orbiting it), the heart's light
        and the energy impulse: every frame (_paint_core), from a HeartClock —
        pulse → STRONG pulse → brief contraction → expansion → pause.
      * Pulses in the veins (_paint_flow): each strong pulse feeds a few
        arteries through a pooled VeinFlow, and between beats a few dim
        particles drift; drawn every frame with the layer's own rotation so
        they stay on the veins.
      * State × mode (_LIFE, _SERIOUS): tempo, beat strength, arteries per
        beat, branching, flow speed, drift and core glow — calm when idle,
        alert when listening, busy when thinking or executing; Serious beats
        harder and circulates faster. Speaking: the heart keeps beating and
        the voice is felt only around the core.
    "heartbeat": false in config/api_keys.json stops the beat (the core still
    breathes and the veins still drift). The live sky shows behind it (StarField).
    Geometry lives in ui_holo.py (numpy); this class only draws it.
    (The class keeps its old name so ambient mode and tests stay valid.)"""

    chip_clicked = pyqtSignal(str)          # kept for compatibility; nothing emits it now

    _P_ALPHA = (58, 104, 152, 204, 255)          # particle brightness per level
    _L_ALPHA = (20, 42, 72, 108, 158)            # line brightness per level (thin, like the reference)
    _LINE_STYLE = {"track": (1.05, 1.15), "ring": (0.95, 1.05), "tick": (0.75, 0.8), "comet": (1.4, 1.35),
                   "inner": (0.7, 0.6),
                   # Phase 7 filament core
                   "filament": (0.72, 0.66), "hot": (1.0, 1.2), "hair": (0.65, 0.6),
                   # Phase 9: the pulses travelling through the veins
                   "flow": (1.15, 1.35)}

    def __init__(self, face_path: str, assistant_name: str = "JARVIS", parent=None,
                 compact: bool = False, transparent: bool = False):
        super().__init__(face_path, assistant_name, parent)
        from ui_holo import HeartClock, VeinFlow, make_core
        self._compact = compact
        self._transparent = transparent
        if transparent:
            self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(120 if compact else 320, 120 if compact else 320)
        self.telemetry = {"cpu": 0.0, "mem": 0.0}
        self.flags = {"ambient": False}
        quality = str(_read_cfg().get("holo_quality", "high")).lower()
        # Phase 7: the filament core (second reference video) inside the orbits of
        # the Mark LIII.2 core (first video). "holo_style": "particles" = the old one.
        self._sphere = make_core(str(_read_cfg().get("holo_style", "filament")), quality, compact=compact)
        rnd = random.Random(42)
        self._twinkle = [(rnd.random(), rnd.random(), rnd.uniform(0.6, 2.4), rnd.uniform(0, 6.28),
                          rnd.uniform(0.5, 1.4)) for _ in range(0 if compact else 70)]
        self._phase = 0.0
        self._clock = time.monotonic()
        self._r_mult = 1.0
        self._spin = 1.0
        self._frame_no = 0
        self._layer: QPixmap | None = None
        self._layer_key = None
        self._glow_cache: dict = {}
        self._draw_lines_mode = None
        # Phase 9: the heart (the core) and the circulation (the veins)
        self._heartbeat = str(_read_cfg().get("heartbeat", True)).lower() not in ("0", "false", "no", "off")
        self._heart = HeartClock(56.0)
        self._bpm = self._heart.bpm      # mirrors of the heart's clock, for tests / diagnostics
        self._beat = 0.0                 # beats since start
        self._hb = 0.0                   # the heart's light, 0 at rest .. ~1 at the strong pulse
        self._beat_tau = -1.0            # seconds into the current beat
        self._life = list(self._LIFE["IDLE"])           # the eased state × mode profile
        self._voice = 0.0
        self._core_beat = _holo.CoreBeat()
        self._flow = (VeinFlow(self._sphere, seed=31 if compact else 29)
                      if getattr(self._sphere, "has_veins", False) else None)
        self._layer_phase = 0.0
        self._layer_R = 0.0
        self._glow_s = 1.0               # slowly eased state glow
        self._glowk = 1.0
        self._sky_pm: QPixmap | None = None
        self._sky_pm_key = None
        self._sky_t = 0.0

    # -- helpers ------------------------------------------------------------------
    def _c(self, a: float) -> QColor:
        a = max(0, min(255, int(a)))
        return qcol(C.MUTED_C, a) if self.muted else self._live_qcol(a)

    def _targets(self, amp: float) -> tuple[float, float]:
        s = self.state or "IDLE"
        if self.muted:
            return 0.96, 0.35
        if self.speaking:
            return 1.0, 1.25 * self._motion["ring_speed"]      # Phase 9: no voice-driven sphere
        r = {"LISTENING": 1.12, "THINKING": 0.95, "EXECUTING": 0.98}.get(s, 1.0)
        spin = {"THINKING": 2.1, "EXECUTING": 1.8, "LISTENING": 1.15}.get(s, 1.0)
        return r, spin * self._cur_ring_mult * self._motion["ring_speed"]

    # Phase 9 — ALIVE --------------------------------------------------------------
    # Per state × mode: tempo, beat strength, arteries fed per beat, branching,
    # flow speed, drift, core glow, pulse fade (the table lives in ui_holo).
    _LIFE = _holo.LIFE_PROFILES
    _SERIOUS = _holo.SERIOUS_GAIN

    def _life_key(self) -> str:
        if self.muted:
            return "MUTED"
        s = self.state or "IDLE"
        if s in ("ERROR", "WARNING"):
            return s
        if self.speaking:
            return "SPEAKING"
        return s if s in self._LIFE else "IDLE"

    def _life_target(self) -> tuple:
        return _holo.life_profile(self._life_key(), self._mode_name == "serious")

    def _bpm_target(self) -> float:
        return self._life_target()[0]

    def _advance_heart(self, dt: float, now: float) -> None:
        """Every frame: ease the state × mode profile, run the heart's clock, let
        the strong pulse (and its echo) feed the veins, and move the pulses."""
        tgt = self._life_target()
        k = min(1.0, dt * 1.5)
        self._life = [c + (t - c) * k for c, t in zip(self._life, tgt)]
        L = self._life
        v_t = self._amp_disp if self.speaking else self._amp_disp * 0.5
        self._voice += (v_t - self._voice) * min(1.0, dt * 14.0)
        stir = min(1.0, max(0.0, self._spin - 1.0))
        beat, event = self._heart.advance(dt, now, tgt[0], L[1], self._voice, stir, beating=self._heartbeat)
        self._core_beat = beat
        h = self._heart
        self._bpm, self._beat, self._beat_tau, self._hb = h.bpm, h.beat, h.tau, max(0.0, h.light)
        if self._flow is not None:
            if event == "main":
                self._flow.launch(L[2], L[3], L[4], min(1.3, L[1]), L[7])
            elif event == "echo":
                self._flow.launch(max(1.0, L[2] * 0.34), L[3] * 0.6, L[4] * 0.9, L[1] * 0.5, L[7])
            self._flow.step(dt, L[5], L[4])

    def _palette(self) -> tuple:
        base = self._vivid(self._c(255))
        br, bg_, bb = base.red(), base.green(), base.blue()
        return (br, bg_, bb), (min(255, br + 95), min(255, bg_ + 90), min(255, bb + 80))

    def _paint_flow(self, p: QPainter, cx: float, cy: float) -> None:
        """Phase 9 — the pulses travelling through the veins, every frame (smooth at
        60 fps), projected with the cached layer's own rotation so they run exactly
        along the veins drawn in it. Additive."""
        if self._flow is None or self._layer_R <= 0:
            return
        lines, heads = self._sphere.flow_frame(self._flow, self._layer_phase, cx, cy, self._layer_R,
                                               beat=self._core_beat)
        if not lines and not heads:
            return
        gk = self._glowk * self._life[6]
        cols = self._palette()
        self._draw_lines(p, lines, gk, cols)
        (br, bg_, bb), (lr, lg, lb) = cols
        hr, hg, hbl = min(255, lr + 50), min(255, lg + 45), min(255, lb + 40)
        sc = 0.8 if self._compact else 1.0
        for L, big, xs, ys in heads:
            a = self._P_ALPHA[L] * min(1.2, gk)
            pts = QPolygonF([QPointF(x, y) for x, y in zip(xs.tolist(), ys.tolist())])
            if big:                                       # a soft halo, then the bright head
                pen = QPen(QColor(br, bg_, bb, max(0, min(255, int(a * 0.24)))), 6.5 * sc)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawPoints(pts)
                pen = QPen(QColor(hr, hg, hbl, max(0, min(255, int(a * 0.95)))), 2.3 * sc)
            else:                                         # a quiet drift particle
                pen = QPen(QColor(lr, lg, lb, max(0, min(255, int(a * 0.6)))), 1.6 * sc)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawPoints(pts)

    def _paint_core(self, p: QPainter, cx: float, cy: float) -> None:
        """Phase 9 — the brain, every frame: its tangle of veins and the dust
        orbiting it (beating, stirred while thinking, answering the voice), the
        heart's own light (it swells, contracts and pauses with the beat) and the
        small energy impulse each strong pulse sends out. Additive."""
        R0 = self._layer_R
        if R0 <= 0:
            return
        beat = self._core_beat
        gk = self._glowk * self._life[6]
        cols = self._palette()
        fr = self._sphere.frame(self._phase, cx, cy, R0, spin=1.0, excite=max(0.0, self._spin - 1.0),
                                parts="core", beat=beat)
        self._draw_points(p, fr.points, gk, cols)
        self._draw_lines(p, fr.lines, gk, cols)
        size, light = fr.brain
        c = self._c(255)
        key = (tuple(int(v) // 12 for v in (c.red(), c.green(), c.blue())), self.muted)
        gr = int(max(24, R0 * 0.5)) // 8 * 8                       # centre only; few cached sizes
        op = max(0.0, min(1.0, (0.20 + 0.50 * light) * min(1.3, gk)))
        if op > 0.01:
            rr = gr * max(0.6, 1.0 + 0.12 * size)
            pm = self._glow(gr, key)
            p.setOpacity(op)
            p.drawPixmap(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), pm, QRectF(0, 0, pm.width(), pm.height()))
            p.setOpacity(1.0)
        if 0.0 <= beat.impulse < _holo.IMPULSE_LIFE:            # the energy impulse (core region only)
            front, fade = _holo.impulse_front(beat.impulse)
            lr, lg, lb = cols[1]
            a = max(0, min(255, int(40 * fade * min(1.3, beat.strength) * min(1.3, gk))))
            if a > 0:                                   # a thin, soft ring of light
                ro = R0 * 0.82 * (front + 0.10)
                mid = front / (front + 0.10)
                g = QRadialGradient(QPointF(cx, cy), ro)
                g.setColorAt(0.0, QColor(lr, lg, lb, 0))
                g.setColorAt(max(0.0, mid - 0.14), QColor(lr, lg, lb, 0))
                g.setColorAt(mid, QColor(lr, lg, lb, a))
                g.setColorAt(mid + (1.0 - mid) * 0.45, QColor(lr, lg, lb, a // 4))
                g.setColorAt(1.0, QColor(lr, lg, lb, 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(g))
                p.drawEllipse(QPointF(cx, cy), ro, ro)

    @staticmethod
    def _vivid(c: QColor) -> QColor:
        """Same hue, brighter: deep reds/blues read as light, like a hologram."""
        m = max(c.red(), c.green(), c.blue(), 1)
        k = min(1.7, 240.0 / m)
        return QColor(min(255, int(c.red() * k)), min(255, int(c.green() * k)), min(255, int(c.blue() * k)),
                      c.alpha())

    def _glow(self, radius: int, key) -> QPixmap:
        """Cached additive nebula glow for the core (per colour bucket + size)."""
        ck = (radius, key)
        pm = self._glow_cache.get(ck)
        if pm is not None:
            return pm
        if len(self._glow_cache) > 24:
            self._glow_cache.clear()
        size = radius * 2
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = self._c(255)
        hot = QColor(min(255, base.red() + 120), min(255, base.green() + 110), min(255, base.blue() + 90))
        g = QRadialGradient(QPointF(radius, radius), radius)
        base = self._vivid(base)
        hot = QColor(min(255, base.red() + 110), min(255, base.green() + 100), min(255, base.blue() + 90))
        g.setColorAt(0.00, QColor(hot.red(), hot.green(), hot.blue(), 255))
        g.setColorAt(0.04, QColor(hot.red(), hot.green(), hot.blue(), 215))
        g.setColorAt(0.12, QColor(base.red(), base.green(), base.blue(), 150))
        g.setColorAt(0.28, QColor(base.red(), base.green(), base.blue(), 78))
        g.setColorAt(0.52, QColor(base.red(), base.green(), base.blue(), 28))
        g.setColorAt(0.78, QColor(base.red(), base.green(), base.blue(), 8))
        g.setColorAt(1.00, QColor(0, 0, 0, 0))
        q.setPen(Qt.PenStyle.NoPen)
        q.setBrush(QBrush(g))
        q.drawEllipse(QPointF(radius, radius), radius, radius)
        q.end()
        self._glow_cache[ck] = pm
        return pm

    def _draw_segments(self, q: QPainter, x0, y0, x1, y1) -> None:
        lines = [QLineF(a, b, c, d) for a, b, c, d in zip(x0.tolist(), y0.tolist(), x1.tolist(), y1.tolist())]
        if not lines:
            return
        mode = self._draw_lines_mode
        if mode in (None, "list"):
            try:
                q.drawLines(lines)
                self._draw_lines_mode = "list"
                return
            except TypeError:
                self._draw_lines_mode = "loop"
        for ln in lines:
            q.drawLine(ln)

    # -- render one sphere frame into a transparent layer -----------------------
    def _render_layer(self, W: int, H: int) -> QPixmap:
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(W * dpr), int(H * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        q.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        cx, cy = W / 2.0, H / 2.0
        # Phase 9: the shell only — veins, hairs, orbits and the steady nebula glow. It
        # never beats and never follows the voice; the brain is painted live (_paint_core).
        R0 = min(W, H) * (0.33 if self._compact else 0.34) * self._r_mult
        fr = self._sphere.frame(self._phase, cx, cy, R0, spin=1.0, amp=0.0,
                                excite=max(0.0, self._spin - 1.0), parts="shell")
        self._layer_phase, self._layer_R = self._phase, R0
        cols = self._palette()
        br, bg_, bb = cols[0]

        # nebula core glow (cached radial glow; steady — its faint tail reaches past the sphere)
        ccx, ccy, cr, ci = fr.core
        grad_r = int(max(24, R0 * 1.2)) // 4 * 4
        key = (tuple(int(c) // 12 for c in (br, bg_, bb)), self.muted)
        q.setOpacity(max(0.4, min(1.0, 0.3 + ci * self._glowk * 0.8)))
        q.drawPixmap(QPointF(ccx - grad_r, ccy - grad_r), self._glow(grad_r, key))
        q.setOpacity(1.0)
        self._draw_points(q, fr.points, self._glowk, cols)    # the particle style's cloud (filament: none)
        self._draw_lines(q, fr.lines, self._glowk, cols)
        q.end()
        return pm

    def _draw_points(self, q: QPainter, points, gk: float, cols) -> None:
        (br, bg_, bb), (lr, lg, lb) = cols
        for L, big, xs, ys in points:
            a = self._P_ALPHA[L] * (1.0 if big else 0.85) * min(1.25, gk)
            col = QColor(lr, lg, lb) if (big or L >= 2) else QColor(br, bg_, bb)
            col.setAlpha(max(0, min(255, int(a))))
            pen = QPen(col, (2.6 if big else 1.5) * (0.8 if self._compact else 1.0))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            q.setPen(pen)
            q.drawPoints(QPolygonF([QPointF(x, y) for x, y in zip(xs.tolist(), ys.tolist())]))

    def _draw_lines(self, q: QPainter, lines, gk: float, cols) -> None:
        """Rings, ticks, comets and veins: a soft glow pass under the bright ones
        (comets; Phase 9: the brain's veins and the pulses), then a crisp pass."""
        (br, bg_, bb), (lr, lg, lb) = cols
        for L, kind, x0, y0, x1, y1 in lines:
            width, gain = self._LINE_STYLE.get(kind, (1.0, 0.8))
            a = self._L_ALPHA[L] * gain * min(1.2, gk)
            if kind == "comet":
                gc = QColor(br, bg_, bb, max(0, min(255, int(a * 0.25))))
                q.setPen(QPen(gc, width * 3.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                self._draw_segments(q, x0, y0, x1, y1)
            elif kind in ("flow", "hot") and L >= 2:
                gc = QColor(br, bg_, bb, max(0, min(255, int(a * 0.24))))
                q.setPen(QPen(gc, width * 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                self._draw_segments(q, x0, y0, x1, y1)
            col = QColor(lr, lg, lb) if kind in ("comet", "hot", "flow") or L >= 4 else QColor(br, bg_, bb)
            col.setAlpha(max(0, min(255, int(a))))
            q.setPen(QPen(col, width * (0.85 if self._compact else 1.0), Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.FlatCap))
            self._draw_segments(q, x0, y0, x1, y1)

    # -- paint --------------------------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        if W < 10 or H < 10:
            p.end()
            return
        bd = None
        if self._transparent:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        else:
            bd = _backdrop_of(self)
            if bd is not None and bd.sky is not None:
                # Phase 8: the live, rotating sky - redrawn ~30 times a second into a
                # cached pixmap (stars move < 0.5 px per redraw here) and blitted every frame
                dpr = self.devicePixelRatioF()
                now_s = time.monotonic()
                if (self._sky_pm is None or self._sky_pm_key != (W, H, dpr)
                        or (bd.sky.omega > 0 and now_s - self._sky_t >= 0.033)):
                    pm = QPixmap(max(1, int(W * dpr)), max(1, int(H * dpr)))
                    pm.setDevicePixelRatio(dpr)
                    pm.fill(QColor(0, 0, 2))
                    q = QPainter(pm)
                    bd.paint_sky(q, self)
                    q.end()
                    self._sky_pm, self._sky_pm_key, self._sky_t = pm, (W, H, dpr), now_s
                p.drawPixmap(0, 0, self._sky_pm)
            elif bd is not None:
                bd.paint_under(p, self, False)
            else:
                p.fillRect(self.rect(), QColor(2, 4, 8))
        cx, cy = W / 2.0, H / 2.0
        R = min(W, H) * 0.5

        # a few stars that twinkle (the old still backdrop only; the live sky twinkles itself)
        if self._twinkle and not self._transparent and (bd is None or bd.sky is None):
            tnow = time.monotonic()
            for sx, sy, w_, ph, sz in self._twinkle:
                a = int(40 + 180 * max(0.0, math.sin(tnow * w_ + ph)) ** 3)
                pen = QPen(QColor(225, 235, 255, a), sz)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawPoint(QPointF(sx * W, sy * H))

        # deep-space vignette behind the sphere, like the reference. Phase 8 (live
        # sky): only the planet itself hides the stars behind it, so space is
        # visible right up to its edge.
        if bd is not None and bd.sky is not None:
            vr = min(W, H) * 0.34 * self._r_mult * 1.18
            vig = QRadialGradient(QPointF(cx, cy), vr)
            vig.setColorAt(0.0, QColor(0, 0, 0, 240))
            vig.setColorAt(0.80, QColor(0, 0, 0, 215))
            vig.setColorAt(1.0, QColor(0, 0, 0, 0))
        else:
            vr = R * 1.02
            vig = QRadialGradient(QPointF(cx, cy), vr)
            vig.setColorAt(0.0, QColor(0, 0, 0, 235 if not self._transparent else 215))
            vig.setColorAt(0.72, QColor(0, 0, 0, 170 if not self._transparent else 150))
            vig.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(vig))
        p.drawEllipse(QPointF(cx, cy), vr, vr)

        # ease state targets, advance the rotation phase
        now = time.monotonic()
        dt = max(0.0, min(0.1, now - self._clock))
        self._clock = now
        r_t, s_t = self._targets(self._amp_disp)
        # Phase 8: slower, smoother easing between listening / speaking / thinking
        self._r_mult += (r_t - self._r_mult) * min(1.0, dt * 1.8)
        self._spin += (s_t - self._spin) * min(1.0, dt * 1.2)
        self._phase += dt * self._spin
        # glow: the STATE part eases over ~0.5 s. Phase 9: the voice no longer lights
        # the whole sphere — speaking lights the core (its _LIFE glow) instead
        g_t = self._cur_glow_mult * self._motion["halo_boost"] * (1.1 if self.speaking else 1.0)
        if self.muted:
            g_t = 0.6
        self._glow_s += (g_t - self._glow_s) * min(1.0, dt * 2.2)
        self._glowk = max(0.55, min(1.9, self._glow_s))
        self._advance_heart(dt, now)

        # re-render the sphere every other frame (≈30 fps); reuse it in between.
        # Phase 7: adaptive — if a render costs too much (slow GPU/CPU), render
        # less often, so the GUI thread never starves the audio threads.
        self._frame_no += 1
        key = (W, H)
        every = getattr(self, "_every", 2)
        if self._layer is None or self._layer_key != key or self._frame_no % every == 0:
            t0 = time.perf_counter()
            self._layer = self._render_layer(W, H)
            self._layer_key = key
            ms = (time.perf_counter() - t0) * 1000.0
            ema = getattr(self, "_render_ms", ms) * 0.9 + ms * 0.1
            self._render_ms = ema
            self._every = 2 if ema < 14 else 3 if ema < 24 else 4
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        p.drawPixmap(0, 0, self._layer)
        # Phase 9, every frame: the pulses in the veins, then the beating brain on top
        self._paint_flow(p, cx, cy)
        self._paint_core(p, cx, cy)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.end()

    # -- mouse: the core has no controls any more; clicks fall through --------------
    def mousePressEvent(self, e):
        super().mousePressEvent(e)


# ═════════════════════════════════════════════════════════════════════════════
# Small painted widgets
# ═════════════════════════════════════════════════════════════════════════════

class StatStrip(QWidget):
    """Top-bar stat cells: label over value, painted (crisp at any DPI)."""

    def __init__(self, cells: list[str], parent=None):
        super().__init__(parent)
        self._cells = {k: ("--", "") for k in cells}
        self._order = cells
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set(self, key: str, value: str, unit: str = ""):
        if self._cells.get(key) != (value, unit):
            self._cells[key] = (value, unit)
            self.update()

    def sizeHint(self):
        return QSize(len(self._order) * 58, 34)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = len(self._order)
        w = self.width() / max(1, n)
        for i, k in enumerate(self._order):
            v, u = self._cells[k]
            x = i * w
            p.setFont(hfont(10, QFont.Weight.Normal, spacing=1.2))
            p.setPen(TEXT_LO)
            p.drawText(QRectF(x, 1, w, 14), Qt.AlignmentFlag.AlignCenter, k)
            p.setFont(hfont(14, QFont.Weight.DemiBold, mono=False))
            warn = False
            try:
                warn = k in ("CPU", "RAM", "DSK") and float(v) >= 85
            except ValueError:
                pass
            p.setPen(qcol(C.RED) if warn else _Accent.c(255, 0.25))
            txt = v + (f" {u}" if u else "")
            p.drawText(QRectF(x, 14, w, 20), Qt.AlignmentFlag.AlignCenter, txt)
            if i:
                p.setPen(QPen(QColor(255, 255, 255, 18), 1))
                p.drawLine(QPointF(x, 6), QPointF(x, 28))
        p.end()


class StatusChips(QWidget):
    """Part One top-bar status: the ACTIVE brain model/provider, the live voice
    model, the conversational voice state, and the capture indicator (EYE —
    lit only while a screen/camera frame is being captured and answered)."""

    ORDER = ("BRAIN", "LIVE", "VOICE", "EYE")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vals = {k: "" for k in self.ORDER}
        self.setMinimumHeight(34)
        self.setMinimumWidth(300)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setToolTip("BRAIN = model answering reasoning requests (RUNTIME = switched by voice, resets on "
                        "restart) · LIVE = voice model · VOICE = conversation state · EYE = capturing the screen")

    def set(self, key: str, value: str) -> None:
        key = str(key).upper()
        if key in self._vals and self._vals[key] != value:
            self._vals[key] = str(value or "")[:60]
            self.update()

    def value(self, key: str) -> str:
        return self._vals.get(str(key).upper(), "")

    def sizeHint(self):
        return QSize(360, 34)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = float(self.width())
        rows = ((("BRAIN", self._vals["BRAIN"]),), (("VOICE", self._vals["VOICE"]), ("LIVE", self._vals["LIVE"])))
        for r, row in enumerate(rows):
            y = 2 + r * 16
            x = 0.0
            for k, v in row:
                p.setFont(hfont(9.5, QFont.Weight.Normal, spacing=1.2))
                p.setPen(TEXT_LO)
                p.drawText(QRectF(x, y, 46, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, k)
                p.setFont(hfont(11, QFont.Weight.DemiBold))
                p.setPen(qcol(C.RED) if (k == "VOICE" and v == "ERROR") else _Accent.c(255, 0.25))
                span = (w - x - 46) if len(row) == 1 else (w / 2 - 46 if x == 0 else w - x - 46)
                fm = p.fontMetrics()
                txt = fm.elidedText(v or "--", Qt.TextElideMode.ElideMiddle, int(max(20, span)))
                p.drawText(QRectF(x + 46, y, span, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, txt)
                x = w / 2
        eye = self._vals["EYE"]
        if eye:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcol(C.RED))
            p.drawEllipse(QPointF(w - 8, 9), 4.5, 4.5)
            p.setFont(hfont(9.5, QFont.Weight.DemiBold, spacing=1.0))
            p.setPen(qcol(C.RED))
            p.drawText(QRectF(w - 120, 2, 104, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       "EYE " + eye.replace("●", "").strip())
        p.end()


class TelemetryRow(QWidget):
    """Drop-in for ui.MetricBar (same set_value API): LABEL  [ value unit ]."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._label = label
        self._value = 0.0
        self._text = "--"
        self.setFixedHeight(34)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, float(pct)))
        self._text = str(text)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        lw = W * 0.40
        p.setFont(hfont(11, QFont.Weight.Normal, spacing=1.6))
        p.setPen(TEXT_MID)
        p.drawText(QRectF(0, 0, lw - 10, H), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   self._label.upper())
        box = QRectF(lw, 3, W - lw - 1, H - 6)
        p.setBrush(QBrush(QColor(4, 16, 26, 170)))
        p.setPen(QPen(_Accent.c(80), 1))
        p.drawRoundedRect(box, 3, 3)
        m = re.match(r"^\s*([-\d.]+)\s*(.*)$", self._text)
        val, unit = (m.group(1), m.group(2)) if m else (self._text, "")
        hot = self._value > 85
        p.setFont(hfont(14, QFont.Weight.DemiBold))
        p.setPen(qcol(C.RED) if hot else _Accent.c(255, 0.3))
        p.drawText(box.adjusted(10, 0, -8, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, val)
        p.setFont(hfont(10, QFont.Weight.Normal, spacing=1.0))
        p.setPen(TEXT_LO)
        p.drawText(box.adjusted(10, 0, -8, -2), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, unit)
        # fill line
        fw = (box.width() - 4) * self._value / 100.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(qcol(C.RED, 200) if hot else _Accent.c(200)))
        p.drawRect(QRectF(box.left() + 2, box.bottom() - 3, fw, 2))
        p.end()


class LinkList(QWidget):
    """Right-aligned quick-launch labels (like the design's LINKS column)."""
    activated = pyqtSignal(str)

    def __init__(self, items: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self._items = items
        self._hover = -1
        self._row = 22
        self.setMouseTracking(True)
        self.setFixedHeight(self._row * len(items) + 4)

    def _idx(self, y: float) -> int:
        i = int(y // self._row)
        return i if 0 <= i < len(self._items) else -1

    def mouseMoveEvent(self, e):
        i = self._idx(e.position().y())
        if i != self._hover:
            self._hover = i
            self.setCursor(Qt.CursorShape.PointingHandCursor if i >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self._hover = -1
        self.update()

    def mousePressEvent(self, e):
        i = self._idx(e.position().y())
        if i >= 0:
            self.activated.emit(self._items[i][1])

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for i, (label, _t) in enumerate(self._items):
            r = QRectF(0, i * self._row, self.width(), self._row)
            hov = i == self._hover
            if hov:
                g = QLinearGradient(r.left(), 0, r.right(), 0)
                g.setColorAt(0.0, _Accent.c(0))
                g.setColorAt(1.0, _Accent.c(45))
                p.fillRect(r, g)
                p.setPen(QPen(_Accent.c(230), 2))
                p.drawLine(QPointF(r.right() - 1, r.top() + 4), QPointF(r.right() - 1, r.bottom() - 4))
            p.setFont(hfont(11.5, QFont.Weight.DemiBold if hov else QFont.Weight.Normal, spacing=1.8))
            p.setPen(_Accent.c(255, 0.3) if hov else TEXT_MID)
            p.drawText(r.adjusted(0, 0, -10, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       label.upper())
        p.end()


class DriveGauges(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.disks: list[dict] = []
        self.setFixedHeight(104)

    def set_disks(self, disks: list[dict]):
        if disks != self.disks:
            self.disks = disks
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        disks = self.disks or []
        if not disks:
            p.setFont(hfont(11))
            p.setPen(TEXT_LO)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Reading drives…")
            p.end()
            return
        n = len(disks)
        cw = self.width() / n
        for i, d in enumerate(disks):
            cx = cw * i + cw / 2
            cy = 40
            r = min(30.0, cw * 0.32)
            p.setBrush(QBrush(QColor(3, 12, 20, 200)))
            p.setPen(QPen(_Accent.c(45), 5))
            p.drawEllipse(QPointF(cx, cy), r, r)
            pct = d.get("pct", 0) / 100.0
            p.setBrush(Qt.BrushStyle.NoBrush)
            hot = pct > 0.9
            p.setPen(QPen(qcol(C.RED, 230) if hot else _Accent.c(230), 5, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.FlatCap))
            p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), 90 * 16, int(-360 * pct * 16))
            p.setPen(QPen(_Accent.c(120), 1))
            p.drawEllipse(QPointF(cx, cy), r - 7, r - 7)
            p.setFont(hfont(r * 0.75, QFont.Weight.DemiBold))
            p.setPen(TEXT_HI)
            p.drawText(QRectF(cx - r, cy - r, 2 * r, 2 * r), Qt.AlignmentFlag.AlignCenter, d.get("label", "?"))
            free = d.get('free_gb', 0)
            p.setFont(hfont(12, QFont.Weight.DemiBold))
            p.setPen(_Accent.c(255, 0.3))
            p.drawText(QRectF(cx - cw / 2, cy + r + 4, cw, 15), Qt.AlignmentFlag.AlignCenter,
                       f"{free:.0f} G" if free >= 100 else f"{free:.1f} G")
            p.setFont(hfont(9, QFont.Weight.Normal, spacing=1.2))
            p.setPen(TEXT_LO)
            p.drawText(QRectF(cx - cw / 2, cy + r + 18, cw, 12), Qt.AlignmentFlag.AlignCenter, "FREE")
        p.end()


class TaskList(QWidget):
    """JARVIS protocols (tasks) — state dot, request, progress."""

    _STATE_COL = {"COMPLETED": "#00ff88", "FAILED": "#ff3355", "CANCELLED": "#64748b",
                  "WAITING_FOR_PERMISSION": "#ffcc00", "WAITING_FOR_INPUT": "#ffcc00"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tasks: list[dict] = []
        self.setMinimumHeight(96)

    def _state_col(self, st: str) -> str:
        """Phase 8: Friendly = blue shades (done = light blue, waiting = bright
        blue); FAILED stays red, CANCELLED grey. Serious keeps the old colours."""
        if _friendly():
            return {"COMPLETED": _Accent.hex(0.45), "FAILED": "#ff3355", "CANCELLED": "#64748b",
                    "WAITING_FOR_PERMISSION": _Accent.hex(0.65),
                    "WAITING_FOR_INPUT": _Accent.hex(0.65)}.get(st, _Accent.hex(0.2))
        return self._STATE_COL.get(st, _Accent.hex(0.2))

    def set_tasks(self, tasks: list[dict]):
        self.tasks = tasks
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W = self.width()
        rows = list(reversed(self.tasks))[: max(1, self.height() // 26)]
        if not rows:
            p.setFont(hfont(12))
            p.setPen(TEXT_MID)
            p.drawText(QRectF(4, 2, W - 8, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       "- no active protocols")
            p.setFont(hfont(11))
            p.setPen(TEXT_LO)
            p.drawText(QRectF(4, 24, W - 8, 40), Qt.TextFlag.TextWordWrap,
                       "Ask for a multi-step job — e.g. \"find my resume PDF and move it to Documents\".")
            p.end()
            return
        fm = QFontMetrics(hfont(12))
        for i, t in enumerate(rows):
            y = i * 26
            st = t.get("state", "")
            col = QColor(self._state_col(st))
            active = st not in ("COMPLETED", "FAILED", "CANCELLED")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(col))
            p.drawEllipse(QPointF(8, y + 12), 3.5 if active else 3, 3.5 if active else 3)
            if active and (int(time.time() * 2) % 2 == 0):
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(col, 1))
                p.drawEllipse(QPointF(8, y + 12), 7, 7)
            steps = t.get("steps") or []
            done = len(t.get("completed_steps") or [])
            prog = f"{done}/{len(steps)}" if steps else ""
            p.setFont(hfont(12, QFont.Weight.DemiBold if active else QFont.Weight.Normal))
            p.setPen(TEXT_HI if active else TEXT_MID)
            txt = fm.elidedText(str(t.get("request", "")), Qt.TextElideMode.ElideRight, int(W - 90))
            p.drawText(QRectF(20, y, W - 90, 24), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, txt)
            p.setFont(hfont(10, QFont.Weight.Normal, spacing=0.8))
            p.setPen(col)
            short = {"WAITING_FOR_PERMISSION": "CONFIRM?", "WAITING_FOR_INPUT": "ASKING",
                     "COMPLETED": "DONE"}.get(st, st[:9])
            p.drawText(QRectF(W - 88, y, 86, 24), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       f"{prog}  {short}".strip())
        p.end()


class WeatherView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = None
        self.error = ""
        self.setMinimumHeight(230)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

    def set_data(self, data, error):
        if (data, error) != (self.data, self.error):
            self.data, self.error = data, error
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W = self.width()
        d = self.data
        if not d:
            p.setFont(hfont(12))
            p.setPen(TEXT_MID)
            p.drawText(QRectF(0, 0, W, 60), Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                       self.error or "Acquiring atmospheric data…")
            p.end()
            return
        p.setFont(hfont(13, QFont.Weight.DemiBold))
        p.setPen(TEXT_HI)
        p.drawText(QRectF(0, 0, W, 18), Qt.AlignmentFlag.AlignLeft, d["city"])
        p.setFont(hfont(10))
        p.setPen(TEXT_LO)
        p.drawText(QRectF(0, 18, W, 14), Qt.AlignmentFlag.AlignLeft, f"Updated {d['updated']}  ·  wttr.in")
        p.setFont(hfont(44, QFont.Weight.Light))
        p.setPen(_Accent.c(255, 0.35))
        tstr = f"{d['temp']}°C"
        p.drawText(QRectF(0, 34, W, 56), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, tstr)
        tw = QFontMetrics(hfont(44, QFont.Weight.Light)).horizontalAdvance(tstr)
        p.setFont(hfont(14, QFont.Weight.DemiBold))
        p.setPen(_Accent.c(255, 0.15))
        p.drawText(QRectF(tw + 12, 50, W - tw - 12, 30), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   d["desc"])
        y = 96
        p.setPen(QPen(QColor(255, 255, 255, 25), 1))
        p.drawLine(QPointF(0, y - 4), QPointF(W, y - 4))
        rows = [("Humidity", f"{d['humidity']}%"), ("Feels like", f"{d['feels']}°C"),
                ("Wind", d["wind"]), ("Sunrise / Sunset", f"{d['sunrise']} / {d['sunset']}"),
                ("Precipitation", f"{d['precip']} mm"), ("Pressure", f"{d['pressure']} hPa"),
                ("Moon", d["moon"])]
        # adapt to the space we were given: forecast first, then trim detail rows
        n_days = len(d.get("days", []))
        room = self.height() - y - 12 - n_days * 20
        rows = rows[: max(3, min(len(rows), int(room // 19)))]
        for label, val in rows:
            p.setFont(hfont(11.5))
            p.setPen(TEXT_MID)
            p.drawText(QRectF(0, y, W * 0.45, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"{label}:")
            p.setFont(hfont(11.5, QFont.Weight.DemiBold))
            p.setPen(TEXT_HI)
            p.drawText(QRectF(W * 0.45, y, W * 0.55, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       str(val))
            y += 19
        y += 4
        p.setPen(QPen(QColor(255, 255, 255, 25), 1))
        p.drawLine(QPointF(0, y), QPointF(W, y))
        y += 6
        for i, day in enumerate(d.get("days", [])):
            try:
                name = "Today" if i == 0 else datetime.strptime(day["date"], "%Y-%m-%d").strftime("%a %d")
            except Exception:
                name = day.get("date", "")
            p.setFont(hfont(11.5, QFont.Weight.DemiBold))
            p.setPen(_Accent.c(255, 0.3))
            p.drawText(QRectF(0, y, W * 0.3, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"{name}:")
            p.setPen(TEXT_HI)
            p.drawText(QRectF(W * 0.3, y, W * 0.25, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"{day['max']}° / {day['min']}°")
            p.setFont(hfont(11))
            p.setPen(TEXT_MID)
            fm = QFontMetrics(hfont(11))
            p.drawText(QRectF(W * 0.55, y, W * 0.45, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(day.get("desc", ""), Qt.TextElideMode.ElideRight, int(W * 0.45)))
            y += 20
        p.end()


class UplinkView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.info: dict = {}
        self._peak = 1.0
        self.setFixedHeight(78)

    def set_info(self, info: dict):
        self.info = info
        self._peak = max(self._peak * 0.98, info.get("down", 0) + info.get("up", 0), 64 * 1024)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W = self.width()
        i = self.info
        ssid = i.get("ssid") or ("Wired / unknown" if i.get("lan") not in (None, "offline") else "Offline")
        p.setFont(hfont(11, spacing=1.0))
        p.setPen(TEXT_LO)
        p.drawText(QRectF(0, 0, 40, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "SSID")
        p.setFont(hfont(12, QFont.Weight.DemiBold))
        p.setPen(TEXT_HI)
        p.drawText(QRectF(40, 0, W * 0.55, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, ssid)
        p.setFont(hfont(12))
        p.drawText(QRectF(W * 0.45, 0, W * 0.55, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"{i.get('lan', '--')}  LAN")
        sig = i.get("signal")
        p.setPen(TEXT_MID)
        p.setFont(hfont(11))
        p.drawText(QRectF(0, 20, W, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"Signal {sig}%" if sig is not None else "Signal --")
        box = QRectF(0, 42, W, 32)
        p.setBrush(QBrush(QColor(4, 16, 26, 170)))
        p.setPen(QPen(_Accent.c(90), 1))
        p.drawRoundedRect(box, 3, 3)
        dv, du = _fmt_rate(i.get("down", 0))
        uv, uu = _fmt_rate(i.get("up", 0))
        p.setFont(hfont(13, QFont.Weight.DemiBold))
        p.setPen(_Accent.c(255, 0.3))
        p.drawText(box.adjusted(10, 0, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{uv} {uu}  UP")
        p.drawText(box.adjusted(0, 0, -10, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"DOWN  {dv} {du}")
        bw = box.width() * 0.34
        bx = box.center().x() - bw / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(_Accent.c(40)))
        p.drawRoundedRect(QRectF(bx, box.center().y() - 3, bw, 6), 3, 3)
        frac = min(1.0, i.get("down", 0) / max(1.0, self._peak))
        p.setBrush(QBrush(_Accent.c(230)))
        p.drawRoundedRect(QRectF(bx, box.center().y() - 3, bw * max(0.04, frac), 6), 3, 3)
        p.end()


class VuMeter(QWidget):
    def __init__(self, hud: HudCanvas, parent=None):
        super().__init__(parent)
        self._hud = hud
        self.setFixedSize(120, 22)
        t = QTimer(self)
        t.timeout.connect(self.update)
        t.start(50)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        amp = self._hud._amp_disp
        n = 18
        bw = self.width() / n
        for i in range(n):
            lvl = (i + 1) / n
            on = amp * 1.3 >= lvl * 0.9 or i == 0
            p.fillRect(QRectF(i * bw + 1, 4, bw - 2, self.height() - 8),
                       _Accent.c(230 if on else 40) if lvl < 0.8 else (qcol(C.ACC2, 230 if on else 40)))
        p.end()


# ═════════════════════════════════════════════════════════════════════════════
# Window chrome
# ═════════════════════════════════════════════════════════════════════════════

class TopBar(QWidget):
    """Status strip + frameless-window drag area."""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self._win = win
        self.setFixedHeight(46)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        _sky_opaque(self, p)
        r = QRectF(self.rect())
        paint_glass(p, self, r, radius=0, strength=1.1, brackets=False)
        g = QLinearGradient(0, 0, r.width(), 0)
        g.setColorAt(0.0, _Accent.c(0))
        g.setColorAt(0.5, _Accent.c(170))
        g.setColorAt(1.0, _Accent.c(0))
        p.setPen(QPen(QBrush(g), 1.2))
        p.drawLine(QPointF(0, r.bottom() - 0.5), QPointF(r.width(), r.bottom() - 0.5))
        # emblem
        cx, cy, s = 26.0, r.height() / 2, 11.0
        hexp = QPainterPath()
        for k in range(6):
            a = math.radians(60 * k + 30)
            pt = QPointF(cx + s * math.cos(a), cy + s * math.sin(a))
            hexp.moveTo(pt) if k == 0 else hexp.lineTo(pt)
        hexp.closeSubpath()
        p.setPen(QPen(_Accent.c(255), 1.6))
        p.setBrush(QBrush(_Accent.c(40)))
        p.drawPath(hexp)
        p.setBrush(QBrush(_Accent.c(255)))
        p.drawEllipse(QPointF(cx, cy), 3.2, 3.2)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._win.windowHandle() is not None:
            self._win.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, e):
        self._win._cc.toggle_max()


def _chrome_btn(text: str, tip: str, danger: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setFixedSize(34, 28)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setToolTip(tip)
    b.setFont(hfont(13))
    hov = "rgba(255,51,85,0.55)" if danger else "rgba(255,255,255,0.10)"
    b.setStyleSheet(f"""
        QPushButton {{ background: transparent; color: #94a3b8; border: none; border-radius: 4px; }}
        QPushButton:hover {{ background: {hov}; color: #ffffff; }}
    """)
    return b


# ═════════════════════════════════════════════════════════════════════════════
# Ambient mode (floating reactor)
# ═════════════════════════════════════════════════════════════════════════════

class AmbientOrb(QWidget):
    """Small always-on-top reactor that mirrors the main HUD. Drag to move,
    double-click (or F10) to return to the Command Center."""

    def __init__(self, main_hud: ArcReactorHud, on_restore):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("JARVIS — Ambient")
        self._main = main_hud
        self._restore = on_restore
        self.resize(250, 250)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.hud = ArcReactorHud("", main_hud._assistant_name, self, compact=True, transparent=True)
        self.hud.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.hud)
        self._sync = QTimer(self)
        self._sync.timeout.connect(self._mirror)
        self._sync.start(30)
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - 270, scr.bottom() - 290)

    def _mirror(self):
        m, h = self._main, self.hud
        h.state, h.speaking, h.muted = m.state, m.speaking, m.muted
        h._mode_name, h._motion = m._mode_name, m._motion
        h._assistant_name = m._assistant_name
        if m._amp_disp > h._live_amp:
            h._live_amp = m._amp_disp
        h.telemetry = m.telemetry

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.windowHandle() is not None:
            self.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, e):
        self._restore()


# ═════════════════════════════════════════════════════════════════════════════
# Builder — assembles the Command Center onto MainWindow
# ═════════════════════════════════════════════════════════════════════════════

_LINE_EDIT_QSS = """
QLineEdit {{
    background: rgba(4, 16, 26, 0.72); color: #e2f1f8;
    border: 1px solid {b}; border-radius: 6px; padding: 6px 12px;
    selection-background-color: {a};
}}
QLineEdit:focus {{ border: 1px solid {a}; background: rgba(6, 22, 36, 0.85); }}
"""

_TEXT_QSS = """
QTextEdit {{
    background: transparent; color: #cfe6f2; border: none; padding: 2px;
    selection-background-color: {a};
}}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; border: none; }}
QScrollBar::handle:vertical {{ background: {b}; border-radius: 3px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; border: none; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


def _label(text: str, px: float, weight=QFont.Weight.Normal, color: str = "#94a3b8",
           spacing: float = 0.0, align=Qt.AlignmentFlag.AlignLeft) -> QLabel:
    l = QLabel(text)
    l.setFont(hfont(px, weight, spacing=spacing))
    l.setStyleSheet(f"color: {color}; background: transparent;")
    l.setAlignment(align | Qt.AlignmentFlag.AlignVCenter)
    return l


class CommandCenter:
    """Owns the new layout's widgets and timers; MainWindow keeps a reference
    as win._cc. Attribute names on `win` match the classic layout."""

    DEFAULT_LINKS = [("YouTube", "https://www.youtube.com"), ("Google", "https://www.google.com"),
                     ("GitHub", "https://github.com"), ("Gmail", "https://mail.google.com"),
                     ("WhatsApp", "https://web.whatsapp.com"), ("Maps", "https://maps.google.com"),
                     ("Explorer", "local:home"), ("Settings", "local:settings")]

    def __init__(self, win, face_path: str, display: str):
        self.win = win
        win._cc = self
        win.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        win.setMinimumSize(1100, 700)
        self.sys = SysInfo()
        self.weather = WeatherService()
        self.tasks = TaskFeed()
        self._accent_key = None
        self._ambient: AmbientOrb | None = None
        self._qss_widgets: list = []
        self._accent_labels: list = []

        root = Backdrop()
        win.setCentralWidget(root)
        self.root = root
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_top(display))

        body = QHBoxLayout()
        body.setContentsMargins(14, 12, 14, 8)
        body.setSpacing(14)
        self.left = self._build_left()
        win._left_panel = self.left
        body.addWidget(self.left)

        # centre: reactor + camera stack + content panel in a splitter
        win.hud = ArcReactorHud(face_path, display)
        _Accent.hud = win.hud
        from memory.config_manager import get_personality_mode as _gpm
        win.hud.set_personality_mode(_gpm())
        cam = QWidget()
        cam.setStyleSheet("background: #000308;")
        cv = QVBoxLayout(cam)
        cv.setContentsMargins(0, 0, 0, 0)
        ch = QHBoxLayout()
        ch.setContentsMargins(10, 6, 10, 6)
        ch.addWidget(_label("◈  CAMERA FEED", 12, QFont.Weight.DemiBold, "#e2f1f8", 2.0))
        ch.addStretch()
        cx_btn = QPushButton("✕  CLOSE")
        cx_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cx_btn.setFont(hfont(11, QFont.Weight.DemiBold))
        cx_btn.setStyleSheet("QPushButton{color:#94a3b8;background:transparent;border:none;}"
                             "QPushButton:hover{color:#ffffff;}")
        cx_btn.clicked.connect(win.stop_camera_stream)
        ch.addWidget(cx_btn)
        cv.addLayout(ch)
        win._cam_live_lbl = QLabel()
        win._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        win._cam_live_lbl.setStyleSheet("background: transparent;")
        win._cam_live_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        cv.addWidget(win._cam_live_lbl, stretch=1)
        win._hud_cam_stack = QStackedWidget()
        win._hud_cam_stack.addWidget(win.hud)
        win._hud_cam_stack.addWidget(cam)
        win._content_panel = self._build_content()
        split = QSplitter(Qt.Orientation.Vertical)
        split.setStyleSheet("QSplitter::handle { background: transparent; height: 8px; }")
        split.addWidget(win._hud_cam_stack)
        split.addWidget(win._content_panel)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setCollapsible(0, False)
        win._center_split = split
        body.addWidget(split, stretch=1)

        self.right = self._build_right()
        win._right_panel = self.right
        body.addWidget(self.right)
        outer.addLayout(body, stretch=1)
        outer.addWidget(self._build_dock())

        # timers
        self._fast = QTimer(win)
        self._fast.timeout.connect(self._tick_fast)
        self._fast.start(1000)
        self._slow = QTimer(win)
        self._slow.timeout.connect(self._tick_slow)
        self._slow.start(250)
        self._tick_fast()
        win.setWindowState(Qt.WindowState.WindowMaximized)

    # ── top ──────────────────────────────────────────────────────────────
    def _build_top(self, display: str) -> QWidget:
        win = self.win
        bar = TopBar(win)
        self.top = bar
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(48, 0, 8, 0)
        lay.setSpacing(14)
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        win._title_lbl = _label(display, 17, QFont.Weight.Bold, "#ffffff", 3.0)
        sub = ("Just A Rather Very Intelligent System" if display in ("JARVIS", "J.A.R.V.I.S")
               else "Personal AI Assistant")
        win._sub_lbl = _label(sub, 9.5, QFont.Weight.Normal, "#64748b", 1.0)
        tcol.addStretch()
        tcol.addWidget(win._title_lbl)
        tcol.addWidget(win._sub_lbl)
        tcol.addStretch()
        lay.addLayout(tcol)
        lay.addStretch(1)
        ccol = QVBoxLayout()
        ccol.setSpacing(0)
        win._clock_lbl = _label("00:00:00", 20, QFont.Weight.DemiBold, "#e2f1f8", 2.0,
                                Qt.AlignmentFlag.AlignHCenter)
        win._date_lbl = _label("", 10, QFont.Weight.Normal, "#94a3b8", 2.0, Qt.AlignmentFlag.AlignHCenter)
        ccol.addStretch()
        ccol.addWidget(win._clock_lbl)
        ccol.addWidget(win._date_lbl)
        ccol.addStretch()
        lay.addLayout(ccol)
        lay.addStretch(1)
        # Only what no panel shows: CPU/RAM/GPU/temp live in Diagnostics, the
        # network in Uplink, disks in Quick Launch (Mark LIII.2 — each once).
        # Part One: active model / voice state / capture indicator
        self.status_chips = StatusChips()
        lay.addWidget(self.status_chips)
        self.strip_right = StatStrip(["VOL", "PWR"])
        self.strip_right.setMinimumWidth(150)
        lay.addWidget(self.strip_right)
        win._drawer_btn = _chrome_btn("⚙", "Settings & controls")
        win._drawer_btn.setCheckable(True)
        win._drawer_btn.clicked.connect(win._toggle_drawer)
        lay.addWidget(win._drawer_btn)
        b_min = _chrome_btn("—", "Minimise")
        b_min.clicked.connect(win.showMinimized)
        lay.addWidget(b_min)
        b_max = _chrome_btn("▢", "Maximise / restore")
        b_max.clicked.connect(self.toggle_max)
        lay.addWidget(b_max)
        b_x = _chrome_btn("✕", "Close JARVIS", danger=True)
        b_x.clicked.connect(win.close)
        lay.addWidget(b_x)
        win._drawer_anchor_y = 46
        return bar

    def set_status(self, key: str, value: str) -> None:
        try:
            self.status_chips.set(key, value)
        except Exception:
            pass

    # ── left column ─────────────────────────────────────────────────────
    def _build_left(self) -> QWidget:
        win = self.win
        col = QWidget()
        col.setStyleSheet("background: transparent;")
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        proto = GlassPanel("Active Protocols", "tasks")
        self.task_list = TaskList()
        proto.body.addWidget(self.task_list)
        v.addWidget(proto, stretch=2)

        diag = GlassPanel("Diagnostics", "pulse")
        win._bar_cpu = TelemetryRow("CPU")
        win._bar_mem = TelemetryRow("Memory")
        win._bar_net = TelemetryRow("Network")   # kept for MainWindow's metric updates; shown in Uplink
        win._bar_net.hide()
        win._bar_gpu = TelemetryRow("GPU")
        win._bar_tmp = TelemetryRow("Thermal")
        for b in (win._bar_cpu, win._bar_mem, win._bar_gpu, win._bar_tmp):
            diag.body.addWidget(b)
        info = QHBoxLayout()
        win._uptime_lbl = _label("UP  --:--", 11, QFont.Weight.DemiBold, "#00ff88", 1.0)
        win._proc_lbl = _label("PROC  --", 11, QFont.Weight.Normal, "#94a3b8", 1.0,
                               Qt.AlignmentFlag.AlignRight)
        info.addWidget(win._uptime_lbl)
        info.addWidget(win._proc_lbl)
        diag.body.addLayout(info)
        v.addWidget(diag)

        links = GlassPanel("Quick Launch", "link")
        items = self._links()
        self.link_list = LinkList(items)
        self.link_list.activated.connect(self._open_target)
        self.drives = DriveGauges()
        lrow = QHBoxLayout()
        lrow.setSpacing(6)
        lrow.addWidget(self.drives, stretch=1)
        lrow.addWidget(self.link_list, stretch=1)
        links.body.addLayout(lrow)
        v.addWidget(links)
        return col

    def _links(self) -> list[tuple[str, str]]:
        cfg = _read_cfg().get("quick_links")
        if isinstance(cfg, list):
            out = []
            for it in cfg[:10]:
                if isinstance(it, dict) and it.get("label") and it.get("target"):
                    out.append((str(it["label"])[:14], str(it["target"])))
            if out:
                return out
        return list(self.DEFAULT_LINKS)

    # ── right column ────────────────────────────────────────────────────
    def _build_right(self) -> QWidget:
        win = self.win
        col = QWidget()
        col.setStyleSheet("background: transparent;")
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        logp = GlassPanel("Activity", "log")
        win._log = _ui.LogWidget()
        win._log.setFont(hfont(12.5, mono=True))
        logp.body.addWidget(win._log)
        v.addWidget(logp, stretch=3)

        wp = GlassPanel("Atmospherics", "cloud")
        self.weather_view = WeatherView()
        wp.body.addWidget(self.weather_view)
        v.addWidget(wp, stretch=0)

        up = GlassPanel("Uplink", "uplink")
        self.uplink = UplinkView()
        up.body.addWidget(self.uplink)
        v.addWidget(up)

        fp = GlassPanel("File Intake", "file")
        win._drop_zone = _ui.FileDropZone()
        win._drop_zone.setFixedHeight(92)
        win._drop_zone.file_selected.connect(win._on_file_selected)
        fp.body.addWidget(win._drop_zone)
        win._file_hint = _label("No file loaded", 11, QFont.Weight.Normal, "#64748b")
        win._file_hint.setWordWrap(True)
        fp.body.addWidget(win._file_hint)
        v.addWidget(fp)
        return col

    # ── content (search results / news) ─────────────────────────────────
    def _build_content(self) -> QWidget:
        win = self.win
        w = GlassPanel(margins=(16, 12, 16, 12))
        hdr = QHBoxLayout()
        dot = _label("◈", 13, QFont.Weight.Bold, _Accent.hex(0.2))
        self._accent_labels.append(dot)
        hdr.addWidget(dot)
        win._content_title_lbl = _label("BRIEFING", 13, QFont.Weight.DemiBold, "#e2f1f8", 2.0)
        hdr.addWidget(win._content_title_lbl)
        hdr.addStretch()
        win._content_ts_lbl = _label("", 11, QFont.Weight.Normal, "#64748b")
        hdr.addWidget(win._content_ts_lbl)
        dismiss = QPushButton("DISMISS  ✕")
        dismiss.setFont(hfont(10.5, QFont.Weight.DemiBold, spacing=1.0))
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet("QPushButton{color:#94a3b8;background:rgba(255,255,255,0.04);"
                              "border:1px solid rgba(255,255,255,0.12);border-radius:4px;padding:3px 8px;}"
                              "QPushButton:hover{color:#ffffff;border-color:rgba(255,255,255,0.35);}")
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        w.body.addLayout(hdr)
        win._content_display = QTextEdit()
        win._content_display.setReadOnly(True)
        win._content_display.setFont(hfont(12.5))
        win._content_display.setMinimumHeight(60)
        self._qss_widgets.append((win._content_display, _TEXT_QSS))
        w.body.addWidget(win._content_display)
        w.hide()
        return w

    # ── dock ──────────────────────────────────────────────────────────────
    def _build_dock(self) -> QWidget:
        win = self.win
        dock = GlassPanel(margins=(12, 8, 12, 8))
        dock.setFixedHeight(62)
        dock._title = ""
        row = QHBoxLayout()
        row.setSpacing(10)
        # Friendly / Serious / Ambient live here now — the core itself carries no controls.
        self.mode_btns: dict[str, QPushButton] = {}
        for name, text, tip in (("mode_friendly", "FRIENDLY", "Friendly mode — warm, conversational (blue)"),
                                ("mode_serious", "SERIOUS", "Serious mode — focused, operational (red)"),
                                ("ambient", "◎ AMBIENT", "Ambient view — floating core  [F10]"),
                                ("mission", "🛰 MISSION", "Mission Control — agents, tasks, schedule, content, logs, "
                                                          "permissions, learning  [F9]")):
            b = QPushButton(text)
            b.setFixedHeight(36)
            b.setFont(hfont(11.5, QFont.Weight.DemiBold, spacing=1.4))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip + "\nShortcuts: F4 mute · Esc interrupt · F10 ambient · F11 full screen · F12 stop all")
            b.clicked.connect(lambda _=False, n=name: self._on_chip(n))
            self.mode_btns[name] = b
            row.addWidget(b)
        row.addStretch(1)
        win._input = QLineEdit()
        win._input.setPlaceholderText(f"Type a command for {win._assistant_name}…")
        win._input.setFont(hfont(13.5))
        win._input.setFixedHeight(36)
        win._input.setMinimumWidth(420)
        win._input.returnPressed.connect(win._send)
        self._qss_widgets.append((win._input, _LINE_EDIT_QSS))
        row.addWidget(win._input, stretch=3)
        send = QPushButton("SEND ▸")
        send.setFixedHeight(36)
        send.setFont(hfont(12, QFont.Weight.DemiBold, spacing=1.5))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.clicked.connect(win._send)
        self._send_btn = send
        row.addWidget(send)
        row.addSpacing(6)
        win._mute_btn = QPushButton()
        win._mute_btn.setFixedHeight(36)
        win._mute_btn.setFont(hfont(12, QFont.Weight.DemiBold, spacing=1.2))
        win._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        win._mute_btn.clicked.connect(win._toggle_mute)
        row.addWidget(win._mute_btn)
        win._interrupt_btn = QPushButton("✋ INTERRUPT")
        win._interrupt_btn.setFixedHeight(36)
        win._interrupt_btn.setFont(hfont(12, QFont.Weight.DemiBold, spacing=1.2))
        win._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        win._interrupt_btn.setStyleSheet(
            "QPushButton{background:rgba(255,51,102,0.10);color:#ff6688;border:1px solid rgba(255,51,102,0.55);"
            "border-radius:6px;padding:0 14px;}"
            "QPushButton:hover{background:rgba(255,51,102,0.22);color:#ffffff;}")
        win._interrupt_btn.clicked.connect(win._do_interrupt)
        row.addWidget(win._interrupt_btn)
        win._stop_btn = QPushButton("⛔ STOP")
        win._stop_btn.setFixedHeight(36)
        win._stop_btn.setFont(hfont(12, QFont.Weight.Bold, spacing=1.4))
        win._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        win._stop_btn.setToolTip("Emergency stop (F12): cancel every task, withdraw confirmations, "
                                 "stop mouse/keyboard control")
        win._stop_btn.setStyleSheet(
            "QPushButton{background:rgba(255,34,34,0.18);color:#ff4d4d;border:1px solid rgba(255,34,34,0.85);"
            "border-radius:6px;padding:0 14px;}"
            "QPushButton:hover{background:rgba(255,34,34,0.40);color:#ffffff;}")
        win._stop_btn.clicked.connect(win._do_emergency_stop)
        row.addWidget(win._stop_btn)
        row.addSpacing(6)
        self.vu = VuMeter(win.hud)
        row.addWidget(self.vu)
        row.addStretch(1)
        grip = QSizeGrip(dock)
        grip.setFixedSize(14, 14)
        row.addWidget(grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        dock.body.addLayout(row)
        self.style_mute_btn(False)
        return dock

    # ── styling that follows the live accent ───────────────────────────
    def style_mute_btn(self, muted: bool):
        b = self.win._mute_btn
        if muted and _friendly():                     # Phase 8: Friendly = blue (muted = a dim blue)
            b.setText("🔇 MIC MUTED")
            b.setStyleSheet("QPushButton{background:rgba(63,111,174,0.16);color:#8fb4e0;"
                            "border:1px solid rgba(63,111,174,0.85);border-radius:6px;padding:0 14px;}")
        elif muted:
            b.setText("🔇 MIC MUTED")
            b.setStyleSheet("QPushButton{background:rgba(255,51,102,0.12);color:#ff3366;"
                            "border:1px solid rgba(255,51,102,0.7);border-radius:6px;padding:0 14px;}")
        else:
            a = _Accent.c(255)
            b.setText("🎙 MIC LIVE")
            b.setStyleSheet(f"QPushButton{{background:rgba({a.red()},{a.green()},{a.blue()},0.14);color:#e2f1f8;"
                            f"border:1px solid rgba({a.red()},{a.green()},{a.blue()},0.75);border-radius:6px;"
                            f"padding:0 14px;}}"
                            f"QPushButton:hover{{background:rgba({a.red()},{a.green()},{a.blue()},0.26);}}")

    def _restyle(self):
        a = _Accent.c(255)
        rgb = f"{a.red()},{a.green()},{a.blue()}"
        for w, qss in self._qss_widgets:
            w.setStyleSheet(qss.format(a=f"rgba({rgb},0.85)", b=f"rgba({rgb},0.35)"))
        self.win._log.setStyleSheet(_TEXT_QSS.format(a=f"rgba({rgb},0.5)", b=f"rgba({rgb},0.35)"))
        self._send_btn.setStyleSheet(
            f"QPushButton{{background:rgba({rgb},0.18);color:#ffffff;border:1px solid rgba({rgb},0.8);"
            f"border-radius:6px;padding:0 16px;}}QPushButton:hover{{background:rgba({rgb},0.32);}}")
        for l in self._accent_labels:
            l.setStyleSheet(f"color: {_Accent.hex(0.2)}; background: transparent;")
        # Phase 8: Friendly = all blue (INTERRUPT was pink-red, uptime green); STOP stays red
        ib = getattr(self.win, "_interrupt_btn", None)
        if ib is not None:
            irgb = rgb if _friendly() else "255,51,102"
            icol = _Accent.hex(0.45) if _friendly() else "#ff6688"
            ib.setStyleSheet(f"QPushButton{{background:rgba({irgb},0.10);color:{icol};"
                             f"border:1px solid rgba({irgb},0.55);border-radius:6px;padding:0 14px;}}"
                             f"QPushButton:hover{{background:rgba({irgb},0.22);color:#ffffff;}}")
        ul = getattr(self.win, "_uptime_lbl", None)
        if ul is not None:
            ul.setStyleSheet(f"color: {_Accent.hex(0.35) if _friendly() else '#00ff88'}; background: transparent;")
        self.style_mute_btn(self.win._muted)
        self.root.update()
        for w in self.root.findChildren(QWidget):
            if isinstance(w, (GlassPanel, TopBar, StatStrip, TelemetryRow, LinkList, DriveGauges, TaskList,
                              WeatherView, UplinkView)):
                w.update()

    # ── timers ────────────────────────────────────────────────────────────
    def _tick_slow(self):
        key = _Accent.key() + (self.win._muted,)
        if key != self._accent_key:
            self._accent_key = key
            self._restyle()
        if self.tasks.poll():
            self.task_list.set_tasks(self.tasks.tasks)
        self.win.hud.flags["ambient"] = bool(self._ambient and self._ambient.isVisible())
        mk = (self.win.hud._mode_name, self.win.hud.flags["ambient"], _Accent.key())
        if mk != getattr(self, "_mode_key", None):
            self._mode_key = mk
            self._style_mode_btns()

    def _style_mode_btns(self):
        mode = self.win.hud._mode_name
        blue, red = "62,166,255", "220,52,70"
        for name, b in getattr(self, "mode_btns", {}).items():
            rgb = blue if name == "mode_friendly" else red if name == "mode_serious" else \
                ",".join(str(v) for v in _Accent.rgb())
            active = (name == f"mode_{mode}") or (name == "ambient" and self.win.hud.flags.get("ambient"))
            if mode == "friendly" and not active:      # Phase 8: Friendly = all blue
                rgb = blue
            if active:
                b.setStyleSheet(f"QPushButton{{background:rgba({rgb},0.30);color:#ffffff;"
                                f"border:1px solid rgba({rgb},0.95);border-radius:6px;padding:0 12px;}}")
            else:
                b.setStyleSheet(f"QPushButton{{background:rgba({rgb},0.06);color:#94a3b8;"
                                f"border:1px solid rgba({rgb},0.35);border-radius:6px;padding:0 12px;}}"
                                f"QPushButton:hover{{background:rgba({rgb},0.18);color:#ffffff;}}")

    def _tick_fast(self):
        win = self.win
        s = self.sys.snapshot()
        m = _ui._metrics.snapshot()
        win.hud.telemetry = {"cpu": m.get("cpu", 0.0), "mem": m.get("mem", 0.0)}
        vol = s.get("volume")
        self.strip_right.set("VOL", f"{vol}" if vol is not None else "--", "%" if vol is not None else "")
        bat = s.get("battery")
        self.strip_right.set("PWR", f"{bat:.0f}" if bat is not None else "AC",
                             ("%⚡" if s.get("plugged") else "%") if bat is not None else "")
        disks = s.get("disks") or []
        self.drives.set_disks(disks)
        self.uplink.set_info(s)
        self.weather_view.set_data(*self.weather.snapshot())

    # ── actions ───────────────────────────────────────────────────────────
    def _open_target(self, target: str):
        try:
            if target.startswith("local:"):
                what = target[6:]
                if what == "home":
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path.home())))
                elif what == "settings":
                    if _OS == "Windows":
                        os.startfile("ms-settings:")        # type: ignore[attr-defined]
                elif what in ("desktop", "documents", "downloads"):
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path.home() / what.title())))
                return
            if re.match(r"^https?://", target):
                QDesktopServices.openUrl(QUrl(target))
            else:
                self.win._log.append_log(f"SYS: Quick link ignored (only http/https or local: targets) — {target}")
        except Exception as e:
            self.win._log.append_log(f"ERR: Could not open {target} — {e}")

    def _on_chip(self, name: str):
        win = self.win
        if name in ("mode_friendly", "mode_serious"):
            mode = name.split("_", 1)[1]
            cb = getattr(win, "on_mode_change", None)
            if cb:                              # direct switch: save, recolour, refresh the voice session
                win.hud.set_personality_mode(mode)
                threading.Thread(target=cb, args=(mode,), daemon=True).start()
                win._log.append_log(f"SYS: {mode.title()} mode.")
            elif win.on_text_command:
                threading.Thread(target=win.on_text_command,
                                 args=(f"Switch to {mode} mode.",), daemon=True).start()
                win._log.append_log(f"SYS: Requesting {mode} mode…")
            else:
                win.hud.set_personality_mode(mode)
        elif name == "mic":
            win._toggle_mute()
        elif name == "ambient":
            self.enter_ambient()
        elif name == "mission":
            win._open_mission_control()
        elif name == "drawer":
            win._drawer_btn.setChecked(not win._drawer_btn.isChecked())
            win._toggle_drawer(win._drawer_btn.isChecked())
        elif name in ("docs", "desktop", "downloads"):
            self._open_target("local:" + {"docs": "documents"}.get(name, name))
        elif name == "web":
            QDesktopServices.openUrl(QUrl("https://www.google.com"))
        elif name == "code":
            if win.on_text_command:
                threading.Thread(target=win.on_text_command, args=("Open VS Code.",), daemon=True).start()
        elif name == "memory":
            win._open_memory_panel()
        elif name == "customize":
            win._open_customize()

    def toggle_max(self):
        w = self.win
        if w.isMaximized() or w.isFullScreen():
            w.showNormal()
        else:
            w.showMaximized()

    def enter_ambient(self):
        if self._ambient is None:
            self._ambient = AmbientOrb(self.win.hud, self.exit_ambient)
        self._was_max = self.win.isMaximized()
        self._ambient.show()
        self.win.hide()
        self.win._log.append_log("SYS: Ambient mode — double-click the orb or press F10 to return.")

    def exit_ambient(self):
        if self._ambient is not None:
            self._ambient.hide()
        if getattr(self, "_was_max", True):
            self.win.showMaximized()
        else:
            self.win.showNormal()
        self.win.raise_()
        self.win.activateWindow()

    def toggle_ambient(self):
        if self._ambient is not None and self._ambient.isVisible():
            self.exit_ambient()
        else:
            self.enter_ambient()

    def ensure_visible(self):
        """Something needs the user's eyes (confirmation, setup): leave ambient."""
        if self._ambient is not None and self._ambient.isVisible():
            self.exit_ambient()

    def resize(self):
        W = self.win.width()
        self.left.setFixedWidth(int(max(250, min(360, W * 0.19))))
        self.right.setFixedWidth(int(max(290, min(420, W * 0.22))))
