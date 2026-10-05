"""
memory/memory_hub.py — one front door over JARVIS's memory, organised into
seven explicit categories, without replacing the existing store.

    Category             Where it lives                         Persisted?
    ─────────────────    ─────────────────────────────────────  ──────────
    SHORT_TERM_CONTEXT   RAM ring buffer (this process only)    no
    LONG_TERM_MEMORY     long_term.json: identity, projects,     yes (existing)
                         relationships, wishes, notes
    USER_PREFERENCES     long_term.json: preferences             yes (existing)
    LEARNED_WORKFLOWS    memory/workflows.json (core/workflows)  yes
    TASK_MEMORY          memory/tasks/tasks.json (TaskStore)     yes (last 50)
    SESSION_SUMMARIES    long_term.json: sessions                yes (existing)
    DEVICE_CONTEXT       memory/device_context.json              yes

Phase 5 — the three classic memory systems on top of those stores:

    SEMANTIC     facts that stay true   long_term.json + memory/identity.json
                                        (PersonalModel: projects, apps, people…)
    EPISODIC     events in time         memory/episodes.jsonl (EpisodicStore)
    PROCEDURAL   how to do things       memory/workflows.json (WorkflowStore)

    hub.save_fact() runs memory/policy.py first — secrets are refused,
    passing states become episodes, sensitive-category facts need the
    user's explicit yes, duplicates are skipped.

memory/memory_manager.py is untouched in behaviour — this module calls its
functions. What it adds:

  * SELECTIVITY — remember() refuses secrets (API keys, passwords, OTPs,
    card/account-number-shaped strings) and says so; nothing here stores
    conversations automatically.
  * inspect() / summarize() — "what do you remember about me?" across all
    categories, counts first, without dumping private values into logs.
  * update() — change a value in place (remember() on an existing key).
"""
from __future__ import annotations

import enum
import json
import platform
import re
import socket
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class MemoryCategory(str, enum.Enum):
    EPISODIC = "episodic"
    IDENTITY = "identity_model"
    SHORT_TERM_CONTEXT = "short_term_context"
    LONG_TERM_MEMORY = "long_term_memory"
    USER_PREFERENCES = "user_preferences"
    LEARNED_WORKFLOWS = "learned_workflows"
    TASK_MEMORY = "task_memory"
    SESSION_SUMMARIES = "session_summaries"
    DEVICE_CONTEXT = "device_context"


LONG_TERM_SUBCATEGORIES = ("identity", "projects", "relationships", "wishes", "notes")

# Things that must never be stored, even if the user asks.
_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)\b(passwords?|passcode|passwd|pin|pin code|pin number|otp|one[- ]time code|cvv|cvc|"
               r"security code|secret key|private key|api key|access token|recovery phrase|seed phrase)\b"),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),          # card / account-number-shaped digit runs
    re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),       # 12-digit national-ID-shaped numbers (e.g. Aadhaar)
]


def is_sensitive(text: str) -> tuple[bool, str]:
    s = str(text or "")
    words = re.sub(r"[_\-.]+", " ", s)          # "wifi_password" → "wifi password"
    try:
        from core.secret_store import looks_like_secret
        if looks_like_secret(s):
            return True, "it looks like a credential or key"
    except Exception:
        pass
    for p in _SENSITIVE_PATTERNS:
        if p.search(s) or p.search(words):
            return True, "it looks like a password, code, or account/ID number"
    return False, ""


class ShortTermContext:
    """Last N turns/events of this session. Never written to disk."""

    def __init__(self, maxlen: int = 40):
        self._items: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, role: str, text: str) -> None:
        with self._lock:
            self._items.append({"ts": datetime.now().isoformat(timespec="seconds"),
                                "role": role, "text": str(text)[:500]})

    def recent(self, n: int = 10) -> list[dict]:
        with self._lock:
            return list(self._items)[-n:]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class DeviceContext:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def refresh(self) -> dict:
        d = self.load()
        d.update({"os": platform.system(), "os_version": platform.release(),
                  "machine": platform.machine(), "hostname": socket.gethostname(),
                  "python": platform.python_version(),
                  "updated": datetime.now().isoformat(timespec="seconds")})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(d, indent=2), encoding="utf-8")
        except Exception:
            pass
        return d

    def set(self, key: str, value: Any) -> None:
        d = self.load()
        d[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=2), encoding="utf-8")


class MemoryHub:
    def __init__(self, base_dir: Path | None = None, task_store=None, workflows=None):
        if base_dir is None:
            from core.secret_store import base_dir as _bd
            base_dir = _bd()
        self.base = Path(base_dir)
        self.short_term = ShortTermContext()
        self.device = DeviceContext(self.base / "memory" / "device_context.json")
        self.task_store = task_store
        self.workflows = workflows
        self._episodic = None
        self._identity = None

    # -- Phase 5 stores (lazy so tests / old callers pay nothing) ---------------
    @property
    def episodic(self):
        if self._episodic is None:
            from memory.episodic import EpisodicStore
            self._episodic = EpisodicStore(self.base / "memory" / "episodes.jsonl")
        return self._episodic

    @property
    def identity(self):
        if self._identity is None:
            from memory.identity import PersonalModel
            self._identity = PersonalModel(self.base, workflows=self.workflows,
                                           memory_loader=lambda: self._mm().load_memory())
        return self._identity

    @property
    def semantic(self):
        return self._mm()

    @property
    def procedural(self):
        return self.workflows

    def save_fact(self, category: str, key: str, value: str, explicit: bool = False,
                  source: str = "tool", said: str = "") -> str:
        """The selective path used by the save_memory tool. Durable facts are
        written with memory_manager.write_fact (atomic + verified)."""
        from memory import policy
        mm = self._mm()
        d = policy.evaluate(category, key, value, explicit=explicit, existing=mm.load_memory())
        wf = getattr(mm, "write_fact", None)
        if wf is None:                         # injected test double without verified writes
            return policy.apply(d, episodic=self.episodic, update_memory=mm.update_memory)
        return policy.apply(d, episodic=self.episodic, write_fact=wf, source=source, said=said)

    # -- long-term / preferences (delegates to memory_manager) ----------------
    @staticmethod
    def _mm():
        from memory import memory_manager
        return memory_manager

    def remember(self, key: str, value: str, category: str = "notes") -> str:
        key, value = str(key or "").strip(), str(value or "").strip()
        if not key or not value:
            return "I need both what to remember and its value."
        bad, why = is_sensitive(f"{key} {value}")
        if bad:
            return (f"I won't store that because {why}. Sensitive details like passwords, codes and "
                    f"account numbers stay out of my memory.")
        cat = self._subcategory(category)
        self._mm().update_memory({cat: {key: {"value": value}}})
        return f"Remembered under {cat}: {key}."

    update = remember

    def forget(self, key: str, category: str = "notes") -> str:
        return self._mm().forget(key, self._subcategory(category))

    @staticmethod
    def _subcategory(category: str) -> str:
        c = str(category or "notes").strip().lower()
        if c in ("user_preferences", "preferences", "preference"):
            return "preferences"
        if c in LONG_TERM_SUBCATEGORIES:
            return c
        return "notes"

    # -- inspection -------------------------------------------------------------
    def inspect(self, category: str | None = None) -> dict:
        mem = self._mm().load_memory()
        out: dict = {}

        def want(c: MemoryCategory) -> bool:
            return category in (None, "", "all") or category == c.value

        if want(MemoryCategory.LONG_TERM_MEMORY):
            out[MemoryCategory.LONG_TERM_MEMORY.value] = {
                k: {kk: (vv.get("value") if isinstance(vv, dict) else vv) for kk, vv in (mem.get(k) or {}).items()}
                for k in LONG_TERM_SUBCATEGORIES}
        if want(MemoryCategory.USER_PREFERENCES):
            out[MemoryCategory.USER_PREFERENCES.value] = {
                k: (v.get("value") if isinstance(v, dict) else v) for k, v in (mem.get("preferences") or {}).items()}
        if want(MemoryCategory.SESSION_SUMMARIES):
            out[MemoryCategory.SESSION_SUMMARIES.value] = list(mem.get("sessions") or [])
        if want(MemoryCategory.SHORT_TERM_CONTEXT):
            out[MemoryCategory.SHORT_TERM_CONTEXT.value] = self.short_term.recent(10)
        if want(MemoryCategory.LEARNED_WORKFLOWS) and self.workflows is not None:
            out[MemoryCategory.LEARNED_WORKFLOWS.value] = [w["name"] for w in self.workflows.list()]
        if want(MemoryCategory.TASK_MEMORY) and self.task_store is not None:
            out[MemoryCategory.TASK_MEMORY.value] = [
                {"id": t.id, "request": t.request[:80], "state": t.state.value} for t in self.task_store.all()[-10:]]
        if want(MemoryCategory.DEVICE_CONTEXT):
            out[MemoryCategory.DEVICE_CONTEXT.value] = self.device.load()
        if want(MemoryCategory.EPISODIC):
            try:
                out[MemoryCategory.EPISODIC.value] = [
                    {"when": e["ts"], "kind": e["kind"], "title": e["title"], "project": e.get("project")}
                    for e in self.episodic.recent(10)]
            except Exception:
                out[MemoryCategory.EPISODIC.value] = []
        if want(MemoryCategory.IDENTITY):
            try:
                idm = self.identity
                out[MemoryCategory.IDENTITY.value] = {
                    "projects": {k: {kk: vv for kk, vv in v.items() if kk in ("path", "last_active", "source")}
                                 for k, v in idm.projects().items()},
                    "frequent_apps": idm.frequent_apps(8)}
            except Exception:
                out[MemoryCategory.IDENTITY.value] = {}
        return out

    def summarize(self) -> str:
        d = self.inspect()
        lt = d.get(MemoryCategory.LONG_TERM_MEMORY.value, {})
        counts = {k: len(v) for k, v in lt.items() if v}
        prefs = d.get(MemoryCategory.USER_PREFERENCES.value, {})
        parts = []
        if counts:
            parts.append("Long-term memory: " + ", ".join(f"{n} {k}" for k, n in counts.items()) + ".")
        else:
            parts.append("Long-term memory is empty.")
        parts.append(f"{len(prefs)} preference(s) saved" + (": " + ", ".join(list(prefs)[:6]) if prefs else "") + ".")
        wf = d.get(MemoryCategory.LEARNED_WORKFLOWS.value)
        if wf is not None:
            parts.append(f"{len(wf)} saved workflow(s)" + (": " + ", ".join(wf[:5]) if wf else "") + ".")
        tm = d.get(MemoryCategory.TASK_MEMORY.value)
        if tm is not None:
            parts.append(f"{len(tm)} recent task(s) on record.")
        ss = d.get(MemoryCategory.SESSION_SUMMARIES.value) or []
        parts.append(f"{len(ss)} pending session summary(ies).")
        ep = d.get(MemoryCategory.EPISODIC.value)
        if ep is not None:
            try:
                st = self.episodic.stats()
                parts.append(f"Episodic memory: {st.get('total', 0)} dated episode(s)"
                             + (f" across {len(st['projects'])} project(s)" if st.get("projects") else "") + ".")
            except Exception:
                pass
        idm = d.get(MemoryCategory.IDENTITY.value) or {}
        if idm.get("projects"):
            parts.append("Known projects: " + ", ".join(list(idm["projects"])[:5]) + ".")
        parts.append("I never store passwords, codes, keys or account numbers, and I don't save "
                     "conversations automatically — only what you ask me to remember.")
        return " ".join(parts)
