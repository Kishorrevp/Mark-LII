"""
core/vision.py — seeing the screen, windows, documents, images and the camera.

    capture(target)      "screen" (primary monitor) · "window" (the foreground
                         window, cropped) · "camera" · a file path. Returns the
                         image, its size, and the mapping back to real screen
                         pixels (needed by the computer-use agent).
    list_windows()       every visible top-level window: title, process, rect,
                         active/minimised — local, no model call
    analyze(mode, …)     one vision-tier model call with a prompt built for
                         the mode:

        general    what is on screen, what the user is doing
        ui         interactive elements with positions (buttons, fields, menus)
        errors     error dialogs / stack traces / red text → cause + fix
        code       read the code on screen → explain / spot bugs
        chart      read a chart: type, axes, values, trend, takeaway
        document   transcribe / summarise a page (OCR-style)
        camera     the physical environment in front of the webcam
        image      an image file on disk

Privacy: capture happens only when a tool is invoked for it; the image goes
to the configured AI provider for that one request and is not stored (the
computer-use agent keeps nothing on disk either).

Part One — current-frame guarantees:
    * ONE capture path: actions/screen_processor's screen capture delegates
      here (the Live screen_process tool, analyze_screen and the computer
      agent all use capture()).
    * Every Capture carries `at` (wall-clock time of the grab); fresh() re-grabs
      when a frame is older than `max_age_s`, so the image answered about is
      never an old one.
    * assess() says when a frame cannot support an answer (tiny, black,
      blank/uniform — e.g. a locked screen or DRM video) so JARVIS says so
      instead of guessing.
    * The mime type is taken from the bytes (core/providers/base.sniff_image_mime).
"""
from __future__ import annotations

import io
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core.tools.registry import ToolResult

MAX_SIDE = 1600


@dataclass
class Capture:
    data: bytes
    mime: str
    width: int                    # image pixels (after downscale)
    height: int
    screen_w: int                 # real pixels the image covers
    screen_h: int
    origin: tuple = (0, 0)        # screen coords of the image's top-left
    source: str = "screen"
    title: str = ""
    gray_small: list = field(default_factory=list)   # 32×18 thumbnail for change detection
    at: float = field(default_factory=time.time)     # when the frame was grabbed (wall clock)

    def age_s(self, now: Optional[float] = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.at)

    def to_screen(self, point_yx_1000) -> tuple[int, int]:
        """Gemini-style normalised [y, x] (0–1000) → absolute screen pixel."""
        y, x = float(point_yx_1000[0]), float(point_yx_1000[1])
        y, x = max(0.0, min(1000.0, y)), max(0.0, min(1000.0, x))
        return (int(self.origin[0] + x / 1000.0 * self.screen_w),
                int(self.origin[1] + y / 1000.0 * self.screen_h))


def _thumb(img) -> list:
    try:
        return list(img.convert("L").resize((32, 18)).getdata())
    except Exception:
        return []


def change_ratio(a: Capture, b: Capture) -> float:
    """0.0 = identical, 1.0 = completely different (coarse, cheap)."""
    if not a.gray_small or not b.gray_small or len(a.gray_small) != len(b.gray_small):
        return 1.0
    diff = sum(1 for x, y in zip(a.gray_small, b.gray_small) if abs(x - y) > 12)
    return diff / len(a.gray_small)


def _encode(img, max_side: int = MAX_SIDE) -> tuple[bytes, str, int, int]:
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue(), "image/jpeg", img.size[0], img.size[1]


# ── windows ──────────────────────────────────────────────────────────────────

def list_windows() -> list[dict]:
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        fg = user32.GetForegroundWindow()
        out: list[dict] = []
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            iconic = bool(user32.IsIconic(hwnd))
            # minimised windows report a tiny off-screen rect — keep them
            if not iconic and (r.right - r.left < 50 or r.bottom - r.top < 30):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc = ""
            try:
                import psutil
                proc = psutil.Process(pid.value).name()
            except Exception:
                pass
            out.append({"title": buf.value[:160], "process": proc, "pid": int(pid.value),
                        "rect": [r.left, r.top, r.right, r.bottom], "active": hwnd == fg,
                        "minimized": iconic, "hwnd": int(hwnd)})
            return True
        user32.EnumWindows(EnumProc(cb), 0)
        return out[:60]
    except Exception:
        return []


def active_window() -> dict:
    for w in list_windows():
        if w.get("active"):
            return w
    try:
        from core.context_engine import active_window as _aw
        return _aw()
    except Exception:
        return {}


def window_text(title_part: str = "") -> str:
    """Best-effort read of the text inside the foreground window's edit controls
    (Windows UI Automation, then a Win32 WM_GETTEXT fallback). Used to VERIFY a
    typing action really landed — more reliable than reading pixels. Returns ''
    when nothing can be read (never raises)."""
    if os.name != "nt":
        return ""
    # 1) UI Automation (covers modern Notepad, most apps) if a provider is installed
    try:
        import uiautomation as auto  # type: ignore
        win = auto.GetForegroundControl()
        if win is not None:
            texts: list[str] = []

            def walk(ctrl, depth=0):
                if depth > 6 or len(texts) > 40:
                    return
                try:
                    v = ctrl.GetValuePattern().Value
                    if v:
                        texts.append(v)
                except Exception:
                    pass
                try:
                    if not texts and ctrl.Name:
                        pass
                except Exception:
                    pass
                for ch in ctrl.GetChildren():
                    walk(ch, depth + 1)
            walk(win)
            joined = "\n".join(t for t in texts if t).strip()
            if joined:
                return joined[:4000]
    except Exception:
        pass
    # 2) Win32 fallback: WM_GETTEXT on a classic Edit/RichEdit child
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        WM_GETTEXT, WM_GETTEXTLENGTH = 0x000D, 0x000E
        found: list[str] = []

        def read(h):
            n = user32.SendMessageW(h, WM_GETTEXTLENGTH, 0, 0)
            if n and n < 100000:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.SendMessageW(h, WM_GETTEXT, n + 1, ctypes.byref(buf))
                if buf.value.strip():
                    found.append(buf.value)

        EnumChild = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(child, _):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(child, cls, 64)
            if any(k in cls.value for k in ("Edit", "RICHEDIT", "RichEdit", "TextBox")):
                read(child)
            return True
        user32.EnumChildWindows(hwnd, EnumChild(cb), 0)
        if not found:
            read(hwnd)
        return ("\n".join(found)).strip()[:4000]
    except Exception:
        return ""


def focus_window(title_part: str) -> bool:
    """Bring a window whose title contains `title_part` to the front."""
    if os.name != "nt" or not title_part:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        want = title_part.lower()
        for w in list_windows():
            if want in w["title"].lower() or want in (w.get("process") or "").lower():
                hwnd = w["hwnd"]
                if w.get("minimized"):
                    user32.ShowWindow(hwnd, 9)            # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                return True
    except Exception:
        pass
    return False


# ── capture ──────────────────────────────────────────────────────────────────

def capture(target: str = "screen", max_side: int = MAX_SIDE) -> Capture:
    t = (target or "screen").strip()
    from PIL import Image
    if t == "camera":
        from actions.screen_processor import _capture_camera
        data, mime = _capture_camera()
        img = Image.open(io.BytesIO(data))
        return Capture(data, mime, img.size[0], img.size[1], img.size[0], img.size[1], source="camera",
                       gray_small=_thumb(img))
    if t not in ("screen", "window"):
        p = Path(os.path.expandvars(os.path.expanduser(t)))
        if not p.is_file():
            raise FileNotFoundError(f"No image at {p}")
        img = Image.open(p)
        data, mime, w, h = _encode(img, max_side)
        return Capture(data, mime, w, h, img.size[0], img.size[1], source="file", title=p.name,
                       gray_small=_thumb(img))
    import mss
    with mss.mss() as sct:
        mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        region = {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}
        title = ""
        if t == "window":
            w = active_window()
            if w.get("rect"):
                l, tp, r, b = w["rect"]
                l, tp = max(l, mon["left"]), max(tp, mon["top"])
                r, b = min(r, mon["left"] + mon["width"]), min(b, mon["top"] + mon["height"])
                if r - l > 50 and b - tp > 30:
                    region = {"left": l, "top": tp, "width": r - l, "height": b - tp}
                title = w.get("title", "")
        shot = sct.grab(region)
        img = Image.frombytes("RGB", shot.size, shot.rgb)
    data, mime, w, h = _encode(img, max_side)
    return Capture(data, mime, w, h, region["width"], region["height"], (region["left"], region["top"]),
                   source=t, title=title, gray_small=_thumb(img))


def assess(cap: Capture) -> list[str]:
    """Reasons this frame may not be enough to answer from ([] = looks usable)."""
    notes = []
    if cap.width and cap.height and (cap.width < 64 or cap.height < 64):
        notes.append(f"the image is tiny ({cap.width}×{cap.height})")
    g = cap.gray_small or []
    if g:
        n = len(g)
        mean = sum(g) / n
        var = sum((x - mean) ** 2 for x in g) / n
        sd = var ** 0.5
        if mean < 12 and sd < 6:
            notes.append("the frame is almost completely black (screen off, locked, or protected content)")
        elif sd < 2.5:
            notes.append("the frame is a single flat colour (nothing readable is visible)")
        elif mean > 247 and sd < 5:
            notes.append("the frame is almost completely white")
    if not cap.data:
        notes.append("no image data was captured")
    return notes


def fresh(cap: Optional[Capture], target: str = "screen", max_age_s: float = 2.5,
          max_side: int = MAX_SIDE, grab: Optional[Callable[..., Capture]] = None) -> tuple[Capture, bool]:
    """Return (frame, recaptured). Re-grabs when `cap` is missing or older than
    `max_age_s` — the answer is always about the screen as it is NOW."""
    if cap is not None and cap.age_s() <= max_age_s:
        return cap, False
    g = grab or capture
    return g(target, max_side=max_side), True


def describe_frame(cap: Capture, now: Optional[float] = None) -> str:
    from datetime import datetime
    t = datetime.fromtimestamp(cap.at).strftime("%H:%M:%S")
    return f"{cap.source} frame {cap.width}×{cap.height}, captured {t} ({cap.age_s(now):.1f} s ago)"


# ── analysis ─────────────────────────────────────────────────────────────────

MODES = {
    "general": "Describe what is on this screen: which app/page is in front, what the user appears to be doing, "
               "and anything that needs attention. Be concise.",
    "ui": "List the interactive elements visible (buttons, links, text fields, menus, tabs, checkboxes) with "
          "their visible label and state. Mention which one looks like the primary action.",
    "errors": "Find any error, warning, crash dialog, failed build/test output or red text. Quote the exact error "
              "message, explain the most likely cause in plain language, and give the concrete fix steps. "
              "If there is no error visible, say so.",
    "code": "Read the code visible on screen. Say what language/file it is, what it does, and point out bugs, "
            "risky patterns or improvements with line references where visible.",
    "chart": "Read the chart/graph: type, title, axes and units, the key values you can read, the trend, "
             "outliers, and the one-sentence takeaway. Say which numbers are estimates.",
    "document": "Transcribe the readable text of this document/page faithfully (keep headings and lists), then "
                "give a 3-bullet summary. Mark unreadable parts as [unclear].",
    "camera": "Describe what the webcam sees: the environment, objects, people (no identification of who "
              "they are), lighting, and anything notable or unsafe.",
    "image": "Describe this image in detail and answer the question about it.",
}
_DEEPER = {"errors", "code", "chart"}


def analyze(mode: str = "general", question: str = "", target: str = "screen",
            cap: Optional[Capture] = None, model: Optional[Callable] = None) -> tuple[str, Capture]:
    mode = mode if mode in MODES else "general"
    if cap is None:
        cap = capture("camera" if mode == "camera" else target)
    prompt = MODES[mode] + (f"\nThe user asks: {question}" if question else "")
    if cap.title:
        prompt += f"\n(Window: {cap.title})"
    issues = assess(cap)
    prompt += (f"\nThis frame was captured just now ({describe_frame(cap)}). If it does not show enough to "
               f"answer, say exactly what is missing instead of guessing.")
    if issues:
        prompt += "\nCapture check: " + "; ".join(issues) + "."
    if model is not None:
        return model(prompt, cap), cap
    from core.ai_gateway import get_gateway
    text = get_gateway().vision(prompt, cap.data, cap.mime, tier="vision",
                                reasoning="medium" if mode in _DEEPER else None, timeout=60.0)
    return (text or "").strip(), cap


# ── tools ────────────────────────────────────────────────────────────────────

def t_screen_analyze(args: dict, ctx) -> ToolResult:
    mode = str(args.get("mode") or "general").lower()
    target = str(args.get("target") or ("camera" if mode == "camera" else "screen"))
    if args.get("path"):
        target, mode = str(args["path"]), (mode if mode != "general" else "image")
    try:
        text, cap = analyze(mode, str(args.get("question") or ""), target)
    except Exception as e:
        from core.secret_store import redact
        return ToolResult.failure(f"I couldn't look at the {target}: {redact(e)[:160]}", retryable=True)
    if not text:
        return ToolResult.failure("The vision model returned nothing.", retryable=True)
    issues = assess(cap)
    msg = text + (f"\n[CAPTURE NOTE] {'; '.join(issues)} — tell the user the image may not show enough."
                  if issues else "")
    return ToolResult(True, {"description": text, "mode": mode, "source": cap.source, "window": cap.title,
                             "captured_at": cap.at, "frame": describe_frame(cap), "issues": issues}, msg)


def t_window_list(args: dict, ctx) -> ToolResult:
    wins = list_windows()
    if not wins:
        return ToolResult.failure("I can't list windows on this system.")
    act = next((w for w in wins if w["active"]), None)
    names = [f"{w['title']} ({w['process']})" + (" — minimised" if w["minimized"] else "") for w in wins[:15]]
    msg = (f"In front: {act['title']}. " if act else "") + f"{len(wins)} windows open: " + "; ".join(names[:8])
    return ToolResult(True, {"windows": [{k: v for k, v in w.items() if k != "hwnd"} for w in wins],
                             "active": act["title"] if act else ""}, msg)


def register(reg) -> None:
    from core.permissions import RiskLevel
    from core.tools.registry import ToolSpec
    from core import verification as V
    STR = {"type": "string"}
    reg.register(ToolSpec(
        "screen.analyze", "Look at the screen / active window / camera / an image file in a specific mode "
        "(general, ui, errors, code, chart, document, camera, image) and answer.", t_screen_analyze,
        {"type": "object", "properties": {"mode": STR, "question": STR, "target": STR, "path": STR}, "required": []},
        {"description": "str"}, agent="vision", risk=RiskLevel.SAFE, retry_safe=True,
        verify=lambda a, r: V.text_nonempty(r.data.get("description"), "description") if r.ok else V.failed(r.error)))
    reg.register(ToolSpec(
        "window.list", "List the open windows and which one is in front (local, no AI call).", t_window_list,
        {"type": "object", "properties": {}, "required": []}, {"windows": "list"}, agent="vision",
        risk=RiskLevel.SAFE, retry_safe=True))
