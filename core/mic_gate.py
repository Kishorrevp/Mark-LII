"""
core/mic_gate.py — when the microphone is streamed to the voice model.

Phase 7 (feature ideas from the Mark LV assistant the user supplied; written
independently here). config/runtime.json → voice:

    "mic_mode": "always"        JARVIS hears everything (the default, as before)
                "wake_word"     nothing is sent until "Hey JARVIS" is heard on THIS
                                PC (openWakeWord, local, free); then the mic stays
                                open for `wake_window_s` seconds after the last
                                thing either side said
                "push_to_talk"  only while `ptt_key` is held (works while another
                                window has focus on Windows), plus a short tail
    "wake_word": "hey_jarvis", "wake_threshold": 0.5, "wake_window_s": 20,
    "ptt_key": "F8"

Honesty rules: if openWakeWord is not installed (pip install openwakeword) or
its model cannot load, wake-word mode says so ONCE and falls back to "always"
— it never silently stops listening. Detection runs locally; audio is never
sent anywhere for it.

MicGate is dependency-free and tested; WakeWordDetector and HotkeyWatcher are
thin adapters around optional packages / the Windows API.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

MODES = ("always", "wake_word", "push_to_talk")


class MicGate:
    def __init__(self, mode: str = "always", window_s: float = 20.0, ptt_tail_s: float = 0.4,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print):
        self.mode = mode if mode in MODES else "always"
        self.window_s = max(5.0, float(window_s or 20.0))
        self.ptt_tail_s = ptt_tail_s
        self._clock = clock
        self._log = log
        self._open_until = 0.0
        self._pressed = False
        self._released_at = 0.0
        self.wakes = 0

    # ── events ─────────────────────────────────────────────────────────────
    def wake(self) -> None:
        """The wake word was heard."""
        self.wakes += 1
        self._open_until = self._clock() + self.window_s

    def activity(self) -> None:
        """Either side spoke — keep a wake-word conversation open."""
        if self.mode == "wake_word" and self._clock() < self._open_until:
            self._open_until = self._clock() + self.window_s

    def key(self, pressed: bool) -> None:
        if self._pressed and not pressed:
            self._released_at = self._clock()
        self._pressed = bool(pressed)

    def fallback(self, why: str) -> None:
        if self.mode != "always":
            self._log(f"[Mic] {self.mode.replace('_', ' ')} unavailable ({why}) — listening normally instead.")
        self.mode = "always"

    # ── the question asked for every mic block ─────────────────────────────
    def allow(self) -> bool:
        if self.mode == "always":
            return True
        now = self._clock()
        if self.mode == "push_to_talk":
            return self._pressed or (now - self._released_at) < self.ptt_tail_s
        return now < self._open_until

    def status(self) -> str:
        if self.mode == "always":
            return "LIVE"
        if self.mode == "push_to_talk":
            return "TALK" if self.allow() else "HOLD KEY"
        return "AWAKE" if self.allow() else "WAKE WORD"


class WakeWordDetector:
    """openWakeWord on a background thread. feed() is cheap (queues the block);
    on_wake() is called from the detector thread."""

    FRAME = 1280                      # 80 ms at 16 kHz, what openWakeWord expects

    def __init__(self, word: str = "hey_jarvis", threshold: float = 0.5,
                 on_wake: Callable[[], None] = lambda: None, log: Callable[[str], None] = print):
        self.word, self.threshold = word, float(threshold or 0.5)
        self._on_wake, self._log = on_wake, log
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=64)
        self._buf = b""
        self._model = None
        self._last = 0.0
        self.error = ""

    def start(self) -> bool:
        try:
            import numpy as np  # noqa: F401
            from openwakeword.model import Model
            try:
                self._model = Model(wakeword_models=[self.word], inference_framework="onnx")
            except TypeError:
                self._model = Model(wakeword_models=[self.word])
        except Exception as e:
            self.error = f"{type(e).__name__}: {str(e)[:120]}"
            return False
        threading.Thread(target=self._run, daemon=True, name="wake-word").start()
        return True

    def feed(self, block: bytes) -> None:
        try:
            self._q.put_nowait(block)
        except queue.Full:
            pass

    def _run(self) -> None:
        import numpy as np
        while True:
            self._buf += self._q.get()
            while len(self._buf) >= self.FRAME * 2:
                frame, self._buf = self._buf[:self.FRAME * 2], self._buf[self.FRAME * 2:]
                try:
                    scores = self._model.predict(np.frombuffer(frame, dtype=np.int16))
                except Exception as e:
                    self._log(f"[Mic] wake-word detector error: {type(e).__name__}")
                    continue
                score = max(scores.values()) if scores else 0.0
                if score >= self.threshold and time.monotonic() - self._last > 1.5:
                    self._last = time.monotonic()
                    try:
                        self._on_wake()
                    except Exception:
                        pass


_VK = {f"f{i}": 0x6F + i for i in range(1, 13)}
_VK.update({"space": 0x20, "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "capslock": 0x14,
            "scrolllock": 0x91, "pause": 0x13, "insert": 0x2D})


def parse_chord(chord: str) -> list[int]:
    keys = []
    for part in str(chord or "F8").lower().replace(" ", "").split("+"):
        if part in _VK:
            keys.append(_VK[part])
        elif len(part) == 1 and part.isalnum():
            keys.append(ord(part.upper()))
    return keys or [_VK["f8"]]


class HotkeyWatcher:
    """Windows: polls GetAsyncKeyState 40×/s — works while any window has focus,
    needs no hook and no extra package. Elsewhere it reports unavailable."""

    def __init__(self, chord: str, on_change: Callable[[bool], None]):
        self.keys = parse_chord(chord)
        self._on_change = on_change
        self.error = ""

    def start(self) -> bool:
        try:
            import ctypes
            user32 = ctypes.windll.user32            # type: ignore[attr-defined]
        except Exception as e:
            self.error = f"not on Windows ({type(e).__name__})"
            return False

        def run():
            down = False
            while True:
                now = all(user32.GetAsyncKeyState(k) & 0x8000 for k in self.keys)
                if now != down:
                    down = now
                    try:
                        self._on_change(down)
                    except Exception:
                        pass
                time.sleep(0.025)
        threading.Thread(target=run, daemon=True, name="push-to-talk").start()
        return True
