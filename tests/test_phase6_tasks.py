"""Phase 6 — task priorities, scheduler, GUI lock, BLOCKED/RECOVERING states,
step timeouts, error classification, structured undo records.

Verification level: GENUINE EXECUTION of the real Orchestrator, TaskScheduler,
ResourceLocks, TaskStore, recovery classifier and undo stack on real threads.
Tools are small in-process handlers (sleeping / counting); no model is called.
"""
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import recovery as R                                        # noqa: E402
from core import undo                                                  # noqa: E402
from core.agents.builtin import default_agents                         # noqa: E402
from core.agents.orchestrator import Orchestrator                      # noqa: E402
from core.agents.planner import Plan                                   # noqa: E402
from core.permissions import PermissionEngine, PermissionPolicy, RiskLevel  # noqa: E402
from core.safety import KillSwitch                                     # noqa: E402
from core.tasks.scheduler import Priority, ResourceLocks, TaskScheduler  # noqa: E402
from core.tasks.state import Step, StepStatus, Task, TaskState, TaskStore  # noqa: E402
from core.tools.registry import ToolRegistry, ToolResult, ToolSpec      # noqa: E402

OBJ = {"type": "object", "properties": {"x": {"type": "string"}}, "required": []}


class Confirmer:
    def __init__(self, answer=True, delay=0.03):
        self.answer, self.delay = answer, delay

    def request(self, key, title, detail, on_approve, on_deny):
        if self.answer is True:
            threading.Timer(self.delay, on_approve).start()
        elif self.answer is False:
            threading.Timer(self.delay, lambda: on_deny("declined")).start()

    def cancel(self, key):
        pass


class Planner:
    def __init__(self, plans):
        self.plans, self.recover_calls = plans, []

    def plan(self, request, context=""):
        return Plan(steps=[Step(id=s.id, description=s.description, tool=s.tool, args=dict(s.args))
                           for s in self.plans[request]])

    def recover(self, request, done, failed, error, context=""):
        self.recover_calls.append(failed.id)
        return Plan(steps=[Step("r1", "other way", "t.ok")], source="recovery")


class World:
    """Tools that record when they run, so tests can see ordering and overlap."""

    def __init__(self):
        self.log, self.lock = [], threading.Lock()
        self.gui_now, self.gui_max = 0, 0
        self.attempts = {}
        self.release = threading.Event()

    def reg(self):
        reg = ToolRegistry()

        def mark(name, args):
            with self.lock:
                self.log.append((name, args.get("x", "")))
                self.attempts[name] = self.attempts.get(name, 0) + 1

        def ok(args, ctx):
            mark("ok", args)
            return ToolResult(True, {}, "ok")

        def hold(args, ctx):                      # runs until released or told to stop
            mark("hold", args)
            while not self.release.is_set():
                if ctx.extras["should_stop"]():
                    return ToolResult.failure("stopped")
                time.sleep(0.01)
            return ToolResult(True, {}, "released")

        def gui(args, ctx):
            mark("gui", args)
            with self.lock:
                self.gui_now += 1
                self.gui_max = max(self.gui_max, self.gui_now)
            time.sleep(0.15)
            with self.lock:
                self.gui_now -= 1
            return ToolResult(True, {}, "typed")

        def slow(args, ctx):                      # respects should_stop
            mark("slow", args)
            for _ in range(500):
                if ctx.extras["should_stop"]():
                    return ToolResult.failure("stopped early")
                time.sleep(0.01)
            return ToolResult(True, {}, "slow done")

        def confirm_then_ok(args, ctx):
            mark("confirm", args)
            ok_ = ctx.extras["confirm"]("Press the big button", "test")
            return ToolResult(True, {}, "pressed") if ok_ else ToolResult.failure("declined")

        def notfound(args, ctx):
            mark("notfound", args)
            return ToolResult.failure("FileNotFoundError: no such file 'report.pdf'")

        def netdown(args, ctx):
            mark("netdown", args)
            return ToolResult.failure("ConnectionError: Failed to establish a new connection")

        def badkey(args, ctx):
            mark("badkey", args)
            return ToolResult.failure("google: authentication failed — API key not valid")

        def mover(args, ctx):
            mark("mover", args)
            undo.push_undo("moved a.txt to B/", lambda: "moved back", kind="move", paths=["C:/A/a.txt", "C:/B/a.txt"])
            return ToolResult(True, {}, "moved")

        S = RiskLevel.SAFE
        for spec in (ToolSpec("t.ok", "ok", ok, OBJ, risk=S, retry_safe=True),
                     ToolSpec("t.hold", "hold", hold, OBJ, risk=S),
                     ToolSpec("t.gui", "gui", gui, OBJ, risk=S, resources=["gui"]),
                     ToolSpec("t.slow", "slow", slow, OBJ, risk=S, retry_safe=True, timeout=0.3),
                     ToolSpec("t.confirm", "confirm", confirm_then_ok, OBJ, risk=S, timeout=0.3),
                     ToolSpec("t.notfound", "nf", notfound, OBJ, risk=S, retry_safe=True),
                     ToolSpec("t.net", "net", netdown, OBJ, risk=S, retry_safe=True),
                     ToolSpec("t.badkey", "key", badkey, OBJ, risk=S, retry_safe=True),
                     ToolSpec("t.move", "move", mover, OBJ, risk=S)):
            reg.register(spec)
        return reg


def make(plans, world=None, max_parallel=3, confirmer=None, locks=None, **kw):
    world = world or World()
    store = TaskStore(persist=False)
    o = Orchestrator(world.reg(), PermissionEngine(world.reg(), policy=PermissionPolicy()), default_agents(),
                     Planner(plans), store, confirmer=confirmer or Confirmer(True), sleep=lambda s: None,
                     permission_timeout=3.0, input_timeout=3.0, kill=KillSwitch(),
                     scheduler=None, locks=locks or ResourceLocks(), stop_grace=1.0, **kw)
    o.scheduler._fixed_max = max_parallel
    return o, store, world


def until(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class PriorityTests(unittest.TestCase):
    def test_parse_and_infer(self):
        self.assertEqual(Priority.parse("high"), Priority.HIGH)
        self.assertEqual(Priority.parse(None), Priority.NORMAL)
        self.assertEqual(Priority.infer("do this right now please"), Priority.HIGH)
        self.assertEqual(Priority.infer("tidy my downloads whenever you're free"), Priority.LOW)
        self.assertEqual(Priority.infer("check disk space", origin="proactive"), Priority.BACKGROUND)
        self.assertEqual(Priority.infer("open notepad"), Priority.NORMAL)


class SchedulerTests(unittest.TestCase):
    def test_queue_order_follows_priority_then_arrival(self):
        plans = {"blocker": [Step("s1", "hold", "t.hold")], "a": [Step("s1", "a", "t.ok", args={"x": "a"})],
                 "b": [Step("s1", "b", "t.ok", args={"x": "b"})], "c": [Step("s1", "c", "t.ok", args={"x": "c"})]}
        o, store, w = make(plans, max_parallel=1)
        first = o.submit("blocker")
        self.assertTrue(until(lambda: ("hold", "") in w.log))
        low = o.submit("a", priority="low")
        normal = o.submit("b")
        high = o.submit("c", priority="high")
        for t in (low, normal, high):
            self.assertEqual(store.get(t.id).state, TaskState.QUEUED)
            self.assertIn("Queued", store.get(t.id).blocked_reason)
        self.assertEqual(o.scheduler.queued(), [high.id, normal.id, low.id])
        w.release.set()
        for t in (first, low, normal, high):
            self.assertEqual(o.wait(t.id, 5).state, TaskState.COMPLETED)
        order = [x for n, x in w.log if n == "ok"]
        self.assertEqual(order, ["c", "b", "a"])

    def test_critical_bypasses_the_limit(self):
        plans = {"blocker": [Step("s1", "hold", "t.hold")], "now": [Step("s1", "n", "t.ok", args={"x": "n"})]}
        o, store, w = make(plans, max_parallel=1)
        o.submit("blocker")
        self.assertTrue(until(lambda: ("hold", "") in w.log))
        t = o.submit("now", priority="critical")
        self.assertEqual(o.wait(t.id, 3).state, TaskState.COMPLETED)
        w.release.set()

    def test_cancel_a_queued_task_finishes_it_without_running(self):
        plans = {"blocker": [Step("s1", "hold", "t.hold")], "later": [Step("s1", "l", "t.ok", args={"x": "l"})]}
        o, store, w = make(plans, max_parallel=1)
        o.submit("blocker")
        self.assertTrue(until(lambda: ("hold", "") in w.log))
        t = o.submit("later")
        self.assertIn("had not started", o.cancel(t.id))
        self.assertEqual(store.get(t.id).state, TaskState.CANCELLED)
        w.release.set()
        time.sleep(0.2)
        self.assertNotIn(("ok", "l"), w.log)

    def test_waiting_for_the_user_frees_the_slot(self):
        plans = {"ask": [Step("s1", "confirm", "t.confirm")], "other": [Step("s1", "o", "t.ok", args={"x": "o"})]}
        o, store, w = make(plans, max_parallel=1, confirmer=Confirmer(True, delay=0.6))
        a = o.submit("ask")
        self.assertTrue(until(lambda: store.get(a.id).state == TaskState.WAITING_FOR_PERMISSION))
        b = o.submit("other")
        self.assertEqual(o.wait(b.id, 3).state, TaskState.COMPLETED)      # ran while A waited
        # A's tool has a 0.3 s timeout, but time spent waiting for the HUD doesn't count
        self.assertEqual(o.wait(a.id, 5).state, TaskState.COMPLETED)


class GuiLockTests(unittest.TestCase):
    def test_gui_steps_never_overlap_and_the_waiter_is_blocked(self):
        plans = {"g1": [Step("s1", "type 1", "t.gui", args={"x": "1"})],
                 "g2": [Step("s1", "type 2", "t.gui", args={"x": "2"})]}
        o, store, w = make(plans, max_parallel=3)
        seen = []
        store.on_transition(lambda t, old, new: seen.append((t.id, new)))
        a, b = o.submit("g1"), o.submit("g2")
        self.assertEqual(o.wait(a.id, 5).state, TaskState.COMPLETED)
        self.assertEqual(o.wait(b.id, 5).state, TaskState.COMPLETED)
        self.assertEqual(w.gui_max, 1)
        blocked = [tid for tid, st in seen if st == TaskState.BLOCKED]
        self.assertEqual(len(blocked), 1)
        waiter = store.get(blocked[0])
        self.assertTrue(any("mouse and keyboard" in h[3] for h in waiter.history))

    def test_voice_command_lease_blocks_then_expires(self):
        locks = ResourceLocks()
        self.assertTrue(locks.acquire("gui", "voice", lease=0.4))
        o, store, w = make({"g": [Step("s1", "type", "t.gui")]}, locks=locks)
        t0 = time.time()
        t = o.submit("g")
        self.assertEqual(o.wait(t.id, 5).state, TaskState.COMPLETED)
        self.assertGreaterEqual(time.time() - t0, 0.35)
        self.assertIsNone(locks.holder("gui"))

    def test_cancel_while_blocked(self):
        locks = ResourceLocks()
        locks.acquire("gui", "someone-else")
        o, store, w = make({"g": [Step("s1", "type", "t.gui")]}, locks=locks)
        t = o.submit("g")
        self.assertTrue(until(lambda: store.get(t.id).state == TaskState.BLOCKED))
        o.cancel(t.id)
        self.assertEqual(o.wait(t.id, 3).state, TaskState.CANCELLED)
        self.assertEqual(w.log, [])


class DirectVoiceCallTests(unittest.TestCase):
    def test_direct_gui_command_respects_a_task_holding_the_mouse(self):
        from core.jarvis_core import JarvisCore
        from core.tasks.scheduler import resource_locks
        core = JarvisCore(persist=False, use_llm_planner=False)
        locks = resource_locks()
        self.assertTrue(locks.acquire("gui", "task:T123"))
        try:
            msg = core.gate_direct_call("desktop_control", {"action": "wallpaper"})
            self.assertIn("task T123", msg)
            self.assertIn("did not run", msg)
        finally:
            locks.release("gui", "task:T123")
        locks.acquire("gui", "voice", lease=30)
        core.after_direct_call("desktop_control", {"action": "wallpaper"}, "Done.")
        self.assertIsNone(locks.holder("gui"))


class TimeoutAndClassificationTests(unittest.TestCase):
    def test_step_timeout_stops_the_tool_and_is_not_retried(self):
        o, store, w = make({"s": [Step("s1", "slow thing", "t.slow")]}, recovery=False)
        t = o.submit("s", background=False)
        self.assertEqual(t.state, TaskState.FAILED)
        self.assertIn("Timed out", t.steps[0].error)
        self.assertIn("took too long", t.error)
        self.assertEqual(t.steps[0].error_class, "timeout")
        self.assertEqual(w.attempts["slow"], 1)                      # retry_safe, yet not retried

    def test_not_found_is_not_retried_but_network_is(self):
        o, store, w = make({"nf": [Step("s1", "open report", "t.notfound")],
                            "net": [Step("s1", "fetch", "t.net")]}, recovery=False)
        t1 = o.submit("nf", background=False)
        t2 = o.submit("net", background=False)
        self.assertEqual((w.attempts["notfound"], w.attempts["netdown"]), (1, 3))
        self.assertEqual(t1.steps[0].error_class, "not_found")
        self.assertEqual(t2.steps[0].error_class, "network")

    def test_no_replanning_around_a_rejected_key_but_replan_uses_recovering(self):
        o, store, w = make({"key": [Step("s1", "ask ai", "t.badkey")], "nf": [Step("s1", "open", "t.notfound")]})
        t = o.submit("key", background=False)
        self.assertEqual(t.state, TaskState.FAILED)
        self.assertEqual(o.planner.recover_calls, [])
        self.assertIn("API key was rejected", o._failure_sentence(t, t.steps[0]))
        t2 = o.submit("nf", background=False)
        self.assertEqual(t2.state, TaskState.COMPLETED)
        self.assertIn(TaskState.RECOVERING.value, [h[2] for h in t2.history])

    def test_classifier_examples(self):
        cases = {
            "ConnectionError: Max retries exceeded with url": "network",
            "429 RESOURCE_EXHAUSTED retry in 30s": "rate_limit",
            "429 ... quota GenerateRequestsPerDayPerProjectPerModel-FreeTier": "quota_exhausted",
            "PermissionError: [WinError 5] Access is denied": "access_denied",
            "ModuleNotFoundError: No module named 'playwright'": "dependency",
            "The process cannot access the file because it is being used by another process": "access_denied",
            "Timed out after 120 s": "timeout",
            "missing required 'path'": "invalid_input",
            "Permission declined.": "declined",
            "weird": "unknown",
        }
        for text, kind in cases.items():
            self.assertEqual(R.classify(text).kind.value, kind, text)
        self.assertFalse(R.classify("API key not valid").replan)
        self.assertTrue(R.classify(ToolResult.failure("odd", retryable=True)).retry)


class StateAndUndoTests(unittest.TestCase):
    def test_new_states_and_transitions(self):
        s = TaskStore(persist=False)
        t = s.create("x", priority="HIGH")
        for st in (TaskState.BLOCKED, TaskState.RUNNING, TaskState.RECOVERING, TaskState.RUNNING,
                   TaskState.VERIFYING, TaskState.COMPLETED):
            s.transition(t, st)
        self.assertEqual((t.progress, t.phase, t.priority), (100, "COMPLETED", "HIGH"))
        u = s.create("y")
        self.assertEqual(u.phase, "PENDING")
        u.steps = [Step("a", "a", "t"), Step("b", "b", "t")]
        u.steps[0].status = StepStatus.DONE
        self.assertEqual(u.progress, 50)
        d = u.to_dict()
        self.assertEqual((d["priority"], d["progress"]), ("NORMAL", 50))
        self.assertEqual(Task.from_dict(t.to_dict()).priority, "HIGH")

    def test_undo_records_carry_task_and_step(self):
        got = []
        undo.set_listener(got.append)
        try:
            o, store, w = make({"mv": [Step("s1", "move a.txt", "t.move")]})
            t = o.submit("mv", background=False)
            self.assertEqual(t.state, TaskState.COMPLETED)
            rec = undo.records()[0]
            self.assertEqual((rec["task_id"], rec["step_id"], rec["tool"], rec["kind"]),
                             (t.id, "s1", "t.move", "move"))
            self.assertEqual(rec["paths"], ["C:/A/a.txt", "C:/B/a.txt"])
            self.assertEqual(got[-1]["task_id"], t.id)
            self.assertIn("moved back", undo.undo_last())
        finally:
            undo.set_listener(None)
            undo.clear()


if __name__ == "__main__":
    unittest.main()
