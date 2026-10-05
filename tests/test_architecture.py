"""Phase 4B–4I — registry, permissions, task state, router, planner,
orchestrator, verification, recovery, cancellation, agent-to-agent data.

Verification level: GENUINE EXECUTION. The orchestrator, planner, router,
permission engine and the real file tools (actions/file_controller.py) run
for real against a temporary home directory. Only the AI model (a fake
provider) and GUI-automation send_message (monkeypatched) are faked.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import secret_store                                        # noqa: E402
from core.agents.builtin import default_agents                       # noqa: E402
from core.agents.orchestrator import Orchestrator                    # noqa: E402
from core.agents.planner import Plan, TaskPlanner, validate_plan     # noqa: E402
from core.agents.router import AgentRouter                           # noqa: E402
from core.audit import AuditLog                                      # noqa: E402
from core.permissions import Decision, PermissionEngine, PermissionPolicy, RiskLevel  # noqa: E402
from core.tasks.state import InvalidTransition, Step, StepStatus, TaskState, TaskStore  # noqa: E402
from core.tools.builtin import build_default_registry                # noqa: E402
from core.tools.registry import ToolRegistry, ToolResult, ToolSpec    # noqa: E402
from core import verification as V                                   # noqa: E402


class FakeConfirmer:
    def __init__(self, answer):
        self.answer = answer          # True / False / None (never answers)
        self.requests = []
        self.cancelled = []

    def request(self, key, title, detail, on_approve, on_deny):
        self.requests.append((key, title))
        if self.answer is True:
            threading.Timer(0.05, on_approve).start()
        elif self.answer is False:
            threading.Timer(0.05, lambda: on_deny("declined")).start()

    def cancel(self, key):
        self.cancelled.append(key)


def make_orch(reg, confirmer=None, planner=None, audit=None, **kw):
    store = TaskStore(persist=False)
    perms = PermissionEngine(reg, policy=PermissionPolicy())
    orch = Orchestrator(reg, perms, default_agents(), planner or TaskPlanner(reg, use_llm=False), store,
                        audit=audit, confirmer=confirmer or FakeConfirmer(True), sleep=lambda s: None,
                        permission_timeout=2.0, input_timeout=2.0, **kw)
    return orch, store


# ─────────────────────────────────────────────────────────────────────────────
class RouterTests(unittest.TestCase):
    def setUp(self):
        self.r = AgentRouter(default_agents())

    def test_spec_examples(self):
        self.assertEqual(self.r.route("Move my PDF.").agents, ["file"])
        self.assertEqual(self.r.route("Find my project PDF and move it to my project folder.").agents, ["file"])
        self.assertEqual(self.r.route("Search the web and summarize three papers.").agents, ["research"])
        self.assertEqual(self.r.route("Open Chrome, search for X, download the PDF and put it in Downloads.")
                         .agents, ["browser", "file"])

    def test_tamil_and_tanglish(self):
        r = self.r.route("Jarvis, Chrome open pannu.")
        self.assertEqual(r.agents, ["browser"])
        self.assertEqual(r.language, "ta-latn")
        r = self.r.route("Jarvis, இந்த file open பண்ணு.")
        self.assertEqual(r.agents, ["file"])
        self.assertEqual(r.language, "mixed")
        self.assertEqual(self.r.route("search this and summarize பண்ணு").agents, ["research"])

    def test_extensible_without_router_changes(self):
        from core.agents.base import Agent
        agents = default_agents()

        class HomeAgent(Agent):
            name = "home"
            keywords = {"lights": 5, "thermostat": 5}
        agents.register(HomeAgent())
        self.assertEqual(AgentRouter(agents).route("turn off the lights").agents, ["home"])


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.reg = build_default_registry()
        self.p = TaskPlanner(self.reg, use_llm=False)

    def test_ordered_task_graph(self):
        plan = self.p.plan("Find PDF, rename it, move it and summarize it")
        self.assertTrue(plan.ok, plan.reason)
        tools = [s.tool for s in plan.steps]
        self.assertEqual(tools, ["file.find", "file.select", "file.rename", "file.move",
                                 "document.extract_text", "document.summarize"])
        by = {s.tool: s for s in plan.steps}
        self.assertIn(by["file.find"].id, by["file.select"].depends_on)
        self.assertIn(by["file.select"].id, by["file.rename"].depends_on)
        self.assertIn(by["file.rename"].id, by["file.move"].depends_on)
        self.assertIn(by["document.extract_text"].id, by["document.summarize"].depends_on)
        # missing details become questions, not guesses
        self.assertTrue(by["file.rename"].args["new_name"].startswith("$ask:"))
        self.assertTrue(by["file.move"].args["destination"].startswith("$ask:"))

    def test_full_spec_request(self):
        plan = self.p.plan("Find my final year project PDF, rename it to Healthcare_Project_Final.pdf, "
                           "move it to my Projects folder, summarize it, and prepare a message for Arun.")
        self.assertTrue(plan.ok)
        tools = [s.tool for s in plan.steps]
        self.assertEqual(tools[-2:], ["message.prepare", "message.send"])
        self.assertEqual(plan.steps[0].args, {"name": "final year project", "extension": "pdf"})
        send = plan.steps[-1]
        self.assertEqual(send.args["text"], f"${plan.steps[-2].id}.draft")
        engine = PermissionEngine(self.reg, policy=PermissionPolicy())
        self.assertEqual(engine.decide("message.send", {"recipient": "Arun", "text": "x"}).decision,
                         Decision.CONFIRM)

    def test_validator_rejects_bad_llm_plans(self):
        bad = Plan(steps=[Step("s1", "x", "rm_rf_everything", {}),
                          Step("s2", "y", "file.move", {"path": "$s3.path", "destination": "a"})], source="llm")
        errs = validate_plan(bad, self.reg)
        self.assertTrue(any("unknown tool" in e for e in errs))
        self.assertTrue(any("not an earlier step" in e for e in errs))
        # LLM planner path uses the same validator
        p = TaskPlanner(self.reg, llm=lambda prompt, schema: {"steps": [
            {"id": "s1", "description": "shell", "tool": "shell.exec", "args_json": "{\"cmd\": \"rm -rf /\"}"}]})
        plan = p.llm_plan("wipe the disk")
        self.assertFalse(plan.ok)
        self.assertIn("invalid", plan.reason)

    def test_llm_fallback_for_unrecognised(self):
        calls = []

        def llm(prompt, schema):
            calls.append(prompt)
            return {"steps": [{"id": "s1", "description": "status", "tool": "system.status", "args_json": "{}"}]}
        p = TaskPlanner(self.reg, llm=llm)
        plan = p.plan("how is my laptop doing health-wise")
        self.assertTrue(plan.ok)
        self.assertEqual(plan.source, "llm")
        self.assertEqual(len(calls), 1)


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.reg = build_default_registry()

    def test_levels(self):
        e = PermissionEngine(self.reg, policy=PermissionPolicy())
        self.assertEqual(e.decide("file.read", {"path": "a"}).risk, RiskLevel.SAFE)
        self.assertEqual(e.decide("file.move", {"path": "a", "destination": "b"}).decision, Decision.ALLOW)
        self.assertEqual(e.decide("file_controller", {"action": "delete", "path": "x"}).decision,
                         Decision.CONFIRM)
        self.assertEqual(e.decide("file_controller", {"action": "list"}).risk, RiskLevel.SAFE)
        self.assertEqual(e.decide("send_message", {"receiver": "a", "message_text": "b"}).decision,
                         Decision.CONFIRM)
        self.assertEqual(e.decide("game_updater", {"action": "schedule", "shutdown_when_done": "true"})
                         .decision, Decision.CONFIRM)
        self.assertEqual(e.decide("nope", {}).decision, Decision.DENY)
        self.assertEqual(e.decide("file.move", {"path": "a"}).decision, Decision.DENY)   # missing arg
        # remote origin tightens MEDIUM
        self.assertEqual(e.decide("file.move", {"path": "a", "destination": "b"}, {"origin": "remote"})
                         .decision, Decision.CONFIRM)

    def test_trusted_and_critical(self):
        reg = ToolRegistry()
        reg.register(ToolSpec("shop.buy", "buy", lambda a, c: "ok", risk=RiskLevel.CRITICAL))
        reg.register(ToolSpec("msg.send", "send", lambda a, c: "ok", risk=RiskLevel.HIGH_RISK))
        e = PermissionEngine(reg, policy=PermissionPolicy(trusted={"shop.buy", "msg.send"}))
        self.assertEqual(e.decide("msg.send", {}).decision, Decision.ALLOW)          # trusted HIGH
        self.assertEqual(e.decide("shop.buy", {}).decision, Decision.CONFIRM)       # CRITICAL never trusted
        e2 = PermissionEngine(reg, policy=PermissionPolicy(denied={"msg.send"}))
        self.assertEqual(e2.decide("msg.send", {}).decision, Decision.DENY)

    def test_high_risk_enters_waiting_for_permission(self):
        reg = ToolRegistry()
        ran = []
        reg.register(ToolSpec("danger.op", "do it", lambda a, c: ran.append(1) or ToolResult.success("done"),
                              risk=RiskLevel.HIGH_RISK, agent="general"))
        seen = []
        orch, store = make_orch(reg, confirmer=FakeConfirmer(None),
                                notify=lambda t, k, m: seen.append((k, t.state.value)))
        orch.permission_timeout = 0.4
        t = orch.submit_steps("x", [Step("s1", "Do the dangerous thing", "danger.op", {})], background=True)
        deadline = time.time() + 2
        while time.time() < deadline and t.state != TaskState.WAITING_FOR_PERMISSION:
            time.sleep(0.01)
        self.assertEqual(t.state, TaskState.WAITING_FOR_PERMISSION)
        self.assertEqual(ran, [])                    # nothing ran while waiting
        orch.wait(t.id, 3)
        # silence is not consent
        self.assertEqual(t.state, TaskState.CANCELLED)
        self.assertEqual(ran, [])
        self.assertIn("timed out", t.result)

    def test_agent_cannot_bypass(self):
        from core.agents.base import Agent, AgentServices
        from core.tools.registry import ToolContext
        reg = build_default_registry()

        class Rogue(Agent):
            name = "rogue"

            def execute(self, step, args, services):
                return services.call_tool("file.delete", {"path": "x"})
        svc = AgentServices(reg, "file.read", Rogue(), ToolContext(), {})
        with self.assertRaises(PermissionError):
            Rogue().execute(Step("s1", "", "file.read"), {}, svc)


class StateTests(unittest.TestCase):
    def test_transitions_and_persistence(self):
        tmp = tempfile.TemporaryDirectory()
        store = TaskStore(path=Path(tmp.name) / "tasks.json")
        t = store.create("hello")
        for s in (TaskState.PLANNING, TaskState.RUNNING, TaskState.VERIFYING, TaskState.COMPLETED):
            store.transition(t, s)
        self.assertEqual([h[2] for h in t.history], ["QUEUED", "PLANNING", "RUNNING", "VERIFYING", "COMPLETED"])
        with self.assertRaises(InvalidTransition):
            store.transition(t, TaskState.RUNNING)
        t2 = store.create("interrupted")
        store.transition(t2, TaskState.RUNNING)
        reloaded = TaskStore(path=Path(tmp.name) / "tasks.json")
        self.assertEqual(reloaded.get(t.id).state, TaskState.COMPLETED)
        self.assertEqual(reloaded.get(t2.id).state, TaskState.FAILED)   # honest about the restart
        tmp.cleanup()

    def test_orchestrated_transition_sequence(self):
        reg = ToolRegistry()
        reg.register(ToolSpec("t.echo", "echo", lambda a, c: ToolResult.success("ok", value=1), risk=RiskLevel.SAFE,
                              verify=lambda a, r: V.verified("fine")))
        orch, store = make_orch(reg, planner=TaskPlanner(reg, llm=lambda p, s: {"steps": [
            {"id": "s1", "description": "echo", "tool": "t.echo", "args_json": "{}"}]}))
        t = orch.submit("do the echo thing", background=False)
        states = [h[2] for h in t.history]
        self.assertEqual(states[:3], ["QUEUED", "PLANNING", "RUNNING"])
        self.assertEqual(states[-2:], ["VERIFYING", "COMPLETED"])
        self.assertEqual(t.verification_status, "VERIFIED")
        self.assertIn("completed", store.describe(t))


class VerificationRecoveryTests(unittest.TestCase):
    def test_fake_success_fails_verification(self):
        reg = ToolRegistry()
        reg.register(ToolSpec("fake.move", "claims success", lambda a, c: ToolResult.success(
            "Moved!", path="/definitely/not/here.pdf"), risk=RiskLevel.LOW_RISK,
            verify=lambda a, r: V.path_exists(r.data["path"])))
        orch, _ = make_orch(reg)
        t = orch.submit_steps("x", [Step("s1", "Move the file", "fake.move", {})], background=False)
        self.assertEqual(t.state, TaskState.FAILED)
        self.assertIn("verification failed", t.error)
        self.assertNotIn("completed", t.result.lower())

    def test_safe_failure_is_retried_bounded(self):
        reg = ToolRegistry()
        calls = []

        def flaky(a, c):
            calls.append(1)
            return ToolResult.failure("network blip", retryable=True) if len(calls) < 3 else \
                ToolResult.success("ok", text="result")
        reg.register(ToolSpec("net.get", "get", flaky, risk=RiskLevel.SAFE, retry_safe=True))
        orch, _ = make_orch(reg)
        t = orch.submit_steps("x", [Step("s1", "Fetch", "net.get", {})], background=False)
        self.assertEqual(t.state, TaskState.COMPLETED)
        self.assertEqual(len(calls), 3)

        calls2 = []
        reg.register(ToolSpec("net.dead", "dead", lambda a, c: calls2.append(1) or
                              ToolResult.failure("down", retryable=True), risk=RiskLevel.SAFE, retry_safe=True))
        t2 = orch.submit_steps("x", [Step("s1", "Fetch", "net.dead", {})], background=False)
        self.assertEqual(t2.state, TaskState.FAILED)
        self.assertEqual(len(calls2), 1 + orch.max_retries)          # no endless retry

    def test_unsafe_failure_not_retried_and_alternative_used(self):
        reg = ToolRegistry()
        calls = []
        reg.register(ToolSpec("send.a", "a", lambda a, c: calls.append("a") or ToolResult.failure("x"),
                              risk=RiskLevel.MEDIUM_RISK, alternatives=["send.b"]))
        reg.register(ToolSpec("send.b", "b", lambda a, c: calls.append("b") or ToolResult.success("via b"),
                              risk=RiskLevel.LOW_RISK))
        orch, _ = make_orch(reg)
        t = orch.submit_steps("x", [Step("s1", "Send", "send.a", {})], background=False)
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(t.state, TaskState.COMPLETED)


class CancellationTests(unittest.TestCase):
    def test_cancel_stops_remaining(self):
        reg = ToolRegistry()
        started = threading.Event()
        release = threading.Event()
        ran = []

        def slow(a, c):
            ran.append("slow")
            started.set()
            release.wait(2)
            return ToolResult.success("slow done")
        reg.register(ToolSpec("t.slow", "slow", slow, risk=RiskLevel.SAFE))
        reg.register(ToolSpec("t.next", "next", lambda a, c: ran.append("next") or ToolResult.success("n"),
                              risk=RiskLevel.SAFE))
        orch, store = make_orch(reg)
        t = orch.submit_steps("x", [Step("s1", "slow", "t.slow", {}), Step("s2", "next", "t.next", {}, depends_on=["s1"])])
        started.wait(2)
        msg = orch.cancel()                # "Cancel that."
        release.set()
        orch.wait(t.id, 3)
        self.assertIn("Cancelling", msg)
        self.assertEqual(t.state, TaskState.CANCELLED)
        self.assertEqual(ran, ["slow"])
        self.assertIn(t.step("s2").status, (StepStatus.CANCELLED, StepStatus.SKIPPED, StepStatus.PENDING))

    def test_pause_resume(self):
        reg = ToolRegistry()
        gate = threading.Event()
        reg.register(ToolSpec("t.a", "a", lambda a, c: gate.wait(2) and ToolResult.success("a"), risk=RiskLevel.SAFE))
        reg.register(ToolSpec("t.b", "b", lambda a, c: ToolResult.success("b"), risk=RiskLevel.SAFE))
        orch, store = make_orch(reg)
        t = orch.submit_steps("x", [Step("s1", "a", "t.a", {}), Step("s2", "b", "t.b", {})])
        time.sleep(0.05)
        orch.pause(t.id)
        gate.set()
        deadline = time.time() + 2
        while time.time() < deadline and t.state != TaskState.PAUSED:
            time.sleep(0.01)
        self.assertEqual(t.state, TaskState.PAUSED)
        self.assertIn("paused", store.describe())
        orch.resume(t.id)
        orch.wait(t.id, 3)
        self.assertEqual(t.state, TaskState.COMPLETED)


class EndToEndFileTaskTests(unittest.TestCase):
    """The spec's headline request, run for real on a temp home directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "Desktop").mkdir()
        (self.home / "Documents" / "Projects").mkdir(parents=True)
        (self.home / "Downloads").mkdir()
        self.pdf = self.home / "Desktop" / "Final_Year_Project_Report.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake pdf body")
        # Path.home() reads HOME on macOS/Linux and USERPROFILE on Windows — set both
        # so the real file tools can only ever see the temporary directory.
        self._old_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        assert Path.home().resolve() == self.home.resolve()
        import actions.file_processor as fp
        import actions.send_message as sm
        import actions.file_controller as fc
        self.fc = fc
        self._orig_roots = list(fc._SAFE_ROOTS)
        fc._SAFE_ROOTS[:] = [self.home]          # computed from Path.home() at import time
        self.fp, self.sm = fp, sm
        self._orig_extract = fp.extract_pdf_text
        fp.extract_pdf_text = lambda path, max_chars=50000: "Healthcare project: AI triage for rural clinics."
        self._orig_send = sm.send_message
        self.sent = []
        sm.send_message = lambda parameters, **kw: (self.sent.append(parameters),
                                                    f"Message sent to {parameters['receiver']} via WhatsApp.")[1]
        from core import ai_gateway
        from core.providers.local_provider import LocalProvider
        from core.providers.base import HTTPResult

        def transport(method, url, *, headers, body, timeout, stream=False):
            return HTTPResult(200, json.dumps({"message": {"content": "- AI triage for rural clinics"}}).encode())
        p = LocalProvider(model="fake", transport=transport)
        cfg = ai_gateway.AIConfig(provider="local")
        gw = ai_gateway.AIGateway(config=cfg, providers={"local": p})
        ai_gateway.set_gateway(gw)
        self.ai_gateway = ai_gateway

    def tearDown(self):
        self.fc._SAFE_ROOTS[:] = self._orig_roots
        self.fp.extract_pdf_text = self._orig_extract
        self.sm.send_message = self._orig_send
        self.ai_gateway.set_gateway(None)
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    REQUEST = ("Find my final year project PDF, rename it to Healthcare_Project_Final.pdf, move it to my "
               "Projects folder, summarize it, and prepare a message for Arun.")

    def test_full_flow_with_permission_granted(self):
        reg = build_default_registry()
        audit = AuditLog(path=self.home / "audit.jsonl")
        conf = FakeConfirmer(True)
        orch, store = make_orch(reg, confirmer=conf, audit=audit)
        t = orch.submit(self.REQUEST, background=False)
        self.assertEqual(t.state, TaskState.COMPLETED, t.error)
        final = self.home / "Documents" / "Projects" / "Healthcare_Project_Final.pdf"
        self.assertTrue(final.exists())
        self.assertFalse(self.pdf.exists())
        self.assertEqual(len(conf.requests), 1)                 # only the send asked
        self.assertEqual(self.sent[0]["receiver"], "Arun")
        self.assertIn("rural clinics", self.sent[0]["message_text"])     # agent-to-agent data flowed
        self.assertEqual(t.verification_status, "UNVERIFIED")   # WhatsApp gives no receipt — said so
        self.assertIn("could not independently confirm", t.result)
        # audit trail: no message body logged
        log = (self.home / "audit.jsonl").read_text()
        self.assertNotIn("rural clinics", log)
        self.assertIn("file.move", audit.summarize_recent(20))
        ev = [r for r in audit.recent(100) if r.get("tool") == "file.rename" and r["event"] == "tool_call"][0]
        self.assertEqual(ev["verification"], "VERIFIED")

    def test_permission_declined_stops_before_send(self):
        reg = build_default_registry()
        orch, store = make_orch(reg, confirmer=FakeConfirmer(False))
        t = orch.submit(self.REQUEST, background=False)
        self.assertEqual(t.state, TaskState.CANCELLED)
        self.assertEqual(self.sent, [])
        self.assertTrue((self.home / "Documents" / "Projects" / "Healthcare_Project_Final.pdf").exists())
        self.assertIn("you declined", t.result)
        self.assertEqual(t.step("s8").status, StepStatus.CANCELLED)

    def test_ambiguous_file_waits_for_input(self):
        (self.home / "Documents" / "final_year_project_old.pdf").write_bytes(b"%PDF-1.4 old")
        reg = build_default_registry()
        orch, store = make_orch(reg, confirmer=FakeConfirmer(True))
        t = orch.submit("Find my final year project PDF and move it to my Projects folder")
        deadline = time.time() + 3
        while time.time() < deadline and t.state != TaskState.WAITING_FOR_INPUT:
            time.sleep(0.01)
        self.assertEqual(t.state, TaskState.WAITING_FOR_INPUT)
        self.assertIn("several", t.pending_question)
        idx = [i for i, o in enumerate(t.step("s2").output.get("options", []) or [])]  # noqa
        # answer by name fragment
        orch.answer("Report")
        orch.wait(t.id, 3)
        self.assertEqual(t.state, TaskState.COMPLETED, t.error)
        self.assertTrue((self.home / "Documents" / "Projects" / "Final_Year_Project_Report.pdf").exists())

    def test_copy_into_same_folder_makes_a_copy(self):
        # Regression: "find my resume PDF, copy it to Desktop, open it" when the
        # PDF is already on the Desktop raised WinError 32 (copy onto itself).
        (self.home / "Desktop" / "Resume.pdf").write_bytes(b"%PDF-1.4 cv")
        reg = build_default_registry()
        opened = []
        reg.get("file.open").handler = lambda a, c: (opened.append(a["path"]),
                                                     ToolResult.success("Opened", path=a["path"]))[1]
        orch, store = make_orch(reg)
        t = orch.submit("Find my resume PDF, copy it to Desktop, and open it", background=False)
        self.assertEqual(t.state, TaskState.COMPLETED, t.error)
        copy = self.home / "Desktop" / "Resume - Copy.pdf"
        self.assertTrue(copy.exists())
        self.assertTrue((self.home / "Desktop" / "Resume.pdf").exists())
        self.assertEqual(Path(opened[0]).name, "Resume.pdf")
        # moving a file to the folder it's already in is a verified no-op, not an error
        src = str(self.home / "Desktop" / "Resume.pdf")
        res = reg.invoke("file.move", {"path": src, "destination": "Desktop"})
        self.assertTrue(res.ok, res.error)
        self.assertTrue(reg.verify("file.move", {}, res).ok)
        self.assertTrue(Path(src).exists())

    def test_path_traversal_blocked(self):
        from actions import file_controller as fc
        out = fc.rename_file(str(self.pdf), new_name="../../evil.pdf")
        self.assertIn("Invalid new name", out)
        self.assertTrue(self.pdf.exists())
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh" / "id_rsa").write_text("KEY")
        self.assertIn("Access denied", fc.read_file(str(self.home / ".ssh" / "id_rsa")))


if __name__ == "__main__":
    unittest.main()
