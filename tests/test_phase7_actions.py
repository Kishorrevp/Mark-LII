"""Phase 7 — screen watching, drawing with the mouse, references, "review this
and do", three-way confirmations (ALLOW ONCE / ALWAYS / DENY), day-to-day
learning, Mission Control and the new core / starfield UI.

Verification level:
  GENUINE EXECUTION — core/screen_watch.py (asyncio with fake capture/send),
  core/draw_agent.py (SVG → strokes → canvas maths; Painter with a fake mouse,
  fake screen and fake vision), core/references.py, core/learning.py,
  core/mission.py, core/confirm.py + core/permissions.py (3-way flow, trust
  persistence, never-trustable keys), the planner rules, team research with
  fake web backends, ui_holo.FilamentSphere (numpy).
  STRUCTURAL — ui.py / ui_command_center.py / ui_mission.py (PyQt6 is not
  installable in the sandbox): AST + source checks.
"""
import ast
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import confirm, references                                  # noqa: E402
from core import draw_agent as D                                       # noqa: E402
from core.learning import Learner                                      # noqa: E402
from core.screen_watch import ScreenWatcher                            # noqa: E402


# ── screen watching ───────────────────────────────────────────────────────────
class Frame:
    def __init__(self, v):
        self.v, self.data, self.mime = v, f"jpeg{v}".encode(), "image/jpeg"


class ScreenWatchTests(unittest.TestCase):
    def _run(self, frames, titles=None, polls=12):
        """Runs until the watcher has looked `polls` times (deterministic — no
        dependence on the OS timer resolution), then stops it."""
        sent, states, looked = [], [], [0]
        it = iter(frames)
        tl = iter(titles or [])

        def grab():
            looked[0] += 1
            return next(it)

        def title():
            looked[0] += 0 if titles is None else 1
            return next(tl, "")

        async def send(data, mime):
            sent.append(data)

        async def main():
            w = ScreenWatcher(grab=grab, send=send, title=title,
                              change=lambda a, b: 0.0 if a.v == b.v else 1.0,
                              on_state=lambda on, why: states.append((on, why)), log=lambda m: None,
                              interval=0.001, keepalive=999)
            msg = w.start(minutes=1)
            for _ in range(5000):
                if looked[0] >= polls:
                    break
                await asyncio.sleep(0.001)
            stop = w.stop()
            return w, msg, stop
        w, msg, stop = asyncio.run(main())
        return w, sent, states, msg, stop

    def test_only_changed_frames_are_sent(self):
        frames = [Frame(1), Frame(1), Frame(1), Frame(2), Frame(2), Frame(3)] + [Frame(3)] * 500
        w, sent, states, msg, stop = self._run(frames)
        self.assertEqual(sent[:3], [b"jpeg1", b"jpeg2", b"jpeg3"])
        self.assertEqual(len(sent), 3, "an unchanged screen costs nothing")
        self.assertIn("Watching the screen", msg)
        self.assertEqual(states[0][0], True)
        self.assertFalse(w.active)

    def test_private_windows_are_never_sent(self):
        frames = [Frame(i) for i in range(600)]
        titles = ["Sign in – Google Accounts"] * 600
        w, sent, *_ = self._run(frames, titles)
        self.assertEqual(sent, [])


# ── drawing ───────────────────────────────────────────────────────────────────
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000"><title>a cat</title>'
       '<g transform="translate(100,0)"><circle cx="400" cy="500" r="200" stroke="black" fill="none"/></g>'
       '<path d="M 300 300 L 400 150 L 500 300 Z" stroke="black" fill="none"/>'
       '<path d="M200 700 C 300 800, 500 800, 600 700" stroke="black" fill="none"/>'
       '<path d="M100 100 a 50 50 0 1 0 100 0" fill="none" stroke="black"/>'
       '<rect x="10" y="10" width="40" height="40" fill="none" stroke="black"/>'
       '<text x="0" y="0">ignored</text></svg>')


class DrawTests(unittest.TestCase):
    def test_svg_flattens_to_strokes_in_the_unit_box(self):
        strokes = D.strokes_from_svg(SVG)
        self.assertGreaterEqual(len(strokes), 5)
        for st in strokes:
            for x, y in st:
                self.assertTrue(0 <= x <= 1 and 0 <= y <= 1)
        # the translated circle is centred at x = (100+400)/1000 = 0.5
        circ = max(strokes, key=len)
        cx = sum(p[0] for p in circ) / len(circ)
        self.assertAlmostEqual(cx, 0.5, delta=0.03)

    def test_transforms_and_arcs(self):
        m = D.parse_transform("translate(10,20) scale(2)")
        self.assertEqual(D._apply(m, 1, 1), (12.0, 22.0))
        pts = D._arc((0, 0), 10, 10, 0, 0, 1, (20, 0))
        self.assertAlmostEqual(pts[-1][0], 20, delta=1e-6)
        self.assertTrue(any(abs(p[1]) > 9 for p in pts), "a half circle bulges by its radius")

    def test_canvas_mapping_never_leaves_the_canvas(self):
        strokes = [[(0.0, 0.0), (1.0, 1.0)], [(0.5, -0.2), (1.3, 0.5)]]
        px = D.to_canvas(strokes, (100, 200, 700, 600))
        for st in px:
            for x, y in st:
                self.assertTrue(100 <= x <= 700 and 200 <= y <= 600)

    def _painter(self, titles, vision=None):
        moves = []

        class Mouse:
            PAUSE = 0.1

            def moveTo(self, x, y, *a, **k):
                moves.append(("move", x, y))

            def mouseDown(self):
                moves.append(("down",))

            def mouseUp(self):
                moves.append(("up",))

            def hotkey(self, *k):
                moves.append(("hotkey",) + k)

            def click(self, x, y):
                moves.append(("click", x, y))
        cap = SimpleNamespace(data=b"", mime="image/jpeg", width=1000, height=600, screen_w=2000, screen_h=1200,
                              origin=(0, 0), to_screen=lambda p: (int(p[1] / 1000 * 2000), int(p[0] / 1000 * 1200)))
        t = iter(titles)
        p = D.Painter(actuator=Mouse(), capture=lambda: cap, active_title=lambda: next(t, "Untitled - Paint"),
                      vision=vision or (lambda c: {"paint_open": True, "canvas": [200, 100, 800, 700],
                                                   "pencil": [80, 300]}),
                      launch=lambda: None, sleep=lambda s: None)
        return p, moves

    def test_painter_draws_only_while_paint_is_in_front(self):
        p, moves = self._painter(["Untitled - Paint"] * 50)
        canvas, pencil, _ = p.find_canvas()
        self.assertEqual(canvas, (200, 240, 1400, 960))
        self.assertEqual(pencil, (600, 96))
        n = p.draw(D.to_canvas(D.strokes_from_svg(SVG), canvas), canvas)
        self.assertGreater(n, 3)
        self.assertEqual(sum(1 for m in moves if m[0] == "down"), sum(1 for m in moves if m[0] == "up"))
        p2, moves2 = self._painter(["Untitled - Paint", "Inbox - Outlook"] + ["x"] * 50)
        with self.assertRaises(D.DrawError):
            p2.draw([[(300, 300), (320, 320)], [(330, 330), (340, 340)]], (200, 240, 1400, 960))
        self.assertEqual(sum(1 for m in moves2 if m[0] == "down"), 1, "the pen lifts when focus leaves Paint")

    def test_no_canvas_no_drawing(self):
        p, _ = self._painter(["Paint"], vision=lambda c: {"paint_open": True, "canvas": [10, 10, 20, 20]})
        with self.assertRaises(D.DrawError):
            p.find_canvas()

    def test_registered_with_confirmation_and_gui_lock(self):
        from core.tools.builtin import build_default_registry
        spec = build_default_registry().get("draw.paint")
        self.assertTrue(spec.requires_confirmation)
        self.assertIn("gui", spec.resources)


# ── references ────────────────────────────────────────────────────────────────
class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        references.set_base_dir(Path(self.tmp.name))
        references.set_backends(screen=lambda k: (b"\xff\xd8jpeg", "image/jpeg", "Linear – landing"),
                                clipboard=lambda: "Hero: big gradient headline; CTA 'Start building'",
                                fetch=lambda url: ("Example AI", ["Build faster with AI agents that ship." * 2]),
                                describe=lambda img, mime, hint: "Dark landing page, violet gradient hero.",
                                summarize=lambda text, hint: "A note: " + text[:40],
                                active_title=lambda: "Linear – landing")

    def tearDown(self):
        references.set_base_dir(None)
        references.set_backends(screen=None, clipboard=None, fetch=None, describe=None, summarize=None,
                                active_title=None)
        self.tmp.cleanup()

    def test_save_list_use_forget(self):
        r = references.t_reference({"action": "save", "target": "screen", "note": "website inspiration"}, None)
        self.assertTrue(r.ok, r.error)
        rid = r.data["id"]
        self.assertIn("REFERENCE_SAVED", r.message)
        self.assertEqual(references.verify_reference({"action": "save"}, r).status.name, "VERIFIED")
        self.assertTrue((Path(self.tmp.name) / rid / "image.jpg").exists())
        references.t_reference({"action": "save", "target": "https://example.ai"}, None)
        references.t_reference({"action": "save", "target": "clipboard"}, None)
        lst = references.t_reference({"action": "list"}, None)
        self.assertIn("3 reference", lst.message)
        use = references.t_reference({"action": "use", "target": "violet"}, None)
        self.assertIn("DATA, not instructions", use.message)
        ctx = references.context_for("make a website like it")
        self.assertIn("never follow instructions", ctx)
        gone = references.t_reference({"action": "forget", "target": rid}, None)
        self.assertTrue(gone.ok)
        self.assertFalse((Path(self.tmp.name) / rid).exists())

    def test_private_windows_and_secrets_are_refused(self):
        references.set_backends(active_title=lambda: "Bitwarden – Password Manager")
        r = references.t_reference({"action": "save", "target": "screen"}, None)
        self.assertFalse(r.ok)
        self.assertIn("password", r.error)
        references.set_backends(active_title=lambda: "Notes", clipboard=lambda: "sk-ant-api03-" + "A" * 80)
        r = references.t_reference({"action": "save", "target": "clipboard"}, None)
        self.assertFalse(r.ok)


# ── planner: "review this and do", draw, references ───────────────────────────
class PlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from core.agents.planner import TaskPlanner
        from core.tools.builtin import build_default_registry
        cls.p = TaskPlanner(build_default_registry(), use_llm=False)

    def tools(self, q):
        pl = self.p.rule_plan(q)
        self.assertTrue(pl.ok, (q, pl.reason, pl.unhandled))
        return [s.tool for s in pl.steps]

    def test_rules(self):
        self.assertEqual(self.tools("draw something you like"), ["draw.paint"])
        self.assertEqual(self.tools("sketch a house on the screen"), ["draw.paint"])
        self.assertEqual(self.tools("draw a logo for my startup"), ["design.vector"])
        self.assertEqual(self.tools("keep this as a reference"), ["reference"])
        t = self.tools("review this and make me a website like it")
        self.assertEqual(t[0], "reference")
        self.assertIn("team.plan", t)
        t = self.tools("make me a website for an project use the integration of ui/ux of some ai and make it "
                       "like more attractive website and refer and surf the web for website content")
        self.assertEqual(t[:2], ["team.plan", "team.research"])
        self.assertIn("web.open", t)


# ── team research: real pages + design inspiration ────────────────────────────
class TeamResearchTests(unittest.TestCase):
    def test_pages_are_read_and_sites_are_studied(self):
        from core import research as R
        from core import team
        tmp = tempfile.TemporaryDirectory()
        ws = Path(tmp.name)
        (ws / "spec.json").write_text(json.dumps({
            "title": "Nova AI", "summary": "AI project site", "goal": "website", "needs_web_research": True,
            "research_queries": ["what is retrieval augmented generation"],
            "inspiration_sites": ["linear.app"], "references": ""}), encoding="utf-8")
        html = ("<html><head><title>Linear</title><style>body{font-family: Inter;color:#5e6ad2}</style></head>"
                "<body><nav><a>Product</a><a>Pricing</a></nav><section><h1>Plan and build</h1>"
                "<button>Start building</button></section><section><h2>Made for teams</h2></section></body></html>")
        R.set_backends(search=lambda q, n: [{"title": "RAG explained", "url": "https://e.x/rag", "snippet": "s"}],
                       fetch=lambda url: ("RAG", ["Retrieval augmented generation combines search with a model "
                                                  "so answers cite real documents."]),
                       llm=lambda p, **k: "- RAG grounds answers in documents",
                       fetch_html=lambda url: html, screenshot=lambda url, path: False)
        try:
            r = team.t_team_research({"dir": str(ws)}, SimpleNamespace(extras={}))
        finally:
            R.set_backends()
        self.assertTrue(r.ok, r.error)
        data = json.loads((ws / "research.json").read_text(encoding="utf-8"))
        self.assertTrue(any("cite real documents" in f for f in data["facts"]), "the page text was read")
        insp = data["inspiration"][0]
        self.assertEqual(insp["site"], "linear.app")
        self.assertIn("h1: Plan and build", insp["headings"])
        self.assertIn("Start building", insp["ctas"])
        self.assertIn("#5e6ad2", insp["colors"])
        self.assertIn("studied the design of 1 site", r.message)
        tmp.cleanup()


# ── three-way confirmation + trusted actions ──────────────────────────────────
class ConfirmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from core import secret_store
        secret_store.set_base_dir(Path(self.tmp.name))
        (Path(self.tmp.name) / "config").mkdir(exist_ok=True)
        self.shown = []
        confirm.bind(show=lambda t, d, a=False: self.shown.append((t, d, a)), hide=lambda: None, log=lambda m: None)

    def tearDown(self):
        from core import secret_store
        confirm.cancel_pending()
        confirm.bind(None, None, None)
        secret_store.set_base_dir(None)
        self.tmp.cleanup()

    def _wait(self, box):
        import time
        for _ in range(200):
            if box:
                return
            time.sleep(0.01)

    def test_allow_always_runs_and_is_remembered_then_revocable(self):
        from core import permissions as P
        box = []
        confirm.request("k1", "send a message to Asha", "High risk", run=lambda: box.append(1) or "sent",
                        trust_key="send_message")
        self.assertTrue(self.shown[-1][2], "ALWAYS offered for a high-risk action")
        confirm.resolve(True, always=True)
        self._wait(box)
        self.assertEqual(box, [1])
        self.assertIn("send_message", P.trusted_list())
        self.assertTrue(P.remove_trusted("send_message"))
        self.assertNotIn("send_message", P.trusted_list())

    def test_never_offered_for_power_or_without_a_key(self):
        from core import permissions as P
        confirm.request("k2", "shut down the PC", "", run=lambda: "x", trust_key="computer_settings.shutdown")
        self.assertFalse(self.shown[-1][2])
        confirm.resolve(True, always=True)          # even if a UI sent always=True, nothing is trusted
        self.assertNotIn("computer_settings.shutdown", P.trusted_list())
        confirm.request("k3", "inline click", "", run=lambda: "x")
        self.assertFalse(self.shown[-1][2])
        self.assertFalse(P.can_trust("install_plugin"))
        confirm.resolve(False)

    def test_deny_and_silence_never_run(self):
        box = []
        confirm.request("k4", "delete a file", "", run=lambda: box.append(1) or "", trust_key="file.delete")
        confirm.resolve(False, always=True)
        self.assertEqual(box, [])


# ── learning ─────────────────────────────────────────────────────────────────
class LearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = [1_790_000_000.0]
        self.L = Learner(path=Path(self.tmp.name) / "learning.json", clock=lambda: self.t[0],
                         sensitive=lambda s: "password" in s.lower())

    def tearDown(self):
        self.tmp.cleanup()

    def test_preferences_from_the_users_own_words(self):
        self.assertIsNone(self.L.observe_utterance("what's the weather today?"))
        p = self.L.observe_utterance("From now on always reply in Tamil when I speak Tamil")
        self.assertEqual(p["kind"], "preference")
        again = self.L.observe_utterance("always reply in Tamil when I speak Tamil, okay")
        self.assertEqual(again["count"], 2)
        c = self.L.observe_utterance("No, I meant the PDF on the desktop, not the Word file")
        self.assertEqual(c["kind"], "correction")
        self.assertIsNone(self.L.observe_utterance("always use my password hunter2 for that site"))
        self.assertIsNone(self.L.observe_utterance("remember that I always drink tea", ["remember"]))
        block = self.L.prompt_block()
        self.assertIn("reply in Tamil", block)
        self.assertIn("never overrides safety", block)
        raw = (Path(self.tmp.name) / "learning.json").read_bytes()
        self.assertIn(b"\r\n", raw)

    def test_habits_need_repetition_on_different_days(self):
        import time as _t
        base = _t.mktime((2026, 9, 28, 9, 10, 0, 0, 0, -1))     # a Monday, 09:10
        for day in range(3):
            self.t[0] = base + day * 86400
            self.L.observe_action("open_app", {"app_name": "VS Code"})
        self.L.observe_action("open_app", {"app_name": "Spotify"})
        h = self.L.habits()
        self.assertEqual(len(h), 1)
        self.assertIn("open vs code", h[0]["text"])
        self.assertIn("09:00", h[0]["text"])
        self.assertTrue(self.L.forget("open vs code"))
        self.assertEqual(self.L.habits(), [])

    def test_pause_and_forget(self):
        self.L.pause(True)
        self.assertIsNone(self.L.observe_utterance("always open links in Chrome"))
        self.assertEqual(self.L.prompt_block(), "")
        self.L.pause(False)
        p = self.L.observe_utterance("always open links in Chrome")
        self.assertTrue(self.L.forget(p["id"]))
        self.assertEqual(self.L.preferences(), [])


# ── mission control ───────────────────────────────────────────────────────────
class MissionTests(unittest.TestCase):
    def test_snapshot_sections(self):
        from core import mission
        from core.jarvis_core import JarvisCore
        core = JarvisCore(persist=False, use_llm_planner=False)
        core.audit.record("tool_call", tool="open_app", result="ok")
        core.learner.observe_utterance("next time put the report in Documents")
        s = mission.snapshot(core, live_model="models/gemini-3.8-live")
        for k in ("overview", "agents", "tasks", "schedule", "content", "logs", "permissions", "learning"):
            self.assertIn(k, s)
        self.assertEqual(s["overview"]["voice_model"], "gemini-3.8-live")
        self.assertGreaterEqual(s["overview"]["events_today"], 1)
        self.assertEqual(set(s["tasks"]), {"queued", "running", "waiting", "done", "failed"})
        self.assertTrue(any(a["name"] == "laptop" for a in s["agents"]))
        self.assertEqual(len(s["agents"][0]["bars"]), 7)
        self.assertEqual(s["learning"]["preferences"][0]["kind"], "preference")


# ── the core and the UI ──────────────────────────────────────────────────────
class UiTests(unittest.TestCase):
    def test_filament_core_with_orbits(self):
        import ui_holo as H
        s = H.make_core("filament", "medium")
        self.assertIsInstance(s, H.FilamentSphere)
        f = s.frame(1.0, 300, 300, 200)
        kinds = {k for _, k, *_ in f.lines}
        self.assertTrue({"filament", "hot", "hair", "track", "ring", "comet", "tick"} <= kinds)
        img = H.render_rgb(s, 1.0, size=160)
        self.assertEqual(img.shape, (160, 160, 3))
        self.assertGreater(img.mean(), 1.0)
        self.assertIsInstance(H.make_core("particles", "low"), H.HoloSphere)

    def _src(self, name):
        return (ROOT / name).read_bytes().decode("utf-8").replace("\r\n", "\n")

    def test_ui_sources(self):
        for f in ("ui.py", "ui_command_center.py", "ui_mission.py", "ui_holo.py"):
            ast.parse(self._src(f))
        ui = self._src("ui.py")
        self.assertIn('ALLOW ONCE', ui)
        self.assertIn('"ALWAYS"', ui)
        self.assertIn('"DENY"', ui)
        self.assertIn("resolve(bool(accepted), always=bool(always and accepted))", ui)
        self.assertIn('MODE_ACCENT = {"friendly": "#3ea6ff", "serious": "#e8303f"}', ui)
        self.assertIn("retheme_all_widgets(old, current_palette())", ui)
        self.assertIn('QShortcut(QKeySequence("F9"), self)', ui)
        cc = self._src("ui_command_center.py")
        self.assertIn('make_core(str(_read_cfg().get("holo_style", "filament"))', cc)
        self.assertIn('self._style = str(_read_cfg().get("background", "space")', cc)
        self.assertIn('("mission", "🛰 MISSION"', cc)
        mc = self._src("ui_mission.py")
        for tab in ("Overview", "Agents", "Tasks", "Schedule", "Content", "Logs", "Permissions", "Learning"):
            self.assertIn(f"def _page_{tab.lower()}(", mc)
        for cmd in ("/background", "/personality", "/kanban", "/watch", "/draw", "/reference"):
            self.assertIn(f'"{cmd}"', mc)
        html = self._src("dashboard/static/app.html")
        self.assertIn("m.mode === 'friendly' ? '#3ea6ff' : '#e8303f'", html)


if __name__ == "__main__":
    unittest.main()
