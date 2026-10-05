"""
core/config.py — one place for JARVIS's non-secret settings.

    config/models.json    which model serves each tier (fast, normal, deep,
                          vision, research, computer_use, image, live), the
                          fallback chain of every tier, per-provider tables
    config/runtime.json   task scheduler, tool timeouts, terminal, web-dev,
                          proactive behaviour

Both files are optional: the defaults below are complete, and a file only has
to contain what the user wants to change (it is deep-merged over the
defaults). Files are re-read when their modification time changes, so an edit
takes effect without restarting JARVIS.

SECRETS NEVER LIVE HERE. API keys are read only through core/secret_store.py
(.env, DPAPI-encrypted). load() refuses to return any key that looks like a
credential, so a key pasted into these files by mistake is ignored (and
reported by problems()) instead of being used or logged.

A tier that borrows its model ("@normal") also borrows that tier's
"alternates" (e.g. deep = the normal models with a high thinking budget).

Precedence for a tier's model (first hit wins):
    1. environment / .env        JARVIS_AI_MODEL_<TIER>   e.g. JARVIS_AI_MODEL_DEEP
    2. config/api_keys.json      ai_model_<tier>          (legacy, still honoured)
    3. config/models.json        providers.<provider>.tiers.<tier>.model
    4. the defaults below

A model value of "@normal" / "@fast" means "whatever the provider's normal /
fast model is", so switching provider (or the normal model) carries the
dependent tiers along.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

# ── Defaults (Google free tier, checked against ai.google.dev, Sept 2026) ────

MODEL_DEFAULTS: dict = {
    "provider": "google",
    "providers": {
        "google": {
            "tiers": {
                # Each model has its OWN free daily quota (gemini-3.8-flash: 20 requests/day
                # on the user's key, Sept 2026), so every lane lists other free models to
                # continue on once one runs out. All of these answered on the user's key.
                # Phase 7: fastest-first by measurement; gemini-3.7-flash dropped (it hung).
                "fast": {"model": "gemini-flash-lite-latest",
                         "alternates": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"], "fallback": []},
                "normal": {"model": "gemini-flash-latest",
                           "alternates": ["gemini-3-flash-preview", "gemini-3.5-flash", "gemini-3.6-flash"],
                           "fallback": ["fast"]},
                "deep": {"model": "@normal", "effort": "high", "max_output_tokens": 8192,
                         "fallback": ["normal", "fast"]},
                "vision": {"model": "@normal", "fallback": ["fast"]},
                "research": {"model": "@normal", "effort": "high", "max_output_tokens": 8192,
                             "grounding": True, "fallback": ["normal", "fast"]},
                "computer_use": {"model": "@normal", "fallback": ["fast"]},
                "image": {"model": "gemini-2.5-flash-image",
                          "alternates": ["gemini-3.1-flash-lite-image", "gemini-3.1-flash-image"],
                          "fallback": []},
                # Phase 7: measured 0.65 s speech → first audio vs 2.82 s; 2.5 stays as the fallback rung
                "live": {"model": "models/gemini-3.8-live",
                         "alternates": ["models/gemini-2.5-flash-native-audio-preview-12-2025"]},
            },
            "rpm": 9,
        },
        # Future providers: only used if the user selects them (JARVIS_AI_PROVIDER)
        # and supplies their own key. Nothing paid is ever chosen automatically.
        "anthropic": {"tiers": {"deep": {"model": "@normal", "effort": "high", "fallback": ["normal", "fast"]},
                                "normal": {"model": "", "fallback": ["fast"]}, "fast": {"model": ""}}},
        "openai": {"tiers": {"deep": {"model": "@normal", "effort": "high", "fallback": ["normal", "fast"]},
                             "normal": {"model": "", "fallback": ["fast"]}, "fast": {"model": ""}}},
        "local": {"tiers": {"normal": {"model": "", "fallback": ["fast"]}, "fast": {"model": ""}}},
        # NVIDIA NIM — FREE, OpenAI-compatible. Used only if you set a tier to it
        # (tier_providers) or JARVIS_AI_PROVIDER=nvidia, and add NVIDIA_API_KEY.
        # A good use is deep→nvidia so heavy reasoning survives Gemini's daily quota.
        "nvidia": {"tiers": {
            # nemotron-super-49b-v1.5 / llama-3.1-8b reached end of life on NVIDIA (Aug 2026, HTTP 410);
            # laguna-xs-2.1 answered on the user's key (1 Oct 2026)
            "fast": {"model": "poolside/laguna-xs-2.1", "fallback": []},
            "normal": {"model": "poolside/laguna-xs-2.1", "fallback": ["fast"]},
            "deep": {"model": "poolside/laguna-xs-2.1", "effort": "high",
                     "max_output_tokens": 8192, "fallback": ["normal", "fast"]},
            "vision": {"model": "meta/llama-3.2-90b-vision-instruct", "fallback": ["normal"]},
            "research": {"model": "@normal", "effort": "high", "fallback": ["normal", "fast"]},
            "computer_use": {"model": "@vision", "fallback": ["normal"]},
        }},
    },
    # Route a tier to another provider, e.g. {"deep": "nvidia"}. Empty = all on `provider`.
    "tier_providers": {},
    "health": {"recheck_hours": 24},
}

RUNTIME_DEFAULTS: dict = {
    "tasks": {
        "max_parallel": 3,               # tasks doing work at the same time (waiting ones don't count)
        "default_tool_timeout": 300,     # seconds, when a tool declares none
        "tool_timeouts": {},             # per-tool override, e.g. {"research.collect": 900}
        "gui_exclusive": True,           # one step at a time may drive the mouse/keyboard
        "gui_wait": 600,                 # longest a task waits for the mouse/keyboard (seconds)
    },
    "web_dev": {
        "port_first": 8765, "port_last": 8799,
        "desktop_viewport": [1366, 850], "mobile_viewport": [390, 844],
        "max_fix_rounds": 3,
        "open_in_browser": True,
    },
    "coding": {"max_fix_rounds": 3, "test_timeout": 180},
    "design": {"image_size_hint": "1024x1024", "vector_size": [1024, 1024]},
    "proactive": {
        "enabled": True,
        "max_per_hour": 2,
        "quiet_hours": ["23:00", "07:00"],
        "offer_unfinished_work": True,
    },
    # Part One
    "voice": {
        "barge_in": "auto",              # off | always | auto (headphones → full duplex, speakers → echo gate)
        "barge_in_ratio": 3.0,           # speech must beat the learned echo level by this factor
        "barge_in_min_ms": 300,          # … for this long
        "barge_in_floor_rms": 450,
        "end_of_turn_silence_ms": None,  # ONE end-of-turn threshold; None = Gemini's default
        "start_sensitivity": None,       # None | "low" | "high"
        "end_sensitivity": None,         # None | "low" | "high"
        "stop_phrases": True,            # "stop" / "wait" / "hold on" / "JARVIS, stop" cut speech
        "tts_provider": "gemini_live",   # optional out-of-session speech: edge | kokoro | elevenlabs
    },
    "knowledge": {
        "folders": [],                   # [] = <JARVIS>/knowledge + ~/Documents/JARVIS Notes
        "project_readmes": True,         # index README.md of registered projects
        "show_sources": "auto",          # auto (panel after answers that used sources) | on_request
    },
    "vision": {
        "max_frame_age_s": 2.5,          # a frame older than this is re-captured before it is used
    },
}

_DEFAULTS = {"models": MODEL_DEFAULTS, "runtime": RUNTIME_DEFAULTS}
TIER_NAMES = ("fast", "normal", "deep", "vision", "research", "computer_use", "image", "live")

_SECRETISH_KEY = re.compile(r"(api[_-]?key|secret|token|password|passwd|credential)", re.I)
_SECRETISH_VAL = re.compile(r"^(AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9_\-]{16,}|dpapi:)")

_cache: dict[str, tuple[float, dict, list]] = {}
_lock = threading.Lock()
_DIR_OVERRIDE: Path | None = None


def config_dir() -> Path:
    if _DIR_OVERRIDE is not None:
        return _DIR_OVERRIDE
    try:
        from core.secret_store import base_dir
        return base_dir() / "config"
    except Exception:  # pragma: no cover
        return Path(__file__).resolve().parent.parent / "config"


def set_config_dir(path: Path | None) -> None:
    """Test hook."""
    global _DIR_OVERRIDE
    _DIR_OVERRIDE = Path(path) if path else None
    with _lock:
        _cache.clear()


def _merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def _scrub(obj: Any, path: str, problems: list) -> Any:
    """Drop anything that looks like a credential (never used, never echoed)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if _SECRETISH_KEY.search(str(k)) and isinstance(v, str) and v:
                problems.append(f"{p}: looks like a secret — ignored (keys belong in .env)")
                continue
            out[k] = _scrub(v, p, problems)
        return out
    if isinstance(obj, list):
        return [_scrub(v, path, problems) for v in obj]
    if isinstance(obj, str) and _SECRETISH_VAL.match(obj.strip()):
        problems.append(f"{path}: looks like a secret — ignored (keys belong in .env)")
        return ""
    return obj


def load(name: str) -> dict:
    """Merged settings for config/<name>.json (defaults + file)."""
    base = _DEFAULTS.get(name, {})
    path = config_dir() / f"{name}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0
    with _lock:
        hit = _cache.get(name)
        if hit and hit[0] == mtime:
            return copy.deepcopy(hit[1])
    problems: list[str] = []
    data: dict = {}
    if mtime >= 0:
        try:
            raw = json.loads(path.read_bytes().decode("utf-8-sig") or "{}")
            if isinstance(raw, dict):
                data = _scrub({k: v for k, v in raw.items() if not str(k).startswith("_")}, "", problems)
            else:
                problems.append(f"{path.name}: top level must be an object — ignored")
        except Exception as e:
            problems.append(f"{path.name}: invalid JSON ({type(e).__name__}) — using defaults")
    merged = _merge(base, data)
    with _lock:
        _cache[name] = (mtime, merged, problems)
    return copy.deepcopy(merged)


def problems(name: str) -> list[str]:
    load(name)
    with _lock:
        hit = _cache.get(name)
    return list(hit[2]) if hit else []


def save(name: str, patch: dict) -> dict:
    """Deep-merge `patch` into config/<name>.json (only the file's own keys, not
    the defaults) and write it back as CRLF UTF-8 JSON."""
    problems_: list[str] = []
    patch = _scrub(patch, "", problems_)
    if problems_:
        raise ValueError("refusing to write secrets into config: " + "; ".join(problems_))
    path = config_dir() / f"{name}.json"
    current: dict = {}
    try:
        current = json.loads(path.read_bytes().decode("utf-8-sig") or "{}")
    except Exception:
        current = {}
    merged = _merge(current, patch)
    text = json.dumps(merged, indent=2, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8") + b"\r\n")
    os.replace(tmp, path)
    with _lock:
        _cache.pop(name, None)
    return load(name)


def runtime(section: str, key: str | None = None, default: Any = None) -> Any:
    sec = load("runtime").get(section, {})
    if key is None:
        return sec
    return sec.get(key, default) if isinstance(sec, dict) else default


# ── Models ───────────────────────────────────────────────────────────────────

def _legacy_json() -> dict:
    try:
        from core.secret_store import legacy_config_path
        return json.loads(legacy_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    try:
        from core.secret_store import _read_env_file
        return (_read_env_file().get(name) or "").strip()
    except Exception:
        return ""


def active_provider() -> str:
    """The provider JARVIS_AI_PROVIDER / api_keys.json / models.json selects."""
    v = _env("JARVIS_AI_PROVIDER") or str(_legacy_json().get("ai_provider") or "").strip() \
        or str(load("models").get("provider") or "google")
    return v.strip().lower() or "google"


def foreign_model(model: str, provider: str) -> bool:
    """True when a model id obviously belongs to another vendor — such a name
    is never sent to `provider` (a Claude id to NVIDIA answers 404)."""
    m = str(model or "").lower()
    if m.startswith(("gemini", "models/gemini", "gemma")) and provider != "google":
        return True
    if m.startswith("claude") and provider != "anthropic":
        return True
    if m.startswith(("gpt-4", "gpt-5", "o1-", "o3-", "o4-")) and provider in ("google", "anthropic", "nvidia"):
        return True
    return False


def tier_table(provider: str = "google", *, overrides: bool = True) -> dict[str, dict]:
    """{tier: {"model", "effort", "max_output_tokens", "fallback", "alternates", …}}
    for one provider, with env / legacy overrides applied.

    Part One fix: JARVIS_AI_MODEL* overrides belong to the ACTIVE provider only.
    Before, JARVIS_AI_MODEL=claude-… was also written into the NVIDIA table, so
    the NVIDIA fallback asked NVIDIA for a Claude model and got 404. (The live
    voice model override always applies to Google, the only live provider.)"""
    m = load("models")
    prov = (m.get("providers") or {}).get(provider) or {}
    tiers = copy.deepcopy(prov.get("tiers") or {})
    for t in TIER_NAMES:
        tiers.setdefault(t, {})
        tiers[t].setdefault("model", "@normal" if t not in ("fast", "normal", "image", "live") else "")
        tiers[t].setdefault("fallback", [])
    if overrides:
        legacy = _legacy_json()
        is_active = provider == active_provider()
        for t in TIER_NAMES:
            if not is_active and not (t == "live" and provider == "google"):
                continue
            env_name = "JARVIS_AI_MODEL" if t == "normal" else f"JARVIS_AI_MODEL_{t.upper()}"
            leg_name = "ai_model" if t == "normal" else f"ai_model_{t}"
            v = _env(env_name) or str(legacy.get(leg_name) or "").strip()
            if v and not _SECRETISH_VAL.match(v):
                if foreign_model(v, provider):
                    continue          # another vendor's model name is never sent here
                tiers[t]["model"] = v
                tiers[t]["source"] = "env" if _env(env_name) else "api_keys.json"
    return tiers


def model_for(tier: str, provider: str = "google") -> str:
    """Concrete model id for a tier ("@normal"/"@fast" resolved)."""
    tiers = tier_table(provider)
    seen = set()
    t = tier
    while True:
        v = str((tiers.get(t) or {}).get("model") or "")
        if v.startswith("@") and v[1:] not in seen:
            seen.add(t)
            t = v[1:]
            continue
        return v


def live_model() -> str:
    """The Gemini Live (native audio) model used by the voice session."""
    v = model_for("live", "google") or MODEL_DEFAULTS["providers"]["google"]["tiers"]["live"]["model"]
    return v if v.startswith("models/") else f"models/{v}"


def live_ladder() -> list[str]:
    """Phase 7: the voice model, then its alternates (used only when a model
    cannot be used at all — see core/live_profile.py)."""
    from core.live_profile import ladder
    try:
        t = (tier_table("google").get("live") or {})
        alts = list(t.get("alternates") or [])
    except Exception:
        alts = []
    return ladder(live_model(), alts)


def provider_rpm(provider: str) -> int | None:
    prov = (load("models").get("providers") or {}).get(provider) or {}
    try:
        return int(prov["rpm"]) if "rpm" in prov else None
    except Exception:
        return None
