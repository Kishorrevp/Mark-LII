"""Phase 5 — model router, gateway tiers, free-tier throttle.

Level of verification (stated honestly): GENUINE EXECUTION. The router and the
gateway are dependency-free; providers are in-process fakes that record the
requests the gateway builds (model, reasoning effort, output budget), so these
tests exercise the real routing / throttling code paths end to end.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import model_router                                    # noqa: E402
from core.ai_gateway import AIConfig, AIGateway, RateGate         # noqa: E402
from core.providers.base import (                                 # noqa: E402
    AIProvider, AIResponse, Capability, ProviderError, RateLimited, ReasoningEffort,
)


class RecProvider(AIProvider):
    name = "google"
    capabilities = frozenset({Capability.TEXT, Capability.VISION, Capability.STRUCTURED,
                              Capability.REASONING})
    default_model = "flash"
    fast_model = "flash-lite"

    def __init__(self, script=None):
        self.reqs = []
        self.script = list(script or [])

    def generate(self, req):
        self.reqs.append(req)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
        return AIResponse(text=f"ok:{req.model}", provider=self.name, model=req.model)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _no_alternates():
    """Phase 6 added per-model alternates (other free Flash models) to every
    lane; these tests pin the Phase 5 lane semantics, so alternates are removed
    here. The alternates themselves are covered by test_phase6_models."""
    from core.ai_gateway import default_tiers
    t = default_tiers("google")
    for v in t.values():
        v.pop("alternates", None)
    return t


def _gw(provider, **cfg):
    base = dict(provider="google", max_retries=2, rpm=0, tiers=_no_alternates())
    base.update(cfg)
    clk = FakeClock()
    return AIGateway(config=AIConfig(**base), providers={"google": provider}, sleep=clk.sleep, clock=clk)


class RouterTests(unittest.TestCase):
    def test_examples_from_the_brief(self):
        cases = {
            "What's 25 × 42?": "local",
            "Write an email.": "normal",
            "Analyze my entire software project.": "deep",
            "Look at this screenshot.": "vision",
        }
        for q, want in cases.items():
            self.assertEqual(model_router.route(q).as_dict()["tier"], want, q)

    def test_more_routes(self):
        self.assertEqual(model_router.route("hi jarvis").tier, "fast")
        self.assertEqual(model_router.route("what time is it").tier, "fast")
        self.assertEqual(model_router.route("review my code").tier, "deep")
        self.assertEqual(model_router.route("review this email draft").tier, "normal")
        self.assertEqual(model_router.route("why does my python script crash with a KeyError").tier, "deep")
        self.assertEqual(model_router.route("x " * 4000).tier, "deep")
        self.assertEqual(model_router.route("describe", images=1).tier, "vision")
        self.assertEqual(model_router.route("என் முழு திட்டத்தை ஆய்வு செய்").tier, "deep")
        self.assertEqual(model_router.route("anything", hint="deep").effort, "high")

    def test_exact_arithmetic(self):
        s = model_router.solve_arithmetic
        self.assertEqual(s("What's 25 × 42?"), "1050")
        self.assertEqual(s("0.1+0.2"), "0.3")                 # exact, no float noise
        self.assertEqual(s("15% of 2400"), "360")
        self.assertEqual(s("subtract 3 from 10"), "7")
        self.assertEqual(s("square root of 144"), "12")
        self.assertEqual(s("what's 2^10"), "1024")
        self.assertEqual(s("12345 * 2"), "24,690")
        self.assertEqual(s("10/0"), "undefined (division by zero)")
        for not_math in ("what's the weather", "what is 5", "call 98765 43210", "9**999999",
                         "__import__('os')", "open 2 tabs"):
            self.assertIsNone(s(not_math), not_math)


class GatewayTierTests(unittest.TestCase):
    def test_auto_arithmetic_makes_no_model_call(self):
        p = RecProvider()
        gw = _gw(p)
        r = gw.generate("What's 25 × 42?", tier="auto")
        self.assertEqual(r.text, "1050")
        self.assertEqual(r.meta["tier"], "local")
        self.assertEqual(p.reqs, [])
        self.assertEqual(gw.stats["local_answers"], 1)

    def test_arithmetic_not_short_circuited_for_structured_calls(self):
        p = RecProvider()
        gw = _gw(p)
        gw.generate("2+2", tier="auto", schema={"type": "object"})
        self.assertEqual(len(p.reqs), 1)

    def test_tier_model_and_effort_selection(self):
        p = RecProvider()
        gw = _gw(p)
        gw.generate("hello", tier="fast")
        gw.generate("write an email", tier="normal")
        gw.generate("analyze my entire project", tier="auto")
        gw.generate("look", tier="vision", images=[b"\x89PNG"])
        fast, normal, deep, vision = p.reqs
        self.assertEqual((fast.model, normal.model, deep.model, vision.model),
                         ("flash-lite", "flash", "flash", "flash"))
        self.assertIsNone(normal.reasoning)
        self.assertEqual(deep.reasoning, ReasoningEffort.HIGH)
        self.assertEqual(deep.max_output_tokens, 8192)

    def test_configured_deep_and_vision_models(self):
        p = RecProvider()
        gw = _gw(p, deep_model="thinker", vision_model="eyes")
        r = gw.generate("x", tier="deep")
        gw.generate("x", tier="vision", images=[b"i"])
        self.assertEqual([q.model for q in p.reqs], ["thinker", "eyes"])
        self.assertEqual(r.meta["tier"], "deep")

    def test_deep_tier_on_provider_without_thinking_still_works(self):
        class NoThink(RecProvider):
            capabilities = frozenset({Capability.TEXT})
        p = NoThink()
        r = _gw(p).generate("analyze my entire project", tier="auto")
        self.assertEqual(r.meta["tier"], "deep")
        self.assertIsNone(p.reqs[0].reasoning)

    def test_explicit_reasoning_beats_tier_default(self):
        p = RecProvider()
        _gw(p).generate("x", tier="deep", reasoning="low")
        self.assertEqual(p.reqs[0].reasoning, ReasoningEffort.LOW)

    def test_default_tier_is_backward_compatible(self):
        p = RecProvider()
        _gw(p).generate("x")
        self.assertEqual(p.reqs[0].model, "flash")

    def test_rejected_thinking_setting_is_dropped_once(self):
        p = RecProvider([ProviderError("thinking_budget is not supported for this model", status=400)])
        r = _gw(p).generate("x", tier="deep")
        self.assertEqual(r.text, "ok:flash")
        self.assertEqual(p.reqs[0].reasoning, ReasoningEffort.HIGH)
        self.assertIsNone(p.reqs[1].reasoning)

    def test_quota_error_degrades_to_fast_model(self):
        p = RecProvider([RateLimited("quota exceeded, retry in 90s")])
        gw = _gw(p, rate_wait_max=20.0)
        r = gw.generate("write an email", tier="normal")
        self.assertTrue(r.meta["degraded"])
        self.assertEqual(r.model, "flash-lite")
        self.assertEqual(gw.stats["degraded"], 1)

    def test_overloaded_model_degrades_to_fast(self):
        busy = ProviderError("503 UNAVAILABLE high demand", provider="google", retryable=True, status=503)
        p = RecProvider([busy])
        r = _gw(p).generate("analyze my entire project", tier="auto")
        self.assertTrue(r.meta["degraded"])
        self.assertEqual(r.model, "flash-lite")
        # Phase 7: the overloaded model is not hammered with retries — one call, then the next model
        self.assertEqual([q.model for q in p.reqs], ["flash", "flash-lite"])

    def test_overloaded_single_model_is_retried(self):
        busy = ProviderError("503 UNAVAILABLE high demand", provider="google", retryable=True, status=503)
        p = RecProvider([busy])
        r = _gw(p).generate("x", model="flash")          # pinned: nothing else may answer
        self.assertEqual(r.model, "flash")
        self.assertEqual(len(p.reqs), 2)

    def test_stuck_model_on_a_short_request_rests(self):
        from core.model_health import ModelHealth
        slow = ProviderError("google: flash timed out after 30s (ReadTimeout)", provider="google", retryable=True)
        p = RecProvider([slow])
        h = ModelHealth(persist=False)
        gw = AIGateway(config=AIConfig(provider="google", rpm=0), providers={"google": p},
                       sleep=lambda s: None, health=h)
        r = gw.generate("hello", tier="normal")
        self.assertEqual(r.model, "flash-lite")
        self.assertFalse(h.usable("flash"), "a model stuck on a short request is rested")

    def test_daily_quota_is_not_retried(self):
        p = RecProvider([RateLimited("GenerateRequestsPerDay quota exhausted"),
                         RateLimited("GenerateRequestsPerDay quota exhausted")])
        gw = _gw(p)
        with self.assertRaises(RateLimited):
            gw.generate("x", tier="fast")
        self.assertEqual(len(p.reqs), 1)

    def test_short_retry_hint_is_honoured(self):
        slept = []
        clk = FakeClock()

        def sleep(s):
            slept.append(s)
            clk.sleep(s)
        p = RecProvider([RateLimited("429 RESOURCE_EXHAUSTED retryDelay: '3s'")])
        gw = AIGateway(config=AIConfig(provider="google", rpm=0), providers={"google": p},
                       sleep=sleep, clock=clk)
        r = gw.generate("x", tier="fast")
        self.assertEqual(r.text, "ok:flash-lite")
        self.assertIn(3.0, slept)

    def test_info_lists_tiers(self):
        info = _gw(RecProvider(), deep_model="thinker").info()
        self.assertEqual(info["tiers"]["deep"], "thinker")
        self.assertEqual(info["tiers"]["fast"], "flash-lite")


class RateGateTests(unittest.TestCase):
    def test_sliding_window_waits_then_admits(self):
        clk = FakeClock()
        g = RateGate(clock=clk, sleep=clk.sleep)
        for _ in range(3):
            self.assertEqual(g.acquire("google:flash", 3, 120), 0.0)
        waited = g.acquire("google:flash", 3, 120)
        self.assertGreater(waited, 59)
        self.assertEqual(g.acquire("google:flash-lite", 3, 120), 0.0)   # separate quota

    def test_refuses_instead_of_freezing(self):
        clk = FakeClock()
        g = RateGate(clock=clk, sleep=clk.sleep)
        g.acquire("k", 1, 5)
        with self.assertRaises(RateLimited) as cm:
            g.acquire("k", 1, 5)
        self.assertTrue(getattr(cm.exception, "local_gate", False))

    def test_cooldown_applies_to_everyone(self):
        clk = FakeClock()
        g = RateGate(clock=clk, sleep=clk.sleep)
        g.cooldown("k", 4)
        self.assertAlmostEqual(g.acquire("k", 0, 10), 4.0)

    def test_gateway_default_rpm_for_google(self):
        self.assertEqual(AIConfig(provider="google").rpm_for("google"), 9)
        self.assertEqual(AIConfig(provider="local").rpm_for("local"), 0)
        self.assertEqual(AIConfig(provider="google", rpm=0).rpm_for("google"), 0)


if __name__ == "__main__":
    unittest.main()
