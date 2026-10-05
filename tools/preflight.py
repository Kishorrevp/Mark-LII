"""
tools/preflight.py — JARVIS end-to-end preflight (Part One).

    python tools/preflight.py                 offline checks (no network, ~5 s)
    python tools/preflight.py --live          + real provider calls (≈3–5 free requests),
                                              a Live voice connect, dashboard HTTP checks
    python tools/preflight.py --real-memory   probe the REAL memory file (one namespaced
                                              entry is written, verified, then deleted)

Prints PASS / FAIL / WARN per check, then
    JARVIS PREFLIGHT PASS: N FAIL: N WARN: N
and exits 1 if any critical check failed. A value-free copy of the report is
written to tools/preflight.log (no secrets are ever printed or logged).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:                                   # Windows consoles: print ✓/→ without crashing
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="JARVIS end-to-end preflight")
    ap.add_argument("--live", action="store_true", help="also make real provider / voice / dashboard calls")
    ap.add_argument("--real-memory", action="store_true", help="probe the real memory file (cleans up after)")
    a = ap.parse_args(argv)
    from core import preflight
    lines: list[str] = []

    def log(s: str) -> None:
        lines.append(s)
        print(s, flush=True)

    log(f"JARVIS preflight — {'offline + live' if a.live else 'offline'} checks"
        + (" (real memory file)" if a.real_memory else ""))
    rep = preflight.run(live=a.live, real_memory=a.real_memory, log=log)
    log("")
    if rep.critical_failures:
        log("Critical failures: " + ", ".join(c.name for c in rep.critical_failures))
    log(rep.summary())
    try:
        (ROOT / "tools" / "preflight.log").write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    except OSError:
        pass
    return rep.exit_code()


if __name__ == "__main__":
    sys.exit(main())
