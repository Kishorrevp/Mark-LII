"""
core/model_health.py — what each configured model can actually do on THIS key.

Google's free tier changes: a model can be missing (404), exist but carry no
free quota ("limit: 0"), run out of its daily quota, or be fine. Guessing is
how assistants end up faking features, so JARVIS records what really happened:

    record_ok(model)            a real call succeeded
    record_error(model, err)    classifies the failure:
        unavailable   404 / "not found" / "not supported for generateContent"
        no_quota      429 whose quota limit is 0 (not part of the free tier)
        exhausted     the daily quota ran out (comes back tomorrow)
        (anything else — overload, timeouts — is transient and NOT recorded)
    usable(model)               False while a recent unavailable / no_quota /
                                exhausted record is still fresh; the gateway
                                then skips straight to the tier's fallback
                                instead of spending a request to rediscover it
    probe(...)                  deliberate check of every tier (tools/model_probe.py)

Stored in memory/model_health.json — model ids, statuses, short redacted
reasons, timestamps. Never a key, never a prompt.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

STATUS_OK, STATUS_UNAVAILABLE, STATUS_NO_QUOTA, STATUS_EXHAUSTED = "ok", "unavailable", "no_quota", "exhausted"
# Phase 7 (lesson from Mark LV's model ladder): a model that is overloaded or
# not answering was retried on EVERY call — 12-15 s each time. It now rests;
# so does a per-minute rate limit and a key the provider rejects.
STATUS_OVERLOADED, STATUS_RATE, STATUS_UNAUTHORIZED = "overloaded", "rate_limited", "unauthorized"
_BAD = {STATUS_UNAVAILABLE, STATUS_NO_QUOTA, STATUS_EXHAUSTED, STATUS_OVERLOADED, STATUS_RATE, STATUS_UNAUTHORIZED}
# how long a bad record is trusted before JARVIS tries the model again (hours)
_TTL_H = {STATUS_UNAVAILABLE: 24.0, STATUS_NO_QUOTA: 24.0, STATUS_EXHAUSTED: 3.0,
          STATUS_OVERLOADED: 10 / 60, STATUS_RATE: 2 / 60, STATUS_UNAUTHORIZED: 6.0}
# (client timeouts are NOT here: a deep request that timed out says nothing
#  about the same model answering a light one — the gateway handles that per lane)
_OVERLOAD = re.compile(r"\b50[023]\b|\bUNAVAILABLE\b|overloaded|high demand", re.I)
_NO_CREDIT = re.compile(r"credit balance is too low|insufficient[_ ]quota|payment required|\bHTTP 402\b", re.I)

_NOT_FOUND = re.compile(r"\b404\b|NOT_FOUND|is not found|not supported for (?:generateContent|this method)|"
                        r"model .{0,60}(?:does not exist|not available)|unknown model|no such model", re.I)
# a retired model (NVIDIA answers 410 Gone "reached its end of life") is unavailable, not flaky
_RETIRED = re.compile(r"\b410\b|end of life|no longer available|has been (?:deprecated|retired)", re.I)
_LIMIT_ZERO = re.compile(r"limit[\"'\s:=]+0\b|quota.{0,40}\b0\b.{0,20}(?:free|limit)|free[_ ]tier.{0,40}"
                         r"(?:not (?:available|supported)|limit[\"'\s:=]+0)", re.I)
_DAILY = re.compile(r"per[ _-]?day|perday|daily|requests_per_day|RPD", re.I)


def classify(err) -> Optional[str]:
    msg = str(err)
    status = getattr(err, "status", None)
    if status in (404, 410) or _NOT_FOUND.search(msg) or _RETIRED.search(msg):
        return STATUS_UNAVAILABLE
    if _NO_CREDIT.search(msg):
        return STATUS_NO_QUOTA
    if status in (401, 403) or re.search(r"authentication failed|invalid api key|API key not valid", msg, re.I):
        return STATUS_UNAUTHORIZED
    rate = status == 429 or "RESOURCE_EXHAUSTED" in msg or "429" in msg or type(err).__name__ == "RateLimited"
    if rate and _LIMIT_ZERO.search(msg):
        return STATUS_NO_QUOTA
    if rate and _DAILY.search(msg):
        return STATUS_EXHAUSTED
    if rate:
        return STATUS_RATE
    if status in (500, 502, 503) or _OVERLOAD.search(msg):
        return STATUS_OVERLOADED
    return None


class ModelHealth:
    def __init__(self, path: Optional[Path] = None, persist: bool = True,
                 clock: Callable[[], float] = time.time):
        if path is None and persist:
            from core.secret_store import base_dir
            path = base_dir() / "memory" / "model_health.json"
        self.path = Path(path) if path else None
        self.persist = persist and self.path is not None
        self._clock = clock
        self._lock = threading.Lock()
        self._data: Optional[dict] = None

    def _load(self) -> dict:
        if self._data is None:
            self._data = {}
            if self.persist and self.path.exists():
                try:
                    d = json.loads(self.path.read_text(encoding="utf-8"))
                    self._data = d if isinstance(d, dict) else {}
                except Exception:
                    self._data = {}
        return self._data

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(self._data, indent=2, sort_keys=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
            tmp.replace(self.path)
        except Exception:
            pass

    # -- records --------------------------------------------------------------
    def get(self, model: str) -> dict:
        with self._lock:
            return dict(self._load().get(model) or {})

    def all(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._load().items()}

    def record_ok(self, model: str, note: str = "") -> None:
        if not model:
            return
        with self._lock:
            d = self._load()
            prev = d.get(model) or {}
            if prev.get("status") == STATUS_OK and not note and self._clock() - prev.get("checked", 0) < 3600:
                return                                  # nothing new — don't rewrite the file every call
            d[model] = {"status": STATUS_OK, "checked": round(self._clock()), **({"note": note[:160]} if note else {})}
            self._save()

    def record_error(self, model: str, err) -> Optional[str]:
        st = classify(err)
        if not model or st is None:
            return None
        from core.secret_store import redact
        with self._lock:
            self._load()[model] = {"status": st, "checked": round(self._clock()),
                                   "detail": redact(str(err))[:180]}
            self._save()
        return st

    def mark(self, model: str, status: str, note: str = "") -> None:
        with self._lock:
            self._load()[model] = {"status": status, "checked": round(self._clock()),
                                   **({"note": note[:160]} if note else {})}
            self._save()

    def usable(self, model: str) -> bool:
        rec = self.get(model)
        st = rec.get("status")
        if st not in _BAD:
            return True
        age_h = (self._clock() - float(rec.get("checked") or 0)) / 3600.0
        return age_h >= _TTL_H.get(st, 24.0)

    def forget(self, model: Optional[str] = None) -> None:
        with self._lock:
            if model is None:
                self._load().clear()
            else:
                self._load().pop(model, None)
            self._save()


_HEALTH: Optional[ModelHealth] = None
_HLOCK = threading.Lock()


def get_health() -> ModelHealth:
    global _HEALTH
    with _HLOCK:
        if _HEALTH is None:
            _HEALTH = ModelHealth()
        return _HEALTH


# ── deliberate probe ─────────────────────────────────────────────────────────

def probe(gateway=None, tiers=("fast", "normal", "deep", "vision", "research", "computer_use", "image"),
          image: bool = True, log: Callable[[str], None] = lambda s: None) -> dict:
    """Make the smallest real request each tier's model can serve and record the
    outcome. Costs about one free-tier request per distinct model (plus one image
    generation when `image` is True and an image model answers)."""
    from core.ai_gateway import get_gateway
    from core.providers.base import ProviderError
    gw = gateway or get_gateway()
    health = gw.health
    out: dict = {}
    done: dict = {}
    listed = gw.list_models()
    cfg, prov = gw.config, gw.provider()
    for tier in tiers:
        first = gw._model_for(prov, cfg, tier, True)
        alts = [a for a in gw._alternates(cfg, tier) if a != first]
        models = [first] + alts
        if tier == "image" and first in (prov.default_model, prov.fast_model):
            out[tier] = [{"model": "", "status": "not_configured"}]
            log(f"{tier:<13} (no image model configured)")
            continue
        for model in models:
            if model in done:
                out.setdefault(tier, []).append({"model": model, **done[model]})
                log(f"{tier:<13} {model:<42} {done[model].get('status', ''):<11} (same model as above)")
                continue
            rec: dict = {"listed": (model in listed) if listed else None}
            t0 = time.time()
            try:
                if tier == "image":
                    if not image:
                        rec.update(status="skipped")
                    else:
                        r = gw.generate_image("A flat icon: one solid blue circle on a white background.",
                                              model=model, timeout=90.0)
                        ok = bool(r.images)
                        rec.update(status=STATUS_OK if ok else "no_image",
                                   detail=f"{len(r.images)} image(s), {sum(len(b) for b, _ in r.images)} bytes")
                        if ok:
                            health.record_ok(model, "probe: image generated")
                else:
                    r = gw.generate("Reply with the single word OK.", tier=tier, model=model,
                                    reasoning="none" if tier not in ("deep", "research") else "low",
                                    max_output_tokens=256, timeout=60.0, fallback=False,
                                    web_grounding=(tier == "research"))
                    rec.update(status=STATUS_OK if r.text else "empty",
                               detail=f"answered {r.text[:12]!r} as {r.model}")
                    health.record_ok(model, f"probe: {tier}")
            except ProviderError as e:
                st = health.record_error(model, e)
                from core.secret_store import redact
                rec.update(status=st or "error", detail=redact(str(e))[:160])
            except Exception as e:  # pragma: no cover
                from core.secret_store import redact
                rec.update(status="error", detail=f"{type(e).__name__}: {redact(e)[:140]}")
            rec["seconds"] = round(time.time() - t0, 1)
            done[model] = rec
            out.setdefault(tier, []).append({"model": model, **rec})
            log(f"{tier:<13} {model:<42} {rec['status']:<11} {rec.get('detail', '')[:80]}")
            if tier == "image" and rec.get("status") == STATUS_OK:
                break          # one working image model is enough
    return out


def summary(res: dict) -> tuple[list[str], list[str]]:
    """(tiers with at least one working model, tiers with none)."""
    ok = [t for t, rows in res.items() if any(r.get("status") == STATUS_OK for r in rows)]
    return ok, [t for t in res if t not in ok]
