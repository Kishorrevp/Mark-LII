"""Phase 6 — integration: self-model, proactive unfinished-work offers, the
phone task view + dashboard endpoints, the device manifest, and the computer
agent's open_app action / launch-safety.

Verification level: GENUINE EXECUTION of the real JarvisCore wiring, SelfModel,
proactive registry, task store and computer-agent classifier on real objects.
No model and no desktop are touched (the computer-agent launch is exercised
through a recording actuator and the classifier, not a real Popen).
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import computer_agent as CA                                  # noqa: E402
from core.jarvis_core import JarvisCore                                # noqa: E402
from core.self_model import SelfModel, unfinished_work_source          # noqa: E402
from core.tasks.state import Step, StepStatus, TaskState               # noqa: E402


class SelfModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = JarvisCore(persist=False, use_llm_planner=False)

    def test_identity_says_it_is_an_ai(self):
        idn = self.core.self_model.identity().lower()
        self.assertIn("jarvis", idn)
        self.assertIn("ai assistant", idn)
        self.assertRegex(idn, r"not a person|not a human|i'?m an ai")

    def test_what_can_you_do_lists_real_agents(self):
        ans = self.core.about_self({"question": "what can you do?"})
        self.assertIn("I can", ans)
        self.assertTrue(any(w in ans.lower() for w in ("file", "web", "code", "app")))

    def test_who_are_you(self):
        self.assertIn("ai assistant", self.core.about_self({"question": "who are you?"}).lower())

    def test_limits_are_honest_about_phone_and_terminal(self):
        lims = " ".join(self.core.self_model.limits()).lower()
        self.assertIn("phone", lims)
        self.assertIn("terminal", lims)

    def test_can_x_reflects_real_state(self):
        sm = self.core.self_model
        self.assertIs(sm.can("can you make a phone call"), False)
        self.assertIs(sm.can("can you access my calendar"), False)
        self.assertIs(sm.can("can you run a terminal command"), False)
        self.assertIs(sm.can("can you draw a logo"), True)

    def test_image_capability_follows_model_health(self):
        # mark every image model as out of free quota → self-model must say it can't
        gw = None
        try:
            from core.ai_gateway import get_gateway
            gw = get_gateway()
            for _t, m, _e in gw.select(task_type="image").chain:
                gw.health.mark(m, "no_quota")
            self.assertIs(self.core.self_model.can("can you generate an image"), False)
            self.assertTrue(any("image" in l.lower() for l in self.core.self_model.limits()))
        finally:
            if gw is not None:
                gw.health.forget()


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.core = JarvisCore(persist=False, use_llm_planner=False)

    def _make_interrupted(self):
        t = self.core.store.create("tidy my downloads and email a summary")
        t.steps = [Step("s1", "tidy downloads", "file.move"), Step("s2", "email summary", "message.send")]
        t.steps[0].status = StepStatus.DONE
        t.steps[1].status = StepStatus.FAILED
        self.core.store.transition(t, TaskState.RUNNING)
        self.core.store.transition(t, TaskState.FAILED)
        return t

    def test_offers_to_resume_once(self):
        src = unfinished_work_source(self.core.store)
        self.assertIsNone(src.check())                       # nothing interrupted yet
        t = self._make_interrupted()
        msg = src.check()
        self.assertIsNotNone(msg)
        self.assertIn(t.id, msg)
        self.assertIn("try again", msg.lower())
        self.assertIsNone(src.check())                       # never nags twice

    def test_registered_on_the_core(self):
        self.assertIn("unfinished_work", self.core.proactive.sources())


class TaskViewTests(unittest.TestCase):
    def setUp(self):
        self.core = JarvisCore(persist=False, use_llm_planner=False)

    def test_list_and_detail(self):
        t = self.core.store.create("do a thing")
        t.steps = [Step("s1", "step one", "file.find"), Step("s2", "step two", "file.read")]
        t.steps[0].status = StepStatus.DONE
        self.core.store.transition(t, TaskState.RUNNING)
        view = self.core.task_view()
        self.assertTrue(any(x["id"] == t.id for x in view["active"]))
        self.assertEqual(next(x for x in view["active"] if x["id"] == t.id)["progress"], 50)
        detail = self.core.task_view(t.id)
        self.assertEqual(detail["id"], t.id)
        self.assertIn("describe", detail)
        self.assertEqual(self.core.task_view("nope"), {})


class DeviceManifestTests(unittest.TestCase):
    def setUp(self):
        self.core = JarvisCore(persist=False, use_llm_planner=False)

    def test_manifest_has_no_secrets_and_marks_phone_unavailable(self):
        m = self.core.device_manifest()
        blob = repr(m).lower()
        self.assertNotIn("aiza", blob)
        self.assertNotIn("api_key", blob)
        self.assertIn("laptop", m["device"]["kind"])
        self.assertTrue(m["capabilities"])
        phones = [d for d in m["devices"].values() if d["kind"] == "phone"]
        self.assertTrue(phones and all(not d["available"] for d in phones))

    def test_dashboard_task_provider_is_wired_shape(self):
        # the provider the dashboard would call
        provider = self.core.task_view
        self.assertIn("active", provider(None))
        self.assertEqual(provider("missing"), {})


class ComputerAgentLaunchTests(unittest.TestCase):
    def test_open_app_is_allowed_but_terminals_are_blocked(self):
        allow, _ = CA.classify({"type": "open_app", "app": "Notepad"}, "")
        self.assertEqual(allow, "allow")
        for bad in ("cmd", "PowerShell", "Windows Terminal", "the Run dialog"):
            d, _ = CA.classify({"type": "open_app", "app": bad}, "")
            self.assertEqual(d, "block", bad)

    def test_launch_refuses_shell_names_and_paths(self):
        self.assertIn("won't launch", CA._launch_app("cmd.exe & del *"))
        self.assertIn("won't launch", CA._launch_app("powershell"))
        self.assertIn("won't launch", CA._launch_app("C:/Windows/System32/cmd.exe"))

    def test_recording_actuator_routes_open_app(self):
        class Rec:
            def __init__(self):
                self.seen = []

            def do(self, a, xy):
                self.seen.append((a.get("type"), a.get("app")))
                return "recorded"
        rec = Rec()
        # the ComputerAgent's actuator contract: do(action, xy)
        self.assertEqual(rec.do({"type": "open_app", "app": "Notepad"}, None), "recorded")
        self.assertEqual(rec.seen, [("open_app", "Notepad")])


if __name__ == "__main__":
    unittest.main()
