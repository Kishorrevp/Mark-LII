"""
core/secret_store.py — one place that knows where API keys live.

WHY THIS EXISTS
    Before Phase 4A, eleven files each opened config/api_keys.json and read
    ["gemini_api_key"] themselves. That meant the key sat in the same JSON file
    as UI colour and microphone name (a file the settings code rewrites all the
    time and that is easy to zip up and send to someone), and every file had its
    own copy of "where is the key?" logic.

RESOLUTION ORDER (first hit wins)
    1. Real process environment variables     (GOOGLE_API_KEY, ...)
    2. A `.env` file in the project root       (KEY=value lines, gitignored)
    3. LEGACY: config/api_keys.json            (only for the Google key, only
                                                until migrate_legacy_secrets()
                                                has moved it into .env)

    Environment first is the standard convention (CI, containers, and a user
    who sets a key in Windows "Environment Variables" all just work). `.env`
    second because the setup overlay has to be able to *save* a key somewhere
    without asking the user to edit system settings.

WHAT THIS MODULE GUARANTEES
    - Secret values are never printed. `describe()` reports presence + source
      only; `redact()` scrubs any known key value (and anything shaped like an
      API key) out of arbitrary text before it reaches a log, an error message
      or an audit record.
    - `.env` is written atomically (temp file + replace) so a crash mid-save
      cannot leave a half-written secrets file.

SECRETS VAULT (Phase 5, Windows)
    Values in `.env` are encrypted with Windows DPAPI (CryptProtectData, via
    ctypes — no extra package) and stored as `NAME=dpapi:<base64>`. The
    encryption key is derived by Windows from YOUR user login — it is not
    stored next to the file — so a copied `.env` (a backup, a zip sent to
    someone, another user on the PC) is useless without your Windows account.
    Every write is verified by decrypting it again; if that fails the value is
    kept in plain text rather than lost. Honest limit: anything already
    running as your Windows user can still ask DPAPI to decrypt it — DPAPI
    protects the file at rest, not against malware on your account.
    Opt out with config "encrypt_secrets": false. Non-Windows: plain `.env`.

No third-party imports — this is safe to import from anywhere, including
tests, without Qt/audio/genai installed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

# ── Known secret names ───────────────────────────────────────────────────────
# Canonical name → accepted aliases (checked in order). GEMINI_API_KEY is an
# alias because it is what Google's own docs use in some places.
KNOWN_SECRETS: dict[str, tuple[str, ...]] = {
    "GOOGLE_API_KEY":    ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "NVIDIA_API_KEY": ("NVIDIA_API_KEY",),
    "ANTHROPIC_API_KEY": ("ANTHROPIC_API_KEY",),
    "OPENAI_API_KEY":    ("OPENAI_API_KEY",),
    # Optional: some local servers (LM Studio, vLLM) want a bearer token.
    "LOCAL_AI_API_KEY":  ("LOCAL_AI_API_KEY",),
    # Phase 7
    "NVIDIA_API_KEY_2":  ("NVIDIA_API_KEY_2",),
    "ELEVENLABS_API_KEY": ("ELEVENLABS_API_KEY",),
    "TELEGRAM_BOT_TOKEN": ("TELEGRAM_BOT_TOKEN",),
    "GOOGLE_OAUTH_CLIENT_SECRET": ("GOOGLE_OAUTH_CLIENT_SECRET",),
}

# Names that are secrets even though they are not listed above — per-model
# NVIDIA keys (NVIDIA_API_KEY__KIMI_K3 …) and anything named *_API_KEY,
# *_TOKEN or *_SECRET. They are encrypted, redacted and never logged like the
# rest.
_SECRET_NAME = re.compile(r"^(NVIDIA_API_KEY(?:_\d+|__[A-Z0-9_]+)?|[A-Z0-9_]+_(?:API_KEY|TOKEN|SECRET|PASSWORD))$")


def is_secret_name(name: str) -> bool:
    n = str(name or "")
    return any(n in aliases for aliases in KNOWN_SECRETS.values()) or bool(_SECRET_NAME.match(n))

# The only key that ever lived in api_keys.json.
_LEGACY_JSON_FIELD = {"GOOGLE_API_KEY": "gemini_api_key"}

ENC_PREFIX = "dpapi:"
_ENTROPY = b"JARVIS-secrets-vault-v1"


# ── DPAPI (Windows) — injectable for tests ───────────────────────────────────
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes) -> BLOB:
        buf = ctypes.create_string_buffer(b, len(b))
        return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, ent, out = blob(data), blob(_ENTROPY), BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = (fn(ctypes.byref(src), "JARVIS" if protect else None, ctypes.byref(ent), None, None, 0x01,
             ctypes.byref(out)) if protect else
          fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x01, ctypes.byref(out)))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


_VAULT = {"protect": None, "unprotect": None}      # tests inject reversible fakes


def _protect(b: bytes) -> bytes:
    return (_VAULT["protect"] or (lambda x: _dpapi(x, True)))(b)


def _unprotect(b: bytes) -> bytes:
    return (_VAULT["unprotect"] or (lambda x: _dpapi(x, False)))(b)


def set_vault_backend(protect=None, unprotect=None) -> None:
    _VAULT.update(protect=protect, unprotect=unprotect)


def vault_available() -> bool:
    if _VAULT["protect"] is not None:
        return True
    if os.name != "nt":
        return False
    try:
        cfg = _read_legacy_json()
        if cfg.get("encrypt_secrets") is False:
            return False
    except Exception:
        pass
    return True


def _encrypt_value(v: str) -> str:
    import base64
    return ENC_PREFIX + base64.b64encode(_protect(v.encode("utf-8"))).decode("ascii")


def _decode_value(v: str) -> str:
    """Plain values pass through; dpapi: values are decrypted ('' if that fails)."""
    if not v.startswith(ENC_PREFIX):
        return v
    import base64
    try:
        return _unprotect(base64.b64decode(v[len(ENC_PREFIX):])).decode("utf-8")
    except Exception:
        return ""
_lock = threading.RLock()


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# Overridable for tests (set_base_dir) — never needed in the app.
_BASE_OVERRIDE: Path | None = None


def set_base_dir(path: Path | None) -> None:
    """Test hook: point the store at a temporary project root."""
    global _BASE_OVERRIDE
    _BASE_OVERRIDE = Path(path) if path else None


def base_dir() -> Path:
    return _BASE_OVERRIDE or _base_dir()


def env_path() -> Path:
    return base_dir() / ".env"


def legacy_config_path() -> Path:
    return base_dir() / "config" / "api_keys.json"


# ── .env parsing ─────────────────────────────────────────────────────────────
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


def _parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        else:
            # strip trailing inline comment on unquoted values
            val = val.split(" #", 1)[0].strip()
        out[key] = val
    return out


def _read_env_file() -> dict[str, str]:
    p = env_path()
    try:
        return _parse_env_text(p.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _read_legacy_json() -> dict:
    try:
        return json.loads(legacy_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── Public API ───────────────────────────────────────────────────────────────

def get_secret(name: str) -> str:
    """Return the secret value, or '' when it is not configured anywhere.
    Never raises, never logs the value."""
    aliases = KNOWN_SECRETS.get(name, (name,))
    for a in aliases:
        v = os.environ.get(a, "").strip()
        if v:
            return v
    env = _read_env_file()
    for a in aliases:
        v = _decode_value((env.get(a) or "").strip()).strip()
        if v:
            return v
    legacy_field = _LEGACY_JSON_FIELD.get(name)
    if legacy_field:
        v = str(_read_legacy_json().get(legacy_field) or "").strip()
        if v:
            return v
    return ""


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


def secret_source(name: str) -> str:
    """'env' | 'dotenv' | 'legacy_json' | '' — where the value came from.
    Used by describe() and diagnostics; never exposes the value."""
    aliases = KNOWN_SECRETS.get(name, (name,))
    if any(os.environ.get(a, "").strip() for a in aliases):
        return "env"
    env = _read_env_file()
    if any((env.get(a) or "").strip().startswith(ENC_PREFIX) for a in aliases):
        return "vault" if any(_decode_value((env.get(a) or "").strip()) for a in aliases) else "vault_unreadable"
    if any((env.get(a) or "").strip() for a in aliases):
        return "dotenv"
    lf = _LEGACY_JSON_FIELD.get(name)
    if lf and str(_read_legacy_json().get(lf) or "").strip():
        return "legacy_json"
    return ""


def describe() -> dict[str, str]:
    """Safe-to-log summary: {name: 'set (env)' | 'set (.env)' | 'missing'}."""
    out = {}
    for name in KNOWN_SECRETS:
        src = secret_source(name)
        out[name] = {"env": "set (environment)", "dotenv": "set (.env)",
                     "vault": "set (.env, encrypted with Windows DPAPI)",
                     "vault_unreadable": "present but cannot be decrypted by this Windows account",
                     "legacy_json": "set (legacy api_keys.json)"}.get(src, "missing")
    return out


def _write_env_file(values: dict[str, str]) -> None:
    """Rewrite .env preserving comments/unknown lines, updating given keys.
    Atomic: temp file in the same directory + os.replace."""
    p = env_path()
    try:
        existing = p.read_bytes().decode("utf-8").replace("\r\n", "\n").split("\n")
    except FileNotFoundError:
        existing = ["# JARVIS local secrets — never commit this file (.gitignore excludes it)."]
    remaining = dict(values)
    lines: list[str] = []
    for raw in existing:
        m = _ENV_LINE.match(raw.strip()) if raw.strip() and not raw.strip().startswith("#") else None
        if m and m.group(1) in remaining:
            val = remaining.pop(m.group(1))
            if val:                      # empty value == delete the line
                lines.append(f"{m.group(1)}={_quote(val)}")
            continue
        lines.append(raw)
    for k, v in remaining.items():
        if v:
            lines.append(f"{k}={_quote(v)}")
    while lines and lines[-1] == "":
        lines.pop()
    data = ("\r\n".join(lines) + "\r\n").encode("utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    try:
        os.chmod(p, 0o600)   # no-op-ish on Windows, meaningful on macOS/Linux
    except Exception:
        pass


def _quote(v: str) -> str:
    if re.search(r"\s|#|['\"]", v):
        return '"' + v.replace('"', '') + '"'
    return v


def save_secret(name: str, value: str) -> None:
    """Persist a secret to .env (never to api_keys.json). Empty value removes it."""
    canonical = name if name in KNOWN_SECRETS else name
    value = (value or "").strip()
    with _lock:
        stored = value
        if value and vault_available():
            try:
                enc = _encrypt_value(value)
                if _decode_value(enc) == value:          # round-trip before trusting it
                    stored = enc
            except Exception:
                stored = value                          # never lose the key over encryption
        _write_env_file({canonical: stored})
        _SECRET_CACHE.clear()


def encrypt_env_secrets() -> str:
    """Encrypt plain-text known secrets already in .env (idempotent, value-free status).
    Order: encrypt → decrypt-verify → write → re-read-verify → else restore plain."""
    if not vault_available():
        return "vault not available (not Windows, or disabled in settings)"
    with _lock:
        env = _read_env_file()
        todo = {}
        names = [a for aliases in KNOWN_SECRETS.values() for a in aliases]
        names += [k for k in env if is_secret_name(k) and k not in names]
        for a in names:
            v = (env.get(a) or "").strip()
            if v and not v.startswith(ENC_PREFIX):
                try:
                    enc = _encrypt_value(v)
                except Exception as e:
                    return f"vault: encryption failed ({type(e).__name__}); keys left as they were"
                if _decode_value(enc) != v:
                    return "vault: round-trip check failed; keys left as they were"
                todo[a] = (enc, v)
        if not todo:
            return "vault: nothing to encrypt"
        _write_env_file({k: enc for k, (enc, _) in todo.items()})
        back = _read_env_file()
        bad = [k for k, (_, plain) in todo.items() if _decode_value(back.get(k, "")) != plain]
        if bad:
            _write_env_file({k: todo[k][1] for k in bad})
            return f"vault: read-back failed for {len(bad)} key(s); restored them in plain text"
        _SECRET_CACHE.clear()
        return f"vault: encrypted {len(todo)} key(s) in .env with Windows DPAPI"


def migrate_legacy_secrets() -> str:
    """Move gemini_api_key out of config/api_keys.json into .env.

    Order is write → verify read-back → only then remove from the JSON, so a
    failure at any point leaves the key in at least one place. Returns a short,
    value-free status string for the log. Idempotent."""
    with _lock:
        data = _read_legacy_json()
        legacy = str(data.get("gemini_api_key") or "").strip()
        if not legacy:
            return "no legacy secrets"
        env = _read_env_file()
        if not (env.get("GOOGLE_API_KEY") or env.get("GEMINI_API_KEY")):
            try:
                _write_env_file({"GOOGLE_API_KEY": legacy})
            except Exception as e:
                return f"legacy key left in api_keys.json (could not write .env: {type(e).__name__})"
            if _read_env_file().get("GOOGLE_API_KEY", "") != legacy:
                return "legacy key left in api_keys.json (.env read-back mismatch)"
        # .env now holds the key (either just written or already there)
        data.pop("gemini_api_key", None)
        try:
            p = legacy_config_path()
            fd, tmp = tempfile.mkstemp(prefix="api_keys.", suffix=".tmp", dir=str(p.parent))
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(data, indent=4).encode("utf-8"))
            os.replace(tmp, p)
        except Exception as e:
            return f"key copied to .env but api_keys.json not rewritten ({type(e).__name__})"
        _SECRET_CACHE.clear()
        return "migrated Google API key from config/api_keys.json to .env"


# ── Redaction ────────────────────────────────────────────────────────────────
_SECRET_CACHE: dict[str, str] = {}

# Generic shapes: Google keys (AIza...), Anthropic (sk-ant-...), OpenAI (sk-...),
# bearer tokens, and key=value pairs whose key names look secret.
_GENERIC_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z\-_]{20,}"),
    re.compile(r"\bAQ\.[0-9A-Za-z\-_]{20,}"),              # newer Google API-key format
    re.compile(r"dpapi:[A-Za-z0-9+/=]{16,}"),
    re.compile(r"sk-ant-[0-9A-Za-z\-_]{10,}"),
    re.compile(r"nvapi-[0-9A-Za-z\-_]{20,}"),                # NVIDIA NIM keys
    re.compile(r"(?<![0-9])\d{6,12}:[0-9A-Za-z\-_]{30,}"),   # Telegram bot tokens (Phase 7)
    re.compile(r"sk-[0-9A-Za-z\-_]{16,}"),
    re.compile(r"(?i)bearer\s+[0-9A-Za-z\-_.=]{12,}"),
    re.compile(r"(?i)((?:api[_-]?key|x-api-key|x-goog-api-key|password|passwd|secret|token)\s*[=:]\s*)"
               r"([\"']?)[^\s\"',;&]{6,}\2"),
]


def _known_values() -> list[str]:
    vals = []
    names = list(KNOWN_SECRETS)
    try:
        names += [k for k in _read_env_file() if is_secret_name(k) and k not in names]
    except Exception:
        pass
    for name in names:
        v = get_secret(name)
        if v and len(v) >= 8 and v not in vals:
            vals.append(v)
    return vals


def redact(text) -> str:
    """Scrub secrets out of any text. Safe on non-strings (they are str()'d)."""
    s = str(text)
    for v in _known_values():
        s = s.replace(v, "[REDACTED]")
    for pat in _GENERIC_PATTERNS:
        if pat.groups >= 2:
            s = pat.sub(lambda m: m.group(1) + "[REDACTED]", s)
        else:
            s = pat.sub("[REDACTED]", s)
    return s


def looks_like_secret(text: str) -> bool:
    """True when a string contains something shaped like a credential. Used by
    the memory layer to refuse storing secrets."""
    s = str(text or "")
    if any(v in s for v in _known_values()):
        return True
    return redact(s) != s
