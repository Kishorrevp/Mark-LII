import json
import sys
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    """Phase 4A: secrets are stored by core/secret_store in .env, never in
    api_keys.json (which holds ordinary settings and gets rewritten often)."""
    from core.secret_store import save_secret
    save_secret("GOOGLE_API_KEY", gemini_api_key)

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {type(e).__name__}")
        return {}

def get_gemini_key() -> str | None:
    from core.secret_store import get_secret
    return get_secret("GOOGLE_API_KEY") or None

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


def get_assistant_name() -> str:
    """Return the configured assistant name, or 'JARVIS' if not set."""
    return load_api_keys().get("assistant_name", "JARVIS") or "JARVIS"


def get_user_name() -> str:
    """Return the configured user name for addressing."""
    return load_api_keys().get("user_name", "")


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    """Persist assistant name and user name to config."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["assistant_name"] = assistant_name.strip() or "JARVIS"
    data["user_name"] = user_name.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Assistant voice ──────────────────────────────────────────────────────────
# Gemini Live prebuilt voices. Names are proper nouns — identical in every
# language, so this list is safe to show verbatim in any locale.
AVAILABLE_VOICES = [
    "Charon",
    "Puck",
    "Kore",
    "Fenrir",
    "Aoede",
    "JARVIS",
    "FRIDAY",
]

DEFAULT_VOICE = "Charon"

ELEVENLABS_VOICES = {
    "JARVIS": "ELEVENLABS_JARVIS_VOICE_ID",
    "FRIDAY": "ELEVENLABS_FRIDAY_VOICE_ID",
}


def get_voice() -> str:
    """Return the configured voice SELECTION (a Gemini voice or an identity:
    JARVIS / FRIDAY), falling back to the default if unset or unrecognised.
    Never pass this straight to Gemini — use get_live_voice()."""
    v = load_api_keys().get("voice_name", DEFAULT_VOICE) or DEFAULT_VOICE
    return v if v in AVAILABLE_VOICES else DEFAULT_VOICE


def get_live_voice() -> str:
    """The Gemini prebuilt voice the Live session must receive. JARVIS and
    FRIDAY are identities, not Gemini voices: they map to a real prebuilt
    voice (config/persona.json → identities.*.live_voice; Charon / Kore by
    default), so picking them can never send an invalid name and break the
    voice connection. ElevenLabs voice ids (ELEVENLABS_VOICES) are optional
    and only used by the optional out-of-session TTS provider."""
    try:
        from core.voice import resolve_voice
        from core.persona import _cfg
        return resolve_voice(get_voice(), (_cfg().get("identities") or {}), DEFAULT_VOICE).live_voice
    except Exception:
        v = get_voice()
        return {"JARVIS": "Charon", "FRIDAY": "Kore"}.get(v, v if v not in ("JARVIS", "FRIDAY") else DEFAULT_VOICE)


def save_voice(voice_name: str) -> None:
    """Persist the chosen Live voice. Unknown names collapse to the default so a
    bad value can never reach the API and break the session."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    v = (voice_name or "").strip()
    data["voice_name"] = v if v in AVAILABLE_VOICES else DEFAULT_VOICE
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)


def save_brief_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["morning_brief_enabled"] = enabled
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Audio devices ────────────────────────────────────────────────────────────
# Stored as device NAMES, not sounddevice indices. Indices shift every time a
# USB device is plugged in or removed, so a saved index silently starts pointing
# at a different microphone. The empty string means "system default", which is
# both the factory setting and what an unresolvable saved device falls back to —
# so unplugging a headset degrades to the built-in speakers instead of crashing.

def _patch_config(**fields) -> None:
    """Read-modify-write one or more keys in api_keys.json.

    Every setter in this file open-coded this. Collapsing it here means a new
    setting is one line, and there is one place where a corrupt config file is
    handled instead of nine."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(fields)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_input_device() -> str:
    """Microphone device name, or '' for the system default."""
    return (load_api_keys().get("input_device", "") or "").strip()


def save_input_device(name: str) -> None:
    _patch_config(input_device=(name or "").strip())


def get_output_device() -> str:
    """Speaker device name, or '' for the system default."""
    return (load_api_keys().get("output_device", "") or "").strip()


def save_output_device(name: str) -> None:
    _patch_config(output_device=(name or "").strip())


def get_plugin_enabled(plugin_name: str) -> bool:
    """Plugins are enabled by default the moment they're discovered (opt-out model)."""
    return load_api_keys().get("plugins_enabled", {}).get(plugin_name, True)


def save_plugin_enabled(plugin_name: str, enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    plugins_cfg = data.get("plugins_enabled")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    plugins_cfg[plugin_name] = enabled
    data["plugins_enabled"] = plugins_cfg
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Personality mode ─────────────────────────────────────────────────────────
# 'serious' is the default on purpose: it is the tone JARVIS already had before
# Friendly Mode existed, so an existing config with no personality_mode key at
# all keeps behaving exactly as it did before this setting was added.
AVAILABLE_MODES = ["serious", "friendly"]
DEFAULT_MODE    = "serious"


def get_personality_mode() -> str:
    """Return the configured personality mode, falling back to the default if
    unset or if the stored value is not one we recognise."""
    m = load_api_keys().get("personality_mode", DEFAULT_MODE) or DEFAULT_MODE
    return m if m in AVAILABLE_MODES else DEFAULT_MODE


def save_personality_mode(mode: str) -> None:
    """Persist the chosen personality mode. Unknown values collapse to the
    default so a bad value can never reach the prompt builder."""
    m = (mode or "").strip().lower()
    _patch_config(personality_mode=m if m in AVAILABLE_MODES else DEFAULT_MODE)


# ── AI provider (core/ai_provider.py) ──────────────────────────────────────
# 'google' is the only one that actually works today (see core/ai_provider.py
# for why) — but the setting exists now so switching models, or providers
# once more of them exist, is a config change, not a code change.
# Phase 4A: all four are real implementations now (core/providers/*). Only
# google (free tier) and local (free, Ollama) respect the project's no-paid-
# keys rule; anthropic/openai work only if the user supplies a paid key.
# Env vars JARVIS_AI_PROVIDER / JARVIS_AI_MODEL override these (ai_gateway).
AVAILABLE_AI_PROVIDERS = ["google", "anthropic", "openai", "local"]
DEFAULT_AI_PROVIDER    = "google"
DEFAULT_AI_MODEL       = "gemini-flash-latest"


def get_ai_provider() -> str:
    p = load_api_keys().get("ai_provider", DEFAULT_AI_PROVIDER) or DEFAULT_AI_PROVIDER
    return p if p in AVAILABLE_AI_PROVIDERS else DEFAULT_AI_PROVIDER


def get_ai_model() -> str:
    m = load_api_keys().get("ai_model", DEFAULT_AI_MODEL) or DEFAULT_AI_MODEL
    return str(m).strip() or DEFAULT_AI_MODEL


def save_ai_provider(provider: str) -> None:
    p = (provider or "").strip().lower()
    _patch_config(ai_provider=p if p in AVAILABLE_AI_PROVIDERS else DEFAULT_AI_PROVIDER)


def save_ai_model(model: str) -> None:
    m = (model or "").strip()
    _patch_config(ai_model=m or DEFAULT_AI_MODEL)