"""Phase 6 — the edit-and-verify Coding Agent and the Design Agent (raster/vector).

Verification level: GENUINE EXECUTION. code_agent really scans a temp project,
applies real search/replace edits to real files, writes .bak backups + undo
records, and runs a real py_compile / pytest check. design_agent really parses
and renders SVG to PNG (Playwright headless Chromium) and checks it isn't
blank. The MODEL is a fake gateway that returns scripted responses — so no
network, but every edit/apply/verify/render code path runs for real.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import code_agent, design_agent, research, undo                    # noqa: E402
from core.ai_gateway import AIGateway, set_gateway                            # noqa: E402
from core.providers.base import AIResponse, Capability, CapabilityNotSupported, ProviderError, RateLimited  # noqa: E402
from core.tools.registry import ToolContext                                   # noqa: E402


def _has_pw() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(args=["--no-sandbox"]).close()
        return True
    except Exception:
        return False


HAVE_PW = _has_pw()


class FakeGateway:
    """Stands in for AIGateway. Scripts by call kind: json edits, svg text,
    image bytes, vision json."""

    def __init__(self, *, edits=None, svg=None, image=None, vision=None, raise_image=None):
        self.edits = list(edits or [])
        self.svg = svg
        self.image = image
        self.vision = vision or {"matches": True, "problems": [], "summary": "looks good"}
        self.raise_image = raise_image
        self.calls = []

    def generate(self, prompt="", *, schema=None, images=(), tier="normal", **kw):
        self.calls.append({"tier": tier, "schema": bool(schema), "images": len(list(images or []))})
        if images:                                   # vision check
            return AIResponse(data=self.vision, model="v")
        if schema:                                   # coding edits (json)
            item = self.edits.pop(0) if self.edits else {"edits": [], "done": True}
            return AIResponse(data=item, model="deep")
        return AIResponse(text=self.svg or "", model="deep")   # svg text

    def generate_image(self, prompt, *, images=(), aspect_ratio=None, model=None, timeout=120.0):
        self.calls.append({"image": True})
        if self.raise_image is not None:
            raise self.raise_image
        if not self.image:
            return AIResponse(images=[], model="img")
        return AIResponse(images=[(self.image, "image/png")], model="img")


def project(**files) -> Path:
    d = Path(tempfile.mkdtemp())
    for name, body in files.items():
        p = d / name.replace("__", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


class CodeAgentTests(unittest.TestCase):
    def tearDown(self):
        set_gateway(None)
        undo.clear()

    def _ctx(self, root):
        return ToolContext(extras={"should_stop": lambda: False, "progress": lambda m: None,
                                   "project_root": str(root)})

    def test_edit_applies_backs_up_and_syntax_checks(self):
        root = project(**{"calc.py": "def add(a, b):\n    return a - b\n"})   # bug: minus
        set_gateway(FakeGateway(edits=[{"summary": "fix add", "done": True, "edits": [
            {"file": "calc.py", "action": "replace", "find": "return a - b", "replacement": "return a + b"}]}]))
        res = code_agent.t_code_task({"request": "fix the add function", "path": str(root)}, self._ctx(root))
        self.assertTrue(res.ok, res.error)
        self.assertEqual((root / "calc.py").read_text().strip().splitlines()[-1].strip(), "return a + b")
        self.assertTrue((root / "calc.py.bak").exists())
        self.assertEqual((root / "calc.py.bak").read_text(), "def add(a, b):\n    return a - b\n")
        rec = undo.records()[0]
        self.assertEqual((rec["kind"], Path(rec["paths"][0]).name), ("edit", "calc.py"))
        v = code_agent.verify_code_task({}, res)
        # no test suite in this project → honestly "unverifiable" (edited + syntax-checked), never a false pass
        self.assertFalse(v.failed)
        self.assertFalse(res.data["tested"])

    def test_runs_tests_and_fixes_over_rounds(self):
        # unittest-style so it runs whether or not pytest is installed (code_agent
        # falls back to `python -m unittest` when pytest is absent).
        root = project(**{
            "m.py": "def double(x):\n    return x + x\n",
            "test_m.py": "import unittest\nfrom m import double\n\n"
                         "class T(unittest.TestCase):\n"
                         "    def test_double(self):\n        self.assertEqual(double(3), 6)\n"
                         "    def test_triple(self):\n        from m import triple\n"
                         "        self.assertEqual(triple(3), 9)\n"})
        set_gateway(FakeGateway(edits=[
            {"summary": "add triple", "done": True, "edits": [
                {"file": "m.py", "action": "append", "replacement": "def triple(x):\n    return x + x\n"}]},
            {"summary": "fix triple", "done": True, "edits": [
                {"file": "m.py", "action": "replace", "find": "def triple(x):\n    return x + x",
                 "replacement": "def triple(x):\n    return x * 3"}]}]))
        res = code_agent.t_code_task({"request": "add a triple function", "path": str(root),
                                      "max_rounds": 3}, self._ctx(root))
        self.assertTrue(res.ok, res.error)
        self.assertTrue(res.data["tested"])
        self.assertEqual(res.data["rounds"], 2)                 # first attempt failed the test, second fixed it

    def test_edit_outside_project_is_rejected(self):
        root = project(**{"a.py": "x = 1\n"})
        set_gateway(FakeGateway(edits=[{"done": True, "edits": [
            {"file": "../evil.py", "action": "create", "replacement": "bad"},
            {"file": "a.py", "action": "replace", "find": "x = 1", "replacement": "x = 2"}]}]))
        res = code_agent.t_code_task({"request": "change x", "path": str(root)}, self._ctx(root))
        self.assertTrue(res.ok, res.error)
        self.assertFalse((root.parent / "evil.py").exists())
        self.assertEqual((root / "a.py").read_text().strip(), "x = 2")

    def test_failing_change_reports_honestly(self):
        root = project(**{"s.py": "value = 1\n"})
        set_gateway(FakeGateway(edits=[{"done": True, "edits": [
            {"file": "s.py", "action": "replace", "find": "value = 1", "replacement": "value = ("}]}]))  # broken
        res = code_agent.t_code_task({"request": "break it", "path": str(root), "max_rounds": 1},
                                     self._ctx(root))
        self.assertFalse(res.ok)
        self.assertIn("isn't passing", res.error)

    def test_unfound_text_is_rejected_not_applied(self):
        root = project(**{"a.py": "x = 1\n"})
        set_gateway(FakeGateway(edits=[{"done": True, "edits": [
            {"file": "a.py", "action": "replace", "find": "nonexistent code", "replacement": "y"}]}]))
        res = code_agent.t_code_task({"request": "x", "path": str(root), "max_rounds": 1}, self._ctx(root))
        self.assertFalse(res.ok)
        self.assertEqual((root / "a.py").read_text(), "x = 1\n")


class DesignVectorTests(unittest.TestCase):
    def tearDown(self):
        set_gateway(None)

    def _ctx(self):
        return ToolContext(extras={"should_stop": lambda: False, "progress": lambda m: None})

    GOOD_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
                '<rect width="100" height="100" fill="#0af"/><circle cx="50" cy="50" r="30" fill="#fff"/></svg>')

    def test_rejects_svg_with_script(self):
        set_gateway(FakeGateway(svg='<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'))
        res = design_agent.t_design_vector({"prompt": "a logo"}, self._ctx())
        self.assertFalse(res.ok)
        self.assertIn("disallowed", res.error)

    def test_rejects_non_svg(self):
        set_gateway(FakeGateway(svg="I can't draw that."))
        res = design_agent.t_design_vector({"prompt": "a logo"}, self._ctx())
        self.assertFalse(res.ok)

    @unittest.skipUnless(HAVE_PW, "Playwright not available")
    def test_draws_renders_and_vision_checks(self):
        set_gateway(FakeGateway(svg=self.GOOD_SVG, vision={"matches": True, "problems": [], "summary": "a blue disc"}))
        res = design_agent.t_design_vector({"prompt": "a blue circle logo"}, self._ctx())
        self.assertTrue(res.ok, res.error)
        self.assertTrue(res.data["rendered"])
        self.assertTrue(Path(res.data["svg"]).is_file())
        self.assertTrue(Path(res.data["png"]).is_file())
        self.assertTrue(design_agent._not_blank(Path(res.data["png"])))
        v = design_agent.verify_design({}, res)
        self.assertTrue(v.ok)

    @unittest.skipUnless(HAVE_PW, "Playwright not available")
    def test_vision_flags_a_mismatch(self):
        set_gateway(FakeGateway(svg=self.GOOD_SVG,
                                vision={"matches": False, "problems": ["it's a circle, not a cat"],
                                        "summary": "a disc"}))
        res = design_agent.t_design_vector({"prompt": "a cat"}, self._ctx())
        self.assertTrue(res.ok)                                  # still produced a file
        self.assertFalse(design_agent.verify_design({}, res).ok)  # but verification fails the mismatch


class DesignImageTests(unittest.TestCase):
    def tearDown(self):
        set_gateway(None)

    def _ctx(self):
        return ToolContext(extras={"should_stop": lambda: False, "progress": lambda m: None})

    def test_real_image_bytes_are_saved(self):
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        set_gateway(FakeGateway(image=png))
        res = design_agent.t_design_image({"prompt": "a sunset"}, self._ctx())
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.data["kind"], "image")
        self.assertTrue(Path(res.data["path"]).is_file())
        self.assertEqual(Path(res.data["path"]).read_bytes(), png)

    @unittest.skipUnless(HAVE_PW, "Playwright not available")
    def test_no_free_quota_falls_back_to_vector_never_fakes(self):
        gw = FakeGateway(raise_image=RateLimited("429 ... limit: 0, model: gemini-2.5-flash-image"),
                         svg=DesignVectorTests.GOOD_SVG)
        set_gateway(gw)
        res = design_agent.t_design_image({"prompt": "a blue circle"}, self._ctx())
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.data["kind"], "vector")            # honest fallback, not a fake image
        self.assertTrue(res.data.get("fell_back_from_image"))
        self.assertIn("isn't available", res.message)

    def test_capability_missing_falls_back(self):
        gw = FakeGateway(raise_image=CapabilityNotSupported("local", Capability.IMAGE_OUTPUT),
                         svg=DesignVectorTests.GOOD_SVG)
        set_gateway(gw)
        res = design_agent.t_design_image({"prompt": "a logo"}, self._ctx())
        self.assertEqual(res.data["kind"], "vector")


if __name__ == "__main__":
    unittest.main()
