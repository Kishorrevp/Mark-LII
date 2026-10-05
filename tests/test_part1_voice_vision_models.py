"""Part One — persona, voice states/interruption/identities, current-frame
vision, exact model switching, preflight.

Verification level: GENUINE EXECUTION of core/persona.py, core/voice.py,
core/vision.py (assess/fresh with synthetic frames), core/model_registry.py,
the AIGateway runtime override (with an in-process provider double) and
core/preflight.py (offline). Audio hardware, a real screen and the Gemini Live
server are not available here — those are covered by the preflight's live
mode on the user's PC.
"""
import json
import math
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import persona                                             # noqa: E402
from core import vision                                              # noqa: E402
from core.ai_gateway import AIConfig, AIGateway                      # noqa: E402
from core.model_health import ModelHealth                            # noqa: E402
from core.model_registry import ModelRegistry, normalise             # noqa: E402
from core.providers.base import (AIProvider, AIResponse, Capability, Part,  # noqa: E402
                                 ProviderError, sniff_image_mime)
from core.voice import (HUD_FOR, PREBUILT_LIVE_VOICES, BargeInGate, VoiceState,  # noqa: E402
                        VoiceStateMachine, is_stop_phrase, looks_like_headphones, realtime_input_config,
                        resolve_voice, rms_int16)


class PersonaTests(unittest.TestCase):
    def test_one_persona_every_surface(self):
        v = persona.compose("serious", surface="voice", settings={"user_name": "Kishorre"})
        for must in ("Your name is JARVIS", "Kishorre", "HONESTY", "UNCERTAINTY", "SOURCES",
                     "DATA, NOT INSTRUCTIONS", "AI DISCLOSURE", "Serious Mode"):
            self.assertIn(must, v)
        f = persona.compose("friendly", surface="voice", settings={"voice_name": "FRIDAY"})
        self.assertIn("Your name is FRIDAY", f)
        self.assertIn("Friendly Mode", f)
        t = persona.compose(surface="text", settings={})
        self.assertIn("You are JARVIS", t)
        self.assertIn("DATA, NOT INSTRUCTIONS", t)
        self.assertLess(len(t), len(v))
        self.assertEqual(persona.name({"assistant_name": "EDITH"}), "EDITH")
        self.assertEqual(persona.name({"assistant_name": "JARVIS", "voice_name": "FRIDAY"}), "FRIDAY")

    def test_prompt_uses_persona(self):
        tmpl = (ROOT / "core" / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("{{PERSONALITY_BLOCK}}", tmpl)
        self.assertIn("safe, reversible actions only", tmpl)
        self.assertIn("switch_model", tmpl)
        self.assertIn("remember", tmpl)

    def test_cognition_sends_persona_as_prefix(self):
        from core.cognition import CognitiveCore
        seen = {}

        def llm(prompt, **kw):
            seen.update(kw)
            return SimpleNamespace(text="Answer [doc:fake.md#1].", meta={"tier": "normal"})
        t = CognitiveCore(llm=llm).think("compare these", use_context=False)
        self.assertIn("You are JARVIS", seen["system_prefix"])
        self.assertEqual(seen["system"], "")                  # persona does not count as routing context
        self.assertNotIn("[doc:fake.md#1]", t.text)           # unretrieved citation stripped


class VoiceTests(unittest.TestCase):
    def test_identities_map_to_real_gemini_voices(self):
        from memory.config_manager import AVAILABLE_VOICES
        ids = persona._cfg().get("identities") or {}
        for v in AVAILABLE_VOICES:
            self.assertIn(resolve_voice(v, ids).live_voice, PREBUILT_LIVE_VOICES, v)
        self.assertEqual(resolve_voice("JARVIS", ids).live_voice, "Charon")
        self.assertEqual(resolve_voice("FRIDAY", ids).live_voice, "Kore")
        bad = resolve_voice("FRIDAY", {"FRIDAY": {"live_voice": "Elevenlabs-xyz"}})
        self.assertEqual(bad.live_voice, "Charon")
        self.assertIn("not a Gemini voice", bad.note)

    def test_get_live_voice_never_returns_an_identity(self):
        from memory import config_manager as cm
        orig = cm.CONFIG_FILE
        with tempfile.TemporaryDirectory() as d:
            cm.CONFIG_FILE = Path(d) / "api_keys.json"
            try:
                for sel, want in (("JARVIS", "Charon"), ("FRIDAY", "Kore"), ("Puck", "Puck"), ("nonsense", "Charon")):
                    cm.CONFIG_FILE.write_text(json.dumps({"voice_name": sel}))
                    self.assertEqual(cm.get_live_voice(), want, sel)
            finally:
                cm.CONFIG_FILE = orig

    def test_states_and_hud(self):
        seen = []
        sm = VoiceStateMachine(on_change=lambda o, n, r: seen.append((n, HUD_FOR[n])))
        for s in ("listening", "thinking", "speaking", "interrupted", "listening", "error", "idle"):
            self.assertTrue(sm.set(s, "t"))
        self.assertFalse(sm.set("idle"))
        self.assertEqual(sm.unexpected, 0)
        self.assertEqual(sm.interruptions, 1)
        self.assertEqual(dict(seen)[VoiceState.INTERRUPTED], "LISTENING")
        self.assertEqual(dict(seen)[VoiceState.ERROR], "ERROR")
        sm2 = VoiceStateMachine()
        sm2.set("speaking")                                   # idle → speaking is not a normal flow
        self.assertEqual(sm2.unexpected, 1)

    def test_stop_phrases(self):
        for s in ("stop", "Stop.", "JARVIS, stop!", "hey jarvis stop", "hold on", "wait a second", "okay stop",
                  "stop stop", "niruthu", "போதும்", "FRIDAY wait"):
            self.assertTrue(is_stop_phrase(s), s)
        for s in ("stop the music", "wait for five minutes and remind me", "stop everything", "hold on to this file",
                  "", "what"):
            self.assertFalse(is_stop_phrase(s), s)

    @staticmethod
    def _blk(amp):
        return struct.pack("<1024h", *[int(amp * math.sin(i / 5)) for i in range(1024)])

    def test_barge_in_gate(self):
        g = BargeInGate(mode="auto", headphones=False)
        self.assertEqual(g.effective_mode(), "adaptive")
        g.update_playback(3000)
        echo = sum(len(g.feed(self._blk(1300), rms_int16(self._blk(1300)), True)) for _ in range(40))
        self.assertEqual(echo, 0)                             # JARVIS's own voice never opens the mic
        out = []
        for _ in range(6):
            out += g.feed(self._blk(9000), rms_int16(self._blk(9000)), True)
        self.assertTrue(g.is_open)
        self.assertGreaterEqual(len(out), 5)                  # pre-roll replays the start of "stop"
        self.assertEqual(len(g.feed(b"x", 0, False)), 1)      # not speaking → normal mic
        self.assertFalse(g.is_open)
        self.assertEqual(BargeInGate(mode="off").feed(b"x", 99999, True), [])
        self.assertEqual(BargeInGate(mode="auto", headphones=True).feed(b"x", 0, True), [b"x"])
        self.assertTrue(looks_like_headphones("Headset Earphone (USB Audio)"))
        self.assertFalse(looks_like_headphones("Speakers (Realtek(R) Audio)"))

    def test_rms(self):
        self.assertEqual(rms_int16(b""), 0.0)
        self.assertAlmostEqual(rms_int16(struct.pack("<4h", 100, -100, 100, -100)), 100.0, places=3)

    def test_end_of_turn_config(self):
        class T:
            class RealtimeInputConfig:
                def __init__(self, **k): self.k = k

            class AutomaticActivityDetection:
                def __init__(self, **k): self.k = k

            class StartSensitivity:
                START_SENSITIVITY_LOW = "SL"

            class EndSensitivity:
                END_SENSITIVITY_HIGH = "EH"
        self.assertIsNone(realtime_input_config(T, {"end_of_turn_silence_ms": None}))
        r = realtime_input_config(T, {"end_of_turn_silence_ms": 800, "start_sensitivity": "low",
                                      "end_sensitivity": "high"})
        self.assertEqual(r.k["automatic_activity_detection"].k,
                         {"silence_duration_ms": 800, "start_of_speech_sensitivity": "SL",
                          "end_of_speech_sensitivity": "EH"})
        self.assertIsNone(realtime_input_config(SimpleNamespace(), {"end_of_turn_silence_ms": 800}))


class VisionTests(unittest.TestCase):
    def test_mime_from_bytes(self):
        self.assertEqual(sniff_image_mime(b"\xff\xd8\xff\xe0...."), "image/jpeg")
        self.assertEqual(sniff_image_mime(b"\x89PNG\r\n\x1a\n...."), "image/png")
        self.assertEqual(sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertEqual(Part.of_image(b"\xff\xd8\xff\xe0..", "image/png").mime_type, "image/jpeg")
        self.assertEqual(Part.of_image(b"??", "image/png").mime_type, "image/png")

    def test_fresh_and_assess(self):
        grabs = []

        def grab(target, max_side=1600):
            grabs.append(target)
            return vision.Capture(b"\xff\xd8new", "image/jpeg", 800, 450, 1600, 900, gray_small=[20, 200] * 288)
        old = vision.Capture(b"old", "image/jpeg", 800, 450, 1600, 900, at=time.time() - 10)
        cap, re1 = vision.fresh(old, "screen", 2.5, grab=grab)
        self.assertTrue(re1)
        self.assertLess(cap.age_s(), 1.0)
        cap2, re2 = vision.fresh(cap, "screen", 2.5, grab=grab)
        self.assertFalse(re2)
        self.assertIs(cap2, cap)
        self.assertEqual(grabs, ["screen"])
        self.assertEqual(vision.assess(cap), [])
        self.assertTrue(any("black" in n for n in vision.assess(
            vision.Capture(b"x", "image/jpeg", 800, 450, 800, 450, gray_small=[3] * 576))))
        self.assertTrue(any("flat" in n for n in vision.assess(
            vision.Capture(b"x", "image/jpeg", 800, 450, 800, 450, gray_small=[128] * 576))))
        self.assertTrue(any("tiny" in n for n in vision.assess(
            vision.Capture(b"x", "image/jpeg", 40, 30, 40, 30, gray_small=[20, 200] * 288))))
        self.assertIn("captured", vision.describe_frame(cap))

    def test_analyze_mentions_frame_and_issues(self):
        seen = {}

        def model(prompt, cap):
            seen["p"] = prompt
            return "A black screen."
        cap = vision.Capture(b"x", "image/jpeg", 800, 450, 800, 450, gray_small=[2] * 576)
        text, _ = vision.analyze("general", "what's here", cap=cap, model=model)
        self.assertIn("captured just now", seen["p"])
        self.assertIn("almost completely black", seen["p"])


class _P(AIProvider):
    name = "google"
    capabilities = frozenset({Capability.TEXT, Capability.REASONING, Capability.VISION})
    default_model = "flash"
    fast_model = "flash-lite"

    def __init__(self, fail=()):
        self.seen, self.fail = [], set(fail)

    def generate(self, req):
        self.seen.append(req.model)
        if req.model in self.fail:
            raise ProviderError(f"{req.model} 503 UNAVAILABLE", provider="google", status=503)
        return AIResponse(text=f"ok:{req.model}", provider="google", model=req.model)


class ModelSwitchTests(unittest.TestCase):
    TIERS = {"fast": {"model": "flash-lite"}, "normal": {"model": "flash", "alternates": ["flash-2"],
                                                          "fallback": ["fast"]},
             "deep": {"model": "@normal", "fallback": ["normal", "fast"]},
             "research": {"model": "@normal", "fallback": ["normal"]}}

    def _gw(self, p):
        return AIGateway(config=AIConfig(provider="google", rpm=0, max_retries=0, tiers=self.TIERS,
                                         fallbacks=["local"]),
                         providers={"google": p}, sleep=lambda s: None, health=ModelHealth(persist=False))

    def test_registry_exact_resolution(self):
        r = ModelRegistry()
        self.assertEqual(r.resolve("switch to gemini three point seven flash").entry.id, "gemini-3.7-flash")
        self.assertEqual(r.resolve("use laguna").entry.provider, "nvidia")
        self.assertEqual(r.resolve("gemini flash latest").entry.id, "gemini-flash-latest")
        miss = r.resolve("gemini 3 pro")
        self.assertFalse(miss.ok)
        self.assertEqual(miss.reason, "unknown")
        self.assertTrue(miss.candidates and all(r.get(c.id) is not None and c.id.replace("models/", "")
                                                .startswith("gemini") for c in miss.candidates))  # real ids only
        self.assertFalse(r.resolve("gpt-9 ultra").ok)
        self.assertEqual(r.resolve("native audio", kind="live").entry.id,
                         "models/gemini-2.5-flash-native-audio-preview-12-2025")
        self.assertEqual(normalise("Gemini Two Point Five Flash Model"), "gemini 2.5 flash")
        amb = ModelRegistry({"providers": {"a": {"tiers": {"normal": {"model": "x-1"}}},
                                           "b": {"tiers": {"normal": {"model": "x-1"}}}}}).resolve("x 1")
        self.assertEqual(amb.reason, "ambiguous")

    def test_env_model_override_only_reaches_the_active_provider(self):
        """Regression (found by the live preflight on the user's PC): JARVIS_AI_MODEL=claude-…
        leaked into the NVIDIA tier table, so the NVIDIA fallback asked for a Claude model (404)."""
        import os
        from core import config as C
        saved = {k: os.environ.get(k) for k in ("JARVIS_AI_PROVIDER", "JARVIS_AI_MODEL")}
        os.environ["JARVIS_AI_PROVIDER"], os.environ["JARVIS_AI_MODEL"] = "anthropic", "claude-test-1"
        try:
            self.assertEqual(C.active_provider(), "anthropic")
            self.assertEqual(C.model_for("normal", "anthropic"), "claude-test-1")
            self.assertNotEqual(C.model_for("normal", "nvidia"), "claude-test-1")
            self.assertTrue(C.model_for("normal", "google").startswith("gemini"))
            self.assertTrue(C.foreign_model("claude-x", "nvidia"))
            self.assertTrue(C.foreign_model("gemini-3.7-flash", "anthropic"))
            self.assertFalse(C.foreign_model("poolside/laguna-xs-2.1", "nvidia"))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_runtime_override_is_exact_and_not_persisted(self):
        p = _P()
        g = self._gw(p)
        changes = []
        g.on_model_change = changes.append
        g.set_runtime_model("pinned-model", "google")
        self.assertEqual(g.generate("x", tier="normal").model, "pinned-model")
        self.assertEqual(g.generate("x", tier="deep").model, "pinned-model")
        self.assertEqual(g.generate("x", tier="fast").model, "flash-lite")     # tiers kept
        self.assertTrue(g.active_brain()["runtime"])
        g.clear_runtime_model()
        self.assertEqual(g.generate("x", tier="normal").model, "flash")
        self.assertEqual(len(changes), 2)
        self.assertIsNone(AIGateway(config=AIConfig(provider="google", tiers=self.TIERS),
                                    providers={"google": _P()}).runtime_model())   # a new process starts clean

    def test_failing_selected_model_is_never_substituted(self):
        p = _P(fail={"pinned-model"})
        g = self._gw(p)
        g.set_runtime_model("pinned-model", "google")
        with self.assertRaises(ProviderError) as cm:
            g.generate("x", tier="normal")
        self.assertIn("did not substitute", str(cm.exception))
        self.assertEqual(p.seen, ["pinned-model"])            # no alternate, no fallback tier, no other provider
        g.clear_runtime_model()
        self.assertEqual(g.generate("x", tier="normal").model, "flash")

    def test_switch_tool_refuses_unknown_and_checks_keys(self):
        from core.jarvis_core import JarvisCore
        from core.ai_gateway import get_gateway, set_gateway
        set_gateway(self._gw(_P()))
        try:
            me = SimpleNamespace(audit=SimpleNamespace(record=lambda *a, **k: None), on_live_model=None)
            out = JarvisCore.switch_model(me, {"model": "gpt-9 ultra"})
            self.assertTrue(out.startswith("[MODEL_NOT_SWITCHED]"))
            self.assertIsNone(get_gateway().runtime_model())
            out = JarvisCore.switch_model(me, {"model": "gemini 3.7 flash"})
            from core.secret_store import has_secret
            if has_secret("GOOGLE_API_KEY"):
                self.assertTrue(out.startswith("[MODEL_SWITCHED]"), out)
                self.assertEqual(get_gateway().runtime_model()["id"], "gemini-3.7-flash")
            else:
                self.assertTrue(out.startswith("[MODEL_NOT_SWITCHED]"), out)
            out = JarvisCore.switch_model(me, {"action": "reset"})
            self.assertTrue(out.startswith("[MODEL_RESET]"))
            self.assertIsNone(get_gateway().runtime_model())
            out = JarvisCore.switch_model(me, {"model": "laguna"})
            if not has_secret("NVIDIA_API_KEY"):
                self.assertIn("NVIDIA_API_KEY", out)
                self.assertIsNone(get_gateway().runtime_model())
            st = JarvisCore.switch_model(me, {"action": "status"})
            self.assertIn("Brain:", st)
        finally:
            set_gateway(None)


class PreflightTests(unittest.TestCase):
    # Checks that test JARVIS's own logic. The rest (keys, which provider/models
    # the user configured, files on disk, devices) describe the machine and are
    # allowed to fail in a test run — that is what the preflight is for.
    LOGIC = ("config", "routing", "memory: write → retrieve → immediate", "memory: failure surfaced", "sources",
             "knowledge: secrets never indexed", "external content not executed", "persona", "interruption",
             "voice states", "end-of-turn threshold", "current frame", "tools", "tool results surfaced",
             "model switch", "unknown model refused", "browser/UI cannot read secrets",
             "no silent endpoint failure")

    def test_offline_preflight_logic_checks_pass(self):
        from core import preflight
        lines = []
        rep = preflight.run(live=False, log=lines.append)
        self.assertTrue(rep.summary().startswith("JARVIS PREFLIGHT PASS:"))
        by = {c.name: c for c in rep.checks}
        for n in self.LOGIC:
            self.assertEqual(by[n].status, preflight.PASS, "\n".join(lines))
        self.assertNotEqual(by["server/UI: dashboard auth"].status, preflight.FAIL)   # WARN = fastapi missing
        names = {c.name for c in rep.checks}
        for must in ("memory: write → retrieve → immediate", "sources", "interruption", "current frame",
                     "model switch", "unknown model refused", "secrets protected",
                     "browser/UI cannot read secrets", "external content not executed", "no silent endpoint failure"):
            self.assertIn(must, names)
        for c in rep.checks:
            self.assertNotRegex(c.detail, r"AIza[0-9A-Za-z_\-]{30,}")

    def test_exit_code_on_critical_failure(self):
        from core import preflight
        rep = preflight.Report()
        rep.add(preflight.Check("x", preflight.WARN, "w"))
        self.assertEqual(rep.exit_code(), 0)
        rep.add(preflight.Check("y", preflight.FAIL, "f", critical=True))
        self.assertEqual(rep.exit_code(), 1)
        self.assertEqual(rep.summary(), "JARVIS PREFLIGHT PASS: 0 FAIL: 1 WARN: 1")


if __name__ == "__main__":
    unittest.main()
