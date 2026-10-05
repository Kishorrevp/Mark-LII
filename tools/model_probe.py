"""
tools/model_probe.py — which configured models actually work on YOUR key?

    python tools/model_probe.py              # every tier incl. one tiny image generation
    python tools/model_probe.py --no-image   # skip the image test
    python tools/model_probe.py --list       # also print every model id the key can see

For each tier in config/models.json it makes the smallest real request
(≈ one free-tier request per distinct model), records the result in
memory/model_health.json (so JARVIS skips models that are missing or have no
free quota), and writes a report to tools/model_probe.log.

The report contains model ids, statuses and short redacted reasons only —
never the key, never a prompt beyond "Reply with the single word OK".
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LOG = ROOT / "tools" / "model_probe.log"
_lines: list[str] = []


def log(line: str) -> None:
    _lines.append(line)
    print(line, flush=True)
    LOG.write_bytes(("\r\n".join(_lines) + "\r\n").encode("utf-8"))


def main() -> int:
    from core import config as C
    from core.ai_gateway import get_gateway
    from core.model_health import probe, summary
    from core.secret_store import redact
    log(f"model probe {time.strftime('%Y-%m-%d %H:%M:%S')}")
    for p in C.problems("models"):
        log(f"config: {p}")
    gw = get_gateway()
    try:
        info = gw.info()
    except Exception as e:
        log(f"gateway error: {redact(e)[:200]}")
        return 1
    log(f"provider={info.get('provider')} rpm={info.get('rpm')}")
    for tier, model in (info.get("tiers") or {}).items():
        log(f"  tier {tier:<13} → {model}")
    if "--list" in sys.argv:
        names = gw.list_models()
        log(f"{len(names)} models visible to this key:")
        for n in sorted(names):
            log(f"    {n}")
    log("probing …")
    res = probe(gw, image="--no-image" not in sys.argv, log=lambda s: log("  " + s))
    ok, bad = summary(res)
    log(f"working tiers (at least one model answers): {', '.join(ok) or 'none'}")
    if bad:
        log(f"NOT working on this key: {', '.join(bad)}")
    log("models marked exhausted / no_quota / unavailable are skipped automatically until they recover "
        "(memory/model_health.json)")
    log(f"live voice model (not probed here — the running assistant proves it): {C.live_model()}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
