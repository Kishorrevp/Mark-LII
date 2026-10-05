"""Phase 7 — voice latency: per-model Live features, the voice-model ladder,
the latency meter, and the main.py wiring.

Verification level: GENUINE EXECUTION of core/live_profile.py and
core/latency.py (dependency-free). main.py needs Qt / sounddevice /
google-genai, which the sandbox lacks, so its wiring is checked STRUCTURALLY
(AST + source inspection). The real timings come from tools/latency_bench.py
and memory/latency.jsonl on the user's PC.
"""
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import live_profile as LP                                   # noqa: E402
from core.latency import LatencyMeter                                 # noqa: E402


class LiveProfileTests(unittest.TestCase):
    def test_features_follow_the_model(self):
        n = LP.features("models/gemini-2.5-flash-native-audio-preview-12-2025", {})
        self.assertEqual((n["affective"], n["proactive"], n["api_version"]), (True, True, "v1alpha"))
        f = LP.features("models/gemini-3.8-live", {"proactive_audio": True, "affective_dialog": True})
        self.assertEqual((f["affective"], f["proactive"], f["api_version"]), (False, False, "v1beta"),
                         "a feature the model does not support is never sent, even if asked")
        off = LP.features("gemini-2.5-flash-native-audio-x", {"proactive_audio": "false"})
        self.assertEqual((off["proactive"], off["affective"]), (False, True))
        self.assertEqual(LP.features("models/gemini-3.8-live", {"media_resolution": "LOW"})["media_resolution"], "low")
        self.assertIsNone(LP.features("models/gemini-3.8-live", {})["media_resolution"], "not sent by default")
        self.assertIsNone(LP.features("models/gemini-3.8-live", {"media_resolution": "ultra"})["media_resolution"])

    def test_ladder_and_unusable(self):
        self.assertEqual(LP.ladder("gemini-3.8-live", ["models/gemini-3.8-live", "gemini-2.5-x", ""]),
                         ["models/gemini-3.8-live", "models/gemini-2.5-x"])
        self.assertTrue(LP.unusable("1008 models/gemini-3.8-live is not found for API version v1beta"))
        self.assertTrue(LP.unusable("PERMISSION_DENIED"))
        self.assertFalse(LP.unusable("TimeoutError: timed out"))
        self.assertFalse(LP.unusable("proactive_audio is not supported for this model"),
                         "a rejected feature is not a reason to change the model")


class LatencyMeterTests(unittest.TestCase):
    def test_voice_turn_with_a_tool(self):
        tmp = tempfile.TemporaryDirectory()
        lines = []
        m = LatencyMeter(path=Path(tmp.name) / "latency.jsonl", log=lines.append)
        m.device_latency = 0.05
        m.mic(2000, now=10.0)
        m.mic(2500, now=11.0)          # last loud frame = speech end
        m.mic(100, now=11.3)           # quiet frames don't move it
        m.tool_call(now=11.6)
        m.tool_done("system_status", 0.4)
        m.mic(3000, now=11.9)          # echo / noise after the tool call doesn't move it either
        m.audio_in(now=12.2)
        m.played(now=12.25)
        rec = m.turn_done()
        self.assertEqual(rec["speech_to_audio"], 1.2)
        self.assertEqual(rec["speech_to_tool_call"], 0.6)
        self.assertEqual(rec["audio_to_speaker"], 0.05)
        self.assertEqual(rec["speech_to_heard"], 1.3)
        self.assertEqual(rec["tools"], [{"name": "system_status", "s": 0.4}])
        self.assertIn("speech end → first audio 1.20s", lines[0])
        raw = (Path(tmp.name) / "latency.jsonl").read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertEqual(json.loads(raw.split(b"\r\n")[0])["speech_to_heard"], 1.3)
        tmp.cleanup()

    def test_async_tool_turn_is_measured_across_the_turn_boundary(self):
        m = LatencyMeter()
        m.mic(3000, now=1.0)
        m.tool_call(now=1.6)
        self.assertIsNone(m.turn_done(now=1.61))           # the calling turn closes first (3.8-live)
        m.tool_done("system_status", 0.2)
        m.audio_in(now=3.4)
        m.played(now=3.42)
        rec = m.turn_done()
        self.assertEqual(rec["speech_to_tool_call"], 0.6)
        self.assertEqual(rec["speech_to_audio"], 2.4)

    def test_interrupted_and_empty_turns_are_not_recorded(self):
        m = LatencyMeter()
        m.mic(2000, now=1.0)
        m.audio_in(now=2.0)
        m.cancel()
        self.assertIsNone(m.turn_done())
        self.assertEqual(m.summary()["turns"], 0)

    def test_summary_medians(self):
        m = LatencyMeter()
        for se, rx in ((1.0, 1.8), (5.0, 6.0), (9.0, 9.6)):
            m.mic(900, now=se)
            m.audio_in(now=rx)
            m.played(now=rx)
            m.turn_done()
        self.assertEqual(m.summary()["median_heard_s"], 0.8)
        m.typed(now=20.0)
        m.audio_in(now=21.0)
        m.played(now=21.0)
        self.assertEqual(m.turn_done()["source"], "text")
        self.assertEqual(m.summary()["median_heard_s"], 0.8, "typed turns don't count as voice latency")


def _voice(f0, formants, sr, n, amp, seed=0):
    import numpy as np
    t = np.arange(n) / sr
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for h in range(1, 40):
        f = f0 * h
        if f > sr / 2 - 100:
            break
        g = sum(np.exp(-((f - F) / bw) ** 2) for F, bw in formants)
        x += g * np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28))
    return (x / np.max(np.abs(x)) * amp * 32767).astype(np.int16).tobytes()


JARVIS_A = [(700, 130), (1220, 150), (2600, 200)]
USER_I = [(300, 90), (2300, 180), (3000, 200)]


class EchoGuardTests(unittest.TestCase):
    def test_own_voice_is_echo_a_second_voice_is_not(self):
        from core.echo_guard import EchoGuard
        g = EchoGuard(clock=lambda: 10.0)
        self.assertFalse(g.is_echo(_voice(110, JARVIS_A, 16000, 640, 0.2)), "nothing played → it is the user")
        g.note_output(_voice(110, JARVIS_A, 24000, 24000, 0.5))
        echo = [g.is_echo(_voice(110, JARVIS_A, 16000, 640, 0.2, seed=k)) for k in range(10)]
        self.assertEqual(sum(echo), 10)
        import numpy as np
        mixed = []
        for k in range(10):
            u = np.frombuffer(_voice(230, USER_I, 16000, 640, 0.2, seed=k), dtype=np.int16).astype(np.int32)
            e = np.frombuffer(_voice(110, JARVIS_A, 16000, 640, 0.06, seed=k + 9), dtype=np.int16).astype(np.int32)
            mixed.append(g.is_echo(np.clip(u + e, -32768, 32767).astype(np.int16).tobytes()))
        self.assertEqual(sum(mixed), 0, "the user talking over JARVIS is never taken for echo")
        self.assertFalse(g.is_echo(b"\x00\x00" * 640), "silence is not removed")

    def test_barge_in_gate_measures_real_block_length_and_consults_the_guard(self):
        from core.voice import BargeInGate, rms_int16
        g = BargeInGate(mode="auto", headphones=False, min_ms=300)
        g.update_playback(3000)
        loud40 = _voice(230, USER_I, 16000, 640, 0.5)        # 40 ms blocks
        opened_after = None
        for i in range(12):
            if g.feed(loud40, rms_int16(loud40), True):
                opened_after = i + 1
                break
        self.assertEqual(opened_after, 8, "300 ms of 40 ms blocks = 8 blocks")

        class AlwaysEcho:
            def is_echo(self, b):
                return True
        g2 = BargeInGate(mode="auto", headphones=False)
        g2.update_playback(3000)
        g2.echo_guard = AlwaysEcho()
        self.assertEqual(sum(len(g2.feed(loud40, rms_int16(loud40), True)) for _ in range(30)), 0,
                         "a loud block that is our own voice never opens the mic")


class MicGateTests(unittest.TestCase):
    def test_modes(self):
        from core.mic_gate import MicGate, parse_chord
        t = [0.0]
        g = MicGate("always", clock=lambda: t[0], log=lambda m: None)
        self.assertTrue(g.allow())
        w = MicGate("wake_word", window_s=20, clock=lambda: t[0], log=lambda m: None)
        self.assertFalse(w.allow())
        w.wake()
        self.assertTrue(w.allow())
        t[0] = 15.0
        w.activity()                     # someone spoke → window extended
        t[0] = 30.0
        self.assertTrue(w.allow())
        t[0] = 60.0
        self.assertFalse(w.allow())
        self.assertEqual(w.status(), "WAKE WORD")
        p = MicGate("push_to_talk", clock=lambda: t[0], log=lambda m: None)
        self.assertFalse(p.allow())
        p.key(True)
        self.assertTrue(p.allow())
        p.key(False)
        t[0] += 0.2
        self.assertTrue(p.allow(), "short tail after release")
        t[0] += 1.0
        self.assertFalse(p.allow())
        logs = []
        f = MicGate("wake_word", log=logs.append)
        f.fallback("openwakeword missing")
        self.assertEqual(f.mode, "always")
        self.assertIn("listening normally", logs[0])
        self.assertEqual(parse_chord("ctrl+shift+F8"), [0x11, 0x10, 0x77])
        self.assertEqual(parse_chord(""), [0x77])


class MainWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "main.py").read_bytes().decode("utf-8").replace("\r\n", "\n")
        cls.tree = ast.parse(cls.src)

    def _const(self, name):
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
                return ast.literal_eval(node.value)
        self.fail(name)

    def test_audio_constants(self):
        self.assertEqual(self._const("MIC_CHUNK"), 640)            # 40 ms at 16 kHz
        self.assertEqual(self._const("PLAY_BATCH_BYTES"), 4800)    # 100 ms at 24 kHz
        self.assertIn('sd.InputStream(latency="low", **kw)', self.src)
        self.assertIn('sd.RawOutputStream(latency="low", **kw)', self.src)
        self.assertIn('audio=types.Blob(data=msg["data"], mime_type="audio/pcm;rate=16000")', self.src)

    def test_features_are_per_model_and_tools_are_lean(self):
        self.assertIn('if _feat["affective"]:', self.src)
        self.assertIn('if _feat["proactive"]:', self.src)
        self.assertNotIn('cfg["enable_affective_dialog"] = True\n            cfg["proactivity"]', self.src)
        self.assertIn('"function_declarations": self._live_declarations()', self.src)
        self.assertIn("if fc.name == _live_tools.USE_TOOL_NAME:", self.src)
        # use_tool recurses through _execute_tool → the permission gate still runs first
        i_use = self.src.index("if fc.name == _live_tools.USE_TOOL_NAME:")
        i_gate = self.src.index("_gate = self._core.gate_direct_call(name, args")
        self.assertLess(i_use, i_gate)

    def test_latency_events_are_wired(self):
        for call in ("self._lat.mic(", "self._lat.audio_in()", "self._lat.tool_call()", "self._lat.tool_done(",
                     "self._lat.turn_done()", "self._lat.played()", "self._lat.cancel()", "self._lat.typed()"):
            self.assertIn(call, self.src, call)

    def test_echo_and_mic_gate_are_wired(self):
        for s in ("self._barge.echo_guard = self._echo", "self._echo.note_output(bytes(batch))",
                  "if self._echo.is_echo(data):", "if not self._mic_gate.allow():", "self._wake.feed(data)",
                  "self._setup_mic_gate()"):
            self.assertIn(s, self.src, s)

    def test_no_turkish_network_message(self):
        self.assertNotIn("Bağlantı", self.src)


if __name__ == "__main__":
    unittest.main()
