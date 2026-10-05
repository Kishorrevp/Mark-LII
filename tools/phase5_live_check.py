"""
tools/phase5_live_check.py — live verification of Phase 5 on the real machine.

    python tools/phase5_live_check.py            # quick checks (≈ 4 free-tier model calls)
    python tools/phase5_live_check.py --research # + a real research → report run (≈ 2 more calls)
    python tools/phase5_live_check.py --team     # + a real multi-agent website build (≈ 6-8 calls)
    add --skip-quick to run only the research / team parts

Writes a value-free report to tools/phase5_live_check.log. It never prints a
secret, never writes into your Documents (research/team runs use a temporary
folder that is reported at the end), never moves the mouse or presses keys
(the computer agent runs one planning step against a recording "actuator"),
and never changes your real .env (the DPAPI test uses a dummy key in a
temporary folder).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LOG = ROOT / "tools" / "phase5_live_check.log"
_lines: list[str] = []


def log(tag: str, ok, detail="") -> None:
    mark = "PASS" if ok is True else ("FAIL" if ok is False else "INFO")
    line = f"[{mark}] {tag}: {detail}"
    _lines.append(line)
    print(line, flush=True)
    LOG.write_bytes(("\r\n".join(_lines) + "\r\n").encode("utf-8"))


def step(name, timeout: float = 180.0):
    """Each check runs under a watchdog so one hung network call can't stall the report."""
    def deco(fn):
        def run():
            import threading
            t = time.time()

            def body():
                try:
                    fn()
                except Exception as e:
                    from core.secret_store import redact
                    log(name, False, f"{type(e).__name__}: {redact(e)[:200]} | "
                                     + redact(traceback.format_exc().splitlines()[-3])[:160])
            th = threading.Thread(target=body, daemon=True)
            th.start()
            th.join(timeout)
            if th.is_alive():
                log(name, False, f"timed out after {timeout:.0f}s")
            log(name + " time", None, f"{time.time() - t:.1f}s")
        return run
    return deco


@step("environment")
def environment():
    from core import secret_store
    log("python", None, sys.version.split()[0] + " " + sys.platform)
    log("key sources (no values)", None, json.dumps(secret_store.describe()))
    log("vault available", secret_store.vault_available() or None, str(secret_store.vault_available()))


@step("dpapi vault (dummy key, temp folder)")
def dpapi():
    from core import secret_store
    real = secret_store.base_dir()
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_vault_"))
    try:
        secret_store.set_base_dir(tmp)
        dummy = "AIzaSyDUMMY-phase5-vault-test-000000000"
        secret_store.save_secret("GOOGLE_API_KEY", dummy)
        raw = (tmp / ".env").read_text(encoding="utf-8")
        log("dpapi: stored encrypted", "dpapi:" in raw and dummy not in raw)
        log("dpapi: decrypts back", secret_store.get_secret("GOOGLE_API_KEY") == dummy)
        log("dpapi: describe()", None, secret_store.describe()["GOOGLE_API_KEY"])
        blob = secret_store._dpapi(b"x", True)
        log("dpapi: raw round trip", secret_store._dpapi(blob, False) == b"x", f"{len(blob)} byte blob")
    finally:
        secret_store.set_base_dir(None if real == secret_store._base_dir() else real)


@step("model router + gateway tiers (live)")
def tiers():
    from core.ai_gateway import get_gateway
    gw = get_gateway()
    for q in ("What's 25 × 42?", "Write an email.", "Analyze my entire software project.",
              "Look at this screenshot."):
        log("route", None, f"{q!r} → {gw.route(q).as_dict()['tier']}")
    r = gw.generate("What's 25 × 42?", tier="auto")
    log("auto arithmetic is local (no model call)", r.text == "1050" and r.provider == "local-exact", r.text)
    r = gw.generate("Reply with exactly the word: pong", tier="fast", timeout=40)
    log("fast tier", "pong" in r.text.lower(), f"model={r.model} text={r.text[:30]!r}")
    r = gw.generate("In two sentences: why does ice float on water?", tier="deep", timeout=90)
    log("deep tier (high thinking budget)", len(r.text) > 40,
        f"model={r.model} tier={r.meta.get('tier')} degraded={r.meta.get('degraded')} "
        f"output_tokens={r.usage.output_tokens} chars={len(r.text)}")
    info = gw.info()
    log("tiers", None, json.dumps(info.get("tiers")))
    log("free-tier throttle", None, f"rpm={info.get('rpm')} window={info.get('window')} stats={info.get('stats')}")


@step("vision")
def vision():
    from core import vision as VZ
    wins = VZ.list_windows()
    log("list_windows", len(wins) > 0, f"{len(wins)} windows; active present={any(w['active'] for w in wins)}")
    cap = VZ.capture("screen")
    log("capture screen", cap.width > 0, f"{cap.width}x{cap.height} image of a {cap.screen_w}x{cap.screen_h} screen")
    text, _ = VZ.analyze("general", "", cap=cap)
    log("analyze (general)", len(text) > 20, f"{len(text)} chars (content not logged)")


@step("computer agent — one planning step, NO real input")
def computer():
    from core.computer_agent import ComputerAgent
    from core.safety import KillSwitch

    class Recorder:
        def __init__(self):
            self.actions = []

        def do(self, a, xy):
            self.actions.append((a.get("type"), xy))
            return "recorded (not executed)"
    rec = Recorder()
    ag = ComputerAgent(actuator=rec, kill=KillSwitch(), max_steps=1, settle=0.3)
    r = ag.run("Open the Windows Start menu")
    first = r.steps[0] if r.steps else None
    log("planned an action", first is not None,
        f"{first.action.get('type') if first else None} decision={first.decision if first else None} "
        f"result={r.reason}")
    if rec.actions and rec.actions[0][1]:
        x, y = rec.actions[0][1]
        from core import vision as VZ
        c = VZ.capture("screen")
        log("coordinates inside the screen", 0 <= x <= c.screen_w and 0 <= y <= c.screen_h, f"{x},{y}")
    log("nothing was clicked or typed", True, f"{len(rec.actions)} action(s) recorded only")


@step("core wiring (JarvisCore, registry, agents, catalog)")
def wiring():
    from core.jarvis_core import JarvisCore
    core = JarvisCore(persist=False, use_llm_planner=False)
    names = {t.name for t in core.registry.list()}
    need = {"research.collect", "research.analyze", "document.write", "project.analyze", "team.plan",
            "team.review", "screen.analyze", "window.list", "computer.run", "plugin.find", "plugin.install"}
    log("phase 5 tools registered", need <= names, f"{len(names)} tools; missing={sorted(need - names)}")
    log("agents", None, ", ".join(a.name for a in core.agents.all()))
    log("capability search", "currency_converter" in core.capabilities({"need": "convert 100 dollars to rupees"}))
    plan = core.planner.plan("Build me a website for my portfolio")
    log("team plan", [s.agent for s in plan.steps][:6] == ["manager", "research", "ui", "coding", "testing", "review"],
        " → ".join(s.agent for s in plan.steps))
    msg = core.kill.engage("live check")
    log("kill switch", core.kill.engaged(), msg[:80])
    core.kill.release("live check")
    b = core.context.build("continue the project we worked on yesterday")
    log("context engine", bool(b.text), f"intents={b.intents} project={b.project} chars={len(b.text)}")


def _temp_docs():
    from core import research as R
    d = Path(tempfile.mkdtemp(prefix="jarvis_live_"))
    R.documents_dir = lambda: d
    return d


@step("research pipeline (live, temp folder)", timeout=300.0)
def research():
    from core import research as R
    from core.tools.registry import ToolContext
    d = _temp_docs()
    progress = []
    ctx = ToolContext(extras={"progress": progress.append, "should_stop": lambda: False})
    c = R.t_research_collect({"topic": "free open-source vector databases 2026", "depth": "quick"}, ctx)
    log("collect", c.ok, c.message or c.error)
    if not c.ok:
        return
    a = R.t_research_analyze({"path": c.data["path"]}, ctx)
    log("analyze (deep)", a.ok, (a.message or a.error)[:120])
    if a.ok:
        w = R.t_document_write({"content": a.data["analysis"], "format": "docx", "folder": c.data["dir"]}, ctx)
        v = R.verify_document({}, w)
        log("document.write + verify", w.ok and v.ok, f"{w.data.get('name')} — {v.detail}")
    log("workspace", None, str(d))


@step("multi-agent team build (live, temp folder)", timeout=900.0)
def team():
    from core.tools.registry import ToolContext
    from core import team as T
    d = _temp_docs()
    ctx = ToolContext(extras={"progress": lambda m: log("progress", None, m[:90]), "should_stop": lambda: False,
                              "context": lambda q: ""})
    p = T.t_team_plan({"goal": "Build a one-page website for a personal voice assistant project called JARVIS "
                               "with a hero, a features grid and a contact section"}, ctx)
    log("manager", p.ok, p.message or p.error)
    if not p.ok:
        return
    for fn in (T.t_team_research, T.t_team_design, T.t_team_implement, T.t_team_test, T.t_team_review):
        r = fn({"dir": p.data["dir"]}, ctx)
        log(fn.__name__, r.ok, (r.message or r.error)[:160])
        if not r.ok:
            return
    v = T.verify_review({}, r)
    log("team verdict", v.ok, v.detail)
    log("site", None, r.data.get("entry"))


if __name__ == "__main__":
    LOG.write_bytes(b"")
    log("started", None, time.strftime("%Y-%m-%d %H:%M:%S"))
    if "--skip-quick" not in sys.argv:
        for fn in (environment, dpapi, tiers, vision, computer, wiring):
            fn()
    if "--research" in sys.argv:
        research()
    if "--team" in sys.argv:
        team()
    fails = sum(1 for l in _lines if l.startswith("[FAIL]"))
    log("finished", fails == 0, f"{fails} failure(s)")
