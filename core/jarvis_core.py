"""
core/jarvis_core.py — composition root for the agent architecture.

main.py builds ONE JarvisCore and talks to it through a handful of methods.
Everything else is wired here, by dependency injection, so no module imports
main.py or ui.py (no circular imports) and every piece can be tested alone.

    User ─voice─► Gemini Live (main.py) ─tool call─►  JarvisCore
                                                       ├─ gate_direct_call()   PermissionGate: every direct
                                                       │                       tool call → PermissionEngine
                                                       ├─ after_direct_call()  audit + verification + workflow
                                                       ├─ run_task()           Router → Planner → Orchestrator
                                                       ├─ task_control()       status / cancel / pause / resume /
                                                       │                       answer / history ("what did you do?")
                                                       ├─ workflow()           list / suggestions / save / run
                                                       ├─ memory_inspect()     MemoryHub
                                                       └─ set_language()       core/multilingual
    Shared services: ToolRegistry · PermissionEngine · AgentRegistry · AgentRouter ·
    TaskPlanner · TaskStore · Orchestrator · AuditLog · WorkflowStore · MemoryHub ·
    NotificationPolicy · DeviceRouter · PairingManager

    Phase 5 — Cognitive Core (core/cognition.py):
                                                       ├─ think()              model router + context + self-critique
                                                       ├─ recall_context()     Context Engine (minimal relevant context)
                                                       ├─ save_fact()          selective memory policy
                                                       ├─ emergency_stop()     kill switch: tasks, HUD prompts, computer agent
                                                       ├─ capabilities()       capability search / plugin catalog
                                                       ├─ install_plugin()     HUD-confirmed, checksum-pinned install
                                                       ├─ analyze_screen()     vision modes
                                                       └─ computer_use()       observe→act→verify agent (as a task)
    Every finished task: SELF-CHECK (verify) → episode + project activity (learn).

    Part One — knowledge, sources, memory growth, model switching:
                                                       ├─ recall_memory()      memory layer (sources tracked)
                                                       ├─ search_knowledge()   notes/docs + memory + history
                                                       ├─ show_sources()       what the last answer used
                                                       ├─ remember()           "remember that…" → verified write
                                                       ├─ finish_turn()        per-turn source record
                                                       └─ switch_model()       exact model or refusal
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from core.agents.builtin import default_agents
from core.agents.orchestrator import HUDConfirmer, Orchestrator
from core.agents.planner import TaskPlanner
from core.agents.router import AgentRouter
from core.audit import AuditLog
from core.devices.base import DeviceRouter, LaptopDevice
from core.permissions import Decision, PermissionEngine, RiskLevel
from core.proactive_policy import NotificationPolicy, Priority, ProactiveRegistry
from core.tasks.state import Task, TaskState, TaskStore
from core.tools.builtin import build_default_registry
from core.tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec, normalise_result
from core.workflows import WorkflowStore
from core import verification as V
from core.safety import kill_switch

# Task state → existing HUD state names (ui.py already renders THINKING,
# EXECUTING, WARNING, ERROR, LISTENING). No HUD code changes needed.
HUD_STATE = {
    TaskState.QUEUED: "THINKING",
    TaskState.PLANNING: "THINKING",
    TaskState.RUNNING: "EXECUTING",
    TaskState.VERIFYING: "EXECUTING",
    TaskState.WAITING_FOR_PERMISSION: "WARNING",
    TaskState.WAITING_FOR_INPUT: "LISTENING",
    TaskState.PAUSED: "LISTENING",
    TaskState.BLOCKED: "THINKING",
    TaskState.RECOVERING: "THINKING",
    TaskState.COMPLETED: "LISTENING",
    TaskState.CANCELLED: "LISTENING",
    TaskState.FAILED: "ERROR",
}


def register_plugin_tools(reg: ToolRegistry, plugin_registry) -> int:
    """Every valid plugin becomes a ToolSpec — same permission gate, audit and
    verification as core tools. Plugins cannot opt out of the gate."""
    if plugin_registry is None or not hasattr(plugin_registry, "records"):
        return 0
    n = 0
    for rec in plugin_registry.records():
        register_plugin_record(reg, plugin_registry, rec)
        n += 1
    return n


def register_plugin_record(reg: ToolRegistry, plugin_registry, rec) -> None:
    """One plugin → one ToolSpec (also used for runtime installs)."""
    if True:
        def handler(args, ctx, _name=rec.name):
            return plugin_registry.run(_name, args, player=ctx.player, session_memory=None)

        verify = None
        if rec.verify is not None:
            def verify(args, result: ToolResult, _fn=rec.verify):
                try:
                    out = _fn(args, result.message or result.data.get("text", ""))
                except Exception as e:
                    return V.unverifiable(f"plugin verify() raised {type(e).__name__}")
                ok, detail = (out if isinstance(out, tuple) else (bool(out), ""))
                return V.verified(detail or "plugin verified") if ok else V.failed(detail or "plugin check failed")
        params = dict(rec.parameters or {})
        reg.register(ToolSpec(
            name=rec.name, description=rec.description, handler=handler, input_schema=params,
            agent="plugin", risk=RiskLevel.parse(rec.risk), requires_confirmation=rec.requires_confirmation,
            verify=verify, source="plugin", live_exposed=True, planner_visible=True,
            permissions=list(rec.permissions), capabilities=list(rec.capabilities)), replace=True)


class JarvisCore:
    def __init__(self, *, player=None, speak: Callable[[str], None] | None = None,
                 announce: Callable[[str], None] | None = None,
                 set_hud: Callable[[str], None] | None = None,
                 log: Callable[[str], None] | None = None,
                 plugin_registry=None, registry: ToolRegistry | None = None,
                 confirmer=None, persist: bool = True, use_llm_planner: bool = True):
        self.player = player
        self._speak = speak                  # tool-level speech (actions that talk mid-run)
        self._announce = announce or (lambda msg: None)   # tell the Live model something to relay
        self._set_hud = set_hud or (lambda s: None)
        self._log = log or (lambda m: None)

        self.registry = registry or build_default_registry()
        self.last_gui_task = ""                 # Phase 7: the screen watcher follows this task
        try:
            from core.tools.registry import set_current
            set_current(self.registry)
        except Exception:
            pass
        self.plugins_registered = register_plugin_tools(self.registry, plugin_registry)
        self.permissions = PermissionEngine(self.registry)
        self.agents = default_agents()
        self.router = AgentRouter(self.agents)
        self.planner = TaskPlanner(self.registry, use_llm=use_llm_planner)
        self.store = TaskStore(persist=persist)
        self.audit = AuditLog() if persist else AuditLog(path=_tmp_audit())
        if persist:
            # durable trail of every reversible change (metadata only — the undo closures stay in memory)
            from core import undo as _undo
            _undo.set_listener(lambda rec: self.audit.record("undo_recorded", **rec))
        self.workflows = WorkflowStore(registry=self.registry, persist=persist)
        # Phase 7: day-to-day learning (stated preferences, corrections, habits)
        from core.learning import Learner, get_learner, set_learner
        if persist:
            self.learner = get_learner()
        else:
            self.learner = Learner(path=_tmp_audit().with_suffix(".learning.json"))
            set_learner(self.learner)
        self.notifications = NotificationPolicy()
        self.proactive = ProactiveRegistry(self.notifications)
        self.kill = kill_switch()
        self.kill.audit = self.audit
        self.context = None                  # set below, once memory exists
        self.orchestrator = Orchestrator(
            self.registry, self.permissions, self.agents, self.planner, self.store,
            audit=self.audit, confirmer=confirmer or HUDConfirmer(),
            ask_ai=self._ask_ai, notify=self._on_task_event,
            context_factory=lambda task, step: ToolContext(player=self.player, speak=self._speak,
                                                           task_id=task.id, step_id=step.id),
            on_step_done=lambda task, step, res: self.workflows.observe(step.tool, step.args, True),
            kill=self.kill, context_provider=self._context_text)
        self.devices = DeviceRouter()
        self.devices.register(LaptopDevice(self.orchestrator))
        from memory.memory_hub import MemoryHub
        self.memory = MemoryHub(task_store=self.store, workflows=self.workflows) if persist else \
            MemoryHub(base_dir=_tmp_audit().parent / f"jarvis_mem_{int(time.time() * 1000)}",
                      task_store=self.store, workflows=self.workflows)
        try:
            self.memory.device.refresh()
        except Exception:
            pass
        self._confirmer = confirmer or HUDConfirmer()
        # Phase 7: the Telegram pocket line — the bridge always exists; the line
        # itself starts only in start_pocket() and only with TELEGRAM_BOT_TOKEN set.
        from core import telegram_bot as _tg
        self.pocket_bridge = _tg.PocketBridge(self)
        _tg.set_bridge(self.pocket_bridge)

        # ── Phase 5: context engine, cognitive core, safety, plugin market ──
        from core.cognition import CognitiveCore
        from core.context_engine import ContextEngine
        from core.multilingual import response_language
        # Part One: one knowledge index + a per-turn source record, shared by the
        # context engine, the recall tools and the UI's SOURCES panel.
        from core.knowledge import KnowledgeBase, SourceTracker
        self.sources = SourceTracker()
        self.knowledge = KnowledgeBase(memory_hub=self.memory, task_store=self.store, tracker=self.sources,
                                       folders=None if persist else (lambda: []))
        self.on_sources: Callable[..., None] | None = None      # UI: show the SOURCES panel
        self.on_memory: Callable[..., None] | None = None       # UI: log a verified save
        self.on_live_model: Callable[[str | None], str] | None = None   # main: switch the voice model
        self.context = ContextEngine(memory_hub=self.memory, episodic=self.memory.episodic,
                                     identity=self.memory.identity, task_store=self.store,
                                     workflows=self.workflows, audit=self.audit, language=response_language,
                                     knowledge=self.knowledge)
        self.cognition = CognitiveCore(context=self.context, memory=self.memory, planner=self.planner,
                                       orchestrator=self.orchestrator, audit=self.audit)
        self.kill.register("confirmations", self._stop_confirmations)
        self.on_plugins_changed: Callable[[str], None] | None = None
        from core import plugin_market
        self.market = plugin_market.PluginMarket(self.registry, plugin_registry, register_plugin_record,
                                                 on_installed=self._plugin_installed)
        plugin_market.bind(self.market)

        # ── Phase 6: self-model + proactive unfinished-work offers ──────────
        from core.self_model import SelfModel, unfinished_work_source
        self.self_model = SelfModel(self)
        try:
            self.proactive.register(unfinished_work_source(self.store))
        except Exception:
            pass
        # PhoneDevice is registered but reports itself unavailable (no companion app yet)
        try:
            from core.devices.base import PhoneDevice
            self.devices.register(PhoneDevice("phone-unpaired", "Phone (not set up)"))
        except Exception:
            pass

    # ═════════════════════════════════════════════════════════════════════
    # AI helper for agents
    # ═════════════════════════════════════════════════════════════════════
    @staticmethod
    def _ask_ai(prompt: str, **kw) -> str:
        from core.ai_gateway import get_gateway
        return get_gateway().text(prompt, **kw)

    def _context_text(self, request: str) -> str:
        if self.context is None:
            return ""
        try:
            text = self.context.build(request, budget=1200).text
        except Exception:
            text = ""
        try:                                   # Phase 7: how the user likes things done
            learned = self.learner.prompt_block(600)
            if learned:
                text = (text + "\n" + learned).strip()
        except Exception:
            pass
        return text

    # ── Phase 7: learning from the user ─────────────────────────────────────
    def learn_turn(self, text: str, tools=()) -> None:
        """A finished voice/text turn: the user's own words may state a preference."""
        try:
            rec = self.learner.observe_utterance(text, tools)
            if rec is not None and rec.get("count", 1) == 1:
                self.audit.record("learned", kind=rec.get("kind"), detail=str(rec.get("text"))[:100])
        except Exception:
            pass

    def learning(self, args: dict) -> str:
        a = {k: v for k, v in dict(args or {}).items() if v not in (None, "")}
        gate = self.gate_direct_call("learning", a)
        if gate is not None:
            return gate
        res = self.registry.invoke("learning", a, ToolContext(player=self.player))
        return self.after_direct_call("learning", a, res)

    @staticmethod
    def _stop_confirmations(reason: str) -> str:
        from core import confirm
        return "HUD prompt withdrawn" if confirm.cancel_pending(None, reason="emergency stop") else "no HUD prompt"

    def _plugin_installed(self, name: str) -> None:
        self.audit.record("plugin_installed", tool=name)
        if self.on_plugins_changed:
            try:
                self.on_plugins_changed(name)
            except Exception:
                pass

    # ═════════════════════════════════════════════════════════════════════
    # Phase 5 — cognitive tools called by the voice model
    # ═════════════════════════════════════════════════════════════════════
    def think(self, args: dict) -> str:
        q = str(args.get("question") or args.get("query") or "").strip()
        if not q:
            return "What should I think about?"
        try:
            t = self.cognition.think(q, depth=str(args.get("depth") or "auto"))
            if t.context_used and getattr(t, "bundle", None) is not None:
                self._record_bundle(t.bundle, via="think")
        except Exception as e:
            from core.secret_store import redact
            return f"I couldn't reason about that right now ({redact(e)[:120]})."
        if not t.text:
            return "I came up empty on that."
        lane = {"local": "computed exactly on this PC", "fast": "quick model", "normal": "standard model",
                "deep": "deep reasoning", "vision": "vision model"}.get(t.tier, t.tier)
        self.audit.record("think", tier=t.tier, detail=q[:100], revised=t.revised)
        return (f"{t.text}\n[THINK: {lane}{'; self-checked and revised' if t.revised else ''}"
                f"{'; used your context' if t.context_used else ''}. Speak a concise version; offer details.]")

    def recall_context(self, args: dict) -> str:
        q = str(args.get("query") or args.get("request") or "").strip()
        b = self.context.build(q, budget=int(args.get("budget") or 1400),
                               include_window=bool(args.get("include_screen")) or None)
        self._record_bundle(b, via="recall_context")
        if not b.text:
            return "I have no relevant context for that."
        from core.knowledge import ROUTE_HINT, classify_personal_query
        kind = classify_personal_query(q)
        hint = f"\n({ROUTE_HINT[kind]})" if kind == "current_screen" else ""
        return b.text + ("\n(Context is background — confirm with the user before acting on a guess.)"
                         if b.project else "") + hint

    # ═════════════════════════════════════════════════════════════════════
    # Part One — knowledge, sources, memory growth, model switching
    # ═════════════════════════════════════════════════════════════════════
    def _record_bundle(self, b, via: str = "") -> int:
        """Context items that point at real stored data become tracked sources."""
        from core.knowledge import Source, clean_excerpt
        out = []
        for it in getattr(b, "items", None) or []:
            ref = getattr(it, "ref", None)
            if not ref or not ref.get("id"):
                continue
            ex, fl = clean_excerpt(ref.get("excerpt") or it.text)
            layer = {"memory": "memory", "identity": "memory", "episode": "memory", "document": "personal",
                     "conversation": "conversation", "task": "task"}.get(ref.get("type", ""), "memory")
            out.append(Source(ref["id"], ref.get("type", "memory"), layer, ref.get("title") or it.source, ex,
                              path=ref.get("path", ""), timestamp=ref.get("timestamp", ""),
                              score=float(ref.get("score") or min(0.99, it.score / 5.0)),
                              reason=ref.get("reason") or f"relevant {it.source}", flagged=fl))
        if out:
            self.sources.record(out, via=via)
        return len(out)

    _RECENT_RX = None

    def recall_memory(self, args: dict) -> str:
        import re as _re
        q = str(args.get("query") or "").strip()
        recent = args.get("recent")
        if JarvisCore._RECENT_RX is None:
            JarvisCore._RECENT_RX = _re.compile(r"(?i)\b(just|last|latest|recently|a (?:minute|moment) ago)\b.*"
                                                r"\b(remember|note|save|told|asked)|what did i (?:just )?ask you to "
                                                r"remember")
        if recent or JarvisCore._RECENT_RX.search(q):
            try:
                n = max(1, min(10, int(recent))) if str(recent).isdigit() else 3
            except Exception:
                n = 3
            srcs = self.knowledge.recent_memory(n)
            return self.knowledge.format_for_model(srcs, empty="Nothing has been saved yet.") + \
                "\n(Newest first — the first line is what the user asked you to remember most recently.)"
        if not q:
            srcs = self.knowledge.recent_memory(12)
            if not srcs:
                return "I have not stored anything about this person yet."
            return "Everything currently stored (newest first, up to 12):\n" + \
                self.knowledge.format_for_model(srcs)
        srcs = self.knowledge.search(q, layers=("memory",), limit=8, via="recall_memory")
        return self.knowledge.format_for_model(srcs, query=q)

    def search_knowledge(self, args: dict) -> str:
        from core.knowledge import ROUTE_HINT, classify_personal_query
        q = str(args.get("query") or "").strip()
        if not q:
            return "What should I look for?"
        scope = str(args.get("scope") or "all").strip().lower()
        layers = {"notes": ("personal",), "documents": ("personal",), "memory": ("memory",),
                  "history": ("memory", "task", "conversation", "tool", "screen")}.get(scope)
        kind = classify_personal_query(q)
        if kind == "current_screen":
            return ROUTE_HINT[kind]
        srcs = self.knowledge.search(q, layers=layers, limit=int(args.get("limit") or 6), via="search_knowledge")
        out = self.knowledge.format_for_model(srcs, query=q)
        if not srcs:
            st = self.knowledge.stats
            out += (f"\n(Searched {st.get('memory_docs', 0)} memory items and {st.get('document_chunks', 0)} note "
                    f"passages from {st.get('files', 0)} files.)")
        return out

    def show_sources(self, args: dict | None = None) -> str:
        from core.knowledge import cards
        ts = self.sources.last()
        if ts is None or not (ts.used or ts.retrieved):
            return ("[NO_SOURCES] The recent answers did not use any stored source — they came from general "
                    "knowledge or the conversation itself. Say exactly that; do not name any source.")
        if self.on_sources:
            try:
                self.on_sources(ts, True)
            except Exception:
                pass
        srcs = ts.used or ts.retrieved
        spoken = "; ".join(f"{s.title} ({s.type}{', ' + s.when() if s.timestamp else ''})" for s in srcs[:4])
        return (f"[SOURCES shown on screen] For '{ts.question[:80]}': {spoken}."
                f"{' (retrieved but not used: ' + str(len(ts.retrieved) - len(ts.used)) + ')' if ts.used and len(ts.retrieved) > len(ts.used) else ''}"
                f"\nName these briefly. Never add a source that is not listed.\n" + cards(ts, include_unused=True))

    def finish_turn(self, question: str, answer: str):
        return self.sources.finish_turn(question, answer)

    _OBSERVED_TOOLS = {"web_search": "tool", "weather_report": "tool", "system_status": "tool",
                       "analyze_screen": "screen", "file_processor": "tool", "flight_finder": "tool"}

    def observe_tool(self, name: str, args: dict, result: Any) -> None:
        """Informational tool results become citable (tracked) sources for this turn.
        External text (web pages, files) is cleaned and flagged — data, not instructions."""
        kind = self._OBSERVED_TOOLS.get(name)
        if not kind:
            return
        text = str(result or "")
        if not text or text.startswith(("I did not run", "Tool '", "Unknown tool")):
            return
        from datetime import datetime
        from core.knowledge import Source, clean_excerpt
        ex, fl = clean_excerpt(text, 300)
        now = datetime.now()
        q = str(args.get("query") or args.get("question") or args.get("mode") or args.get("city") or "")[:60]
        src = Source(f"{kind}:{name}@{now.strftime('%H%M%S')}", kind, kind,
                     f"{name.replace('_', ' ')}" + (f": {q}" if q else ""), ex,
                     timestamp=now.isoformat(timespec="seconds"), score=0.8,
                     reason="result of a tool JARVIS ran this turn", flagged=fl)
        self.sources.observe(src)
        self.sources.record([src], via=name)

    def remember(self, args: dict, source: str = "voice") -> str:
        """The "remember that…" workflow: extract → policy → verified write → index → receipt."""
        from memory.capture import capture
        text = str(args.get("text") or args.get("what") or args.get("value") or "").strip()
        if not text:
            return "[MEMORY_NOT_SAVED] There was nothing to remember. Ask what to remember."
        r = capture(text, source=source, category=str(args.get("category") or ""),
                    key=str(args.get("key") or ""),
                    value=str(args.get("value") or "") if args.get("text") else "",
                    memory_hub=self.memory)
        self.audit.record("memory_capture", tool="remember", result="ok" if r.ok else "failed",
                          detail=f"{r.action} {r.id}"[:120])
        if r.ok:
            self.knowledge.invalidate()
        if self.on_memory:
            try:
                self.on_memory(r)
            except Exception:
                pass
        return r.confirmation()

    def switch_model(self, args: dict) -> str:
        """Exact runtime model switch, or a refusal with real alternatives."""
        from core.ai_gateway import get_gateway, FREE_PROVIDERS
        from core.model_registry import BRAIN_TIERS, ModelRegistry
        gw = get_gateway()
        target = str(args.get("model") or "").strip()
        scope = str(args.get("scope") or "brain").strip().lower()
        action = str(args.get("action") or "").strip().lower()
        if scope in ("live", "speech"):
            scope = "voice"
        if action == "status" or (not target and action != "reset"):
            b = gw.active_brain()
            voice = ""
            if self.on_live_model is not None:
                try:
                    voice = self.on_live_model("?status")
                except Exception:
                    voice = ""
            return (f"Brain: {b['model'] or 'unknown'} on {b['provider']}"
                    f"{' (switched at runtime; resets on restart)' if b['runtime'] else ' (default from config)'}."
                    + (f" Voice model: {voice}." if voice else ""))
        if action == "reset" or target.lower() in ("default", "reset", "the default", "back", "normal"):
            if scope == "voice":
                return self.on_live_model(None) if self.on_live_model else "The voice model is the default."
            was = gw.runtime_model()
            gw.clear_runtime_model()
            self.audit.record("model_switch", tool="switch_model", result="reset",
                              detail=(was or {}).get("id", "") or "")
            b = gw.active_brain()
            return f"[MODEL_RESET] Back to the configured default: {b['model']} on {b['provider']}."
        reg = ModelRegistry()
        res = reg.resolve(target, kind="live" if scope == "voice" else "text")
        if not res.ok:
            cur = gw.active_brain()
            self.audit.record("model_switch", tool="switch_model", result="refused", detail=target[:80])
            return (f"[MODEL_NOT_SWITCHED] {res.message()} Nothing changed — still using "
                    f"{cur['model'] or 'the configured default'} on {cur['provider']}. Tell the user exactly this; "
                    f"never pretend to switch.")
        e = res.entry
        if scope == "voice":
            if self.on_live_model is None:
                return "[MODEL_NOT_SWITCHED] Voice-model switching is not available in this mode."
            return self.on_live_model(e.id)
        from core.secret_store import has_secret
        key_name = {"google": "GOOGLE_API_KEY", "nvidia": "NVIDIA_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                    "openai": "OPENAI_API_KEY"}.get(e.provider)
        if key_name and not has_secret(key_name):
            return (f"[MODEL_NOT_SWITCHED] {e.id} runs on {e.provider}, and no {key_name} is configured, so it "
                    f"cannot answer. Nothing changed.")
        gw.set_runtime_model(e.id, e.provider, tiers=BRAIN_TIERS)
        self.audit.record("model_switch", tool="switch_model", result="ok", detail=f"{e.provider}:{e.id}")
        warn = ""
        try:
            h = gw.health.get(e.id) or {}
            if h and not gw.health.usable(e.id):
                warn = (f" Note: {e.id} was recently marked {h.get('status', 'unavailable')} on this key — requests "
                        f"may fail; if they do I will say so rather than use another model.")
        except Exception:
            pass
        paid = "" if e.provider in FREE_PROVIDERS else f" ({e.provider} is a paid provider — your key, your cost.)"
        return (f"[MODEL_SWITCHED] Reasoning (normal, deep, research) now uses exactly {e.id} on {e.provider}. "
                f"Fast checks, vision and the voice model are unchanged. This lasts until JARVIS restarts or the "
                f"user says 'reset the model'. If it fails I report it — I never fall back to a different model."
                f"{paid}{warn}")

    def save_fact(self, category: str, key: str, value: str, explicit: bool = False) -> str:
        return self.cognition.remember(category, key, value, explicit=explicit)

    def emergency_stop(self, reason: str = "user") -> str:
        return self.kill.engage(reason)

    def capabilities(self, args: dict) -> str:
        need = str(args.get("need") or "").strip()
        if need:
            return self.registry.invoke("plugin.find", {"need": need}, ToolContext()).message
        from core.ai_gateway import get_gateway
        try:
            tiers = get_gateway().info().get("tiers", {})
        except Exception:
            tiers = {}
        n_tools = len(self.registry.list())
        return (f"{n_tools} tools across {len(self.agents.all()) if hasattr(self.agents, 'all') else 15} agents. "
                f"Model lanes: exact local maths, fast={tiers.get('fast', '?')}, normal={tiers.get('normal', '?')}, "
                f"deep={tiers.get('deep', '?')} with a high thinking budget, vision={tiers.get('vision', '?')}. "
                f"Ask with a specific need to check a capability or find a plugin.")

    def about_self(self, args: dict) -> str:
        """Answer 'who are you?' / 'what can you do?' / 'can you X?' from the live
        tool registry and real model health — never a hand-kept claim list."""
        return self.self_model.answer(str(args.get("question") or args.get("query") or ""))

    def task_view(self, task_id: str | None = None) -> dict:
        """Read-only task snapshot for the phone dashboard (no control here)."""
        if task_id:
            t = self.store.get(task_id)
            if t is None:
                return {}
            d = t.to_dict()
            d["describe"] = self.store.describe(t)
            return d
        active = self.store.active()
        recent = self.store.all()[-8:]
        return {
            "active": [{"id": t.id, "request": t.request[:80], "state": t.state.value, "phase": t.phase,
                        "progress": t.progress, "priority": t.priority} for t in active],
            "recent": [{"id": t.id, "request": t.request[:80], "state": t.state.value,
                        "progress": t.progress} for t in reversed(recent)],
            "summary": self.store.describe(),
        }

    def device_manifest(self) -> dict:
        """This machine's capabilities + a sync-safe export (no secrets) that a
        future companion device can read to know what this one can do."""
        import platform
        caps = {}
        try:
            caps = {d["id"]: d for d in self.devices.devices()}
        except Exception:
            pass
        tools = sorted({t.agent for t in self.registry.list() if getattr(t, "planner_visible", True)})
        return {
            "device": {"name": platform.node(), "os": f"{platform.system()} {platform.release()}",
                       "kind": "laptop"},
            "capabilities": tools,
            "devices": caps,
            "projects": (lambda m: [{"name": k, "stack": v.get("stack", [])[:5]}
                                    for k, v in list(m.projects().items())[:20]])(self.memory.identity)
            if hasattr(self.memory, "identity") else [],
            "limits": self.self_model.limits(),
        }

    def install_plugin(self, args: dict, on_confirmed_result=None) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            return "Which plugin should I install?"
        gate = self.gate_direct_call("plugin.install", {"name": name}, on_confirmed_result=on_confirmed_result)
        if gate is None:          # trusted by the user's settings
            return self.registry.invoke("plugin.install", {"name": name}, ToolContext()).message
        return gate

    def analyze_screen(self, args: dict) -> str:
        a = {k: v for k, v in {"mode": args.get("mode"), "question": args.get("question"),
                               "target": args.get("target"), "path": args.get("path")}.items() if v}
        gate = self.gate_direct_call("screen.analyze", a)
        if gate is not None:
            return gate
        res = self.registry.invoke("screen.analyze", a, ToolContext(player=self.player))
        return self.after_direct_call("screen.analyze", a, res)

    def computer_use(self, args: dict) -> str:
        goal = str(args.get("goal") or "").strip()
        if not goal:
            return "What should I do on the screen?"
        from core.tasks.state import Step
        task = self.orchestrator.submit_steps(goal, [Step("s1", f"Use the screen: {goal[:80]}", "computer.run",
                                                          {"goal": goal}, agent="laptop")], origin="voice")
        self.audit.record("task_submitted", task_id=task.id, agents=["laptop"], detail=goal[:120])
        self.last_gui_task = task.id
        return (f"[TASK_STARTED] Task {task.id}: computer use. The user must first press CONFIRM on the HUD to "
                f"let you control the mouse and keyboard; important clicks ask again. Tell them that in one "
                f"sentence. F12 or 'stop everything' halts it instantly.")

    # ── Phase 7: drawing with the mouse, references ─────────────────────────
    def draw(self, args: dict) -> str:
        """Draw in Paint with the mouse — a background task (asks first; F12 stops it)."""
        subject = str(args.get("subject") or args.get("what") or "").strip()
        from core.tasks.state import Step
        label = subject or "something JARVIS likes"
        task = self.orchestrator.submit_steps(f"Draw {label} in Paint", [Step(
            "s1", f"Draw {label[:60]} in Paint", "draw.paint", {"subject": subject}, agent="laptop")],
            origin="voice")
        self.audit.record("task_submitted", task_id=task.id, agents=["laptop"], detail=f"draw {label[:80]}")
        self.last_gui_task = task.id
        return (f"[TASK_STARTED] Task {task.id}: drawing {label} in Paint with the mouse. The user must allow it "
                f"on the HUD first (ALLOW ONCE / ALWAYS / DENY). Say that in one short sentence; you will see the "
                f"screen while it draws. F12 or 'stop everything' lifts the pen at once.")

    def task_active(self, task_id: str) -> bool:
        t = self.store.get(task_id) if task_id else None
        return bool(t is not None and not t.state.terminal)

    def reference(self, args: dict) -> str:
        a = {k: v for k, v in dict(args or {}).items() if v not in (None, "")}
        gate = self.gate_direct_call("reference", a)
        if gate is not None:
            return gate
        res = self.registry.invoke("reference", a, ToolContext(player=self.player))
        return self.after_direct_call("reference", a, res)

    # ── Phase 7: the pocket line (Telegram) ─────────────────────────────────
    def pocket(self, args: dict) -> str:
        a = {k: v for k, v in dict(args or {}).items() if v not in (None, "")}
        gate = self.gate_direct_call("pocket", a)
        if gate is not None:
            return gate
        res = self.registry.invoke("pocket", a, ToolContext(player=self.player))
        return self.after_direct_call("pocket", a, res)

    def start_pocket(self, on_code: Callable[[str], None] | None = None, state_path=None, http=None) -> str:
        """Start the Telegram line when TELEGRAM_BOT_TOKEN is configured.
        → a HUD sentence, or '' while it stays dormant (no token)."""
        from core import telegram_bot as tg
        from core.secret_store import get_secret
        b = self.pocket_bridge
        if b.pocket is None:
            def _log(m: str) -> None:
                print(m)
                self._log(m)

            def _code(code: str) -> None:           # on the HUD only — never spoken, never to the model
                self._log(f"📱 Telegram pairing code: {code} — send it to your JARVIS bot within 10 minutes "
                          f"(works once).")
            b.pocket = tg.TelegramPocket(
                token_fn=lambda: get_secret("TELEGRAM_BOT_TOKEN"), handle_text=b.handle_text,
                transcribe=tg.default_transcriber(), speak=tg.default_speaker(), brief=b.desk_status,
                on_code=on_code or _code, state_path=state_path, http=http, log=_log)
        if not b.pocket.configured or not b.pocket.start():
            return ""
        return "📱 Telegram line on" + ("." if b.pocket.paired else " — not paired yet: say 'pocket code'.")

    def pocket_urgent(self, text: str) -> bool:
        """An urgent desk event (system alert): reaches the phone only when the line is armed."""
        try:
            return bool(self.pocket_bridge.urgent_alert(text))
        except Exception:
            return False

    def pocket_standing_order(self, text: str) -> str:
        """Backstop for what the user said at the desk ('only message me if it's urgent')."""
        try:
            line = self.pocket_bridge.standing_order(text)
        except Exception:
            return ""
        if line:
            self._log(line)
            self.audit.record("pocket", detail=line[:80])
        return line

    # ═════════════════════════════════════════════════════════════════════
    # Direct (single) tool calls from the Live model
    # ═════════════════════════════════════════════════════════════════════
    def gate_direct_call(self, name: str, args: dict,
                         on_confirmed_result: Callable[[str], None] | None = None) -> Optional[str]:
        """Central permission check for a tool the Live model called directly.

        Returns None → proceed with main.py's normal dispatch.
        Returns a string → do NOT dispatch; hand this string back to the model
        (a refusal, or a [CONFIRMATION_PENDING] notice — the action then runs
        only when the human presses CONFIRM on the HUD)."""
        spec = self.registry.get(name)
        if spec is None:
            # Unknown to the registry → main.py's own "Unknown tool" path answers.
            return None
        # Phase 6: a direct command that drives the mouse/keyboard must not fight a
        # background task doing the same. The voice lease is released in
        # after_direct_call (and expires by itself after 90 s regardless).
        needs_gui = "gui" in self.registry.resources_for(name)
        if needs_gui:
            from core.tasks.scheduler import resource_locks
            holder = resource_locks().holder("gui")
            if holder and holder != "voice":
                who = holder.replace("task:", "task ")
                return (f"I did not run {name}: the mouse and keyboard are being used by {who} right now. "
                        f"Tell the user; they can wait for it to finish or say 'stop' to cancel it.")
        d = self.permissions.decide(name, args, {"origin": "voice"})
        action = str(args.get("action", "") or "").strip().lower()
        if d.decision in (Decision.ALLOW,) or (d.decision == Decision.CONFIRM and (
                spec.self_gated is True or (isinstance(spec.self_gated, (set, frozenset, list, tuple))
                                            and action in spec.self_gated))):
            if needs_gui:
                from core.tasks.scheduler import resource_locks
                resource_locks().acquire("gui", "voice", timeout=0, lease=90.0)
        if d.decision == Decision.DENY:
            self.audit.record("permission", tool=name, action=action, decision="DENY", risk=d.risk.name,
                              result="denied", detail=d.reason)
            return f"I did not run {name}: {d.reason}"
        if d.decision == Decision.ALLOW:
            return None
        # CONFIRM
        sg = spec.self_gated
        if sg is True or (isinstance(sg, (set, frozenset, list, tuple)) and action in sg):
            return None          # the action shows its own HUD confirmation (pre-existing gate)
        title = spec.confirm_text(args) if spec.confirm_text else f"{name} {action}".strip()
        self.audit.record("permission", tool=name, action=action, decision="CONFIRM", risk=d.risk.name,
                          result="requested")
        from core import confirm

        def run_confirmed() -> str:
            self.audit.record("permission", tool=name, action=action, decision="CONFIRM", risk=d.risk.name,
                              result="approved")
            if d.grant:
                self.permissions.record_grant(name, d.grant)
            if needs_gui:
                from core.tasks.scheduler import resource_locks
                if not resource_locks().acquire("gui", "voice", timeout=30.0, lease=120.0):
                    text = "The mouse and keyboard stayed busy with another task, so I did not do it."
                    if on_confirmed_result:
                        on_confirmed_result(f"[CONFIRMED_ACTION_RESULT] {title}: {text}")
                    return text
            try:
                res = self.registry.invoke(name, args, ToolContext(player=self.player, speak=self._speak))
            finally:
                if needs_gui:
                    from core.tasks.scheduler import resource_locks
                    resource_locks().release("gui", "voice")
            v = self.registry.verify(name, args, res)
            text = res.message or res.error or "Done."
            text = self._with_verification(text, res, v)
            self._audit_direct(name, args, res, v)
            self.workflows.observe(name, args, res.ok)
            if on_confirmed_result:
                on_confirmed_result(f"[CONFIRMED_ACTION_RESULT] {title}: {text}")
            return text

        def cancelled(reason: str) -> None:
            self.audit.record("permission", tool=name, action=action, decision="CONFIRM", risk=d.risk.name,
                              result=reason)
            if on_confirmed_result and reason in ("declined", "expired"):
                on_confirmed_result(f"[CONFIRMATION_{reason.upper()}] {title} was not done "
                                    f"({'you declined' if reason == 'declined' else 'no confirmation was given'}).")

        if confirm.pending_title():
            return ("There is already a confirmation waiting on screen. Ask the user to answer that one "
                    "first; nothing else was done.")
        from core.permissions import RiskLevel as _RL
        trust = (f"{name}.{action}" if action else name) if d.risk < _RL.CRITICAL and not d.grant else ""
        return confirm.request(key=f"direct-{name}", title=title,
                               detail=f"{d.risk.name.replace('_', ' ').title()} action · {d.reason}",
                               run=run_confirmed, on_cancel=cancelled, trust_key=trust)

    def after_direct_call(self, name: str, args: dict, result: Any) -> str:
        """Audit + verify + observe a direct call that already ran. Returns the
        (possibly annotated) result text for the model."""
        if isinstance(result, ToolResult):     # a registry tool: its sentence, never the dataclass repr
            text = result.message or result.error or ("Done." if result.ok else "That did not work.")
        else:
            text = str(result)
        spec = self.registry.get(name)
        try:                                    # Phase 7: habits (what / when), never content
            from core.verification import legacy_result_failed
            self.learner.observe_action(name, args, not legacy_result_failed(text))
        except Exception:
            pass
        try:
            from core.tasks.scheduler import resource_locks
            if resource_locks().holder("gui") == "voice":
                resource_locks().release("gui", "voice")
        except Exception:
            pass
        if spec is None:
            self.audit.record("tool_call", tool=name, args=args, result="unregistered")
            return text
        res = normalise_result(result)
        v = self.registry.verify(name, args, res) if spec.verify else None
        self._audit_direct(name, args, res, v)
        self.workflows.observe(name, args, res.ok and not (v and v.failed))
        try:
            self.cognition.observe_direct(name, args, res.ok and not (v and v.failed))
        except Exception:
            pass
        return self._with_verification(text, res, v)

    @staticmethod
    def _with_verification(text: str, res: ToolResult, v) -> str:
        if v is None:
            return text
        if v.failed and res.ok:
            return (f"{text}\n[VERIFICATION FAILED] {v.detail} Tell the user plainly that this did NOT "
                    f"take effect — do not say it is done.")
        if v.status == V.VStatus.UNVERIFIABLE and res.ok:
            return f"{text}\n[UNVERIFIED] {v.detail} Say it was attempted, not that it is confirmed."
        return text

    def _audit_direct(self, name, args, res: ToolResult, v) -> None:
        self.audit.record("tool_call", agent=(self.registry.get(name).agent if self.registry.get(name) else ""),
                          tool=name, action=str(args.get("action", "") or ""), args=args,
                          result="ok" if res.ok else "failed",
                          verification=(v.status.value if v is not None else None),
                          detail=(v.detail if v is not None else res.error))

    # ═════════════════════════════════════════════════════════════════════
    # Multi-step tasks
    # ═════════════════════════════════════════════════════════════════════
    def run_task(self, request: str, priority: str | None = None) -> str:
        request = str(request or "").strip()
        if not request:
            return "Tell me what the task is."
        wf = self.workflows.find(request)
        if wf and any(w in request.lower() for w in ("workflow", "routine")):
            return self.workflow({"action": "run", "name": wf["name"]})
        route = self.router.route(request)
        task = self.orchestrator.submit(request, origin="voice", language=route.language,
                                        priority=(str(priority).upper() if priority else None))
        self.audit.record("task_submitted", task_id=task.id, agents=route.agents, detail=request[:120])
        agents = ", ".join(self.agents.get(a).title for a in route.agents if self.agents.get(a))
        return (f"[TASK_STARTED] Task {task.id} accepted ({agents}). Tell the user in ONE short sentence, "
                f"in their language, that you're on it. Progress and the result will arrive as "
                f"[TASK_UPDATE] messages — never claim it is finished before the [TASK_UPDATE] says so.")

    def task_control(self, args: dict) -> str:
        action = str(args.get("action", "status") or "status").strip().lower()
        tid = str(args.get("task_id", "") or "").strip() or None
        if action in ("status", "what_are_you_doing", "current"):
            return self.orchestrator.status(tid)
        if action == "cancel":
            return self.orchestrator.cancel(tid)
        if action == "pause":
            return self.orchestrator.pause(tid)
        if action == "resume":
            return self.orchestrator.resume(tid)
        if action == "answer":
            return self.orchestrator.answer(str(args.get("answer", "")), tid)
        if action == "retry":
            r = self.orchestrator.retry(tid)
            if isinstance(r, str):
                return r
            return (f"[TASK_STARTED] Retrying as task {r.id} — completed steps are reused, the rest runs again. "
                    f"Results arrive as [TASK_UPDATE] messages.")
        if action in ("history", "what_did_you_do", "log"):
            return self.audit.summarize_recent(int(args.get("count", 10) or 10))
        if action == "list":
            items = self.store.all()[-8:]
            if not items:
                return "No tasks yet."
            return "Recent tasks: " + "; ".join(f"{t.id} {t.phase.lower()} {t.progress}% "
                                                f"[{t.priority.lower()}] — {t.request[:50]}"
                                                for t in reversed(items))
        return "Unknown task_control action. Use status, cancel, pause, resume, answer, retry, history or list."

    def _on_task_event(self, task: Task, kind: str, message: str) -> None:
        if kind == "state":
            try:
                st = TaskState(message)
                hud = HUD_STATE.get(st)
                if hud:
                    self._set_hud(hud)
            except Exception:
                pass
            return
        if kind in ("log", "step", "retry", "progress"):
            self._log(f"TASK {task.id}: {message}")
            return
        self._log(f"TASK {task.id} [{kind}]: {message[:200]}")
        if kind in ("completed", "failed", "cancelled"):
            # VERIFY (task-level self-check) → LEARN
            try:
                chk = self.cognition.self_check(task)
                message = f"{message}\n{chk.line()}"
                self.audit.record("self_check", task_id=task.id, verdict=chk.verdict, gaps=chk.gaps[:3])
            except Exception:
                pass
            try:
                self.cognition.learn(task)
            except Exception:
                pass
        try:                                   # Phase 7: phone tasks report back; the urgent line
            self.pocket_bridge.on_task_event(task, kind, message)
        except Exception:
            pass
        relay = {
            "planned": "Briefly tell the user the plan (one or two sentences).",
            "question": "Ask the user this question now, in their language, and when they answer call "
                        "task_control with action='answer' and their answer.",
            "permission": "Ask the user this out loud in their language. They must press CONFIRM or CANCEL "
                          "on the HUD — a spoken yes is not enough, and you must not claim it was sent.",
            "completed": "Report the result to the user briefly, in their language. Say it is done ONLY if "
                         "the SELF-CHECK says verified; otherwise say what could not be confirmed.",
            "recovery": "Tell the user in one short sentence that the first approach failed and you are "
                        "trying another way (or what the new approach is).",
            "failed": "Tell the user plainly what failed and what was already done. Do not say it succeeded.",
            "cancelled": "Tell the user briefly that the task stopped, and what (if anything) was done.",
            "paused": "Tell the user the task is paused.",
            "queued": "Tell the user in one short sentence that this task is queued behind other work and "
                      "will start by itself.",
            "blocked": "Tell the user in one short sentence that this task is waiting for the mouse and "
                       "keyboard because another job is using them, and will continue by itself.",
        }.get(kind)
        if relay:
            self._announce(f"[TASK_UPDATE] {message}\n{relay}")

    # ═════════════════════════════════════════════════════════════════════
    # Workflows
    # ═════════════════════════════════════════════════════════════════════
    def workflow(self, args: dict) -> str:
        action = str(args.get("action", "list") or "list").lower()
        if action == "list":
            wfs = self.workflows.list()
            if not wfs:
                return "No saved workflows yet."
            return "Saved workflows: " + "; ".join(
                f"{w['name']} ({len(w['steps'])} steps, used {w.get('use_count', 0)}×)" for w in wfs)
        if action == "suggestions":
            s = self.workflows.suggestions()
            if not s:
                return "I haven't noticed any repeated routines yet."
            return " ".join(f"[{x['id']}] {x['question']}" for x in s)
        if action == "save":
            try:
                wf = self.workflows.save(str(args.get("name", "")), suggestion_id=args.get("suggestion_id") or None,
                                         triggers=[t for t in [args.get("trigger")] if t])
            except ValueError as e:
                return str(e)
            perms = (" It includes steps that will still ask for confirmation: " +
                     ", ".join(wf["required_permissions"]) + ".") if wf["required_permissions"] else ""
            return f"Saved workflow '{wf['name']}' with {len(wf['steps'])} steps.{perms}"
        if action == "dismiss":
            return self.workflows.dismiss(str(args.get("suggestion_id", "")))
        if action == "delete":
            return "Deleted." if self.workflows.delete(str(args.get("name", ""))) else "No such workflow."
        if action == "run":
            wf = self.workflows.find(str(args.get("name", "")))
            if not wf:
                return "I couldn't find a saved workflow with that name."
            self.workflows.mark_used(wf["id"])
            task = self.orchestrator.submit_steps(f"workflow: {wf['name']}", self.workflows.to_steps(wf),
                                                  origin="workflow")
            return (f"[TASK_STARTED] Running workflow '{wf['name']}' as task {task.id}. Each step is still "
                    f"permission-checked. Results arrive as [TASK_UPDATE] messages.")
        return "Unknown workflow action. Use list, suggestions, save, dismiss, delete or run."

    def maybe_offer_workflow(self) -> Optional[str]:
        """Called by the proactive loop: at most one new suggestion, policy-gated."""
        ok, _ = self.notifications.allow("workflow_suggestion", Priority.LOW)
        if not ok:
            return None
        s = self.workflows.next_offer()
        if not s:
            return None
        self.notifications.record("workflow_suggestion")
        return (f"{s['question']} (If they agree, call the workflow tool with action='save', "
                f"suggestion_id='{s['id']}' and a short name they choose.)")

    # ═════════════════════════════════════════════════════════════════════
    # Memory / language
    # ═════════════════════════════════════════════════════════════════════
    def memory_inspect(self, args: dict) -> str:
        action = str(args.get("action", "summarize") or "summarize").lower()
        if action == "inspect":
            import json
            data = self.memory.inspect(str(args.get("category", "") or "") or None)
            return json.dumps(data, ensure_ascii=False)[:3500]
        return self.memory.summarize()

    @staticmethod
    def set_language(lang: str) -> str:
        from core.multilingual import save_response_language
        try:
            v = save_response_language(lang)
        except ValueError as e:
            return str(e)
        names = {"auto": "matching whatever language you use", "en": "English", "ta": "Tamil",
                 "tanglish": "Tanglish"}
        return f"Response language set to {names[v]}. (Takes effect in the next reply.)"

    def shutdown(self) -> None:
        self.store.save()


def _tmp_audit():
    import tempfile
    from pathlib import Path
    return Path(tempfile.gettempdir()) / f"jarvis_audit_{int(time.time() * 1000)}.jsonl"
