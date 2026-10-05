"""
core/providers/base.py — the provider interface every AI backend implements.

Nothing in here imports a vendor SDK. Provider-specific code lives only in
the sibling modules (google_provider, anthropic_provider, openai_provider,
local_provider); JARVIS talks to them only through core/ai_gateway.py.

DATA MODEL (deliberately small)
    Part        one piece of content: text, image, audio or document bytes
    Message     role + parts ("system" is passed separately, not as a message)
    ToolSpec    a callable tool the model may request (JSON-Schema parameters)
    ToolCall    a tool request coming back from the model
    AIRequest   everything a single generation needs
    AIResponse  text + tool calls + usage + raw finish reason

CAPABILITIES
    Each provider declares a frozenset of Capability values. The gateway checks
    the request against it BEFORE calling the provider and raises
    CapabilityNotSupported — so an unsupported feature is a clean, catchable
    error with a readable message, never a vendor stack trace and never a
    silent downgrade.
"""
from __future__ import annotations

import base64
import enum
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional


class Capability(str, enum.Enum):
    TEXT             = "text"
    STREAMING        = "streaming"
    STRUCTURED       = "structured_output"
    TOOLS            = "tool_calling"
    VISION           = "vision"
    AUDIO_INPUT      = "audio_input"
    DOCUMENT_INPUT   = "document_input"     # e.g. PDF bytes sent natively
    REASONING        = "configurable_reasoning"
    WEB_GROUNDING    = "web_grounding"      # provider-side live web search
    TOKEN_COUNT      = "token_count"
    IMAGE_OUTPUT     = "image_output"       # the model can return generated images


class ReasoningEffort(str, enum.Enum):
    NONE   = "none"
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"

    @classmethod
    def parse(cls, value) -> "ReasoningEffort | None":
        if value in (None, ""):
            return None
        if isinstance(value, ReasoningEffort):
            return value
        v = str(value).strip().lower()
        for e in cls:
            if e.value == v:
                return e
        return None


# ── Errors ───────────────────────────────────────────────────────────────────

class ProviderError(RuntimeError):
    """Base for every provider failure. `retryable` tells the gateway whether a
    retry (or a configured fallback provider) is worth trying."""

    def __init__(self, message: str, *, provider: str = "", retryable: bool = False,
                 status: int | None = None):
        super().__init__(message)
        self.provider  = provider
        self.retryable = retryable
        self.status    = status


class ProviderConfigError(ProviderError):
    """Missing key, missing model name, unknown provider — a setup problem."""


class CapabilityNotSupported(ProviderError):
    def __init__(self, provider: str, capability: "Capability | str"):
        cap = capability.value if isinstance(capability, Capability) else str(capability)
        super().__init__(f"The '{provider}' provider does not support {cap}.",
                         provider=provider, retryable=False)
        self.capability = cap


class RateLimited(ProviderError):
    def __init__(self, message: str, provider: str = "", status: int | None = 429):
        super().__init__(message, provider=provider, retryable=True, status=status)


# ── Content ──────────────────────────────────────────────────────────────────

_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"GIF87a", "image/gif"),
          (b"GIF89a", "image/gif"), (b"BM", "image/bmp"))


def sniff_image_mime(data: bytes) -> str:
    """The real image type from the bytes ('' if unknown). A mislabelled image
    (JPEG bytes declared as PNG) is rejected or misread by some providers, so
    the declared type is never trusted over the bytes."""
    b = bytes(data[:16]) if data else b""
    for sig, mime in _MAGIC:
        if b.startswith(sig):
            return mime
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return ""


@dataclass
class Part:
    kind: str                     # "text" | "image" | "audio" | "document"
    text: str = ""
    data: bytes = b""
    mime_type: str = ""

    @staticmethod
    def of_text(t: str) -> "Part":
        return Part("text", text=str(t))

    @staticmethod
    def of_image(data: bytes, mime_type: str = "image/png") -> "Part":
        return Part("image", data=data, mime_type=sniff_image_mime(data) or mime_type)

    @staticmethod
    def of_audio(data: bytes, mime_type: str = "audio/mpeg") -> "Part":
        return Part("audio", data=data, mime_type=mime_type)

    @staticmethod
    def of_document(data: bytes, mime_type: str = "application/pdf") -> "Part":
        return Part("document", data=data, mime_type=mime_type)

    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


@dataclass
class Message:
    role: str                     # "user" | "assistant" | "tool"
    parts: list[Part] = field(default_factory=list)
    # for role == "tool": which call this answers
    tool_call_id: str = ""
    tool_name: str = ""
    # for role == "assistant": tool calls the model made in this turn
    tool_calls: list["ToolCall"] = field(default_factory=list)

    @staticmethod
    def user(*items) -> "Message":
        return Message("user", [p if isinstance(p, Part) else Part.of_text(p) for p in items])

    @staticmethod
    def assistant(text: str) -> "Message":
        return Message("assistant", [Part.of_text(text)])

    def text(self) -> str:
        return "".join(p.text for p in self.parts if p.kind == "text")


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = ""


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class AIRequest:
    messages: list[Message]
    system: str = ""
    model: str = ""
    max_output_tokens: int | None = None
    temperature: float | None = None
    tools: list[ToolSpec] = field(default_factory=list)
    tool_choice: str = "auto"               # "auto" | "none" | "required" | <tool name>
    response_schema: dict | None = None     # JSON Schema → structured output
    reasoning: ReasoningEffort | None = None
    web_grounding: bool = False
    timeout: float = 60.0
    response_modalities: list[str] | None = None   # e.g. ["TEXT", "IMAGE"] for image output
    image_aspect: str | None = None                # "1:1", "16:9", … (image output only)

    def required_capabilities(self) -> set[Capability]:
        caps = {Capability.TEXT}
        for m in self.messages:
            for p in m.parts:
                if p.kind == "image":
                    caps.add(Capability.VISION)
                elif p.kind == "audio":
                    caps.add(Capability.AUDIO_INPUT)
                elif p.kind == "document":
                    caps.add(Capability.DOCUMENT_INPUT)
        if self.tools:
            caps.add(Capability.TOOLS)
        if self.response_schema is not None:
            caps.add(Capability.STRUCTURED)
        if self.reasoning not in (None, ReasoningEffort.NONE):
            caps.add(Capability.REASONING)
        if self.web_grounding:
            caps.add(Capability.WEB_GROUNDING)
        if self.response_modalities and "IMAGE" in [m.upper() for m in self.response_modalities]:
            caps.add(Capability.IMAGE_OUTPUT)
        return caps


@dataclass
class AIResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    provider: str = ""
    model: str = ""
    data: Any = None           # parsed JSON when response_schema was requested
    meta: dict = field(default_factory=dict)   # gateway routing info (tier, route, degraded …)
    images: list = field(default_factory=list) # generated images: [(bytes, mime), …]

    def json(self) -> Any:
        """Parse the text as JSON (tolerating ```json fences). Raises
        ProviderError on invalid JSON rather than returning garbage."""
        if self.data is not None:
            return self.data
        return parse_json_text(self.text, provider=self.provider)


def parse_json_text(text: str, provider: str = "") -> Any:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        # last resort: first {...} or [...] block
        m = re.search(r"(\{.*\}|\[.*\])", t, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    raise ProviderError("Model did not return valid JSON.", provider=provider)


# ── Interface ────────────────────────────────────────────────────────────────

class AIProvider(ABC):
    """Every provider implements generate(); stream() and count_tokens() have
    honest defaults (stream = one chunk; count = unsupported)."""

    name: str = "abstract"
    capabilities: frozenset = frozenset({Capability.TEXT})
    default_model: str = ""
    fast_model: str = ""
    context_window: int | None = None      # tokens, when known for the default model

    def supports(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def check(self, req: AIRequest) -> None:
        for cap in req.required_capabilities():
            if cap not in self.capabilities:
                raise CapabilityNotSupported(self.name, cap)

    @abstractmethod
    def generate(self, req: AIRequest) -> AIResponse:
        raise NotImplementedError

    def stream(self, req: AIRequest) -> Iterator[str]:
        """Yield text chunks. Default: a single chunk from generate(), so every
        provider can be *called* in streaming style even if it cannot stream."""
        yield self.generate(req).text

    def count_tokens(self, req: AIRequest) -> int:
        raise CapabilityNotSupported(self.name, Capability.TOKEN_COUNT)

    def list_models(self) -> list[str]:
        """Model ids this key can see (empty when the provider can't list)."""
        return []

    def info(self) -> dict:
        return {
            "provider": self.name,
            "default_model": self.default_model,
            "fast_model": self.fast_model or self.default_model,
            "context_window": self.context_window,
            "capabilities": sorted(c.value for c in self.capabilities),
        }


# ── HTTP transport (injectable for tests) ────────────────────────────────────
# Anthropic/OpenAI/local providers speak plain HTTPS through this tiny seam,
# so they need no vendor SDK and can be tested with a fake transport.

@dataclass
class HTTPResult:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)

    def json(self):
        return json.loads(self.body.decode("utf-8") or "null")


Transport = Callable[..., HTTPResult]


def default_transport(method: str, url: str, *, headers: dict, body: dict | None,
                      timeout: float, stream: bool = False):
    """requests-based transport. With stream=True returns an iterator of raw
    text lines instead of an HTTPResult."""
    import requests
    resp = requests.request(method, url, headers=headers,
                            data=(json.dumps(body).encode("utf-8") if body is not None else None),
                            timeout=timeout, stream=stream)
    if stream:
        if resp.status_code >= 400:
            return HTTPResult(resp.status_code, resp.content, dict(resp.headers))
        return (line.decode("utf-8", "replace") if isinstance(line, bytes) else line
                for line in resp.iter_lines())
    return HTTPResult(resp.status_code, resp.content, dict(resp.headers))


def raise_for_http(provider: str, res: HTTPResult) -> None:
    """Map HTTP status to our error types — with the body trimmed and redacted
    (vendor error bodies sometimes echo request headers)."""
    if res.status < 400:
        return
    from core.secret_store import redact
    try:
        detail = res.body.decode("utf-8", "replace")[:300]
    except Exception:
        detail = ""
    detail = redact(detail)
    if res.status == 429:
        raise RateLimited(f"{provider}: rate limited ({detail})", provider=provider)
    if res.status in (401, 403):
        raise ProviderConfigError(f"{provider}: authentication failed — check the API key "
                                  f"(HTTP {res.status}).", provider=provider, status=res.status)
    retry = res.status >= 500 or res.status == 408
    raise ProviderError(f"{provider}: HTTP {res.status} — {detail}", provider=provider,
                        retryable=retry, status=res.status)
