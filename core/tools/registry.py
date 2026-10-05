"""
core/tools/registry.py — the single source of truth for what JARVIS can do.

Every capability — the 26 original Live tools, the structured agent tools, and
every plugin — is described by one ToolSpec here: name, schema, owning agent,
risk, confirmation, undo, verification, retry-safety, alternatives. The
permission engine, planner, orchestrator, audit log and HUD all read from this
one table instead of each keeping their own idea of what a tool is.

The registry does NOT re-implement actions. Handlers are thin adapters around
the existing functions in actions/*.py (see core/tools/builtin.py), imported
lazily so building the registry never pulls in pyautogui/Qt/genai.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from core.permissions import RiskLevel
from core.verification import VerificationResult


@dataclass
class ToolResult:
    ok: bool
    data: dict = field(default_factory=dict)     # structured output for other agents
    message: str = ""                             # human/model-facing sentence
    error: str = ""
    retryable: bool = False                       # transient failure worth retrying

    @staticmethod
    def success(message: str = "", **data) -> "ToolResult":
        return ToolResult(True, dict(data), message)

    @staticmethod
    def failure(error: str, retryable: bool = False, **data) -> "ToolResult":
        return ToolResult(False, dict(data), "", error, retryable)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "data": self.data, "message": self.message, "error": self.error}


@dataclass
class ToolContext:
    """What a handler may use. Deliberately small: no registry, no orchestrator,
    no other agents — a tool cannot reach around the permission layer."""
    player: Any = None              # the UI (write_log etc.), may be None
    speak: Optional[Callable[[str], None]] = None
    task_id: str = ""
    step_id: str = ""
    extras: dict = field(default_factory=dict)


RiskSpec = "RiskLevel | Callable[[dict], RiskLevel]"


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: Callable[[dict, ToolContext], Any]
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    output_schema: dict | None = None
    agent: str = "general"
    risk: Any = RiskLevel.MEDIUM_RISK                  # RiskLevel or fn(args) -> RiskLevel
    requires_confirmation: Optional[bool] = None       # None = decided by risk policy
    undoable: bool = False
    verify: Optional[Callable[[dict, ToolResult], VerificationResult]] = None
    verification_required: bool = False
    retry_safe: bool = False                           # idempotent / safe to re-run
    alternatives: list = field(default_factory=list)   # tool names to try if this fails
    self_gated: bool = False                           # action asks via core/confirm itself
    source: str = "core"                               # core | action | plugin
    live_exposed: bool = False                         # declared to Gemini Live directly
    planner_visible: bool = True                       # offered to the task planner
    confirm_text: Optional[Callable[[dict], str]] = None
    capabilities: list = field(default_factory=list)
    permissions: list = field(default_factory=list)    # declared needs: filesystem, network, ...
    timeout: Optional[float] = None                    # seconds; None → config/runtime.json default
    resources: list = field(default_factory=list)      # exclusive resources, e.g. ["gui"] (mouse+keyboard)

    def risk_for(self, args: dict) -> RiskLevel:
        r = self.risk
        if callable(r) and not isinstance(r, RiskLevel):
            try:
                return RiskLevel.parse(r(args or {}))
            except Exception:
                return RiskLevel.HIGH_RISK          # a broken risk function fails closed
        return RiskLevel.parse(r)

    def describe(self) -> str:
        if self.confirm_text:
            try:
                return self.confirm_text({})
            except Exception:
                pass
        return self.description

    def metadata(self) -> dict:
        r = self.risk
        return {
            "name": self.name, "description": self.description, "agent": self.agent,
            "risk": (r.name if isinstance(r, RiskLevel) else "varies by action"),
            "requires_confirmation": self.requires_confirmation,
            "undoable": self.undoable, "verification": bool(self.verify),
            "verification_required": self.verification_required,
            "retry_safe": self.retry_safe, "alternatives": list(self.alternatives),
            "source": self.source, "input_schema": self.input_schema,
            "output_schema": self.output_schema, "permissions": list(self.permissions),
            "timeout": timeout_for(self), "resources": resources_for(self),
        }


# Tools registered before Phase 6 (legacy action wrappers, Phase 4/5 tools)
# get their timeout / resources from these tables; new tools declare them on
# the ToolSpec. config/runtime.json → tasks.tool_timeouts overrides both.
DEFAULT_TIMEOUTS = {
    "research.collect": 600, "research.analyze": 420, "document.write": 240, "document.summarize": 240,
    "project.analyze": 600, "project.scan": 300, "team.plan": 240, "team.research": 420, "team.design": 300,
    "team.implement": 900, "team.test": 600, "team.review": 600, "computer.run": 900,
    "dev_agent": 1500, "code_helper": 900, "browser.download": 900, "file_processor": 600,
    "game_updater": 1800, "flight_finder": 300, "browser_control": 300, "screen_process": 180,
    "plugin.install": 180, "message.send": 180, "send_message": 180, "youtube_video": 180,
}
GUI_TOOLS = {"computer.run", "computer_control", "desktop_control", "message.send", "send_message"}


def timeout_for(spec: "ToolSpec") -> float:
    try:
        from core.config import runtime
        override = (runtime("tasks", "tool_timeouts", {}) or {}).get(spec.name)
        default = float(runtime("tasks", "default_tool_timeout", 300) or 300)
    except Exception:
        override, default = None, 300.0
    for v in (override, spec.timeout, DEFAULT_TIMEOUTS.get(spec.name)):
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return default


def resources_for(spec: "ToolSpec") -> list:
    res = list(spec.resources or [])
    if spec.name in GUI_TOOLS and "gui" not in res:
        res.append("gui")
    return res


# ── Minimal JSON-Schema validation (no dependency) ───────────────────────────
_PY_TYPES = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
    "object": (dict,), "array": (list, tuple),
}


def _validate(value: Any, schema: dict, path: str, errors: list, allow_refs: bool) -> None:
    if not isinstance(schema, dict):
        return
    if allow_refs and isinstance(value, str) and value.startswith("$"):
        return                                   # "$s1.path" — resolved at run time
    t = schema.get("type")
    if isinstance(t, str):
        t = t.lower()
        py = _PY_TYPES.get(t)
        if py:
            ok = isinstance(value, py) and not (t in ("integer", "number") and isinstance(value, bool))
            # tolerate numeric strings for integer/number (LLMs send "10")
            if not ok and t in ("integer", "number") and isinstance(value, str):
                try:
                    float(value)
                    ok = True
                except ValueError:
                    ok = False
            if not ok and t == "boolean" and isinstance(value, str) and value.lower() in ("true", "false"):
                ok = True
            if not ok:
                errors.append(f"{path or 'value'} should be {t}")
                return
    enum_vals = schema.get("enum")
    if enum_vals is not None and value not in enum_vals:
        if not (isinstance(value, str) and value.lower() in [str(e).lower() for e in enum_vals]):
            errors.append(f"{path or 'value'} must be one of {list(enum_vals)[:8]}")
    if isinstance(value, str):
        ml = schema.get("maxLength")
        if isinstance(ml, int) and len(value) > ml:
            errors.append(f"{path or 'value'} is too long (max {ml})")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in value or value[req] in (None, ""):
                errors.append(f"missing required '{req}'")
        for k, v in value.items():
            if k in props:
                _validate(v, props[k], f"{path}.{k}" if path else k, errors, allow_refs)
    if isinstance(value, (list, tuple)) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value[:50]):
            _validate(v, schema["items"], f"{path}[{i}]", errors, allow_refs)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}
        self._lock = threading.RLock()

    def register(self, spec: ToolSpec, replace: bool = False) -> ToolSpec:
        if not spec.name or not callable(spec.handler):
            raise ValueError("ToolSpec needs a name and a callable handler.")
        with self._lock:
            if spec.name in self._tools and not replace:
                raise ValueError(f"Tool '{spec.name}' is already registered.")
            self._tools[spec.name] = spec
        return spec

    def unregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def list(self, agent: str | None = None, source: str | None = None) -> list[ToolSpec]:
        out = list(self._tools.values())
        if agent:
            out = [t for t in out if t.agent == agent]
        if source:
            out = [t for t in out if t.source == source]
        return sorted(out, key=lambda t: t.name)

    def risk_for(self, name: str, args: dict | None = None) -> RiskLevel:
        spec = self.get(name)
        if spec is None:
            return RiskLevel.CRITICAL
        return spec.risk_for(args or {})

    def timeout_for(self, name: str) -> float:
        spec = self.get(name)
        return timeout_for(spec) if spec else 300.0

    def resources_for(self, name: str) -> list:
        spec = self.get(name)
        return resources_for(spec) if spec else []

    def validate_args(self, name: str, args: dict | None, allow_refs: bool = False) -> list[str]:
        spec = self.get(name)
        if spec is None:
            return [f"unknown tool '{name}'"]
        errors: list[str] = []
        _validate(dict(args or {}), spec.input_schema or {}, "", errors, allow_refs)
        return errors

    def invoke(self, name: str, args: dict, ctx: ToolContext | None = None) -> ToolResult:
        """Run the handler and normalise its return value to a ToolResult.

        NOTE: invoke() performs NO permission check by design — it is the
        execution primitive the permission-checked paths (PermissionGate in
        core/jarvis_core.py, Orchestrator) call *after* deciding. Nothing else
        should call it directly."""
        spec = self.get(name)
        if spec is None:
            return ToolResult.failure(f"Unknown tool '{name}'.")
        try:
            raw = spec.handler(dict(args or {}), ctx or ToolContext())
        except Exception as e:
            if getattr(e, "control_flow", False):     # task cancel / stop signals pass through
                raise
            from core.secret_store import redact
            return ToolResult.failure(f"{name} raised {type(e).__name__}: {redact(e)[:200]}",
                                      retryable=spec.retry_safe)
        return normalise_result(raw)

    def verify(self, name: str, args: dict, result: ToolResult) -> VerificationResult:
        from core.verification import VStatus, VerificationResult as VR
        spec = self.get(name)
        if spec is None or spec.verify is None:
            return VR(VStatus.SKIPPED, "No verifier for this tool.")
        try:
            return spec.verify(dict(args or {}), result)
        except Exception as e:
            return VR(VStatus.UNVERIFIABLE, f"Verifier error: {type(e).__name__}")

    def catalog(self, planner_only: bool = True) -> list[dict]:
        """Compact description list for the LLM planner."""
        out = []
        for t in self.list():
            if planner_only and not t.planner_visible:
                continue
            props = (t.input_schema or {}).get("properties") or {}
            out.append({"tool": t.name, "agent": t.agent, "description": t.description[:220],
                        "args": {k: (v.get("type", "any") if isinstance(v, dict) else "any")
                                 for k, v in props.items()},
                        "required": list((t.input_schema or {}).get("required") or []),
                        "risk": t.risk.name if isinstance(t.risk, RiskLevel) else "varies"})
        return out


_CURRENT: dict = {"reg": None}


def set_current(reg: "ToolRegistry | None") -> None:
    """The registry the running assistant uses (set by JarvisCore) — lets
    core.permissions judge an 'Allow always' key without being handed one."""
    _CURRENT["reg"] = reg


def get_registry() -> "ToolRegistry | None":
    return _CURRENT["reg"]


def normalise_result(raw: Any) -> ToolResult:
    """Legacy actions return strings; structured tools return ToolResult or
    dicts. A plain string counts as ok unless it reads like a failure — the
    verifier (not this heuristic) is what decides whether something really
    happened."""
    from core.verification import legacy_result_failed
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, dict):
        ok = bool(raw.get("ok", True))
        msg = str(raw.get("message", "") or "")
        return ToolResult(ok, {k: v for k, v in raw.items() if k not in ("ok", "message", "error")},
                          msg, str(raw.get("error", "") or ""))
    text = "" if raw is None else str(raw)
    if legacy_result_failed(text):
        return ToolResult(False, {"text": text}, "", text)
    return ToolResult(True, {"text": text}, text or "Done.")
