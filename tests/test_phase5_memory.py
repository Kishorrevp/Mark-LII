"""Phase 5 — semantic / episodic / procedural memory, personal model, memory
policy and the context engine.

Level of verification (stated honestly): GENUINE EXECUTION against temporary
directories — real files are written, re-read and searched. The only thing
faked is the clock (so "yesterday" is deterministic) and the foreground-window
probe (Windows-only ctypes).
"""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.context_engine import ContextEngine                    # noqa: E402
from memory import policy                                         # noqa: E402
from memory.episodic import EpisodicStore, resolve_period         # noqa: E402
from memory.identity import PersonalModel                         # noqa: E402

NOW = datetime(2026, 9, 27, 10, 0, 0)          # a Sunday


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def ts(self):
        return self.dt.timestamp()

    def now(self):
        return self.dt


class FakeTasks:
    def __init__(self, active=()):
        self._a = list(active)

    def active(self):
        return self._a

    def all(self):
        return self._a


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.lt = {"identity": {"name": {"value": "Kishorre"}, "city": {"value": "Coimbatore"}},
                   "preferences": {"favorite_editor": {"value": "VS Code"}},
                   "projects": {"portfolio_site": {"value": "personal site in C:\\Users\\K\\Sites\\folio"}},
                   "relationships": {}, "wishes": {}, "notes": {}}
        self.clock = Clock(NOW)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def model(self):
        return PersonalModel(self.dir, memory_loader=lambda: self.lt)


class EpisodicTests(_Base):
    def test_add_search_period_and_persistence(self):
        clk = Clock(NOW - timedelta(days=1, hours=-11))       # yesterday 21:00
        ep = EpisodicStore(self.dir / "episodes.jsonl", clock=clk.ts)
        ep.add("task", "Built the Mark LIII interface", "glass panels, arc reactor", project="JARVIS",
               refs={"paths": ["C:/Users/K/Desktop/Mark-LII-main/ui_command_center.py"]}, outcome="done")
        clk.dt = NOW - timedelta(days=3)
        ep.add("session", "Talked about flights to Chennai")
        again = EpisodicStore(self.dir / "episodes.jsonl", clock=self.clock.ts)   # re-read from disk
        self.assertEqual(len(again.all()), 2)
        per = resolve_period("what did we do yesterday", now=NOW)
        hits = again.search("", period=per[:2])
        self.assertEqual([h[1]["title"] for h in hits], ["Built the Mark LIII interface"])
        self.assertEqual(again.last_project(per[:2]), "JARVIS")
        self.assertEqual(again.search("chennai flights")[0][1]["kind"], "session")
        raw = (self.dir / "episodes.jsonl").read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))                  # CRLF on disk

    def test_secrets_never_reach_disk(self):
        ep = EpisodicStore(self.dir / "e.jsonl", clock=self.clock.ts)
        ep.add("event", "Logged in with password hunter2 for the bank", "card 4111 1111 1111 1111")
        text = (self.dir / "e.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("hunter2", text)
        self.assertNotIn("4111 1111", text)

    def test_coalesce_retention_forget(self):
        ep = EpisodicStore(self.dir / "e.jsonl", clock=self.clock.ts)
        a = ep.add("event", "Opened Chrome", coalesce_s=600)
        ep.add("event", "Opened Chrome", coalesce_s=600)
        self.assertEqual(len(ep.all()), 1)
        self.assertEqual(ep.all()[0]["count"], 2)
        self.assertTrue(ep.forget(a["id"]))
        self.assertEqual(ep.all(), [])

    def test_periods(self):
        self.assertEqual(resolve_period("last week", now=NOW)[2], "last week")
        s, e, _ = resolve_period("3 days ago", now=NOW)
        self.assertEqual(s.date(), (NOW - timedelta(days=3)).date())
        self.assertEqual(resolve_period("on friday", now=NOW)[0].date(), datetime(2026, 9, 25).date())
        self.assertEqual(resolve_period("நேற்று என்ன செய்தோம்", now=NOW)[2], "yesterday")
        self.assertIsNone(resolve_period("open chrome", now=NOW))


class PolicyTests(_Base):
    def test_decisions(self):
        ev = policy.evaluate
        self.assertEqual(ev("notes", "wifi_password", "abc12345").action, "reject")
        self.assertEqual(ev("notes", "mood", "tired today").action, "episodic")
        self.assertEqual(ev("notes", "meeting", "with Ravi at 4 pm").action, "episodic")
        self.assertEqual(ev("notes", "condition", "has diabetes").action, "ask")
        self.assertEqual(ev("notes", "condition", "has diabetes", explicit=True).action, "store")
        self.assertEqual(ev("notes", "x", "yes").action, "reject")
        self.assertEqual(ev("identity", "city", "Coimbatore", existing=self.lt).action, "duplicate")
        d = ev("identity", "city", "Chennai", existing=self.lt)
        self.assertEqual((d.action, d.previous), ("store", "Coimbatore"))
        self.assertEqual(ev("identity", "city", "I currently live in Chennai").action, "store")

    def test_apply_routes_to_the_right_store(self):
        written = []
        ep = EpisodicStore(self.dir / "e.jsonl", clock=self.clock.ts)
        msg = policy.apply(policy.evaluate("notes", "mood", "stressed today"), episodic=ep,
                           update_memory=written.append)
        self.assertIn("episode", msg)
        self.assertEqual(written, [])
        self.assertEqual(len(ep.all()), 1)
        policy.apply(policy.evaluate("preferences", "Favourite Food", "dosa"), update_memory=written.append)
        self.assertEqual(written, [{"preferences": {"favourite_food": {"value": "dosa"}}}])


class IdentityTests(_Base):
    def test_projects_apps_profile(self):
        m = self.model()
        m.register_project("JARVIS", r"C:\Users\K\Desktop\Mark-LII-main", "voice assistant",
                           stack=["python", "pyqt6"], aliases=["mark lii"], source="task")
        self.assertEqual(m.match_project(r"C:\Users\K\Desktop\Mark-LII-main\ui.py"), "JARVIS")
        self.assertEqual(m.match_project("open the mark lii folder"), "JARVIS")
        self.assertEqual(m.match_project(r"C:\Users\K\Sites\folio\index.html"), "portfolio site")
        self.assertIsNone(m.match_project("make me a sandwich"))
        for a in ("chrome", "code", "chrome"):
            m.note_app(a)
        self.assertEqual(m.frequent_apps(2), ["chrome", "code"])
        prof = m.profile()
        self.assertEqual(prof["user"]["name"], "Kishorre")
        self.assertIn("JARVIS", prof["projects"])
        self.assertIn("User: Kishorre", m.summary_line())
        self.assertIn("Forgot", m.forget("frequent_apps", "chrome"))
        raw = (self.dir / "memory" / "identity.json").read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))


class ContextEngineTests(_Base):
    def _engine(self, window=None, tasks=()):
        clk = Clock(NOW - timedelta(hours=13))                  # yesterday 21:00
        ep = EpisodicStore(self.dir / "e.jsonl", clock=clk.ts)
        ep.add("task", "Redesigned the JARVIS interface", "Mark LIII glass UI", project="JARVIS",
               refs={"paths": [r"C:\Users\K\Desktop\Mark-LII-main\ui_command_center.py"]}, outcome="done")
        clk.dt = NOW - timedelta(days=5)
        ep.add("task", "Updated portfolio colours", project="portfolio site")
        m = self.model()
        m.register_project("JARVIS", r"C:\Users\K\Desktop\Mark-LII-main", "voice assistant", source="task")
        return ContextEngine(episodic=ep, identity=m, task_store=FakeTasks(tasks), clock=self.clock.now,
                             window_probe=lambda: window or {})

    def test_continue_yesterdays_project(self):
        b = self._engine().build("Continue the project we worked on yesterday")
        self.assertEqual(b.project, "JARVIS")
        self.assertEqual(b.period, "yesterday")
        self.assertIn(r"C:\Users\K\Desktop\Mark-LII-main", b.text)
        self.assertIn("Redesigned the JARVIS interface", b.text)
        self.assertNotIn("portfolio", b.text)          # irrelevant project is left out

    def test_minimal_and_budgeted(self):
        e = self._engine(window={"title": "Inbox - Gmail", "process": "chrome.exe"})
        b = e.build("what's 2+2")
        self.assertNotIn("Gmail", b.text)             # window only when the request refers to it
        self.assertLess(len(b.text), 400)
        b2 = e.build("what is on this window right now")
        self.assertIn("Gmail", b2.text)
        b3 = e.build("continue the project we worked on yesterday", budget=150)
        self.assertLessEqual(len(b3.text), 150 + 60)
        self.assertGreater(b3.dropped, 0)

    def test_self_query_uses_semantic_memory(self):
        b = self._engine().build("what do you know about me")
        self.assertIn("Kishorre", b.text)
        self.assertIn("VS Code", b.text)

    def test_live_block(self):
        class T:
            id, request, state, pending_question = "t1", "research laptops", type("S", (), {"value": "RUNNING"})(), ""
        blk = self._engine(tasks=[T()]).live_block()
        self.assertIn("Kishorre", blk)
        self.assertIn("recall_context", blk)
        self.assertIn("research laptops", blk)
        self.assertLess(len(blk), 720)


if __name__ == "__main__":
    unittest.main()
