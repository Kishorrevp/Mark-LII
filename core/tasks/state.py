"""
core/tasks/state.py — task and step state, with enforced transitions and
persistence.

    QUEUED → PLANNING → RUNNING ⇄ WAITING_FOR_PERMISSION
      │                   │   ⇄ WAITING_FOR_INPUT
      └→ BLOCKED ─┘       │   ⇄ PAUSED
                          │   ⇄ BLOCKED      (waiting for a shared resource, e.g. the mouse/keyboard)
                          │   ⇄ RECOVERING   (a step failed; working out another way)
                          ▼
                       VERIFYING → COMPLETED
    (any non-terminal) → FAILED | CANCELLED

Spec vocabulary (Phase 6): PENDING = QUEUED, WAITING = WAITING_FOR_PERMISSION /
WAITING_FOR_INPUT / PAUSED; the rest are named as in the spec. Task.phase
gives that vocabulary. Every task also has a priority (CRITICAL, HIGH,
NORMAL, LOW, BACKGROUND — core/tasks/scheduler.py) and a progress figure.

Illegal transitions raise InvalidTransition — a bug in the orchestrator
should be loud, not silently produce a task that is "COMPLETED" after
"CANCELLED".

TaskStore keeps every task in memory, persists the latest 50 to
memory/tasks/tasks.json (atomic write, CRLF-free JSON), and answers "what
are you doing?" in plain language.
"""
from __future__ import annotations

import enum
import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional


class TaskState(str, enum.Enum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_FOR_PERMISSION = "WAITING_FOR_PERMISSION"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    RECOVERING = "RECOVERING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED)


_T = TaskState
ALLOWED: dict[TaskState, set[TaskState]] = {
    _T.QUEUED: {_T.PLANNING, _T.RUNNING, _T.BLOCKED, _T.CANCELLED, _T.FAILED},
    _T.PLANNING: {_T.RUNNING, _T.WAITING_FOR_INPUT, _T.FAILED, _T.CANCELLED},
    _T.RUNNING: {_T.WAITING_FOR_PERMISSION, _T.WAITING_FOR_INPUT, _T.PAUSED, _T.VERIFYING,
                 _T.BLOCKED, _T.RECOVERING, _T.FAILED, _T.CANCELLED},
    _T.BLOCKED: {_T.RUNNING, _T.PLANNING, _T.CANCELLED, _T.FAILED},
    _T.RECOVERING: {_T.RUNNING, _T.FAILED, _T.CANCELLED},
    _T.WAITING_FOR_PERMISSION: {_T.RUNNING, _T.CANCELLED, _T.FAILED},
    _T.WAITING_FOR_INPUT: {_T.RUNNING, _T.PLANNING, _T.CANCELLED, _T.FAILED},
    _T.PAUSED: {_T.RUNNING, _T.CANCELLED},
    _T.VERIFYING: {_T.COMPLETED, _T.FAILED, _T.RUNNING, _T.CANCELLED},
    _T.COMPLETED: set(),
    _T.FAILED: set(),
    _T.CANCELLED: set(),
}


class InvalidTransition(RuntimeError):
    pass


class StepStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"      # replaced by a self-recovery plan (Phase 5)


@dataclass
class Step:
    id: str
    description: str
    tool: str
    args: dict = field(default_factory=dict)
    agent: str = "general"
    depends_on: list = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    output: dict = field(default_factory=dict)
    message: str = ""
    error: str = ""
    verification: dict = field(default_factory=dict)
    permission: dict = field(default_factory=dict)
    started_at: float | None = None
    finished_at: float | None = None
    error_class: str = ""          # core/recovery.py ErrorClass value when the step failed

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class Task:
    id: str
    request: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = ""
    state: TaskState = TaskState.QUEUED
    steps: list = field(default_factory=list)
    current_step: str = ""
    completed_steps: list = field(default_factory=list)
    failed_steps: list = field(default_factory=list)
    required_permissions: list = field(default_factory=list)
    result: str = ""
    error: str = ""
    verification_status: str = "PENDING"        # PENDING | VERIFIED | PARTIAL | FAILED | UNVERIFIED
    language: str = "en"
    origin: str = "voice"
    history: list = field(default_factory=list)  # [(ts, from, to, note)]
    pending_question: str = ""
    recoveries: int = 0                          # self-recovery replans used (Phase 5)
    retry_of: str = ""                           # id of the task this one resumes
    priority: str = "NORMAL"                     # CRITICAL | HIGH | NORMAL | LOW | BACKGROUND (Phase 6)
    blocked_reason: str = ""                     # why the task is BLOCKED / QUEUED behind others

    @property
    def progress(self) -> int:
        """0–100: finished steps of the current plan (superseded steps don't count)."""
        if self.state == TaskState.COMPLETED:
            return 100
        live = [s for s in self.steps if s.status != StepStatus.SUPERSEDED]
        if not live:
            return 0
        done = sum(1 for s in live if s.status in (StepStatus.DONE, StepStatus.SKIPPED))
        return int(round(100 * done / len(live)))

    @property
    def phase(self) -> str:
        """The state in the spec's vocabulary."""
        return {TaskState.QUEUED: "PENDING", TaskState.WAITING_FOR_PERMISSION: "WAITING",
                TaskState.WAITING_FOR_INPUT: "WAITING", TaskState.PAUSED: "WAITING"}.get(self.state,
                                                                                         self.state.value)

    def step(self, step_id: str) -> Optional[Step]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "request": self.request, "created_at": self.created_at,
            "updated_at": self.updated_at, "state": self.state.value,
            "steps": [s.to_dict() for s in self.steps], "current_step": self.current_step,
            "completed_steps": list(self.completed_steps), "failed_steps": list(self.failed_steps),
            "required_permissions": list(self.required_permissions), "result": self.result,
            "error": self.error, "verification_status": self.verification_status,
            "language": self.language, "origin": self.origin, "history": list(self.history[-40:]),
            "pending_question": self.pending_question,
            "recoveries": self.recoveries, "retry_of": self.retry_of,
            "priority": self.priority, "blocked_reason": self.blocked_reason,
            "progress": self.progress, "phase": self.phase,
        }

    @staticmethod
    def from_dict(d: dict) -> "Task":
        t = Task(id=d["id"], request=d.get("request", ""))
        for k in ("created_at", "updated_at", "current_step", "result", "error", "verification_status",
                  "language", "origin", "pending_question", "recoveries", "retry_of", "priority",
                  "blocked_reason"):
            if k in d:
                setattr(t, k, d[k])
        t.state = TaskState(d.get("state", "QUEUED"))
        t.completed_steps = list(d.get("completed_steps", []))
        t.failed_steps = list(d.get("failed_steps", []))
        t.required_permissions = list(d.get("required_permissions", []))
        t.history = [tuple(h) for h in d.get("history", [])]
        for sd in d.get("steps", []):
            s = Step(id=sd["id"], description=sd.get("description", ""), tool=sd.get("tool", ""))
            for k, v in sd.items():
                if k == "status":
                    s.status = StepStatus(v)
                elif hasattr(s, k) and k not in ("id",):
                    setattr(s, k, v)
            t.steps.append(s)
        return t


class TaskStore:
    MAX_PERSISTED = 50

    def __init__(self, path: Path | None = None, persist: bool = True):
        if path is None and persist:
            from core.secret_store import base_dir
            path = base_dir() / "memory" / "tasks" / "tasks.json"
        self.path = Path(path) if path else None
        self._persist = persist and self.path is not None
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Task, TaskState, TaskState], None]] = []
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self) -> None:
        if not self._persist:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        for d in data.get("tasks", []):
            try:
                t = Task.from_dict(d)
            except Exception:
                continue
            # A task that was mid-flight when JARVIS exited did NOT finish.
            # Mark it honestly instead of resuming blind.
            if not t.state.terminal:
                t.history.append((_now(), t.state.value, TaskState.FAILED.value,
                                  "JARVIS restarted before this task finished"))
                t.state = TaskState.FAILED
                t.error = t.error or "Interrupted: JARVIS was closed before this task finished."
            self._tasks[t.id] = t
            self._order.append(t.id)

    def save(self) -> None:
        if not self._persist:
            return
        with self._lock:
            ids = self._order[-self.MAX_PERSISTED:]
            data = {"tasks": [self._tasks[i].to_dict() for i in ids if i in self._tasks]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(data, indent=1, ensure_ascii=False).encode("utf-8"))
            os.replace(tmp, self.path)
        except Exception:
            pass

    # -- CRUD ---------------------------------------------------------------
    def create(self, request: str, origin: str = "voice", language: str = "en",
               priority: str = "NORMAL") -> Task:
        t = Task(id="T" + uuid.uuid4().hex[:6].upper(), request=request, origin=origin,
                 language=language, priority=str(priority or "NORMAL").upper())
        t.updated_at = t.created_at
        t.history.append((_now(), "", TaskState.QUEUED.value, "created"))
        with self._lock:
            self._tasks[t.id] = t
            self._order.append(t.id)
        self.save()
        return t

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def all(self) -> list[Task]:
        with self._lock:
            return [self._tasks[i] for i in self._order if i in self._tasks]

    def active(self) -> list[Task]:
        return [t for t in self.all() if not t.state.terminal]

    def latest(self) -> Optional[Task]:
        items = self.all()
        return items[-1] if items else None

    def on_transition(self, fn: Callable[[Task, TaskState, TaskState], None]) -> None:
        self._listeners.append(fn)

    def transition(self, task: Task, new: TaskState, note: str = "") -> None:
        with self._lock:
            old = task.state
            if old == new:
                return
            if new not in ALLOWED[old]:
                raise InvalidTransition(f"{task.id}: {old.value} → {new.value} is not allowed")
            task.state = new
            task.updated_at = _now()
            task.history.append((task.updated_at, old.value, new.value, note))
        self.save()
        for fn in list(self._listeners):
            try:
                fn(task, old, new)
            except Exception:
                pass

    # -- natural-language status ---------------------------------------------
    def describe(self, task: Task | None = None) -> str:
        """Answer "what are you doing?"."""
        if task is None:
            act = self.active()
            if not act:
                last = self.latest()
                if last is None:
                    return "I'm not working on any task right now."
                return (f"I'm not working on anything right now. My last task ({last.id}) "
                        f"{_state_phrase(last)}.")
            task = act[-1]
        done = len(task.completed_steps)
        total = len(task.steps)
        cur = task.step(task.current_step) if task.current_step else None
        parts = [f"Task {task.id}: \"{task.request[:80]}\" — {_state_phrase(task)}."]
        if total:
            parts.append(f"{done} of {total} steps done ({task.progress}%).")
        if task.priority not in ("", "NORMAL") and not task.state.terminal:
            parts.append(f"Priority: {task.priority.lower()}.")
        if task.state in (TaskState.BLOCKED, TaskState.QUEUED) and task.blocked_reason:
            parts.append(task.blocked_reason)
        others = [t for t in self.active() if t is not task]
        if others and not task.state.terminal:
            parts.append(f"Also {len(others)} other task(s): "
                         + "; ".join(f"{t.id} {_state_phrase(t)}" for t in others[-3:]) + ".")
        if cur and not task.state.terminal:
            parts.append(f"Current step: {cur.description}.")
        if task.state == TaskState.WAITING_FOR_PERMISSION and task.pending_question:
            parts.append(task.pending_question)
        if task.state == TaskState.WAITING_FOR_INPUT and task.pending_question:
            parts.append(task.pending_question)
        if task.failed_steps:
            fs = task.step(task.failed_steps[-1])
            if fs:
                parts.append(f"Failed at: {fs.description} ({fs.error[:120]}).")
        return " ".join(parts)


def _state_phrase(t: Task) -> str:
    return {
        TaskState.QUEUED: "is queued",
        TaskState.PLANNING: "is being planned",
        TaskState.RUNNING: "is running",
        TaskState.WAITING_FOR_PERMISSION: "is waiting for your confirmation on the HUD",
        TaskState.WAITING_FOR_INPUT: "is waiting for more information from you",
        TaskState.PAUSED: "is paused",
        TaskState.BLOCKED: "is blocked" + (f" ({t.blocked_reason[:100]})" if t.blocked_reason else ""),
        TaskState.RECOVERING: "is recovering from a failed step (trying another way)",
        TaskState.VERIFYING: "is being verified",
        TaskState.COMPLETED: "completed" + (" (verified)" if t.verification_status == "VERIFIED"
                                            else f" (verification: {t.verification_status.lower()})"),
        TaskState.FAILED: "failed" + (f": {t.error[:120]}" if t.error else ""),
        TaskState.CANCELLED: "was cancelled",
    }[t.state]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
