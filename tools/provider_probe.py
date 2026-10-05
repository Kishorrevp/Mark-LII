"""
tools/provider_probe.py — which models each of your keys can actually use.

    python tools/provider_probe.py              # Google, NVIDIA (every key), Anthropic
    python tools/provider_probe.py --nvidia     # only NVIDIA

NVIDIA: lists the catalogue with the general key, then calls every model you
configured (config/models.json + per-model keys NVIDIA_API_KEY__<MODEL> in
.env) with ITS key — a one-word chat (or a tiny embedding for embed models)
— and reports ok / error + latency. Google and Anthropic get one minimal call
per configured model. Writes tools/provider_probe.json (model → status) and
records every result in JARVIS's model-health file (memory/model_health.json),
so the gateway rests models your keys cannot use (no credit, unauthorised,
missing) instead of re-trying them on every request. Never prints a key.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NV_BASE = "https://integrate.api.nvidia.com/v1"


def _why(text: str) -> str:
    m = re.search(r'"(?:detail|message|title)"\s*:\s*"([^"]{1,140})', text or "")
    return (m.group(1) if m else (text or "")[:140]).replace("\n", " ")


def probe_nvidia(log) -> dict:
    import requests
    from core.providers.nvidia_keys import key_for, configured_models
    from core.secret_store import redact
    out: dict = {}
    general = key_for("")
    if not general:
        log("NVIDIA: no NVIDIA_API_KEY configured")
        return out
    catalogue: list[str] = []
    try:
        r = requests.get(f"{NV_BASE}/models", headers={"authorization": f"Bearer {general}"}, timeout=25)
        if r.status_code < 400:
            catalogue = sorted(m.get("id") for m in (r.json().get("data") or []) if m.get("id"))
        log(f"NVIDIA catalogue: {len(catalogue)} models visible to the general key (HTTP {r.status_code})")
    except Exception as e:
        log(f"NVIDIA catalogue: unavailable ({type(e).__name__})")
    for want in configured_models():
        mid = want
        if catalogue and want not in catalogue:
            tail = want.split("/")[-1]
            hits = [c for c in catalogue if c.split("/")[-1] == tail]
            if hits:
                mid = hits[0]
        key = key_for(mid)
        embed = "embed" in mid
        t0 = time.time()
        try:
            if embed:
                r = requests.post(f"{NV_BASE}/embeddings", headers={"authorization": f"Bearer {key}"},
                                  json={"model": mid, "input": ["hello"], "input_type": "query"}, timeout=40)
            else:
                r = requests.post(f"{NV_BASE}/chat/completions", headers={"authorization": f"Bearer {key}"},
                                  json={"model": mid, "max_tokens": 16,
                                        "messages": [{"role": "user", "content": "Reply with the single word OK."}]},
                                  timeout=60)
            sec = time.time() - t0
            if r.status_code < 400:
                txt = ""
                if not embed:
                    try:
                        txt = (r.json()["choices"][0]["message"].get("content") or "").strip()[:20]
                    except Exception:
                        txt = ""
                out[mid] = {"status": "ok", "seconds": round(sec, 2), "reply": txt, "key": "per-model"
                            if key != general else "general", "listed": (mid in catalogue) if catalogue else None}
            else:
                out[mid] = {"status": f"http_{r.status_code}", "seconds": round(sec, 2),
                            "detail": redact(_why(r.text)), "listed": (mid in catalogue) if catalogue else None}
        except Exception as e:
            out[mid] = {"status": "error", "detail": f"{type(e).__name__}", "seconds": round(time.time() - t0, 2)}
        row = out[mid]
        log(f"  {mid:<48} {row['status']:<9} {row.get('seconds', 0):>6.2f}s  {row.get('reply') or row.get('detail', '')}")
    return out


def probe_google(log, models: list[str]) -> dict:
    out: dict = {}
    try:
        from google import genai
        from core.secret_store import get_secret, redact
        client = genai.Client(api_key=get_secret("GOOGLE_API_KEY"))
    except Exception as e:
        log(f"Google: unavailable ({type(e).__name__})")
        return out
    for m in models:
        t0 = time.time()
        try:
            r = client.models.generate_content(model=m, contents="Reply with the single word OK.")
            out[m] = {"status": "ok", "seconds": round(time.time() - t0, 2), "reply": (r.text or "").strip()[:20]}
        except Exception as e:
            s = str(e)
            st = ("no_quota" if "limit: 0" in s or "limit:0" in s else "http_404" if "404" in s or "NOT_FOUND" in s
                  else "http_429" if "429" in s else "http_503" if "503" in s else "error")
            out[m] = {"status": st, "seconds": round(time.time() - t0, 2), "detail": redact(s)[:120]}
        row = out[m]
        log(f"  {m:<48} {row['status']:<9} {row['seconds']:>6.2f}s  {row.get('reply') or row.get('detail', '')[:90]}")
    return out


def probe_anthropic(log, models: list[str]) -> dict:
    import requests
    from core.secret_store import get_secret, redact
    key = get_secret("ANTHROPIC_API_KEY")
    out: dict = {}
    if not key:
        log("Anthropic: no key")
        return out
    for m in models:
        t0 = time.time()
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=40,
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json={"model": m, "max_tokens": 16,
                                    "messages": [{"role": "user", "content": "Reply with the single word OK."}]})
            st = "ok" if r.status_code < 400 else ("no_credit" if "credit balance" in r.text else f"http_{r.status_code}")
            out[m] = {"status": st, "seconds": round(time.time() - t0, 2), "detail": redact(_why(r.text))[:110]
                      if r.status_code >= 400 else ""}
        except Exception as e:
            out[m] = {"status": "error", "detail": type(e).__name__}
        log(f"  {m:<48} {out[m]['status']:<9} {out[m].get('seconds', 0):>6.2f}s  {out[m].get('detail', '')}")
    return out


def _record_health(res: dict, log) -> None:
    """Feed the results to core.model_health (same classifier the gateway uses)."""
    try:
        from core.model_health import get_health
        from core.providers.base import ProviderError
        h = get_health()
    except Exception as e:
        log(f"(model health not updated: {type(e).__name__})")
        return
    rested = []
    for prov in ("nvidia", "google", "anthropic"):
        for m, r in (res.get(prov) or {}).items():
            st = str(r.get("status") or "")
            if st == "ok":
                h.record_ok(m, note="provider_probe")
                continue
            detail = str(r.get("detail") or "")
            if st == "no_credit":
                detail = "credit balance is too low " + detail
            code = int(st[5:]) if st.startswith("http_") and st[5:].isdigit() else None
            if st == "no_quota" and "limit: 0" not in detail:
                detail += " limit: 0"
            got = h.record_error(m, ProviderError(f"HTTP {code or ''} {detail}".strip(), status=code))
            if got:
                rested.append(f"{m} ({got})")
    log("model health: " + (("resting → " + ", ".join(rested)) if rested else "nothing to rest"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nvidia", action="store_true")
    a = ap.parse_args(argv)
    lines: list[str] = []

    def log(s):
        lines.append(s)
        print(s, flush=True)
    from core import config as C
    res = {"when": time.strftime("%Y-%m-%d %H:%M"), "nvidia": {}, "google": {}, "anthropic": {}}
    log("== NVIDIA")
    res["nvidia"] = probe_nvidia(log)
    if not a.nvidia:
        g = sorted({v for t, v in ((t, C.model_for(t, "google")) for t in ("fast", "normal", "deep", "vision",
                                                                         "research", "coding"))
                    if v and not v.startswith("@")} | set((C.load("models").get("providers", {}).get("google", {})
                                                          .get("tiers", {}).get("normal", {}) or {})
                                                         .get("alternates") or []))
        g = sorted(set(g) | {"gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                             "gemini-3.1-pro-preview", "gemini-flash-lite-latest", "gemini-3.7-flash"})
        log("== Google")
        res["google"] = probe_google(log, g)
        log("== Anthropic")
        res["anthropic"] = probe_anthropic(log, ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"])
    _record_health(res, log)
    try:
        (ROOT / "tools" / "provider_probe.json").write_bytes(json.dumps(res, indent=1).replace(
            "\n", "\r\n").encode("utf-8"))
        (ROOT / "tools" / "provider_probe.log").write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
