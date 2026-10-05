"""
core/providers/google_provider.py — Google Gemini via the google-genai SDK.

This is the provider JARVIS has always used (free tier). It is the only one
with WEB_GROUNDING (Gemini's built-in google_search tool) and AUDIO_INPUT, both
of which existing actions rely on (web_search.py, file_processor's
transcription).

The realtime voice session (Gemini *Live*, native audio in/out) is NOT behind
this interface — see core/ai_gateway.py "Why Live is not abstracted".

The SDK client is created lazily and can be injected (client=...) so the
request/response mapping can be tested without the SDK or network.
"""
from __future__ import annotations

from typing import Any, Iterator

from core.providers.base import (
    AIProvider, AIRequest, AIResponse, Capability, ProviderConfigError,
    ProviderError, RateLimited, ReasoningEffort, ToolCall, Usage,
)

# Effort → thinking budget (tokens). NONE → 0 disables thinking on models that
# allow it; models that reject 0 surface a normal ProviderError.
_THINKING_BUDGET = {
    ReasoningEffort.NONE: 0,
    ReasoningEffort.LOW: 1024,
    ReasoningEffort.MEDIUM: 4096,
    ReasoningEffort.HIGH: 16384,
}

_DROP_SCHEMA_KEYS = {"$schema", "additionalProperties", "$id", "definitions", "$defs", "title",
                     "default", "examples"}


def to_gemini_schema(schema: Any) -> Any:
    """JSON Schema (lowercase types) → Gemini OpenAPI-subset schema (UPPERCASE
    types, no additionalProperties). Idempotent on already-converted schemas."""
    if isinstance(schema, list):
        return [to_gemini_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k in _DROP_SCHEMA_KEYS:
            continue
        if k == "type":
            if isinstance(v, list):          # ["string","null"] → STRING, nullable
                non_null = [t for t in v if t != "null"]
                out["type"] = (non_null[0] if non_null else "string").upper()
                if "null" in v:
                    out["nullable"] = True
            else:
                out["type"] = str(v).upper()
        elif k == "properties" and isinstance(v, dict):
            out["properties"] = {pk: to_gemini_schema(pv) for pk, pv in v.items()}
        elif k in ("items", "anyOf"):
            out[k] = to_gemini_schema(v)
        else:
            out[k] = v
    return out


class GoogleProvider(AIProvider):
    name = "google"
    capabilities = frozenset({
        Capability.TEXT, Capability.STREAMING, Capability.STRUCTURED, Capability.TOOLS,
        Capability.VISION, Capability.AUDIO_INPUT, Capability.DOCUMENT_INPUT,
        Capability.REASONING, Capability.WEB_GROUNDING, Capability.TOKEN_COUNT,
        Capability.IMAGE_OUTPUT,           # only on the image models (config tier "image")
    })
    default_model = "gemini-flash-latest"
    fast_model = "gemini-flash-lite-latest"
    context_window = 1_000_000   # Gemini Flash family; informational only

    def __init__(self, api_key: str = "", model: str = "", fast_model: str = "", client=None):
        if not api_key and client is None:
            raise ProviderConfigError(
                "Google provider needs GOOGLE_API_KEY (set it in .env or the environment).",
                provider=self.name)
        self._api_key = api_key
        self._client = client
        if model:
            self.default_model = model
        if fast_model:
            self.fast_model = fast_model

    # -- plumbing ---------------------------------------------------------
    def _get_client(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError as e:
                raise ProviderConfigError("google-genai is not installed (pip install google-genai).",
                                          provider=self.name) from e
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    @staticmethod
    def _contents(req: AIRequest) -> list[dict]:
        contents = []
        for m in req.messages:
            role = "model" if m.role == "assistant" else "user"
            parts: list[dict] = []
            if m.role == "tool":
                parts.append({"function_response": {
                    "name": m.tool_name or "tool",
                    "response": {"result": m.text()},
                }})
            else:
                for p in m.parts:
                    if p.kind == "text":
                        if p.text:
                            parts.append({"text": p.text})
                    else:
                        parts.append({"inline_data": {"mime_type": p.mime_type, "data": p.data}})
                for tc in m.tool_calls:
                    parts.append({"function_call": {"name": tc.name, "args": tc.arguments}})
            if parts:
                contents.append({"role": role, "parts": parts})
        return contents

    def _config(self, req: AIRequest) -> dict:
        cfg: dict = {}
        if req.system:
            cfg["system_instruction"] = req.system
        if req.max_output_tokens:
            cfg["max_output_tokens"] = int(req.max_output_tokens)
        if req.temperature is not None:
            cfg["temperature"] = float(req.temperature)
        tools = []
        if req.tools:
            tools.append({"function_declarations": [
                {"name": t.name, "description": t.description,
                 "parameters": to_gemini_schema(t.parameters)} for t in req.tools]})
            mode = {"auto": "AUTO", "none": "NONE", "required": "ANY"}.get(req.tool_choice)
            fcc: dict = {"mode": mode or "ANY"}
            if mode is None:                        # a specific tool name
                fcc["allowed_function_names"] = [req.tool_choice]
            cfg["tool_config"] = {"function_calling_config": fcc}
        if req.web_grounding:
            tools.append({"google_search": {}})
        if tools:
            cfg["tools"] = tools
        if req.response_schema is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_schema"] = to_gemini_schema(req.response_schema)
        if req.reasoning is not None:
            budget = _THINKING_BUDGET[req.reasoning]
            # Gemini 3.x (and the *-latest aliases that now point at it) reject
            # thinking_budget=0 with a bare INVALID_ARGUMENT (verified on the
            # user's key, Sept 2026) — "no thinking" there means: leave it unset.
            if not (budget == 0 and not str(req.model or self.default_model).startswith("gemini-2")):
                cfg["thinking_config"] = {"thinking_budget": budget}
        if req.response_modalities:
            cfg["response_modalities"] = [m.upper() for m in req.response_modalities]
            if req.image_aspect:
                cfg["image_config"] = {"aspect_ratio": req.image_aspect}
        return cfg

    @staticmethod
    def _quota_digest(full: str) -> str:
        """Google puts the useful part of a 429 (which quota, its limit, which
        model) deep inside a long JSON body. Keep exactly that, compactly."""
        import re
        bits = []
        for lim, mdl in re.findall(r"limit:\s*(\d+),\s*model:\s*([\w.\-]+)", full)[:2]:
            bits.append(f"limit: {lim}, model: {mdl}")
        ids = sorted(set(re.findall(r"quotaId['\"]?:\s*['\"]([A-Za-z\-]+)", full)))
        if ids:
            bits.append("quota " + "/".join(ids)[:140])
        m = re.search(r"retry(?:Delay['\"]?:\s*['\"]| in )(\d+(?:\.\d+)?)s", full)
        if m:
            bits.append(f"retry in {float(m.group(1)):.0f}s")
        return ("[" + "; ".join(dict.fromkeys(bits)) + "]") if bits else ""

    def _map_error(self, e: Exception) -> ProviderError:
        from core.secret_store import redact
        import re
        full = redact(str(e))
        msg = full[:300]
        if len(full) > 300:
            digest = self._quota_digest(full)
            if digest:
                # the facts first (they survive any later truncation), then the human message
                head = re.match(r"\s*(\d{3}\s+[A-Z_]+)", full)
                human = re.search(r"'message':\s*'([^']{0,160})", full)
                msg = " ".join(x for x in ((head.group(1) if head else ""), digest,
                                           ("— " + human.group(1).split("\\n")[0].strip()) if human else "") if x)
        code = getattr(e, "code", None)
        if code == 429 or "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            return RateLimited(f"google: quota/rate limit — {msg}", provider=self.name)
        if code in (401, 403) or "API key not valid" in msg or "PERMISSION_DENIED" in msg:
            return ProviderConfigError(f"google: authentication failed — {msg}", provider=self.name,
                                       status=code)
        retry = bool(code and int(code) >= 500) or "UNAVAILABLE" in msg or "timed out" in msg.lower()
        return ProviderError(f"google: {msg}", provider=self.name, retryable=retry, status=code)

    # -- interface ----------------------------------------------------------
    @staticmethod
    def _with_timeout(cfg: dict | None, req: AIRequest) -> dict | None:
        """Phase 7: the request's timeout reaches the HTTP call. Before this, a
        Google request had NO deadline — one stalled model held a tool call for
        minutes (measured: 412 s before Windows dropped the connection)."""
        if not req.timeout:
            return cfg
        ms = int(max(5.0, float(req.timeout)) * 1000)
        return {**(cfg or {}), "http_options": {"timeout": ms}}

    def generate(self, req: AIRequest) -> AIResponse:
        self.check(req)
        client = self._get_client()
        model = req.model or self.default_model
        cfg = self._with_timeout(self._config(req) or None, req)
        try:
            try:
                resp = client.models.generate_content(model=model, contents=self._contents(req), config=cfg)
            except Exception as e:
                low = str(e).lower()
                # older SDKs / models reject image_config — retry once without the aspect ratio
                if cfg and "image_config" in cfg and "image_config" in low:
                    cfg = {k: v for k, v in cfg.items() if k != "image_config"}
                    resp = client.models.generate_content(model=model, contents=self._contents(req), config=cfg)
                elif cfg and "http_options" in cfg and "http_options" in low:
                    cfg = {k: v for k, v in cfg.items() if k != "http_options"} or None
                    resp = client.models.generate_content(model=model, contents=self._contents(req), config=cfg)
                else:
                    raise
        except ProviderError:
            raise
        except Exception as e:
            if "timeout" in type(e).__name__.lower():
                raise ProviderError(f"google: {model} timed out after {req.timeout:.0f}s ({type(e).__name__})",
                                    provider=self.name, retryable=True) from e
            raise self._map_error(e) from e
        return self._parse(resp, model, req)

    def _parse(self, resp, model: str, req: AIRequest) -> AIResponse:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        images: list = []
        finish = ""
        try:
            cand = (getattr(resp, "candidates", None) or [None])[0]
            if cand is not None:
                finish = str(getattr(cand, "finish_reason", "") or "")
                content = getattr(cand, "content", None)
                for part in (getattr(content, "parts", None) or []):
                    t = getattr(part, "text", None)
                    if t and not getattr(part, "thought", False):
                        text_parts.append(t)
                    blob = getattr(part, "inline_data", None)
                    if blob is not None and not getattr(part, "thought", False):
                        mime = str(getattr(blob, "mime_type", "") or "")
                        data = getattr(blob, "data", None)
                        if mime.startswith("image/") and data:
                            if isinstance(data, str):
                                import base64
                                data = base64.b64decode(data)
                            images.append((bytes(data), mime))
                    fc = getattr(part, "function_call", None)
                    if fc is not None and getattr(fc, "name", None):
                        calls.append(ToolCall(name=fc.name, arguments=dict(getattr(fc, "args", {}) or {}),
                                              id=str(getattr(fc, "id", "") or "")))
        except Exception:
            pass
        text = "".join(text_parts)
        if not text and not calls and not images:
            try:
                text = resp.text or ""
            except Exception:
                text = ""
        um = getattr(resp, "usage_metadata", None)
        usage = Usage(
            input_tokens=getattr(um, "prompt_token_count", None),
            output_tokens=getattr(um, "candidates_token_count", None),
            total_tokens=getattr(um, "total_token_count", None),
        ) if um is not None else Usage()
        out = AIResponse(text=text.strip(), tool_calls=calls, usage=usage, finish_reason=finish,
                         provider=self.name, model=model, images=images)
        if req.response_schema is not None and not calls:
            out.data = out.json()
        return out

    def stream(self, req: AIRequest) -> Iterator[str]:
        self.check(req)
        client = self._get_client()
        model = req.model or self.default_model
        try:
            for chunk in client.models.generate_content_stream(
                    model=model, contents=self._contents(req), config=self._config(req) or None):
                t = getattr(chunk, "text", None)
                if t:
                    yield t
        except ProviderError:
            raise
        except Exception as e:
            raise self._map_error(e) from e

    def list_models(self) -> list[str]:
        """Model ids visible to this key (names without the "models/" prefix)."""
        try:
            out = []
            for m in self._get_client().models.list():
                n = str(getattr(m, "name", "") or "")
                out.append(n[7:] if n.startswith("models/") else n)
            return out
        except Exception:
            return []

    def count_tokens(self, req: AIRequest) -> int:
        client = self._get_client()
        try:
            r = client.models.count_tokens(model=req.model or self.default_model,
                                           contents=self._contents(req))
            return int(getattr(r, "total_tokens", 0) or 0)
        except Exception as e:
            raise self._map_error(e) from e
