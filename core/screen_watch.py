"""
core/screen_watch.py — "watch my screen": JARVIS keeps seeing the screen while
you talk, instead of one snapshot per question.

While watching, a frame of the primary screen is sent to the Gemini Live
session (as realtime video input) whenever the picture CHANGES, at most one
frame every `interval` seconds (default 2 s), plus a refresh every `keepalive`
seconds (default 20 s) so the model's view never goes stale. Unchanged screens
cost nothing.

Only when asked (the user's choice): "watch my screen" starts it, "stop
watching" ends it, and it also runs while JARVIS itself is drawing / using
the computer, ending with that task. It always stops by itself after
`max_minutes` (default 10). The HUD shows a red EYE while it is on.

Privacy:
  * frames go to the Live model only — nothing is written to disk;
  * while the window in front looks like a sign-in / password / banking
    screen, frames are NOT sent (the watcher says "paused: private window");
  * the emergency stop ends it.

Dependency-free core (asyncio); capture/send/title are injected.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Awaitable, Callable, Optional

PRIVATE = re.compile(r"(password|passwort|1password|bitwarden|lastpass|keepass|dashlane|credential|"
                     r"sign[- ]?in|log[- ]?in|authenticator|net ?banking|bank|wallet|\botp\b|private browsing|"
                     r"inprivate|incognito)", re.I)


class ScreenWatcher:
    def __init__(self, *, grab: Callable[[], object], send: Callable[[bytes, str], Awaitable[None]],
                 title: Callable[[], str] = lambda: "", change: Optional[Callable[[object, object], float]] = None,
                 on_state: Callable[[bool, str], None] = lambda on, why: None,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.monotonic,
                 interval: float = 2.0, keepalive: float = 20.0, threshold: float = 0.02):
        self._grab, self._send, self._title = grab, send, title
        self._change = change
        self._on_state, self._log, self._clock = on_state, log, clock
        self.interval, self.keepalive, self.threshold = interval, keepalive, threshold
        self._task: Optional[asyncio.Task] = None
        self._until = 0.0
        self.reason = ""
        self.frames_sent = 0
        self.paused_private = False
        self._while: Optional[Callable[[], bool]] = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, minutes: float = 10.0, reason: str = "asked", while_true: Optional[Callable[[], bool]] = None) -> str:
        minutes = max(0.5, min(float(minutes or 10.0), 60.0))
        self._until = self._clock() + minutes * 60.0
        self.reason = reason
        self._while = while_true
        if self.active:
            return f"Already watching the screen — extended to {minutes:g} more minutes."
        self.frames_sent = 0
        self._task = asyncio.get_event_loop().create_task(self._run())
        self._on_state(True, reason)
        return f"Watching the screen now (for up to {minutes:g} minutes, a frame whenever it changes)."

    def stop(self, why: str = "asked") -> str:
        if not self.active:
            return "I wasn't watching the screen."
        self._task.cancel()
        self._task = None
        self._on_state(False, why)
        return f"Stopped watching the screen ({self.frames_sent} frame(s) seen)."

    def status(self) -> str:
        if not self.active:
            return "Not watching the screen."
        left = max(0, int(self._until - self._clock()))
        return (f"Watching the screen ({self.reason}); {self.frames_sent} frame(s) sent; stops in {left // 60}m "
                f"{left % 60}s" + ("; paused on a private window" if self.paused_private else "") + ".")

    async def _run(self) -> None:
        last = None
        last_sent = 0.0
        loop = asyncio.get_event_loop()
        try:
            while self._clock() < self._until:
                if self._while is not None:
                    try:
                        if not self._while():
                            break
                    except Exception:
                        break
                try:
                    title = self._title() or ""
                except Exception:
                    title = ""
                if PRIVATE.search(title):
                    if not self.paused_private:
                        self.paused_private = True
                        self._log("[Watch] paused: private window in front — no frames sent")
                    await asyncio.sleep(self.interval)
                    continue
                self.paused_private = False
                try:
                    cap = await loop.run_in_executor(None, self._grab)
                except Exception as e:
                    self._log(f"[Watch] capture failed: {type(e).__name__}")
                    await asyncio.sleep(self.interval * 2)
                    continue
                now = self._clock()
                changed = 1.0 if last is None or self._change is None else self._change(last, cap)
                if changed >= self.threshold or now - last_sent >= self.keepalive:
                    try:
                        await self._send(cap.data, cap.mime)
                        self.frames_sent += 1
                        last_sent = now
                        last = cap
                    except Exception as e:
                        self._log(f"[Watch] send failed: {type(e).__name__}")
                        break
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        finally:
            if self._task is not None and self._task is asyncio.current_task():
                self._task = None
                self._on_state(False, "finished")
