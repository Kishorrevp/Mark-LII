"""
core/self_model.py — JARVIS's model of ITSELF.

The personal model (memory/identity.py) knows the user. This knows the
assistant: its identity, what it can and cannot do right now (from the live
tool registry and model health, not a hand-kept list), and the honest limits
it must own up to.

    SelfModel(core).identity()      one-paragraph "who/what I am" — always says
                                    it is an AI assistant, never claims to be a
                                    person or to have done something it didn't.
    SelfModel(core).capabilities()  grouped, live: which agents/tools exist,
                                    plus what's degraded (image gen off, no
                                    calendar, phone not built) from real state.
    SelfModel(core).answer(q)       routes "what can you do?", "who are you?",
                                    "can you <X>?" to a truthful answer built
                                    from the above.

This is deliberately fact-derived: "can you generate images?" checks the image
model's health on the real key rather than guessing, so JARVIS never claims a
capability it doesn't actually have (a core safety rule).
"""
from __future__ import annotations

import re
from typing import Optional

NAME = "JARVIS"
ONE_LINER = ("JARVIS — a voice-controlled AI assistant that runs on this Windows PC: it listens for a wake "
             "word, understands English, Tamil and Tanglish, and carries out real tasks with your confirmation "
             "on anything risky.")

# agent name → what the user would call that capability
_AGENT_BLURB = {
    "laptop": "open apps and control the desktop",
    "file": "find, rename, move, read and summarise files and documents",
    "browser": "drive the web browser and read pages",
    "coding": "write and fix code, and build small projects",
    "research": "search the web, gather news, weather and do research",
    "communication": "prepare messages (and send them once you confirm)",
    "system": "adjust volume, brightness and report system health",
    "vision": "look at your screen or camera and describe what's there",
    "reminder": "set reminders",
    "ui": "design and draw (vector art; raster when the model allows)",
    "manager": "run a team of agents to build a website",
    "webdev": "run and check a website it built",
    "plugin": "use installed plugins",
}


class SelfModel:
    def __init__(self, core=None):
        self.core = core

    # -- identity ---------------------------------------------------------------
    def identity(self) -> str:
        return (f"I'm {NAME}, {ONE_LINER[len(NAME) + 3:]} I'm an AI assistant — not a person — and I run "
                "locally for you; I won't pretend to have done something I haven't, and I ask before anything "
                "irreversible.")

    # -- capabilities (live) ----------------------------------------------------
    def _registry(self):
        return getattr(self.core, "registry", None)

    def capabilities(self) -> dict:
        reg = self._registry()
        agents: dict[str, int] = {}
        if reg is not None:
            for t in reg.list():
                if getattr(t, "planner_visible", True):
                    agents[t.agent] = agents.get(t.agent, 0) + 1
        can = [f"{_AGENT_BLURB[a]}" for a in _AGENT_BLURB if a in agents]
        return {"can": can, "limits": self.limits(), "agents": agents}

    def limits(self) -> list[str]:
        out: list[str] = []
        # image generation — check the real key
        try:
            from core.ai_gateway import get_gateway
            gw = get_gateway()
            if not gw.tier_available("image"):
                out.append("I can't generate raster images on your current key (no free image quota) — I draw "
                           "vector art instead.")
        except Exception:
            pass
        out.append("I don't have calendar access yet.")
        out.append("I can't make phone calls or run on your phone yet (the companion app isn't built).")
        out.append("I never run terminal commands or handle passwords for you — you do those yourself.")
        return out

    def can(self, query: str) -> Optional[bool]:
        """Best-effort yes/no for 'can you <X>?' — None when unsure."""
        q = query.lower()
        if re.search(r"\b(generate|create|make|draw) .*\bimage|picture|photo\b", q) or "image generation" in q:
            try:
                from core.ai_gateway import get_gateway
                return bool(get_gateway().tier_available("image"))
            except Exception:
                return None
        if re.search(r"\b(call|phone call|ring)\b", q):
            return False
        if re.search(r"\bcalendar|schedule a meeting\b", q):
            return False
        if re.search(r"\brun .*(command|terminal|shell|cmd)\b", q):
            return False
        reg = self._registry()
        if reg is not None:
            for kw, ok in (("draw", True), ("website", True), ("code", True), ("search", True),
                           ("remind", True), ("screen", True), ("file", True)):
                if kw in q:
                    return ok
        return None

    def answer(self, query: str = "") -> str:
        q = (query or "").lower()
        if re.search(r"\bwho are you\b|\bwhat are you\b|your name\b|are you (?:human|a person|real)\b", q):
            return self.identity()
        yn = self.can(q) if re.search(r"\bcan you\b|\bare you able\b|\bdo you (?:support|do)\b", q) else None
        cap = self.capabilities()
        if yn is True:
            return "Yes — that's something I can do. " + self._can_sentence(cap)
        if yn is False:
            lim = next((l for l in cap["limits"] if any(w in l.lower() for w in q.split())), None)
            return (lim or "That's outside what I can do right now.") + " Is there another way I can help?"
        # "what can you do?" / anything else
        return self._can_sentence(cap) + " " + (cap["limits"][0] if cap["limits"] else "")

    @staticmethod
    def _can_sentence(cap: dict) -> str:
        cans = cap["can"]
        if not cans:
            return "I can help with everyday tasks on this PC."
        head = "; ".join(cans[:6])
        return f"I can {head}. Just say the word — I plan multi-step jobs and check my own work before I call it done."


# -- proactive: offer to resume unfinished work -------------------------------

def unfinished_work_source(store, offered_ids=None):
    """A ProactiveSource that offers, at most once each, to resume a task that
    was interrupted (FAILED with steps done, or left non-terminal by a restart).
    Never nags: each task is offered once, and only when there's real progress
    worth resuming."""
    from core.proactive_policy import Priority, ProactiveSource
    from core.tasks.state import StepStatus, TaskState

    seen = offered_ids if offered_ids is not None else set()

    class UnfinishedWork(ProactiveSource):
        name = "unfinished_work"
        interval_s = 180.0
        priority = Priority.LOW

        def check(self):
            for t in reversed(store.all()):
                if t.id in seen:
                    continue
                if t.state in (TaskState.FAILED,) and t.origin != "proactive":
                    done = [s for s in t.steps if s.status == StepStatus.DONE]
                    pending = [s for s in t.steps if s.status in (StepStatus.SKIPPED, StepStatus.PENDING,
                                                                  StepStatus.FAILED)]
                    if done and pending:
                        seen.add(t.id)
                        return (f"Earlier, task {t.id} (\"{t.request[:60]}\") stopped after finishing "
                                f"{len(done)} of {len(t.steps)} steps. Want me to pick it up where it left "
                                f"off? Say \"try again\".")
            return None

    return UnfinishedWork()
