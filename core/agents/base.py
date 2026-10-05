"""
core/agents/base.py — what an agent is, and the only door it has to the world.

An Agent is a specialist: it owns a set of tools (by name prefix), knows the
words that signal its domain (for routing), and executes plan steps in its
domain. It does NOT hold a reference to the registry, the orchestrator, or
any other agent. Everything it can do goes through AgentServices, which the
orchestrator builds per step:

    services.call_tool(name, args)   → only the tool this step was approved
                                       for, or a SAFE read-only tool the agent
                                       owns (lookups). Anything else raises
                                       PermissionError — an agent cannot
                                       reach around the permission engine.
    services.ask_ai(prompt, ...)     → the AI gateway (provider-agnostic)
    services.inputs                  → structured outputs of prior steps
    services.log(msg)                → audit/HUD log

Adding an agent: subclass Agent, set name/description/keywords/tool_prefixes,
and register it (see core/agents/builtin.py → default_agents()). The router
picks it up from its keywords; no router code changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from core.permissions import RiskLevel
from core.tools.registry import ToolContext, ToolRegistry, ToolResult


class AgentServices:
    def __init__(self, registry: ToolRegistry, approved_tool: str, owner: "Agent",
                 ctx: ToolContext, inputs: dict, ask_ai: Callable[..., str] | None = None,
                 log: Callable[[str], None] | None = None):
        self._registry = registry
        self._approved = approved_tool
        self._owner = owner
        self._ctx = ctx
        self.inputs = inputs
        self._ask_ai = ask_ai
        self._log = log
        self.calls: list[str] = []

    def call_tool(self, name: str, args: dict) -> ToolResult:
        spec = self._registry.get(name)
        if spec is None:
            raise PermissionError(f"Unknown tool '{name}'.")
        owned_lookup = ((self._owner.owns(name) or spec.agent == self._owner.name)
                        and spec.risk_for(args) == RiskLevel.SAFE)
        if name != self._approved and not owned_lookup:
            raise PermissionError(
                f"Agent '{self._owner.name}' tried to call '{name}' outside its approved step "
                f"('{self._approved}'). Blocked by the permission layer.")
        self.calls.append(name)
        return self._registry.invoke(name, args, self._ctx)

    def ask_ai(self, prompt: str, **kw) -> str:
        if self._ask_ai is None:
            raise RuntimeError("No AI gateway available.")
        return self._ask_ai(prompt, **kw)

    def log(self, msg: str) -> None:
        if self._log:
            try:
                self._log(msg)
            except Exception:
                pass


class Agent:
    name: str = "general"
    title: str = "General Agent"
    description: str = ""
    # keyword → weight. Multi-word phrases allowed. Matched against the
    # normalised (English-keyword) text, so Tamil/Tanglish work too.
    keywords: dict = {}
    tool_prefixes: tuple = ()
    extra_tools: tuple = ()           # full tool names owned outside the prefixes

    def owns(self, tool_name: str) -> bool:
        return tool_name in self.extra_tools or any(tool_name.startswith(p) for p in self.tool_prefixes)

    def prepare_args(self, tool: str, args: dict, services: AgentServices) -> dict:
        """Hook: an agent may enrich arguments (e.g. resolve a folder name)
        before execution. Default: unchanged."""
        return args

    def execute(self, step, args: dict, services: AgentServices) -> ToolResult:
        """Default behaviour: run the approved tool with prepared args."""
        return services.call_tool(step.tool, self.prepare_args(step.tool, args, services))

    def describe(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description,
                "tool_prefixes": list(self.tool_prefixes)}


class AgentRegistry:
    def __init__(self, agents: list[Agent] | None = None):
        self._agents: dict[str, Agent] = {}
        for a in agents or []:
            self.register(a)

    def register(self, agent: Agent) -> None:
        self._agents[agent.name] = agent

    def get(self, name: str) -> Optional[Agent]:
        return self._agents.get(name)

    def all(self) -> list[Agent]:
        return list(self._agents.values())

    def owner_of(self, tool_name: str) -> Agent:
        best = None
        best_len = -1
        for a in self._agents.values():
            if tool_name in a.extra_tools:
                return a
            for p in a.tool_prefixes:
                if tool_name.startswith(p) and len(p) > best_len:
                    best, best_len = a, len(p)
        return best or self._agents.get("general") or next(iter(self._agents.values()))
