"""
memory/episodic.py — EPISODIC memory: what happened, when, on which project.

    SEMANTIC    facts that stay true        long_term.json (memory_manager) + identity.json
    EPISODIC    events in time              memory/episodes.jsonl        ← this module
    PROCEDURAL  how to do things            memory/workflows.json (core/workflows.py)

An episode is a small record — never a transcript:

    {"id": "ep_…", "ts": "2026-09-26T21:14:03", "kind": "task",
     "title": "Built a portfolio website", "summary": "…one or two sentences…",
     "project": "JARVIS", "tags": ["website", "html"],
     "refs": {"task_id": "…", "paths": ["C:/…/index.html"]}, "outcome": "done"}

kinds: session (end-of-session summary), task (a finished/failed task),
       artifact (a file JARVIS produced), event (a notable single action),
       decision (something the user decided / approved).

What makes it answer "continue the project we worked on yesterday":
    * time phrases → date ranges: today, yesterday, last night, this week,
      last week, N days ago, on monday … (resolve_period)
    * search(query, period=…) scores keyword overlap × recency
    * last_project(period) → the project with the most activity in that range

SAFETY
    * Everything written passes is_sensitive(): credentials / ID numbers are
      replaced by "[redacted]" rather than stored.
    * Only titles and short summaries are kept (400 chars max) — no message
      bodies, no document text.
    * Retention: events 30 days, everything else 365 days, 3,000 records max.
      The user can list, search and forget episodes (forget(id) / clear()).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

KINDS = ("session", "task", "artifact", "event", "decision")
MAX_RECORDS = 3000
RETENTION_DAYS = {"event": 30}
DEFAULT_RETENTION = 365
SUMMARY_MAX = 400
TITLE_MAX = 140

_STOP = set("""a an the and or of to in on for with at by from is are was were be been it this that these
those my our your me i you we us please jarvis can could would should will do did does done what which who
how when where why about into over just again continue resume last yesterday today tonight morning evening
night week weeks day days ago worked working work on up get let lets let's""".split())

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[\w஀-௿]+", (text or "").lower()):
        if w in _STOP or len(w) < 2:
            continue
        # tiny stemmer: projects→project, building→build, analysed→analys
        for suf in ("ing", "ed", "es", "s"):
            if len(w) > 4 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.append(w)
    return out


def _scrub(text: str, limit: int) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    try:
        from memory.memory_hub import is_sensitive
        bad, _ = is_sensitive(s)
        if bad:
            # keep the sentence, drop the secret-shaped tokens
            s = re.sub(r"\b(?:\d[ -]?){8,}\b", "[redacted]", s)
            s = re.sub(r"(?i)\b(password|passcode|pin|otp|api key|token|secret)\b\s*[:=]?\s*\S+",
                       r"\1 [redacted]", s)
            try:
                from core.secret_store import redact
                s = redact(s)
            except Exception:
                pass
    except Exception:
        pass
    return s[:limit]


def resolve_period(text: str, now: Optional[datetime] = None) -> Optional[tuple[datetime, datetime, str]]:
    """Map a time phrase in `text` to (start, end, label). None if no phrase."""
    now = now or datetime.now()
    t = (text or "").lower()
    day0 = datetime.combine(now.date(), datetime.min.time())
    if re.search(r"\blast night\b", t):
        y = day0 - timedelta(days=1)
        return y + timedelta(hours=17), day0 + timedelta(hours=5), "last night"
    if re.search(r"\b(day before yesterday)\b", t):
        return day0 - timedelta(days=2), day0 - timedelta(days=1), "the day before yesterday"
    if re.search(r"\b(yesterday|நேற்று|netru|nethu)\b", t) or "நேற்று" in t:
        return day0 - timedelta(days=1), day0, "yesterday"
    if re.search(r"\b(today|this morning|earlier today|indru)\b", t) or "இன்று" in t:
        return day0, now + timedelta(seconds=1), "today"
    m = re.search(r"\b(\d{1,2}|a|one|two|three|four|five|six|seven) days? ago\b", t)
    if m:
        n = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}.get(m.group(1))
        n = n or int(m.group(1))
        s = day0 - timedelta(days=n)
        return s, s + timedelta(days=1), f"{n} day{'s' if n != 1 else ''} ago"
    if re.search(r"\bthis week\b", t):
        s = day0 - timedelta(days=now.weekday())
        return s, now + timedelta(seconds=1), "this week"
    if re.search(r"\blast week\b", t):
        s = day0 - timedelta(days=now.weekday() + 7)
        return s, s + timedelta(days=7), "last week"
    m = re.search(r"\b(\d{1,2}|a|one|two|three|four) weeks? ago\b", t)
    if m:
        n = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4}.get(m.group(1)) or int(m.group(1))
        s = day0 - timedelta(days=now.weekday() + 7 * n)
        return s, s + timedelta(days=7), f"{n} week{'s' if n != 1 else ''} ago"
    if re.search(r"\bthis month\b", t):
        return day0.replace(day=1), now + timedelta(seconds=1), "this month"
    m = re.search(r"\b(?:last month|a month ago|(\d{1,2}|one|two|three|six) months? ago)\b", t)
    if m:
        n = {"one": 1, "two": 2, "three": 3, "six": 6}.get(m.group(1) or "", 0) or int(m.group(1) or 1)
        y, mo = day0.year, day0.month - n
        while mo <= 0:
            mo += 12
            y -= 1
        s = day0.replace(year=y, month=mo, day=1)
        e_y, e_mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        label = "last month" if n == 1 else f"{n} months ago"
        return s, s.replace(year=e_y, month=e_mo), label
    if re.search(r"\b(recently|lately|last time|previous(?:ly)?)\b", t):
        return day0 - timedelta(days=14), now + timedelta(seconds=1), "recently"
    for i, wd in enumerate(_WEEKDAYS):
        if re.search(rf"\b(?:on |last )?{wd}\b", t):
            back = (now.weekday() - i) % 7 or 7
            s = day0 - timedelta(days=back)
            return s, s + timedelta(days=1), wd.capitalize()
    return None


class EpisodicStore:
    def __init__(self, path: Path | None = None, clock=time.time):
        if path is None:
            from core.secret_store import base_dir
            path = base_dir() / "memory" / "episodes.jsonl"
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: list[dict] | None = None

    # -- persistence ------------------------------------------------------------
    def _load(self) -> list[dict]:
        if self._cache is not None:
            return self._cache
        items: list[dict] = []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if isinstance(d, dict) and d.get("id"):
                            items.append(d)
                    except Exception:
                        continue
        except FileNotFoundError:
            pass
        except Exception:
            pass
        self._cache = items
        return items

    def _rewrite(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".episodes.", dir=str(self.path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            for d in items:
                f.write(json.dumps(d, ensure_ascii=False) + "\r\n")
        os.replace(tmp, self.path)
        self._cache = items

    def _prune(self, items: list[dict]) -> list[dict]:
        now = datetime.fromtimestamp(self._clock())
        keep = []
        for d in items:
            try:
                age = (now - datetime.fromisoformat(d["ts"])).days
            except Exception:
                age = 0
            if age <= RETENTION_DAYS.get(d.get("kind", ""), DEFAULT_RETENTION):
                keep.append(d)
        return keep[-MAX_RECORDS:]

    # -- write --------------------------------------------------------------------
    def add(self, kind: str, title: str, summary: str = "", *, project: str | None = None,
            tags: Iterable[str] = (), refs: dict | None = None, outcome: str = "",
            coalesce_s: float = 0) -> dict | None:
        kind = kind if kind in KINDS else "event"
        title = _scrub(title, TITLE_MAX)
        if not title:
            return None
        ts = datetime.fromtimestamp(self._clock())
        rec = {"id": "ep_" + uuid.uuid4().hex[:10], "ts": ts.isoformat(timespec="seconds"),
               "kind": kind, "title": title, "summary": _scrub(summary, SUMMARY_MAX),
               "project": (project or None), "tags": [str(t)[:30] for t in list(tags)[:8]],
               "refs": _clean_refs(refs), "outcome": str(outcome or "")[:20]}
        with self._lock:
            items = list(self._load())
            if coalesce_s and items:
                last = items[-1]
                try:
                    close = (ts - datetime.fromisoformat(last["ts"])).total_seconds() < coalesce_s
                except Exception:
                    close = False
                if close and last.get("kind") == kind and last.get("title") == title:
                    last["ts"] = rec["ts"]
                    last["count"] = int(last.get("count", 1)) + 1
                    self._rewrite(items)
                    return last
            items.append(rec)
            if len(items) > MAX_RECORDS or len(items) % 50 == 0:
                items = self._prune(items)
                self._rewrite(items)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\r\n")
                self._cache = items
        return rec

    def forget(self, episode_id: str) -> bool:
        with self._lock:
            items = self._load()
            keep = [d for d in items if d.get("id") != episode_id]
            if len(keep) == len(items):
                return False
            self._rewrite(keep)
            return True

    def forget_project(self, project: str) -> int:
        with self._lock:
            items = self._load()
            keep = [d for d in items if (d.get("project") or "").lower() != project.lower()]
            n = len(items) - len(keep)
            if n:
                self._rewrite(keep)
            return n

    def clear(self) -> int:
        with self._lock:
            n = len(self._load())
            self._rewrite([])
            return n

    # -- read ---------------------------------------------------------------------
    def all(self) -> list[dict]:
        with self._lock:
            return list(self._load())

    def recent(self, n: int = 10, kind: str | None = None, project: str | None = None) -> list[dict]:
        items = [d for d in self.all()
                 if (kind is None or d.get("kind") == kind)
                 and (project is None or (d.get("project") or "").lower() == project.lower())]
        items.sort(key=lambda d: d.get("ts", ""))
        return items[-n:]

    def between(self, start: datetime, end: datetime) -> list[dict]:
        out = []
        for d in self.all():
            try:
                ts = datetime.fromisoformat(d["ts"])
            except Exception:
                continue
            if start <= ts < end:
                out.append(d)
        return out

    def search(self, query: str = "", *, limit: int = 6, period: tuple | None = None,
               kinds: Iterable[str] | None = None, project: str | None = None) -> list[tuple[float, dict]]:
        """Scored matches, best first. Recency always contributes, so an empty
        query returns the most recent episodes of the period."""
        q = set(_words(query))
        now = datetime.fromtimestamp(self._clock())
        kinds = set(kinds) if kinds else None
        pool = self.between(period[0], period[1]) if period else self.all()
        scored = []
        for d in pool:
            if kinds and d.get("kind") not in kinds:
                continue
            if project and (d.get("project") or "").lower() != project.lower():
                continue
            text = " ".join([d.get("title", ""), d.get("summary", ""), d.get("project") or "",
                             " ".join(d.get("tags") or [])])
            words = set(_words(text))
            overlap = len(q & words)
            if q and not overlap and not period:
                continue
            try:
                age_h = max(0.0, (now - datetime.fromisoformat(d["ts"])).total_seconds() / 3600)
            except Exception:
                age_h = 9999
            recency = 1.0 / (1.0 + age_h / 24.0)
            weight = {"session": 1.2, "task": 1.3, "artifact": 1.3, "decision": 1.4}.get(d.get("kind"), 0.8)
            score = (overlap * 2.0 + recency) * weight
            scored.append((round(score, 4), d))
        scored.sort(key=lambda x: (x[0], x[1].get("ts", "")), reverse=True)
        return scored[:limit]

    def last_project(self, period: tuple | None = None) -> Optional[str]:
        pool = self.between(period[0], period[1]) if period else self.recent(200)
        c = Counter()
        for i, d in enumerate(pool):
            if d.get("project"):
                c[d["project"]] += 1 + i / 1000.0      # ties → the more recent one
        if not c:
            return None
        return max(c.items(), key=lambda kv: kv[1])[0]

    def stats(self) -> dict:
        items = self.all()
        c = Counter(d.get("kind") for d in items)
        return {"total": len(items), **dict(c),
                "projects": sorted({d["project"] for d in items if d.get("project")})[:20]}


def _clean_refs(refs: dict | None) -> dict:
    if not refs:
        return {}
    out = {}
    for k, v in refs.items():
        if k in ("task_id", "workflow", "tool", "url", "app"):
            out[k] = str(v)[:200]
        elif k == "paths" and isinstance(v, (list, tuple)):
            out[k] = [str(p)[:260] for p in v[:6]]
    return out


def fmt_when(ts: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    try:
        d = datetime.fromisoformat(ts)
    except Exception:
        return ts
    days = (now.date() - d.date()).days
    hm = d.strftime("%I:%M %p").lstrip("0")
    if days == 0:
        return f"today {hm}"
    if days == 1:
        return f"yesterday {hm}"
    if days < 7:
        return f"{d.strftime('%A')} {hm}"
    return d.strftime("%d %b")
