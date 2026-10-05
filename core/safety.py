"""
core/safety.py — the EMERGENCY STOP.

One switch, reachable three ways, that halts everything JARVIS is doing:

    F12 (global to the JARVIS window) · the STOP button on the dock ·
    the voice command "JARVIS, stop everything" (emergency_stop tool)

engage() runs every registered stopper, in order, each isolated so one failure
cannot block the others:

    * the Orchestrator cancels every running / waiting / paused task
    * pending HUD confirmations are withdrawn (nothing can be approved late)
    * the computer-use agent stops before its next mouse/keyboard action
      (it also checks the switch between every single action)
    * long-running research / team builds poll should_stop() and unwind
    * text-to-speech playback is cut

While engaged, check() raises EmergencyStop — any code about to act on the
machine calls it first. The switch stays engaged until the user gives a NEW
command (release() is called when a fresh user request is submitted), so a
task that was already queued cannot slip through after a stop.

Every engage/release is written to the audit log.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional


class EmergencyStop(RuntimeError):
    """Raised by check() while the emergency stop is engaged."""


class KillSwitch:
    def __init__(self):
        self._engaged = threading.Event()
        self._stoppers: list[tuple[str, Callable[[str], object]]] = []
        self._lock = threading.Lock()
        self.reason = ""
        self.engaged_at: Optional[float] = None
        self.audit = None
        self.generation = 0          # bumps on every engage — long jobs compare it

    # -- registration -----------------------------------------------------------
    def register(self, name: str, stopper: Callable[[str], object]) -> None:
        with self._lock:
            self._stoppers = [(n, s) for n, s in self._stoppers if n != name]
            self._stoppers.append((name, stopper))

    def unregister(self, name: str) -> None:
        with self._lock:
            self._stoppers = [(n, s) for n, s in self._stoppers if n != name]

    # -- state -------------------------------------------------------------------
    def engaged(self) -> bool:
        return self._engaged.is_set()

    def check(self) -> None:
        if self._engaged.is_set():
            raise EmergencyStop(f"Emergency stop is engaged ({self.reason or 'user'}).")

    def engage(self, reason: str = "user") -> str:
        with self._lock:
            self._engaged.set()
            self.reason = str(reason or "user")[:80]
            self.engaged_at = time.time()
            self.generation += 1
            stoppers = list(self._stoppers)
        results = []
        for name, fn in stoppers:
            try:
                r = fn(self.reason)
                results.append(f"{name}: {r}" if r not in (None, "") else name)
            except Exception as e:           # a stopper must never block the others
                results.append(f"{name}: failed ({type(e).__name__})")
        if self.audit is not None:
            try:
                self.audit.record("emergency_stop", reason=self.reason, stopped=results)
            except Exception:
                pass
        return ("Emergency stop engaged. Everything has been halted" +
                (f" — {'; '.join(results)}." if results else ".") +
                " Nothing will run until you give me a new command.")

    def release(self, by: str = "new command") -> bool:
        if not self._engaged.is_set():
            return False
        self._engaged.clear()
        if self.audit is not None:
            try:
                self.audit.record("emergency_release", by=by)
            except Exception:
                pass
        return True

    def status(self) -> dict:
        return {"engaged": self.engaged(), "reason": self.reason, "since": self.engaged_at,
                "stoppers": [n for n, _ in self._stoppers]}


_SWITCH: Optional[KillSwitch] = None
_SLOCK = threading.Lock()


def kill_switch() -> KillSwitch:
    global _SWITCH
    with _SLOCK:
        if _SWITCH is None:
            _SWITCH = KillSwitch()
        return _SWITCH


def set_kill_switch(ks: Optional[KillSwitch]) -> None:
    """Test hook."""
    global _SWITCH
    with _SLOCK:
        _SWITCH = ks
