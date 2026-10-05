"""
core/ai_gateway.py — the single door between JARVIS and any AI model.

    JARVIS code ──► AIGateway ──► AIProvider interface ──┬─ GoogleProvider     (default, free tier)
                                                          ├─ AnthropicProvider  (paid, opt-in)
                                                          ├─ OpenAIProvider     (paid, opt-in)
                                                          └─ LocalProvider      (free, Ollama)
                                                             (or OpenAIProvider → local server)

Nothing outside core/providers/ imports a vendor SDK for *text/vision*
generation any more. Callers say what they need (a prompt, an image, a JSON
schema, tools, a speed tier) and the gateway picks the configured provider,
checks capabilities up front, retries transient failures, and — only if the
user configured it — tries fallback providers.

CONFIGURATION (first hit wins)
    environment / .env          JARVIS_AI_PROVIDER, JARVIS_AI_MODEL,
                                JARVIS_AI_MODEL_FAST, JARVIS_AI_MODEL_DEEP,
                                JARVIS_AI_MODEL_VISION, JARVIS_AI_EFFORT,
                                JARVIS_AI_FALLBACKS (comma list), JARVIS_AI_RPM,
                                JARVIS_AI_RATE_WAIT,
                                JARVIS_LOCAL_BASE_URL, JARVIS_LOCAL_API,
                                OPENAI_BASE_URL
    config/api_keys.json        ai_provider, ai_model, ai_model_fast, ai_model_deep,
                                ai_model_vision, ai_effort, ai_rpm, ai_rate_wait,
                                ai_fallbacks, local_base_url, local_api
    defaults                    google / gemini-flash-latest /
                                gemini-flash-lite-latest

MODEL TIERS (Phase 6 — config/models.json via core/config.py; see core/model_router.py)
    fast · normal · deep · vision · research · computer_use   → generate(tier=…)
    image                                                     → generate_image(…)
    live                                                      → main.py voice session
    Every tier has a FALLBACK CHAIN (deep → normal → fast by default). A model
    that is rate-limited, overloaded, missing or out of free quota is skipped
    for the rest of the chain; a request that is too large for the model is
    SIMPLIFIED once (history trimmed, long text middle-cut) before giving up.
    core/model_health.py remembers models that are missing / have no free
    quota on this key, so later requests don't spend a call rediscovering it.

MODEL TIERS (Phase 5 — see core/model_router.py)
    generate(..., tier=)   "fast"   → fast model (flash-lite)
                           "normal" / "default" → default model (flash)
                           "deep"   → deep model if configured, else default
                                      model + HIGH thinking budget
                           "vision" → vision model if configured, else default
                           "auto"   → the router decides; pure arithmetic is
                                      answered exactly with no model call
    FREE-TIER THROTTLE     a client-side sliding window keeps each model under
                           its requests-per-minute quota (google default 9 RPM,
                           i.e. just under the free Flash limit) instead of
                           hammering the API into 429s. On a quota error the
                           gateway honours the server's retry delay, and a
                           normal/deep request may degrade once to the fast
                           model (separate free quota) — also when Flash is
                           overloaded (5xx "high demand") — flagged in the
                           response's meta so callers can say so.

    Example — switch the whole assistant's text brain to a local model:
        JARVIS_AI_PROVIDER=local
        JARVIS_AI_MODEL=qwen3:8b

    API keys are read only through core/secret_store.py.

COST POLICY (project rule: no paid API keys)
    The default is Google's free tier. Paid providers (anthropic, openai with the
    default base URL) are never auto-selected and never used as an implicit
    fallback — only if the user names them in ai_provider / ai_fallbacks.

WHY THE LIVE VOICE SESSION IS NOT BEHIND THIS INTERFACE
    main.py's realtime session uses Gemini Live: bidirectional native audio,
    server-side VAD, session resumption, affective dialog. None of the other
    providers offers an equivalent free realtime-audio API, so an abstraction
    over it would have exactly one implementation and would have to fake the
    rest. It stays a Gemini-specific transport (main.py / screen_processor.py)
    and is documented as such; it now reads its key through secret_store.
    Everything *behind* the voice — planning, summarising, vision analysis,
    classification, extraction — goes through this gateway.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Optional

from core import model_router, secret_store
from core.providers.base import (
    AIProvider, AIRequest, AIResponse, Capability, CapabilityNotSupported, Message, Part,
    ProviderConfigError, ProviderError, RateLimited, ReasoningEffort, ToolSpec,
)
from core.model_health import ModelHealth, classify as _health_class

KNOWN_PROVIDERS = ("google", "anthropic", "openai", "local", "nvidia")
PAID_PROVIDERS = {"anthropic", "openai"}
# Free providers that may be auto-selected / used for fallback and per-tier routing.
FREE_PROVIDERS = {"google", "local", "nvidia"}

GOOGLE_DEFAULT_MODEL = "gemini-flash-latest"
GOOGLE_FAST_MODEL = "gemini-flash-lite-latest"
# Free-tier requests-per-minute kept per model. Google's free Flash tier is
# ~10 RPM (Flash-Lite ~15); 9 leaves headroom for clock skew. 0 = no limit.
DEFAULT_RPM = {"google": 9}


@dataclass
class AIConfig:
    provider: str = "google"
    model: str = ""
    fast_model: str = ""
    effort: Optional[ReasoningEffort] = None
    fallbacks: list[str] = field(default_factory=list)
    local_base_url: str = ""
    local_api: str = "ollama"            # "ollama" | "openai"
    openai_base_url: str = ""
    max_retries: int = 2
    deep_model: str = ""
    vision_model: str = ""
    rpm: Optional[int] = None            # None → provider default (google 9, others unlimited)
    rate_wait_max: float = 20.0          # longest we will wait for a free-tier slot / retry delay
    tiers: dict = field(default_factory=dict)   # config/models.json tier table ({} → built-in defaults)
    tier_providers: dict = field(default_factory=dict)   # {tier: provider} — route a tier elsewhere (e.g. deep→nvidia)

    def provider_for(self, tier: str) -> str:
        """Which provider serves a tier: its per-tier override, else the default."""
        return str((self.tier_providers or {}).get(tier) or self.provider)

    def rpm_for(self, provider: str) -> int:
        if self.rpm is not None:
            return max(0, int(self.rpm))
        return DEFAULT_RPM.get(provider, 0)

    def signature(self) -> str:
        d = dict(self.__dict__)
        d["effort"] = self.effort.value if self.effort else None
        return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()


def _read_json_config() -> dict:
    try:
        return json.loads(secret_store.legacy_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _env(name: str) -> str:
    """Environment first, then .env (non-secret settings may live there too)."""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    return (secret_store._read_env_file().get(name) or "").strip()


def default_tiers(provider: str) -> dict:
    from core.config import MODEL_DEFAULTS
    import copy
    return copy.deepcopy(((MODEL_DEFAULTS.get("providers") or {}).get(provider) or {}).get("tiers") or {})


def load_config() -> AIConfig:
    j = _read_json_config()
    try:
        from core import config as _cfgmod
        cfg_provider = str(_cfgmod.load("models").get("provider") or "google")
    except Exception:
        _cfgmod, cfg_provider = None, "google"
    provider = (_env("JARVIS_AI_PROVIDER") or str(j.get("ai_provider") or cfg_provider)).strip().lower()
    if provider not in KNOWN_PROVIDERS:
        provider = provider  # keep unknown so build_provider can raise a clear config error
    model = _env("JARVIS_AI_MODEL") or str(j.get("ai_model") or "").strip()
    fast = _env("JARVIS_AI_MODEL_FAST") or str(j.get("ai_model_fast") or "").strip()
    deep = _env("JARVIS_AI_MODEL_DEEP") or str(j.get("ai_model_deep") or "").strip()
    vision = _env("JARVIS_AI_MODEL_VISION") or str(j.get("ai_model_vision") or "").strip()
    # config_manager's historical default ("gemini-flash-latest") is a Google
    # model name; never send it to another provider.
    if provider != "google":
        model, fast, deep, vision = ("" if m.startswith("gemini") else m
                                     for m in (model, fast, deep, vision))
    rpm_raw = _env("JARVIS_AI_RPM") or j.get("ai_rpm")
    try:
        rpm = None if rpm_raw in (None, "") else max(0, int(rpm_raw))
    except Exception:
        rpm = None
    try:
        rate_wait = float(_env("JARVIS_AI_RATE_WAIT") or j.get("ai_rate_wait") or 20.0)
    except Exception:
        rate_wait = 20.0
    fb_raw = _env("JARVIS_AI_FALLBACKS") or j.get("ai_fallbacks") or []
    if isinstance(fb_raw, str):
        fb_raw = [x.strip() for x in fb_raw.split(",")]
    fallbacks = [str(x).strip().lower() for x in fb_raw if str(x).strip() and str(x).strip().lower() != provider]
    try:
        retries = int(_env("JARVIS_AI_MAX_RETRIES") or j.get("ai_max_retries") or 2)
    except Exception:
        retries = 2
    tiers = default_tiers(provider)
    if _cfgmod is not None:
        try:
            tiers = _cfgmod.tier_table(provider)
            if rpm is None:
                rpm = _cfgmod.provider_rpm(provider)
        except Exception:
            pass
    # the provider's own normal / fast models come from the tier table unless set above
    def _concrete(t):
        v = str((tiers.get(t) or {}).get("model") or "")
        return "" if v.startswith("@") else v
    model = model or _concrete("normal")
    fast = fast or _concrete("fast")
    if provider != "google":
        model, fast = ("" if m.startswith("gemini") else m for m in (model, fast))
    # per-tier provider routing (config/models.json "tier_providers", or env)
    tier_providers: dict = {}
    if _cfgmod is not None:
        try:
            tp = _cfgmod.load("models").get("tier_providers")
            if isinstance(tp, dict):
                tier_providers = {str(k).lower(): str(v).lower() for k, v in tp.items() if v}
        except Exception:
            pass
    env_tp = _env("JARVIS_AI_TIER_PROVIDERS")            # "deep=nvidia,vision=google"
    for pair in env_tp.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            tier_providers[k.strip().lower()] = v.strip().lower()
    tier_providers = {k: v for k, v in tier_providers.items() if v in KNOWN_PROVIDERS and v != provider}
    return AIConfig(
        provider=provider,
        model=model,
        fast_model=fast,
        effort=ReasoningEffort.parse(_env("JARVIS_AI_EFFORT") or j.get("ai_effort")),
        fallbacks=fallbacks,
        local_base_url=_env("JARVIS_LOCAL_BASE_URL") or str(j.get("local_base_url") or ""),
        local_api=(_env("JARVIS_LOCAL_API") or str(j.get("local_api") or "ollama")).lower(),
        openai_base_url=_env("OPENAI_BASE_URL") or str(j.get("openai_base_url") or ""),
        max_retries=max(0, min(retries, 5)),
        deep_model=deep,
        vision_model=vision,
        rpm=rpm,
        rate_wait_max=max(0.0, min(rate_wait, 120.0)),
        tiers=tiers,
        tier_providers=tier_providers,
    )


# ── Provider factory registry (extensible) ───────────────────────────────────
ProviderFactory = Callable[[AIConfig, str, str], AIProvider]
_FACTORIES: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Add a provider without editing this file: factory(cfg, model, fast_model)."""
    _FACTORIES[name.strip().lower()] = factory


def _google(cfg: AIConfig, model: str, fast: str) -> AIProvider:
    from core.providers.google_provider import GoogleProvider
    return GoogleProvider(api_key=secret_store.get_secret("GOOGLE_API_KEY"),
                          model=model or GOOGLE_DEFAULT_MODEL, fast_model=fast or GOOGLE_FAST_MODEL)


def _config_models(provider: str, model: str, fast: str, default: str, default_fast: str) -> tuple[str, str]:
    """A fallback / tier-routed provider gets no model from the caller → take the
    provider's own normal / fast models from config/models.json."""
    try:
        from core.config import model_for
        model = model or model_for("normal", provider) or ""
        fast = fast or model_for("fast", provider) or ""
    except Exception:
        pass
    model = model if model and not model.startswith("@") else default
    fast = fast if fast and not fast.startswith("@") else (default_fast or model)
    return model, fast


ANTHROPIC_DEFAULT_MODEL, ANTHROPIC_FAST_MODEL = "claude-sonnet-5-5", "claude-haiku-4-5"


def _anthropic(cfg: AIConfig, model: str, fast: str) -> AIProvider:
    from core.providers.anthropic_provider import AnthropicProvider
    model, fast = _config_models("anthropic", model, fast, ANTHROPIC_DEFAULT_MODEL, ANTHROPIC_FAST_MODEL)
    return AnthropicProvider(api_key=secret_store.get_secret("ANTHROPIC_API_KEY"),
                             model=model, fast_model=fast)


def _openai(cfg: AIConfig, model: str, fast: str) -> AIProvider:
    from core.providers.openai_provider import OpenAIProvider
    return OpenAIProvider(api_key=secret_store.get_secret("OPENAI_API_KEY"), model=model,
                          fast_model=fast, base_url=cfg.openai_base_url)


def _local(cfg: AIConfig, model: str, fast: str) -> AIProvider:
    if cfg.local_api == "openai":
        from core.providers.openai_provider import OpenAIProvider
        return OpenAIProvider(api_key=secret_store.get_secret("LOCAL_AI_API_KEY"), model=model,
                              fast_model=fast, base_url=cfg.local_base_url or "http://localhost:1234/v1",
                              name="local", require_key=False)
    from core.providers.local_provider import LocalProvider
    return LocalProvider(model=model, fast_model=fast, base_url=cfg.local_base_url)


NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_DEFAULT_MODEL = "poolside/laguna-xs-2.1"       # nemotron-super-49b-v1.5 reached end of life (410)


def _nvidia(cfg: AIConfig, model: str, fast: str) -> AIProvider:
    """NVIDIA NIM — https://integrate.api.nvidia.com/v1, OpenAI-compatible and
    FREE-tier (nemotron / deepseek / llama models). A genuinely free second
    provider, so it (unlike openai/anthropic) may be used as a fallback and for
    per-tier routing without breaking the no-paid-keys rule. Needs NVIDIA_API_KEY."""
    from core.providers.openai_provider import OpenAIProvider
    # tier-routed / fallback use gives no model here → its own normal + fast models from config
    model, fast = _config_models("nvidia", model, fast, NVIDIA_DEFAULT_MODEL, NVIDIA_DEFAULT_MODEL)
    from core.providers import nvidia_keys
    return OpenAIProvider(api_key=nvidia_keys.key_for(""), model=model, fast_model=fast,
                          base_url=NVIDIA_BASE_URL, name="nvidia", keys_for=nvidia_keys.keys_for)


register_provider("google", _google)
register_provider("anthropic", _anthropic)
register_provider("openai", _openai)
register_provider("local", _local)
register_provider("nvidia", _nvidia)


def build_provider(name: str, cfg: AIConfig, primary: bool = True) -> AIProvider:
    f = _FACTORIES.get(name)
    if f is None:
        raise ProviderConfigError(f"Unknown AI provider '{name}'. Known: {', '.join(sorted(_FACTORIES))}.",
                                  provider=name)
    # Fallback providers use their own default model (a model name is provider-specific).
    return f(cfg, cfg.model if primary else "", cfg.fast_model if primary else "")


# ── Free-tier throttle ───────────────────────────────────────────────────────

class RateGate:
    """Client-side sliding-window limiter, one window per provider:model.

    acquire() blocks until a slot is free (at most `max_wait` seconds) and
    returns how long it waited; if the wait would be longer it raises
    RateLimited immediately so the gateway can try a fallback instead of
    freezing the assistant. cooldown() records a server-side 429 so every
    caller backs off together instead of each discovering the limit."""

    WINDOW = 60.0

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._clock, self._sleep = clock, sleep
        self._hits: dict[str, list[float]] = {}
        self._cool: dict[str, float] = {}
        self._lock = threading.Lock()

    def _wait_needed(self, key: str, rpm: int, now: float) -> float:
        hits = [t for t in self._hits.get(key, []) if now - t < self.WINDOW]
        self._hits[key] = hits
        wait = max(0.0, self._cool.get(key, 0.0) - now)
        if rpm > 0 and len(hits) >= rpm:
            wait = max(wait, self.WINDOW - (now - hits[len(hits) - rpm]) + 0.05)
        return wait

    def acquire(self, key: str, rpm: int, max_wait: float) -> float:
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                wait = self._wait_needed(key, rpm, now)
                if wait <= 0:
                    self._hits.setdefault(key, []).append(now)
                    return waited
            if waited + wait > max_wait:
                err = RateLimited(f"free-tier limit for {key.split(':', 1)[-1]} reached — "
                                  f"next slot in {int(wait) + 1}s", provider=key.split(":", 1)[0])
                err.local_gate = True
                raise err
            self._sleep(wait)
            waited += wait

    def cooldown(self, key: str, seconds: float) -> None:
        with self._lock:
            self._cool[key] = max(self._cool.get(key, 0.0), self._clock() + max(0.0, seconds))

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            return {k: len([t for t in v if now - t < self.WINDOW]) for k, v in self._hits.items()}


_RETRY_HINT = re.compile(r"retry(?:[_ ]?delay)?[\"':\s]*(?:in\s*)?(\d+(?:\.\d+)?)\s*s", re.I)
_DAILY_HINT = re.compile(r"per[ _-]?day|perday|daily|requests_per_day|RPD", re.I)


def _retry_after(err: Exception) -> float | None:
    m = _RETRY_HINT.search(str(err))
    return float(m.group(1)) if m else None


# ── Gateway ──────────────────────────────────────────────────────────────────

class AIGateway:
    def __init__(self, config: AIConfig | None = None, providers: dict[str, AIProvider] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 health: ModelHealth | None = None):
        self._fixed_config = config
        # injected configs (tests, tools) never touch memory/model_health.json
        self.health = health or ModelHealth(persist=config is None)
        self._providers: dict[str, AIProvider] = dict(providers or {})
        for _p in self._providers.values():
            try:
                _p._injected = True          # survive config-change rebuilds
            except Exception:
                pass
        self._sig = ""
        self._lock = threading.Lock()
        self._sleep = sleep
        self.gate = RateGate(clock=clock, sleep=sleep)
        self.stats = {"calls": 0, "failures": 0, "fallbacks_used": 0, "local_answers": 0,
                      "degraded": 0, "simplified": 0, "images": 0, "throttled_s": 0.0, "by_tier": {}}
        self.last_route: dict | None = None
        # Part One: an exact model chosen at runtime ("switch to gemini 3.7 flash").
        # In memory only — a restart returns to config/models.json. While set, the
        # listed tiers use exactly this model: no alternates, no other provider,
        # no silent substitution; a failure is reported as a failure.
        self._runtime_model: dict | None = None
        self.on_model_change: Callable[[dict | None], None] | None = None

    # -- runtime model override (Part One) ----------------------------------
    def set_runtime_model(self, model_id: str, provider: str,
                          tiers: Iterable[str] = ("normal", "deep", "research")) -> dict:
        prov = str(provider or "").strip().lower()
        if prov not in KNOWN_PROVIDERS:
            raise ProviderConfigError(f"Unknown provider '{provider}'.")
        mid = str(model_id or "").strip()
        if not mid:
            raise ProviderConfigError("No model id given.")
        self._runtime_model = {"id": mid, "provider": prov, "tiers": tuple(tiers), "since": time.time()}
        self._notify_model_change()
        return dict(self._runtime_model)

    def clear_runtime_model(self) -> None:
        self._runtime_model = None
        self._notify_model_change()

    def runtime_model(self) -> dict | None:
        return dict(self._runtime_model) if self._runtime_model else None

    def active_brain(self) -> dict:
        """What answers a 'normal' request right now: {model, provider, runtime}."""
        if self._runtime_model:
            return {"model": self._runtime_model["id"], "provider": self._runtime_model["provider"],
                    "runtime": True, "tiers": list(self._runtime_model["tiers"])}
        cfg = self.config
        prov = cfg.provider_for("normal")
        try:
            c = cfg if prov == cfg.provider else self._reconfig(cfg, prov)
            p = self._provider(prov, c, True, sig_cfg=cfg)
            model = self._model_for(p, c, "normal", True)
        except Exception:
            model = ""
        return {"model": model, "provider": prov, "runtime": False, "tiers": []}

    def _notify_model_change(self) -> None:
        cb = self.on_model_change
        if cb is not None:
            try:
                cb(self.runtime_model())
            except Exception:
                pass

    # -- config / provider resolution ------------------------------------
    @property
    def config(self) -> AIConfig:
        return self._fixed_config or load_config()

    def _provider(self, name: str, cfg: AIConfig, primary: bool, sig_cfg: AIConfig | None = None) -> AIProvider:
        # sig_cfg (the base on-disk config) drives cache invalidation, so per-request
        # tier-routing configs don't thrash the provider cache.
        with self._lock:
            sig = (sig_cfg or cfg).signature()
            if sig != self._sig and self._fixed_config is None:
                # config changed on disk → rebuild lazily (injected providers survive)
                self._providers = {k: v for k, v in self._providers.items() if getattr(v, "_injected", False)}
                self._sig = sig
            p = self._providers.get(name)
            if p is None:
                p = build_provider(name, cfg, primary=(primary and name == cfg.provider))
                self._providers[name] = p
            return p

    @staticmethod
    def _reconfig(cfg: AIConfig, provider: str) -> AIConfig:
        """A per-request config that routes to `provider`, using that provider's
        own tier table (config/models.json) and its own models."""
        from dataclasses import replace
        try:
            from core import config as _cfgmod
            tiers = _cfgmod.tier_table(provider)
            rpm = _cfgmod.provider_rpm(provider)
        except Exception:
            tiers, rpm = default_tiers(provider), None
        return replace(cfg, provider=provider, tiers=tiers, model="", fast_model="", deep_model="",
                       vision_model="", tier_providers={},
                       rpm=rpm if rpm is not None else (None if cfg.rpm is None else cfg.rpm))

    def provider(self) -> AIProvider:
        cfg = self.config
        return self._provider(cfg.provider, cfg, True)

    def capabilities(self) -> set[str]:
        try:
            return {c.value for c in self.provider().capabilities}
        except ProviderError:
            return set()

    def supports(self, cap: Capability) -> bool:
        try:
            return self.provider().supports(cap)
        except ProviderError:
            return False

    def info(self) -> dict:
        cfg = self.config
        out = {"provider": cfg.provider, "fallbacks": cfg.fallbacks,
               "effort": cfg.effort.value if cfg.effort else None,
               "paid_provider_selected": cfg.provider in PAID_PROVIDERS and not (
                   cfg.provider == "openai" and cfg.openai_base_url),
               "keys": secret_store.describe(), "stats": dict(self.stats),
               "rpm": cfg.rpm_for(cfg.provider), "window": self.gate.snapshot(),
               "runtime_model": self.runtime_model()}
        try:
            p = self.provider()
            out.update(p.info())
            out["tiers"] = {t: self._model_for(p, cfg, t, True) for t in model_router.ALL_TIERS}
            out["tiers"]["deep_effort"] = str((self._tier_cfg(cfg, "deep") or {}).get("effort") or "high")
            out["chains"] = {
                t: [f"{lab}:{m}" + (f"+{getattr(e, 'value', e)}" if e else "") for lab, m, e in
                    self._entries(p, cfg, t, True, None,
                                  ReasoningEffort.parse(self._tier_cfg(cfg, t).get("effort")), True)]
                for t in model_router.TIERS + ("image",)}
            out["health"] = {m: r.get("status") for m, r in self.health.all().items()}
        except ProviderError as e:
            out["error"] = str(e)
        return out

    # -- tiers ----------------------------------------------------------------
    @staticmethod
    def _tier_cfg(cfg: AIConfig, tier: str) -> dict:
        table = cfg.tiers or default_tiers(cfg.provider) or default_tiers("google")
        return table.get(tier) or {}

    @classmethod
    def _model_for(cls, p: AIProvider, cfg: AIConfig, tier: str, primary: bool, _depth: int = 0) -> str:
        if tier == "fast":
            return p.fast_model or p.default_model
        if tier == "normal" or _depth > 4:
            return p.default_model
        if tier == "deep" and primary and cfg.deep_model:
            return cfg.deep_model
        if tier == "vision" and primary and cfg.vision_model:
            return cfg.vision_model
        v = str(cls._tier_cfg(cfg, tier).get("model") or "")
        if v.startswith("@"):
            return cls._model_for(p, cfg, v[1:], primary, _depth + 1)
        if v and primary and not (p.name != "google" and v.startswith("gemini")):
            return v
        return p.default_model

    def _entries(self, p: AIProvider, cfg: AIConfig, tier: str, primary: bool, model: str | None,
                 effort, soft: bool, fallback: bool = True) -> list[tuple[str, str, Any]]:
        """[(tier label, model, effort), …] — the order this request will try."""
        tcfg = self._tier_cfg(cfg, tier)
        first = model or self._model_for(p, cfg, tier, primary)
        out = [(tier, first, effort)]
        if fallback and not model:
            if primary:
                for alt in self._alternates(cfg, tier):
                    out.append((tier, alt, effort))
            for fb in tcfg.get("fallback") or []:
                fb = model_router.normalize_tier(fb) if fb != "image" else fb
                fcfg = self._tier_cfg(cfg, fb)
                # a fallback lane uses its own effort unless the caller insisted on one
                feff = effort if not soft else ReasoningEffort.parse(fcfg.get("effort"))
                out.append((fb, self._model_for(p, cfg, fb, primary), feff))
                if primary:
                    for alt in self._alternates(cfg, fb):
                        out.append((fb, alt, feff))
        seen, uniq = set(), []
        for e in out:
            k = (e[1], e[2])
            if k not in seen:
                seen.add(k)
                uniq.append(e)
        usable = [e for e in uniq if self.health.usable(e[1])]
        return usable or uniq[:1]

    @classmethod
    def _alternates(cls, cfg: AIConfig, tier: str, _depth: int = 0) -> list[str]:
        """A tier's other models; a tier that borrows its model ("@normal")
        borrows that tier's alternates too (each model has its own free quota)."""
        tcfg = cls._tier_cfg(cfg, tier)
        if tcfg.get("alternates"):
            return [str(a) for a in tcfg["alternates"]]
        v = str(tcfg.get("model") or "")
        if v.startswith("@") and _depth < 4:
            return cls._alternates(cfg, v[1:], _depth + 1)
        return []

    def select(self, task_type: str = "auto", complexity: str = "auto", **kw) -> model_router.ModelChoice:
        """ModelRouter.select() against the live provider's models."""
        cfg = self.config
        try:
            p = self.provider()
            resolve = lambda tier, v: self._model_for(p, cfg, tier, True)   # noqa: E731
        except ProviderError:
            resolve = None
        table = cfg.tiers or default_tiers(cfg.provider)
        return model_router.ModelRouter(table, resolve=resolve, usable=self.health.usable).select(
            task_type, complexity, **kw)

    def list_models(self) -> list[str]:
        try:
            return list(self.provider().list_models())
        except Exception:
            return []

    def tier_models(self, tier: str) -> list[str]:
        """Every model CONFIGURED for a tier (primary + alternates), unfiltered by
        health — for asking 'is this capability available at all on this key?'."""
        cfg = self.config
        try:
            first = self._model_for(self.provider(), cfg, tier, True)
        except ProviderError:
            v = str(self._tier_cfg(cfg, tier).get("model") or "")
            first = "" if v.startswith("@") else v
        models = [first] + [a for a in self._alternates(cfg, tier) if a != first]
        return [m for m in models if m]

    def tier_available(self, tier: str) -> bool:
        """True when at least one configured model for the tier is usable now."""
        models = self.tier_models(tier)
        return bool(models) and any(self.health.usable(m) for m in models)

    def route(self, prompt: Any = "", **kw) -> model_router.RouteDecision:
        """Expose the router's decision without calling a model."""
        return model_router.route(self._prompt_text(prompt), **kw)

    @staticmethod
    def _prompt_text(prompt: Any) -> str:
        if isinstance(prompt, list):
            out = []
            for m in prompt:
                for part in getattr(m, "parts", []) or []:
                    if getattr(part, "kind", "") == "text":
                        out.append(str(getattr(part, "text", "") or ""))
            return "\n".join(out)
        return "" if prompt is None else str(prompt)

    # -- request building -------------------------------------------------
    @staticmethod
    def _messages(prompt, images, audio, documents, history) -> list[Message]:
        msgs: list[Message] = list(history or [])
        if isinstance(prompt, list) and prompt and isinstance(prompt[0], Message):
            msgs.extend(prompt)
            return msgs
        parts: list[Part] = []
        for img in images or []:
            data, mime = (img if isinstance(img, tuple) else (img, "image/png"))
            parts.append(Part.of_image(data, mime))
        for a in audio or []:
            data, mime = (a if isinstance(a, tuple) else (a, "audio/mpeg"))
            parts.append(Part.of_audio(data, mime))
        for d in documents or []:
            data, mime = (d if isinstance(d, tuple) else (d, "application/pdf"))
            parts.append(Part.of_document(data, mime))
        if prompt:
            parts.append(Part.of_text(str(prompt)))
        msgs.append(Message("user", parts))
        return msgs

    def generate(self, prompt: Any = "", *, system: str = "", images: Iterable = (),
                 audio: Iterable = (), documents: Iterable = (), history: Iterable[Message] = (),
                 tier: str = "default", schema: dict | None = None, tools: Iterable[ToolSpec] = (),
                 tool_choice: str = "auto", reasoning: Any = None, web_grounding: bool | None = None,
                 max_output_tokens: int | None = None, temperature: float | None = None,
                 timeout: float = 60.0, model: str | None = None, fallback: bool = True,
                 system_prefix: str = "") -> AIResponse:
        """One text/vision generation. `tier` picks the lane (see module doc);
        `model` pins an exact model (no fallback chain); `fallback=False` tries
        only the tier's first model. `system_prefix` (the persona) is sent
        ahead of `system` but does not count as request context when routing."""
        cfg = self.config
        images, audio, documents, history = list(images or ()), list(audio or ()), \
            list(documents or ()), list(history or ())
        tools = list(tools or ())
        decision: model_router.RouteDecision | None = None
        t = model_router.normalize_tier(tier)
        if t == "auto":
            hist_chars = sum(len(self._prompt_text([m])) for m in history)
            decision = model_router.route(
                self._prompt_text(prompt), images=len(images), documents=len(documents),
                audio=len(audio), context_chars=len(system or "") + hist_chars,
                allow_local=not (schema or tools or history))
            self.last_route = decision.as_dict()
            if decision.local_answer is not None:
                self.stats["local_answers"] += 1
                self._count_tier("local")
                return AIResponse(text=decision.local_answer, provider="local-exact", model="arithmetic",
                                  finish_reason="stop",
                                  meta={"tier": "local", "route": decision.as_dict(), "degraded": False})
            t = decision.tier
        # Per-tier provider routing: a tier can be served by a different provider
        # (e.g. deep → nvidia). That provider's own tier table supplies its models.
        base_cfg = cfg
        override = self._runtime_model if (model is None and self._runtime_model
                                           and t in self._runtime_model.get("tiers", ())) else None
        if override:
            model, fallback = override["id"], False      # exactly this model, nothing else
            prov_name = override["provider"]
        else:
            prov_name = cfg.provider_for(t)
        if prov_name != cfg.provider:
            cfg = self._reconfig(cfg, prov_name)
        tcfg = self._tier_cfg(cfg, t)
        # reasoning: explicit argument > tier setting (config/models.json) > router > configured effort
        soft_reasoning = reasoning is None       # implicit → dropped if a provider can't do it
        if reasoning is not None:
            eff = ReasoningEffort.parse(reasoning)
        elif tcfg.get("effort"):
            eff = ReasoningEffort.parse(tcfg.get("effort"))
        elif decision is not None and decision.effort:
            eff = ReasoningEffort.parse(decision.effort)
        else:
            eff = cfg.effort
        if max_output_tokens is None and decision is not None:
            max_output_tokens = decision.max_output_tokens
        if max_output_tokens is None and tcfg.get("max_output_tokens"):
            max_output_tokens = int(tcfg["max_output_tokens"])
        soft_grounding = web_grounding is None
        if web_grounding is None:
            web_grounding = bool(tcfg.get("grounding")) and not schema and not tools
        if system_prefix:
            system = f"{system_prefix}\n\n{system}" if system else system_prefix
        base = AIRequest(
            messages=self._messages(prompt, images, audio, documents, history), system=system,
            tools=tools, tool_choice=tool_choice, response_schema=schema,
            reasoning=eff, web_grounding=bool(web_grounding), max_output_tokens=max_output_tokens,
            temperature=temperature, timeout=timeout)
        self._count_tier(t)
        chain = self._provider_chain(cfg, base_cfg, fallback)
        last: Exception | None = None
        for idx, name in enumerate(chain):
            try:
                p = self._provider(name, cfg, primary=(idx == 0), sig_cfg=base_cfg)
            except ProviderError as e:
                last = e
                continue
            if model is None and idx < len(chain) - 1 and self._all_resting(p, cfg, t, idx == 0, eff,
                                                                             soft_reasoning, fallback):
                # every model this provider would try is resting (no credit, quota
                # used up, overloaded …) — don't spend seconds proving it again
                self.stats["providers_skipped"] = self.stats.get("providers_skipped", 0) + 1
                last = ProviderError(f"{name}: every model for '{t}' is resting "
                                     f"({', '.join(self._resting_reasons(p, cfg, t, idx == 0))})",
                                     provider=name, retryable=True)
                continue
            r = self._run_chain(p, cfg, base, t, idx == 0, model, eff, soft_reasoning, soft_grounding,
                                fallback)
            if isinstance(r, AIResponse):
                if idx > 0:
                    self.stats["fallbacks_used"] += 1
                r.meta = {**(r.meta or {}), "tier": t,
                          "route": decision.as_dict() if decision else None,
                          "fallback_provider": name if idx > 0 else None,
                          "runtime_model": bool(override)}
                return r
            last = r
            # Config errors on the primary (bad key) should still allow a configured fallback.
        self.stats["failures"] += 1
        if last is None:
            last = ProviderConfigError("No AI provider available.")
        if override:
            raise ProviderError(f"The model you selected, {override['id']} ({override['provider']}), failed and "
                                f"JARVIS did not substitute another model: {last}. Say 'reset the model' to go "
                                f"back to the default.", provider=override["provider"], retryable=False,
                                status=getattr(last, "status", None))
        raise last

    # -- fallback chain -------------------------------------------------------
    CHAIN_MAX_ATTEMPTS = 5             # models actually called for one request, at most

    @staticmethod
    def _provider_chain(cfg: AIConfig, base_cfg: AIConfig, fallback: bool) -> list[str]:
        """Providers to try, in order: the tier's provider, then the configured
        fallbacks. When a tier is routed to another provider (coding → nvidia),
        the main provider stays in the chain right after it — a tier routed away
        must never lose the brain the user chose as primary."""
        chain = [cfg.provider]
        if not fallback:
            return chain
        if base_cfg.provider not in chain:
            chain.append(base_cfg.provider)
        for f in base_cfg.fallbacks:
            if f not in chain:
                chain.append(f)
        return chain

    def _all_resting(self, p: AIProvider, cfg: AIConfig, t: str, primary: bool, eff, soft: bool,
                     fallback: bool) -> bool:
        try:
            ents = self._entries(p, cfg, t, primary, None, eff, soft, fallback)
        except Exception:
            return False
        return bool(ents) and all(not self.health.usable(e[1]) for e in ents)

    def _resting_reasons(self, p: AIProvider, cfg: AIConfig, t: str, primary: bool) -> list[str]:
        out = []
        try:
            for e in self._entries(p, cfg, t, primary, None, ReasoningEffort.parse(None), True, True):
                out.append(f"{e[1]}: {(self.health.get(e[1]) or {}).get('status') or 'resting'}")
        except Exception:
            pass
        return out[:3] or ["resting"]

    @staticmethod
    def _overloaded(r) -> bool:
        return (isinstance(r, ProviderError) and not isinstance(r, (CapabilityNotSupported, ProviderConfigError))
                and (getattr(r, "status", None) in (500, 502, 503, 504)
                     or "UNAVAILABLE" in str(r) or "overloaded" in str(r).lower()))

    @staticmethod
    def _light(req: AIRequest) -> bool:
        """A short request a healthy model answers in seconds."""
        try:
            chars = len(req.system or "") + sum(len(pt.text or "") for m in req.messages for pt in m.parts
                                                if pt.kind == "text")
        except Exception:
            return False
        return chars < 6000 and (req.max_output_tokens is None or req.max_output_tokens <= 2048)

    @staticmethod
    def _timed_out(r) -> bool:
        s = str(r).lower()
        return isinstance(r, ProviderError) and ("timed out" in s or "timeout" in s or "deadline" in s)

    _TOKEN_ERR = re.compile(r"token.{0,60}(exceed|too (?:long|large|many)|maximum|limit)|"
                            r"(exceed|too long|too large).{0,60}token|context (?:length|window)|"
                            r"request payload size|input is too long", re.I)

    @classmethod
    def _too_big(cls, r) -> bool:
        return isinstance(r, ProviderError) and not isinstance(r, RateLimited) \
            and bool(cls._TOKEN_ERR.search(str(r)))

    @staticmethod
    def _empty_truncated(r: AIResponse) -> bool:
        return (not r.text and not r.tool_calls and not r.images and r.data is None
                and "MAX_TOKENS" in str(r.finish_reason).upper())

    @staticmethod
    def _simplify(req: AIRequest) -> AIRequest:
        """Make a request smaller without changing what it asks: keep the last 4
        history messages, keep ≤ 2 images, middle-cut long text parts."""
        msgs = list(req.messages)
        last = msgs[-1:] if msgs else []
        hist = msgs[:-1][-4:]
        total = sum(len(pt.text) for m in msgs for pt in m.parts if pt.kind == "text")
        budget = max(8000, total // 2)

        def cut(s: str, limit: int) -> str:
            if len(s) <= limit:
                return s
            head = int(limit * 0.6)
            return s[:head] + "\n\n…[trimmed for length]…\n\n" + s[-(limit - head):]
        out = []
        n_img = 0
        for m in hist + last:
            parts = []
            for pt in m.parts:
                if pt.kind == "image":
                    n_img += 1
                    if n_img > 2:
                        continue
                if pt.kind == "text":
                    pt = Part.of_text(cut(pt.text, max(2000, budget // max(1, len(msgs)) * 2)))
                parts.append(pt)
            out.append(Message(m.role, parts, m.tool_call_id, m.tool_name, list(m.tool_calls)))
        return AIRequest(**{**req.__dict__, "messages": out, "system": cut(req.system or "", 6000)})

    def _run_chain(self, p: AIProvider, cfg: AIConfig, base: AIRequest, t: str, primary: bool,
                   model: str | None, eff, soft_reasoning: bool, soft_grounding: bool,
                   fallback: bool):
        entries = self._entries(p, cfg, t, primary, model, eff, soft_reasoning, fallback)
        blocked: set[str] = set()          # models that are rate-limited / overloaded / missing now
        slow_lanes: set[str] = set()       # lanes that timed out — try a lighter lane, not a sibling
        simplified = False
        r: Any = None
        tried = 0
        max_tries = self.CHAIN_MAX_ATTEMPTS
        started = time.monotonic()
        for j, (label, m, e) in enumerate(entries):
            if m in blocked or label in slow_lanes:
                continue
            if tried >= max_tries or (tried and time.monotonic() - started > base.timeout * 2.5):
                break                            # bounded: never cascade for minutes
            req = AIRequest(**{**base.__dict__, "model": m, "reasoning": e})
            tried += 1
            r = self._with_retries(p, req, cfg.max_retries if tried == 1 else 0, cfg=cfg,
                                   soft_reasoning=soft_reasoning or j > 0, soft_grounding=soft_grounding,
                                   alone=len(entries) == 1)
            if isinstance(r, AIResponse) and self._empty_truncated(r) and j + 1 < len(entries):
                r = ProviderError(f"{p.name}: empty answer — the output budget ran out", provider=p.name,
                                  retryable=True)
            if self._too_big(r) and not simplified:
                simplified = True
                r2 = self._with_retries(p, self._simplify(req), 0, cfg=cfg, soft_reasoning=True,
                                        soft_grounding=soft_grounding)
                if isinstance(r2, AIResponse):
                    r = r2
                    self.stats["simplified"] += 1
            if isinstance(r, AIResponse):
                self.health.record_ok(m)
                degraded = j > 0
                if degraded:
                    self.stats["degraded"] += 1
                r.meta = {**(r.meta or {}), "degraded": degraded, "served_tier": label,
                          "simplified": simplified, "attempted_models": tried}
                return r
            # ── failure: decide whether the next link of the chain can help ──
            if self.health.record_error(m, r):
                blocked.add(m)
            elif self._timed_out(r) and self._light(req):
                # Phase 7: a model that cannot answer a SHORT request in time is not
                # slow, it is stuck (measured: one Gemini model held a request for
                # 412 s). Rest it so the next requests go straight to one that answers.
                try:
                    from core.model_health import STATUS_OVERLOADED
                    self.health.mark(m, STATUS_OVERLOADED, "timed out on a short request")
                except Exception:
                    pass
                blocked.add(m)
            if isinstance(r, (CapabilityNotSupported, ProviderConfigError)):
                return r                         # provider-level: no model here will do better
            if isinstance(r, RateLimited) or self._overloaded(r):
                blocked.add(m)                   # quota/overload is per model — skip its other lanes
                continue
            if self._timed_out(r):
                slow_lanes.add(label)
                continue
            if self._too_big(r) or m in blocked or \
                    (isinstance(r, ProviderError) and r.retryable):
                continue                         # a lighter lane / other model may still answer
            return r                             # a real request error: don't burn more quota
        return r if r is not None else ProviderError(f"{p.name}: no usable model for tier '{t}'",
                                                     provider=p.name)

    def _count_tier(self, t: str) -> None:
        bt = self.stats.setdefault("by_tier", {})
        bt[t] = bt.get(t, 0) + 1

    def _with_retries(self, p: AIProvider, req: AIRequest, retries: int, cfg: AIConfig | None = None,
                      soft_reasoning: bool = False, soft_grounding: bool = False, alone: bool = True):
        """One model, with retries for what a retry can fix. Phase 7: a timeout is
        never retried on the same model (a stuck model would hold the user for
        another full timeout), and an overloaded model (5xx) is retried only when
        it is the only model this request may use — otherwise the chain moves on."""
        cfg = cfg or self.config
        attempt_req = req
        if soft_reasoning and attempt_req.reasoning is not None and not p.supports(Capability.REASONING):
            attempt_req = AIRequest(**{**attempt_req.__dict__, "reasoning": None})
        if soft_grounding and attempt_req.web_grounding and not p.supports(Capability.WEB_GROUNDING):
            attempt_req = AIRequest(**{**attempt_req.__dict__, "web_grounding": False})
        model = attempt_req.model or p.default_model
        key = f"{p.name}:{model}"
        rpm = cfg.rpm_for(p.name)
        delay = 1.0
        dropped_thinking = False
        attempt = 0
        while True:
            try:
                p.check(attempt_req)
                waited = self.gate.acquire(key, rpm, cfg.rate_wait_max)
                if waited:
                    self.stats["throttled_s"] = round(self.stats.get("throttled_s", 0.0) + waited, 2)
                self.stats["calls"] += 1
                return p.generate(attempt_req)
            except CapabilityNotSupported as e:
                return e
            except RateLimited as e:
                if getattr(e, "local_gate", False) or _DAILY_HINT.search(str(e)):
                    return e      # our own window is full / daily quota: waiting won't help now
                if _health_class(e) in ("no_quota", "exhausted", "unavailable", "unauthorized"):
                    return e      # "limit: 0" → this model has no free quota on this key
                hint = _retry_after(e)
                self.gate.cooldown(key, hint if hint is not None else 0.0)
                if attempt >= retries:
                    return e
                pause = max(delay, hint or 0.0)
                if pause > cfg.rate_wait_max:
                    return e
                self._sleep(pause)
                self.stats["throttled_s"] = round(self.stats.get("throttled_s", 0.0) + pause, 2)
                delay = min(delay * 2, 8.0)
            except ProviderError as e:
                msg = str(e).lower()
                if (not dropped_thinking and attempt_req.reasoning is not None and not e.retryable
                        and ("think" in msg or "reasoning" in msg
                             or (getattr(e, "status", None) == 400 and "invalid_argument" in msg
                                 and "request contains an invalid argument" in msg))):
                    # model rejected the thinking setting → retry once without it
                    attempt_req = AIRequest(**{**attempt_req.__dict__, "reasoning": None})
                    dropped_thinking = True
                    continue
                if not e.retryable or attempt >= retries:
                    return e
                if self._timed_out(e) or (self._overloaded(e) and not alone):
                    return e
                self._sleep(delay)
                delay = min(delay * 2, 8.0)
            except Exception as e:  # never let a vendor exception type escape
                return ProviderError(f"{p.name}: {secret_store.redact(e)}", provider=p.name)
            attempt += 1

    # -- image output -----------------------------------------------------------
    def generate_image(self, prompt: str, *, images: Iterable = (), aspect_ratio: str | None = None,
                       model: str | None = None, timeout: float = 120.0) -> AIResponse:
        """Generate (or edit, when `images` are given) a picture with the image
        tier's model. Returns an AIResponse whose .images holds [(bytes, mime)].
        Raises CapabilityNotSupported when the provider has no image model, and
        the underlying ProviderError (e.g. no free quota) when none of the
        configured image models answers — never a fake picture."""
        cfg = self.config
        p = self.provider()
        if not p.supports(Capability.IMAGE_OUTPUT):
            raise CapabilityNotSupported(p.name, Capability.IMAGE_OUTPUT)
        base = AIRequest(messages=self._messages(prompt, list(images or ()), (), (), ()),
                         response_modalities=["TEXT", "IMAGE"], image_aspect=aspect_ratio, timeout=timeout)
        self._count_tier("image")
        entries = self._entries(p, cfg, "image", True, model, None, True, fallback=model is None)
        last: Any = None
        for _label, m, _e in entries:
            if m in ("", p.default_model, p.fast_model) and not model:
                continue                     # never let a text model stand in for an image model
            r = self._with_retries(p, AIRequest(**{**base.__dict__, "model": m}), 1, cfg=cfg)
            if isinstance(r, AIResponse):
                if r.images:
                    self.health.record_ok(m)
                    self.stats["images"] += 1
                    r.meta = {**(r.meta or {}), "tier": "image", "served_tier": "image"}
                    return r
                last = ProviderError(f"{p.name}: {m} answered without an image"
                                     + (f" ({r.text[:120]})" if r.text else ""), provider=p.name)
                continue
            self.health.record_error(m, r)
            last = r
            if isinstance(r, (CapabilityNotSupported, ProviderConfigError)):
                break
        self.stats["failures"] += 1
        raise last if last is not None else ProviderConfigError("No image model is configured.",
                                                                provider=p.name)

    # -- conveniences -------------------------------------------------------
    def text(self, prompt: Any, **kw) -> str:
        return self.generate(prompt, **kw).text

    def structured(self, prompt: Any, schema: dict, **kw) -> Any:
        return self.generate(prompt, schema=schema, **kw).json()

    def vision(self, prompt: str, image: bytes, mime_type: str = "image/png", **kw) -> str:
        return self.generate(prompt, images=[(image, mime_type)], **kw).text

    def stream(self, prompt: Any, *, system: str = "", tier: str = "default", **kw) -> Iterator[str]:
        cfg = self.config
        p = self._provider(cfg.provider, cfg, True)
        images = list(kw.pop("images", ()) or ())
        t = model_router.normalize_tier(tier)
        if t == "auto":
            t = model_router.route(self._prompt_text(prompt), images=len(images), allow_local=False).tier
        model = self._model_for(p, cfg, t, True)
        req = AIRequest(messages=self._messages(prompt, images, (), (), ()),
                        system=system, model=model,
                        reasoning=ReasoningEffort.parse(self._tier_cfg(cfg, t).get("effort")) or cfg.effort,
                        **kw)
        p.check(req)
        waited = self.gate.acquire(f"{p.name}:{model}", cfg.rpm_for(p.name), cfg.rate_wait_max)
        if waited:
            self.stats["throttled_s"] = round(self.stats.get("throttled_s", 0.0) + waited, 2)
        yield from p.stream(req)

    def count_tokens(self, prompt: Any, **kw) -> int:
        p = self.provider()
        return p.count_tokens(AIRequest(messages=self._messages(prompt, (), (), (), ()),
                                        model=p.default_model, **kw))


_GATEWAY: AIGateway | None = None
_GLOCK = threading.Lock()


def get_gateway() -> AIGateway:
    global _GATEWAY
    with _GLOCK:
        if _GATEWAY is None:
            _GATEWAY = AIGateway()
        return _GATEWAY


def set_gateway(gw: AIGateway | None) -> None:
    """Test / dependency-injection hook."""
    global _GATEWAY
    with _GLOCK:
        _GATEWAY = gw
