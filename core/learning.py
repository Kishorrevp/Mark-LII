"""
core/learning.py — JARVIS learns from you, day to day: what you do, how you do
it, and how you like things done.

Three kinds of knowledge, all visible and forgettable (Settings → Mission
Control → Learning, or "what have you learned about me" / "forget that"):

  1. HOW YOU LIKE IT — things you SAY about how you want JARVIS to work:
     "always answer in Tamil", "don't open a new tab", "make it shorter",
     "next time put it in Documents", "I prefer dark designs", and corrections
     ("no, I meant the PDF", "that's wrong — use Chrome"). Stored in your own
     words, counted when repeated, used in every conversation from then on.

  2. WHAT YOU DO — habits seen in your direct commands, by time of day and
     weekday ("usually opens VS Code around 09:00 on weekdays"). A habit needs
     at least 3 occurrences on at least 2 different days before it counts.

  3. HOW YOU DO IT — the multi-step routines core/workflows.py already spots
     ("you often find a file, rename it, then email it") — referenced here.

What is NEVER learned: anything memory/memory_hub.is_sensitive() flags
(passwords, codes, account numbers, health details), window contents, or
anything from web pages / documents / tool results (only the user's own
words count as preferences). "pause learning" stops all of it.

prompt_block() is the short "[HOW THE USER LIKES THINGS]" section given to
the voice model and the task planner. It is guidance about style and habits —
it can never widen what JARVIS is allowed to do (permissions, confirmations
and the safety policy are applied in code, after the model).

Dependency-free; tested by real execution. Stored in memory/learning.json
(CRLF, value-free of secrets by construction).
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

MAX_PREFS = 80
MAX_EVENTS = 1500
HABIT_MIN = 3
HABIT_DAYS = 2

# what a preference / instruction about JARVIS's behaviour sounds like
_PREF = re.compile(
    r"\b(i (?:prefer|like it when|like|love|want you to|need you to|hate|don'?t like)|always|never|"
    r"don'?t (?:ever |you )?|do not|please don'?t|next time|from now on|in (?:the )?future|stop (?:doing|saying|"
    r"asking)|make (?:it|them|the \w+) (?:shorter|longer|simpler|brief|briefer|detailed|more|less)|"
    r"(?:keep|be) (?:it )?(?:short|brief|concise)|call me|speak (?:in|slower|faster)|reply in|answer in|"
    r"talk (?:in|to me in)|use (?:chrome|edge|firefox|brave|vs ?code|notepad|word|excel|dark|light)|"
    r"by default|whenever i|every time i|when i say)\b", re.I)
_CORRECTION = re.compile(r"^\s*(?:no[,.!]?\s|nope|not that|wrong|that'?s (?:wrong|not)|i (?:said|meant)|"
                         r"actually[, ]|you (?:got it wrong|misunderstood)|that isn'?t what)", re.I)
_TOO_SHORT = 12
_QUESTION_ONLY = re.compile(r"^\s*(?:what|who|when|where|why|how|which|is|are|can|could|do|does|will)\b[^.!]*\?\s*$",
                            re.I)


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9஀-௿]{3,}", (s or "").lower())}


def _similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return a.strip().lower() == b.strip().lower()
    return len(wa & wb) / len(wa | wb) >= 0.6


class Learner:
    def __init__(self, path: Optional[Path] = None, clock: Callable[[], float] = time.time,
                 sensitive: Optional[Callable[[str], bool]] = None):
        if path is None:
            from core.secret_store import base_dir
            path = base_dir() / "memory" / "learning.json"
        self.path = Path(path)
        self._clock = clock
        self._sensitive = sensitive
        self._lock = threading.Lock()
        self._data: Optional[dict] = None

    # ── storage ─────────────────────────────────────────────────────────────
    def _load(self) -> dict:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_bytes().decode("utf-8"))
            except Exception:
                self._data = {}
            self._data.setdefault("paused", False)
            self._data.setdefault("preferences", [])
            self._data.setdefault("events", [])
        return self._data

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(json.dumps(self._data, indent=1, ensure_ascii=False).replace("\n", "\r\n")
                            .encode("utf-8"))
            tmp.replace(self.path)
        except Exception:
            pass

    def _is_sensitive(self, text: str) -> bool:
        if self._sensitive is not None:
            return bool(self._sensitive(text))
        try:
            from memory.memory_hub import is_sensitive
            return bool(is_sensitive(text)[0])
        except Exception:
            return False

    @property
    def paused(self) -> bool:
        with self._lock:
            return bool(self._load().get("paused"))

    def pause(self, on: bool = True) -> str:
        with self._lock:
            self._load()["paused"] = bool(on)
            self._save()
        return "Learning is paused — I won't pick up new habits or preferences." if on else \
            "Learning is back on."

    # ── 1. what the user says ───────────────────────────────────────────────
    def observe_utterance(self, text: str, tools: list | tuple = ()) -> Optional[dict]:
        """A finished user turn (their words) → a preference / correction, or None."""
        t = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(t) < _TOO_SHORT or len(t) > 400 or _QUESTION_ONLY.match(t):
            return None
        kind = "correction" if _CORRECTION.match(t) else ("preference" if _PREF.search(t) else "")
        if not kind:
            return None
        if self._is_sensitive(t):
            return None
        if {"remember", "save_memory"} & set(tools or ()):
            return None                       # an explicit memory save already handled it
        now = self._clock()
        with self._lock:
            d = self._load()
            if d.get("paused"):
                return None
            for p in d["preferences"]:
                if _similar(p["text"], t):
                    p["count"] = int(p.get("count", 1)) + 1
                    p["last"] = now
                    if len(t) > len(p["text"]):
                        p["text"] = t[:240]
                    self._save()
                    return p
            rec = {"id": f"pref-{int(now * 1000) % 10_000_000:07d}", "text": t[:240], "kind": kind, "count": 1,
                   "first": now, "last": now}
            d["preferences"].append(rec)
            if len(d["preferences"]) > MAX_PREFS:            # forget the stalest single mentions first
                d["preferences"].sort(key=lambda p: (p.get("count", 1) > 1, p.get("last", 0)))
                d["preferences"] = d["preferences"][-MAX_PREFS:]
            self._save()
            return rec

    # ── 2. what the user does ───────────────────────────────────────────────
    @staticmethod
    def _label(tool: str, args: dict) -> str:
        a = args or {}
        if tool in ("open_app", "app.open"):
            return f"open {str(a.get('app_name') or a.get('app') or '').strip().lower()}"
        if tool == "youtube_video":
            return "play YouTube"
        if tool == "web_search":
            return "search the web"
        if tool == "weather_report":
            return "check the weather"
        if tool in ("computer_settings",):
            return f"{str(a.get('action') or 'settings').replace('_', ' ')}"
        if tool == "browser_control":
            return f"browser {str(a.get('action') or '').replace('_', ' ')}".strip()
        if tool == "system_status":
            return "check the PC's status"
        if tool in ("run_task", "draw", "reference", "watch_screen", "send_message", "reminder"):
            return tool.replace("_", " ")
        return ""

    def observe_action(self, tool: str, args: dict, ok: bool = True) -> None:
        if not ok:
            return
        label = self._label(tool, args)
        if not label or label.endswith(" "):
            return
        now = self._clock()
        lt = time.localtime(now)
        with self._lock:
            d = self._load()
            if d.get("paused"):
                return
            d["events"].append({"a": label[:60], "h": lt.tm_hour, "wd": lt.tm_wday,
                                "day": time.strftime("%Y-%m-%d", lt)})
            if len(d["events"]) > MAX_EVENTS:
                d["events"] = d["events"][-MAX_EVENTS:]
            self._save()

    def habits(self) -> list[dict]:
        with self._lock:
            ev = list(self._load()["events"])
        out = []
        by = {}
        for e in ev:
            by.setdefault(e["a"], []).append(e)
        for action, es in by.items():
            if len(es) < HABIT_MIN or len({e["day"] for e in es}) < HABIT_DAYS:
                continue
            hours = Counter(e["h"] for e in es)
            # the peak hour, and how many fall within an hour of it
            best_h = max(range(24), key=lambda h: (hours.get(h, 0), -h))
            near = sum(hours.get((best_h + d) % 24, 0) for d in (-1, 0, 1))
            share = near / len(es)
            wd = Counter(e["wd"] for e in es)
            weekday_share = sum(n for d_, n in wd.items() if d_ < 5) / len(es)
            when = f"around {best_h:02d}:00" if share >= 0.6 else "at varied times"
            days = " on weekdays" if weekday_share >= 0.85 else (" at weekends" if weekday_share <= 0.15 else "")
            out.append({"action": action, "count": len(es), "days": len({e['day'] for e in es}),
                        "text": f"usually {action} {when}{days}", "hour": best_h if share >= 0.6 else None})
        return sorted(out, key=lambda h: -h["count"])[:12]

    def due_now(self, now: Optional[float] = None) -> list[str]:
        """Habits whose usual time is now (for a gentle proactive offer)."""
        h = time.localtime(now if now is not None else self._clock()).tm_hour
        return [x["action"] for x in self.habits() if x.get("hour") is not None and abs(x["hour"] - h) <= 1]

    # ── reading / forgetting ────────────────────────────────────────────────
    def preferences(self) -> list[dict]:
        with self._lock:
            return sorted(self._load()["preferences"], key=lambda p: (-int(p.get("count", 1)), -p.get("last", 0)))

    def forget(self, ident: str) -> bool:
        """By id ('pref-…'), by words of the preference, or a habit's action."""
        s = str(ident or "").strip()
        with self._lock:
            d = self._load()
            before = (len(d["preferences"]), len(d["events"]))
            d["preferences"] = [p for p in d["preferences"]
                                if not (p["id"] == s or (s and not s.startswith("pref-") and _similar(p["text"], s)))]
            d["events"] = [e for e in d["events"] if e["a"] != s.lower()]
            changed = (len(d["preferences"]), len(d["events"])) != before
            if changed:
                self._save()
            return changed

    def forget_all(self) -> None:
        with self._lock:
            d = self._load()
            d["preferences"], d["events"] = [], []
            self._save()

    def prompt_block(self, max_chars: int = 1200) -> str:
        if self.paused:
            return ""
        prefs = self.preferences()[:12]
        habits = self.habits()[:5]
        if not prefs and not habits:
            return ""
        lines = ["[HOW THE USER LIKES THINGS — learned from what they said and did; follow it where it fits. "
                 "It never overrides safety rules or confirmations.]"]
        for p in prefs:
            tag = "correction" if p.get("kind") == "correction" else "said"
            lines.append(f"- ({tag}{', ×' + str(p['count']) if p.get('count', 1) > 1 else ''}) \"{p['text']}\"")
        for h in habits:
            lines.append(f"- habit: {h['text']}")
        out, n = [], 0
        for ln in lines:
            if n + len(ln) > max_chars:
                break
            out.append(ln)
            n += len(ln) + 1
        return "\n".join(out)

    def summary(self) -> str:
        prefs, habits = self.preferences(), self.habits()
        if not prefs and not habits:
            return ("I haven't learned any preferences or habits yet." +
                    (" (Learning is paused.)" if self.paused else ""))
        bits = []
        if prefs:
            bits.append("How you like things: " + "; ".join(f"\"{p['text'][:80]}\"" for p in prefs[:6]))
        if habits:
            bits.append("Habits: " + "; ".join(h["text"] for h in habits[:4]))
        return ". ".join(bits) + ("." if bits else "") + (" (Learning is paused.)" if self.paused else "")


_LEARNER: Optional[Learner] = None


def get_learner() -> Learner:
    global _LEARNER
    if _LEARNER is None:
        _LEARNER = Learner()
    return _LEARNER


def set_learner(learner: Optional[Learner]) -> None:
    global _LEARNER
    _LEARNER = learner


def t_learning(args: dict, ctx):
    from core.tools.registry import ToolResult
    L = get_learner()
    action = str(args.get("action") or "list").strip().lower()
    if action in ("list", "show", "summary"):
        return ToolResult(True, {"preferences": L.preferences()[:20], "habits": L.habits()}, L.summary())
    if action in ("forget", "delete", "remove"):
        target = str(args.get("target") or "").strip()
        if target in ("all", "everything"):
            L.forget_all()
            return ToolResult(True, {}, "Forgot every learned preference and habit.")
        ok = L.forget(target)
        return ToolResult(ok, {}, f"Forgot \"{target}\"." if ok else "", "" if ok else "Nothing matched that.")
    if action in ("pause", "stop"):
        return ToolResult(True, {}, L.pause(True))
    if action in ("resume", "start"):
        return ToolResult(True, {}, L.pause(False))
    return ToolResult.failure(f"Unknown learning action '{action}'.")


def register(reg) -> None:
    from core.permissions import RiskLevel
    from core.tools.registry import ToolSpec
    reg.register(ToolSpec(
        "learning", "What JARVIS has learned about how the user likes things and their habits: action list | "
        "forget (target = words / id / 'all') | pause | resume.", t_learning,
        {"type": "object", "properties": {"action": {"type": "string"}, "target": {"type": "string"}},
         "required": []}, {"preferences": "list"}, agent="memory",
        risk=lambda a: RiskLevel.MEDIUM_RISK if str(a.get("action") or "").lower() in ("forget", "delete", "remove")
        else RiskLevel.SAFE, retry_safe=True))
