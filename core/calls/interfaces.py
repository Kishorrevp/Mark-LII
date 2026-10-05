"""
core/calls/interfaces.py — interfaces for the future Call Agent (ROADMAP Phase 7).

    Incoming call → identify caller → detect language → determine purpose →
    conversation → urgency assessment → message/summary → escalate to user

NOTHING HERE ANSWERS A PHONE CALL. There is no telephony integration in this
phase and TelephonyUnavailableAgent says so. What IS implemented, because it
is pure logic that the future agent must obey and that can be tested now:

  * CallPolicy + CallPolicyEngine
      - caller identification from the user's contact list
      - priority rules (VIP / family always escalate, blocked numbers never)
      - quiet-hours handling
      - AI DISCLOSURE: the opening line always states it is an AI assistant
        speaking on the user's behalf, in the caller's language
      - IMPERSONATION GUARD: check_utterance() rejects any line in which the
        assistant would claim to BE the user ("this is Architha speaking",
        "I am Architha") — it may only speak *for* the user
  * assess_urgency(): keyword-based urgency scoring in English, Tamil and
    Tanglish — a baseline the real agent can refine with a model
  * CallSummary: the structured record handed back to the user

The CallAgent ABC is the contract a Phase-7 implementation will fulfil.
"""
from __future__ import annotations

import enum
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime
from typing import Optional


class Urgency(enum.IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    EMERGENCY = 3


@dataclass
class IncomingCall:
    number: str
    at: datetime = field(default_factory=datetime.now)
    network_name: str = ""          # caller-ID name from the network, if any


@dataclass
class CallerIdentity:
    number: str
    name: str = ""
    known: bool = False
    relationship: str = ""          # family / friend / work / ...
    vip: bool = False
    blocked: bool = False


@dataclass
class UrgencyAssessment:
    level: Urgency
    reasons: list = field(default_factory=list)


@dataclass
class CallSummary:
    caller: CallerIdentity
    language: str
    purpose: str
    urgency: UrgencyAssessment
    message_for_user: str
    escalate: bool
    transcript_excerpt: str = ""


@dataclass
class CallPolicy:
    user_name: str
    assistant_name: str = "JARVIS"
    contacts: dict = field(default_factory=dict)        # number → {"name", "relationship", "vip"}
    blocked: set = field(default_factory=set)
    always_escalate_relationships: set = field(default_factory=lambda: {"family"})
    quiet_hours: Optional[tuple] = (dtime(22, 30), dtime(7, 0))
    escalate_at: Urgency = Urgency.HIGH


_DISCLOSURE = {
    "en": "Hello, this is {assistant}, an AI assistant answering for {user}. {user} can't take the call "
          "right now. May I know who's calling and what it's about?",
    "ta": "வணக்கம், நான் {assistant}, {user} அவர்களுக்காக பதில் அளிக்கும் AI உதவியாளர். {user} இப்போது "
          "அழைப்பை எடுக்க முடியாது. யார் பேசுகிறீர்கள், என்ன விஷயம் என்று சொல்ல முடியுமா?",
    "ta-latn": "Vanakkam, naan {assistant}, {user}-kaaga answer pannura AI assistant. {user} ippo call "
               "edukka mudiyadhu. Yaar pesureenga, enna vishayam nu sollunga?",
}

_URGENT = {
    Urgency.EMERGENCY: ["emergency", "accident", "hospital", "ambulance", "police", "fire", "died", "death",
                        "bleeding", "unconscious", "அவசரம்", "விபத்து", "மருத்துவமனை", "aavasaram",
                        "avasaram", "accident aayiduchu", "hospital la"],
    Urgency.HIGH: ["urgent", "asap", "immediately", "right now", "important", "deadline", "today itself",
                   "முக்கியம்", "உடனே", "romba mukkiyam", "udane", "seekiram"],
    Urgency.LOW: ["no hurry", "whenever", "not urgent", "just checking", "casual", "later is fine",
                  "avasaram illa", "mella"],
}


class CallPolicyEngine:
    def __init__(self, policy: CallPolicy):
        self.p = policy

    @staticmethod
    def _norm(num: str) -> str:
        return re.sub(r"[^\d+]", "", num or "")[-10:]

    def identify(self, call: IncomingCall) -> CallerIdentity:
        n = self._norm(call.number)
        blocked = any(self._norm(b) == n for b in self.p.blocked)
        for num, info in self.p.contacts.items():
            if self._norm(num) == n:
                return CallerIdentity(call.number, info.get("name", ""), True, info.get("relationship", ""),
                                      bool(info.get("vip")), blocked)
        return CallerIdentity(call.number, call.network_name, False, "", False, blocked)

    def opening_line(self, language: str = "en") -> str:
        tpl = _DISCLOSURE.get(language, _DISCLOSURE["en"])
        return tpl.format(assistant=self.p.assistant_name, user=self.p.user_name)

    def check_utterance(self, text: str) -> tuple[bool, str]:
        """False if the line would impersonate the user."""
        u = re.escape(self.p.user_name.strip())
        if not u:
            return True, ""
        own = r"(?!['’]s)\b"          # "Architha's assistant" is fine; "I am Architha" is not
        bad = [rf"\bthis is {u}{own}", rf"\bi am {u}{own}", rf"\bi'm {u}{own}", rf"\b{u} (?:here|speaking)\b",
               rf"\bnaan {u}{own}", rf"நான் {u}(?!['’]s)"]
        for pat in bad:
            if re.search(pat, text, re.I):
                return False, "The assistant may speak for the user, never as the user."
        return True, ""

    def assess_urgency(self, text: str, caller: CallerIdentity | None = None) -> UrgencyAssessment:
        t = (text or "").lower()
        level, reasons = Urgency.NORMAL, []
        for lvl in (Urgency.EMERGENCY, Urgency.HIGH):
            hits = [w for w in _URGENT[lvl] if w in t]
            if hits:
                level, reasons = lvl, [f"said: {', '.join(hits[:3])}"]
                break
        else:
            hits = [w for w in _URGENT[Urgency.LOW] if w in t]
            if hits:
                level, reasons = Urgency.LOW, [f"said: {', '.join(hits[:3])}"]
        if caller and caller.vip and level < Urgency.HIGH:
            level = Urgency.HIGH
            reasons.append("VIP contact")
        return UrgencyAssessment(level, reasons)

    def should_escalate(self, caller: CallerIdentity, urgency: UrgencyAssessment,
                        now: datetime | None = None) -> bool:
        if caller.blocked:
            return False
        if urgency.level >= Urgency.EMERGENCY:
            return True
        if caller.relationship in self.p.always_escalate_relationships:
            return True
        qh = self.p.quiet_hours
        if qh:
            t = (now or datetime.now()).time()
            quiet = (qh[0] <= t or t < qh[1]) if qh[0] > qh[1] else (qh[0] <= t < qh[1])
            if quiet:
                return False
        return urgency.level >= self.p.escalate_at


class CallAgent(ABC):
    """Contract for the Phase-7 Call Agent."""

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def handle(self, call: IncomingCall) -> CallSummary: ...


class TelephonyUnavailableAgent(CallAgent):
    """What exists today: nothing can answer calls. Says so; never pretends."""

    def available(self) -> bool:
        return False

    def handle(self, call: IncomingCall) -> CallSummary:
        raise NotImplementedError("Call handling needs the phone companion app and a telephony "
                                  "integration — planned for Phase 7, not built yet.")
