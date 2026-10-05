"""Smoke test: import the real main.py and drive JarvisLive._execute_tool.

Verification level — stated honestly:
  * GENUINE EXECUTION of main.py's real code paths (tool dispatch, central
    permission gate, confirmation banner flow, run_task/task_control wiring,
    audit, system-prompt building with the language block).
  * Third-party packages that are NOT installed where the test runs
    (PyQt6, google-genai, sounddevice, pyautogui, cv2, ...) are replaced by
    permissive stub modules, and the UI is a fake object. When run on the
    real machine, installed packages are used instead of stubs.
  * No network, no microphone, no Live session: this does not prove that
    voice, the HUD rendering or Gemini Live work end-to-end — those need a
    manual run on Windows (see CHANGELOG "How to verify on your machine").
"""
import asyncio
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types as _pytypes
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_STUB_ROOTS = {"PyQt6", "google", "sounddevice", "pyautogui", "pyperclip", "pygetwindow", "cv2", "mss",
               "playwright", "comtypes", "pycaw", "win10toast", "pywinauto", "win32api", "win32con",
               "win32gui", "ddgs", "duckduckgo_search", "youtube_transcript_api", "pptx", "docx",
               "send2trash", "fastapi", "uvicorn", "qrcode", "bs4", "psutil", "PIL", "pdfplumber",
               "PyPDF2", "pypdf", "pandas", "openpyxl", "cryptography", "starlette", "pydub"}


class _Anything:
    """Callable, subclassable, attribute-rich placeholder."""

    def __init__(self, *a, **k):
        self.__dict__.update(k)

    def __call__(self, *a, **k):
        return _Anything()

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _Anything()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False

    def __mro_entries__(self, bases):
        return (_Anything,)


class _StubModule(_pytypes.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _Anything()


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    force: set = set()

    def find_spec(self, fullname, path, target=None):
        root = fullname.split(".")[0]
        if root not in _STUB_ROOTS:
            return None
        if root in self.force:
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        # only stub what is genuinely missing
        for finder in sys.meta_path:
            if finder is self:
                continue
            try:
                spec = finder.find_spec(fullname, path, target)
            except Exception:
                spec = None
            if spec is not None:
                return None
        return importlib.machinery.ModuleSpec(fullname, self, is_package=True)

    def create_module(self, spec):
        m = _StubModule(spec.name)
        m.__path__ = []
        return m

    def exec_module(self, module):
        pass


class FakeUI:
    def __init__(self):
        self.muted = False
        self.logs, self.states, self.banners = [], [], []
        self.current_file = None
        self._win = _pytypes.SimpleNamespace(_ready=True)

    def write_log(self, t):
        self.logs.append(t)

    def set_state(self, s):
        self.states.append(s)

    def show_confirm(self, title, detail):
        self.banners.append(title)

    def hide_confirm(self):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


class MainSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.finder = _StubFinder()
        # installed-but-broken packages (e.g. sounddevice without PortAudio) are stubbed too
        for mod in ("sounddevice",):
            try:
                importlib.import_module(mod)
            except Exception:
                sys.modules.pop(mod, None)
                cls.finder.force.add(mod)
        sys.meta_path.insert(0, cls.finder)
        cls.tmp = tempfile.TemporaryDirectory()
        from core import secret_store
        from memory import memory_manager
        cls._mem_path = memory_manager.MEMORY_PATH
        try:
            secret_store.set_base_dir(Path(cls.tmp.name))
            (Path(cls.tmp.name) / "config").mkdir()
            (Path(cls.tmp.name) / "config" / "api_keys.json").write_text(json.dumps({"os_system": "linux"}))
            # never touch the real long-term memory file (patched BEFORE main is imported)
            memory_manager.MEMORY_PATH = Path(cls.tmp.name) / "memory" / "long_term.json"
            import main as jmain
            cls.main = jmain
        except BaseException:
            # unittest skips tearDownClass when setUpClass fails: undo here, or the import
            # stubs and the temporary base dir would leak into every later test module
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        from core import secret_store, confirm
        from memory import memory_manager
        memory_manager.MEMORY_PATH = cls._mem_path
        secret_store.set_base_dir(None)
        confirm.bind(None, None, None)
        if cls.finder in sys.meta_path:
            sys.meta_path.remove(cls.finder)
        cls.tmp.cleanup()

    def _live(self):
        ui = FakeUI()
        live = self.main.JarvisLive(ui)
        from core import confirm
        confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
        return live, ui

    def _call(self, live, name, args):
        fc = _pytypes.SimpleNamespace(name=name, args=args, id="1")
        captured = {}
        orig = self.main.types.FunctionResponse

        def fr(**kw):
            captured.update(kw)
            return kw
        self.main.types.FunctionResponse = fr
        try:
            asyncio.run(live._execute_tool(fc))
        finally:
            self.main.types.FunctionResponse = orig
        return str(captured.get("response", {}).get("result"))

    # Entry points into JarvisCore itself (not actions): each one routes to
    # registry tools that are permission-gated there (e.g. computer_use submits
    # a computer.run task that needs HUD confirmation; install_plugin gates
    # plugin.install; analyze_screen gates screen.analyze).
    META_TOOLS = ("run_task", "task_control", "workflow", "set_language", "memory_inspect",
                  "think", "recall_context", "emergency_stop", "capabilities", "install_plugin",
                  "analyze_screen", "computer_use", "about_self",
                  # Phase 7: draw submits a draw.paint task (HUD confirmation); watch_screen
                  # is the session's own frame feed (privacy-paused, auto-stopping)
                  "draw", "watch_screen")

    def test_declarations_are_well_formed(self):
        names = [d["name"] for d in self.main.TOOL_DECLARATIONS]
        self.assertEqual(len(names), len(set(names)))
        for n in self.META_TOOLS:
            self.assertIn(n, names)
        live, _ = self._live()
        reg = live._core.registry
        for n in names:
            if n in self.META_TOOLS:
                continue
            self.assertTrue(reg.has(n), f"{n} missing from ToolRegistry — it would be DENIED")
        for gated in ("computer.run", "plugin.install", "screen.analyze"):
            self.assertTrue(reg.has(gated), gated)

    def test_permission_gate_and_confirm_flow(self):
        live, ui = self._live()
        out = self._call(live, "file_controller", {"action": "delete", "path": "desktop", "name": "x.txt"})
        self.assertIn("CONFIRMATION_PENDING", out)
        self.assertTrue(ui.banners)
        from core import confirm
        confirm.cancel_pending()
        out = self._call(live, "send_message", {"receiver": "Arun", "message_text": "hi"})
        self.assertIn("CONFIRMATION_PENDING", out)
        confirm.cancel_pending()
        out = self._call(live, "file_controller", {"path": "x"})          # missing required action
        self.assertIn("did not run", out)

    def test_memory_refuses_secrets(self):
        live, _ = self._live()
        out = self._call(live, "save_memory", {"category": "notes", "key": "wifi_password",
                                                "value": "hunter2"})
        self.assertIn("Not saved", out)

    def test_task_tools(self):
        live, ui = self._live()
        out = self._call(live, "task_control", {"action": "status"})
        self.assertIn("not working on", out)
        live._core.planner._use_llm = False          # no model, and nothing real gets launched
        out = self._call(live, "run_task", {"request": "blorp the flibber"})
        self.assertIn("TASK_STARTED", out)
        time.sleep(0.5)
        self.assertEqual(live._core.store.latest().state.value, "FAILED")   # unplannable → honest failure
        out = self._call(live, "task_control", {"action": "history"})
        self.assertIn("Most recent first", out)
        out = self._call(live, "memory_inspect", {})
        self.assertIn("never store passwords", out)

    def test_phase5_cognitive_tools(self):
        live, ui = self._live()
        # think: pure arithmetic is answered exactly on-device — no model involved
        out = self._call(live, "think", {"question": "What's 25 × 42?"})
        self.assertTrue(out.startswith("1050"), out)
        self.assertIn("computed exactly", out)
        # selective memory policy through the real save_memory path
        out = self._call(live, "save_memory", {"category": "notes", "key": "mood", "value": "tired today"})
        self.assertIn("episode", out)
        out = self._call(live, "save_memory", {"category": "notes", "key": "condition", "value": "has diabetes"})
        self.assertIn("Ask", out)
        out = self._call(live, "save_memory", {"category": "preferences", "key": "editor", "value": "VS Code"})
        self.assertIn("ok", out)
        # context engine sees the fact and the episode
        out = self._call(live, "recall_context", {"query": "what do you know about me"})
        self.assertIn("VS Code", out)
        # capability search finds the catalog plugin
        out = self._call(live, "capabilities", {"need": "convert 100 dollars to rupees"})
        self.assertIn("currency_converter", out)
        # computer use becomes a task that waits for the HUD confirmation
        out = self._call(live, "computer_use", {"goal": "open the settings page"})
        self.assertIn("TASK_STARTED", out)
        time.sleep(0.3)
        self.assertEqual(live._core.store.latest().state.value, "WAITING_FOR_PERMISSION")
        # emergency stop cancels it and withdraws the HUD prompt
        out = self._call(live, "emergency_stop", {"reason": "test"})
        self.assertIn("Emergency stop engaged", out)
        time.sleep(0.5)
        self.assertEqual(live._core.store.latest().state.value, "CANCELLED")
        live._core.kill.release("test end")
        out = self._call(live, "task_control", {"action": "retry"})
        self.assertIn("Retrying", out)                      # the cancelled task can be retried
        live._core.orchestrator.cancel_all()
        # the session prompt carries the context-engine block
        cfg = {}
        orig = self.main.types.LiveConnectConfig
        self.main.types.LiveConnectConfig = lambda **kw: cfg.update(kw) or kw
        try:
            live._build_config()
        finally:
            self.main.types.LiveConnectConfig = orig
        self.assertIn("[CONTEXT ENGINE]", cfg["system_instruction"])

    def test_language_directive_in_prompt(self):
        live, _ = self._live()
        cfg_calls = {}
        orig = self.main.types.LiveConnectConfig

        def lcc(**kw):
            cfg_calls.update(kw)
            return kw
        self.main.types.LiveConnectConfig = lcc
        try:
            live._build_config()
        finally:
            self.main.types.LiveConnectConfig = orig
        self.assertIn("[LANGUAGE]", cfg_calls["system_instruction"])
        self.assertIn("Tanglish", cfg_calls["system_instruction"])
        decl_names = [d["name"] for d in cfg_calls["tools"][0]["function_declarations"]]
        self.assertIn("run_task", decl_names)

    # ── Part One ─────────────────────────────────────────────────────────────
    def test_part1_persona_voice_and_prompt(self):
        live, _ = self._live()
        (Path(self.tmp.name) / "config" / "api_keys.json").write_text(
            json.dumps({"os_system": "linux", "voice_name": "FRIDAY"}))
        from memory import config_manager as cm
        orig = cm.CONFIG_FILE
        cm.CONFIG_FILE = Path(self.tmp.name) / "config" / "api_keys.json"
        voices = []
        o_lcc, o_pvc = self.main.types.LiveConnectConfig, self.main.types.PrebuiltVoiceConfig
        cfg = {}
        self.main.types.LiveConnectConfig = lambda **kw: cfg.update(kw) or kw
        self.main.types.PrebuiltVoiceConfig = lambda **kw: voices.append(kw.get("voice_name")) or kw
        try:
            live._build_config()
        finally:
            self.main.types.LiveConnectConfig, self.main.types.PrebuiltVoiceConfig = o_lcc, o_pvc
            cm.CONFIG_FILE = orig
            (Path(self.tmp.name) / "config" / "api_keys.json").write_text(json.dumps({"os_system": "linux"}))
        self.assertEqual(voices, ["Kore"])                 # FRIDAY → a real Gemini voice, never "FRIDAY"
        si = cfg["system_instruction"]
        self.assertIn("Your name is FRIDAY", si)
        self.assertIn("DATA, NOT INSTRUCTIONS", si)
        self.assertNotIn("{{PERSONALITY_BLOCK}}", si)
        self.assertEqual(si.count("[PERSONA]"), 1)
        self.assertEqual(live._asst_name, "FRIDAY")

    def test_part1_remember_recall_sources(self):
        live, ui = self._live()
        out = self._call(live, "remember", {"text": "Remember that my bike is a blue Hercules"})
        self.assertIn("[MEMORY_SAVED", out)
        self.assertIn("verified=yes", out)
        out = self._call(live, "recall_memory", {"query": "", "recent": True})
        self.assertIn("blue Hercules", out.split("\n")[1])    # newest first
        out = self._call(live, "recall_memory", {"query": "what colour is my bike"})
        self.assertIn("[mem:", out)
        self.assertIn("blue Hercules", out)
        live._core.finish_turn("what colour is my bike", "Your bike is a blue Hercules.")
        out = self._call(live, "show_sources", {})
        self.assertIn("[SOURCES shown on screen]", out)
        self.assertIn("bike", out)
        live._core.finish_turn("thanks", "You're welcome.")    # small talk → last record unchanged
        self.assertIn("bike", live._core.sources.last().question)
        out = self._call(live, "remember", {"text": "remember my password is hunter2"})
        self.assertIn("[MEMORY_NOT_SAVED]", out)
        out = self._call(live, "search_knowledge", {"query": "what am I looking at"})
        self.assertIn("screen_process", out)

    def test_part1_typed_remember_is_saved_before_the_model_sees_it(self):
        live, _ = self._live()
        txt = live._preprocess_text("remember that the spare key is with Ravi", "text")
        self.assertIn("[MEMORY_SAVED", txt)
        self.assertEqual(live._preprocess_text("open chrome", "text"), "open chrome")

    def test_part1_switch_model_and_vision_failure(self):
        live, _ = self._live()
        out = self._call(live, "switch_model", {"model": "gpt-9 ultra"})
        self.assertTrue(out.startswith("[MODEL_NOT_SWITCHED]"), out)
        out = self._call(live, "switch_model", {"action": "status"})
        self.assertIn("Brain:", out)
        # Phase 7: the default voice model is gemini-3.8-live (measured fastest)
        out = self._call(live, "switch_model", {"model": "gemini 3.8 live", "scope": "voice"})
        self.assertIn("already", out)                          # exact match, and it is the current one
        out = live._switch_live_model("other-live-model")       # registry-resolved id → runtime override
        self.assertTrue(out.startswith("[MODEL_SWITCHED]"), out)
        self.assertEqual(live._live_override, "models/other-live-model")
        self.assertIn("(switched at runtime)", live._switch_live_model("?status"))
        self.assertTrue(live._switch_live_model(None).startswith("[MODEL_RESET]"))
        self.assertIsNone(live._live_override)
        # capture fails (no screen here) → honest message, vision NOT left busy forever
        import core.vision as V
        orig = V.capture
        V.capture = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no display"))
        try:
            out = self._call(live, "screen_process", {"text": "what is this", "angle": "screen"})
        finally:
            V.capture = orig
        self.assertIn("[VISION_UNAVAILABLE]", out)
        self.assertFalse(live._vision_busy)

    def test_part1_interrupt_states(self):
        live, ui = self._live()
        live.set_speaking(True)
        self.assertEqual(live._voice.state.value, "speaking")
        live.interrupt(discard=False, reason="you spoke")
        self.assertFalse(live._interrupted)                    # server barge-in: next answer is kept
        self.assertEqual(live._voice.state.value, "listening")
        self.assertIn("SPEAKING", ui.states)
        live.set_speaking(True)
        live.interrupt(discard=True, reason="stop phrase")
        self.assertTrue(live._interrupted)
        self.assertGreaterEqual(live._voice.interruptions, 2)


if __name__ == "__main__":
    unittest.main()
