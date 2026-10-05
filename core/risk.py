"""
core/risk.py — compatibility shim (Phase 4B).

Phase 4 introduced a hand-written TOOL_RISK table here with three tiers
(low/medium/high), of which only "high" was enforced, by two actions gating
themselves. Phase 4B moved risk into the ToolRegistry (core/tools/builtin.py)
as the single source of truth, on the five-level scale in core/permissions.py
(SAFE / LOW_RISK / MEDIUM_RISK / HIGH_RISK / CRITICAL), enforced centrally by
the PermissionEngine for every tool call — direct voice calls, task steps,
workflows and plugins alike.

Keeping a second table here would let the two drift apart, so this module no
longer holds one. risk_level() is kept for any external caller and answers
from the registry, mapped back onto the old three words:
    SAFE, LOW_RISK → "low"   MEDIUM_RISK → "medium"   HIGH_RISK, CRITICAL → "high"

Changes versus the Phase 4 table (deliberate, per the Phase 4 spec):
    file_controller.delete            medium → HIGH  (confirmation required)
    send_message                      medium → HIGH  (confirmation required)
    game_updater + shutdown_when_done low    → HIGH  (it powers the PC off)
    read-only actions (list/read/find/info/status/...) low → SAFE
"""
from __future__ import annotations

_REGISTRY = None


def _registry():
    global _REGISTRY
    if _REGISTRY is None:
        from core.tools.builtin import build_default_registry
        _REGISTRY = build_default_registry()
    return _REGISTRY


def risk_level(tool_name: str, action: str | None = None) -> str:
    """Legacy three-word answer. Unknown tools → "high" (fail closed; the
    Phase 4 table said "medium", but the permission engine now DENIES unknown
    tools outright, so "high" is the honest mapping)."""
    from core.permissions import RiskLevel
    reg = _registry()
    if not reg.has(tool_name):
        return "high"
    r = reg.risk_for(tool_name, {"action": action} if action else {})
    if r <= RiskLevel.LOW_RISK:
        return "low"
    if r == RiskLevel.MEDIUM_RISK:
        return "medium"
    return "high"
