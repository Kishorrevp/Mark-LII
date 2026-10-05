"""
core/agents/orchestrator.py — the Agent Manager: runs planned tasks.

    submit(request)
      └─ QUEUED → PLANNING (TaskPlanner) → RUNNING
           for each step, in dependency order:
             · cancelled? paused?                  → CANCELLED / PAUSED
             · resolve "$sN.field" inputs          (agent-to-agent structured data)
             · "$ask:..." / ambiguous file         → WAITING_FOR_INPUT (asks, waits)
             · PermissionEngine.decide()           → DENY: fail · CONFIRM: WAITING_FOR_PERMISSION
             · owning Agent executes via AgentServices (can only call the approved tool)
             · failure → retry if retry-safe (bounded) → alternative tool → fail
             · VERIFYING: registry verifier checks the real post-condition
           → VERIFYING (task) → COMPLETED
      Any unrecoverable step failure → FAILED, remaining steps SKIPPED.
      A declined / expired permission → CANCELLED, remaining steps stop.

PHASE 5 additions
  · SELF-RECOVERY — when a step still fails after retries and alternatives,
    the planner is asked (once per task) for a different way to finish:
    analyse failure → alternative plan → validate → continue. Failed and
    remaining steps are marked SUPERSEDED; the new steps (r1, r2 …) run
    through exactly the same permission / verification path. Never used
    after a permission DENY, a decline, or a cancel.
  · RETRY — retry(task_id) resumes a failed / interrupted task: steps that
    completed keep their outputs; only the rest runs again.
  · EMERGENCY STOP — the kill switch (core/safety.py) cancels every task;
    every step boundary checks it.
  · MID-TASK HOOKS — tools receive ctx.extras["confirm"](title, detail) →
    bool (HUD confirmation, never auto-approved), ["ask"](question) → str,
    ["progress"](msg) and ["should_stop"]() for long-running work.
  · CONTEXT — the planner gets the Context Engine's minimal context.

It never reports "done" from a return value alone: a step is DONE only when
its verifier says VERIFIED, or it has no verifier (read-only / generative
tools), or it is UNVERIFIABLE — which is carried into the final message as
"I couldn't confirm ...", never as success.

PHASE 6 additions
  · SCHEDULER — tasks carry a priority (CRITICAL … BACKGROUND); at most
    tasks.max_parallel do work at once, the rest stay PENDING (QUEUED) in
    priority order (core/tasks/scheduler.py). A task waiting for the user does
    not hold a slot. Cancelling a queued task finishes it at once.
  · GUI LOCK — a step whose tool drives the real mouse/keyboard
    (ToolSpec.resources ["gui"]) takes the shared lock; a task that has to wait
    for it is BLOCKED with the reason, and resumes when the lock frees.
  · TIMEOUTS — every step runs under a watchdog (ToolSpec.timeout, the
    DEFAULT_TIMEOUTS table, or config/runtime.json). Time spent waiting for a
    HUD confirmation / an answer does not count. On expiry the tool is told to
    stop (should_stop) and the step fails as a timeout — not retried blindly.
  · ERROR CLASSIFICATION — every failure is classified (core/recovery.py):
    network, rate limit, quota, timeout, not found, access denied, missing
    dependency, auth, policy, declined … Classification only removes retries
    that cannot help and never replans around auth/policy/declines; the final
    message states the likely cause. Self-recovery runs in the RECOVERING state.
  · UNDO RECORDS — undo entries pushed during a step carry the task id, step
    id and tool (core/undo.py undo_context).

Threads: each running task has its own daemon thread (started by the
scheduler) and each step runs on a watchdog-supervised worker thread;
everything the UI or the voice loop calls (cancel, pause, resume, answer,
status) is thread-safe and returns immediately.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from core.agents.base import AgentRegistry, AgentServices
from core.agents.planner import Plan, REF_RE, TaskPlanner, validate_plan
from core.permissions import Decision, PermissionEngine, RiskLevel
from core import recovery as _recovery
from core.safety import EmergencyStop, KillSwitch, kill_switch
from core.tasks.scheduler import RESOURCE_LABEL, Priority, ResourceLocks, TaskScheduler, resource_locks
from core.tasks.state import Step, StepStatus, Task, TaskState, TaskStore
from core.undo import undo_context

_IDLE_STATES = (TaskState.WAITING_FOR_PERMISSION, TaskState.WAITING_FOR_INPUT, TaskState.PAUSED,
                TaskState.BLOCKED)
_USER_WAIT = (TaskState.WAITING_FOR_PERMISSION, TaskState.WAITING_FOR_INPUT, TaskState.PAUSED)
from core.tools.registry import ToolContext, ToolRegistry, ToolResult
from core.verification import VStatus


# ── Confirmation seam ────────────────────────────────────────────────────────

class Confirmer(Protocol):
    def request(self, key: str, title: str, detail: str,
                on_approve: Callable[[], None], on_deny: Callable[[str], None]) -> None: ...

    def cancel(self, key: str) -> None: ...


class HUDConfirmer:
    """Uses core/confirm.py — the HUD CONFIRM/CANCEL banner the model cannot
    press for the user."""

    def request(self, key, title, detail, on_approve, on_deny, trust_key: str = ""):
        from core import confirm
        confirm.request(key=key, title=title, detail=detail,
                        run=lambda: (on_approve(), "approved")[1], on_cancel=on_deny, trust_key=trust_key)

    def cancel(self, key):
        from core import confirm
        confirm.cancel_pending(key)


# ── Per-task control block ───────────────────────────────────────────────────

@dataclass
class _Control:
    cancel: threading.Event = field(default_factory=threading.Event)
    resume: threading.Event = field(default_factory=threading.Event)
    paused: bool = False
    decision: threading.Event = field(default_factory=threading.Event)
    approved: Optional[bool] = None
    deny_reason: str = ""
    answer_ready: threading.Event = field(default_factory=threading.Event)
    answer: Optional[str] = None
    confirm_key: str = ""


class TaskCancelled(Exception):
    control_flow = True          # registry.invoke re-raises instead of turning it into a failure


class Orchestrator:
    def __init__(self, registry: ToolRegistry, permissions: PermissionEngine, agents: AgentRegistry,
                 planner: TaskPlanner, store: TaskStore, audit=None, confirmer: Confirmer | None = None,
                 ask_ai: Callable[..., str] | None = None,
                 notify: Callable[[Task, str, str], None] | None = None,
                 context_factory: Callable[[Task, Step], ToolContext] | None = None,
                 max_retries: int = 2, retry_backoff: float = 0.8,
                 permission_timeout: float = 120.0, input_timeout: float = 300.0,
                 sleep: Callable[[float], None] = time.sleep, on_step_done=None,
                 kill: KillSwitch | None = None, recovery: bool = True, max_recoveries: int = 1,
                 context_provider: Callable[[str], str] | None = None,
                 scheduler: TaskScheduler | None = None, locks: ResourceLocks | None = None,
                 stop_grace: float = 10.0):
        self.registry, self.permissions, self.agents = registry, permissions, agents
        self.planner, self.store, self.audit = planner, store, audit
        self.confirmer = confirmer or HUDConfirmer()
        self.ask_ai = ask_ai
        self.notify = notify or (lambda task, kind, msg: None)
        self.context_factory = context_factory or (lambda task, step: ToolContext(task_id=task.id,
                                                                                  step_id=step.id))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = retry_backoff
        self.permission_timeout = permission_timeout
        self.input_timeout = input_timeout
        self._sleep = sleep
        self._controls: dict[str, _Control] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self.on_step_done = on_step_done      # (task, step, result) → e.g. workflow observer
        self.kill = kill or kill_switch()
        self.kill.register("tasks", self.cancel_all)
        self.recovery = recovery
        self.max_recoveries = max(0, int(max_recoveries))
        self.context_provider = context_provider
        self.scheduler = scheduler or TaskScheduler(is_idle=self._is_idle)
        self.locks = locks or resource_locks()
        self.stop_grace = stop_grace

    # ═════════════════════════════════════════════════════════════════════
    # Public API
    # ═════════════════════════════════════════════════════════════════════
    def submit(self, request: str, origin: str = "voice", background: bool = True,
               language: str = "en", priority: Any = None) -> Task:
        self._release_on_user_command(origin)
        prio = Priority.parse(priority) if priority not in (None, "") else Priority.infer(request, origin)
        task = self.store.create(request, origin=origin, language=language, priority=prio.name)
        self._start(task, background)
        return task

    def _release_on_user_command(self, origin: str) -> None:
        # A NEW command from the user lifts an emergency stop; nothing proactive does,
        # and neither does a phone message (Phase 7: only the desk can lift it).
        if origin not in ("proactive", "telegram") and self.kill.engaged():
            self.kill.release(by=f"new {origin} command")

    def cancel_all(self, reason: str = "user") -> str:
        n = 0
        for t in list(self.store.active()):
            if self._controls.get(t.id) is not None:
                self.cancel(t.id)
                n += 1
        return f"{n} task(s) cancelled" if n else "no tasks running"

    def retry(self, task_id: str | None = None, background: bool = True):
        """Resume a FAILED or CANCELLED task: completed steps keep their outputs,
        everything else runs again (with fresh permission checks)."""
        src = self.store.get(task_id) if task_id else next(
            (t for t in reversed(self.store.all())
             if t.state in (TaskState.FAILED, TaskState.CANCELLED) and t.steps), None)
        if src is None:
            return "There is no failed task to retry."
        if not src.state.terminal:
            return f"Task {src.id} is still {src.state.value.lower()}."
        if src.state == TaskState.COMPLETED:
            return f"Task {src.id} already completed."
        if not src.steps:
            return self.submit(src.request, origin="retry", background=background, language=src.language)
        self._release_on_user_command("retry")
        task = self.store.create(src.request, origin="retry", language=src.language, priority=src.priority)
        task.retry_of = src.id
        for s in src.steps:
            if s.status == StepStatus.SUPERSEDED:
                continue
            ns = Step(id=s.id, description=s.description, tool=s.tool, args=dict(s.args), agent=s.agent,
                      depends_on=list(s.depends_on))
            if s.status == StepStatus.DONE:
                ns.status, ns.output, ns.message = StepStatus.DONE, dict(s.output), s.message
                ns.verification = dict(s.verification)
                task.completed_steps.append(ns.id)
            task.steps.append(ns)
        self._start(task, background)
        return task

    def submit_steps(self, request: str, steps: list[Step], origin: str = "workflow",
                     background: bool = True) -> Task:
        """Run a pre-built plan (saved workflow). Still validated, still
        permission-checked step by step."""
        self._release_on_user_command(origin)
        task = self.store.create(request, origin=origin, priority=Priority.infer(request, origin).name)
        task.steps = [Step(id=s.id, description=s.description, tool=s.tool, args=dict(s.args),
                           agent=s.agent, depends_on=list(s.depends_on)) for s in steps]
        self._start(task, background)
        return task

    def _start(self, task: Task, background: bool) -> None:
        ctl = _Control()
        ctl.resume.set()
        with self._lock:
            self._controls[task.id] = ctl
        if background:
            how = self.scheduler.submit(task.id, Priority.parse(task.priority), lambda: self._run_safe(task))
            if how == "queued":
                n = self.scheduler.position(task.id)
                task.blocked_reason = (f"Queued ({task.priority.lower()} priority) — {n} task(s) ahead; "
                                       f"at most {self.scheduler.max_parallel} run at once.")
                self.store.save()
                self.notify(task, "queued", f"Task {task.id} is queued: {task.blocked_reason}")
        else:
            self._run_safe(task)

    def _is_idle(self, task_id: str) -> bool:
        t = self.store.get(task_id)
        return t is not None and t.state in _IDLE_STATES

    def wait(self, task_id: str, timeout: float | None = None) -> Task:
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            th = self._threads.get(task_id) or self.scheduler.thread(task_id)
            if th is not None:
                th.join(None if end is None else max(0.0, end - time.monotonic()))
                return self.store.get(task_id)
            t = self.store.get(task_id)
            if t is None or t.state.terminal or (end is not None and time.monotonic() >= end):
                return t
            time.sleep(0.05)

    def _target(self, task_id: str | None) -> Optional[Task]:
        if task_id:
            return self.store.get(task_id)
        act = self.store.active()
        return act[-1] if act else None

    def cancel(self, task_id: str | None = None) -> str:
        task = self._target(task_id)
        if task is None or task.state.terminal:
            return "There is no running task to cancel."
        ctl = self._controls.get(task.id)
        if ctl is None:
            return "That task is not controllable."
        ctl.cancel.set()
        ctl.resume.set()
        ctl.approved = False
        ctl.deny_reason = "cancelled"
        ctl.decision.set()
        ctl.answer_ready.set()
        if ctl.confirm_key:
            try:
                self.confirmer.cancel(ctl.confirm_key)
            except Exception:
                pass
        if task.state == TaskState.QUEUED and self.scheduler.remove(task.id):
            task.blocked_reason = ""
            self._finish_cancelled(task, f"Task {task.id} was cancelled before it started.")
            return f"Cancelled task {task.id} (it had not started yet)."
        return f"Cancelling task {task.id}. Remaining steps will not run."

    def pause(self, task_id: str | None = None) -> str:
        task = self._target(task_id)
        if task is None or task.state.terminal:
            return "There is no running task to pause."
        ctl = self._controls.get(task.id)
        ctl.paused = True
        ctl.resume.clear()
        return f"Task {task.id} will pause after the current step."

    def resume(self, task_id: str | None = None) -> str:
        task = self._target(task_id)
        if task is None:
            return "There is no task to resume."
        ctl = self._controls.get(task.id)
        if ctl is None or not ctl.paused:
            return f"Task {task.id} is not paused."
        ctl.paused = False
        ctl.resume.set()
        return f"Resuming task {task.id}."

    def answer(self, text: str, task_id: str | None = None) -> str:
        task = None
        if task_id:
            task = self.store.get(task_id)
        else:
            waiting = [t for t in self.store.active() if t.state == TaskState.WAITING_FOR_INPUT]
            task = waiting[-1] if waiting else None
        if task is None or task.state != TaskState.WAITING_FOR_INPUT:
            return "No task is waiting for an answer."
        ctl = self._controls[task.id]
        ctl.answer = str(text)
        ctl.answer_ready.set()
        return f"Thanks — continuing task {task.id}."

    def status(self, task_id: str | None = None) -> str:
        return self.store.describe(self.store.get(task_id) if task_id else None)

    # ═════════════════════════════════════════════════════════════════════
    # Execution
    # ═════════════════════════════════════════════════════════════════════
    def _run_safe(self, task: Task) -> None:
        try:
            self._run(task)
        except _Stop:
            return
        except TaskCancelled:
            self._finish_cancelled(task, "Cancelled.")
        except Exception as e:  # an orchestrator bug must not leave a task "RUNNING" forever
            from core.secret_store import redact
            task.error = f"Internal error: {type(e).__name__}: {redact(e)[:160]}"
            if not task.state.terminal:
                self._transition(task, TaskState.FAILED, task.error)
            self._finish(task, "failed", task.error)

    def _transition(self, task: Task, state: TaskState, note: str = "") -> None:
        self.store.transition(task, state, note)
        self.notify(task, "state", state.value)
        if state in _IDLE_STATES:
            try:
                self.scheduler.pump()          # a task waiting for the user frees its slot
            except Exception:
                pass

    def _check_cancel(self, task: Task) -> None:
        ctl = self._controls[task.id]
        if self.kill.engaged():
            ctl.cancel.set()
        if ctl.cancel.is_set():
            raise TaskCancelled()
        if ctl.paused:
            self._transition(task, TaskState.PAUSED, "paused by user")
            self.notify(task, "paused", f"Task {task.id} paused.")
            while not ctl.resume.wait(0.2):
                if ctl.cancel.is_set():
                    raise TaskCancelled()
            if ctl.cancel.is_set():
                raise TaskCancelled()
            self._transition(task, TaskState.RUNNING, "resumed")

    def _run(self, task: Task) -> None:
        task.blocked_reason = ""
        # ── plan ───────────────────────────────────────────────────────────
        if not task.steps:
            self._transition(task, TaskState.PLANNING)
            ctx_text = self._context(task.request)
            try:
                plan: Plan = self.planner.plan(task.request, context=ctx_text)
            except TypeError:          # a planner without context support
                plan = self.planner.plan(task.request)
            if not plan.ok:
                task.error = plan.reason or "I couldn't plan that."
                self._transition(task, TaskState.FAILED, "planning failed")
                self._finish(task, "failed", task.error)
                return
            task.steps = plan.steps
            task.language = plan.language
        else:
            done_ids = [s.id for s in task.steps if s.status == StepStatus.DONE]
            todo = [s for s in task.steps if s.status != StepStatus.DONE]
            errs = validate_plan(Plan(steps=todo), self.registry, prior_ids=done_ids) if todo else []
            if errs:
                task.error = "The saved plan is no longer valid: " + "; ".join(errs[:2])
                self._transition(task, TaskState.FAILED, "invalid plan")
                self._finish(task, "failed", task.error)
                return
        task.required_permissions = self._preview_permissions(task)
        self.store.save()
        if self.audit:
            self.audit.record("task_planned", task_id=task.id, steps=[f"{s.id}:{s.tool}" for s in task.steps],
                              origin=task.origin)
        self.notify(task, "planned", self._plan_sentence(task))
        self._check_cancel(task)
        self._transition(task, TaskState.RUNNING)

        outputs: dict[str, dict] = {}
        unverified: list[str] = []
        for s in task.steps:                         # retry: reuse completed outputs
            if s.status == StepStatus.DONE:
                outputs[s.id] = dict(s.output, message=s.message)
        i = 0
        while i < len(task.steps):                   # index loop: recovery may append steps
            step = task.steps[i]
            i += 1
            if step.status in (StepStatus.DONE, StepStatus.SUPERSEDED):
                continue
            self._check_cancel(task)
            bad_dep = [d for d in step.depends_on
                       if (task.step(d) is None) or task.step(d).status != StepStatus.DONE]
            if bad_dep:
                step.status = StepStatus.SKIPPED
                step.error = f"Skipped: depends on {', '.join(bad_dep)} which did not complete."
                continue
            task.current_step = step.id
            ok = self._run_step(task, step, outputs)
            if not ok:
                if task.state.terminal and task.state != TaskState.CANCELLED:
                    return                           # already finalised (e.g. unanswered question)
                if task.state != TaskState.CANCELLED and (
                        self._controls[task.id].cancel.is_set() or self.kill.engaged()):
                    raise TaskCancelled()            # stopped mid-step → CANCELLED, not FAILED
                if task.state == TaskState.CANCELLED:
                    for s in task.steps:
                        if s.status == StepStatus.PENDING:
                            s.status = StepStatus.SKIPPED
                    return
                if self._try_recover(task, step, outputs):
                    continue
                for s in task.steps:
                    if s.status == StepStatus.PENDING:
                        s.status = StepStatus.SKIPPED
                task.failed_steps.append(step.id)
                task.error = step.error or "A step failed."
                self._transition(task, TaskState.FAILED, f"{step.id} failed")
                self._finish(task, "failed", self._failure_sentence(task, step))
                return
            if step.verification.get("status") == VStatus.UNVERIFIABLE.value:
                unverified.append(step.id)

        # ── task-level verification ────────────────────────────────────────
        self._transition(task, TaskState.VERIFYING, "final check")
        not_done = [s.id for s in task.steps if s.status not in (StepStatus.DONE, StepStatus.SUPERSEDED)]
        if not_done:
            task.error = f"Steps did not complete: {', '.join(not_done)}"
            task.verification_status = "FAILED"
            self._transition(task, TaskState.FAILED, "final verification failed")
            self._finish(task, "failed", task.error)
            return
        task.verification_status = "UNVERIFIED" if unverified else "VERIFIED"
        task.current_step = ""
        task.result = self._success_sentence(task, outputs, unverified)
        self._transition(task, TaskState.COMPLETED)
        self._finish(task, "completed", task.result)

    # ── one step ─────────────────────────────────────────────────────────────
    def _run_step(self, task: Task, step: Step, outputs: dict) -> bool:
        spec = self.registry.get(step.tool)
        if spec is None:
            step.status, step.error = StepStatus.FAILED, f"Unknown tool {step.tool}"
            return False
        args = self._resolve(step.args, outputs)
        if isinstance(args, _Missing):
            step.status, step.error = StepStatus.FAILED, args.reason
            return False
        # "$ask:" values → WAITING_FOR_INPUT
        for k, v in list(args.items()):
            if isinstance(v, str) and v.startswith("$ask:"):
                ans = self._ask(task, step, v[5:].strip())
                args[k] = ans
                step.args[k] = ans          # remember the answer in the plan

        decision = self.permissions.decide(step.tool, args, {"origin": task.origin})
        step.permission = decision.to_dict()
        if decision.decision == Decision.DENY:
            self._audit_perm(task, step, decision, "denied")
            step.status, step.error = StepStatus.FAILED, decision.reason
            return False
        if decision.decision == Decision.CONFIRM:
            if not self._await_permission(task, step, spec, args, decision):
                return False
        else:
            self._audit_perm(task, step, decision, "allowed")

        agent = self.agents.get(spec.agent) or self.agents.owner_of(step.tool)
        tools_to_try = [step.tool] + [a for a in (spec.alternatives or []) if self.registry.has(a)]
        last: ToolResult | None = None
        for tool_name in tools_to_try:
            if tool_name != step.tool:
                alt_dec = self.permissions.decide(tool_name, args, {"origin": task.origin})
                if alt_dec.decision != Decision.ALLOW:
                    continue          # never escalate silently through an alternative
            tspec = self.registry.get(tool_name)
            attempts_allowed = 1 + (self.max_retries if tspec.retry_safe else 0)
            delay = self.retry_backoff
            while step.attempts < 50:
                self._check_cancel(task)
                step.attempts += 1
                step.status = StepStatus.RUNNING
                step.started_at = step.started_at or time.time()
                self.notify(task, "step", f"{step.id}: {step.description}")
                result = self._execute(task, step, agent, tool_name, args)
                last = result
                if not result.ok and result.data.get("needs_input"):
                    ans = self._ask(task, step, result.error or "Which one?")
                    args["choice"] = ans
                    step.attempts -= 1
                    continue
                if result.ok:
                    v = self._verify(task, step, tool_name, args, result)
                    if v is None or not v.failed:
                        step.status = StepStatus.DONE
                        step.finished_at = time.time()
                        step.output = dict(result.data)
                        step.message = result.message
                        outputs[step.id] = dict(result.data, message=result.message)
                        task.completed_steps.append(step.id)
                        self._audit_call(task, step, tool_name, args, "ok", step.verification)
                        if self.on_step_done:
                            try:
                                self.on_step_done(task, step, result)
                            except Exception:
                                pass
                        return True
                    # verification failed: a real post-condition is false
                    step.error = ("The action reported success, but verification failed: "
                                  + v.detail)
                    self._audit_call(task, step, tool_name, args, "verification_failed", step.verification)
                    retry = tspec.retry_safe and step.attempts < attempts_allowed
                else:
                    step.error = result.error or "The step failed."
                    fail = _recovery.classify(result)
                    step.error_class = fail.kind.value
                    self._audit_call(task, step, tool_name, args, "failed", {})
                    # classification can only remove retries that cannot help — never add unsafe ones
                    retry = ((tspec.retry_safe or result.retryable) and fail.retry
                             and not result.data.get("timed_out") and step.attempts < attempts_allowed)
                    delay = max(delay, fail.backoff)
                if not retry:
                    break
                self.notify(task, "retry", f"Retrying {step.description} ({step.attempts}/{attempts_allowed}).")
                self._sleep(delay)
                delay = min(delay * 2, 8.0)
            step.attempts = 0 if tool_name != tools_to_try[-1] else step.attempts
        step.status = StepStatus.FAILED
        step.finished_at = time.time()
        if last is not None and not step.error:
            step.error = last.error
        if not step.error_class and step.error:
            step.error_class = _recovery.classify(step.error).kind.value
        return False

    def _execute(self, task: Task, step: Step, agent, tool_name: str, args: dict) -> ToolResult:
        ctx = self.context_factory(task, step)
        step_stop = threading.Event()
        try:
            for k, v in self._hooks(task, step).items():
                ctx.extras.setdefault(k, v)
        except Exception:
            pass
        base_stop = ctx.extras.get("should_stop") or (lambda: False)
        ctx.extras["should_stop"] = lambda: step_stop.is_set() or bool(base_stop())
        services = AgentServices(self.registry, tool_name, agent, ctx, inputs={},
                                 ask_ai=self.ask_ai, log=lambda m: self.notify(task, "log", m))
        run_step = step if step.tool == tool_name else Step(id=step.id, description=step.description,
                                                            tool=tool_name, args=args)
        owner = f"task:{task.id}"
        held: list[str] = []
        try:
            self.kill.check()
            for res in self._resources(tool_name):
                if not self._acquire(task, res, owner):
                    if self._controls[task.id].cancel.is_set() or self.kill.engaged():
                        raise TaskCancelled()
                    return ToolResult.failure(f"{RESOURCE_LABEL.get(res, res).capitalize()} stayed busy "
                                              f"({self.locks.holder(res) or 'another job'}), so I gave up waiting.",
                                              retryable=False)
                held.append(res)
            return self._watchdog(task, step, tool_name, lambda: agent.execute(run_step, dict(args), services),
                                  step_stop)
        except EmergencyStop as e:
            self._controls[task.id].cancel.set()
            return ToolResult.failure(str(e))
        except TaskCancelled:
            raise
        except PermissionError as e:
            return ToolResult.failure(str(e))
        except Exception as e:
            from core.secret_store import redact
            return ToolResult.failure(f"{type(e).__name__}: {redact(e)[:200]}")
        finally:
            for res in held:
                self.locks.release(res, owner)

    # ── resources, watchdog ─────────────────────────────────────────────────
    def _resources(self, tool_name: str) -> list:
        try:
            from core.config import runtime
            if not runtime("tasks", "gui_exclusive", True):
                return []
        except Exception:
            pass
        rf = getattr(self.registry, "resources_for", None)
        return list(rf(tool_name)) if rf else []

    def _acquire(self, task: Task, res: str, owner: str) -> bool:
        ctl = self._controls[task.id]
        try:
            from core.config import runtime
            wait_max = float(runtime("tasks", "gui_wait", 600) or 600)
        except Exception:
            wait_max = 600.0
        label = RESOURCE_LABEL.get(res, res)

        def on_wait(holder: str) -> None:
            who = (f"task {holder.split(':', 1)[1]}" if holder.startswith("task:")
                   else "a voice command" if holder == "voice" else holder)
            task.blocked_reason = f"Waiting for {label} — {who} is using them."
            if task.state == TaskState.RUNNING:
                self._transition(task, TaskState.BLOCKED, task.blocked_reason)
            self.notify(task, "blocked", f"Task {task.id}: {task.blocked_reason}")

        ok = self.locks.acquire(res, owner, timeout=wait_max, on_wait=on_wait,
                                should_stop=lambda: ctl.cancel.is_set() or self.kill.engaged())
        if task.state == TaskState.BLOCKED:
            self._transition(task, TaskState.RUNNING, f"{label} free" if ok else "gave up waiting")
        task.blocked_reason = ""
        return ok

    def _watchdog(self, task: Task, step: Step, tool_name: str, call: Callable[[], Any],
                  step_stop: threading.Event) -> Any:
        """Run one tool call on a worker thread under its timeout. Waiting for the
        user (HUD confirmation / an answer) does not count against the timeout."""
        rt = getattr(self.registry, "timeout_for", None)
        timeout = float(rt(tool_name)) if rt else 300.0
        box: dict = {}

        def work():
            try:
                with undo_context(task_id=task.id, step_id=step.id, tool=tool_name):
                    box["r"] = call()
            except BaseException as e:           # re-raised on the task thread
                box["e"] = e
        th = threading.Thread(target=work, daemon=True, name=f"step-{task.id}-{step.id}")
        th.start()
        ctl = self._controls[task.id]
        deadline = time.monotonic() + timeout
        last = time.monotonic()
        stop_seen = None
        while True:
            th.join(0.1)
            if not th.is_alive():
                break
            now = time.monotonic()
            if task.state in _USER_WAIT:
                deadline += now - last                # the clock stops while the user decides
            last = now
            if ctl.cancel.is_set() or self.kill.engaged():
                step_stop.set()
                stop_seen = stop_seen or now
                if now - stop_seen > self.stop_grace:
                    raise TaskCancelled()             # tool ignores should_stop — abandon it
            elif now > deadline:
                step_stop.set()
                th.join(self.stop_grace)
                if self.audit:
                    self.audit.record("step_timeout", task_id=task.id, step=step.id, tool=tool_name,
                                      detail=f"{int(timeout)}s; still running={th.is_alive()}")
                return ToolResult.failure(f"Timed out after {int(timeout)} s — \"{step.description}\" took too "
                                          f"long, so I stopped it.", retryable=False, timed_out=True)
        if "e" in box:
            raise box["e"]
        return box.get("r") if box.get("r") is not None else ToolResult.failure("The step returned nothing.")

    def _verify(self, task: Task, step: Step, tool_name: str, args: dict, result: ToolResult):
        spec = self.registry.get(tool_name)
        if spec is None or spec.verify is None:
            step.verification = {"status": VStatus.SKIPPED.value, "detail": "no verifier"}
            return None
        self._transition(task, TaskState.VERIFYING, f"verify {step.id}")
        v = self.registry.verify(tool_name, args, result)
        step.verification = v.to_dict()
        self._transition(task, TaskState.RUNNING, "verified" if v.ok else v.status.value)
        return v

    # ── waiting states ───────────────────────────────────────────────────────
    def _ask(self, task: Task, step: Step, question: str) -> str:
        ctl = self._controls[task.id]
        ctl.answer, ctl.answer_ready = None, threading.Event()
        task.pending_question = question
        step.status = StepStatus.WAITING
        self._transition(task, TaskState.WAITING_FOR_INPUT, question)
        self.notify(task, "question", question)
        got = ctl.answer_ready.wait(self.input_timeout)
        task.pending_question = ""
        if ctl.cancel.is_set():
            raise TaskCancelled()
        if not got or not (ctl.answer or "").strip():
            step.status = StepStatus.FAILED
            step.error = "No answer was given, so I stopped here."
            task.error = step.error
            self._transition(task, TaskState.FAILED, "no answer")
            self._finish(task, "failed", step.error)
            raise _Stop()
        self._transition(task, TaskState.RUNNING, "answer received")
        return ctl.answer.strip()

    def _await_permission(self, task, step, spec, args, decision) -> bool:
        ctl = self._controls[task.id]
        title = spec.confirm_text(args) if spec.confirm_text else f"{step.description}"
        question = self._permission_question(step, args, title)
        ctl.decision = threading.Event()
        ctl.approved, ctl.deny_reason = None, ""
        ctl.confirm_key = f"task-{task.id}-{step.id}"
        task.pending_question = question
        step.status = StepStatus.WAITING
        self._transition(task, TaskState.WAITING_FOR_PERMISSION, title)
        self.notify(task, "permission", question)

        def approve():
            ctl.approved = True
            ctl.decision.set()

        def deny(reason: str):
            ctl.approved = False
            ctl.deny_reason = reason or "declined"
            ctl.decision.set()

        # Phase 7: ALLOW ALWAYS is offered for this tool (+action) unless it is
        # critical or a plugin-permission grant; core.permissions decides the rest.
        _act = str((args or {}).get("action", "") or "").strip().lower()
        _trust = "" if (decision.risk >= RiskLevel.CRITICAL or getattr(decision, "grant", None)) \
            else (f"{step.tool}.{_act}" if _act else step.tool)
        try:
            try:
                self.confirmer.request(ctl.confirm_key, title,
                                       f"Task {task.id} · {decision.risk.name.replace('_', ' ').title()} · "
                                       f"{decision.reason}", approve, deny, trust_key=_trust)
            except TypeError:               # a confirmer without the 3-way banner (tests, plugins)
                self.confirmer.request(ctl.confirm_key, title,
                                       f"Task {task.id} · {decision.risk.name.replace('_', ' ').title()} · "
                                       f"{decision.reason}", approve, deny)
        except Exception as e:
            deny(f"unavailable ({type(e).__name__})")
        got = ctl.decision.wait(self.permission_timeout)
        ctl.confirm_key = ""
        task.pending_question = ""
        if not got:
            try:
                self.confirmer.cancel(f"task-{task.id}-{step.id}")
            except Exception:
                pass
            ctl.approved, ctl.deny_reason = False, "expired"
        if ctl.cancel.is_set():
            raise TaskCancelled()
        if ctl.approved is True:
            if getattr(decision, "grant", None):
                try:
                    self.permissions.record_grant(step.tool, decision.grant)
                except Exception:
                    pass
            self._audit_perm(task, step, decision, "approved")
            self._transition(task, TaskState.RUNNING, "permission granted")
            return True
        self._audit_perm(task, step, decision, ctl.deny_reason or "declined")
        step.status = StepStatus.CANCELLED
        step.error = f"Permission {ctl.deny_reason or 'declined'}."
        self._finish_cancelled(task, self._declined_sentence(task, step, ctl.deny_reason))
        return False

    # ── self-recovery ────────────────────────────────────────────────────────
    def _try_recover(self, task: Task, step: Step, outputs: dict) -> bool:
        """analyse failure → alternative plan → validate → continue (once per task)."""
        if not self.recovery or task.recoveries >= self.max_recoveries:
            return False
        if not hasattr(self.planner, "recover"):
            return False
        perm = (step.permission or {}).get("decision")
        if perm == Decision.DENY.value or step.status == StepStatus.CANCELLED:
            return False                     # policy said no — never route around it
        if self.kill.engaged() or self._controls[task.id].cancel.is_set():
            return False
        fail = _recovery.failure_for(step.error_class) or _recovery.classify(step.error or "")
        if not fail.replan:
            return False                     # auth / policy / declined / cancelled / bad input: replanning can't help
        task.recoveries += 1
        self._transition(task, TaskState.RECOVERING, f"{step.id} failed ({fail.kind.value})")
        done = [s for s in task.steps if s.status == StepStatus.DONE]
        self.notify(task, "recovery", f"\"{step.description}\" failed ({(step.error or '')[:120]}). "
                                      f"Looking for another way…")
        try:
            plan = self.planner.recover(task.request, done, step, step.error or "", context=self._context(
                task.request))
        except Exception as e:
            plan = Plan(ok=False, reason=f"{type(e).__name__}")
        if self.audit:
            self.audit.record("task_recovery", task_id=task.id, failed_step=step.id, tool=step.tool,
                              ok=bool(plan.ok), steps=[f"{s.id}:{s.tool}" for s in plan.steps] if plan.ok else [],
                              detail=plan.reason[:160] if not plan.ok else "")
        if not plan.ok or not plan.steps:
            step.error = (step.error or "") + (f" (No alternative: {plan.reason[:120]})" if plan.reason else "")
            self._transition(task, TaskState.RUNNING, "no alternative")
            return False
        taken = {s.id for s in task.steps}
        for s in plan.steps:
            if s.id in taken:
                self._transition(task, TaskState.RUNNING, "invalid recovery plan")
                return False
        step.status = StepStatus.SUPERSEDED
        step.error = (step.error or "") + " — superseded by a recovery plan."
        for s in task.steps:
            if s.status == StepStatus.PENDING:
                s.status = StepStatus.SUPERSEDED
                s.error = "Superseded by a recovery plan."
        task.steps.extend(plan.steps)
        self.store.save()
        self._transition(task, TaskState.RUNNING, "recovery plan")
        self.notify(task, "recovery", "New approach: " + " → ".join(s.description for s in plan.steps))
        return True

    def _context(self, request: str) -> str:
        if not self.context_provider:
            return ""
        try:
            return str(self.context_provider(request) or "")
        except Exception:
            return ""

    # ── mid-task hooks given to tools ────────────────────────────────────────
    def _confirm_inline(self, task: Task, step: Step, title: str, detail: str = "") -> bool:
        """A tool asks for a HUD confirmation mid-step (e.g. the computer agent
        before a risky click). Blocks; False on decline / timeout / cancel."""
        ctl = self._controls[task.id]
        self._check_cancel(task)
        ev = threading.Event()
        box = {"ok": False}
        key = f"task-{task.id}-{step.id}-inline-{int(time.time() * 1000) % 100000}"
        ctl.confirm_key = key
        task.pending_question = f"{title} — shall I go ahead? Please confirm on the HUD."
        prev = task.state
        if task.state != TaskState.WAITING_FOR_PERMISSION:
            if task.state == TaskState.VERIFYING:
                self._transition(task, TaskState.RUNNING)
            self._transition(task, TaskState.WAITING_FOR_PERMISSION, title)
        self.notify(task, "permission", task.pending_question)

        def approve():
            box["ok"] = True
            ev.set()

        def deny(reason: str = ""):
            box["ok"] = False
            ev.set()

        try:
            self.confirmer.request(key, title, f"Task {task.id} · {detail}"[:200], approve, deny)
        except Exception:
            deny("unavailable")
        # poll so an emergency stop / cancel interrupts the wait
        deadline = time.time() + self.permission_timeout
        while not ev.wait(0.2):
            if ctl.cancel.is_set() or self.kill.engaged() or time.time() > deadline:
                try:
                    self.confirmer.cancel(key)
                except Exception:
                    pass
                break
        ctl.confirm_key = ""
        task.pending_question = ""
        if self.audit:
            self.audit.record("permission", task_id=task.id, step=step.id, tool=step.tool,
                              action="inline", decision="CONFIRM", result="approved" if box["ok"] else "declined",
                              detail=title[:120])
        if not task.state.terminal and task.state == TaskState.WAITING_FOR_PERMISSION:
            self._transition(task, TaskState.RUNNING, "inline confirmation " + ("approved" if box["ok"] else "declined"))
        return bool(box["ok"]) and not ctl.cancel.is_set() and not self.kill.engaged()

    def _hooks(self, task: Task, step: Step) -> dict:
        ctl = self._controls[task.id]
        return {
            "confirm": lambda title, detail="": self._confirm_inline(task, step, title, detail),
            "ask": lambda q: self._ask(task, step, q),
            "progress": lambda msg: self.notify(task, "progress", str(msg)[:300]),
            "should_stop": lambda: ctl.cancel.is_set() or self.kill.engaged(),
            "context": lambda q="": self._context(q or task.request),
            "task_request": task.request,
        }

    # ── helpers ──────────────────────────────────────────────────────────────
    def _resolve(self, value: Any, outputs: dict) -> Any:
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                r = self._resolve(v, outputs)
                if isinstance(r, _Missing):
                    return r
                out[k] = r
            return out
        if isinstance(value, list):
            out = []
            for v in value:
                r = self._resolve(v, outputs)
                if isinstance(r, _Missing):
                    return r
                out.append(r)
            return out
        if isinstance(value, str) and "$s" in value:
            full = REF_RE.fullmatch(value.strip())
            if full:
                sid, fld = full.group(1), full.group(2)
                if sid not in outputs or fld not in outputs[sid]:
                    return _Missing(f"Step input {value} is not available (the earlier step produced no "
                                    f"'{fld}').")
                return outputs[sid][fld]

            def sub(m):
                sid, fld = m.group(1), m.group(2)
                return str(outputs.get(sid, {}).get(fld, m.group(0)))
            return REF_RE.sub(sub, value)
        return value

    def _preview_permissions(self, task: Task) -> list:
        out = []
        for s in task.steps:
            spec = self.registry.get(s.tool)
            if spec is None:
                continue
            args_preview = {k: v for k, v in s.args.items() if not (isinstance(v, str) and v.startswith("$"))}
            d = self.permissions.decide(s.tool, {**{k: "x" for k in (spec.input_schema or {}).get("required", [])},
                                                 **args_preview}, {"origin": task.origin})
            if d.decision == Decision.CONFIRM:
                out.append(f"{s.id}: {s.description}")
        return out

    def _audit_perm(self, task, step, decision, result):
        if self.audit:
            self.audit.record("permission", task_id=task.id, step=step.id, agent=step.agent, tool=step.tool,
                              action=decision.action, decision=decision.decision.value,
                              risk=decision.risk.name, result=result)

    def _audit_call(self, task, step, tool, args, result, verification):
        if self.audit:
            self.audit.record("tool_call", task_id=task.id, step=step.id, agent=step.agent, tool=tool,
                              action=str(args.get("action", "")), args=args, result=result,
                              verification=(verification or {}).get("status"),
                              detail=(verification or {}).get("detail") or step.error)

    def _finish(self, task: Task, kind: str, message: str) -> None:
        self.locks.release_all(f"task:{task.id}")
        self.store.save()
        if self.audit:
            self.audit.record("task_finished", task_id=task.id, result=task.state.value,
                              verification=task.verification_status, detail=message)
        self.notify(task, kind, message)

    def _finish_cancelled(self, task: Task, message: str) -> None:
        for s in task.steps:
            if s.status in (StepStatus.PENDING, StepStatus.WAITING, StepStatus.RUNNING):
                s.status = StepStatus.CANCELLED
        if not task.state.terminal:
            self._transition(task, TaskState.CANCELLED, message)
        task.result = message
        self._finish(task, "cancelled", message)

    # ── sentences (English; the voice layer re-phrases in the user's language)
    def _plan_sentence(self, task: Task) -> str:
        steps = "; ".join(f"{i + 1}. {s.description}" for i, s in enumerate(task.steps))
        extra = (" I'll ask before: " + ", ".join(p.split(": ", 1)[-1] for p in task.required_permissions) + "."
                 if task.required_permissions else "")
        return f"Plan for task {task.id} ({len(task.steps)} steps): {steps}.{extra}"

    @staticmethod
    def _permission_question(step: Step, args: dict, title: str) -> str:
        if step.tool in ("message.send", "send_message"):
            who = args.get("recipient") or args.get("receiver") or "they"
            return f"The message for {who} is ready. Shall I send it? Please confirm on the HUD."
        return f"{title} — shall I go ahead? Please confirm on the HUD."

    @staticmethod
    def _declined_sentence(task: Task, step: Step, reason: str) -> str:
        done = [task.step(s).description for s in task.completed_steps if task.step(s)]
        why = {"expired": "the confirmation timed out", "declined": "you declined",
               "cancelled": "it was cancelled", "superseded": "another confirmation replaced it",
               "unavailable": "I couldn't show the confirmation"}.get(reason, reason or "you declined")
        base = f"I stopped before \"{step.description}\" because {why}."
        return base + (f" Already done: {'; '.join(done)}." if done else " Nothing was changed.")

    @staticmethod
    def _failure_sentence(task: Task, step: Step) -> str:
        done = [task.step(s).description for s in task.completed_steps if task.step(s)]
        msg = f"Task {task.id} failed at \"{step.description}\": {step.error}"
        fail = _recovery.failure_for(step.error_class) if step.error_class else None
        if fail is not None and fail.hint and fail.hint.lower() not in (step.error or "").lower():
            msg += f" (Likely cause: {fail.hint}.)"
        if task.recoveries:
            msg += " (I had already tried a different approach after the first failure.)"
        if done:
            msg += f" Completed before that: {'; '.join(done)}."
        skipped = [s.description for s in task.steps if s.status == StepStatus.SKIPPED]
        if skipped:
            msg += f" Not done: {'; '.join(skipped)}."
        return msg

    @staticmethod
    def _success_sentence(task: Task, outputs: dict, unverified: list) -> str:
        parts = [f"Task {task.id} completed: " + "; ".join(s.message or s.description for s in task.steps
                                                           if s.message or s.description)]
        last = task.steps[-1] if task.steps else None
        if last and last.output.get("summary"):
            parts.append("Summary: " + str(last.output["summary"])[:600])
        elif last and last.output.get("draft"):
            parts.append("Draft: " + str(last.output["draft"])[:400])
        if unverified:
            names = [task.step(s).description for s in unverified if task.step(s)]
            parts.append("Note — I could not independently confirm: " + "; ".join(names) + ".")
        return " ".join(parts)


class _Missing:
    def __init__(self, reason: str):
        self.reason = reason


class _Stop(TaskCancelled):
    """Internal: task already finalised (e.g. no answer) — unwind quietly."""
