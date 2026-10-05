"""
core/draw_agent.py — "draw something you like": JARVIS opens Paint and draws
with the mouse, the way a person would, then looks at the result.

    subject ─► LINE ART (the model draws an SVG of black strokes)
              └► STROKES (this module flattens the SVG — paths, curves, arcs,
                 circles, polygons, transforms — into mouse polylines)
                  └► PAINT (opened by JARVIS itself, brought to the front)
                      └► LOOK (vision: where is the canvas, where is the pencil)
                          └► DRAW (mouse down → move along each stroke → up)
                              └► LOOK AGAIN (did the canvas change? does it look
                                 like the subject?) → honest report

SAFETY (in code, not left to the model)
  * It only ever draws into a window whose title says Paint and which JARVIS
    brought up; before EVERY stroke it re-checks the window in front — if the
    user clicks into anything else, the pen lifts and the run stops.
  * Every point is clamped inside the canvas rectangle the vision step found,
    which must be on screen, away from the screen corners (PyAutoGUI's
    FAILSAFE corner stays armed) and large enough to be a canvas.
  * The emergency stop (F12 / "stop everything") is checked between strokes.
  * The task asks first (HUD: ALLOW ONCE / ALWAYS / DENY) because it moves the
    real mouse. Nothing is typed, nothing is saved, no other app is touched;
    the drawing is left open in Paint for the user to keep or discard.

The SVG and a rendered PNG preview are also saved (Documents/JARVIS Research),
so there is a clean copy whatever happens on screen.
"""
from __future__ import annotations

import math
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Optional

from core.tools.registry import ToolResult

MAX_POINTS = 2400          # total mouse points per drawing
MAX_STROKES = 160
PAINT_TITLE = re.compile(r"\bpaint\b", re.I)

# ═════════════════════════════════════════════════════════════════════════════
# SVG → polylines (dependency-free)
# ═════════════════════════════════════════════════════════════════════════════
Mat = tuple  # (a, b, c, d, e, f): x' = a*x + c*y + e ; y' = b*x + d*y + f
IDENT: Mat = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(m: Mat, n: Mat) -> Mat:
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2,
            a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def _apply(m: Mat, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def _nums(s: str) -> list[float]:
    return [float(x) for x in re.findall(_NUM, s or "")]


def parse_transform(s: str) -> Mat:
    m = IDENT
    for name, args in re.findall(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", s or ""):
        v = _nums(args)
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate" and v:
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0.0)
        elif name == "scale" and v:
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate" and v:
            r = math.radians(v[0])
            cs, sn = math.cos(r), math.sin(r)
            t = (cs, sn, -sn, cs, 0, 0)
            if len(v) >= 3:
                t = _mul(_mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "skewX" and v:
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY" and v:
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = _mul(m, tuple(float(x) for x in t))
    return m


def _cubic(p0, p1, p2, p3, n):
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def _quad(p0, p1, p2, n):
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0], u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return out


def _arc(p0, rx, ry, phi_deg, large, sweep, p1):
    """SVG elliptical arc (endpoint parameterisation, SVG spec F.6.5) → points."""
    if rx == 0 or ry == 0 or p0 == p1:
        return [p1]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg % 360)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if bool(large) == bool(sweep):
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (p0[0] + p1[0]) / 2
    cy = sp * cxp + cp * cyp + (p0[1] + p1[1]) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a
    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = max(4, int(abs(dt) / (math.pi / 16)))
    out = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        out.append((cp * x - sp * y + cx, sp * x + cp * y + cy))
    return out


def path_polylines(d: str, curve_n: int = 12) -> list[list[tuple[float, float]]]:
    toks = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|" + _NUM, d or "")
    subs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    x = y = sx = sy = 0.0
    last_c2 = last_q = None
    cmd = None
    i = 0

    def take(k):
        nonlocal i
        vals = []
        for _ in range(k):
            if i >= len(toks) or re.fullmatch(r"[A-Za-z]", toks[i]):
                raise ValueError("short")
            vals.append(float(toks[i]))
            i += 1
        return vals
    while i < len(toks):
        t = toks[i]
        if re.fullmatch(r"[A-Za-z]", t):
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur:
                    cur.append((sx, sy))
                    subs.append(cur)
                    cur = []
                x, y = sx, sy
                last_c2 = last_q = None
                continue
        elif cmd is None:
            break
        try:
            rel = cmd.islower()
            C = cmd.upper()
            if C == "M":
                nx, ny = take(2)
                x, y = (x + nx, y + ny) if rel else (nx, ny)
                if cur and len(cur) > 1:
                    subs.append(cur)
                cur = [(x, y)]
                sx, sy = x, y
                cmd = "l" if rel else "L"          # implicit lineto after a moveto
                last_c2 = last_q = None
            elif C == "L":
                nx, ny = take(2)
                x, y = (x + nx, y + ny) if rel else (nx, ny)
                cur.append((x, y))
                last_c2 = last_q = None
            elif C == "H":
                (nx,) = take(1)
                x = x + nx if rel else nx
                cur.append((x, y))
                last_c2 = last_q = None
            elif C == "V":
                (ny,) = take(1)
                y = y + ny if rel else ny
                cur.append((x, y))
                last_c2 = last_q = None
            elif C in "CS":
                if C == "C":
                    x1, y1, x2, y2, ex, ey = take(6)
                    if rel:
                        x1, y1, x2, y2, ex, ey = x + x1, y + y1, x + x2, y + y2, x + ex, y + ey
                else:
                    x2, y2, ex, ey = take(4)
                    if rel:
                        x2, y2, ex, ey = x + x2, y + y2, x + ex, y + ey
                    x1, y1 = (2 * x - last_c2[0], 2 * y - last_c2[1]) if last_c2 else (x, y)
                cur.extend(_cubic((x, y), (x1, y1), (x2, y2), (ex, ey), curve_n))
                last_c2, last_q = (x2, y2), None
                x, y = ex, ey
            elif C in "QT":
                if C == "Q":
                    x1, y1, ex, ey = take(4)
                    if rel:
                        x1, y1, ex, ey = x + x1, y + y1, x + ex, y + ey
                else:
                    ex, ey = take(2)
                    if rel:
                        ex, ey = x + ex, y + ey
                    x1, y1 = (2 * x - last_q[0], 2 * y - last_q[1]) if last_q else (x, y)
                cur.extend(_quad((x, y), (x1, y1), (ex, ey), max(6, curve_n - 2)))
                last_q, last_c2 = (x1, y1), None
                x, y = ex, ey
            elif C == "A":
                rx, ry, rot, la, sw, ex, ey = take(7)
                if rel:
                    ex, ey = x + ex, y + ey
                cur.extend(_arc((x, y), rx, ry, rot, la, sw, (ex, ey)))
                x, y = ex, ey
                last_c2 = last_q = None
            else:
                i += 1
        except ValueError:
            break
        if not cur:
            cur = [(x, y)]
    if cur and len(cur) > 1:
        subs.append(cur)
    return subs


def _f(el, name, default=0.0) -> float:
    v = _nums(str(el.get(name) or ""))
    return v[0] if v else default


def _ellipse(cx, cy, rx, ry, n=40):
    return [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


_SKIP = {"defs", "clippath", "mask", "style", "title", "desc", "metadata", "pattern", "symbol", "lineargradient",
         "radialgradient", "filter", "text", "image", "script", "foreignobject", "marker"}


def svg_polylines(svg: str) -> tuple[list[list[tuple[float, float]]], tuple[float, float, float, float]]:
    """All drawable outlines of `svg` in its own coordinates (transforms
    applied) and its viewBox (x, y, w, h)."""
    root = ET.fromstring(svg)
    vb = _nums(root.get("viewBox") or "")
    if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
        box = tuple(vb)
    else:
        box = (0.0, 0.0, _f(root, "width", 1000.0) or 1000.0, _f(root, "height", 1000.0) or 1000.0)
    out: list[list[tuple[float, float]]] = []

    def walk(el, m: Mat):
        tag = el.tag.split("}")[-1].lower()
        if tag in _SKIP:
            return
        style = (el.get("style") or "") + " " + (el.get("display") or "") + " " + (el.get("visibility") or "")
        if re.search(r"display\s*:\s*none|\bnone\b(?=\s*$)|visibility\s*:\s*hidden|\bhidden\b", style.strip()):
            return
        m2 = _mul(m, parse_transform(el.get("transform") or "")) if el.get("transform") else m
        polys: list[list[tuple[float, float]]] = []
        if tag == "path":
            polys = path_polylines(el.get("d") or "")
        elif tag == "line":
            polys = [[(_f(el, "x1"), _f(el, "y1")), (_f(el, "x2"), _f(el, "y2"))]]
        elif tag in ("polyline", "polygon"):
            v = _nums(el.get("points") or "")
            pts = list(zip(v[0::2], v[1::2]))
            if tag == "polygon" and pts:
                pts.append(pts[0])
            polys = [pts] if len(pts) > 1 else []
        elif tag == "rect":
            x0, y0, w, h = _f(el, "x"), _f(el, "y"), _f(el, "width"), _f(el, "height")
            if w > 0 and h > 0:
                polys = [[(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)]]
        elif tag == "circle":
            r = _f(el, "r")
            if r > 0:
                polys = [_ellipse(_f(el, "cx"), _f(el, "cy"), r, r, max(16, min(64, int(r / 3) + 16)))]
        elif tag == "ellipse":
            rx, ry = _f(el, "rx"), _f(el, "ry")
            if rx > 0 and ry > 0:
                polys = [_ellipse(_f(el, "cx"), _f(el, "cy"), rx, ry, max(16, min(64, int(max(rx, ry) / 3) + 16)))]
        for p in polys:
            q = [_apply(m2, px, py) for px, py in p]
            if len(q) > 1:
                out.append(q)
        for ch in list(el):
            walk(ch, m2)
    walk(root, IDENT)
    return out, box


def _rdp(pts: list, eps: float) -> list:
    if len(pts) < 3:
        return pts
    (x0, y0), (x1, y1) = pts[0], pts[-1]
    dx, dy = x1 - x0, y1 - y0
    norm = math.hypot(dx, dy) or 1e-9
    dmax, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        d = abs(dy * px - dx * py + x1 * y0 - y1 * x0) / norm if (dx or dy) else math.hypot(px - x0, py - y0)
        if d > dmax:
            dmax, idx = d, i
    if dmax > eps:
        return _rdp(pts[:idx + 1], eps)[:-1] + _rdp(pts[idx:], eps)
    return [pts[0], pts[-1]]


def normalise(polys, box, eps: float = 0.002) -> list[list[tuple[float, float]]]:
    """→ strokes in 0..1 × 0..1, aspect ratio kept and centred, simplified,
    tiny specks dropped, total points capped."""
    bx, by, bw, bh = box
    s = 1.0 / max(bw, bh)
    ox, oy = (1 - bw * s) / 2, (1 - bh * s) / 2
    strokes = []
    for p in polys:
        q = [((x - bx) * s + ox, (y - by) * s + oy) for x, y in p]
        q = [(min(1.0, max(0.0, x)), min(1.0, max(0.0, y))) for x, y in q]
        q = _rdp(q, eps)
        length = sum(math.hypot(q[i + 1][0] - q[i][0], q[i + 1][1] - q[i][1]) for i in range(len(q) - 1))
        if len(q) >= 2 and length > 0.006:
            strokes.append(q)
    strokes = strokes[:MAX_STROKES]
    total = sum(len(st) for st in strokes)
    while total > MAX_POINTS and eps < 0.05:
        eps *= 1.6
        strokes = [_rdp(st, eps) for st in strokes]
        total = sum(len(st) for st in strokes)
    return strokes


def strokes_from_svg(svg: str) -> list[list[tuple[float, float]]]:
    polys, box = svg_polylines(svg)
    return normalise(polys, box)


def to_canvas(strokes, canvas: tuple[int, int, int, int], margin: float = 0.08) -> list[list[tuple[int, int]]]:
    """0..1 strokes → screen pixels inside canvas (left, top, right, bottom),
    every point clamped inside it."""
    l, t, r, b = canvas
    w, h = r - l, b - t
    side = min(w, h) * (1 - 2 * margin)
    ox, oy = l + (w - side) / 2, t + (h - side) / 2
    out = []
    for st in strokes:
        pts = []
        for x, y in st:
            px = int(round(ox + x * side))
            py = int(round(oy + y * side))
            pts.append((min(r - 2, max(l + 2, px)), min(b - 2, max(t + 2, py))))
        dedup = [pts[0]] + [p for i, p in enumerate(pts[1:], 1) if p != pts[i - 1]]
        if len(dedup) >= 2:
            out.append(dedup)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# the drawing run
# ═════════════════════════════════════════════════════════════════════════════
LINE_ART_PROMPT = (
    "Draw {subject} as simple, recognisable LINE ART that a pen plotter will trace with one thin black pen. "
    "Output ONLY one <svg> element: viewBox=\"0 0 1000 1000\", stroke=\"black\", fill=\"none\" on every shape, "
    "no text, no gradients, no fills, no clipPath, no images. 8-30 shapes using path / circle / ellipse / line / "
    "polyline. Smooth curves are fine. Leave a margin. Make it charming and clearly readable."
    "{choice}"
)
CHOICE_NOTE = (" The user said 'draw something you like' — choose a subject YOU like that works as line art "
               "(an animal, a landscape, an object…) and put its name in a <title> element.")

CANVAS_SCHEMA = {"type": "object", "properties": {
    "paint_open": {"type": "boolean"},
    "canvas": {"type": "array", "items": {"type": "number"}},       # [y0, x0, y1, x1] 0-1000
    "pencil": {"type": "array", "items": {"type": "number"}},       # [y, x] 0-1000 or []
    "note": {"type": "string"}}, "required": ["paint_open", "canvas"]}


class DrawError(Exception):
    pass


class Painter:
    """The on-screen part. Every collaborator is injectable for tests."""

    def __init__(self, *, actuator=None, capture=None, active_title=None, vision=None, launch=None,
                 should_stop=None, progress=None, sleep=time.sleep, kill=None):
        self._act = actuator
        self._capture = capture
        self._title = active_title
        self._vision = vision
        self._launch = launch
        self._stop = should_stop or (lambda: False)
        self._progress = progress or (lambda m: None)
        self._sleep = sleep
        self._kill = kill

    # -- collaborators (lazy real ones) ------------------------------------------
    def _p(self):
        if self._act is None:
            import pyautogui
            pyautogui.FAILSAFE = True
            self._act = pyautogui
        return self._act

    def title(self) -> str:
        if self._title:
            return self._title()
        from core.vision import active_window
        return str(active_window().get("title") or "")

    def grab(self):
        if self._capture:
            return self._capture()
        from core.vision import capture
        return capture("screen", max_side=1600)

    def look(self, cap) -> dict:
        if self._vision:
            return self._vision(cap)
        from core.ai_gateway import get_gateway
        q = ("This is a Windows screenshot. Is Microsoft Paint open in front? Give the drawing CANVAS (the white "
             "drawing area, not the toolbar) as [y0, x0, y1, x1] normalised 0-1000, and the centre of the PENCIL "
             "tool button as [y, x] (empty list if you can't see it).")
        return get_gateway().generate(q, images=[(cap.data, cap.mime)], schema=CANVAS_SCHEMA, tier="computer_use",
                                      temperature=0.1, timeout=60.0).json() or {}

    def check(self) -> None:
        if self._kill is not None:
            self._kill.check()
        if self._stop():
            from core.safety import EmergencyStop
            raise EmergencyStop("Stopped.")

    # -- steps -----------------------------------------------------------------
    def open_paint(self) -> None:
        if PAINT_TITLE.search(self.title() or ""):
            return
        self._progress("Opening Paint…")
        if self._launch:
            self._launch()
        else:
            from core.computer_agent import _launch_app
            _launch_app("paint")
        for _ in range(16):
            self._sleep(0.5)
            if PAINT_TITLE.search(self.title() or ""):
                break
        else:
            raise DrawError("Paint didn't come to the front, so I didn't draw anything")
        try:
            self._p().hotkey("win", "up")          # maximise — a bigger canvas
        except Exception:
            pass
        self._sleep(0.8)

    def find_canvas(self) -> tuple[tuple[int, int, int, int], Optional[tuple[int, int]], Any]:
        cap = self.grab()
        info = self.look(cap) or {}
        if not info.get("paint_open"):
            raise DrawError("I couldn't see Paint on the screen")
        box = [float(v) for v in (info.get("canvas") or [])][:4]
        if len(box) != 4:
            raise DrawError("I couldn't find Paint's canvas")
        y0, x0, y1, x1 = box
        l, t = cap.to_screen([y0, x0])
        r, b = cap.to_screen([y1, x1])
        l, r = sorted((l, r))
        t, b = sorted((t, b))
        # stay clear of the edges and the PyAutoGUI fail-safe corners
        ox, oy = cap.origin
        sw, sh = cap.screen_w, cap.screen_h
        l, t = max(l, ox + 12), max(t, oy + 12)
        r, b = min(r, ox + sw - 12), min(b, oy + sh - 12)
        if r - l < 180 or b - t < 140:
            raise DrawError("the canvas is too small to draw on — maximise Paint and try again")
        pen = info.get("pencil") or []
        pencil = cap.to_screen(pen[:2]) if len(pen) >= 2 else None
        return (l, t, r, b), pencil, cap

    def draw(self, strokes_px: list[list[tuple[int, int]]], canvas) -> int:
        p = self._p()
        l, t, r, b = canvas
        old_pause = getattr(p, "PAUSE", 0.1)
        drawn = 0
        try:
            p.PAUSE = 0.0
            for n, st in enumerate(strokes_px, 1):
                self.check()
                if not PAINT_TITLE.search(self.title() or ""):
                    raise DrawError(f"Paint is no longer in front (after {drawn} strokes), so I lifted the pen")
                if n % 10 == 1:
                    self._progress(f"Drawing… stroke {n} of {len(strokes_px)}")
                for x, y in st:                       # belt and braces: never leave the canvas
                    if not (l <= x <= r and t <= y <= b):
                        raise DrawError("a stroke left the canvas — stopped")
                p.moveTo(st[0][0], st[0][1])
                p.mouseDown()
                try:
                    for x, y in st[1:]:
                        p.moveTo(x, y)
                        self._sleep(0.004)
                finally:
                    p.mouseUp()
                drawn += 1
                self._sleep(0.02)
        finally:
            try:
                p.PAUSE = old_pause
            except Exception:
                pass
        return drawn


def _canvas_crop(cap, canvas) -> Optional[bytes]:
    """JPEG of just the canvas region of a full-screen capture."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(cap.data))
        l, t, r, b = canvas
        ox, oy = cap.origin
        sx, sy = cap.width / cap.screen_w, cap.height / cap.screen_h
        box = (int((l - ox) * sx), int((t - oy) * sy), int((r - ox) * sx), int((b - oy) * sy))
        buf = io.BytesIO()
        img.crop(box).convert("RGB").save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return None


def _ink(jpeg: Optional[bytes]) -> float:
    """Fraction of clearly dark pixels — 0 for an empty white canvas."""
    if not jpeg:
        return 0.0
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(jpeg)).convert("L").resize((160, 120))
        px = list(im.getdata())
        return sum(1 for v in px if v < 140) / len(px)
    except Exception:
        return 0.0


def _line_art(subject: str) -> tuple[str, str]:
    from core.ai_gateway import get_gateway
    from core.design_agent import _extract_svg, _valid_svg
    chosen = not subject or bool(re.search(r"\b(something|anything)\b.*\b(you like|you want|of your choice|"
                                           r"you choose)\b|^(?:something|anything)$", subject, re.I))
    ask = LINE_ART_PROMPT.format(subject="a subject of your choice" if chosen else subject,
                                 choice=CHOICE_NOTE if chosen else "")
    text = get_gateway().generate(ask, tier="deep", reasoning="low", max_output_tokens=12000, timeout=120.0).text
    svg = _extract_svg(text or "")
    if not svg:
        raise DrawError("the model didn't return a drawing")
    bad = _valid_svg(svg)
    if bad:
        raise DrawError(f"the drawing was rejected: {bad}")
    title = subject
    m = re.search(r"<title>([^<]{1,80})</title>", svg, re.I)
    if chosen:
        title = (m.group(1).strip() if m else "something I like")
    return svg, title


def t_draw_paint(args: dict, ctx) -> ToolResult:
    subject = str(args.get("subject") or args.get("prompt") or "").strip()
    ex = getattr(ctx, "extras", None) or {}
    progress = ex.get("progress", lambda m: None)
    from core import research as R
    from core.design_agent import _render_png, _slug
    from datetime import datetime
    try:
        progress("Sketching the line art…")
        svg, title = _line_art(subject)
        strokes = strokes_from_svg(svg)
        if not strokes:
            return ToolResult.failure("The drawing had no strokes I could trace.", retryable=True)
        name = _slug(title or "drawing")
        d = R.workspace("research", f"drawing {name}", reuse=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H%M")
        svg_path = d / f"{name} {stamp}.svg"
        svg_path.write_text(svg, encoding="utf-8")
        png_path = d / f"{name} {stamp}.png"
        rendered, _how = _render_png(svg, png_path)
        try:
            from core.safety import kill_switch
            kill = kill_switch()
        except Exception:
            kill = None
        painter = (ex.get("painter_factory") or Painter)(should_stop=ex.get("should_stop"), progress=progress,
                                                         kill=kill)
        painter.open_paint()
        progress("Finding the canvas…")
        canvas, pencil, before = painter.find_canvas()
        if pencil is not None:
            l, t, r, b = canvas
            painter.check()
            if PAINT_TITLE.search(painter.title() or "") and not (l <= pencil[0] <= r and t <= pencil[1] <= b):
                painter._p().click(pencil[0], pencil[1])        # the pencil tool, in Paint's toolbar
                time.sleep(0.3)
        px = to_canvas(strokes, canvas)
        n = painter.draw(px, canvas)
        time.sleep(0.6)
        after = painter.grab()
        ink_before, ink_after = _ink(_canvas_crop(before, canvas)), _ink(_canvas_crop(after, canvas))
        review = {}
        crop = _canvas_crop(after, canvas)
        if crop and ink_after > ink_before + 0.002:
            review = _review(crop, title)
    except DrawError as e:
        return ToolResult.failure(f"I couldn't finish the drawing: {e}.", retryable=False)
    except Exception as e:
        from core.safety import EmergencyStop
        if isinstance(e, EmergencyStop):
            return ToolResult.failure("Stopped by the emergency stop — the pen is lifted.")
        from core.secret_store import redact
        return ToolResult.failure(f"The drawing failed: {redact(e)[:160]}", retryable=True)
    points = sum(len(s) for s in px)
    data = {"subject": title, "strokes": n, "points": points, "canvas": list(canvas), "svg": str(svg_path),
            "png": str(png_path) if rendered else "", "ink_before": round(ink_before, 4),
            "ink_after": round(ink_after, 4), "review": review}
    if ink_after <= ink_before + 0.002:
        return ToolResult(False, data, "", f"I moved the pen through {n} strokes but the canvas didn't change — "
                          "Paint may have had another tool selected. The line art is saved as "
                          f"{svg_path.name}.")
    msg = f"Drew {title} in Paint — {n} strokes, {points} points."
    if review.get("matches") is True:
        msg += " I looked at it afterwards: it reads as " + (review.get("summary") or title)[:100] + "."
    elif review.get("matches") is False:
        msg += " Looking at it, it doesn't quite read as intended: " + (review.get("summary") or "")[:120] + "."
    return ToolResult(True, data, msg + " It's open in Paint; save it there if you want to keep it.")


def _review(jpeg: bytes, subject: str) -> dict:
    from core.ai_gateway import get_gateway
    schema = {"type": "object", "properties": {"matches": {"type": "boolean"}, "summary": {"type": "string"}},
              "required": ["matches", "summary"]}
    try:
        r = get_gateway().generate(f"This is a pen drawing on a canvas. Does it recognisably show: {subject}? "
                                   "One short sentence describing what it looks like.",
                                   images=[(jpeg, "image/jpeg")], schema=schema, tier="vision", timeout=45.0)
        return r.data if r.data is not None else r.json()
    except Exception as e:
        return {"matches": None, "summary": f"(couldn't check: {type(e).__name__})"}


def verify_draw(args, result):
    from core import verification as V
    if not result.ok:
        return V.failed(result.error)
    d = result.data or {}
    if d.get("ink_after", 0) <= d.get("ink_before", 0):
        return V.failed("the canvas did not change")
    rv = d.get("review") or {}
    if rv.get("matches") is False:
        return V.unverifiable("drawn, but it doesn't clearly look like " + str(d.get("subject"))[:60])
    if rv.get("matches") is True:
        return V.verified("ink appeared on the canvas and it looks like " + str(d.get("subject"))[:60])
    return V.unverifiable("ink appeared on the canvas (couldn't check what it looks like)")


def register(reg) -> None:
    from core.permissions import RiskLevel
    from core.tools.registry import ToolSpec
    reg.register(ToolSpec(
        "draw.paint", "Open Microsoft Paint and draw a subject with the mouse, like a person would (line art), "
        "then look at the result. 'something you like' lets JARVIS choose.", t_draw_paint,
        {"type": "object", "properties": {"subject": {"type": "string"}}, "required": []},
        {"subject": "str", "strokes": "int"}, agent="laptop", risk=RiskLevel.HIGH_RISK,
        requires_confirmation=True, retry_safe=False, timeout=360, resources=["gui"],
        confirm_text=lambda a: "use the mouse to draw " + (str(a.get("subject") or "something it likes")[:60])
        + " in Paint",
        verify=verify_draw))
