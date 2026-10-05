"""
core/providers/openai_provider.py — OpenAI Chat Completions over HTTPS, and any
server that speaks the same protocol (LM Studio, llama.cpp server, vLLM, ...).

COST WARNING — api.openai.com is a PAID API. Never selected by default, never
used as an automatic fallback. The same class, pointed at a LOCAL
OpenAI-compatible server via base_url, costs nothing — that is how
`ai_provider=local` + `local_api=openai` is served (see core/ai_gateway.py).

No `openai` SDK dependency — plain HTTPS through the injectable transport in
providers/base.py. Verified in this build against a fake transport replaying
the documented request/response shapes, not against the live API (no key).

Model: no baked-in default; set ai_model / JARVIS_AI_MODEL.
"""
from __future__ import annotations

import json
from typing import Iterator

from core.providers.base import (
    AIProvider, AIRequest, AIResponse, Capability, ProviderConfigError,
    ProviderError, ReasoningEffort, ToolCall, Usage, default_transport, raise_for_http,
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider(AIProvider):
    name = "openai"
    capabilities = frozenset({
        Capability.TEXT, Capability.STREAMING, Capability.STRUCTURED, Capability.TOOLS,
        Capability.VISION, Capability.REASONING,
    })
    default_model = ""

    def __init__(self, api_key: str = "", model: str = "", fast_model: str = "",
                 base_url: str = "", transport=None, name: str = "openai",
                 require_key: bool = True, capabilities: frozenset | None = None,
                 keys_for=None):
        self.name = name
        # Phase 7: per-model keys (NVIDIA hands out one key per model page).
        # keys_for(model) → [key, …] best first; the next key is tried on 401/403.
        self._keys_for = keys_for
        if require_key and not api_key and not (keys_for and keys_for("")):
            raise ProviderConfigError(
                "OpenAI provider needs OPENAI_API_KEY (paid API — see changelog cost notes).",
                provider=self.name)
        if not model:
            raise ProviderConfigError(
                f"{name} provider needs a model name: set ai_model in config/api_keys.json "
                "or JARVIS_AI_MODEL in .env.", provider=self.name)
        self._key = api_key
        self.default_model = model
        self.fast_model = fast_model or model
        self._base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport or default_transport
        if capabilities is not None:
            self.capabilities = capabilities

    def _headers(self, key: str | None = None) -> dict:
        h = {"content-type": "application/json"}
        k = key if key is not None else self._key
        if k:
            h["authorization"] = f"Bearer {k}"
        return h

    def _candidate_keys(self, model: str) -> list:
        if self._keys_for is None:
            return [None]
        try:
            ks = [k for k in (self._keys_for(model) or []) if k]
        except Exception:
            ks = []
        return ks or [None]

    @staticmethod
    def _message(m) -> dict | list[dict]:
        if m.role == "tool":
            return {"role": "tool", "tool_call_id": m.tool_call_id or m.tool_name, "content": m.text()}
        if m.role == "assistant":
            msg: dict = {"role": "assistant", "content": m.text() or None}
            if m.tool_calls:
                msg["tool_calls"] = [{"id": tc.id or tc.name, "type": "function",
                                      "function": {"name": tc.name,
                                                   "arguments": json.dumps(tc.arguments)}}
                                     for tc in m.tool_calls]
            return msg
        has_media = any(p.kind != "text" for p in m.parts)
        if not has_media:
            return {"role": "user", "content": m.text()}
        content = []
        for p in m.parts:
            if p.kind == "text":
                content.append({"type": "text", "text": p.text})
            elif p.kind == "image":
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:{p.mime_type};base64,{p.b64()}"}})
        return {"role": "user", "content": content}

    def build_body(self, req: AIRequest, stream: bool = False) -> dict:
        msgs = []
        if req.system:
            msgs.append({"role": "system", "content": req.system})
        msgs.extend(self._message(m) for m in req.messages)
        body: dict = {"model": req.model or self.default_model, "messages": msgs}
        if req.max_output_tokens:
            body["max_completion_tokens"] = int(req.max_output_tokens)
        if req.temperature is not None and req.reasoning in (None, ReasoningEffort.NONE):
            body["temperature"] = float(req.temperature)
        if req.tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in req.tools]
            if req.tool_choice in ("auto", "none", "required"):
                body["tool_choice"] = req.tool_choice
            elif req.tool_choice:
                body["tool_choice"] = {"type": "function", "function": {"name": req.tool_choice}}
        if req.response_schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "result", "schema": req.response_schema, "strict": False}}
        if req.reasoning not in (None, ReasoningEffort.NONE):
            body["reasoning_effort"] = req.reasoning.value
        if stream:
            body["stream"] = True
        return body

    def list_models(self) -> list[str]:
        """GET {base}/models — the ids this key can use (OpenAI, NVIDIA NIM,
        most local servers). Empty on any failure: listing is advisory."""
        try:
            res = self._transport("GET", f"{self._base}/models", headers=self._headers(), body=None, timeout=20.0)
            if res.status >= 400:
                return []
            data = res.json() or {}
            return sorted(str(m.get("id")) for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id"))
        except Exception:
            return []

    def generate(self, req: AIRequest) -> AIResponse:
        self.check(req)
        body = self.build_body(req)
        res = None
        for key in self._candidate_keys(body["model"]):
            try:
                res = self._transport("POST", f"{self._base}/chat/completions", headers=self._headers(key),
                                      body=body, timeout=req.timeout)
            except ProviderError:
                raise
            except Exception as e:
                raise ProviderError(f"{self.name}: network error ({type(e).__name__})",
                                    provider=self.name, retryable=True) from e
            if res.status not in (401, 403):
                break                      # a key that authenticates (or a non-auth error) — stop here
        raise_for_http(self.name, res)
        data = res.json() or {}
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {"_raw": fn.get("arguments")}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args, id=tc.get("id", "")))
        u = data.get("usage") or {}
        out = AIResponse(text=(msg.get("content") or "").strip(), tool_calls=calls,
                         usage=Usage(u.get("prompt_tokens"), u.get("completion_tokens"),
                                     u.get("total_tokens")),
                         finish_reason=str(choice.get("finish_reason") or ""),
                         provider=self.name, model=data.get("model") or body["model"])
        if req.response_schema is not None and not calls:
            out.data = out.json()
        return out

    def stream(self, req: AIRequest) -> Iterator[str]:
        self.check(req)
        lines = self._transport("POST", f"{self._base}/chat/completions",
                                headers=self._headers(self._candidate_keys(req.model or self.default_model)[0]),
                                body=self.build_body(req, stream=True), timeout=req.timeout,
                                stream=True)
        if hasattr(lines, "status"):
            raise_for_http(self.name, lines)
            return
        for line in lines:
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                ev = json.loads(payload)
            except Exception:
                continue
            delta = ((ev.get("choices") or [{}])[0].get("delta") or {})
            if delta.get("content"):
                yield delta["content"]
