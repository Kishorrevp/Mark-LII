"""
core/mission.py — Mission Control: one read-only snapshot of everything JARVIS
is doing, for the Settings → MISSION CONTROL panel (and the phone dashboard).

    Overview     brain (provider · model), voice model, voice latency (median),
                 events today, open tasks, trusted actions, references, learning
    Agents       each agent lane: actions logged (today / 7 days) + a 7-day bar
    Tasks        a kanban — queued · running · waiting · done · failed
    Schedule     saved workflows (routines) and learned habits with their time
    Content      files JARVIS made recently (projects, research, drawings)
    Logs         the latest audit events (already redacted at write time)
    Permissions  the "Allow always" list, each revocable
    Learning     how the user likes things + habits, each forgettable

Nothing here acts — the panel's buttons call the existing, permission-checked
paths (permissions.remove_trusted, learning.forget, references.forget,
on_text_command). Dependency-free; tested by real execution.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

KANBAN = (("queued", ("QUEUED", "PLANNING", "CREATED", "PENDING")),
          ("running", ("RUNNING", "VERIFYING", "RECOVERING", "EXECUTING")),
          ("waiting", ("WAITING_FOR_PERMISSION", "WAITING_FOR_INPUT", "PAUSED")),
          ("done", ("COMPLETED",)),
          ("failed", ("FAILED", "CANCELLED")))


def _lane(state: str) -> str:
    s = str(state or "").upper()
    for name, states in KANBAN:
        if s in states:
            return name
    return "queued"


def _recent_files(roots: list[Path], n: int = 14) -> list[dict]:
    out = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower() in (".html", ".md", ".docx", ".pdf", ".svg", ".png", ".txt",
                                                        ".json", ".css", ".js", ".py", ".csv", ".xlsx", ".pptx"):
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    out.append({"path": str(p), "name": p.name, "folder": p.parent.name, "bytes": st.st_size,
                                "mtime": st.st_mtime})
        except Exception:
            continue
    out.sort(key=lambda f: -f["mtime"])
    return out[:n]


def snapshot(core: Any, latency: Any = None, live_model: str = "", now: float | None = None) -> dict:
    """Everything the panel shows. `core` is the JarvisCore; each section is
    best-effort (a broken source shows as empty, never as an exception)."""
    now = now or time.time()
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    snap: dict = {"at": datetime.fromtimestamp(now).strftime("%H:%M:%S")}

    # ── overview ─────────────────────────────────────────────────────────────
    ov: dict = {}
    try:
        from core.ai_gateway import get_gateway
        b = get_gateway().active_brain()
        ov["brain"] = f"{b.get('model') or '?'} · {b.get('provider')}" + (" · runtime" if b.get("runtime") else "")
    except Exception:
        ov["brain"] = "unavailable"
    ov["voice_model"] = str(live_model or "").replace("models/", "")
    if latency is not None:
        try:
            ov["latency"] = latency.summary()
        except Exception:
            ov["latency"] = {}
    events = []
    try:
        events = list(core.audit.recent(300))
    except Exception:
        pass
    ov["events_today"] = sum(1 for e in events if str(e.get("ts", "")).startswith(today))
    ov["events_logged"] = len(events)
    tv = {}
    try:
        tv = core.task_view()
    except Exception:
        pass
    ov["open_tasks"] = len(tv.get("active") or [])
    try:
        from core.permissions import trusted_list
        trusted = trusted_list()
    except Exception:
        trusted = []
    ov["trusted"] = len(trusted)
    refs = []
    try:
        from core import references
        refs = references.all_refs(include_archived=True)
    except Exception:
        pass
    ov["references"] = len(refs)
    learner = getattr(core, "learner", None)
    prefs, habits = [], []
    if learner is not None:
        try:
            prefs, habits = learner.preferences(), learner.habits()
        except Exception:
            pass
    ov["learned"] = len(prefs) + len(habits)
    ov["learning_paused"] = bool(getattr(learner, "paused", False)) if learner is not None else False
    snap["overview"] = ov

    # ── agents ──────────────────────────────────────────────────────────────
    agents = []
    days = [(datetime.fromtimestamp(now) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    owner = {}
    try:
        reg_agents = core.agents.all()
    except Exception:
        reg_agents = []
    for e in events:
        tool = e.get("tool") or ""
        ag = e.get("agent") or ""
        if not ag and tool:
            try:
                ag = owner.get(tool) or core.agents.owner_of(tool).name
                owner[tool] = ag
            except Exception:
                ag = ""
        if ag:
            e["_agent"] = ag
    for a in reg_agents:
        mine = [e for e in events if e.get("_agent") == a.name]
        per_day = Counter(str(e.get("ts", ""))[:10] for e in mine)
        agents.append({"name": a.name, "title": getattr(a, "title", a.name), "today": per_day.get(today, 0),
                       "total": len(mine), "bars": [per_day.get(d, 0) for d in days],
                       "description": getattr(a, "description", "")[:90]})
    snap["agents"] = agents

    # ── tasks (kanban) ──────────────────────────────────────────────────────
    lanes: dict = {name: [] for name, _ in KANBAN}
    try:
        for t in core.store.all()[-40:]:
            lanes[_lane(getattr(t.state, "value", t.state))].append(
                {"id": t.id, "request": t.request[:90], "state": getattr(t.state, "value", str(t.state)),
                 "progress": getattr(t, "progress", "")})
    except Exception:
        pass
    for k in lanes:
        lanes[k] = list(reversed(lanes[k]))[:12]
    snap["tasks"] = lanes

    # ── schedule ────────────────────────────────────────────────────────────
    sched = {"workflows": [], "habits": habits[:8]}
    try:
        for w in core.workflows.list()[:12]:
            sched["workflows"].append({"name": w.get("name"), "steps": len(w.get("steps") or []),
                                       "runs": w.get("runs", 0)})
    except Exception:
        pass
    snap["schedule"] = sched

    # ── content ─────────────────────────────────────────────────────────────
    try:
        from core import research as R
        roots = [R.documents_dir() / "JARVIS Projects", R.documents_dir() / "JARVIS Research"]
    except Exception:
        roots = []
    snap["content"] = _recent_files(roots)

    # ── logs ────────────────────────────────────────────────────────────────
    logs = []
    for e in events[-40:]:
        bits = [str(e.get(k)) for k in ("tool", "action", "decision", "result", "detail") if e.get(k)]
        logs.append({"ts": str(e.get("ts", ""))[11:19], "event": e.get("event", ""),
                     "text": " · ".join(bits)[:140]})
    snap["logs"] = list(reversed(logs))

    # ── permissions / learning / references ──────────────────────────────────
    snap["permissions"] = {"trusted": trusted}
    snap["learning"] = {"preferences": prefs[:30], "habits": habits, "paused": ov["learning_paused"]}
    snap["references"] = [{"id": r.id, "title": r.title, "kind": r.kind, "active": r.active} for r in refs[:20]]

    snap["kg_data"] = {}
    return snap
