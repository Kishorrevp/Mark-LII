"""Phase 4A — provider abstraction + secrets. Pure-stdlib unittest.

Level of verification (stated honestly):
  * secret_store, gateway, anthropic/openai/local providers: GENUINE EXECUTION
    of the real code, with the network replaced by a fake transport that
    returns the documented response shapes.
  * google provider: GENUINE EXECUTION of request/response mapping against a
    fake client object (google-genai itself is not installable in the sandbox).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import secret_store                                   # noqa: E402
from core.ai_gateway import AIConfig, AIGateway                 # noqa: E402
from core.providers.base import (                               # noqa: E402
    AIRequest, Capability, CapabilityNotSupported, HTTPResult, Message, ProviderConfigError,
    ProviderError, RateLimited, ReasoningEffort, ToolSpec,
)
from core.providers.anthropic_provider import AnthropicProvider  # noqa: E402
from core.providers.openai_provider import OpenAIProvider        # noqa: E402
from core.providers.local_provider import LocalProvider          # noqa: E402
from core.providers.google_provider import GoogleProvider, to_gemini_schema  # noqa: E402


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout, stream=False):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body,
                           "stream": stream})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(obj, status=200):
    return HTTPResult(status, json.dumps(obj).encode())


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "config").mkdir()
        secret_store.set_base_dir(base)
        self.base = base
        self._saved = {k: os.environ.pop(k, None) for k in ("GOOGLE_API_KEY", "GEMINI_API_KEY")}

    def tearDown(self):
        secret_store.set_base_dir(None)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_resolution_order_and_migration(self):
        legacy = "AIzaLEGACYKEY0000000000000000000000000"
        (self.base / "config" / "api_keys.json").write_text(json.dumps(
            {"gemini_api_key": legacy, "voice_name": "Puck"}))
        self.assertEqual(secret_store.get_secret("GOOGLE_API_KEY"), legacy)
        self.assertEqual(secret_store.secret_source("GOOGLE_API_KEY"), "legacy_json")
        msg = secret_store.migrate_legacy_secrets()
        self.assertIn("migrated", msg)
        self.assertNotIn(legacy, msg)
        cfg = json.loads((self.base / "config" / "api_keys.json").read_text())
        self.assertNotIn("gemini_api_key", cfg)
        self.assertEqual(cfg["voice_name"], "Puck")          # other settings preserved
        self.assertEqual(secret_store.secret_source("GOOGLE_API_KEY"), "dotenv")
        self.assertEqual(secret_store.get_secret("GOOGLE_API_KEY"), legacy)
        raw = (self.base / ".env").read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))  # CRLF
        os.environ["GEMINI_API_KEY"] = "AIzaENVKEY000000000000000000000000000"
        self.assertEqual(secret_store.get_secret("GOOGLE_API_KEY"), os.environ["GEMINI_API_KEY"])
        self.assertEqual(secret_store.secret_source("GOOGLE_API_KEY"), "env")
        del os.environ["GEMINI_API_KEY"]
        self.assertEqual(secret_store.migrate_legacy_secrets(), "no legacy secrets")

    def test_redaction(self):
        secret_store.save_secret("GOOGLE_API_KEY", "AIzaSAVEDKEY11111111111111111111111")
        s = secret_store.redact("error: key AIzaSAVEDKEY11111111111111111111111 bad; "
                                "sk-ant-abcdefghijklmnop password=hunter22 Bearer abcdefghijklmnop")
        self.assertNotIn("AIzaSAVEDKEY", s)
        self.assertNotIn("sk-ant-abc", s)
        self.assertNotIn("hunter22", s)
        self.assertNotIn("abcdefghijklmnop", s)
        self.assertTrue(secret_store.looks_like_secret("my api_key: 12345678abc"))
        self.assertFalse(secret_store.looks_like_secret("my favourite editor is VS Code"))
        d = secret_store.describe()
        # a safe "set (...)" summary — the exact wording depends on the platform
        # (plain .env vs DPAPI-encrypted vault on Windows); never the raw key.
        self.assertTrue(d["GOOGLE_API_KEY"].startswith("set ("), d["GOOGLE_API_KEY"])
        self.assertNotIn("AIza", json.dumps(d))


class AnthropicTests(unittest.TestCase):
    def test_text_tools_structured_vision(self):
        t = FakeTransport(ok({"content": [{"type": "text", "text": "Hello"},
                                          {"type": "tool_use", "id": "tu1", "name": "move_file",
                                           "input": {"src": "a"}}],
                              "usage": {"input_tokens": 10, "output_tokens": 3},
                              "stop_reason": "tool_use", "model": "m"}),
                          ok({"content": [{"type": "tool_use", "id": "x", "name": "emit_structured_result",
                                           "input": {"steps": [1, 2]}}]}))
        p = AnthropicProvider(api_key="sk-ant-test", model="claude-test", transport=t)
        r = p.generate(AIRequest(messages=[Message.user("hi")], system="sys",
                                 tools=[ToolSpec("move_file", "Move", {"type": "object", "properties": {}})]))
        self.assertEqual(r.text, "Hello")
        self.assertEqual(r.tool_calls[0].name, "move_file")
        self.assertEqual(r.usage.total_tokens, 13)
        body = t.calls[0]["body"]
        self.assertEqual(body["system"], "sys")
        self.assertEqual(t.calls[0]["headers"]["anthropic-version"], "2023-06-01")
        r2 = p.generate(AIRequest(messages=[Message.user("plan")], response_schema={"type": "object"}))
        self.assertEqual(r2.data, {"steps": [1, 2]})
        self.assertEqual(t.calls[1]["body"]["tool_choice"]["name"], "emit_structured_result")
        # vision request body shape
        from core.providers.base import Part
        b = p.build_body(AIRequest(messages=[Message("user", [Part.of_image(b"\x89PNG", "image/png"),
                                                              Part.of_text("what")])],
                                   reasoning=ReasoningEffort.HIGH))
        self.assertEqual(b["messages"][0]["content"][0]["type"], "image")
        self.assertEqual(b["thinking"]["type"], "enabled")
        self.assertGreater(b["max_tokens"], b["thinking"]["budget_tokens"])

    def test_capability_and_errors(self):
        p = AnthropicProvider(api_key="k", model="m", transport=FakeTransport(ok({})))
        with self.assertRaises(CapabilityNotSupported):
            p.generate(AIRequest(messages=[Message.user("x")], web_grounding=True))
        p2 = AnthropicProvider(api_key="k", model="m",
                               transport=FakeTransport(HTTPResult(429, b'{"error":"slow down"}')))
        with self.assertRaises(RateLimited):
            p2.generate(AIRequest(messages=[Message.user("x")]))
        with self.assertRaises(ProviderConfigError):
            AnthropicProvider(api_key="", model="m")
        with self.assertRaises(ProviderConfigError):
            AnthropicProvider(api_key="k", model="")

    def test_stream(self):
        lines = iter(['event: x', 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"He"}}',
                      'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"llo"}}'])
        p = AnthropicProvider(api_key="k", model="m", transport=FakeTransport(lines))
        self.assertEqual("".join(p.stream(AIRequest(messages=[Message.user("x")]))), "Hello")


class OpenAITests(unittest.TestCase):
    def test_generate_structured_tools(self):
        t = FakeTransport(ok({"choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}),
                          ok({"choices": [{"message": {"content": None, "tool_calls": [
                              {"id": "c1", "type": "function",
                               "function": {"name": "open_app", "arguments": '{"app_name":"chrome"}'}}]}}]}))
        p = OpenAIProvider(api_key="sk-test", model="gpt-test", transport=t)
        r = p.generate(AIRequest(messages=[Message.user("x")], response_schema={"type": "object"},
                                 reasoning=ReasoningEffort.LOW))
        self.assertEqual(r.data, {"a": 1})
        self.assertEqual(t.calls[0]["body"]["reasoning_effort"], "low")
        self.assertEqual(t.calls[0]["headers"]["authorization"], "Bearer sk-test")
        r2 = p.generate(AIRequest(messages=[Message.user("x")], tools=[ToolSpec("open_app", "d")]))
        self.assertEqual(r2.tool_calls[0].arguments, {"app_name": "chrome"})

    def test_stream_and_audio_capability(self):
        lines = iter(['data: {"choices":[{"delta":{"content":"a"}}]}', 'data: {"choices":[{"delta":{"content":"b"}}]}',
                      'data: [DONE]'])
        p = OpenAIProvider(api_key="k", model="m", transport=FakeTransport(lines))
        self.assertEqual("".join(p.stream(AIRequest(messages=[Message.user("x")]))), "ab")
        from core.providers.base import Part
        with self.assertRaises(CapabilityNotSupported):
            p.check(AIRequest(messages=[Message("user", [Part.of_audio(b"x")])]))


class LocalTests(unittest.TestCase):
    def test_ollama_shapes(self):
        t = FakeTransport(ok({"model": "qwen3", "message": {"role": "assistant", "content": '{"ok": true}'},
                              "done": True, "prompt_eval_count": 4, "eval_count": 2}))
        p = LocalProvider(model="qwen3", transport=t)
        r = p.generate(AIRequest(messages=[Message.user("x")], response_schema={"type": "object"},
                                 reasoning=ReasoningEffort.NONE))
        self.assertEqual(r.data, {"ok": True})
        self.assertTrue(t.calls[0]["url"].endswith("/api/chat"))
        self.assertEqual(t.calls[0]["body"]["format"], {"type": "object"})
        self.assertIs(t.calls[0]["body"]["think"], False)
        self.assertEqual(r.usage.total_tokens, 6)

    def test_unreachable_is_retryable(self):
        p = LocalProvider(model="m", transport=FakeTransport(ConnectionError("refused")))
        with self.assertRaises(ProviderError) as cm:
            p.generate(AIRequest(messages=[Message.user("x")]))
        self.assertTrue(cm.exception.retryable)


class GoogleTests(unittest.TestCase):
    def _client(self, resp):
        calls = []

        class Models:
            def generate_content(self, **kw):
                calls.append(kw)
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return SimpleNamespace(models=Models()), calls

    def test_mapping(self):
        part_text = SimpleNamespace(text="Hi", function_call=None, thought=False)
        part_fc = SimpleNamespace(text=None, function_call=SimpleNamespace(name="open_app", args={"a": 1}, id="1"))
        resp = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part_text, part_fc]),
                                                           finish_reason="STOP")],
                               usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=1,
                                                              total_token_count=4))
        client, calls = self._client(resp)
        p = GoogleProvider(client=client)
        r = p.generate(AIRequest(messages=[Message.user("x")], system="s", web_grounding=True,
                                 tools=[ToolSpec("open_app", "d", {"type": "object",
                                                                  "properties": {"a": {"type": "integer"}},
                                                                  "additionalProperties": False})],
                                 reasoning=ReasoningEffort.LOW))
        self.assertEqual(r.text, "Hi")
        self.assertEqual(r.tool_calls[0].name, "open_app")
        cfg = calls[0]["config"]
        self.assertIn({"google_search": {}}, cfg["tools"])
        decl = cfg["tools"][0]["function_declarations"][0]
        self.assertEqual(decl["parameters"]["type"], "OBJECT")
        self.assertNotIn("additionalProperties", decl["parameters"])
        self.assertEqual(cfg["thinking_config"]["thinking_budget"], 1024)
        self.assertEqual(calls[0]["model"], "gemini-flash-latest")

    def test_error_mapping(self):
        client, _ = self._client(RuntimeError("429 RESOURCE_EXHAUSTED key AIzaXXXXXXXXXXXXXXXXXXXXXXXX"))
        p = GoogleProvider(client=client)
        with self.assertRaises(RateLimited) as cm:
            p.generate(AIRequest(messages=[Message.user("x")]))
        self.assertNotIn("AIzaXXXX", str(cm.exception))
        with self.assertRaises(ProviderConfigError):
            GoogleProvider(api_key="")

    def test_schema_conversion(self):
        s = to_gemini_schema({"type": "object", "properties": {"x": {"type": ["string", "null"]}},
                              "additionalProperties": False})
        self.assertEqual(s, {"type": "OBJECT", "properties": {"x": {"type": "STRING", "nullable": True}}})


class GatewayTests(unittest.TestCase):
    def test_retry_then_fallback_and_capability(self):
        flaky = FakeTransport(HTTPResult(503, b"down"), HTTPResult(503, b"down"), HTTPResult(503, b"down"))
        primary = OpenAIProvider(api_key="", model="m", base_url="http://x", transport=flaky,
                                 name="local", require_key=False)
        backup_t = FakeTransport(ok({"message": {"content": "from backup"}, "done": True}))
        backup = LocalProvider(model="b", transport=backup_t)
        cfg = AIConfig(provider="local", fallbacks=["google"], max_retries=2)
        gw = AIGateway(config=cfg, providers={"local": primary, "google": backup}, sleep=lambda s: None)
        gw._sig = cfg.signature()
        self.assertEqual(gw.text("hi"), "from backup")
        self.assertEqual(len(flaky.calls), 3)             # 1 try + 2 retries
        self.assertEqual(gw.stats["fallbacks_used"], 1)

    def test_no_fallback_raises_clean_error(self):
        p = LocalProvider(model="m", transport=FakeTransport(ok({})))
        cfg = AIConfig(provider="local")
        gw = AIGateway(config=cfg, providers={"local": p}, sleep=lambda s: None)
        gw._sig = cfg.signature()
        with self.assertRaises(CapabilityNotSupported):
            gw.text("news", web_grounding=True)
        with self.assertRaises(CapabilityNotSupported):
            gw.generate("x", audio=[(b"x", "audio/mp3")])

    def test_config_from_env(self):
        from core import ai_gateway
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name)
        (base / "config").mkdir()
        (base / "config" / "api_keys.json").write_text(json.dumps({"ai_provider": "anthropic",
                                                                  "ai_model": "gemini-flash-latest"}))
        secret_store.set_base_dir(base)
        try:
            cfg = ai_gateway.load_config()
            self.assertEqual(cfg.provider, "anthropic")
            self.assertEqual(cfg.model, "")            # gemini name never sent to another provider
            os.environ["JARVIS_AI_PROVIDER"] = "local"
            os.environ["JARVIS_AI_MODEL"] = "qwen3:8b"
            cfg = ai_gateway.load_config()
            self.assertEqual((cfg.provider, cfg.model), ("local", "qwen3:8b"))
            with self.assertRaises(ProviderConfigError):
                ai_gateway.build_provider("nope", cfg)
        finally:
            os.environ.pop("JARVIS_AI_PROVIDER", None)
            os.environ.pop("JARVIS_AI_MODEL", None)
            secret_store.set_base_dir(None)
            tmp.cleanup()

    def test_nvidia_factory_uses_openai_compatible_endpoint(self):
        from core import ai_gateway
        cfg = AIConfig(provider="nvidia", model="nvidia-model", fast_model="nvidia-fast")
        with patch.object(secret_store, "get_secret", return_value="test-key"):
            provider = ai_gateway.build_provider("nvidia", cfg)
        self.assertIsInstance(provider, OpenAIProvider)
        self.assertEqual(provider.name, "nvidia")
        self.assertEqual(provider._base, "https://integrate.api.nvidia.com/v1")
        self.assertEqual(provider.default_model, "nvidia-model")
        self.assertEqual(provider.fast_model, "nvidia-fast")

    def test_legacy_facade(self):
        from core import ai_provider, ai_gateway
        p = LocalProvider(model="m", transport=FakeTransport(ok({"message": {"content": "summary"}})))
        cfg = AIConfig(provider="local")
        gw = AIGateway(config=cfg, providers={"local": p})
        gw._sig = cfg.signature()
        ai_gateway.set_gateway(gw)
        try:
            self.assertEqual(ai_provider.get_provider().generate_text("x"), "summary")
        finally:
            ai_gateway.set_gateway(None)


if __name__ == "__main__":
    unittest.main()
