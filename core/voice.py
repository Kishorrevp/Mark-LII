"""
core/voice.py — two-way voice: explicit states, interruption, identities.

The voice pipeline itself stays Gemini Live native audio (lowest latency,
Tamil/English, affective dialog, session resumption). This module adds what
it lacked, without a second voice engine:

STATES   one machine, every transition recorded with a reason
             idle ─► listening ─► thinking ─► speaking ─► listening
                          ▲            │           │
                          └── interrupted ◄────────┘      error (any) ─► idle/listening
         → HUD: idle=SLEEPING, listening=LISTENING, thinking=THINKING,
           speaking=SPEAKING, interrupted=LISTENING (+ log), error=ERROR

INTERRUPTION
    * ESC / the HUD button             main.interrupt() (unchanged, ~50 ms)
    * the server's own barge-in        server_content.interrupted → drain
    * a spoken stop phrase             "stop", "wait", "hold on", "JARVIS, stop",
                                       "niruthu", "போதும்" … (is_stop_phrase) seen in
                                       the live input transcription → interrupt
    * BargeInGate decides whether the mic may reach the server WHILE JARVIS
      is speaking. Before Part One the mic was hard-gated during speech, so a
      spoken "stop" could never arrive.
          off     old behaviour (gated; ESC only)
          always  full duplex (headphones — no echo)
          auto    headphones → always; speakers → adaptive echo gate: it learns
                  how loud JARVIS's own voice is at the mic (echo coupling) and
                  opens only for sustained near-field speech well above that,
                  then replays ~300 ms of pre-roll so the first word is heard.

END OF TURN  config/runtime.json → voice.end_of_turn_silence_ms (one number;
             null = the server default) → Live realtime_input_config.

IDENTITIES   JARVIS / FRIDAY are identities, not Gemini voice names. They map
             to a prebuilt Live voice (Charon / Kore by default, editable in
             config/persona.json) so selecting them can never send an invalid
             voice to Gemini. Optional external TTS providers (Edge, Kokoro,
             ElevenLabs — core/tts.py) are registered for speech outside the
             Live session; none is required, ElevenLabs is never mandatory.

Dependency-free (numpy-free maths on plain floats); tested by real execution.
"""
from __future__ import annotations

import enum
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional


class VoiceState(str, enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    ERROR = "error"


HUD_FOR = {VoiceState.IDLE: "SLEEPING", VoiceState.LISTENING: "LISTENING", VoiceState.THINKING: "THINKING",
           VoiceState.SPEAKING: "SPEAKING", VoiceState.INTERRUPTED: "LISTENING", VoiceState.ERROR: "ERROR"}

# transitions that make sense; anything else is still applied but counted as
# "unexpected" so preflight / tests can see a broken flow
_ALLOWED = {
    VoiceState.IDLE: {VoiceState.LISTENING, VoiceState.THINKING, VoiceState.ERROR},
    VoiceState.LISTENING: {VoiceState.THINKING, VoiceState.SPEAKING, VoiceState.IDLE, VoiceState.ERROR,
                           VoiceState.INTERRUPTED},
    VoiceState.THINKING: {VoiceState.SPEAKING, VoiceState.LISTENING, VoiceState.IDLE, VoiceState.ERROR,
                          VoiceState.INTERRUPTED},
    VoiceState.SPEAKING: {VoiceState.LISTENING, VoiceState.INTERRUPTED, VoiceState.THINKING, VoiceState.IDLE,
                          VoiceState.ERROR},
    VoiceState.INTERRUPTED: {VoiceState.LISTENING, VoiceState.THINKING, VoiceState.SPEAKING, VoiceState.IDLE,
                             VoiceState.ERROR},
    VoiceState.ERROR: {VoiceState.IDLE, VoiceState.LISTENING, VoiceState.THINKING},
}


class VoiceStateMachine:
    def __init__(self, on_change: Optional[Callable[["VoiceState", "VoiceState", str], None]] = None,
                 clock: Callable[[], float] = time.monotonic, history: int = 50):
        self.state = VoiceState.IDLE
        self.since = clock()
        self._clock = clock
        self._on_change = on_change
        self._lock = threading.Lock()
        self.history: deque = deque(maxlen=history)
        self.unexpected = 0
        self.interruptions = 0

    def set(self, new: "VoiceState | str", reason: str = "") -> bool:
        new = VoiceState(new) if not isinstance(new, VoiceState) else new
        with self._lock:
            old = self.state
            if new == old:
                return False
            if new not in _ALLOWED.get(old, set()):
                self.unexpected += 1
            if new == VoiceState.INTERRUPTED:
                self.interruptions += 1
            self.state = new
            self.since = self._clock()
            self.history.append((round(self.since, 3), old.value, new.value, reason[:60]))
        cb = self._on_change
        if cb is not None:
            try:
                cb(old, new, reason)
            except Exception:
                pass
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state.value, "for_s": round(self._clock() - self.since, 2),
                    "interruptions": self.interruptions, "unexpected": self.unexpected,
                    "recent": list(self.history)[-8:]}


# ── stop phrases ────────────────────────────────────────────────────────────

_STOP_SET = {
    "stop", "wait", "hold on", "hang on", "pause", "quiet", "be quiet", "shut up", "enough", "thats enough",
    "that is enough", "stop talking", "stop it", "stop there", "one sec", "one second", "one moment",
    "wait a second", "wait a sec", "wait a minute", "hold on a second", "hold on a sec", "stop stop",
    "ok stop", "okay stop", "no stop", "stop please", "please stop", "wait wait", "hold up",
    "niruthu", "nirutthu", "nillu", "podhum", "pothum", "wait pannu", "stop pannu",
    "நிறுத்து", "போதும்", "நில்லு", "இரு",
}
_NAMES = {"jarvis", "friday", "hey", "hey jarvis", "hey friday"}


def _norm_utt(text: str) -> str:
    s = str(text or "").lower()
    s = re.sub(r"[’'`]", "", s)
    s = re.sub(r"[^\w஀-௿ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_stop_phrase(text: str) -> bool:
    """True when the utterance IS a stop command ("stop", "JARVIS, stop",
    "hold on", "wait a second", "niruthu"). Exact phrases only — "wait for
    five minutes then remind me" or "stop the music" are requests, not
    interruptions, and go to the model."""
    s = _norm_utt(text)
    if not s:
        return False
    words = [w for w in s.split() if w not in ("jarvis", "friday", "hey", "please", "just", "um", "uh")]
    core = " ".join(words)
    if core in _STOP_SET:
        return True
    # "stop stop stop", "wait wait"
    return bool(words) and len(words) <= 4 and all(w in ("stop", "wait") for w in words)


# ── barge-in gate ───────────────────────────────────────────────────────────

_HEADPHONE_RX = re.compile(r"(?i)head ?phone|head ?set|ear ?bud|ear ?phone|airpods|buds|hands-?free|earpiece")


def looks_like_headphones(device_name: str) -> bool:
    return bool(_HEADPHONE_RX.search(str(device_name or "")))


def rms_int16(data: bytes) -> float:
    """RMS of little-endian int16 PCM without numpy."""
    n = len(data) // 2
    if n <= 0:
        return 0.0
    import array
    a = array.array("h")
    a.frombytes(data[: n * 2])
    if a.itemsize != 2:
        return 0.0
    step = max(1, n // 512)                    # sample ≤512 points: cheap enough for a 64 ms block
    acc, cnt = 0.0, 0
    for i in range(0, n, step):
        v = a[i]
        acc += v * v
        cnt += 1
    return math.sqrt(acc / max(1, cnt))


@dataclass
class BargeInGate:
    mode: str = "auto"                 # off | always | auto
    headphones: bool = False
    ratio: float = 3.0                 # speech must be this many times the learned echo level
    floor_rms: float = 450.0           # and above this absolute level
    min_ms: float = 300.0              # sustained this long
    block_ms: float = 40.0             # nominal; Phase 7 measures each block (640 samples @ 16 kHz = 40 ms)
    preroll_ms: float = 320.0
    coupling: float = 0.35             # learned mic_rms / playback_rms (echo), EMA
    _play_rms: float = 0.0
    _above: int = 0
    _open: bool = False
    _pre: deque = field(default_factory=lambda: deque(maxlen=12))
    echo_guard: object = None          # Phase 7: optional core.echo_guard.EchoGuard (content check)
    opened: int = 0                    # how many times barge-in opened (stats)

    def effective_mode(self) -> str:
        if self.mode == "auto":
            return "always" if self.headphones else "adaptive"
        return self.mode if self.mode in ("off", "always", "adaptive") else "off"

    def update_playback(self, rms: float) -> None:
        self._play_rms = max(0.0, float(rms))

    def reset(self) -> None:
        self._above = 0
        self._open = False
        self._pre.clear()

    @property
    def is_open(self) -> bool:
        return self._open

    def threshold(self) -> float:
        return max(self.floor_rms, self.coupling * self._play_rms * self.ratio)

    def feed(self, block: bytes, mic_rms: float, speaking: bool) -> list:
        """Blocks to forward to the server for this mic block."""
        if not speaking:
            if self._open or self._above:
                self.reset()
            return [block]
        mode = self.effective_mode()
        if mode == "always":
            return [block]
        if mode == "off":
            return []
        if self._open:
            return [block]
        self._pre.append(block)
        # Phase 7: the real block length (16-bit mono @ 16 kHz → 32 bytes per ms), so
        # min_ms / preroll_ms mean milliseconds whatever block size the mic uses
        blk_ms = len(block) / 32.0 if len(block) >= 320 else self.block_ms
        need = max(1, int(round(self.min_ms / blk_ms)))
        loud = mic_rms >= self.threshold() and self._play_rms > 0
        if loud and self.echo_guard is not None:
            try:                               # loud is not enough: it must not be our own voice
                loud = not self.echo_guard.is_echo(block)
            except Exception:
                pass
        if loud:
            self._above += 1
        else:
            self._above = 0
            # learn the echo coupling only from blocks that are clearly not speech
            if self._play_rms > 50:
                c = min(3.0, mic_rms / self._play_rms)
                self.coupling = 0.95 * self.coupling + 0.05 * c
        if self._above >= need:
            self._open = True
            self.opened += 1
            keep = max(1, int(round(self.preroll_ms / blk_ms)))
            out = list(self._pre)[-keep:]
            self._pre.clear()
            return out
        return []


# ── identities & providers ──────────────────────────────────────────────────

PREBUILT_LIVE_VOICES = ("Charon", "Puck", "Kore", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr", "Callirrhoe",
                        "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib",
                        "Rasalgethi", "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima",
                        "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat")

VOICE_PROVIDERS = {
    "gemini_live": {"paid": False, "needs": "GOOGLE_API_KEY (already used for the voice session)",
                    "role": "the conversation itself — speech is generated by the Live model (lowest latency)"},
    "edge": {"paid": False, "needs": "internet (pip install edge-tts)", "role": "optional speech outside the session"},
    "kokoro": {"paid": False, "needs": "local model (~330 MB, pip install kokoro)",
               "role": "optional offline speech outside the session"},
    "elevenlabs": {"paid": True, "needs": "ELEVENLABS_API_KEY + a voice id — OPTIONAL, never required",
                   "role": "optional premium speech outside the session"},
}


@dataclass
class VoiceChoice:
    selected: str                 # what the settings say (Charon / JARVIS / FRIDAY / …)
    identity: Optional[str]       # JARVIS / FRIDAY / None
    live_voice: str               # what Gemini Live receives — always a real prebuilt voice
    note: str = ""


def resolve_voice(selected: str, identities: Optional[dict] = None, default: str = "Charon") -> VoiceChoice:
    sel = str(selected or "").strip()
    ids = identities or {}
    up = sel.upper()
    if up in ("JARVIS", "FRIDAY"):
        lv = str((ids.get(up) or {}).get("live_voice") or {"JARVIS": "Charon", "FRIDAY": "Kore"}[up])
        if lv not in PREBUILT_LIVE_VOICES:
            return VoiceChoice(sel, up, default, f"{up}'s live_voice '{lv}' is not a Gemini voice — using {default}")
        return VoiceChoice(sel, up, lv)
    if sel in PREBUILT_LIVE_VOICES:
        return VoiceChoice(sel, None, sel)
    return VoiceChoice(sel, None, default, f"unknown voice '{sel}' — using {default}" if sel else "")


def voice_settings() -> dict:
    """config/runtime.json → voice section (merged with defaults)."""
    d = {"barge_in": "auto", "barge_in_ratio": 3.0, "barge_in_min_ms": 300, "barge_in_floor_rms": 450,
         "end_of_turn_silence_ms": None, "start_sensitivity": None, "end_sensitivity": None,
         "stop_phrases": True, "tts_provider": "gemini_live",
         # Phase 7 (core/live_profile.py, core/live_tools.py): "auto" = what the
         # voice model supports; tool_profile "lean" | "full"
         "proactive_audio": "auto", "affective_dialog": "auto", "media_resolution": "default",
         "tool_profile": "full",
         # Phase 7 (core/mic_gate.py): always | wake_word | push_to_talk
         "mic_mode": "always", "wake_word": "hey_jarvis", "wake_threshold": 0.5, "wake_window_s": 20,
         "ptt_key": "F8"}
    try:
        from core import config as _c
        sec = _c.runtime("voice") or {}
        if isinstance(sec, dict):
            d.update({k: v for k, v in sec.items() if k in d})
    except Exception:
        pass
    return d


def realtime_input_config(types_mod, settings: Optional[dict] = None):
    """Build the Live realtime_input_config from settings, or None when
    nothing is configured / the installed SDK lacks the types."""
    s = settings or voice_settings()
    ms = s.get("end_of_turn_silence_ms")
    ss, es = s.get("start_sensitivity"), s.get("end_sensitivity")
    if ms in (None, "", 0) and not ss and not es:
        return None
    RIC = getattr(types_mod, "RealtimeInputConfig", None)
    AAD = getattr(types_mod, "AutomaticActivityDetection", None)
    if RIC is None or AAD is None:
        return None
    kw = {}
    try:
        if ms not in (None, "", 0):
            kw["silence_duration_ms"] = int(ms)
        if ss and getattr(types_mod, "StartSensitivity", None) is not None:
            kw["start_of_speech_sensitivity"] = getattr(types_mod.StartSensitivity,
                                                        f"START_SENSITIVITY_{str(ss).upper()}")
        if es and getattr(types_mod, "EndSensitivity", None) is not None:
            kw["end_of_speech_sensitivity"] = getattr(types_mod.EndSensitivity,
                                                      f"END_SENSITIVITY_{str(es).upper()}")
        return RIC(automatic_activity_detection=AAD(**kw))
    except Exception:
        return None
