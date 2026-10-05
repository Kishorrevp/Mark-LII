"""
memory/capture.py — "Remember that …" as a real, verified workflow.

    utterance ─► detect ─► extract ─► validate ─► persist ─► verify ─► index ─► confirm
                 phrase    category /  memory/     atomic      re-read    version    receipt the
                 intent    key / value policy.py   write       from disk  bump →     model speaks
                                                                          knowledge  (never "saved"
                                                                          sees it    unless it was)

Triggers (case-insensitive, start or middle of the sentence):
    remember that / remember this / remember: / please remember
    note this / note that / make a note / take a note / note down
    don't let me forget / do not let me forget / don't forget (that)
    keep this in mind / keep in mind that
    save this for later / save that for later
    Tamil / Tanglish: ஞாபகம் வச்சுக்கோ · nyabagam vachuko · marakkadhe · note pannu

Durable vs temporary:
    memory/policy.py decides. "my sister's name is Ayse" → a durable fact in
    long_term.json. "I have a meeting at 4 tomorrow" → only true for now → a
    dated episode (still immediately retrievable) + an offer to set a reminder.
    Secrets are refused even when the user says "remember". Health / money /
    beliefs are stored because the user explicitly asked.

Extraction is deterministic (no model call): "my X is Y" patterns become
category/key/value; anything else becomes a note keyed by its first content
words, with the full sentence kept as the value. The Live model may pass its
own category/key/value through the `remember` tool — the verbatim words are
always kept as provenance.

Dependency-free; tested by real execution.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

_TRIGGERS = [
    r"(?:please\s+)?remember(?:\s+(?:that|this|to|:))?",
    r"(?:please\s+)?note (?:this|that|down)(?:\s*:)?",
    r"(?:make|take) a note(?: (?:that|of this|of that))?(?:\s*:)?",
    r"(?:don'?t|do not) let me forget(?: (?:that|to))?",
    r"(?:don'?t|do not) forget(?: (?:that|this))?",
    r"keep (?:this|that) in mind(?:\s*:)?",
    r"keep in mind(?: that)?",
    r"save (?:this|that) for later(?:\s*:)?",
    r"ஞாபகம் வச்சுக்கோ",
    r"nyabagam vach(?:u|chu)ko",
    r"marakkad?he",
    r"note pannu",
]
_TRIGGER_RX = re.compile(r"(?i)(?:^|[\s,.!?;:—-])(" + "|".join(_TRIGGERS) + r")(?=$|[\s,.!?;:—-])")
# Questions ABOUT memory are not instructions to store something
_QUESTION_RX = re.compile(r"(?i)\b(what did i|do you remember|did i (?:tell|ask)|what (?:have|do) you "
                          r"remember|can you remember|remember when|remember what|remember (?:me|my name)\?)")
_NEG_RX = re.compile(r"(?i)\b(don'?t|do not|never) remember\b|\bforget (?:that|it|about)\b(?!.*\blet me\b)")

_REL = r"(?:sister|brother|mom|mother|dad|father|wife|husband|partner|girlfriend|boyfriend|son|daughter|" \
       r"friend|best friend|boss|manager|colleague|cousin|uncle|aunt|grandma|grandmother|grandpa|grandfather|" \
       r"teacher|neighbou?r|roommate)"
_PATTERNS = [
    # my sister's name is Ayse / my best friend is Ravi
    (re.compile(rf"(?i)^my ({_REL})(?:'s)? name is (.+)$"), "relationships", lambda m: (f"{m[1]}_name", m[2])),
    (re.compile(rf"(?i)^my ({_REL}) is (.+)$"), "relationships", lambda m: (m[1], m[2])),
    (re.compile(rf"(?i)^({_REL})(?:'s)? birthday is (.+)$"), "relationships", lambda m: (f"{m[1]}_birthday", m[2])),
    (re.compile(rf"(?i)^my ({_REL})(?:'s)? birthday is (.+)$"), "relationships",
     lambda m: (f"{m[1]}_birthday", m[2])),
    # my favourite food is dosa / I prefer dark mode / I like / I love / I hate
    (re.compile(r"(?i)^my favou?rite ([\w ]{2,30}?) is (.+)$"), "preferences", lambda m: (f"favorite_{m[1]}", m[2])),
    (re.compile(r"(?i)^i (prefer|like|love|hate|dislike|don'?t like) (.+)$"), "preferences",
     lambda m: (f"{m[1].replace(chr(39), '').replace(' ', '_')}_{_slug(m[2], 3)}", f"{m[1]} {m[2]}")),
    # my birthday is / my name is / I live in / I work as
    (re.compile(r"(?i)^my (name|birthday|city|job|age|nickname) is (.+)$"), "identity", lambda m: (m[1], m[2])),
    (re.compile(r"(?i)^i live in (.+)$"), "identity", lambda m: ("city", m[1])),
    (re.compile(r"(?i)^i (?:work as|am working as) (?:an? )?(.+)$"), "identity", lambda m: ("job", m[1])),
    # project / goals
    (re.compile(r"(?i)^(?:i am|i'm) (?:building|working on|making) (.+)$"), "projects",
     lambda m: (_slug(m[1], 3), m[1])),
    (re.compile(r"(?i)^i (?:want|plan|hope|wish) to (.+)$"), "wishes", lambda m: (_slug(m[1], 3), m[1])),
    # generic "my X is Y"
    (re.compile(r"(?i)^my ([\w' ]{2,40}?) (?:is|are) (.+)$"), "notes", lambda m: (_slug(m[1], 4), m[2])),
]

_STOP = set("""a an the and or of to in on for with at by from is are was were be been it this that these those
my our your me i you we us please jarvis that's its it's about into just so then also really very""".split())


def _slug(text: str, n: int = 4) -> str:
    words = [w for w in re.findall(r"[\w஀-௿]+", str(text or "").lower()) if w not in _STOP]
    return "_".join(words[:n])[:60] or "note"


@dataclass
class Intent:
    trigger: str
    payload: str
    text: str


@dataclass
class Extracted:
    category: str
    key: str
    value: str
    said: str


@dataclass
class CaptureResult:
    ok: bool                       # True only when stored AND verified (or kept as a dated episode)
    action: str                    # store | episodic | duplicate | ask | reject | error | none
    id: str = ""
    category: str = ""
    key: str = ""
    value: str = ""
    durability: str = ""           # durable | temporary
    ts: str = ""
    previous: Optional[str] = None
    message: str = ""              # what the model should say / do
    said: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def confirmation(self) -> str:
        """Tool-result text for the voice model."""
        if self.action == "store" and self.ok:
            upd = f" (updated — it was: {self.previous})" if self.previous else ""
            return (f"[MEMORY_SAVED id={self.id} verified=yes at={self.ts}] Stored {self.category}/"
                    f"{self.key.replace('_', ' ')} = {self.value}{upd}. Confirm in ONE short sentence what "
                    f"you will remember, in the user's language.")
        if self.action == "duplicate":
            return (f"[MEMORY_SAVED id={self.id} already-known] That was already remembered "
                    f"({self.value}). Say so briefly.")
        if self.action == "episodic" and self.ok:
            return (f"[MEMORY_SAVED id={self.id} temporary at={self.ts}] Kept as a dated note (it is only true "
                    f"for now): {self.value}. Confirm briefly and offer a reminder if it has a time.")
        if self.action == "ask":
            return f"[MEMORY_NOT_SAVED] {self.message}"
        return (f"[MEMORY_NOT_SAVED] {self.message} Tell the user plainly it was NOT saved and why. "
                f"Never say it was remembered.")


def detect(text: str) -> Optional[Intent]:
    """A "remember this" instruction in `text`, or None. Questions about
    memory ("what did I ask you to remember?") are not instructions."""
    s = str(text or "").strip()
    if not s or _QUESTION_RX.search(s) or _NEG_RX.search(s):
        return None
    m = _TRIGGER_RX.search(s)
    if not m:
        return None
    payload = s[m.end():].strip(" ,.:;—-")
    if not payload:                                    # "…the meeting is at 5. Remember that."
        payload = s[:m.start()].strip(" ,.:;—-")
    payload = re.sub(r"(?i)^(?:jarvis|friday)[, ]+", "", payload).strip()
    # trailing "…, okay" / "…, JARVIS" — the assistant's name only after a comma, so
    # "my bike service is on Friday" keeps its Friday
    payload = re.sub(r"(?i)(?:,\s*(?:okay|ok|please|jarvis|friday)|\s+(?:okay|ok|please))\s*[.!?]*$", "",
                     payload).strip(" .!?")
    if len(payload) < 3:
        return None
    return Intent(m.group(1), payload, s)


def _normalise_person(text: str) -> str:
    # "that my sister ..." → "my sister ..."; "I'm" stays
    t = re.sub(r"(?i)^(?:that|this|to)\s+", "", text.strip())
    return t[:1].lower() + t[1:] if t[:2].lower() in ("my", "i ") or t.lower().startswith("i'm") else t


def extract(payload: str, *, category: str = "", key: str = "", value: str = "") -> Extracted:
    """Category/key/value from the payload. Explicit arguments (from the
    model) win; the verbatim payload is always kept as `said`."""
    said = payload.strip()
    if category and key and value:
        return Extracted(category, key, value, said)
    p = _normalise_person(said).rstrip(" .!?")
    for rx, cat, fn in _PATTERNS:
        m = rx.match(p)
        if m:
            k, v = fn(m)
            k = re.sub(r"[^\w]+", "_", str(k).lower()).strip("_")[:60]
            v = str(v).strip(" .!?")
            if k and v:
                return Extracted(category or cat, key or k, value or v, said)
    return Extracted(category or "notes", key or _slug(p, 4), value or p[:380], said)


def capture(text: str, *, source: str = "voice", category: str = "", key: str = "", value: str = "",
            memory_hub=None, intent: Optional[Intent] = None, write_fact=None, load_memory=None,
            episodic=None) -> CaptureResult:
    """Run the whole workflow for one utterance. `text` may be the raw
    utterance (the trigger is found) or, when called from the remember tool,
    just the thing to remember."""
    from memory import policy
    it = intent or detect(text)
    payload = it.payload if it else str(text or "").strip()
    if not payload and not value:
        return CaptureResult(False, "none", message="There was nothing to remember.")
    ex = extract(payload or value, category=category, key=key, value=value)
    if write_fact is None or load_memory is None:
        from memory import memory_manager as _mm
        write_fact = write_fact or _mm.write_fact
        load_memory = load_memory or _mm.load_memory
    if episodic is None and memory_hub is not None:
        try:
            episodic = memory_hub.episodic
        except Exception:
            episodic = None
    try:
        existing = load_memory()
    except Exception:
        existing = {}
    d = policy.evaluate(ex.category, ex.key, ex.value, explicit=True, existing=existing)
    if d.action == "reject":
        why = d.reason.replace("never stored: ", "")
        msg = (f"I never store that — {why}." if d.reason.startswith("never stored")
               else f"Not saved — {d.reason}.")
        return CaptureResult(False, "reject", category=d.category, key=d.key, value=d.value, message=msg,
                             said=ex.said)
    if d.action == "ask":                      # explicit=True never asks, kept for completeness
        return CaptureResult(False, "ask", category=d.category, key=d.key, value=d.value,
                             message=d.reason, said=ex.said)
    if d.action == "duplicate":
        return CaptureResult(True, "duplicate", id=f"mem:{d.category}/{d.key}", category=d.category,
                             key=d.key, value=d.value, durability="durable", previous=d.previous,
                             message="already remembered", said=ex.said)
    if d.action == "episodic":
        if episodic is None:
            return CaptureResult(False, "error", message="the episode store is unavailable", said=ex.said)
        try:
            rec = episodic.add("event", f"{d.key.replace('_', ' ')}: {d.value}", ex.said,
                               tags=[d.category, "noted", source])
        except Exception as e:
            return CaptureResult(False, "error", message=f"the episode store failed ({type(e).__name__})",
                                 said=ex.said)
        if not rec:
            return CaptureResult(False, "error", message="nothing was left to note after removing sensitive parts",
                                 said=ex.said)
        # verify: it must be readable back
        try:
            ok = any(e.get("id") == rec.get("id") for e in episodic.recent(20))
        except Exception:
            ok = False
        if not ok:
            return CaptureResult(False, "error", message="the note did not appear in the episode store",
                                 said=ex.said)
        return CaptureResult(True, "episodic", id=str(rec.get("id")), category=d.category, key=d.key,
                             value=d.value, durability="temporary", ts=str(rec.get("ts", "")),
                             message="kept as a dated episode", said=ex.said)
    r = write_fact(d.category, d.key, d.value, source=source, durability="durable", said=ex.said)
    if not getattr(r, "ok", False):
        return CaptureResult(False, "error", id=getattr(r, "id", ""), category=d.category, key=d.key,
                             value=d.value, message=f"the memory write failed: {getattr(r, 'error', 'unknown')}",
                             said=ex.said)
    return CaptureResult(True, "store", id=r.id, category=r.category, key=r.key, value=r.value,
                         durability="durable", ts=r.ts, previous=r.previous, message="stored and verified",
                         said=ex.said)
