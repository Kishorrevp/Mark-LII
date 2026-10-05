"""
memory/policy.py — deciding WHAT deserves to be remembered.

"Not everything should be saved blindly." The Live model is told to call
save_memory when the user reveals something personal; this policy sits between
that call and the disk and answers one question per fact:

    reject     credentials, codes, card/ID numbers (never stored, even if asked)
               or a value too vague to be useful ("yes", "that")
    episodic   true only for now — "I'm tired today", "meeting at 4 tomorrow",
               "currently debugging the login page" → an EPISODE (dated, expires)
               instead of a permanent fact
    ask        a sensitive-category fact (health, money, religion, politics …)
               that the user did not explicitly ask to be remembered → JARVIS
               asks first; saved only if the user says yes (explicit=true)
    duplicate  already known with the same value → nothing written
    store      a durable fact → SEMANTIC memory (long_term.json), and the
               reply says whether it was new or an update ("was: …")

Dependency-free; unit-tested by real execution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from memory.memory_hub import is_sensitive

_TRANSIENT = re.compile(
    r"\b(today|tonight|tomorrow|right now|at the moment|this (?:morning|afternoon|evening|week|"
    r"weekend)|for now|later today|in an hour|in \d+ (?:minutes?|hours?)|next hour|just now|"
    r"is (?:tired|busy|sick|hungry|sleepy|bored|angry|sad|stressed)|feel(?:s|ing)? (?:tired|sick|sleepy|"
    r"bored|sad|stressed)|at \d{1,2}(?::\d{2})?\s*(?:am|pm))\b", re.I)
_SPECIAL = {
    "health": re.compile(r"\b(diagnos\w*|disease|illness|disorder|medication|medicine|prescription|"
                         r"therapy|therapist|surgery|allerg\w*|diabet\w*|depress\w*|anxiety|pregnan\w*|"
                         r"blood (?:pressure|sugar)|doctor said)\b", re.I),
    "finance": re.compile(r"\b(salary|income|net worth|debt|loan|emi|credit score|bank balance|"
                          r"savings of|earns?|owes?)\b", re.I),
    "beliefs": re.compile(r"\b(religio\w*|caste|political|votes? for|party member|sexual\w*|"
                          r"orientation)\b", re.I),
}
_VAGUE = {"yes", "no", "ok", "okay", "that", "this", "it", "something", "stuff", "things", "none",
          "n/a", "na", "unknown", "?"}
VALID_CATEGORIES = ("identity", "preferences", "projects", "relationships", "wishes", "notes")


@dataclass
class PolicyDecision:
    action: str                 # store | episodic | ask | duplicate | reject
    category: str
    key: str
    value: str
    reason: str
    previous: Optional[str] = None
    special: Optional[str] = None

    @property
    def stored(self) -> bool:
        return self.action in ("store", "episodic")


def _norm_key(key: str) -> str:
    k = re.sub(r"[^\w]+", "_", str(key or "").strip().lower()).strip("_")
    return k[:60]


def _norm_cat(cat: str) -> str:
    c = str(cat or "notes").strip().lower()
    if c in ("preference", "user_preferences"):
        c = "preferences"
    if c in ("relationship", "people", "person", "family"):
        c = "relationships"
    if c in ("project",):
        c = "projects"
    if c in ("goal", "goals", "wish", "plans"):
        c = "wishes"
    return c if c in VALID_CATEGORIES else "notes"


def evaluate(category: str, key: str, value: str, *, explicit: bool = False,
             existing: Optional[dict] = None) -> PolicyDecision:
    """`existing` is the loaded long_term.json dict (for duplicate/update detection)."""
    cat, k = _norm_cat(category), _norm_key(key)
    v = re.sub(r"\s+", " ", str(value or "")).strip()
    if not k or not v:
        return PolicyDecision("reject", cat, k, v, "missing key or value")
    bad, why = is_sensitive(f"{key} {value}")
    if bad:
        return PolicyDecision("reject", cat, k, v, f"never stored: {why}")
    if v.lower().strip(".!") in _VAGUE or len(v) < 2:
        return PolicyDecision("reject", cat, k, v, "too vague to be useful")
    if len(v) > 380:
        v = v[:377] + "…"

    text = f"{k.replace('_', ' ')} {v}"
    for name, rx in _SPECIAL.items():
        if rx.search(text) and not explicit:
            return PolicyDecision("ask", cat, k, v,
                                  f"this is {name}-related; ask the user whether to remember it",
                                  special=name)

    if (_TRANSIENT.search(text) or re.search(r"(^|_)(current|now|today|tonight|tomorrow)(_|$)", k)) \
            and cat not in ("wishes",):
        return PolicyDecision("episodic", cat, k, v, "only true for now — kept as a dated episode")

    prev = None
    if existing:
        entry = (existing.get(cat) or {}).get(k)
        if entry is not None:
            prev = entry.get("value") if isinstance(entry, dict) else str(entry)
            if str(prev).strip().lower() == v.lower():
                return PolicyDecision("duplicate", cat, k, v, "already remembered", previous=prev)
    return PolicyDecision("store", cat, k, v, "updated" if prev else "new fact", previous=prev)


def apply(decision: PolicyDecision, *, episodic=None, update_memory=None, write_fact=None,
          source: str = "tool", said: str = "") -> str:
    """Carry out a decision; returns the tool-result text for the model.

    Durable facts go through memory_manager.write_fact (atomic write + re-read
    verification) unless a test injects `update_memory`. A write that did not
    land returns "NOT saved — …" so the model can never confirm a save that
    did not happen."""
    d = decision
    if d.action == "reject":
        if d.reason.startswith("never stored"):
            return (f"Not saved — {d.reason}. Tell the user you never store passwords, codes, keys "
                    f"or account numbers.")
        return f"Not saved — {d.reason}."
    if d.action == "ask":
        return (f"Not saved yet — {d.reason}. Ask: 'Would you like me to remember that?' and only if "
                f"they say yes call save_memory again with explicit=true.")
    if d.action == "duplicate":
        return "Already remembered — nothing changed."
    if d.action == "episodic":
        rec = None
        if episodic is not None:
            try:
                rec = episodic.add("event", f"{d.key.replace('_', ' ')}: {d.value}", tags=[d.category, "noted"])
            except Exception as e:
                return (f"NOT saved — the episode store failed ({type(e).__name__}). Tell the user it "
                        f"could not be noted.")
        if episodic is not None and not rec:
            return "NOT saved — the note was empty after removing sensitive parts."
        return ("Noted for now (kept as a dated episode, not a permanent fact). If it is a "
                "time-bound plan, offer to set a reminder.")
    if update_memory is None:
        if write_fact is None:
            from memory.memory_manager import write_fact as _wf
            write_fact = _wf
        r = write_fact(d.category, d.key, d.value, source=source, said=said)
        if not r.ok:
            return (f"NOT saved — {r.error}. Tell the user the memory write failed; do not say it was "
                    f"remembered.")
        if d.previous:
            return f"ok — updated {d.category}/{d.key} (was: {d.previous})."
        return "ok"
    update_memory({d.category: {d.key: {"value": d.value}}})
    if d.previous:
        return f"ok — updated {d.category}/{d.key} (was: {d.previous})."
    return "ok"
