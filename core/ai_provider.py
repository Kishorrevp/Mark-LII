"""
core/ai_provider.py — backwards-compatible facade over core/ai_gateway.py.

Phase 4 introduced get_provider().generate_text(prompt) with only Google
working and the other three providers as NotImplementedError stubs. Phase 4A
replaced that with a real gateway + four real provider implementations
(core/providers/*). This module is kept so every existing call site
(dev_agent.py, code_helper.py, main.py's session summary) keeps working
unchanged: get_provider() now returns a thin adapter whose generate_text()
goes through the gateway — so it honours ai_provider / ai_model, retries,
capability checks and configured fallbacks.

New code should import core.ai_gateway.get_gateway() directly.
"""
from __future__ import annotations

from core.ai_gateway import get_gateway, AIGateway, load_config  # noqa: F401  (re-export)
from core.providers.base import (  # noqa: F401  (re-export for older imports)
    AIProvider, ProviderError, ProviderConfigError, CapabilityNotSupported,
)


class _TextAdapter:
    """What get_provider() used to return, now backed by the gateway."""

    def __init__(self, gateway: AIGateway | None = None, tier: str = "default"):
        self._gw = gateway
        self._tier = tier

    @property
    def gateway(self) -> AIGateway:
        return self._gw or get_gateway()

    def generate_text(self, prompt: str, *, system: str = "", tier: str | None = None) -> str:
        """Return the model's reply as plain text. Raises ProviderError on
        failure — every existing caller already wraps this in try/except."""
        return self.gateway.text(prompt, system=system, tier=tier or self._tier)


def get_provider(tier: str = "default") -> _TextAdapter:
    return _TextAdapter(tier=tier)
