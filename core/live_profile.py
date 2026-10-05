"""
core/live_profile.py — what each Gemini Live voice model is sent, and in what
order JARVIS tries them.

Live models differ in what they accept, and sending a feature a model does
not support either fails the connection or (worse) is silently ignored while
still costing set-up time:

    gemini-2.5-flash-native-audio-*   affective dialog, proactive audio
    gemini-3.8-live                   neither (Google's 3.8 Live notes); low-latency
                                      model — no thinking settings are sent
    gemini-3.1-flash-live-preview     neither

Order (config/models.json → providers.google.tiers.live):
    "model"       the voice model JARVIS connects to first
    "alternates"  the next rungs, used ONLY when a model cannot be used at all
                  (not found / not permitted / unsupported) — and JARVIS says so
                  in the log and out loud. Never a silent substitute.

A model picked at runtime ("switch the voice model to …") is exact: if it
fails JARVIS reports it and returns to the default, it never walks the ladder.

Dependency-free; tested by real execution.
"""
from __future__ import annotations

NATIVE_25 = "native25"
LIVE_38 = "live38"
LIVE_31 = "live31"
OTHER = "other"

MEDIA_RESOLUTIONS = ("low", "medium", "high")

# connection errors that mean "this model cannot be used on this key" (as
# opposed to a network blip, which is retried on the same model)
_UNUSABLE = ("NOT_FOUND", "not found", "not supported", "PERMISSION_DENIED", "is not available",
             "does not exist", "1008")


_FEATURE_WORDS = ("affective", "proactiv", "realtime_input", "activity_detection", "media_resolution",
                  "sensitivity", "silence_duration", "resum", "handle")


def family(model: str) -> str:
    m = str(model or "").lower()
    if "native-audio" in m:
        return NATIVE_25
    if "3.8-live" in m or "3-8-live" in m:
        return LIVE_38
    if "3.1-flash-live" in m or "live-preview" in m:
        return LIVE_31
    return OTHER


def _flag(value, supported: bool, default: bool) -> bool:
    """'auto' / None → default (if supported); True/False → as asked, but never
    a feature the model does not support."""
    if not supported:
        return False
    if value in (None, "", "auto"):
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def features(model: str, settings: dict | None = None) -> dict:
    """{'affective': bool, 'proactive': bool, 'media_resolution': str|None,
    'api_version': 'v1alpha'|'v1beta'} for `model` under the voice settings."""
    s = settings or {}
    fam = family(model)
    native = fam == NATIVE_25
    affective = _flag(s.get("affective_dialog"), native, True)
    proactive = _flag(s.get("proactive_audio"), native, True)
    # "default" (the shipped setting) sends nothing — the server's own default. low | medium | high
    # are sent explicitly; a server that rejects the field gets it dropped on reconnect (main.py).
    mr = str(s.get("media_resolution") or "default").strip().lower()
    return {
        "family": fam,
        "affective": affective,
        "proactive": proactive,
        "media_resolution": mr if mr in MEDIA_RESOLUTIONS else None,
        # the enhanced audio fields live on v1alpha; nothing else needs it
        "api_version": "v1alpha" if (affective or proactive) else "v1beta",
    }


def ladder(primary: str, alternates: list | tuple = ()) -> list[str]:
    """Primary first, then alternates; 'models/' prefix normalised, no repeats."""
    out: list[str] = []
    for m in [primary, *list(alternates or ())]:
        m = str(m or "").strip()
        if not m:
            continue
        m = m if m.startswith("models/") else f"models/{m}"
        if m not in out:
            out.append(m)
    return out


def unusable(err_text: str) -> bool:
    """True when a connection error says the MODEL cannot be used (move to the
    next rung) rather than a transient failure (retry the same model)."""
    t = str(err_text or "")
    if any(k in t.lower() for k in _FEATURE_WORDS):
        return False           # a rejected FEATURE is dropped by its own handler, the model stays
    return any(k in t for k in _UNUSABLE)
