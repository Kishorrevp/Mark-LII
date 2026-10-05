"""
core/proactive_policy.py — when JARVIS may speak without being asked.

The existing proactive loops in main.py (system-health alerts, news-topic
monitor, idle check-ins) keep their own logic for WHAT to say. Before any of
them speaks, it now asks this policy WHETHER it may:

    NotificationPolicy.allow(source, priority) → (ok, reason)

    * quiet hours      default 22:30–07:00 local time — only CRITICAL passes
    * rate limit       max N proactive messages per rolling hour (default 4)
    * min gap          at least M seconds between any two (default 120)
    * per-source off   the user can switch a source off entirely
    * user busy        never while JARVIS is speaking or the user spoke < 10 s ago
                       (checked by the caller, which knows those facts)

Settings (config/api_keys.json, all optional):
    "proactive_quiet_hours": "22:30-07:00"     ("" disables quiet hours)
    "proactive_max_per_hour": 4
    "proactive_min_gap_seconds": 120
    "proactive_disabled_sources": ["checkin"]

ProactiveSource / ProactiveRegistry give future proactive agents (calendar,
project monitor, notification monitor, routine assistant — ROADMAP Phase 8)
a common shape: a name, a poll interval, and check() → Optional[message].
They are registered and gated here; none of the future ones are
implemented in this phase.
"""
from __future__ import annotations

import enum
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime
from typing import Callable, Optional


class Priority(enum.IntEnum):
    LOW = 0          # idle check-ins, suggestions
    NORMAL = 1       # monitored-topic news, workflow suggestions
    HIGH = 2         # system health warnings
    CRITICAL = 3     # battery about to die, disk full — passes quiet hours


def _parse_hours(v: str) -> Optional[tuple[dtime, dtime]]:
    try:
        a, b = str(v).split("-")
        ha, ma = (int(x) for x in a.strip().split(":"))
        hb, mb = (int(x) for x in b.strip().split(":"))
        return dtime(ha, ma), dtime(hb, mb)
    except Exception:
        return None


@dataclass
class PolicyConfig:
    quiet_hours: Optional[tuple] = (dtime(22, 30), dtime(7, 0))
    max_per_hour: int = 4
    min_gap_s: float = 120.0
    disabled_sources: set = field(default_factory=set)

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> "PolicyConfig":
        if cfg is None:
            try:
                from core.secret_store import legacy_config_path
                cfg = json.loads(legacy_config_path().read_text(encoding="utf-8"))
            except Exception:
                cfg = {}
        pc = cls()
        if "proactive_quiet_hours" in cfg:
            v = cfg.get("proactive_quiet_hours")
            pc.quiet_hours = _parse_hours(v) if v else None
        try:
            pc.max_per_hour = int(cfg.get("proactive_max_per_hour", pc.max_per_hour))
            pc.min_gap_s = float(cfg.get("proactive_min_gap_seconds", pc.min_gap_s))
        except Exception:
            pass
        pc.disabled_sources = {str(s).lower() for s in (cfg.get("proactive_disabled_sources") or [])}
        return pc


class NotificationPolicy:
    def __init__(self, config: PolicyConfig | None = None, clock=time.time, now=datetime.now):
        self._fixed = config
        self._clock = clock
        self._now = now
        self._sent: deque = deque(maxlen=100)
        self._lock = threading.Lock()

    @property
    def config(self) -> PolicyConfig:
        return self._fixed or PolicyConfig.from_config()

    def in_quiet_hours(self) -> bool:
        qh = self.config.quiet_hours
        if not qh:
            return False
        start, end = qh
        t = self._now().time()
        return (start <= t or t < end) if start > end else (start <= t < end)

    def allow(self, source: str, priority: Priority = Priority.NORMAL) -> tuple[bool, str]:
        cfg = self.config
        src = (source or "").lower()
        if src in cfg.disabled_sources:
            return False, f"'{source}' notifications are switched off"
        if priority < Priority.CRITICAL and self.in_quiet_hours():
            return False, "quiet hours"
        now = self._clock()
        with self._lock:
            recent = [t for t in self._sent if now - t < 3600]
            if priority < Priority.CRITICAL:
                if len(recent) >= cfg.max_per_hour:
                    return False, "hourly notification limit reached"
                if recent and now - recent[-1] < cfg.min_gap_s:
                    return False, "too soon after the last notification"
        return True, "ok"

    def record(self, source: str) -> None:
        with self._lock:
            self._sent.append(self._clock())

    def try_send(self, source: str, priority: Priority, send: Callable[[], None]) -> tuple[bool, str]:
        ok, why = self.allow(source, priority)
        if ok:
            send()
            self.record(source)
        return ok, why


class ProactiveSource:
    """Base for future proactive agents (Phase 8)."""
    name: str = "source"
    interval_s: float = 300.0
    priority: Priority = Priority.NORMAL

    def check(self) -> Optional[str]:
        """Return a message to deliver, or None. Must be fast and never raise."""
        return None


class ProactiveRegistry:
    def __init__(self, policy: NotificationPolicy):
        self.policy = policy
        self._sources: dict[str, ProactiveSource] = {}
        self._last: dict[str, float] = {}

    def register(self, src: ProactiveSource) -> None:
        self._sources[src.name] = src

    def sources(self) -> list[str]:
        return sorted(self._sources)

    def poll(self, deliver: Callable[[str, ProactiveSource], None], now: float | None = None) -> list[str]:
        """Run due sources once; deliver allowed messages. Returns what was delivered."""
        now = now if now is not None else time.time()
        out = []
        for name, src in self._sources.items():
            if now - self._last.get(name, 0) < src.interval_s:
                continue
            self._last[name] = now
            try:
                msg = src.check()
            except Exception:
                msg = None
            if not msg:
                continue
            ok, _ = self.policy.allow(name, src.priority)
            if ok:
                deliver(msg, src)
                self.policy.record(name)
                out.append(msg)
        return out
