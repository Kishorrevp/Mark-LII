"""
core/providers/anthropic_provider.py — Anthropic Claude via the Messages HTTP API.

COST WARNING — this is a PAID API. There is no free tier. The project's cost
boundary is "no paid API keys", so this provider is never selected by
default, never used as an automatic fallback, and only activates when the user
explicitly sets ai_provider=anthropic AND supplies ANTHROPIC_API_KEY. It exists
because the architecture spec asked for the provider seam to be real, not
because JARVIS needs it.

Implemented against the documented Messages API shape using plain HTTPS (no
`anthropic` SDK dependency). Verified in this build only against a fake
transport that replays documented request/response shapes — there was no key
to test live. See CHANGELOG Phase 4A "Verification".

Model: there is intentionally no baked-in default model ID — model names
change; set ai_model / JARVIS_AI_MODEL to the model you want.
"""
from __future__ import annotations

import json
from typing import Iterator

from core.providers.base import (
    AIProvider, AIRequest, AIResponse, Capability, ProviderConfigError,
    ProviderError, ReasoningEffort, ToolCall, Usage, default_transport, raise_for_http,
)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
_STRUCT_TOOL = "emit_structured_result"

_THINKING_BUDGET = {
    ReasoningEffort.LOW: 1024,
    ReasoningEffort.MEDIUM: 4096,
    ReasoningEffort.HIGH: 16000,
}


class AnthropicProvider(AIProvider):
    name = "anthropic"
    capabilities = frozenset({
        Capability.TEXT, Capability.STREAMING, Capability.STRUCTURED, Capability.TOOLS,
        Capability.VISION, Capability.DOCUMENT_INPUT, Capability.REASONING, Capability.TOKEN_COUNT,
    })
    default_model = ""
    context_window = 200_000

    def __init__(self, api_key: str = "", model: str = "", fast_model: str = "",
                 transport=None, base_url: str = ""):
        if not api_key:
            raise ProviderConfigError(
                "Anthropic provider needs ANTHROPIC_API_KEY (paid API — see changelog cost notes).",
                provider=self.name)
        if not model:
            raise ProviderConfigError(
                "Anthropic provider needs a model name: set ai_model in config/api_keys.json "
                "or JARVIS_AI_MODEL in .env.", provider=self.name)
        self._key = api_key
        self.default_model = model
        self.fast_model = fast_model or model
        self._transport = transport or default_transport
        self._url = (base_url.rstrip("/") + "/v1/messages") if base_url else API_URL

    def _headers(self) -> dict:
        return {"x-api-key": self._key, "anthropic-version": API_VERSION,
                "content-type": "application/json"}

    @staticmethod
    def _content(m) -> list[dict]:
        blocks: list[dict] = []
        if m.role == "tool":
            return [{"type": "tool_result", "tool_use_id": m.tool_call_id or m.tool_name,
                     "content": m.text()}]
        for p in m.parts:
            if p.kind == "text":
                if p.text:
                    blocks.append({"type": "text", "text": p.text})
            elif p.kind == "image":
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": p.mime_type, "data": p.b64()}})
            elif p.kind == "document":
                blocks.append({"type": "document", "source": {
                    "type": "base64", "media_type": p.mime_type or "application/pdf", "data": p.b64()}})
        for tc in m.tool_calls:
            blocks.append({"type": "tool_use", "id": tc.id or tc.name, "name": tc.name,
                           "input": tc.arguments})
        return blocks

    def build_body(self, req: AIRequest, stream: bool = False) -> dict:
        msgs = []
        for m in req.messages:
            role = "assistant" if m.role == "assistant" else "user"
            content = self._content(m)
            if content:
                msgs.append({"role": role, "content": content})
        max_tokens = int(req.max_output_tokens or 4096)
        body: dict = {"model": req.model or self.default_model, "max_tokens": max_tokens,
                      "messages": msgs}
        system = req.system
        thinking = req.reasoning not in (None, ReasoningEffort.NONE)
        tools = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                 for t in req.tools]
        if req.response_schema is not None:
            if thinking:
                # Forced tool_choice is not allowed together with extended
                # thinking, so fall back to instruction + JSON parse.
                system = (system + "\n\n" if system else "") + (
                    "Respond with ONLY a JSON value matching this JSON Schema, no prose:\n"
                    + json.dumps(req.response_schema))
            else:
                tools.append({"name": _STRUCT_TOOL, "description": "Return the final structured result.",
                              "input_schema": req.response_schema})
                body["tool_choice"] = {"type": "tool", "name": _STRUCT_TOOL}
        if tools:
            body["tools"] = tools
            if "tool_choice" not in body:
                if req.tool_choice == "required":
                    body["tool_choice"] = {"type": "any"}
                elif req.tool_choice == "none":
                    body["tool_choice"] = {"type": "none"}
                elif req.tool_choice not in ("auto", "", None):
                    body["tool_choice"] = {"type": "tool", "name": req.tool_choice}
        if system:
            body["system"] = system
        if thinking:
            budget = _THINKING_BUDGET[req.reasoning]
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            if body["max_tokens"] <= budget:
                body["max_tokens"] = budget + 2048
        elif req.temperature is not None:
            body["temperature"] = float(req.temperature)
        if stream:
            body["stream"] = True
        return body

    def generate(self, req: AIRequest) -> AIResponse:
        self.check(req)
        body = self.build_body(req)
        try:
            res = self._transport("POST", self._url, headers=self._headers(), body=body,
                                  timeout=req.timeout)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"anthropic: network error ({type(e).__name__})",
                                provider=self.name, retryable=True) from e
        raise_for_http(self.name, res)
        data = res.json() or {}
        text, calls, structured = [], [], None
        for block in data.get("content", []) or []:
            bt = block.get("type")
            if bt == "text":
                text.append(block.get("text", ""))
            elif bt == "tool_use":
                if block.get("name") == _STRUCT_TOOL:
                    structured = block.get("input")
                else:
                    calls.append(ToolCall(name=block.get("name", ""), arguments=block.get("input") or {},
                                          id=block.get("id", "")))
        u = data.get("usage") or {}
        usage = Usage(u.get("input_tokens"), u.get("output_tokens"),
                      (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0) if u else None)
        out = AIResponse(text="".join(text).strip(), tool_calls=calls, usage=usage,
                         finish_reason=str(data.get("stop_reason") or ""), provider=self.name,
                         model=data.get("model") or body["model"])
        if req.response_schema is not None:
            out.data = structured if structured is not None else out.json()
        return out

    def stream(self, req: AIRequest) -> Iterator[str]:
        self.check(req)
        body = self.build_body(req, stream=True)
        lines = self._transport("POST", self._url, headers=self._headers(), body=body,
                                timeout=req.timeout, stream=True)
        if hasattr(lines, "status"):
            raise_for_http(self.name, lines)
            return
        for line in lines:
            if not line or not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            if ev.get("type") == "content_block_delta":
                d = ev.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    yield d["text"]
            elif ev.get("type") == "error":
                raise ProviderError(f"anthropic stream error: {ev.get('error', {}).get('type', '?')}",
                                    provider=self.name)

    def count_tokens(self, req: AIRequest) -> int:
        body = self.build_body(req)
        body.pop("max_tokens", None)
        url = self._url.replace("/v1/messages", "/v1/messages/count_tokens")
        res = self._transport("POST", url, headers=self._headers(), body=body, timeout=req.timeout)
        raise_for_http(self.name, res)
        return int((res.json() or {}).get("input_tokens", 0))
