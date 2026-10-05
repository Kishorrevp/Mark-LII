"""Phase 4J–4K — memory hub, learned workflows, multilingual, proactive
policy, device pairing, call-agent policy, plugin integration.

Verification level: GENUINE EXECUTION of all logic (stdlib only). Nothing
here touches a phone, a telephony service, or the network.
"""
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.calls.interfaces import (CallPolicy, CallPolicyEngine, IncomingCall,          # noqa: E402
                                   TelephonyUnavailableAgent, Urgency)
from core.devices.base import (DeviceRouter, DeviceUnavailable, LaptopDevice, PhoneDevice,  # noqa: E402
                               RemoteTask)
from core.devices.pairing import PairingError, PairingManager                          # noqa: E402
from core.multilingual import detect_language, normalize                               # noqa: E402
from core.proactive_policy import (NotificationPolicy, PolicyConfig, Priority,          # noqa: E402
                                   ProactiveRegistry, ProactiveSource)
from core.tools.builtin import build_default_registry                                  # noqa: E402
from core.workflows import MIN_REPEATS, WorkflowStore                                   # noqa: E402
from memory.memory_hub import MemoryHub, is_sensitive                                   # noqa: E402


class MultilingualTests(unittest.TestCase):
    def test_detection(self):
        self.assertEqual(detect_language("open chrome please"), "en")
        self.assertEqual(detect_language("Chrome open pannu"), "ta-latn")
        self.assertEqual(detect_language("இந்த கோப்பை திற"), "ta")
        self.assertEqual(detect_language("இந்த file open பண்ணு"), "mixed")

    def test_normalize(self):
        self.assertEqual(normalize("Chrome open pannu"), "chrome open")
        self.assertIn("open", normalize("இந்த file திற"))
        self.assertIn("search", normalize("google la thedu"))
        self.assertIn("send", normalize("Arun ku message anuppu"))


class MemoryHubTests(unittest.TestCase):
    def test_refuses_sensitive(self):
        for s in ("my wifi_password is hunter2", "OTP 482913", "card 4111 1111 1111 1111",
                  "api key AIzaSyA1234567890123456789012345678901"):
            self.assertTrue(is_sensitive(s)[0], s)
        for s in ("I prefer VS Code", "my sister's name is Kavya", "project deadline is Friday"):
            self.assertFalse(is_sensitive(s)[0], s)

    def test_remember_inspect_summarize(self):
        import memory.memory_manager as mm
        tmp = tempfile.TemporaryDirectory()
        orig = mm.MEMORY_PATH
        mm.MEMORY_PATH = Path(tmp.name) / "long_term.json"
        try:
            hub = MemoryHub(base_dir=Path(tmp.name), workflows=WorkflowStore(persist=False))
            self.assertIn("Remembered", hub.remember("editor", "VS Code", "user_preferences"))
            self.assertIn("won't store", hub.remember("bank_pin", "1234", "notes"))
            hub.short_term.add("user", "hello")
            d = hub.inspect()
            self.assertEqual(d["user_preferences"]["editor"], "VS Code")
            self.assertEqual(d["short_term_context"][0]["text"], "hello")
            self.assertIn("1 preference", hub.summarize())
            self.assertIn("Forgotten", hub.forget("editor", "preferences"))
            self.assertFalse(Path(tmp.name, "memory", "short_term.json").exists())   # never persisted
        finally:
            mm.MEMORY_PATH = orig
            tmp.cleanup()


class WorkflowTests(unittest.TestCase):
    def test_detect_suggest_save_run(self):
        now = [1_000_000.0]
        reg = build_default_registry()
        wf = WorkflowStore(persist=False, registry=reg, clock=lambda: now[0])
        seq = [("open_app", {"app_name": "VS Code"}),
               ("file_controller", {"action": "list", "path": "desktop/JARVIS"}),
               ("code_helper", {"action": "run", "file_path": "main.py"})]
        for day in range(MIN_REPEATS):
            for tool, args in seq:
                wf.observe(tool, args)
                now[0] += 30
            now[0] += 86400
        sugg = wf.suggestions()
        self.assertTrue(sugg)
        self.assertEqual(len(sugg[0]["steps"]), 3)
        self.assertIn("Would you like me to save it", sugg[0]["question"])
        # nothing saved until the user says yes
        self.assertEqual(wf.list(), [])
        offer = wf.next_offer()
        self.assertIsNotNone(offer)
        self.assertIsNone(wf.next_offer())            # offered once only
        saved = wf.save("JARVIS development", suggestion_id=offer["id"])
        self.assertEqual(saved["tools"], ["code_helper", "file_controller", "open_app"])
        self.assertEqual(wf.find("run my JARVIS development workflow")["name"], "JARVIS development")
        steps = wf.to_steps(saved)
        self.assertEqual([s.depends_on for s in steps], [[], ["s1"], ["s2"]])
        from core.agents.planner import Plan, validate_plan
        self.assertEqual(validate_plan(Plan(steps=steps), reg), [])

    def test_message_bodies_never_stored(self):
        wf = WorkflowStore(persist=False)
        wf.observe("send_message", {"receiver": "Arun", "message_text": "secret plans", "platform": "whatsapp"})
        self.assertNotIn("secret plans", json.dumps(wf._data))


class ProactivePolicyTests(unittest.TestCase):
    def test_quiet_hours_and_limits(self):
        clock = [0.0]
        night = NotificationPolicy(PolicyConfig(quiet_hours=(dtime(22, 30), dtime(7, 0))),
                                   clock=lambda: clock[0], now=lambda: datetime(2026, 1, 1, 23, 15))
        self.assertFalse(night.allow("checkin", Priority.NORMAL)[0])
        self.assertTrue(night.allow("battery", Priority.CRITICAL)[0])
        day = NotificationPolicy(PolicyConfig(quiet_hours=None, max_per_hour=2, min_gap_s=60),
                                 clock=lambda: clock[0], now=lambda: datetime(2026, 1, 1, 12, 0))
        self.assertTrue(day.try_send("a", Priority.NORMAL, lambda: None)[0])
        clock[0] += 10
        self.assertEqual(day.allow("a", Priority.NORMAL), (False, "too soon after the last notification"))
        clock[0] += 100
        day.try_send("a", Priority.NORMAL, lambda: None)
        clock[0] += 100
        self.assertEqual(day.allow("a", Priority.NORMAL)[1], "hourly notification limit reached")
        off = NotificationPolicy(PolicyConfig(quiet_hours=None, disabled_sources={"checkin"}))
        self.assertFalse(off.allow("checkin")[0])

    def test_registry(self):
        class Src(ProactiveSource):
            name = "demo"
            interval_s = 0

            def check(self):
                return "hello"
        pol = NotificationPolicy(PolicyConfig(quiet_hours=None))
        reg = ProactiveRegistry(pol)
        reg.register(Src())
        got = []
        self.assertEqual(reg.poll(lambda m, s: got.append(m), now=10), ["hello"])
        self.assertEqual(reg.poll(lambda m, s: got.append(m), now=11), [])   # min gap


class DeviceTests(unittest.TestCase):
    def test_pairing_and_signing(self):
        clock = [1000.0]
        pm = PairingManager(persist=False, clock=lambda: clock[0])
        code = pm.start_pairing()
        with self.assertRaises(PairingError):
            pm.complete_pairing("000000" if code != "000000" else "111111", "Pixel")
        creds = pm.complete_pairing(code, "Pixel")
        with self.assertRaises(PairingError):
            pm.complete_pairing(code, "Pixel")                 # single use
        body = b'{"request": "status"}'
        hdr = PairingManager.sign(creds["device_id"], creds["secret"], body, ts=int(clock[0]))
        self.assertEqual(pm.verify(hdr, body), creds["device_id"])
        with self.assertRaises(PairingError):
            pm.verify(hdr, body)                               # replay
        hdr2 = PairingManager.sign(creds["device_id"], creds["secret"], body, ts=int(clock[0]))
        with self.assertRaises(PairingError):
            pm.verify(hdr2, b'{"request": "delete everything"}')   # tampered body
        clock[0] += 500
        hdr3 = PairingManager.sign(creds["device_id"], creds["secret"], body, ts=1000)
        with self.assertRaises(PairingError):
            pm.verify(hdr3, body)                              # stale
        pm.revoke(creds["device_id"])
        with self.assertRaises(PairingError):
            pm.verify(PairingManager.sign(creds["device_id"], creds["secret"], body, ts=int(clock[0])), body)

    def test_code_expiry(self):
        clock = [0.0]
        pm = PairingManager(persist=False, clock=lambda: clock[0])
        code = pm.start_pairing()
        clock[0] += 301
        with self.assertRaises(PairingError):
            pm.complete_pairing(code, "x")

    def test_router_never_routes_to_unbuilt_phone(self):
        class FakeOrch:
            class store:
                @staticmethod
                def get(i):
                    return None

            def submit(self, request, origin="voice"):
                from core.tasks.state import Task
                t = Task(id="T1", request=request)
                t.origin = origin
                return t
        r = DeviceRouter()
        r.register(LaptopDevice(FakeOrch()))
        r.register(PhoneDevice("phone-1"))
        self.assertEqual(r.route(RemoteTask("open notepad", "phone-1")).device_id, "laptop-local")
        with self.assertRaises(DeviceUnavailable):
            r.route(RemoteTask("call mom", "laptop-local", required=["calls"]))


class CallPolicyTests(unittest.TestCase):
    def setUp(self):
        self.eng = CallPolicyEngine(CallPolicy(
            user_name="Architha", contacts={"+91 98765 43210": {"name": "Amma", "relationship": "family"},
                                            "9000000001": {"name": "Boss", "relationship": "work", "vip": True}},
            blocked={"9111111111"}))

    def test_disclosure_and_impersonation_guard(self):
        for lang in ("en", "ta", "ta-latn"):
            line = self.eng.opening_line(lang)
            self.assertIn("AI", line)
            self.assertIn("Architha", line)
        self.assertFalse(self.eng.check_utterance("Hi, this is Architha speaking")[0])
        self.assertFalse(self.eng.check_utterance("I am Architha, tell me")[0])
        self.assertTrue(self.eng.check_utterance("I'm Architha's assistant; she is busy")[0])

    def test_identify_urgency_escalation(self):
        amma = self.eng.identify(IncomingCall("9876543210"))
        self.assertTrue(amma.known)
        self.assertEqual(amma.relationship, "family")
        u = self.eng.assess_urgency("romba mukkiyam, please call back", None)
        self.assertEqual(u.level, Urgency.HIGH)
        e = self.eng.assess_urgency("there was an accident, we are at the hospital")
        self.assertEqual(e.level, Urgency.EMERGENCY)
        stranger = self.eng.identify(IncomingCall("9222222222"))
        night = datetime(2026, 1, 1, 23, 30)
        self.assertTrue(self.eng.should_escalate(stranger, e, night))                 # emergencies always
        self.assertFalse(self.eng.should_escalate(stranger, self.eng.assess_urgency("just checking"), night))
        self.assertTrue(self.eng.should_escalate(amma, self.eng.assess_urgency("hi"), night))  # family
        blocked = self.eng.identify(IncomingCall("9111111111"))
        self.assertFalse(self.eng.should_escalate(blocked, e, night))
        boss = self.eng.identify(IncomingCall("9000000001"))
        self.assertEqual(self.eng.assess_urgency("hello", boss).level, Urgency.HIGH)

    def test_no_fake_call_agent(self):
        a = TelephonyUnavailableAgent()
        self.assertFalse(a.available())
        with self.assertRaises(NotImplementedError):
            a.handle(IncomingCall("1"))


class PluginIntegrationTests(unittest.TestCase):
    def test_plugins_register_with_risk_and_gate(self):
        tmp = tempfile.TemporaryDirectory()
        pdir = Path(tmp.name)
        (pdir / "lights.py").write_text(
            'PLUGIN = {"name": "smart_lights", "description": "Toggle lights.", '
            '"parameters": {"type": "OBJECT", "properties": {"on": {"type": "BOOLEAN"}}}, '
            '"risk": "high", "permissions": ["network"]}\n'
            'LOADED = []\n'
            'def on_load(cfg): LOADED.append(cfg)\n'
            'def run(parameters): return "Lights toggled."\n'
            'def verify(parameters, text): return (False, "bridge did not acknowledge")\n', encoding="utf-8")
        (pdir / "broken.py").write_text('PLUGIN = {"name": "bad", "description": "x", "risk": "yolo"}\n'
                                        'def run(p): return ""\n', encoding="utf-8")
        from core.plugin_loader import discover_plugins
        import memory.config_manager as cm
        orig = cm.get_plugin_enabled
        import core.plugin_loader as pl
        pl.get_plugin_enabled = lambda name: True
        try:
            preg = discover_plugins(pdir, core_tool_names=set(), logger=lambda m: None)
            from core.jarvis_core import register_plugin_tools
            from core.permissions import Decision, PermissionEngine, PermissionPolicy
            reg = build_default_registry()
            self.assertEqual(register_plugin_tools(reg, preg), 1)
            spec = reg.get("smart_lights")
            self.assertEqual(spec.permissions, ["network"])
            eng = PermissionEngine(reg, policy=PermissionPolicy())
            self.assertEqual(eng.decide("smart_lights", {"on": True}).decision, Decision.CONFIRM)
            res = reg.invoke("smart_lights", {"on": True})
            self.assertTrue(res.ok)
            self.assertTrue(reg.verify("smart_lights", {}, res).failed)     # plugin's own verifier honoured
            bad = [r for r in preg.list_for_ui() if r["file"] == "broken.py"][0]
            self.assertFalse(bad["valid"])
        finally:
            pl.get_plugin_enabled = orig
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
