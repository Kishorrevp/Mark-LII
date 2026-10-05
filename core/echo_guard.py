"""
core/echo_guard.py — is the microphone hearing the user, or JARVIS's own voice
coming back out of the speakers?

Phase 7. The idea (comparing WHAT the mic hears with what was just played,
not only how loud it is) comes from the Mark LV assistant the user supplied;
this is an independent implementation written for this codebase.

How it decides
--------------
Both signals are reduced to energy in a handful of speech bands (sample-rate
independent: the mic runs at 16 kHz, playback at 24 kHz). For a mic block we
look for the recently played slice (last ~1.2 s) that explains it best: scale
that slice by the least-squares factor and subtract it. What is LEFT is the
part of the mic signal our own output cannot explain:

    residual ≈ 0     → it is our echo
    residual large   → a different sound — the user talking over JARVIS

The echo threshold is not a fixed number: while JARVIS speaks, residuals of
blocks that are clearly echo (quiet, or well explained) are tracked, and the
bar is set just above what this room's echo produces (mean + 3·deviation,
clamped). A room with heavy echo therefore needs a clearer voice; headphones
(no echo at all) open instantly.

Used in two places (main.py):
  * barge-in — core.voice.BargeInGate asks is_echo() before counting a loud
    block as speech, so JARVIS's own voice cannot interrupt it;
  * the echo tail — for the device's output latency + 0.25 s after JARVIS
    stops, mic blocks that are pure echo are dropped instead of being sent,
    so the model never hears (and answers) its own last words.

Thread-safety: note_output() runs on the playback path, is_echo() on the
PortAudio mic thread. Each replaces whole objects (a new tuple appended to a
deque, floats reassigned) — safe under the GIL; a stale read only costs one
block of accuracy. Never raises.
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable, Optional

import numpy as np

EDGES = (180.0, 350.0, 600.0, 950.0, 1400.0, 2100.0, 3100.0, 4500.0, 6800.0)
HISTORY_S = 1.2
QUIET = 0.02                 # mic level (0..1 of full scale) below which nothing is decided


def bands(pcm: bytes, rate: int) -> np.ndarray:
    """Energy in each speech band of a 16-bit mono block."""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if x.size < 128:
        return np.zeros(len(EDGES) - 1, dtype=np.float32)
    x -= x.mean()
    n = 1 << (x.size - 1).bit_length()
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size), n=n)) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    idx = np.searchsorted(freqs, EDGES)
    return np.array([spec[idx[i]:idx[i + 1]].sum() for i in range(len(EDGES) - 1)], dtype=np.float32)


def level(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(x * x)) / 32768.0) if x.size else 0.0


def unexplained(mic: np.ndarray, ref: np.ndarray) -> float:
    """Fraction of the mic's band energy left after removing the best-scaled ref."""
    tot = float(mic.sum())
    if tot <= 1e-9:
        return 0.0
    rr = float(np.dot(ref, ref))
    if rr <= 1e-12:
        return 1.0
    a = max(0.0, float(np.dot(mic, ref)) / rr)
    return float(np.maximum(mic - a * ref, 0.0).sum()) / tot


class EchoGuard:
    def __init__(self, mic_rate: int = 16000, out_rate: int = 24000, clock: Callable[[], float] = time.monotonic):
        self.mic_rate, self.out_rate = mic_rate, out_rate
        self._clock = clock
        self._hist: deque = deque(maxlen=64)      # (t, bands) of what was played
        self._mean, self._var, self._n = 0.12, 0.004, 0
        self.last_residual = 0.0

    # playback side
    def note_output(self, pcm: bytes, when: Optional[float] = None) -> None:
        try:
            t = self._clock() if when is None else when
            # split long writes into ~40 ms slices so each can be matched on its own
            step = int(self.out_rate * 0.04) * 2
            for i in range(0, max(len(pcm), 1), step):
                chunk = pcm[i:i + step]
                if len(chunk) >= 256:
                    self._hist.append((t, bands(chunk, self.out_rate)))
        except Exception:
            pass

    def reset(self) -> None:
        self._hist.clear()

    @property
    def threshold(self) -> float:
        return min(0.45, max(0.18, self._mean + 3.0 * math.sqrt(max(self._var, 1e-6))))

    def _learn(self, r: float) -> None:
        self._n = min(self._n + 1, 10_000)
        k = 0.06 if self._n > 20 else 0.25
        d = r - self._mean
        self._mean += k * d
        self._var = (1 - k) * (self._var + k * d * d)

    def residual(self, mic_pcm: bytes, when: Optional[float] = None) -> Optional[float]:
        """Best (smallest) unexplained fraction against recent output; None when
        nothing was played recently (then it cannot be echo)."""
        t = self._clock() if when is None else when
        recent = [b for ts, b in list(self._hist) if 0.0 <= t - ts <= HISTORY_S]
        if not recent:
            return None
        m = bands(mic_pcm, self.mic_rate)
        best = min(unexplained(m, ref) for ref in recent)
        self.last_residual = best
        return best

    def is_echo(self, mic_pcm: bytes, when: Optional[float] = None) -> bool:
        """True when this mic block is (most likely) our own voice coming back."""
        try:
            if level(mic_pcm) < QUIET:
                return False                  # too quiet to matter either way — nothing to remove
            r = self.residual(mic_pcm, when)
            if r is None:
                return False                  # nothing played → it is the user
            if r < self.threshold:
                self._learn(r)
                return True
            return False
        except Exception:
            return False
