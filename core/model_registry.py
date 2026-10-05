"""
core/model_registry.py — exact, spoken-friendly model switching.

"Switch to Gemini 3.7 Flash" must switch to exactly gemini-3.7-flash — or
refuse and say what IS available. It must never quietly pick the nearest
name, and a switch made at runtime must not survive a restart.

    registry   every model JARVIS can actually address: the ids in
               config/models.json (all providers, all tiers, alternates, the
               live voice model) + optional "aliases" in that file + ids the
               provider lists for this key (when probed)
    aliases    generated: "gemini 3.7 flash", "gemini three point seven flash",
               the id without its "models/" or "vendor/" prefix …
    resolve()  normalise the spoken phrase (number words → digits, "point" → ".",
               drop "switch to / use / model") and look it up EXACTLY.
                 one entry   → ok
                 several     → ambiguous: refuse, list them
                 none        → refuse with the closest real ids as suggestions

The switch itself lives in AIGateway.set_runtime_model (brain tiers) and
main.py (live voice model); both are in memory only — restart = defaults.

Dependency-free; tested by real execution.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

_NUM = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
        "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12"}
_FILLER = re.compile(r"(?i)^\s*(?:please\s+)?(?:(?:can you|could you)\s+)?(?:switch|change|swap|move)?\s*"
                     r"(?:(?:the )?(?:brain|model|ai|voice model)\s+)?(?:to|over to|into)?\s*(?:use\s+|using\s+)?"
                     r"(?:the\s+)?")
BRAIN_TIERS = ("normal", "deep", "research")
TEXT_TIERS = ("fast", "normal", "deep", "research", "computer_use", "vision")


def normalise(text: str) -> str:
    s = str(text or "").lower().strip()
    s = s.replace("models/", "")
    s = _FILLER.sub("", s)
    s = re.sub(r"\b(model|please|now|instead|for me)\b", " ", s)
    words = re.split(r"[\s_\-/]+", s)
    words = [_NUM.get(w, w) for w in words if w]
    s = " ".join(words)
    s = re.sub(r"(\d)\s+point\s+(\d)", r"\1.\2", s)
    s = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", s)
    s = re.sub(r"[^\w. ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" .")


@dataclass
class ModelEntry:
    id: str
    provider: str
    kinds: set = field(default_factory=set)       # text | vision | image | live
    tiers: list = field(default_factory=list)
    aliases: set = field(default_factory=set)
    source: str = "config"                         # config | provider

    def label(self) -> str:
        return f"{self.id} ({self.provider})"


@dataclass
class Resolution:
    ok: bool
    entry: Optional[ModelEntry] = None
    candidates: list = field(default_factory=list)   # [ModelEntry]
    reason: str = ""                                  # exact | ambiguous | unknown | empty
    query: str = ""

    def message(self) -> str:
        if self.ok and self.entry:
            return f"{self.entry.id} ({self.entry.provider})"
        names = ", ".join(c.id for c in self.candidates[:5])
        if self.reason == "ambiguous":
            return f"'{self.query}' matches more than one model: {names}. Say the exact one."
        if self.reason == "empty":
            return "Which model? Say its name."
        return (f"I don't have a model called '{self.query}'. "
                + (f"Closest real ones: {names}." if names else "No similar model is configured."))


def _aliases_for(mid: str) -> set:
    out = set()
    base = mid.replace("models/", "")
    out.add(normalise(base))
    if "/" in base:
        out.add(normalise(base.split("/", 1)[1]))
    # "gemini-3.7-flash" also as "gemini 3.7 flash" (already) and "3.7 flash"
    n = normalise(base)
    if n.startswith("gemini "):
        out.add(n[len("gemini "):])
    return {a for a in out if a}


class ModelRegistry:
    def __init__(self, models_cfg: Optional[dict] = None, listed: Optional[dict] = None):
        if models_cfg is None:
            try:
                from core import config as _c
                models_cfg = _c.load("models")
            except Exception:
                models_cfg = {}
        self.cfg = models_cfg or {}
        self._entries: dict[tuple, ModelEntry] = {}
        self._build(listed or {})

    def _add(self, mid: str, provider: str, kind: str, tier: str = "", source: str = "config") -> None:
        mid = str(mid or "").strip()
        if not mid or mid.startswith("@"):
            return
        key = (mid.replace("models/", ""), provider)
        e = self._entries.get(key)
        if e is None:
            e = ModelEntry(mid, provider, source=source)
            e.aliases = _aliases_for(mid)
            self._entries[key] = e
        e.kinds.add(kind)
        if tier and tier not in e.tiers:
            e.tiers.append(tier)

    def _build(self, listed: dict) -> None:
        provs = self.cfg.get("providers") or {}
        for prov, pc in provs.items():
            tiers = (pc or {}).get("tiers") or {}
            for tier, tc in tiers.items():
                tc = tc or {}
                kind = {"image": "image", "live": "live", "vision": "vision"}.get(tier, "text")
                self._add(tc.get("model"), prov, kind, tier)
                for alt in tc.get("alternates") or []:
                    self._add(alt, prov, kind, tier)
            # Phase 7: models you can switch to by name that no lane uses automatically
            for mid in (pc or {}).get("extra_models") or []:
                self._add(mid, prov, "vision" if "vision" in str(mid) or "glimmer" in str(mid) else "text")
        for prov, ids in (listed or {}).items():
            for mid in ids or []:
                self._add(mid, prov, "text", "", source="provider")
        for mid, al in (self.cfg.get("aliases") or {}).items():
            for e in self._entries.values():
                if e.id.replace("models/", "") == str(mid).replace("models/", ""):
                    for a in (al if isinstance(al, list) else [al]):
                        if str(a).strip():
                            e.aliases.add(normalise(str(a)))

    # -- queries -------------------------------------------------------------
    def entries(self, kind: Optional[str] = None, provider: Optional[str] = None) -> list[ModelEntry]:
        out = [e for e in self._entries.values()
               if (kind is None or kind in e.kinds) and (provider is None or e.provider == provider)]
        return sorted(out, key=lambda e: (e.provider, e.id))

    def get(self, mid: str, provider: Optional[str] = None) -> Optional[ModelEntry]:
        m = str(mid or "").replace("models/", "")
        for e in self._entries.values():
            if e.id.replace("models/", "") == m and (provider is None or e.provider == provider):
                return e
        return None

    def resolve(self, spoken: str, kind: Optional[str] = None) -> Resolution:
        q = normalise(spoken)
        if not q:
            return Resolution(False, reason="empty", query=str(spoken or ""))
        pool = self.entries(kind)
        if kind == "text":
            pool = [e for e in self.entries() if e.kinds & {"text", "vision"}]
        exact = [e for e in pool if q in e.aliases or q == normalise(e.id)]
        if len(exact) == 1:
            return Resolution(True, exact[0], reason="exact", query=q)
        if len(exact) > 1:
            return Resolution(False, candidates=exact, reason="ambiguous", query=q)
        return Resolution(False, candidates=self.suggest(q, pool), reason="unknown", query=q)

    @staticmethod
    def suggest(q: str, pool: Iterable[ModelEntry], n: int = 4) -> list[ModelEntry]:
        qt = set(q.split())
        scored = []
        for e in pool:
            best = 0.0
            for a in e.aliases:
                at = set(a.split())
                jac = len(qt & at) / max(1, len(qt | at))
                rat = difflib.SequenceMatcher(None, q, a).ratio()
                best = max(best, 0.6 * rat + 0.4 * jac)
            scored.append((best, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for s, e in scored[:n] if s >= 0.35]

    def describe(self, kind: Optional[str] = None) -> str:
        groups: dict[str, list[str]] = {}
        for e in self.entries(kind):
            groups.setdefault(e.provider, []).append(e.id.replace("models/", ""))
        return "; ".join(f"{p}: {', '.join(ids)}" for p, ids in groups.items())
