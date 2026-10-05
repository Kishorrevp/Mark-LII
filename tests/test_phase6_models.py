"""Phase 6 — central model config, ModelRouter.select, per-tier fallback chains,
request simplification, model health, image output.

Verification level: GENUINE EXECUTION of core/config.py, core/model_router.py,
core/model_health.py and core/ai_gateway.py. Providers are in-process fakes
that record the requests the gateway builds; the Google provider's
request/response mapping runs against a fake SDK client (google-genai is not
installable in the sandbox). Real-key behaviour is checked by
tools/model_probe.py on the user's PC.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import config as C                                          # noqa: E402
from core import secret_store                                         # noqa: E402
from core.ai_gateway import AIConfig, AIGateway, load_config          # noqa: E402
from core.model_health import ModelHealth, classify                   # noqa: E402
from core.model_router import ModelRouter                              # noqa: E402
from core.providers.base import (                                     # noqa: E402
    AIProvider, AIRequest, AIResponse, Capability, CapabilityNotSupported, Message, ProviderError,
    RateLimited, ReasoningEffort,
)
from core.providers.google_provider import GoogleProvider             # noqa: E402


class Rec(AIProvider):
    name = "google"
    capabilities = frozenset({Capability.TEXT, Capability.VISION, Capability.STRUCTURED,
                              Capability.REASONING, Capability.WEB_GROUNDING, Capability.IMAGE_OUTPUT})
    default_model = "flash"
    fast_model = "flash-lite"

    def __init__(self, script=None, image_models=("img-a",)):
        self.reqs, self.script, self.image_models = [], dict(script or {}), set(image_models)

    def generate(self, req):
        self.reqs.append(req)
        queue = self.script.get(req.model)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, AIResponse):
                item.model = req.model
                return item
        if req.response_modalities:
            if req.model in self.image_models:
                return AIResponse(text="here", images=[(b"\x89PNG-fake", "image/png")], model=req.model)
            return AIResponse(text="I can only write text.", model=req.model)
        return AIResponse(text=f"ok:{req.model}", provider=self.name, model=req.model)


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


TABLE = {
    "fast": {"model": "flash-lite"},
    "normal": {"model": "flash", "fallback": ["fast"]},
    "deep": {"model": "@normal", "effort": "high", "max_output_tokens": 8192, "fallback": ["normal", "fast"]},
    "vision": {"model": "@normal", "fallback": ["fast"]},
    "research": {"model": "@normal", "effort": "high", "grounding": True, "fallback": ["normal", "fast"]},
    "computer_use": {"model": "@normal", "fallback": ["fast"]},
    "image": {"model": "img-missing", "alternates": ["img-noquota", "img-a"]},
    "live": {"model": "models/live-x"},
}


def gw(p, health=None, **kw):
    clk = Clock()
    cfg = AIConfig(provider="google", rpm=0, max_retries=1, tiers=json.loads(json.dumps(TABLE)), **kw)
    return AIGateway(config=cfg, providers={"google": p}, sleep=clk.sleep, clock=clk,
                     health=health or ModelHealth(persist=False))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        C.set_config_dir(self.tmp)
        self._env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("JARVIS_AI_MODEL")}
        # Part One: model overrides apply to the ACTIVE provider only; pin it so a
        # .env on the machine running the tests (e.g. JARVIS_AI_PROVIDER=anthropic)
        # cannot change what these Google-table tests see.
        self._prov = os.environ.get("JARVIS_AI_PROVIDER")
        os.environ["JARVIS_AI_PROVIDER"] = "google"

    def tearDown(self):
        C.set_config_dir(None)
        os.environ.update(self._env)
        if self._prov is None:
            os.environ.pop("JARVIS_AI_PROVIDER", None)
        else:
            os.environ["JARVIS_AI_PROVIDER"] = self._prov

    def test_defaults_cover_every_tier(self):
        t = C.tier_table("google", overrides=False)
        for tier in C.TIER_NAMES:
            self.assertIn(tier, t)
        self.assertEqual(t["deep"]["fallback"], ["normal", "fast"])
        self.assertTrue(C.live_model().startswith("models/"))

    def test_file_is_merged_and_secrets_are_refused(self):
        (self.tmp / "models.json").write_text(json.dumps({
            "providers": {"google": {"tiers": {"deep": {"model": "my-deep"},
                                               "fast": {"model": "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE123"}},
                                     "api_key": "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE123"}}}), encoding="utf-8")
        t = C.tier_table("google", overrides=False)
        self.assertEqual(t["deep"]["model"], "my-deep")
        self.assertEqual(t["deep"]["effort"], "high")                 # default kept under the override
        self.assertEqual(t["fast"]["model"], "")                      # key-looking value dropped
        self.assertNotIn("api_key", C.load("models")["providers"]["google"])
        self.assertTrue(any("secret" in p for p in C.problems("models")))
        with self.assertRaises(ValueError):
            C.save("models", {"token": "sk-abcdefghijklmnopqrstu"})

    def test_env_beats_file_and_save_writes_crlf(self):
        C.save("models", {"providers": {"google": {"tiers": {"vision": {"model": "file-eyes"}}}}})
        raw = (self.tmp / "models.json").read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertEqual(C.model_for("vision"), "file-eyes")
        os.environ["JARVIS_AI_MODEL_VISION"] = "env-eyes"
        try:
            self.assertEqual(C.model_for("vision"), "env-eyes")
        finally:
            del os.environ["JARVIS_AI_MODEL_VISION"]

    def test_runtime_section(self):
        self.assertEqual(C.runtime("tasks", "max_parallel"), 3)
        (self.tmp / "runtime.json").write_text('{"tasks": {"max_parallel": 5}}', encoding="utf-8")
        self.assertEqual(C.runtime("tasks", "max_parallel"), 5)
        self.assertTrue(C.runtime("tasks", "gui_exclusive"))

    def test_load_config_uses_the_table(self):
        base = Path(tempfile.mkdtemp())
        (base / "config").mkdir()
        (base / "config" / "models.json").write_text(json.dumps(
            {"providers": {"google": {"tiers": {"normal": {"model": "g-normal"}, "fast": {"model": "g-fast"}},
                                      "rpm": 5}}}), encoding="utf-8")
        secret_store.set_base_dir(base)
        C.set_config_dir(base / "config")
        try:
            cfg = load_config()
            self.assertEqual((cfg.model, cfg.fast_model, cfg.rpm_for("google")), ("g-normal", "g-fast", 5))
            self.assertEqual(cfg.tiers["deep"]["model"], "@normal")
        finally:
            secret_store.set_base_dir(None)


class SelectTests(unittest.TestCase):
    def test_select_lanes(self):
        r = ModelRouter(TABLE)
        cases = {
            ("debug", "auto", ()): "deep", ("write", "low", ()): "fast", ("chat", "high", ()): "deep",
            ("auto", "auto", ("requires_vision",)): "vision",
            ("auto", "auto", ("requires_image_output",)): "image",
            ("auto", "auto", ("requires_computer_use",)): "computer_use",
            ("auto", "auto", ("requires_web",)): "research", ("voice", "auto", ()): "live",
        }
        for (task, cx, flags), want in cases.items():
            c = r.select(task, cx, **{f: True for f in flags})
            self.assertEqual(c.tier, want, (task, cx, flags))
        c = r.select(prompt="analyze my entire codebase for bugs")
        self.assertEqual((c.tier, c.effort, c.max_output_tokens), ("deep", "high", 8192))
        self.assertEqual([m for _, m, _ in c.chain], ["flash", "flash", "flash-lite"])
        self.assertIsNotNone(r.select(prompt="What's 12 × 12?").local_answer)
        self.assertEqual(r.select("research").grounding, True)
        self.assertEqual(r.select("voice").model, "models/live-x")

    def test_chain_skips_unusable_models(self):
        r = ModelRouter(TABLE, usable=lambda m: m != "img-missing")
        self.assertEqual([m for _, m, _ in r.select("image").chain], ["img-noquota", "img-a"])

    def test_gateway_select_uses_provider_models(self):
        g = gw(Rec())
        c = g.select("analyze")
        self.assertEqual(c.chain, [("deep", "flash", "high"), ("normal", "flash", None), ("fast", "flash-lite", None)])


class ChainTests(unittest.TestCase):
    def test_tier_provider_map_routes_using_provider_tiers(self):
        tmp = tempfile.TemporaryDirectory()
        C.set_config_dir(Path(tmp.name))
        (Path(tmp.name) / "models.json").write_text(json.dumps({
            "providers": {
                "google": {"tiers": {"fast": {"model": "google-fast"}}},
                "nvidia": {"tiers": {
                    "fast": {"model": "nvidia-fast"},
                    "normal": {"model": "nvidia-normal"},
                    "deep": {"model": "nvidia-deep", "fallback": []},
                }},
            },
        }), encoding="utf-8")
        google, nvidia = Rec(), Rec()
        cfg = AIConfig(provider="google", tier_providers={"deep": "nvidia"}, rpm=0,
                       tiers=json.loads(json.dumps(TABLE)))
        g = AIGateway(config=cfg, providers={"google": google, "nvidia": nvidia}, sleep=lambda _: None)
        try:
            g.generate("deep task", tier="deep", fallback=False)
            g.generate("fast task", tier="fast", fallback=False)
            self.assertEqual([req.model for req in nvidia.reqs], ["nvidia-deep"])
            self.assertEqual([req.model for req in google.reqs], ["flash-lite"])
        finally:
            C.set_config_dir(None)
            tmp.cleanup()

    def test_deep_timeout_falls_back_to_normal_then_answers(self):
        t_out = ProviderError("google: Deadline exceeded / timed out", retryable=True)
        p = Rec({"flash": [t_out]})
        r = gw(p).generate("x", tier="deep")
        # Phase 7: a timeout is not retried on the same model — straight to the lighter lane
        self.assertEqual([(q.model, q.reasoning) for q in p.reqs],
                         [("flash", ReasoningEffort.HIGH), ("flash", None)])
        self.assertTrue(r.meta["degraded"])
        self.assertEqual(r.meta["served_tier"], "normal")

    def test_rate_limit_skips_every_lane_on_that_model(self):
        p = Rec({"flash": [RateLimited("429 quota exceeded, retry in 90s")]})
        g = gw(p, rate_wait_max=5.0)
        r = g.generate("x", tier="deep")
        self.assertEqual([q.model for q in p.reqs], ["flash", "flash-lite"])
        self.assertEqual((r.meta["served_tier"], g.stats["degraded"]), ("fast", 1))

    def test_real_request_error_does_not_burn_quota(self):
        p = Rec({"flash": [ProviderError("400 INVALID_ARGUMENT: bad schema", status=400)]})
        with self.assertRaises(ProviderError):
            gw(p).generate("x", tier="deep")
        self.assertEqual(len(p.reqs), 1)

    def test_too_large_request_is_simplified_once(self):
        big = ProviderError("400: The input token count (2000000) exceeds the maximum number of tokens "
                            "allowed (1048576).", status=400)
        p = Rec({"flash": [big]})
        g = gw(p)
        hist = [Message.user(f"old {i}") for i in range(10)]
        r = g.generate("A" * 50000, tier="normal", history=hist, system="S" * 9000)
        second = p.reqs[1]
        self.assertTrue(r.meta["simplified"])
        self.assertEqual(len(second.messages), 5)                    # 4 history + the question
        self.assertLess(len(second.messages[-1].text()), 50000)
        self.assertIn("trimmed for length", second.messages[-1].text())
        self.assertLessEqual(len(second.system), 6100)
        self.assertEqual(g.stats["simplified"], 1)

    def test_thinking_that_eats_the_output_budget_moves_on(self):
        p = Rec({"flash": [AIResponse(text="", finish_reason="MAX_TOKENS")]})
        r = gw(p).generate("x", tier="deep")
        self.assertEqual(r.text, "ok:flash")
        self.assertEqual(p.reqs[1].reasoning, None)

    def test_missing_model_is_remembered_and_skipped(self):
        h = ModelHealth(persist=False)
        nf = ProviderError("404 NOT_FOUND: models/flash is not found for API version v1beta", status=404)
        p = Rec({"flash": [nf]})
        g = gw(p, health=h)
        r = g.generate("x", tier="normal")
        self.assertEqual(r.model, "flash-lite")
        self.assertEqual(h.get("flash")["status"], "unavailable")
        p.reqs.clear()
        g.generate("y", tier="normal")
        self.assertEqual([q.model for q in p.reqs], ["flash-lite"])     # no call wasted on 'flash'

    def test_research_tier_grounds_and_degrades_softly(self):
        p = Rec()
        gw(p).generate("latest news on X", tier="research")
        self.assertTrue(p.reqs[0].web_grounding)
        self.assertEqual(p.reqs[0].reasoning, ReasoningEffort.HIGH)

        class NoGround(Rec):
            capabilities = frozenset({Capability.TEXT, Capability.REASONING})
        q = NoGround()
        gw(q).generate("latest news on X", tier="research")
        self.assertFalse(q.reqs[0].web_grounding)
        with self.assertRaises(CapabilityNotSupported):
            gw(NoGround()).generate("x", tier="normal", web_grounding=True)

    def test_pinned_model_has_no_chain(self):
        p = Rec({"exact": [ProviderError("boom", retryable=True)] * 5})
        with self.assertRaises(ProviderError):
            gw(p).generate("x", tier="normal", model="exact")
        self.assertTrue(all(q.model == "exact" for q in p.reqs))

    def test_info_reports_tiers_chains_health(self):
        info = gw(Rec()).info()
        self.assertEqual(info["tiers"]["image"], "img-missing")
        self.assertEqual(info["tiers"]["live"], "models/live-x")
        self.assertEqual(info["chains"]["deep"], ["deep:flash+high", "normal:flash", "fast:flash-lite"])


REAL_429 = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota, please "
            "check your plan and billing details. For more information on this error, head to: https://ai.google.dev/"
            "gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. \\n* Quota "
            "exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, "
            "model: gemini-3.8-flash\\nPlease retry in 36.801232778s.', 'status': 'RESOURCE_EXHAUSTED', 'details': "
            "[{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaMetric': "
            "'generativelanguage.googleapis.com/generate_content_free_tier_requests', 'quotaId': "
            "'GenerateRequestsPerDayPerProjectPerModel-FreeTier', 'quotaDimensions': {'location': 'global', "
            "'model': 'gemini-3.8-flash'}, 'quotaValue': '20'}]}, {'@type': 'type.googleapis.com/google.rpc."
            "RetryInfo', 'retryDelay': '36s'}]}}")


class AlternatesTests(unittest.TestCase):
    """Behaviour measured on the user's real key (27 Sept 2026): the free daily
    quota is per model, gemini-3.8-flash allows 20/day, other Flash models kept
    answering. The 429 text below is the real (key-free) error body."""

    def _table(self):
        t = json.loads(json.dumps(TABLE))
        t["normal"]["alternates"] = ["flash-b", "flash-c"]
        return t

    def test_google_error_keeps_the_quota_facts(self):
        class E(Exception):
            code = 429
        err = GoogleProvider(client=object())._map_error(E(REAL_429))
        self.assertIsInstance(err, RateLimited)
        self.assertIn("limit: 20, model: gemini-3.8-flash", str(err))
        self.assertIn("PerDay", str(err))
        self.assertEqual(classify(err), "exhausted")
        self.assertLess(len(str(err)), 420)

    def test_daily_exhaustion_moves_to_the_next_free_model_and_is_remembered(self):
        class E(Exception):
            code = 429
        daily = GoogleProvider(client=object())._map_error(E(REAL_429))
        h = ModelHealth(persist=False)
        p = Rec({"flash": [daily]})
        clk = Clock()
        g = AIGateway(config=AIConfig(provider="google", rpm=0, tiers=self._table()), providers={"google": p},
                      sleep=clk.sleep, clock=clk, health=h)
        r = g.generate("x", tier="deep")
        self.assertEqual(r.model, "flash-b")
        self.assertEqual((r.meta["served_tier"], p.reqs[1].reasoning), ("deep", ReasoningEffort.HIGH))
        self.assertEqual(h.get("flash")["status"], "exhausted")
        p.reqs.clear()
        g.generate("y", tier="normal")
        self.assertEqual([q.model for q in p.reqs], ["flash-b"])          # 'flash' skipped until reset

    def test_chain_is_bounded(self):
        busy = [ProviderError("503 UNAVAILABLE", status=503, retryable=True)] * 3
        t = self._table()
        t["normal"]["alternates"] = [f"m{i}" for i in range(10)]
        p = Rec({m: list(busy) for m in ["flash", "flash-lite"] + [f"m{i}" for i in range(10)]})
        clk = Clock()
        g = AIGateway(config=AIConfig(provider="google", rpm=0, max_retries=0, tiers=t), providers={"google": p},
                      sleep=clk.sleep, clock=clk, health=ModelHealth(persist=False))
        with self.assertRaises(ProviderError):
            g.generate("x", tier="normal")
        self.assertEqual(len({q.model for q in p.reqs}), AIGateway.CHAIN_MAX_ATTEMPTS)

    def test_timeout_skips_sibling_models_of_the_same_lane(self):
        slow = ProviderError("google: timed out", retryable=True)
        p = Rec({"flash": [slow], "flash-b": [slow]})        # Phase 7: no same-model retry on a timeout
        clk = Clock()
        g = AIGateway(config=AIConfig(provider="google", rpm=0, max_retries=1, tiers=self._table()),
                      providers={"google": p}, sleep=clk.sleep, clock=clk, health=ModelHealth(persist=False))
        r = g.generate("x", tier="deep")
        # deep timed out → no other deep sibling; the lighter 'normal' lane answers
        self.assertEqual([(q.model, q.reasoning) for q in p.reqs][-1], ("flash", None))
        self.assertEqual(r.meta["served_tier"], "normal")

    def test_no_thinking_on_gemini3_means_unset(self):
        cfg = GoogleProvider(client=object())._config(AIRequest(messages=[], model="gemini-flash-lite-latest",
                                                               reasoning=ReasoningEffort.NONE))
        self.assertNotIn("thinking_config", cfg)
        cfg2 = GoogleProvider(client=object())._config(AIRequest(messages=[], model="gemini-2.5-flash",
                                                                reasoning=ReasoningEffort.NONE))
        self.assertEqual(cfg2["thinking_config"], {"thinking_budget": 0})
        cfg3 = GoogleProvider(client=object())._config(AIRequest(messages=[], model="gemini-3.7-flash",
                                                                reasoning=ReasoningEffort.HIGH))
        self.assertEqual(cfg3["thinking_config"], {"thinking_budget": 16384})

    def test_bare_invalid_argument_with_thinking_retries_without_it(self):
        bad = ProviderError("google: 400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': 'Request contains an "
                            "invalid argument.', 'status': 'INVALID_ARGUMENT'}}", status=400)
        p = Rec({"flash-lite": [bad]})
        r = gw(p).generate("x", tier="fast", reasoning="low")
        self.assertEqual(r.text, "ok:flash-lite")
        self.assertIsNone(p.reqs[1].reasoning)


class ImageTests(unittest.TestCase):
    def test_image_chain_classifies_failures_and_returns_real_bytes(self):
        h = ModelHealth(persist=False)
        p = Rec({"img-missing": [ProviderError("404 NOT_FOUND model img-missing", status=404)],
                 "img-noquota": [RateLimited("429 RESOURCE_EXHAUSTED Quota exceeded for metric "
                                             "generate_content_free_tier_requests, limit: 0, model: img-noquota")]})
        g = gw(p, health=h)
        r = g.generate_image("a blue circle", aspect_ratio="1:1")
        self.assertEqual(r.images, [(b"\x89PNG-fake", "image/png")])
        self.assertEqual([q.model for q in p.reqs], ["img-missing", "img-noquota", "img-a"])
        self.assertEqual((h.get("img-missing")["status"], h.get("img-noquota")["status"]),
                         ("unavailable", "no_quota"))
        self.assertEqual(p.reqs[-1].response_modalities, ["TEXT", "IMAGE"])
        self.assertEqual(p.reqs[-1].image_aspect, "1:1")
        p.reqs.clear()
        g.generate_image("again")
        self.assertEqual([q.model for q in p.reqs], ["img-a"])

    def test_no_image_is_an_error_never_a_fake(self):
        p = Rec(image_models=())
        with self.assertRaises(ProviderError):
            gw(p).generate_image("a cat")

        class TextOnly(Rec):
            capabilities = frozenset({Capability.TEXT})
        with self.assertRaises(CapabilityNotSupported):
            gw(TextOnly()).generate_image("a cat")

    def test_text_models_never_stand_in(self):
        p = Rec()
        g = gw(p)
        g.config.tiers["image"] = {"model": "@normal"}
        with self.assertRaises(ProviderError):
            g.generate_image("a cat")
        self.assertEqual(p.reqs, [])


class HealthTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify(ProviderError("404 NOT_FOUND", status=404)), "unavailable")
        self.assertEqual(classify(RateLimited("429 ... limit: 0, model: x")), "no_quota")
        self.assertEqual(classify(RateLimited("GenerateRequestsPerDay quota exhausted")), "exhausted")
        # Phase 7: overloaded / rate-limited models now rest briefly instead of
        # being retried on every call (10 min / 2 min)
        self.assertEqual(classify(ProviderError("503 UNAVAILABLE high demand", status=503)), "overloaded")
        self.assertEqual(classify(RateLimited("429 retry in 3s")), "rate_limited")
        self.assertEqual(classify(ProviderError("anthropic: HTTP 400 — Your credit balance is too low")), "no_quota")
        self.assertIsNone(classify(ProviderError("400 INVALID_ARGUMENT bad field", status=400)))

    def test_ttl_and_persistence(self):
        clk = [0.0]
        path = Path(tempfile.mkdtemp()) / "mh.json"
        h = ModelHealth(path=path, clock=lambda: clk[0])
        h.record_error("m", RateLimited("PerDay quota exhausted"))
        self.assertFalse(h.usable("m"))
        clk[0] = 3 * 3600 + 1
        self.assertTrue(h.usable("m"))
        raw = path.read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertNotIn(b"AIza", raw)
        self.assertEqual(ModelHealth(path=path).get("m")["status"], "exhausted")


class GoogleMappingTests(unittest.TestCase):
    def _client(self, responses, fail_image_config=False):
        calls = []

        class Models:
            def generate_content(self, **kw):
                calls.append(kw)
                if fail_image_config and "image_config" in (kw.get("config") or {}):
                    raise TypeError("unexpected keyword image_config")
                return responses.pop(0)

            def list(self):
                return [SimpleNamespace(name="models/gemini-flash-latest"),
                        SimpleNamespace(name="models/gemini-2.5-flash-image")]
        return SimpleNamespace(models=Models()), calls

    def _resp(self):
        img = SimpleNamespace(text=None, thought=False, function_call=None,
                              inline_data=SimpleNamespace(mime_type="image/png", data=b"PNGDATA"))
        txt = SimpleNamespace(text="Here you go", thought=False, function_call=None, inline_data=None)
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[txt, img]),
                                                           finish_reason="STOP")], usage_metadata=None)

    def test_image_request_and_response(self):
        client, calls = self._client([self._resp()])
        g = GoogleProvider(client=client)
        r = g.generate(AIRequest(messages=[Message.user("draw")], model="gemini-2.5-flash-image",
                                 response_modalities=["text", "image"], image_aspect="16:9"))
        self.assertEqual(calls[0]["config"]["response_modalities"], ["TEXT", "IMAGE"])
        self.assertEqual(calls[0]["config"]["image_config"], {"aspect_ratio": "16:9"})
        self.assertEqual(r.images, [(b"PNGDATA", "image/png")])
        self.assertEqual(r.text, "Here you go")
        self.assertEqual(g.list_models(), ["gemini-flash-latest", "gemini-2.5-flash-image"])

    def test_old_sdk_without_image_config(self):
        client, calls = self._client([self._resp()], fail_image_config=True)
        r = GoogleProvider(client=client).generate(AIRequest(messages=[Message.user("draw")], model="m",
                                                             response_modalities=["IMAGE"], image_aspect="1:1"))
        self.assertEqual(len(calls), 2)
        self.assertNotIn("image_config", calls[1]["config"])
        self.assertTrue(r.images)


if __name__ == "__main__":
    unittest.main()
