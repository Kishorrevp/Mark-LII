"""Phase 5 — Cognitive Core: self-check (the "did I actually do it?" rule),
learning into episodic memory, and think() with self-critique.

Verification level: GENUINE EXECUTION against temporary folders; the model
is a scripted fake.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.cognition import CognitiveCore                              # noqa: E402
from core.tasks.state import Step, StepStatus, Task, TaskState        # noqa: E402
from memory.memory_hub import MemoryHub                               # noqa: E402


def task(state=TaskState.COMPLETED, steps=(), recoveries=0):
    t = Task(id="T1", request="Research vector databases and write a report")
    t.state, t.steps, t.recoveries = state, list(steps), recoveries
    return t


def step(sid, status=StepStatus.DONE, v=None, output=None, tool="document.write"):
    s = Step(sid, f"step {sid}", tool, status=status, output=output or {})
    s.verification = v or {}
    return s


class SelfCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_verified_needs_real_evidence(self):
        f = self.tmp / "report.docx"
        f.write_bytes(b"PK" + b"x" * 500)
        c = CognitiveCore.self_check(task(steps=[
            step("s1", v={"status": "VERIFIED", "detail": "12 sources stored."}),
            step("s2", output={"path": str(f)}, v={"status": "VERIFIED", "detail": "Valid Word document"})]))
        self.assertEqual(c.verdict, "verified")
        self.assertTrue(any("report.docx exists" in e for e in c.evidence))
        self.assertIn("[SELF-CHECK: verified", c.line())

    def test_file_deleted_after_the_fact_is_caught(self):
        c = CognitiveCore.self_check(task(steps=[step("s1", output={"path": str(self.tmp / "gone.docx")},
                                                      v={"status": "VERIFIED", "detail": "ok"})]))
        self.assertNotEqual(c.verdict, "verified")
        self.assertIn("gone.docx is missing now", c.gaps)

    def test_unverifiable_and_failed(self):
        c = CognitiveCore.self_check(task(steps=[step("s1", v={"status": "UNVERIFIABLE", "detail": "x"},
                                                      tool="file.open")]))
        self.assertEqual(c.verdict, "unverified")
        f = CognitiveCore.self_check(task(state=TaskState.FAILED))
        f.gaps  # noqa
        self.assertEqual(f.verdict, "failed")
        sup = CognitiveCore.self_check(task(steps=[step("s1", status=StepStatus.SUPERSEDED),
                                                   step("r1", v={"status": "VERIFIED", "detail": "alt ok"})],
                                            recoveries=1))
        self.assertEqual(sup.verdict, "verified")
        self.assertIn("completed after one self-recovery", sup.evidence)


class LearnAndThinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.hub = MemoryHub(base_dir=self.tmp)
        self.hub._mm = staticmethod(lambda: SimpleNamespace(load_memory=lambda: {}, update_memory=lambda u: None))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_learn_writes_an_episode_linked_to_the_project(self):
        proj = self.tmp / "folio"
        proj.mkdir()
        self.hub.identity.register_project("Folio", str(proj))
        cc = CognitiveCore(memory=self.hub)
        t = task(steps=[step("s1", output={"path": str(proj / "index.html")})])
        t.result = "Built the site."
        cc.learn(t)
        ep = self.hub.episodic.recent(1)[0]
        self.assertEqual((ep["kind"], ep["project"], ep["outcome"]), ("task", "Folio", "done"))
        self.assertEqual(ep["refs"]["task_id"], "T1")

    def test_think_self_critique_revises_a_wrong_deep_answer(self):
        calls = []

        def llm(prompt, **kw):
            calls.append(kw.get("tier"))
            if kw.get("tier") == "fast":
                return SimpleNamespace(text="The total is wrong: 17*23 is 391, not 381.", meta={})
            if kw.get("tier") == "normal":
                return SimpleNamespace(text="Corrected: 17 × 23 = 391, so the budget is 391.", meta={})
            return SimpleNamespace(text="Step by step... 17 × 23 = 381.", meta={"tier": "deep", "route": {}})
        cc = CognitiveCore(llm=llm)
        t = cc.think("Analyse my budget step by step: 17 boxes at 23 each", use_context=False)
        self.assertTrue(t.revised)
        self.assertIn("391", t.text)
        self.assertEqual(calls, ["auto", "fast", "normal"])

    def test_think_keeps_a_good_answer(self):
        def llm(prompt, **kw):
            if kw.get("tier") == "fast":
                return SimpleNamespace(text="OK", meta={})
            return SimpleNamespace(text="A thorough, correct answer.", meta={"tier": "deep"})
        t = CognitiveCore(llm=llm).think("compare X and Y in depth", use_context=False)
        self.assertFalse(t.revised)
        self.assertEqual(t.text, "A thorough, correct answer.")


if __name__ == "__main__":
    unittest.main()
