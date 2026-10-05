"""Phase 7 — multi-provider switching (Google → NVIDIA → Anthropic), NVIDIA
per-model keys, provider-level skip of resting providers, and the lean Live
tool profile.

Verification level: GENUINE EXECUTION of core/ai_gateway.py,
core/providers/nvidia_keys.py, core/providers/openai_provider.py,
core/model_health.py and core/live_tools.py. Providers are in-process fakes
(network is not used); real-key behaviour is checked by
tools/provider_probe.py on the user's PC.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import secret_store                                         # noqa: E402
from core import live_tools                                           # noqa: E402
from core.ai_gateway import AIConfig, AIGateway                       # noqa: E402
from core.model_health import ModelHealth                             # noqa: E402
from core.providers import nvidia_keys                                # noqa: E402
from core.providers.base import (                                     # noqa: E402
    AIProvider, AIResponse, Capability, HTTPResult, ProviderError,
)
from core.providers.openai_provider import OpenAIProvider             # noqa: E402


class Fake(AIProvider):
    capabilities = frozenset({Capability.TEXT, Capability.STRUCTURED, Capability.REASONING,
                              Capability.VISION})

    def __init__(self, name, default="m", fast="m-fast", fail=None):
        self.name, self.default_model, self.fast_model = name, default, fast
        self.reqs, self.fail = [], dict(fail or {})

    def generate(self, req):
        self.reqs.append(req.model)
        err = self.fail.get(req.model, self.fail.get("*"))
        if err is not None:
            raise err
        return AIResponse(text=f"{self.name}:{req.model}", provider=self.name, model=req.model)


TABLE = {
    "fast": {"model": "@normal"},
    "normal": {"model": "", "fallback": ["fast"]},
    "deep": {"model": "@normal", "fallback": ["normal"]},
}


def make(providers, provider="google", fallbacks=("nvidia", "anthropic"), tier_providers=None, health=None):
    cfg = AIConfig(provider=provider, fallbacks=list(fallbacks), rpm=0, max_retries=0,
                   tier_providers=dict(tier_providers or {}), tiers=json.loads(json.dumps(TABLE)))
    return AIGateway(config=cfg, providers=providers, sleep=lambda _: None,
                     health=health or ModelHealth(persist=False))


class ProviderChainTests(unittest.TestCase):
    def test_google_first_then_nvidia_then_anthropic(self):
        g = Fake("google", "gem", "gem-lite", fail={"gem": ProviderError("503 UNAVAILABLE overloaded", status=503),
                                                    "gem-lite": ProviderError("503 UNAVAILABLE", status=503)})
        n = Fake("nvidia", "nv", "nv-fast")
        a = Fake("anthropic", "cl", "cl-fast")
        r = make({"google": g, "nvidia": n, "anthropic": a}).generate("hi", tier="normal")
        self.assertEqual(r.text, "nvidia:nv")
        self.assertEqual(r.meta["fallback_provider"], "nvidia")
        self.assertEqual(a.reqs, [])

    def test_resting_provider_is_skipped_without_a_call(self):
        h = ModelHealth(persist=False)
        no_credit = ProviderError("anthropic: HTTP 400: Your credit balance is too low", status=400)
        h.record_error("cl", no_credit)
        h.record_error("cl-fast", no_credit)
        a = Fake("anthropic", "cl", "cl-fast")
        n = Fake("nvidia", "nv", "nv-fast")
        gw = make({"anthropic": a, "nvidia": n}, provider="anthropic", fallbacks=("nvidia",), health=h)
        r = gw.generate("hi", tier="normal")
        self.assertEqual(a.reqs, [], "a provider whose every model is resting must not be called")
        self.assertEqual(r.text, "nvidia:nv")
        self.assertEqual(gw.stats.get("providers_skipped"), 1)

    def test_last_provider_is_still_tried_even_when_resting(self):
        h = ModelHealth(persist=False)
        h.record_error("nv", ProviderError("HTTP 401 unauthorized", status=401))
        h.record_error("nv-fast", ProviderError("HTTP 401 unauthorized", status=401))
        n = Fake("nvidia", "nv", "nv-fast")
        r = make({"nvidia": n}, provider="nvidia", fallbacks=(), health=h).generate("hi", tier="normal")
        self.assertEqual(r.text, "nvidia:nv")   # cooldowns expire / keys get fixed — never fail without trying

    def test_tier_routed_away_keeps_the_main_provider_in_the_chain(self):
        g = Fake("google", "gem", "gem-lite")
        n = Fake("nvidia", "nv", "nv-fast", fail={"*": ProviderError("HTTP 503 overloaded", status=503)})
        a = Fake("anthropic", "cl", "cl-fast")
        gw = make({"google": g, "nvidia": n, "anthropic": a}, tier_providers={"deep": "nvidia"})
        self.assertEqual(gw._provider_chain(gw._reconfig(gw.config, "nvidia"), gw.config, True),
                         ["nvidia", "google", "anthropic"])
        r = gw.generate("write a function", tier="deep")
        self.assertEqual(r.meta["fallback_provider"], "google")
        self.assertEqual(a.reqs, [])

    def test_pinned_runtime_model_never_falls_back(self):
        n = Fake("nvidia", "nv", "nv-fast", fail={"kimi": ProviderError("HTTP 503 overloaded", status=503)})
        g = Fake("google", "gem", "gem-lite")
        gw = make({"google": g, "nvidia": n})
        gw.set_runtime_model("kimi", "nvidia")
        with self.assertRaises(ProviderError) as cm:
            gw.generate("hi", tier="normal")
        self.assertIn("did not substitute", str(cm.exception))
        self.assertEqual(g.reqs, [])


class GoogleTimeoutTests(unittest.TestCase):
    def test_request_timeout_reaches_the_sdk_and_old_sdks_still_work(self):
        from types import SimpleNamespace
        from core.providers.base import AIRequest, Message
        from core.providers.google_provider import GoogleProvider
        calls = []
        resp = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(
            text="hi", thought=False, function_call=None, inline_data=None)]), finish_reason="STOP")],
            usage_metadata=None)

        class Models:
            def __init__(self, reject):
                self.reject = reject

            def generate_content(self, **kw):
                calls.append(kw)
                if self.reject and "http_options" in (kw.get("config") or {}):
                    raise ValueError("1 validation error: http_options extra fields not permitted")
                return resp
        g = GoogleProvider(client=SimpleNamespace(models=Models(False)))
        g.generate(AIRequest(messages=[Message.user("x")], timeout=12))
        self.assertEqual(calls[-1]["config"]["http_options"], {"timeout": 12000})
        calls.clear()
        g = GoogleProvider(client=SimpleNamespace(models=Models(True)))
        self.assertEqual(g.generate(AIRequest(messages=[Message.user("x")], timeout=12)).text, "hi")
        self.assertEqual(len(calls), 2)

    def test_sdk_timeout_becomes_a_retryable_timed_out_error(self):
        from types import SimpleNamespace
        from core.providers.base import AIRequest, Message
        from core.providers.google_provider import GoogleProvider

        class ReadTimeout(Exception):
            pass

        class Models:
            def generate_content(self, **kw):
                raise ReadTimeout("")
        g = GoogleProvider(client=SimpleNamespace(models=Models()))
        with self.assertRaises(ProviderError) as cm:
            g.generate(AIRequest(messages=[Message.user("x")], model="gemini-3.7-flash", timeout=20))
        self.assertIn("timed out", str(cm.exception))
        self.assertTrue(cm.exception.retryable)


class NvidiaKeyTests(unittest.TestCase):
    NAMES = ("NVIDIA_API_KEY", "NVIDIA_API_KEY_2", "NVIDIA_API_KEY__KIMI_K3")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "config").mkdir()
        secret_store.set_base_dir(base)
        self.saved = {k: os.environ.pop(k, None) for k in self.NAMES}
        os.environ["NVIDIA_API_KEY"] = "nvapi-general-aaaaaaaaaaaaaaaaaaaaaaaa"
        os.environ["NVIDIA_API_KEY_2"] = "nvapi-second-bbbbbbbbbbbbbbbbbbbbbbbbb"
        os.environ["NVIDIA_API_KEY__KIMI_K3"] = "nvapi-kimi-cccccccccccccccccccccccccc"

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        secret_store.set_base_dir(None)
        self.tmp.cleanup()

    def test_slug_and_order(self):
        self.assertEqual(nvidia_keys.slug("moonshotai/kimi-k3"), "KIMI_K3")
        self.assertEqual(nvidia_keys.secret_name("nvidia/nemotron-3-ultra-550b-a55b"),
                         "NVIDIA_API_KEY__NEMOTRON_3_ULTRA_550B_A55B")
        ks = nvidia_keys.keys_for("moonshotai/kimi-k3")
        self.assertEqual([k.split("-")[1] for k in ks], ["kimi", "general", "second"])
        self.assertEqual(nvidia_keys.key_for("other/model").split("-")[1], "general")

    def test_next_key_is_tried_on_401_and_the_value_never_leaks(self):
        calls = []

        def transport(method, url, *, headers, body, timeout, stream=False):
            calls.append(headers.get("authorization", ""))
            if len(calls) == 1:
                return HTTPResult(401, b'{"detail":"Unauthorized"}')
            return HTTPResult(200, json.dumps({"choices": [{"message": {"content": "OK"},
                                                            "finish_reason": "stop"}]}).encode())
        p = OpenAIProvider(api_key=nvidia_keys.key_for(""), model="moonshotai/kimi-k3", name="nvidia",
                           base_url="https://example.invalid/v1", transport=transport,
                           keys_for=nvidia_keys.keys_for)
        from core.providers.base import AIRequest, Message
        r = p.generate(AIRequest(messages=[Message.user("hi")], model="moonshotai/kimi-k3"))
        self.assertEqual(r.text, "OK")
        self.assertEqual(len(calls), 2)
        self.assertIn("nvapi-kimi", calls[0])
        self.assertIn("nvapi-general", calls[1])
        self.assertNotIn("nvapi-", secret_store.redact("key nvapi-kimi-cccccccccccccccccccccccccc failed"))


class LiveToolTests(unittest.TestCase):
    def decls(self):
        import ast
        tree = ast.parse((ROOT / "main.py").read_bytes().decode("utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "TOOL_DECLARATIONS"
                                                    for t in node.targets):
                return ast.literal_eval(node.value)
        self.fail("TOOL_DECLARATIONS not found")

    def test_lean_profile_keeps_every_tool_reachable_and_is_smaller(self):
        full = self.decls()
        lean = live_tools.select_declarations(full, "lean")
        names = {d["name"] for d in lean}
        self.assertIn("use_tool", names)
        moved = {d["name"] for d in full} - names
        use = next(d for d in lean if d["name"] == "use_tool")
        for n in moved:
            self.assertIn(n, use["description"], f"{n} unreachable in the lean profile")
        self.assertLess(live_tools.size(lean), live_tools.size(full) * 0.8)
        full_sel = live_tools.select_declarations(full, "full")
        self.assertEqual(len(full_sel), len(full) + 1)          # everything + use_tool
        self.assertEqual(full_sel[-1]["name"], "use_tool")

    def test_parse_use_tool(self):
        self.assertEqual(live_tools.parse_use_tool({"tool": "flight_finder", "args": '{"origin": "MAA"}'}),
                         ("flight_finder", {"origin": "MAA"}))
        self.assertEqual(live_tools.parse_use_tool({"tool": "x", "args": "not json"}), ("x", {}))
        self.assertEqual(live_tools.parse_use_tool({"tool": "x", "args": {"a": 1}}), ("x", {"a": 1}))


if __name__ == "__main__":
    unittest.main()
