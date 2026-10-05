"""
core/permissions.py — the Permission Engine: one place that decides whether a
tool call may run now, must wait for a human, or must not run at all.

    caller (Live tool call, orchestrator step, workflow, plugin)
        │
        ▼
    PermissionEngine.decide(tool, args, context)
        │  risk  = ToolRegistry.risk_for(tool, args)      (5 levels below)
        │  valid = ToolRegistry.validate_args(tool, args)
        ▼
    ALLOW | CONFIRM | DENY

RISK LEVELS
    SAFE          read-only, no side effects         (read a file, system status)
    LOW_RISK      trivially reversible               (open an app, set volume)
    MEDIUM_RISK   changes real state, recoverable    (move/rename/copy a file — undoable)
    HIGH_RISK     hard to reverse or affects others  (delete a file, send a message,
                                                      restart the PC, run AI-written code)
    CRITICAL      money, security, credentials       (purchases, security settings)

DEFAULT POLICY
    SAFE / LOW / MEDIUM → ALLOW (MEDIUM actions register undo entries instead
                          of asking — the existing core/undo.py design: an
                          assistant that asks before every file move is one
                          nobody uses)
    HIGH                → CONFIRM (human presses CONFIRM on the HUD)
    CRITICAL            → CONFIRM, and can NEVER be added to trusted actions
    invalid args        → DENY (the model sent something that doesn't match
                          the tool's schema — don't guess)
    unknown tool        → DENY

    Stricter contexts: a request that originated on a remote device (the
    Telegram pocket line passes "telegram"), or a replayed workflow, may pass
    `context={"origin": "remote"}` — then MEDIUM also needs confirmation.

TRUSTED ACTIONS (user-configurable)
    config/api_keys.json:  "trusted_actions": ["send_message", "file_controller.delete"]
    Each entry is a tool name or tool.action. Trusting skips the confirmation
    for HIGH_RISK only; CRITICAL is never skippable.

Silence is never consent: the confirmation layer (core/confirm.py) expires a
pending request as a *denial*.
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from core.tools.registry import ToolRegistry


class RiskLevel(enum.IntEnum):
    SAFE = 0
    LOW_RISK = 1
    MEDIUM_RISK = 2
    HIGH_RISK = 3
    CRITICAL = 4

    @classmethod
    def parse(cls, value, default: "RiskLevel" = None) -> "RiskLevel":
        if isinstance(value, RiskLevel):
            return value
        if value is None:
            return default if default is not None else cls.MEDIUM_RISK
        v = str(value).strip().lower().replace("-", "_")
        table = {
            "safe": cls.SAFE, "none": cls.SAFE, "read": cls.SAFE, "read_only": cls.SAFE,
            "low": cls.LOW_RISK, "low_risk": cls.LOW_RISK,
            "medium": cls.MEDIUM_RISK, "medium_risk": cls.MEDIUM_RISK,
            "high": cls.HIGH_RISK, "high_risk": cls.HIGH_RISK,
            "critical": cls.CRITICAL,
        }
        if v in table:
            return table[v]
        return default if default is not None else cls.MEDIUM_RISK

    @property
    def label(self) -> str:
        return self.name


class Decision(str, enum.Enum):
    ALLOW = "ALLOW"
    CONFIRM = "CONFIRM"
    DENY = "DENY"


@dataclass
class PermissionDecision:
    decision: Decision
    risk: RiskLevel
    reason: str
    tool: str
    action: str = ""
    trusted: bool = False
    errors: list = field(default_factory=list)
    grant: list = field(default_factory=list)    # plugin permissions to remember once approved

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.decision == Decision.CONFIRM

    def to_dict(self) -> dict:
        return {"decision": self.decision.value, "risk": self.risk.name, "reason": self.reason,
                "tool": self.tool, "action": self.action, "trusted": self.trusted}


@dataclass
class PermissionPolicy:
    confirm_at: RiskLevel = RiskLevel.HIGH_RISK
    remote_confirm_at: RiskLevel = RiskLevel.MEDIUM_RISK
    trusted: set = field(default_factory=set)
    denied: set = field(default_factory=set)      # user-configured hard blocks
    grants: dict = field(default_factory=dict)    # plugin → {"network", …} approved once (Phase 5)

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> "PermissionPolicy":
        if cfg is None:
            cfg = _read_config()
        trusted = {str(x).strip().lower() for x in (cfg.get("trusted_actions") or []) if str(x).strip()}
        denied = {str(x).strip().lower() for x in (cfg.get("blocked_actions") or []) if str(x).strip()}
        grants = {str(k): {str(p) for p in (v or [])}
                  for k, v in (cfg.get("plugin_permissions") or {}).items() if isinstance(v, (list, tuple))}
        return cls(trusted=trusted, denied=denied, grants=grants)


# Phase 7: actions that "Allow always" is never offered for, whatever their risk
# level — power, installing code, the PC's own security. (CRITICAL is never
# trustable either; that is enforced separately.)
NEVER_ALWAYS = {
    "install_plugin", "plugin.install", "code.run", "code_helper.run",
    "computer_settings.shutdown", "computer_settings.restart", "computer_settings.sleep",
    "computer_settings.hibernate", "computer_settings.lock", "computer_settings.log_off",
    "computer_settings.toggle_wifi", "computer_settings.wifi_off", "file_controller.delete_permanent",
    "emergency_stop",
}


def _trust_risk(key: str):
    """Risk of a trust key (tool or tool.action) according to the live registry."""
    try:
        from core.tools.registry import get_registry
        reg = get_registry()
    except Exception:
        return None
    tool, _, action = key.partition(".")
    for name in (key, tool):
        spec = reg.get(name) if reg is not None else None
        if spec is not None:
            try:
                return reg.risk_for(name, {"action": action} if action and name == tool else {})
            except Exception:
                return getattr(spec, "risk", None)
    return None


def can_trust(key: str) -> bool:
    k = str(key or "").strip().lower()
    if not k or k in NEVER_ALWAYS or k.split(".")[0] in NEVER_ALWAYS:
        return False
    r = _trust_risk(k)
    return not (r is not None and RiskLevel.parse(r) >= RiskLevel.CRITICAL)


def _write_config_list(field_name: str, mutate) -> list:
    import os
    import tempfile
    from core.secret_store import legacy_config_path
    p = legacy_config_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    cur = [str(x) for x in (data.get(field_name) or []) if str(x).strip()]
    new = mutate(list(cur))
    data[field_name] = new
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="api_keys.", suffix=".tmp", dir=str(p.parent))
    with os.fdopen(fd, "wb") as f:
        f.write(json.dumps(data, indent=4).encode("utf-8"))
    os.replace(tmp, p)
    return new


def trusted_list() -> list[str]:
    return sorted(PermissionPolicy.from_config().trusted)


def add_trusted(key: str) -> bool:
    """'Allow always' — persist; refuses keys that may never be trusted."""
    k = str(key or "").strip().lower()
    if not can_trust(k):
        return False
    _write_config_list("trusted_actions", lambda cur: cur if k in [c.lower() for c in cur] else cur + [k])
    return k in trusted_list()


def remove_trusted(key: str) -> bool:
    """Revoke an 'Allow always' (Settings → Permissions). Returns True when gone."""
    k = str(key or "").strip().lower()
    _write_config_list("trusted_actions", lambda cur: [c for c in cur if c.lower() != k])
    return k not in trusted_list()


def _read_config() -> dict:
    try:
        from core.secret_store import legacy_config_path
        return json.loads(legacy_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


class PermissionEngine:
    def __init__(self, registry: "ToolRegistry", policy: PermissionPolicy | None = None,
                 policy_loader: Callable[[], PermissionPolicy] | None = None):
        self._registry = registry
        self._policy = policy
        self._loader = policy_loader or PermissionPolicy.from_config

    @property
    def policy(self) -> PermissionPolicy:
        # Re-read each time unless fixed: a user editing trusted_actions takes
        # effect without a restart. Reading a small JSON file is microseconds.
        return self._policy or self._loader()

    def decide(self, tool: str, args: dict | None = None, context: dict | None = None) -> PermissionDecision:
        args = dict(args or {})
        ctx = context or {}
        spec = self._registry.get(tool)
        action = str(args.get("action", "") or "").strip().lower()
        if spec is None:
            return PermissionDecision(Decision.DENY, RiskLevel.CRITICAL,
                                      f"Unknown tool '{tool}' — refusing to run it.", tool, action)
        errors = self._registry.validate_args(tool, args)
        risk = self._registry.risk_for(tool, args)
        if errors:
            return PermissionDecision(Decision.DENY, risk,
                                      "Invalid parameters: " + "; ".join(errors[:4]), tool, action,
                                      errors=errors)
        pol = self.policy
        keys = {tool.lower(), f"{tool.lower()}.{action}"} if action else {tool.lower()}
        if keys & pol.denied:
            return PermissionDecision(Decision.DENY, risk, "This action is blocked in your settings.",
                                      tool, action)
        if risk >= RiskLevel.CRITICAL:
            return PermissionDecision(Decision.CONFIRM, risk,
                                      "Critical action — always needs your explicit confirmation.",
                                      tool, action)
        if spec.requires_confirmation is True:
            if (keys & pol.trusted) and risk < RiskLevel.CRITICAL:
                return PermissionDecision(Decision.ALLOW, risk, "Trusted by your settings.", tool, action,
                                          trusted=True)
            return PermissionDecision(Decision.CONFIRM, risk, "This tool always asks first.", tool, action)
        threshold = pol.remote_confirm_at if ctx.get("origin") in ("remote", "telegram", "workflow_untrusted") \
            else pol.confirm_at
        if risk >= threshold:
            if keys & pol.trusted:
                return PermissionDecision(Decision.ALLOW, risk, "Trusted by your settings.", tool, action,
                                          trusted=True)
            return PermissionDecision(Decision.CONFIRM, risk,
                                      f"{risk.name.replace('_', ' ').title()} action — needs confirmation.",
                                      tool, action)
        # Phase 5 — capability permissions: a plugin's declared needs ("network",
        # "filesystem", …) are confirmed on first use, then remembered.
        if getattr(spec, "source", "") == "plugin" and spec.permissions:
            missing = sorted(set(map(str, spec.permissions)) - set(pol.grants.get(tool, set())))
            if missing:
                return PermissionDecision(Decision.CONFIRM, risk,
                                          f"First use of the {tool} plugin — it needs: {', '.join(missing)}. "
                                          f"Approving remembers this.", tool, action, grant=missing)
        return PermissionDecision(Decision.ALLOW, risk, f"{risk.name} — allowed.", tool, action)

    def record_grant(self, tool: str, perms: list) -> None:
        """Persist an approved plugin permission (config plugin_permissions)."""
        if not perms:
            return
        try:
            import os
            import tempfile
            from core.secret_store import legacy_config_path
            p = legacy_config_path()
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            g = data.get("plugin_permissions") if isinstance(data.get("plugin_permissions"), dict) else {}
            g[tool] = sorted(set(g.get(tool) or []) | set(map(str, perms)))
            data["plugin_permissions"] = g
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="api_keys.", suffix=".tmp", dir=str(p.parent))
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(data, indent=4).encode("utf-8"))
            os.replace(tmp, p)
        except Exception:
            pass
        if self._policy is not None:
            self._policy.grants.setdefault(tool, set()).update(map(str, perms))
