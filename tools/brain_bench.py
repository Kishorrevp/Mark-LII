"""
tools/brain_bench.py — how fast each text brain answers on YOUR keys, through
JARVIS's real provider code, and whether switching between Google, NVIDIA and
Anthropic actually works.

    python tools/brain_bench.py                 # speed + switching
    python tools/brain_bench.py --speed         # only the per-model speed table
    python tools/brain_bench.py --switch        # only the provider-switching test

Speed: every candidate model answers the same short question twice (Google
models once with their default thinking and once with effort=low), through
core/providers (so the Phase 7 timeout and per-model NVIDIA keys are what is
being measured). Switching: the real gateway answers through its configured
chain (Google first), then pinned to an NVIDIA model, then pinned to an
Anthropic model, then reset — and reports which provider/model REALLY
answered each time (from the response, not from what was asked).

Writes tools/brain_bench.log and tools/brain_bench.json. Never prints a key.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

QUESTION = "In two short sentences: what is the difference between RAM and storage?"
GOOGLE = ["gemini-flash-latest", "gemini-3.6-flash", "gemini-3-flash-preview", "gemini-3.5-flash",
          "gemini-3.8-flash", "gemini-flash-lite-latest", "gemini-3.5-flash-lite"]
NVIDIA = ["poolside/laguna-xs-2.1", "nvidia/nemotron-3-ultra-550b-a55b", "deepseek-ai/deepseek-v4.1-flash",
          "moonshotai/kimi-k3", "nvidia/nemotron-3.5-lightning-30b-a3b"]
TIMEOUT = 30.0


def _redact(s: str) -> str:
    from core.secret_store import redact
    return redact(str(s)).replace("\n", " ")[:110]


def _one(p, model, effort):
    from core.providers.base import AIRequest, Message, ReasoningEffort
    req = AIRequest(messages=[Message.user(QUESTION)], model=model, timeout=TIMEOUT, max_output_tokens=400,
                    reasoning=ReasoningEffort.parse(effort) if effort else None)
    t0 = time.monotonic()
    try:
        r = p.generate(req)
        return {"ok": bool(r.text), "s": round(time.monotonic() - t0, 2), "chars": len(r.text or "")}
    except Exception as e:
        return {"ok": False, "s": round(time.monotonic() - t0, 2), "err": _redact(f"{type(e).__name__}: {e}")}


def speed(log) -> list:
    from core.secret_store import get_secret
    from core.providers.google_provider import GoogleProvider
    from core.providers.openai_provider import OpenAIProvider
    from core.providers import nvidia_keys
    rows = []
    plans = []
    if get_secret("GOOGLE_API_KEY"):
        g = GoogleProvider(api_key=get_secret("GOOGLE_API_KEY"))
        plans += [("google", g, m, None) for m in GOOGLE] + [("google", g, m, "low") for m in GOOGLE]
    if nvidia_keys.has_any():
        from core.ai_gateway import NVIDIA_BASE_URL
        n = OpenAIProvider(api_key=nvidia_keys.key_for(""), model=NVIDIA[0], base_url=NVIDIA_BASE_URL,
                           name="nvidia", keys_for=nvidia_keys.keys_for)
        plans += [("nvidia", n, m, None) for m in NVIDIA]
    log(f"{'provider':<8} {'model':<42} {'effort':<7} {'run 1':>7} {'run 2':>7}  note")
    for prov, p, m, eff in plans:
        a = _one(p, m, eff)
        b = _one(p, m, eff) if a["ok"] else {"ok": False, "s": 0.0}
        ok = [x["s"] for x in (a, b) if x["ok"]]
        row = {"provider": prov, "model": m, "effort": eff or "default", "runs": [a, b],
               "median": round(statistics.median(ok), 2) if ok else None}
        rows.append(row)
        note = a.get("err") or b.get("err") or ""
        log(f"{prov:<8} {m:<42} {(eff or 'def'):<7} {a['s']:>6.2f}{'' if a['ok'] else '!'} "
            f"{b['s']:>6.2f}{'' if b['ok'] else '!'}  {note}")
    return rows


def switch(log) -> list:
    from core.ai_gateway import get_gateway
    gw = get_gateway()
    steps = [("auto (configured chain)", None, None),
             ("pinned NVIDIA", "nvidia/nemotron-3-ultra-550b-a55b", "nvidia"),
             ("pinned NVIDIA fast", "poolside/laguna-xs-2.1", "nvidia"),
             ("pinned Anthropic", "claude-haiku-4-5", "anthropic"),
             ("reset → auto", None, None)]
    out = []
    log(f"configured: provider={gw.config.provider} fallbacks={','.join(gw.config.fallbacks)}")
    for label, mid, prov in steps:
        if mid:
            gw.set_runtime_model(mid, prov)
        else:
            gw.clear_runtime_model()
        t0 = time.monotonic()
        try:
            r = gw.generate("Reply with one short sentence confirming you are working.", tier="normal",
                            timeout=TIMEOUT)
            res = {"step": label, "ok": True, "provider": r.provider, "model": r.model,
                   "fallback_provider": (r.meta or {}).get("fallback_provider"),
                   "s": round(time.monotonic() - t0, 2)}
            log(f"  {label:<26} OK   answered by {r.provider}:{r.model}  {res['s']:.2f}s"
                + (f"  (fell back to {res['fallback_provider']})" if res["fallback_provider"] else ""))
        except Exception as e:
            res = {"step": label, "ok": False, "s": round(time.monotonic() - t0, 2),
                   "error": _redact(f"{type(e).__name__}: {e}")}
            log(f"  {label:<26} FAIL {res['s']:.2f}s  {res['error']}")
        out.append(res)
    gw.clear_runtime_model()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", action="store_true")
    ap.add_argument("--switch", action="store_true")
    a = ap.parse_args(argv)
    both = not (a.speed or a.switch)
    lines: list[str] = []

    def log(s):
        lines.append(s)
        print(s, flush=True)
    log(f"JARVIS brain bench — {time.strftime('%Y-%m-%d %H:%M')}")
    res = {}
    if both or a.speed:
        log("== speed (seconds; ! = failed)")
        res["speed"] = speed(log)
    if both or a.switch:
        log("== switching")
        res["switch"] = switch(log)
    try:
        (ROOT / "tools" / "brain_bench.log").write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
        (ROOT / "tools" / "brain_bench.json").write_bytes(json.dumps(res, indent=1).replace(
            "\n", "\r\n").encode("utf-8"))
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
