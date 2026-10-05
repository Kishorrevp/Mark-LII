"""
core/latency.py — where the time goes between "you stop talking" and "you
hear JARVIS".

One record per answered turn:

    speech_end → first audio    you stopped speaking (last loud mic frame) →
                                first byte of the reply arrives from Gemini.
                                Includes the server's end-of-turn wait, the
                                model, and any tool calls in between.
    → tool call                 speech end → the model asked for a tool
    tools                       each tool's own run time
    first audio → speaker       reply byte arrives → handed to the sound card
    device buffer               the output device's own latency (reported by
                                the driver; what is still queued ahead)
    speech_end → heard          the total you actually feel

Written to memory/latency.jsonl (numbers and tool names only — never words
from the conversation), printed as one log line per turn, and summarised
(median of recent turns) for the HUD / settings.

Dependency-free; tested by real execution. The mic callback runs on the
PortAudio thread — it only assigns floats (atomic in CPython).
"""
from __future__ import annotations

import json
import statistics
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

VOICE_RMS = 500.0          # int16 RMS above which a mic frame counts as speech
KEEP_LINES = 500


class LatencyMeter:
    def __init__(self, path: Optional[Path] = None, clock: Callable[[], float] = time.monotonic,
                 log: Optional[Callable[[str], None]] = None):
        self.path = path
        self._clock = clock
        self._log = log
        self.recent: deque = deque(maxlen=40)
        self.device_latency = 0.0
        self._reset()

    def _reset(self) -> None:
        self._last_voice = 0.0          # last mic frame above VOICE_RMS
        self._speech_end = None         # frozen when the reply starts
        self._tool_call = None
        self._first_rx = None
        self._first_play = None
        self._tools: list[dict] = []
        self._source = "voice"

    # ── events ────────────────────────────────────────────────────────────
    def mic(self, rms: float, now: float | None = None) -> None:
        if rms >= VOICE_RMS and self._first_rx is None and self._tool_call is None:
            self._last_voice = self._clock() if now is None else now

    def typed(self, now: float | None = None) -> None:
        """A turn that started from text (dashboard / phone / system), not the mic."""
        self._reset()
        self._source = "text"
        self._last_voice = self._clock() if now is None else now

    def tool_call(self, now: float | None = None) -> None:
        if self._tool_call is None:
            self._tool_call = self._clock() if now is None else now
            if self._speech_end is None:
                self._speech_end = self._last_voice or None

    def tool_done(self, name: str, seconds: float) -> None:
        self._tools.append({"name": str(name)[:40], "s": round(float(seconds), 3)})

    def audio_in(self, now: float | None = None) -> None:
        if self._first_rx is None:
            self._first_rx = self._clock() if now is None else now
            if self._speech_end is None:
                self._speech_end = self._last_voice or None

    def played(self, now: float | None = None) -> None:
        if self._first_play is None and self._first_rx is not None:
            self._first_play = self._clock() if now is None else now

    def turn_done(self, now: float | None = None) -> Optional[dict]:
        """Close the turn; returns the record (None when nothing was answered).
        A turn that ended right after a tool call, before any audio, is NOT
        closed: models with asynchronous function calling (gemini-3.8-live)
        finish the calling turn first and answer in the next one."""
        if self._first_rx is None and self._tool_call is not None:
            return None
        rec = None
        if self._first_rx is not None:
            se = self._speech_end
            rec = {"ts": round(time.time(), 1), "source": self._source}
            if se:
                rec["speech_to_audio"] = round(self._first_rx - se, 3)
                if self._tool_call is not None:
                    rec["speech_to_tool_call"] = round(self._tool_call - se, 3)
            if self._first_play is not None:
                rec["audio_to_speaker"] = round(self._first_play - self._first_rx, 3)
            rec["device_buffer"] = round(self.device_latency, 3)
            if se and self._first_play is not None:
                rec["speech_to_heard"] = round(self._first_play - se + self.device_latency, 3)
            if self._tools:
                rec["tools"] = list(self._tools)
            self.recent.append(rec)
            self._write(rec)
            if self._log:
                try:
                    self._log(self.line(rec))
                except Exception:
                    pass
        self._reset()
        return rec

    def cancel(self) -> None:
        """An interrupted turn — nothing to report."""
        self._reset()

    # ── reporting ─────────────────────────────────────────────────────────
    @staticmethod
    def line(rec: dict) -> str:
        bits = []
        if "speech_to_audio" in rec:
            bits.append(f"speech end → first audio {rec['speech_to_audio']:.2f}s")
        if "speech_to_tool_call" in rec:
            bits.append(f"→ tool call {rec['speech_to_tool_call']:.2f}s")
        for t in rec.get("tools") or []:
            bits.append(f"{t['name']} {t['s']:.2f}s")
        if "audio_to_speaker" in rec:
            bits.append(f"→ speaker {rec['audio_to_speaker'] * 1000:.0f}ms")
        if rec.get("device_buffer"):
            bits.append(f"device buffer {rec['device_buffer'] * 1000:.0f}ms")
        if "speech_to_heard" in rec:
            bits.append(f"heard after {rec['speech_to_heard']:.2f}s")
        return "[Latency] " + (" · ".join(bits) if bits else "no timing")

    def summary(self) -> dict:
        """Median of recent voice turns without tools / with tools."""
        plain = [r["speech_to_heard"] for r in self.recent
                 if r.get("source") == "voice" and "speech_to_heard" in r and not r.get("tools")]
        tooled = [r["speech_to_heard"] for r in self.recent
                  if r.get("source") == "voice" and "speech_to_heard" in r and r.get("tools")]
        return {"turns": len(self.recent),
                "median_heard_s": round(statistics.median(plain), 2) if plain else None,
                "median_heard_with_tools_s": round(statistics.median(tooled), 2) if tooled else None,
                "device_buffer_ms": round(self.device_latency * 1000)}

    def _write(self, rec: dict) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            if self.path.exists():
                lines = [ln for ln in self.path.read_bytes().decode("utf-8", "replace").split("\r\n") if ln.strip()]
            lines.append(json.dumps(rec, sort_keys=True))
            lines = lines[-KEEP_LINES:]
            self.path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
        except Exception:
            pass
