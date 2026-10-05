"""
core/agents/router.py — which agent(s) does a request need?

Deterministic and instant (no model call): the request is normalised
(Tamil/Tanglish → English keywords, core/multilingual), split into clauses
("open Chrome, search for X, download the PDF and put it in Downloads" → four
clauses), and each clause is scored against every registered agent's
keyword table. The winning agent per clause is kept, in order, de-duplicated.

Ambiguous clauses (e.g. a bare "search for X") inherit the previous clause's
agent when that agent also scored — "open Chrome, search for X" stays in the
Browser Agent rather than jumping to Research.

Adding an agent never touches this file: the router reads keywords from
whatever AgentRegistry it is given.

An optional `llm_fallback(text, agent_names) -> list[str]` can be supplied
for clauses that match nothing; by default unmatched clauses go to the
General Agent — no model call, no latency, no cost.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.agents.base import AgentRegistry
from core.multilingual import detect_language, normalize

_SPLIT = re.compile(r"\s*(?:,|;|\band then\b|\bthen\b|\band\b|\balso\b|\bafter that\b)\s*", re.I)


@dataclass
class ClauseRoute:
    clause: str
    agent: str
    score: float
    matched: list = field(default_factory=list)


@dataclass
class RouteResult:
    agents: list                    # ordered, unique agent names
    clauses: list                   # ClauseRoute per clause
    language: str = "en"
    normalized: str = ""

    @property
    def primary(self) -> str:
        return self.agents[0] if self.agents else "general"

    def to_dict(self) -> dict:
        return {"agents": self.agents, "language": self.language,
                "clauses": [{"clause": c.clause, "agent": c.agent, "score": c.score,
                             "matched": c.matched} for c in self.clauses]}


class AgentRouter:
    def __init__(self, agents: AgentRegistry,
                 llm_fallback: Optional[Callable[[str, list], list]] = None,
                 min_score: float = 2.0):
        self._agents = agents
        self._llm = llm_fallback
        self._min = min_score

    def _score(self, clause: str) -> dict:
        text = f" {clause.lower()} "
        scores: dict[str, tuple[float, list]] = {}
        for a in self._agents.all():
            total, hits = 0.0, []
            for kw, w in (a.keywords or {}).items():
                k = kw.lower()
                # word-boundary match for plain words, substring for phrases/space-padded
                if " " in k.strip() or k != k.strip():
                    found = k in text
                else:
                    found = re.search(rf"(?<![\w]){re.escape(k)}(?![\w])", text) is not None
                if found:
                    total += float(w)
                    hits.append(kw.strip())
            if total:
                scores[a.name] = (total, hits)
        return scores

    def route(self, text: str) -> RouteResult:
        lang = detect_language(text)
        norm = normalize(text) if lang != "en" else (text or "")
        # keep English words from mixed speech: normalize() passes them through
        clauses = [c for c in _SPLIT.split(norm) if c and c.strip()]
        if not clauses:
            clauses = [norm]
        routes: list[ClauseRoute] = []
        prev: str | None = None
        for c in clauses:
            scores = self._score(c)
            if not scores:
                if self._llm:
                    try:
                        picked = [a for a in (self._llm(c, [x.name for x in self._agents.all()]) or [])
                                  if self._agents.get(a)]
                    except Exception:
                        picked = []
                    if picked:
                        routes.append(ClauseRoute(c, picked[0], 0.0, ["llm"]))
                        prev = picked[0]
                        continue
                # Continuation clause ("put it in Downloads" handled above; "and summarize")
                agent = prev or "general"
                routes.append(ClauseRoute(c, agent, 0.0, []))
                continue
            ranked = sorted(scores.items(), key=lambda kv: (-kv[1][0], kv[0]))
            best, (best_score, hits) = ranked[0]
            if prev and prev in scores and prev != best:
                # tie-break toward conversational continuity when prev is close
                if scores[prev][0] >= best_score - 1.0:
                    best, (best_score, hits) = prev, scores[prev]
            if best_score < self._min and prev:
                best, hits = prev, hits
            routes.append(ClauseRoute(c, best, best_score, hits))
            prev = best
        agents: list[str] = []
        for r in routes:
            if r.agent not in agents:
                agents.append(r.agent)
        # "general" only if nothing more specific was found
        if len(agents) > 1 and "general" in agents:
            agents.remove("general")
        return RouteResult(agents=agents or ["general"], clauses=routes, language=lang, normalized=norm)
