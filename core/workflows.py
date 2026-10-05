"""
core/workflows.py — noticing repeated routines, and saving them only with
permission.

OBSERVE
    Every successful tool call (direct voice calls and task steps) is
    recorded as a compact signature: tool + the arguments that identify
    *what* (app name, folder, action) — never bodies/content. Calls less than
    SESSION_GAP apart form one "session" of activity.

DETECT
    A contiguous sequence of 2–5 calls that appears in at least
    MIN_REPEATS distinct sessions within WINDOW_DAYS becomes a *suggestion*:
        "I noticed you often: open VS Code → open the JARVIS folder → run
         the dev server. Would you like me to save it as a workflow?"
    Suggestions are offered at most once each (dismissals are remembered).

SAVE (only on the user's yes)
    name, trigger phrases, steps [{tool, args}], required permissions
    (computed from the registry), tools, created, last_used, use_count.

RUN
    "Run my JARVIS development workflow" → the saved steps are handed to the
    Orchestrator as a pre-built plan — validated and permission-checked step
    by step, exactly like any other task. A workflow never bypasses
    confirmation, and runs with origin="workflow".

Nothing is automated silently.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

SESSION_GAP = 10 * 60          # seconds between calls that still count as one routine
MIN_REPEATS = 3
WINDOW_DAYS = 14
MAX_HISTORY = 400

# args that identify WHAT was done (kept); everything else is dropped from signatures
_IDENTITY_ARGS = {"action", "app_name", "path", "destination", "url", "query", "mode", "platform",
                  "receiver", "recipient", "browser", "name", "extension", "city", "game_name"}
_SKIP_TOOLS = {"recall_memory", "save_memory", "forget_memory", "undo", "run_task", "task_control",
               "workflow", "set_language", "memory_inspect", "screen_process", "close_camera",
               "shutdown_jarvis", "system_status", "system.status", "file.select", "text.extract_url"}


def signature(tool: str, args: dict) -> dict:
    keep = {k: v for k, v in (args or {}).items()
            if k in _IDENTITY_ARGS and isinstance(v, (str, int, float, bool)) and not str(v).startswith("$")}
    # message bodies never become part of a workflow
    if tool in ("send_message", "message.send"):
        keep.pop("message_text", None)
    return {"tool": tool, "args": keep}


def _key(sig: dict) -> str:
    return sig["tool"] + "|" + json.dumps(sig["args"], sort_keys=True)


class WorkflowStore:
    def __init__(self, path: Path | None = None, registry=None, persist: bool = True,
                 clock=time.time):
        if path is None and persist:
            from core.secret_store import base_dir
            path = base_dir() / "memory" / "workflows.json"
        self.path = Path(path) if path else None
        self._persist = persist and self.path is not None
        self._registry = registry
        self._clock = clock
        self._lock = threading.RLock()
        self._data = {"history": [], "workflows": [], "dismissed": [], "offered": []}
        self._load()

    # -- persistence ------------------------------------------------------------
    def _load(self):
        if not self._persist:
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            for k in self._data:
                if isinstance(d.get(k), list):
                    self._data[k] = d[k]
        except Exception:
            pass

    def _save(self):
        if not self._persist:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(self._data, indent=1, ensure_ascii=False).encode("utf-8"))
            os.replace(tmp, self.path)
        except Exception:
            pass

    # -- observe ----------------------------------------------------------------
    def observe(self, tool: str, args: dict, ok: bool = True) -> None:
        if not ok or tool in _SKIP_TOOLS:
            return
        sig = signature(tool, args)
        with self._lock:
            self._data["history"].append({"t": self._clock(), **sig})
            self._data["history"] = self._data["history"][-MAX_HISTORY:]
            self._save()

    def _sessions(self) -> list[list[dict]]:
        cutoff = self._clock() - WINDOW_DAYS * 86400
        hist = [h for h in self._data["history"] if h["t"] >= cutoff]
        sessions: list[list[dict]] = []
        cur: list[dict] = []
        last_t = None
        for h in hist:
            if last_t is not None and h["t"] - last_t > SESSION_GAP:
                if cur:
                    sessions.append(cur)
                cur = []
            cur.append(h)
            last_t = h["t"]
        if cur:
            sessions.append(cur)
        return sessions

    # -- detect -----------------------------------------------------------------
    def suggestions(self, limit: int = 3) -> list[dict]:
        with self._lock:
            sessions = self._sessions()
            counts: dict[tuple, set] = {}
            examples: dict[tuple, list] = {}
            for si, sess in enumerate(sessions):
                keys = [_key(h) for h in sess]
                for n in range(2, 6):
                    for i in range(0, len(keys) - n + 1):
                        seq = tuple(keys[i:i + n])
                        if len(set(seq)) < n:          # ignore "same thing three times"
                            continue
                        counts.setdefault(seq, set()).add(si)
                        examples.setdefault(seq, [{"tool": h["tool"], "args": h["args"]} for h in sess[i:i + n]])
            saved = {tuple(_key(s) for s in w["steps"]) for w in self._data["workflows"]}
            dismissed = {tuple(d) for d in self._data["dismissed"]}
            found = [(seq, len(ss)) for seq, ss in counts.items()
                     if len(ss) >= MIN_REPEATS and seq not in saved and seq not in dismissed]
            # prefer longer, then more frequent; drop sequences contained in a longer found one
            found.sort(key=lambda x: (-len(x[0]), -x[1]))
            chosen: list[tuple] = []
            for seq, n in found:
                if any(_contains(c, seq) for c, _ in chosen):
                    continue
                chosen.append((seq, n))
            out = []
            for seq, n in chosen[:limit]:
                steps = examples[seq]
                out.append({"id": _seq_id(seq), "steps": steps, "times": n,
                            "description": " → ".join(describe_step(s) for s in steps),
                            "question": "I noticed you often do this: "
                                        + " → ".join(describe_step(s) for s in steps)
                                        + ". Would you like me to save it as a workflow?"})
            return out

    def dismiss(self, suggestion_id: str) -> str:
        with self._lock:
            for s in self.suggestions(limit=20):
                if s["id"] == suggestion_id:
                    self._data["dismissed"].append([_key(x) for x in s["steps"]])
                    self._save()
                    return "Okay, I won't suggest that one again."
        return "I couldn't find that suggestion."

    def next_offer(self) -> Optional[dict]:
        """At most one suggestion per sequence is ever *offered* proactively."""
        with self._lock:
            for s in self.suggestions():
                if s["id"] not in self._data["offered"]:
                    self._data["offered"].append(s["id"])
                    self._save()
                    return s
        return None

    # -- save / list / delete ------------------------------------------------------
    def save(self, name: str, steps: list[dict] | None = None, suggestion_id: str | None = None,
             triggers: list[str] | None = None) -> dict:
        name = re.sub(r"\s+", " ", str(name or "").strip())
        if not name:
            raise ValueError("A workflow needs a name.")
        if suggestion_id:
            match = [s for s in self.suggestions(limit=20) if s["id"] == suggestion_id]
            if not match:
                raise ValueError("That suggestion is no longer available.")
            steps = match[0]["steps"]
        if not steps:
            raise ValueError("A workflow needs at least one step.")
        if self._registry is not None:
            for s in steps:
                if not self._registry.has(s["tool"]):
                    raise ValueError(f"Unknown tool in workflow: {s['tool']}")
        perms = self.required_permissions(steps)
        wf = {"id": uuid.uuid4().hex[:8], "name": name,
              "triggers": [t.lower() for t in (triggers or [name])],
              "steps": [{"tool": s["tool"], "args": dict(s.get("args") or {})} for s in steps],
              "tools": sorted({s["tool"] for s in steps}), "required_permissions": perms,
              "created": datetime.now().isoformat(timespec="seconds"), "last_used": None, "use_count": 0}
        with self._lock:
            self._data["workflows"] = [w for w in self._data["workflows"] if w["name"].lower() != name.lower()]
            self._data["workflows"].append(wf)
            self._save()
        return wf

    def required_permissions(self, steps: list[dict]) -> list[str]:
        if self._registry is None:
            return []
        from core.permissions import RiskLevel
        out = []
        for s in steps:
            r = self._registry.risk_for(s["tool"], s.get("args") or {})
            if r >= RiskLevel.HIGH_RISK:
                out.append(f"{s['tool']} ({r.name})")
        return out

    def list(self) -> list[dict]:
        return list(self._data["workflows"])

    def find(self, text: str) -> Optional[dict]:
        t = str(text or "").lower()
        t = re.sub(r"\b(run|start|do|my|the|workflow|routine|please|jarvis)\b", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        best, best_score = None, 0
        for w in self._data["workflows"]:
            for trig in w["triggers"] + [w["name"].lower()]:
                trig_c = re.sub(r"\b(workflow|routine|my|the)\b", " ", trig)
                trig_c = re.sub(r"\s+", " ", trig_c).strip()
                if not trig_c:
                    continue
                words = set(trig_c.split())
                score = len(words & set(t.split())) / max(1, len(words))
                if trig_c in t or t in trig_c:
                    score = max(score, 1.0)
                if score > best_score:
                    best, best_score = w, score
        return best if best_score >= 0.6 else None

    def delete(self, name: str) -> bool:
        with self._lock:
            before = len(self._data["workflows"])
            self._data["workflows"] = [w for w in self._data["workflows"]
                                       if w["name"].lower() != str(name).lower()]
            self._save()
            return len(self._data["workflows"]) < before

    def mark_used(self, wf_id: str) -> None:
        with self._lock:
            for w in self._data["workflows"]:
                if w["id"] == wf_id:
                    w["last_used"] = datetime.now().isoformat(timespec="seconds")
                    w["use_count"] = int(w.get("use_count", 0)) + 1
            self._save()

    def to_steps(self, wf: dict):
        from core.tasks.state import Step
        out = []
        for i, s in enumerate(wf["steps"]):
            sid = f"s{i + 1}"
            out.append(Step(id=sid, description=describe_step(s), tool=s["tool"], args=dict(s["args"]),
                            depends_on=[f"s{i}"] if i else []))
        return out


def describe_step(s: dict) -> str:
    a = s.get("args") or {}
    what = a.get("app_name") or a.get("url") or a.get("query") or a.get("path") or a.get("name") or ""
    act = a.get("action", "")
    label = s["tool"].replace("_", " ").replace(".", " ")
    return f"{label}{' ' + act if act else ''}{': ' + str(what) if what else ''}"


def _seq_id(seq: tuple) -> str:
    import hashlib
    return hashlib.sha1("||".join(seq).encode()).hexdigest()[:8]


def _contains(big: tuple, small: tuple) -> bool:
    n = len(small)
    return any(big[i:i + n] == small for i in range(0, len(big) - n + 1))
