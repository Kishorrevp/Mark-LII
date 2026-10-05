"""Phase 5 — vision helpers and the computer-use agent.

Verification level: GENUINE EXECUTION of the agent loop, safety policy,
coordinate mapping, change detection and the orchestrated computer.run tool.
Faked: the vision model (scripted JSON), the screen (synthetic captures) and
the mouse/keyboard (a recording actuator) — no real desktop is touched.
The real capture/pyautogui paths are exercised live on Windows (CHANGELOG).
"""
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import computer_agent as CA                                   # noqa: E402
from core.agents.builtin import default_agents                          # noqa: E402
from core.agents.orchestrator import Orchestrator                       # noqa: E402
from core.agents.planner import Plan                                    # noqa: E402
from core.permissions import PermissionEngine, PermissionPolicy         # noqa: E402
from core.safety import KillSwitch                                      # noqa: E402
from core.tasks.state import Step, TaskState, TaskStore                 # noqa: E402
from core.tools.builtin import build_default_registry                   # noqa: E402
from core.vision import Capture, change_ratio                           # noqa: E402


class Screen:
    """Synthetic screen: every performed action changes the pixels."""

    def __init__(self, changes=True):
        self.version, self.changes = 0, changes

    def __call__(self):
        v = self.version
        return Capture(b"img", "image/jpeg", 1600, 1000, 2560, 1600, (0, 0), "screen", "Notepad",
                       gray_small=[(v * 40 + i) % 256 for i in range(32 * 18)])


class Actuator:
    def __init__(self, screen, on_do=None):
        self.done, self.screen, self.on_do = [], screen, on_do

    def do(self, a, xy):
        self.done.append((a["type"], xy, a.get("text")))
        if self.screen.changes:
            self.screen.version += 1
        if self.on_do:
            self.on_do()
        return "ok"


def scripted(actions, verify=True):
    it = iter(actions)

    def model(prompt, cap, schema):
        if "done" in schema["properties"] and "evidence" in schema["properties"]:
            return {"done": verify, "evidence": "Saved file visible in title bar"}
        return next(it)
    return model


def agent(model, screen, **kw):
    title = kw.pop("window", "Untitled - Notepad")
    return CA.ComputerAgent(model=model, capture=screen, actuator=kw.pop("actuator", Actuator(screen)),
                            kill=kw.pop("kill", KillSwitch()), sleep=lambda s: None,
                            windows=lambda: [{"title": title, "active": True}], **kw)


class PolicyTests(unittest.TestCase):
    def test_classification(self):
        c = CA.classify
        self.assertEqual(c({"type": "type", "text": "hunter2", "target": "Password field"}, "Login")[0], "block")
        self.assertEqual(c({"type": "type", "text": "sk-abcdefghijklmnopqrstuvwx1234"}, "Notes")[0], "block")
        self.assertEqual(c({"type": "click", "target": "I'm not a robot checkbox"}, "Chrome")[0], "block")
        self.assertEqual(c({"type": "type", "text": "dir"}, "Windows PowerShell")[0], "block")
        self.assertEqual(c({"type": "hotkey", "keys": ["win", "r"]}, "Desktop")[0], "block")
        self.assertEqual(c({"type": "click", "target": "Pay now button"}, "Checkout - Chrome")[0], "confirm")
        self.assertEqual(c({"type": "click", "target": "Send"}, "Gmail")[0], "confirm")
        self.assertEqual(c({"type": "hotkey", "keys": ["alt", "f4"]}, "Word")[0], "confirm")
        self.assertEqual(c({"type": "click", "target": "Next"}, "Setup", "critical")[0], "confirm")
        self.assertEqual(c({"type": "click", "target": "Search box"}, "Chrome")[0], "allow")
        self.assertEqual(c({"type": "type", "text": "weather in Coimbatore"}, "Chrome")[0], "allow")

    def test_coordinates_and_change(self):
        cap = Capture(b"", "image/jpeg", 1600, 1000, 2560, 1600, (100, 50))
        self.assertEqual(cap.to_screen([500, 500]), (100 + 1280, 50 + 800))
        self.assertEqual(cap.to_screen([2000, -5]), (100, 50 + 1600))          # clamped
        a = Capture(b"", "", 1, 1, 1, 1, gray_small=[0] * 10)
        b = Capture(b"", "", 1, 1, 1, 1, gray_small=[0] * 5 + [200] * 5)
        self.assertEqual(change_ratio(a, a), 0.0)
        self.assertEqual(change_ratio(a, b), 0.5)


class LoopTests(unittest.TestCase):
    def test_observe_act_verify_done(self):
        screen = Screen()
        act = Actuator(screen)
        model = scripted([
            {"status": "continue", "thought": "click the text area", "risk": "safe",
             "action": {"type": "click", "target": "text area", "point": [500, 250]}},
            {"status": "continue", "thought": "type", "risk": "safe",
             "action": {"type": "type", "text": "வணக்கம் hello", "point": [500, 250]}},
            {"status": "continue", "thought": "save", "risk": "safe", "action": {"type": "hotkey", "keys": ["ctrl", "s"]}},
            {"status": "done", "thought": "saved", "message": "Typed and saved."},
        ])
        r = agent(model, screen, actuator=act).run("type hello in notepad and save")
        self.assertTrue(r.ok, r.summary)
        self.assertTrue(r.verified)
        self.assertEqual([d[0] for d in act.done], ["click", "type", "hotkey"])
        self.assertEqual(act.done[0][1], (640, 800))                    # [y=500,x=250] on 2560×1600
        self.assertEqual(r.steps[1].action.get("text_len"), len("வணக்கம் hello"))   # never the text itself
        self.assertNotIn("text", r.steps[1].action)

    def test_unverified_done_is_not_accepted(self):
        screen = Screen()
        model = scripted([{"status": "done", "thought": "?"}] * 20, verify=False)
        r = agent(model, screen, max_steps=3).run("x")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "step-limit")

    def test_important_click_needs_confirmation(self):
        step = {"status": "continue", "thought": "send", "risk": "normal",
                "action": {"type": "click", "target": "Send button", "point": [900, 900]}}
        screen = Screen()
        act = Actuator(screen)
        r = agent(scripted([step]), screen, actuator=act).run("send the email")      # no confirm hook
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "declined")
        self.assertEqual(act.done, [])
        asked = []
        screen2 = Screen()
        act2 = Actuator(screen2)
        r2 = agent(scripted([step, {"status": "done", "thought": "sent"}]), screen2, actuator=act2,
                   confirm=lambda t, d: asked.append(t) or True).run("send the email")
        self.assertTrue(r2.ok)
        self.assertEqual(len(asked), 1)
        self.assertEqual(r2.steps[0].decision, "approved")

    def test_blocked_action_never_runs(self):
        screen = Screen()
        act = Actuator(screen)
        r = agent(scripted([{"status": "continue", "thought": "", "action": {"type": "type", "text": "rm -rf"}}]),
                  screen, actuator=act, window="Windows PowerShell").run("clean up")
        self.assertEqual(r.reason, "blocked")
        self.assertEqual(act.done, [])

    def test_emergency_stop_mid_run(self):
        ks = KillSwitch()
        screen = Screen()
        act = Actuator(screen, on_do=lambda: ks.engage("F12"))
        steps = [{"status": "continue", "thought": "", "action": {"type": "scroll", "direction": "down"}}] * 5
        r = agent(scripted(steps), screen, actuator=act, kill=ks).run("scroll a lot")
        self.assertEqual(r.reason, "stopped")
        self.assertEqual(len(act.done), 1)

    def test_no_effect_stops_instead_of_guessing(self):
        screen = Screen(changes=False)
        steps = [{"status": "continue", "thought": "", "action": {"type": "click", "target": "x", "point": [1, 1]}}] * 9
        r = agent(scripted(steps), screen).run("x")
        self.assertEqual(r.reason, "no-effect")
        self.assertEqual(len(r.steps), 3)


class OrchestratedToolTests(unittest.TestCase):
    def tearDown(self):
        CA.set_agent_factory(None)

    def test_computer_run_asks_before_starting_and_passes_the_hook(self):
        screen = Screen()
        act = Actuator(screen)
        seen = {}

        def factory(**kw):
            seen.update(kw)
            return CA.ComputerAgent(model=scripted([
                {"status": "continue", "thought": "", "action": {"type": "click", "target": "Delete", "point": [5, 5]}},
                {"status": "done", "thought": "ok"}]), capture=screen, actuator=act, kill=KillSwitch(),
                sleep=lambda s: None, windows=lambda: [], confirm=kw["confirm"], progress=kw["progress"],
                should_stop=kw["should_stop"])
        CA.set_agent_factory(factory)
        reg = build_default_registry()
        asks = []

        class Conf:
            def request(self, key, title, detail, on_approve, on_deny):
                asks.append(title)
                threading.Timer(0.02, on_approve).start()

            def cancel(self, key):
                pass

        class P:
            def plan(self, request, context=""):
                return Plan(steps=[Step("s1", "use the screen", "computer.run", {"goal": request})])

        o = Orchestrator(reg, PermissionEngine(reg, policy=PermissionPolicy()), default_agents(), P(),
                         TaskStore(persist=False), confirmer=Conf(), sleep=lambda s: None, kill=KillSwitch(),
                         permission_timeout=3.0)
        t = o.submit("delete the draft in the editor", background=False)
        self.assertEqual(t.state, TaskState.COMPLETED, t.error)
        self.assertEqual(len(asks), 2)                     # 1) start controlling the PC  2) the Delete click
        self.assertIn("mouse & keyboard", asks[0])
        self.assertIn("Delete", asks[1])
        self.assertTrue(callable(seen["should_stop"]))


if __name__ == "__main__":
    unittest.main()
