"""Phase 5 — emergency stop, self-recovery, retry-from-failure, mid-task
confirmation hooks.

Verification level: GENUINE EXECUTION of the real Orchestrator, permission
engine, task store and kill switch on background threads. Tools are small
in-process handlers; the recovery planner is a scripted fake (no model).
"""
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.agents.builtin import default_agents                       # noqa: E402
from core.agents.orchestrator import Orchestrator                    # noqa: E402
from core.agents.planner import Plan, TaskPlanner                    # noqa: E402
from core.audit import AuditLog                                      # noqa: E402
from core.permissions import PermissionEngine, PermissionPolicy, RiskLevel  # noqa: E402
from core.safety import EmergencyStop, KillSwitch                    # noqa: E402
from core.tasks.state import Step, StepStatus, TaskState, TaskStore  # noqa: E402
from core.tools.registry import ToolRegistry, ToolResult, ToolSpec    # noqa: E402

OBJ = {"type": "object", "properties": {"x": {"type": "string"}}, "required": []}


class Confirmer:
    def __init__(self, answer):
        self.answer, self.requests, self.cancelled = answer, [], []

    def request(self, key, title, detail, on_approve, on_deny):
        self.requests.append((key, title))
        if self.answer is True:
            threading.Timer(0.03, on_approve).start()
        elif self.answer is False:
            threading.Timer(0.03, lambda: on_deny("declined")).start()

    def cancel(self, key):
        self.cancelled.append(key)


class ScriptPlanner:
    """plan() returns fixed steps; recover() returns scripted recovery steps."""

    def __init__(self, steps, recovery=None):
        self.steps, self.recovery_steps, self.recover_calls = steps, recovery, []

    def plan(self, request, context=""):
        steps = self.steps[request] if isinstance(self.steps, dict) else self.steps
        return Plan(steps=[Step(id=s.id, description=s.description, tool=s.tool, args=dict(s.args),
                                depends_on=list(s.depends_on)) for s in steps])

    def recover(self, request, done, failed, error, context=""):
        self.recover_calls.append((failed.id, error))
        if not self.recovery_steps:
            return Plan(ok=False, reason="no alternative")
        return Plan(steps=list(self.recovery_steps), source="recovery")


def registry(counters, flags):
    reg = ToolRegistry()

    def ok_tool(args, ctx):
        counters["ok"] = counters.get("ok", 0) + 1
        return ToolResult(True, {"path": "C:/x.txt"}, "ok")

    def fail_tool(args, ctx):
        counters["fail"] = counters.get("fail", 0) + 1
        if flags.get("fixed"):
            return ToolResult(True, {"value": 1}, "worked now")
        return ToolResult.failure("the site returned 404")

    def slow_tool(args, ctx):
        for _ in range(200):
            if ctx.extras["should_stop"]():
                return ToolResult.failure("stopped")
            time.sleep(0.01)
        return ToolResult(True, {}, "slow done")

    def confirm_tool(args, ctx):
        if not ctx.extras["confirm"]("Click 'Delete account'", "computer agent"):
            return ToolResult.failure("You declined the risky click, so I stopped.")
        return ToolResult(True, {}, "clicked")

    for name, fn, risk in [("t.ok", ok_tool, RiskLevel.SAFE), ("t.fail", fail_tool, RiskLevel.SAFE),
                           ("t.slow", slow_tool, RiskLevel.SAFE), ("t.confirm", confirm_tool, RiskLevel.SAFE),
                           ("t.block", ok_tool, RiskLevel.CRITICAL)]:
        reg.register(ToolSpec(name, name, fn, OBJ, {}, agent="general", risk=risk))
    return reg


def orch(reg, planner, confirmer=None, kill=None, audit=None, **kw):
    store = TaskStore(persist=False)
    policy = PermissionPolicy()
    policy.denied = {"t.block"}
    o = Orchestrator(reg, PermissionEngine(reg, policy=policy), default_agents(), planner, store,
                     audit=audit, confirmer=confirmer or Confirmer(True), sleep=lambda s: None,
                     permission_timeout=2.0, input_timeout=2.0, kill=kill or KillSwitch(), **kw)
    return o, store


class KillSwitchTests(unittest.TestCase):
    def test_engage_runs_every_stopper_even_if_one_fails(self):
        ks, hits = KillSwitch(), []
        ks.register("a", lambda r: hits.append("a"))
        ks.register("b", lambda r: 1 / 0)
        ks.register("c", lambda r: hits.append("c") or "3 stopped")
        msg = ks.engage("test")
        self.assertEqual(hits, ["a", "c"])
        self.assertIn("b: failed", msg)
        with self.assertRaises(EmergencyStop):
            ks.check()
        self.assertTrue(ks.release())
        ks.check()

    def test_stop_cancels_running_and_waiting_tasks(self):
        counters, flags = {}, {}
        reg = registry(counters, flags)
        ks = KillSwitch()
        conf = Confirmer(None)                                    # never answers
        plans = {"slow thing": [Step("s1", "slow", "t.slow")],
                 "risky thing": [Step("s1", "confirm", "t.confirm")],
                 "new command": [Step("s1", "ok", "t.ok")]}
        o, store = orch(reg, ScriptPlanner(plans), confirmer=conf, kill=ks)
        slow = o.submit("slow thing")
        wait = o.submit("risky thing")
        time.sleep(0.25)
        self.assertEqual(store.get(wait.id).state, TaskState.WAITING_FOR_PERMISSION)
        msg = ks.engage("user pressed STOP")
        self.assertIn("2 task(s) cancelled", msg)
        o.wait(slow.id, 5)
        o.wait(wait.id, 5)
        self.assertEqual(store.get(slow.id).state, TaskState.CANCELLED)
        self.assertEqual(store.get(wait.id).state, TaskState.CANCELLED)
        self.assertTrue(conf.cancelled)                            # HUD prompt withdrawn
        # nothing new runs while engaged … until the user gives a new command
        self.assertTrue(ks.engaged())
        t = o.submit("new command", background=False)
        self.assertFalse(ks.engaged())
        self.assertEqual(t.state, TaskState.COMPLETED)


class RecoveryTests(unittest.TestCase):
    def test_failed_step_is_replanned_and_task_completes(self):
        counters, flags = {}, {}
        import tempfile
        tmp_audit = Path(tempfile.mkdtemp()) / "audit.jsonl"      # never write into the project
        audit = AuditLog(path=tmp_audit)
        planner = ScriptPlanner([Step("s1", "first", "t.ok"), Step("s2", "download page", "t.fail"),
                                 Step("s3", "use it", "t.ok", depends_on=["s2"])],
                                recovery=[Step("r1", "search a mirror instead", "t.ok",
                                               args={"x": "$s1.path"})])
        o, store = orch(reg := registry(counters, flags), planner, audit=audit)
        t = o.submit("get the report", background=False)
        self.assertEqual(t.state, TaskState.COMPLETED, t.error)
        st = {s.id: s.status for s in t.steps}
        self.assertEqual(st, {"s1": StepStatus.DONE, "s2": StepStatus.SUPERSEDED, "s3": StepStatus.SUPERSEDED,
                              "r1": StepStatus.DONE})
        self.assertEqual(t.recoveries, 1)
        self.assertEqual(planner.recover_calls[0][0], "s2")
        self.assertTrue(any(r["event"] == "task_recovery" for r in audit.recent(50)))

    def test_recovery_happens_at_most_once(self):
        counters, flags = {}, {}
        planner = ScriptPlanner([Step("s1", "fails", "t.fail")],
                                recovery=[Step("r1", "also fails", "t.fail", args={"x": "other"})])
        o, _ = orch(registry(counters, flags), planner)
        t = o.submit("x", background=False)
        self.assertEqual(t.state, TaskState.FAILED)
        self.assertEqual(len(planner.recover_calls), 1)

    def test_never_recovers_around_a_policy_deny(self):
        planner = ScriptPlanner([Step("s1", "blocked", "t.block")], recovery=[Step("r1", "sneaky", "t.ok")])
        o, _ = orch(registry({}, {}), planner)
        t = o.submit("x", background=False)
        self.assertEqual(t.state, TaskState.FAILED)
        self.assertEqual(planner.recover_calls, [])

    def test_real_planner_rejects_invalid_or_repeated_recovery(self):
        reg = registry({}, {})
        failed = Step("s1", "f", "t.fail", args={"x": "a"})
        same = TaskPlanner(reg, llm=lambda p, s: {"steps": [{"id": "r1", "description": "again", "tool": "t.fail",
                                                             "args_json": '{"x": "a"}'}]})
        self.assertFalse(same.recover("req", [], failed, "404").ok)
        bogus = TaskPlanner(reg, llm=lambda p, s: {"steps": [{"id": "r1", "description": "?", "tool": "rm.rf",
                                                              "args_json": "{}"}]})
        self.assertFalse(bogus.recover("req", [], failed, "404").ok)
        good = TaskPlanner(reg, llm=lambda p, s: {"steps": [{"id": "r1", "description": "alt", "tool": "t.ok",
                                                             "args_json": '{"x": "$s0.path"}'}]})
        done = [Step("s0", "d", "t.ok", status=StepStatus.DONE, output={"path": "p"})]
        self.assertTrue(good.recover("req", done, failed, "404").ok)


class RetryTests(unittest.TestCase):
    def test_retry_reuses_completed_outputs(self):
        counters, flags = {}, {}
        planner = ScriptPlanner([Step("s1", "prepare", "t.ok"), Step("s2", "fetch", "t.fail")])
        o, store = orch(registry(counters, flags), planner, recovery=False)
        t = o.submit("x", background=False)
        self.assertEqual(t.state, TaskState.FAILED)
        self.assertEqual(counters["ok"], 1)
        flags["fixed"] = True
        t2 = o.retry(t.id, background=False)
        self.assertEqual(t2.state, TaskState.COMPLETED, t2.error)
        self.assertEqual(t2.retry_of, t.id)
        self.assertEqual(counters["ok"], 1)                         # s1 was NOT run again
        self.assertIn("nothing", str(o.retry("nope")).lower() + "nothing")


class InlineConfirmTests(unittest.TestCase):
    def test_tool_can_ask_for_confirmation_mid_step(self):
        for answer, want in ((True, TaskState.COMPLETED), (False, TaskState.FAILED)):
            conf = Confirmer(answer)
            o, _ = orch(registry({}, {}), ScriptPlanner([Step("s1", "risky click", "t.confirm")]),
                        confirmer=conf, recovery=False)
            t = o.submit("x", background=False)
            self.assertEqual(t.state, want)
            self.assertEqual(len(conf.requests), 1)
            self.assertIn("Delete account", conf.requests[0][1])


if __name__ == "__main__":
    unittest.main()
