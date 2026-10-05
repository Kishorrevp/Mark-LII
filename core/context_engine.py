"""
core/context_engine.py — the minimal relevant context for one request.

                 ┌─ conversation (short-term turns)      ─┐
                 ├─ semantic facts (who the user is)      │
    request ───► ├─ episodic memory (what happened, when) ├─► score ─► pack into a
                 ├─ procedural (saved workflows)          │   char budget ─► ContextBundle
                 ├─ tasks (running / waiting / failed)    │
                 ├─ audit (previous actions)              │
                 ├─ project registry (paths, activity)    │
                 └─ computer state (time, device, window) ─┘

The model never gets "everything". Each source contributes candidate lines
with a relevance score (keyword overlap × recency × a prior for the source,
boosted by what the request is about); lines are packed best-first until the
budget (default 1,400 characters ≈ 350 tokens) is used. Sources that the
request does not touch contribute nothing — e.g. the active window title is
only included when the request refers to "this", "here", "my screen",
"what am I doing"; audit history only when it asks what was done.

    "Continue the project we worked on yesterday"
        intents   continuation + temporal(yesterday) + project
        project   episodic.last_project(yesterday) → "JARVIS"
        output    project path + last activity, yesterday's episodes on it,
                  open tasks, the last conversation turns

Two entry points:
    build(query)   → ContextBundle (text + items + snapshot) — used by the
                     recall_context / think tools and the cognitive core
    live_block()   → a short block appended to the voice session's system
                     prompt at connect: time, device, the user's active
                     projects, the last session, running tasks.

Part One: every item can carry a `ref` (source id, type, path, timestamp) so
the answer's sources are tracked (core/knowledge.SourceTracker), and the
optional `knowledge` base adds the user's own notes/documents as "doc" items.

Dependency-free (the window probe uses ctypes on Windows only and degrades
to nothing elsewhere).
"""
from __future__ import annotations

import os
import platform
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from memory.episodic import _words, fmt_when, resolve_period

_INTENTS = {
    "continuation": re.compile(r"\b(continue|resume|pick up|carry on|where (?:were|did) we|left off|"
                               r"keep going|go on with|finish (?:the|that|what))\b", re.I),
    "self": re.compile(r"\b(about me|who am i|my name|what do you know about me|remember about me|"
                       r"my (?:preferences|goals|family|friends))\b", re.I),
    "project": re.compile(r"\b(project|repo(?:sitory)?|codebase|app|website|site|build|code)\b", re.I),
    "current": re.compile(r"\b(this|here|current(?:ly)?|right now|on (?:my|the) screen|what am i doing|"
                          r"active window|open (?:window|app))\b", re.I),
    "history": re.compile(r"\b(what (?:did|have) you (?:do|done)|previous(?:ly)?|last action|undo|"
                          r"what happened|did you)\b", re.I),
    "tasks": re.compile(r"\b(task|progress|status|what are you doing|running|still working|done yet)\b", re.I),
    "routine": re.compile(r"\b(workflow|routine|usual|as always|every (?:day|morning|evening|night))\b", re.I),
    "anaphora": re.compile(r"\b(it|that|those|them|again|same)\b", re.I),
}
_PRIOR = {"now": 0.6, "user": 1.2, "project": 2.0, "episode": 1.4, "workflow": 1.1, "task": 1.6,
          "audit": 1.2, "turn": 1.0, "window": 1.5, "fact": 1.0, "apps": 0.5, "doc": 1.3}
# identity.facts() section → long_term.json category (source ids match core/knowledge)
_SECTION_CAT = {"user": "identity", "preferences": "preferences", "people": "relationships",
                "goals": "wishes", "notes": "notes"}


@dataclass
class ContextItem:
    source: str
    text: str
    score: float
    ref: Optional[dict] = None     # {"id", "type", "title", "path", "timestamp"} when citable


@dataclass
class ContextBundle:
    query: str
    intents: list[str]
    items: list[ContextItem]
    snapshot: dict
    text: str
    project: Optional[str] = None
    period: Optional[str] = None
    dropped: int = 0

    def sources(self) -> dict:
        out: dict[str, int] = {}
        for it in self.items:
            out[it.source] = out.get(it.source, 0) + 1
        return out


def active_window() -> dict:
    """Title + process of the foreground window (Windows). {} elsewhere."""
    if os.name != "nt":
        return {}
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {}
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc = ""
        try:
            import psutil
            proc = psutil.Process(pid.value).name()
        except Exception:
            pass
        return {"title": buf.value[:160], "process": proc, "pid": int(pid.value)}
    except Exception:
        return {}


def _part_of_day(h: int) -> str:
    return ("night" if h < 5 else "morning" if h < 12 else "afternoon" if h < 17
            else "evening" if h < 21 else "night")


class ContextEngine:
    def __init__(self, *, memory_hub=None, episodic=None, identity=None, task_store=None,
                 workflows=None, audit=None, clock: Callable[[], datetime] = datetime.now,
                 window_probe: Callable[[], dict] = active_window, language: Callable[[], str] | None = None,
                 knowledge=None):
        self.knowledge = knowledge
        self.hub = memory_hub
        self.episodic = episodic
        self.identity = identity
        self.tasks = task_store
        self.workflows = workflows
        self.audit = audit
        self._clock = clock
        self._window = window_probe
        self._language = language

    # -- computer / time state ---------------------------------------------------
    def snapshot(self, include_window: bool = False) -> dict:
        now = self._clock()
        snap: dict[str, Any] = {
            "time": now.strftime("%A %d %B %Y, %I:%M %p").replace(" 0", " "),
            "part_of_day": _part_of_day(now.hour),
            "device": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}".strip(),
        }
        try:
            import psutil
            b = psutil.sensors_battery()
            if b is not None:
                snap["battery"] = f"{int(b.percent)}%{' charging' if b.power_plugged else ''}"
        except Exception:
            pass
        if include_window:
            w = {}
            try:
                w = self._window() or {}
            except Exception:
                w = {}
            if w:
                snap["active_window"] = w
        if self.tasks is not None:
            try:
                snap["active_tasks"] = [{"id": t.id, "request": t.request[:80], "state": t.state.value}
                                        for t in self.tasks.active()]
            except Exception:
                pass
        if self._language:
            try:
                snap["language"] = self._language()
            except Exception:
                pass
        return snap

    # -- intents -------------------------------------------------------------------
    def intents(self, query: str) -> tuple[list[str], Optional[tuple]]:
        found = [k for k, rx in _INTENTS.items() if rx.search(query or "")]
        period = resolve_period(query or "", now=self._clock())
        if period:
            found.append("temporal")
        return found, period

    # -- main entry ------------------------------------------------------------------
    def build(self, query: str, budget: int = 1400, *, include_window: Optional[bool] = None) -> ContextBundle:
        q = str(query or "")
        intents, period = self.intents(q)
        qwords = set(_words(q))
        want_window = include_window if include_window is not None else ("current" in intents)
        snap = self.snapshot(include_window=want_window)
        items: list[ContextItem] = []

        def add(src, text, rel, ref=None):
            items.append(ContextItem(src, text, round(rel * _PRIOR.get(src, 1.0), 3), ref))

        add("now", f"Now: {snap['time']} ({snap['part_of_day']}) on {snap['device']}"
                   + (f", battery {snap['battery']}" if snap.get("battery") else ""),
            1.5 if "temporal" in intents else 1.0)

        # ── project resolution ─────────────────────────────────────────────
        project = None
        if self.identity is not None:
            try:
                project = self.identity.match_project(q)
            except Exception:
                project = None
        if project is None and self.episodic is not None and \
                ({"continuation", "project"} & set(intents) or period):
            try:
                project = self.episodic.last_project(period[:2] if period else None)
            except Exception:
                project = None
        if project is None and self.identity is not None and "continuation" in intents:
            try:
                pr = sorted(self.identity.projects().items(), key=lambda kv: kv[1].get("last_active", ""),
                            reverse=True)
                project = pr[0][0] if pr else None
            except Exception:
                pass
        if project and self.identity is not None:
            p = (self.identity.projects() or {}).get(project, {})
            bits = [f"Project {project}"]
            if p.get("path"):
                bits.append(f"folder {p['path']}")
            if p.get("description"):
                bits.append(p["description"][:140])
            if p.get("stack"):
                bits.append("stack " + ", ".join(p["stack"][:6]))
            if p.get("last_active"):
                bits.append("last active " + fmt_when(p["last_active"], self._clock()))
            add("project", " · ".join(bits), 3.0,
                {"id": f"id:projects/{project}", "type": "identity", "title": f"project {project}",
                 "path": str(p.get("path") or ""), "timestamp": str(p.get("last_active") or "")})

        # ── semantic facts ────────────────────────────────────────────────
        if self.identity is not None:
            try:
                name = self.identity.user_name()
                if name:
                    add("user", f"User: {name}", 1.0 + (2.0 if "self" in intents else 0))
                for section, key, value in self.identity.facts():
                    if section in ("projects",) or (section == "user" and key == "name"):
                        continue
                    words = set(_words(f"{key} {value}"))
                    ov = len(qwords & words)
                    if ov == 0 and not ("self" in intents and section in ("user", "preferences", "goals")):
                        continue
                    rel = ov * 1.5 + (0.8 if "self" in intents else 0)
                    cat = _SECTION_CAT.get(section)
                    rid = f"mem:{cat}/{key}" if cat else f"id:{section}/{key}"
                    add("fact", f"{section}/{key.replace('_', ' ')}: {value[:160]}", rel,
                        {"id": rid, "type": "memory" if cat else "identity",
                         "title": f"{section}/{key.replace('_', ' ')}", "path": "", "timestamp": ""})
                if "current" in intents or "routine" in intents:
                    apps = self.identity.frequent_apps(5)
                    if apps:
                        add("apps", "Usual apps: " + ", ".join(apps), 1.0)
            except Exception:
                pass

        # ── episodic ───────────────────────────────────────────────────────
        if self.episodic is not None:
            try:
                hits = self.episodic.search(q, limit=6, period=period[:2] if period else None,
                                            project=project if (project and "continuation" in intents) else None)
                if not hits and project:
                    hits = self.episodic.search("", limit=4, project=project)
                for sc, d in hits:
                    when = fmt_when(d.get("ts", ""), self._clock())
                    line = f"{when} [{d.get('kind')}] {d.get('title')}"
                    if d.get("outcome"):
                        line += f" ({d['outcome']})"
                    if d.get("summary"):
                        line += f" — {d['summary'][:160]}"
                    paths = (d.get("refs") or {}).get("paths") or []
                    if paths:
                        line += f" · {paths[0]}"
                    add("episode", line, 1.0 + sc + (1.0 if period else 0),
                        {"id": str(d.get("id") or ""), "type": "episode", "title": str(d.get("title", ""))[:80],
                         "path": paths[0] if paths else "", "timestamp": str(d.get("ts", ""))})
            except Exception:
                pass

        # ── procedural ────────────────────────────────────────────────────
        if self.workflows is not None:
            try:
                for w in self.workflows.list():
                    words = set(_words(" ".join([w.get("name", "")] + list(w.get("triggers", [])))))
                    ov = len(qwords & words)
                    if ov or "routine" in intents:
                        steps = " → ".join(s.get("tool", "?") for s in w.get("steps", [])[:6])
                        add("workflow", f"Saved workflow '{w.get('name')}': {steps}", ov * 1.5 + 0.5)
            except Exception:
                pass

        # ── tasks ─────────────────────────────────────────────────────────
        if self.tasks is not None:
            try:
                for t in self.tasks.active():
                    add("task", f"Running task {t.id}: {t.request[:90]} — {t.state.value}"
                                + (f", waiting on: {t.pending_question[:80]}" if t.pending_question else ""),
                        2.0 + (1.5 if "tasks" in intents else 0),
                        {"id": f"task:{t.id}", "type": "task", "title": f"task {t.id}", "path": "",
                         "timestamp": str(getattr(t, "updated_at", "") or getattr(t, "created_at", ""))})
                if "tasks" in intents or "continuation" in intents:
                    for t in self.tasks.all()[-4:]:
                        if t.state.terminal and (t.state.value in ("FAILED", "CANCELLED")
                                                 or "tasks" in intents):
                            add("task", f"Earlier task {t.id}: {t.request[:80]} — {t.state.value}"
                                        + (f" ({t.error[:80]})" if t.error else ""), 1.2,
                                {"id": f"task:{t.id}", "type": "task", "title": f"task {t.id}", "path": "",
                                 "timestamp": str(getattr(t, "updated_at", "") or "")})
            except Exception:
                pass

        # ── audit ────────────────────────────────────────────────────────
        if self.audit is not None and "history" in intents:
            try:
                s = self.audit.summarize_recent(6)
                for line in s.splitlines()[1:7]:
                    add("audit", "Action " + line.strip(), 1.6)
            except Exception:
                pass

        # ── conversation ─────────────────────────────────────────────────
        if self.hub is not None and ({"continuation", "anaphora"} & set(intents)):
            try:
                for i, turn in enumerate(self.hub.short_term.recent(6)):
                    add("turn", f"{turn['role']}: {turn['text'][:140]}", 0.8 + i * 0.1,
                        {"id": f"conv:{turn.get('ts', i)}", "type": "conversation",
                         "title": f"{turn['role']} said", "path": "", "timestamp": str(turn.get("ts", ""))})
            except Exception:
                pass

        # ── personal knowledge (the user's notes / documents) ───────────
        if self.knowledge is not None and qwords:
            try:
                for s in self.knowledge.search(q, layers=("personal",), limit=3, record=False,
                                               period=period):
                    add("doc", f"Note {s.title}: {s.excerpt[:200]}", 1.0 + 3.0 * s.score,
                        {"id": s.id, "type": s.type, "title": s.title, "path": s.path,
                         "timestamp": s.timestamp, "score": s.score, "reason": s.reason,
                         "excerpt": s.excerpt})
            except Exception:
                pass

        # ── computer ─────────────────────────────────────────────────────
        w = snap.get("active_window")
        if w:
            add("window", f"Foreground window: {w.get('title', '')} ({w.get('process', '')})", 2.0)

        return self._pack(q, intents, items, snap, budget, project,
                          period[2] if period else None)

    @staticmethod
    def _pack(q, intents, items, snap, budget, project, period_label) -> ContextBundle:
        ranked = sorted(items, key=lambda it: it.score, reverse=True)
        chosen, used, dropped = [], 0, 0
        seen = set()
        for it in ranked:
            line = "• " + it.text
            if it.text in seen:
                continue
            if used + len(line) + 1 > budget:
                dropped += 1
                continue
            chosen.append(it)
            seen.add(it.text)
            used += len(line) + 1
        # present in a stable, readable order (now/user/project first)
        order = ["now", "user", "project", "task", "window", "episode", "doc", "workflow", "audit", "fact",
                 "apps", "turn"]
        chosen.sort(key=lambda it: (order.index(it.source) if it.source in order else 99, -it.score))
        head = f"[CONTEXT for: \"{q[:80]}\"]" if q else "[CONTEXT]"
        text = head + "\n" + "\n".join("• " + it.text for it in chosen) if chosen else ""
        return ContextBundle(q, intents, chosen, snap, text, project, period_label, dropped)

    # -- session start -------------------------------------------------------------
    def live_block(self, budget: int = 700) -> str:
        lines = ["[CONTEXT ENGINE]"]
        if self.identity is not None:
            try:
                s = self.identity.summary_line()
                if s:
                    lines.append(s)
            except Exception:
                pass
        if self.episodic is not None:
            try:
                last = self.episodic.recent(1, kind="session") or self.episodic.recent(1)
                if last:
                    d = last[-1]
                    lines.append(f"Last activity {fmt_when(d['ts'], self._clock())}: {d['title'][:120]}"
                                 + (f" (project {d['project']})" if d.get("project") else ""))
            except Exception:
                pass
        if self.tasks is not None:
            try:
                act = self.tasks.active()
                if act:
                    lines.append("Running tasks: " + "; ".join(f"{t.id} {t.request[:50]}" for t in act[:3]))
            except Exception:
                pass
        lines.append("For anything about past work, projects, what the user was doing, or 'continue …', "
                     "call recall_context first — it returns only the relevant memory.")
        out = "\n".join(lines)
        return out[:budget] + "\n"
