"""
core/tasks/scheduler.py — which task runs now, which waits, and who may use
the mouse and keyboard.

PRIORITIES
    CRITICAL    starts immediately, even when every slot is busy
    HIGH        "urgent", "right now", "asap"
    NORMAL      anything the user asks for
    LOW         long jobs the user said can wait ("later", "when you're free")
    BACKGROUND  proactive / housekeeping work JARVIS started itself

TaskScheduler
    At most `max_parallel` tasks (config/runtime.json → tasks.max_parallel,
    default 3) do work at the same time. A task that is only waiting for the
    user (a HUD confirmation, an answer, a pause) does not hold a slot. Extra
    tasks stay PENDING (QUEUED) in priority order, then first-come-first-served.
    Nothing is ever pre-empted mid-step — stopping a half-done action is what
    the emergency stop is for, not the scheduler.

ResourceLocks
    Exclusive resources shared by tasks and direct voice commands. Today there
    is one: "gui" — the real mouse and keyboard. Two jobs typing into
    different windows at once would corrupt both, so a step that drives the
    GUI takes the lock; a task that has to wait for it is BLOCKED ("task T12
    is using the mouse and keyboard"). A lock can carry a lease so a holder
    that dies cannot keep it forever.
"""
from __future__ import annotations

import enum
import heapq
import itertools
import re
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional


class Priority(enum.IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4

    @classmethod
    def parse(cls, v, default: "Priority | None" = None) -> "Priority":
        if isinstance(v, Priority):
            return v
        if isinstance(v, int) and 0 <= v <= 4:
            return Priority(v)
        s = str(v or "").strip().upper()
        return cls.__members__.get(s, default if default is not None else Priority.NORMAL)

    @classmethod
    def infer(cls, request: str = "", origin: str = "voice") -> "Priority":
        if origin in ("proactive", "background", "workflow_trigger"):
            return Priority.BACKGROUND
        t = (request or "").lower()
        if re.search(r"\b(urgent(?:ly)?|asap|right now|immediately|emergency|high priority)\b", t):
            return Priority.HIGH
        if re.search(r"\b(no rush|whenever|when you(?:'re| are) free|in the background|low priority|"
                     r"later today|later on)\b", t):
            return Priority.LOW
        return Priority.NORMAL


GUI = "gui"
RESOURCE_LABEL = {GUI: "the mouse and keyboard"}


class ResourceLocks:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._cond = threading.Condition()
        self._held: dict[str, dict] = {}          # resource → {owner, count, until}

    def _expire(self, resource: str) -> None:
        h = self._held.get(resource)
        if h and h.get("until") is not None and self._clock() >= h["until"]:
            self._held.pop(resource, None)
            self._cond.notify_all()

    def holder(self, resource: str) -> Optional[str]:
        with self._cond:
            self._expire(resource)
            h = self._held.get(resource)
            return h["owner"] if h else None

    def try_acquire(self, resource: str, owner: str, lease: float | None = None) -> bool:
        return self.acquire(resource, owner, timeout=0, lease=lease)

    def acquire(self, resource: str, owner: str, *, timeout: float | None = None, lease: float | None = None,
                on_wait: Callable[[str], None] | None = None,
                should_stop: Callable[[], bool] | None = None) -> bool:
        """Block until `owner` holds `resource` (re-entrant for the same owner).
        False on timeout or when should_stop() turns true."""
        deadline = None if timeout is None else self._clock() + max(0.0, timeout)
        told = False
        with self._cond:
            while True:
                self._expire(resource)
                h = self._held.get(resource)
                if h is None or h["owner"] == owner:
                    if h is None:
                        self._held[resource] = {"owner": owner, "count": 1,
                                                "until": None if lease is None else self._clock() + lease}
                    else:
                        h["count"] += 1
                        if lease is not None:
                            h["until"] = self._clock() + lease
                    return True
                if deadline is not None and self._clock() >= deadline:
                    return False
                if should_stop is not None and should_stop():
                    return False
                if not told and on_wait is not None:
                    told = True
                    holder = h["owner"]
                    self._cond.release()
                    try:
                        on_wait(holder)
                    finally:
                        self._cond.acquire()
                    continue
                self._cond.wait(0.2)

    def release(self, resource: str, owner: str) -> None:
        with self._cond:
            h = self._held.get(resource)
            if h and h["owner"] == owner:
                h["count"] -= 1
                if h["count"] <= 0:
                    self._held.pop(resource, None)
                self._cond.notify_all()

    def release_all(self, owner: str) -> None:
        with self._cond:
            for r in [r for r, h in self._held.items() if h["owner"] == owner]:
                self._held.pop(r, None)
            self._cond.notify_all()

    @contextmanager
    def hold(self, resource: str, owner: str, **kw):
        ok = self.acquire(resource, owner, **kw)
        try:
            yield ok
        finally:
            if ok:
                self.release(resource, owner)

    def snapshot(self) -> dict:
        with self._cond:
            for r in list(self._held):
                self._expire(r)
            return {r: h["owner"] for r, h in self._held.items()}


_LOCKS: ResourceLocks | None = None
_LLOCK = threading.Lock()


def resource_locks() -> ResourceLocks:
    """The process-wide lock table (shared by every orchestrator and main.py)."""
    global _LOCKS
    with _LLOCK:
        if _LOCKS is None:
            _LOCKS = ResourceLocks()
        return _LOCKS


class TaskScheduler:
    def __init__(self, max_parallel: int | None = None, is_idle: Callable[[str], bool] | None = None):
        self._fixed_max = max_parallel
        self._is_idle = is_idle or (lambda tid: False)
        self._heap: list = []
        self._seq = itertools.count()
        self._queued: dict[str, tuple] = {}
        self._running: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    @property
    def max_parallel(self) -> int:
        if self._fixed_max is not None:
            return max(1, int(self._fixed_max))
        try:
            from core.config import runtime
            return max(1, int(runtime("tasks", "max_parallel", 3)))
        except Exception:
            return 3

    def busy(self) -> int:
        with self._lock:
            return sum(1 for tid, th in self._running.items() if th.is_alive() and not self._is_idle(tid))

    def submit(self, task_id: str, priority: Priority, fn: Callable[[], None]) -> str:
        """'started' or 'queued'."""
        priority = Priority.parse(priority)
        with self._lock:
            if priority == Priority.CRITICAL or self.busy() < self.max_parallel:
                self._spawn(task_id, fn)
                return "started"
            entry = [int(priority), next(self._seq), task_id, fn]
            heapq.heappush(self._heap, entry)
            self._queued[task_id] = entry
            return "queued"

    def _spawn(self, task_id: str, fn: Callable[[], None]) -> None:
        def run():
            try:
                fn()
            finally:
                with self._lock:
                    self._running.pop(task_id, None)
                self.pump()
        th = threading.Thread(target=run, daemon=True, name=f"task-{task_id}")
        self._running[task_id] = th
        th.start()

    def pump(self) -> list[str]:
        """Start queued tasks while slots are free (called when a task ends or
        goes idle waiting for the user)."""
        started = []
        with self._lock:
            while self._heap and self.busy() < self.max_parallel:
                entry = heapq.heappop(self._heap)
                tid = entry[2]
                if self._queued.pop(tid, None) is None:
                    continue                          # removed (cancelled) while queued
                self._spawn(tid, entry[3])
                started.append(tid)
        return started

    def remove(self, task_id: str) -> bool:
        """Drop a task that has not started yet."""
        with self._lock:
            return self._queued.pop(task_id, None) is not None

    def position(self, task_id: str) -> int:
        """1-based place in the queue (0 = not queued)."""
        with self._lock:
            order = sorted((e for e in self._heap if e[2] in self._queued), key=lambda e: (e[0], e[1]))
            for i, e in enumerate(order):
                if e[2] == task_id:
                    return i + 1
            return 0

    def thread(self, task_id: str) -> Optional[threading.Thread]:
        with self._lock:
            return self._running.get(task_id)

    def queued(self) -> list[str]:
        with self._lock:
            return [e[2] for e in sorted(self._heap, key=lambda e: (e[0], e[1])) if e[2] in self._queued]

    def running(self) -> list[str]:
        with self._lock:
            return [t for t, th in self._running.items() if th.is_alive()]
