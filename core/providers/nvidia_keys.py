"""
core/providers/nvidia_keys.py — NVIDIA NIM keys per model.

NVIDIA's build.nvidia.com hands out keys per model page, so a user can hold
several: `.env` may contain

    NVIDIA_API_KEY                      the general key
    NVIDIA_API_KEY_2                    a second general key (tried on 401/403)
    NVIDIA_API_KEY__<MODEL>             a key for one model, e.g.
                                        NVIDIA_API_KEY__KIMI_K3,
                                        NVIDIA_API_KEY__NEMOTRON_3_ULTRA_550B_A55B

<MODEL> is the last path segment of the model id, upper-cased, with every
non-alphanumeric run replaced by "_" — "moonshotai/kimi-k3" → KIMI_K3. The
request for a model uses its own key first, then the general keys.

Values are read through core.secret_store (DPAPI-decrypted on Windows) and
are never logged.
"""
from __future__ import annotations

import re


def slug(model: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(model or "").split("/")[-1].upper()).strip("_")


def secret_name(model: str) -> str:
    return "NVIDIA_API_KEY__" + slug(model)


def general_keys() -> list[str]:
    from core.secret_store import get_secret
    out = []
    for n in ("NVIDIA_API_KEY", "NVIDIA_API_KEY_2"):
        v = get_secret(n)
        if v and v not in out:
            out.append(v)
    return out


def key_for(model: str = "") -> str:
    """The best key for `model`: its own, else the general one ('' if none)."""
    from core.secret_store import get_secret
    if model:
        v = get_secret(secret_name(model))
        if v:
            return v
    g = general_keys()
    return g[0] if g else ""


def keys_for(model: str = "") -> list[str]:
    """Every key worth trying for `model`, best first (de-duplicated)."""
    from core.secret_store import get_secret
    out = []
    if model:
        v = get_secret(secret_name(model))
        if v:
            out.append(v)
    for g in general_keys():
        if g not in out:
            out.append(g)
    return out


def has_any() -> bool:
    return bool(general_keys()) or bool(per_model_names())


def per_model_names() -> list[str]:
    """Names (never values) of per-model keys present in .env."""
    try:
        from core.secret_store import _read_env_file
        return sorted(k for k, v in _read_env_file().items() if k.startswith("NVIDIA_API_KEY__") and v.strip())
    except Exception:
        return []


def configured_models() -> list[str]:
    """Every NVIDIA model JARVIS is configured to use (tiers, alternates, extras)."""
    try:
        from core import config as C
        prov = (C.load("models").get("providers") or {}).get("nvidia") or {}
    except Exception:
        prov = {}
    out: list[str] = []
    for t in (prov.get("tiers") or {}).values():
        for m in [t.get("model")] + list(t.get("alternates") or []):
            m = str(m or "")
            if m and not m.startswith("@") and m not in out:
                out.append(m)
    for m in prov.get("extra_models") or []:
        if m not in out:
            out.append(str(m))
    return out
