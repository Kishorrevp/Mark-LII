"""
core/providers/local_provider.py — a model running on this machine via Ollama.

FREE: no key, no per-token cost, works offline. The trade-off (documented in
the project brief when Gemini was chosen as the default brain) is quality and
Tamil fluency, which depend entirely on the model you pull.

Uses Ollama's native /api/chat endpoint (default http://localhost:11434).
For LM Studio / llama.cpp / vLLM set local_api=openai and the gateway uses
OpenAIProvider against that local base URL instead.

Capabilities are declared for what the *API* supports. Whether a particular
model can actually see images (llava, qwen2.5-vl, gemma3) or call tools
(qwen3, llama3.1+) depends on the model; a model that can't will return an
Ollama error, which surfaces as a normal ProviderError.

Verified in this build against a fake transport replaying Ollama's documented
request/response shapes — no Ollama server was available in the sandbox.
"""
from __future__ import annotations

import json
import uuid
from typing import Iterator

from core.providers.base import (
    AIProvider, AIRequest, AIResponse, Capability, ProviderConfigError,
    ProviderError, ReasoningEffort, ToolCall, Usage, default_transport, raise_for_http,
)

DEFAULT_BASE_URL = "http://localhost:11434"


class LocalProvider(AIProvider):
    name = "local"
    capabilities = frozenset({
        Capability.TEXT, Capability.STREAMING, Capability.STRUCTURED, Capability.TOOLS,
        Capability.VISION, Capability.REASONING,
    })
    default_model = ""

    def __init__(self, model: str = "", fast_model: str = "", base_url: str = "", transport=None):
        if not model:
            raise ProviderConfigError(
                "Local provider needs a model name (e.g. the name shown by `ollama list`): "
                "set ai_model in config/api_keys.json or JARVIS_AI_MODEL in .env.", provider=self.name)
        self.default_model = model
        self.fast_model = fast_model or model
        self._base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport or default_transport

    @staticmethod
    def _message(m) -> dict:
        if m.role == "tool":
            return {"role": "tool", "content": m.text(), "tool_name": m.tool_name}
        msg: dict = {"role": "assistant" if m.role == "assistant" else "user", "content": m.text()}
        images = [p.b64() for p in m.parts if p.kind == "image"]
        if images:
            msg["images"] = images
        if m.tool_calls:
            msg["tool_calls"] = [{"function": {"name": tc.name, "arguments": tc.arguments}}
                                 for tc in m.tool_calls]
        return msg

    def build_body(self, req: AIRequest, stream: bool = False) -> dict:
        msgs = []
        if req.system:
            msgs.append({"role": "system", "content": req.system})
        msgs.extend(self._message(m) for m in req.messages)
        body: dict = {"model": req.model or self.default_model, "messages": msgs, "stream": stream}
        opts = {}
        if req.temperature is not None:
            opts["temperature"] = float(req.temperature)
        if req.max_output_tokens:
            opts["num_predict"] = int(req.max_output_tokens)
        if opts:
            body["options"] = opts
        if req.tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in req.tools]
        if req.response_schema is not None:
            body["format"] = req.response_schema
        if req.reasoning is not None:
            body["think"] = req.reasoning != ReasoningEffort.NONE
        return body

    def generate(self, req: AIRequest) -> AIResponse:
        self.check(req)
        body = self.build_body(req)
        try:
            res = self._transport("POST", f"{self._base}/api/chat", headers={"content-type": "application/json"},
                                  body=body, timeout=max(req.timeout, 120.0))
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"local: cannot reach Ollama at {self._base} ({type(e).__name__}). "
                                "Is `ollama serve` running?", provider=self.name, retryable=True) from e
        raise_for_http(self.name, res)
        data = res.json() or {}
        msg = data.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args, id=uuid.uuid4().hex[:8]))
        pin, pout = data.get("prompt_eval_count"), data.get("eval_count")
        out = AIResponse(text=(msg.get("content") or "").strip(), tool_calls=calls,
                         usage=Usage(pin, pout, (pin or 0) + (pout or 0) if (pin or pout) else None),
                         finish_reason=str(data.get("done_reason") or ""), provider=self.name,
                         model=data.get("model") or body["model"])
        if req.response_schema is not None and not calls:
            out.data = out.json()
        return out

    def stream(self, req: AIRequest) -> Iterator[str]:
        self.check(req)
        lines = self._transport("POST", f"{self._base}/api/chat", headers={"content-type": "application/json"},
                                body=self.build_body(req, stream=True), timeout=max(req.timeout, 120.0),
                                stream=True)
        if hasattr(lines, "status"):
            raise_for_http(self.name, lines)
            return
        for line in lines:
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("error"):
                raise ProviderError(f"local: {ev['error']}", provider=self.name)
            c = (ev.get("message") or {}).get("content")
            if c:
                yield c
            if ev.get("done"):
                break
