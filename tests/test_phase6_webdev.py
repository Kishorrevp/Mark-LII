"""Phase 6 — background processes, running/checking a built website, the web agent.

Verification level: MIXED, stated per test.
  * ProcessManager, in_workspace, free_port: GENUINE EXECUTION (real local
    http.server subprocess, real port binding, real pid-identity check).
  * webdev.run: GENUINE EXECUTION end to end — it serves a real generated site
    and drives the pre-installed headless Chromium (Playwright) over it,
    catching a real broken image and a real JS console error. Skipped only if
    Playwright / a browser engine is unavailable in the environment.
  * BrowserAgent: GENUINE EXECUTION of the decide/act/guard loop against a fake
    in-memory browser session (no network); the model is a scripted function.
    The safety guards (sensitive fields, submit confirmation, CAPTCHA) are the
    point and are exercised for real.
"""
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import browser_agent as BA                                   # noqa: E402
from core import processes as P                                        # noqa: E402
from core import webdev                                                # noqa: E402
from core.browser_agent import BrowserAgent, Observation               # noqa: E402
from core.tools.registry import ToolContext, ToolResult                # noqa: E402


def _has_playwright() -> bool:
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--no-sandbox"])
            b.close()
        return True
    except Exception:
        return False


HAVE_PW = _has_playwright()


class ProcessTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.root = Path(tempfile.mkdtemp())
        self.site = self.root / "site"
        self.site.mkdir()
        (self.site / "index.html").write_text("<!doctype html><title>x</title><h1>hello</h1>", encoding="utf-8")
        self.pm = P.ProcessManager(state_path=self.root / "state.json", extra_roots=[self.root])

    def tearDown(self):
        self.pm.stop_all()

    def test_free_port_and_workspace_guard(self):
        port = P.free_port(8801, 8809)
        self.assertTrue(8801 <= port <= 8809)
        self.assertTrue(P.in_workspace(self.site, [self.root]))
        self.assertFalse(P.in_workspace("/etc", [self.root]))
        outside = self.root.parent / f"outside_{self.root.name}"      # exists, but not a workspace root
        outside.mkdir(exist_ok=True)
        with self.assertRaises(PermissionError):
            self.pm.start_preview(str(outside))

    def test_start_serve_persist_and_stop(self):
        mp = self.pm.start_preview(str(self.site), port=P.free_port(8811, 8819))
        self.assertTrue(P.wait_port(mp.port, 5))
        import urllib.request
        body = urllib.request.urlopen(mp.url, timeout=5).read().decode()
        self.assertIn("hello", body)
        # a second manager reads the state file and sees the live process by identity
        pm2 = P.ProcessManager(state_path=self.root / "state.json", extra_roots=[self.root])
        self.assertEqual([p.name for p in pm2.list()], [mp.name])
        self.assertTrue(self.pm.stop(mp.name))
        self.assertFalse(P.wait_port(mp.port, 1))
        self.assertEqual(self.pm.list(), [])

    def test_stop_only_touches_our_process(self):
        mp = self.pm.start_preview(str(self.site), port=P.free_port(8821, 8829))
        # a stale record whose pid was recycled (create_time won't match) is never killed
        mp2 = P.ManagedProcess(name="ghost", recipe="preview", pid=mp.pid, created=mp.created + 999,
                               cwd=str(self.site), log="", port=0)
        self.assertFalse(self.pm._alive(mp2))


@unittest.skipUnless(HAVE_PW, "Playwright/Chromium not available")
class WebDevRunTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.root = Path(tempfile.mkdtemp())
        self.site = self.root / "JARVIS Projects" / "demo"
        self.site.mkdir(parents=True)
        self._orig_roots = P.workspace_roots
        P.workspace_roots = lambda: [self.root / "JARVIS Projects"]
        P._PM = P.ProcessManager(persist=False)     # fresh singleton; logs go to the temp dir, not memory/

    def tearDown(self):
        P.get_manager().stop_all()
        P.workspace_roots = self._orig_roots
        P._PM = None

    def _write(self, **files):
        for name, body in files.items():
            (self.site / name.replace("__", ".")).write_text(body, encoding="utf-8")

    def test_clean_site_runs_green(self):
        self._write(index__html=(
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Portfolio</title><link rel=stylesheet href=style.css></head>"
            "<body><h1>Jane Doe</h1><p>Hello</p><a href='about.html'>About</a></body></html>"),
            about__html="<!doctype html><html lang=en><head><meta charset=utf-8><title>About</title></head>"
                        "<body><h1>About</h1></body></html>",
            style__css="body{font-family:sans-serif;max-width:40rem;margin:auto}h1{color:#123}")
        rep = webdev.run(str(self.site), vision=False)
        self.assertTrue(rep["ran"], rep)
        self.assertEqual(rep["errors"], [], rep["errors"])
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["pages_checked"], 2)
        self.assertTrue(rep["shots"])

    def test_broken_image_link_and_js_error_are_caught(self):
        self._write(index__html=(
            "<!doctype html><html lang=en><head><meta charset=utf-8><title>Bad</title></head>"
            "<body><h1>Bad</h1><img src='missing.png' alt=x>"
            "<a href='nope.html'>broken link</a>"
            "<script>document.body.style.width = wideOverflowUndefinedVar;</script>"
            "<script src='missing.js'></script></body></html>"))
        rep = webdev.run(str(self.site), vision=False)
        self.assertTrue(rep["ran"])
        self.assertFalse(rep["ok"])
        joined = " | ".join(rep["errors"])
        self.assertIn("missing.png", joined)
        self.assertIn("missing.js", joined)
        self.assertTrue(any("nope.html" in e for e in rep["errors"]), joined)
        self.assertTrue(any("404" in e or "ReferenceError" in e or "is not defined" in e for e in rep["errors"]),
                        joined)

    def test_mobile_overflow_is_a_warning(self):
        self._write(index__html=(
            "<!doctype html><html lang=en><head><meta charset=utf-8><title>Wide</title></head>"
            "<body style='margin:0'><div style='width:1200px;height:40px;background:#ccc'>wide</div></body></html>"))
        rep = webdev.run(str(self.site), vision=False)
        self.assertTrue(rep["ran"])
        self.assertTrue(any("wide" in w or "horizontal scroll" in w for w in rep["warnings"]), rep["warnings"])

    def test_tool_wrapper_reports_and_verifies(self):
        self._write(index__html="<!doctype html><html lang=en><head><meta charset=utf-8><title>OK</title>"
                                "</head><body><h1>OK</h1></body></html>")
        ctx = ToolContext(extras={"should_stop": lambda: False, "progress": lambda m: None})
        res = webdev.t_web_run({"dir": str(self.site), "vision": False}, ctx)
        self.assertTrue(res.ok, res.error)
        v = webdev.verify_web_run({}, res)
        self.assertTrue(v.ok)


# ── Fake browser session for the agent's decide/act/guard loop ───────────────

class FakePage:
    def __init__(self, script):
        self.script, self.i = script, 0
        self.typed, self.clicked, self.selected = [], [], []

    def state(self):
        return self.script[min(self.i, len(self.script) - 1)]


class FakeSession:
    """Implements just what BrowserAgent uses: run(coro) and _get_page()."""

    def __init__(self, pages):
        self.page = FakePage(pages)

    async def _get_page(self):
        return self.page

    def run(self, coro, timeout=30):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


class _AgentSession(FakeSession):
    """Answers the agent's two coroutines: the snapshot evaluate() and the act()."""

    async def _get_page(self):
        sess = self

        class P2:
            async def evaluate(self, js):
                return sess.page.state()

            async def query_selector_all(self, sel):
                els = sess.page.state()["elements"]
                return [_Handle(sess, i) for i in range(len(els))]

            async def wait_for_timeout(self, ms):
                return None

            class mouse:
                @staticmethod
                async def wheel(x, y):
                    return None
        return P2()


class _Handle:
    def __init__(self, sess, idx):
        self.sess, self.idx = sess, idx

    async def is_visible(self):
        return True

    async def click(self, timeout=0):
        self.sess.page.clicked.append(self.idx)
        self.sess.page.i += 1

    async def fill(self, text, timeout=0):
        self.sess.page.typed.append((self.idx, text))

    async def select_option(self, label=None):
        self.sess.page.selected.append((self.idx, label))


class BrowserAgentTests(unittest.TestCase):
    def _page(self, title, text, elements, url="http://x/"):
        return {"title": title, "url": url, "text": text, "elements": elements}

    def test_observation_marks_sensitive_fields(self):
        obs = Observation(elements=[
            {"tag": "input", "type": "password", "label": "Password", "name": "pass"},
            {"tag": "input", "type": "text", "label": "Card number", "name": "cc-number",
             "autocomplete": "cc-number"},
            {"tag": "input", "type": "text", "label": "Full name", "name": "name"}])
        self.assertTrue(obs.sensitive(obs.elements[0]))
        self.assertTrue(obs.sensitive(obs.elements[1]))
        self.assertFalse(obs.sensitive(obs.elements[2]))
        self.assertIn("SENSITIVE", obs.render())

    def test_reads_a_value_and_finishes(self):
        page = self._page("Pricing", "Pro plan is $49 per month", [{"tag": "a", "label": "Home", "href": "/"}])
        sess = _AgentSession([page])
        script = [{"action": "read"}, {"action": "done", "answer": "The Pro plan costs $49/month."}]
        model = lambda prompt, schema: script.pop(0)
        ag = BrowserAgent(session=sess, model=model)
        r = ag.run("find the price of the Pro plan")
        self.assertTrue(r.ok)
        self.assertIn("$49", r.data["answer"])

    def test_refuses_to_type_into_a_password(self):
        page = self._page("Login", "Sign in", [
            {"tag": "input", "type": "password", "label": "Password", "name": "pass"}])
        sess = _AgentSession([page])
        model = lambda prompt, schema: {"action": "type", "index": 0, "text": "hunter2"}
        r = BrowserAgent(session=sess, model=model).run("log in")
        self.assertFalse(r.ok)
        self.assertIn("password", r.error.lower())
        self.assertEqual(sess.page.typed, [])

    def test_asks_before_a_pay_button_and_stops_if_declined(self):
        page = self._page("Cart", "Total $10", [{"tag": "button", "label": "Place order", "name": "buy"}])
        sess = _AgentSession([page])
        asked = {}

        def confirm(title, detail=""):
            asked["t"] = title
            return False
        model = lambda prompt, schema: {"action": "click", "index": 0}
        r = BrowserAgent(session=sess, model=model, confirm=confirm).run("buy it")
        self.assertFalse(r.ok)
        self.assertIn("Place order", asked["t"])
        self.assertEqual(sess.page.clicked, [])

    def test_clicks_a_safe_link_when_confirmed_not_needed(self):
        p1 = self._page("Home", "welcome", [{"tag": "a", "label": "Pricing", "href": "/pricing"}])
        p2 = self._page("Pricing", "Pro $49", [{"tag": "a", "label": "Home", "href": "/"}], url="http://x/pricing")
        sess = _AgentSession([p1, p2])
        script = [{"action": "click", "index": 0}, {"action": "done", "answer": "Pro is $49"}]
        r = BrowserAgent(session=sess, model=lambda p, s: script.pop(0)).run("go to pricing")
        self.assertTrue(r.ok)
        self.assertEqual(sess.page.clicked, [0])

    def test_captcha_stops_the_agent(self):
        page = self._page("Verify", "Please verify you are human", [])
        sess = _AgentSession([page])
        r = BrowserAgent(session=sess, model=lambda p, s: {"action": "read"}).run("continue")
        self.assertFalse(r.ok)
        self.assertIn("CAPTCHA", r.error)

    def test_no_model_degrades_to_reading(self):
        page = self._page("Doc", "Here is the content", [])
        sess = _AgentSession([page])
        r = BrowserAgent(session=sess, model=None).run("summarise this")
        self.assertTrue(r.ok)
        self.assertIn("content", r.message)


if __name__ == "__main__":
    unittest.main()
