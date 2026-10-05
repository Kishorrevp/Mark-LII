"""Phase 5 — long-running research, document writing, project analysis and the
multi-agent build team.

Verification level: GENUINE EXECUTION of the real tools, planner rules and
orchestrator against temporary folders. Only the network and the model are
faked (search results, page HTML, model text are scripted). The Testing
Agent's checks are real: html.parser, bracket scanning, py_compile and — if
Node.js is installed — `node --check`.
"""
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import research as R, secret_store, team as T               # noqa: E402
from core.agents.builtin import default_agents                        # noqa: E402
from core.agents.orchestrator import Orchestrator                     # noqa: E402
from core.agents.planner import TaskPlanner                           # noqa: E402
from core.permissions import PermissionEngine, PermissionPolicy       # noqa: E402
from core.safety import KillSwitch                                    # noqa: E402
from core.tasks.state import TaskState, TaskStore                     # noqa: E402
from core.tools.builtin import build_default_registry                 # noqa: E402
from core.tools.registry import ToolContext                           # noqa: E402

PAGE = """<html><head><title>Vector DB guide</title><script>var x=1;</script></head><body>
<nav>menu menu menu</nav><p>Qdrant is an open-source vector database written in Rust with a free self-hosted
edition and filtering support that is fast.</p><p>Chroma is a lightweight open-source embedding database that is
popular for prototypes and local development with Python.</p><footer>cookie banner text</footer></body></html>"""


class Confirm:
    def request(self, key, title, detail, on_approve, on_deny):
        on_approve()

    def cancel(self, key):
        pass


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "Documents").mkdir()
        secret_store.set_base_dir(self.tmp / "jarvis")
        self._docs = R.documents_dir
        R.documents_dir = lambda: self.tmp / "Documents"
        self.prompts = []

    def tearDown(self):
        R.documents_dir = self._docs
        R.set_backends()
        T.set_structured(None)
        secret_store.set_base_dir(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def orch(self):
        reg = build_default_registry()
        o = Orchestrator(reg, PermissionEngine(reg, policy=PermissionPolicy()), default_agents(),
                         TaskPlanner(reg, use_llm=False), TaskStore(persist=False), confirmer=Confirm(),
                         sleep=lambda s: None, kill=KillSwitch(), recovery=False)
        return o, reg


class ResearchPipelineTests(_Env):
    def test_research_and_write_a_report_end_to_end(self):
        def search(q, n):
            return [{"title": f"Result for {q}", "snippet": "vector database comparison", "url": f"https://ex{i}.com/{len(q)}"}
                    for i in range(2)]

        def llm(prompt, tier="normal", **kw):
            self.prompts.append((tier, prompt))
            if tier == "fast":
                return "best vector db 2026\nqdrant vs chroma"
            return ("# Free vector databases\n## Summary\nQdrant and Chroma are the main free options [1][2].\n"
                    "## Key findings\n- Qdrant is written in Rust [1]\n- Chroma suits prototypes [2]\n"
                    "- A made-up claim [9]\n## Recommendations\n- Start with Chroma, move to Qdrant [1]\n")
        R.set_backends(search=search, fetch=lambda url: R.html_to_text(PAGE), llm=llm)
        o, _ = self.orch()
        t = o.submit("Research the best free vector databases and write a report", background=False)
        self.assertEqual(t.state, TaskState.COMPLETED, t.error)
        self.assertEqual(t.verification_status, "VERIFIED")
        self.assertEqual([s.tool for s in t.steps], ["research.collect", "research.analyze", "document.write"])
        out = t.steps[-1].output["path"]
        self.assertTrue(out.endswith(".docx"))
        with zipfile.ZipFile(out) as z:
            body = z.read("word/document.xml").decode()
        self.assertIn("Qdrant", body)
        analysis = t.steps[1].output["analysis"]
        self.assertNotIn("[9]", analysis)                        # self-check removed a fake citation
        self.assertEqual(t.steps[1].output["invalid_citations_removed"], [9])
        self.assertIn("## Sources", analysis)
        # tiers: sub-queries on the fast model, synthesis on deep
        self.assertEqual([p[0] for p in self.prompts], ["fast", "deep"])
        # page text was reduced to content (no nav / scripts)
        src = json.loads(Path(t.steps[0].output["path"]).read_text(encoding="utf-8"))
        self.assertIn("Qdrant", src["sources"][0]["excerpt"])
        self.assertNotIn("menu menu", src["sources"][0]["excerpt"])
        # stored + remembered
        ws = Path(t.steps[0].output["dir"])
        self.assertTrue((ws / "analysis.md").exists() and (ws / "sources.md").exists())
        from memory.episodic import EpisodicStore
        self.assertTrue(any("Research brief" in e["title"] for e in EpisodicStore().all()))

    def test_offline_search_fails_cleanly(self):
        R.set_backends(search=lambda q, n: [], fetch=lambda u: ("", []), llm=lambda p, **k: "q1")
        r = R.t_research_collect({"topic": "x"}, ToolContext())
        self.assertFalse(r.ok)
        self.assertTrue(r.retryable)

    def test_public_url_guard(self):
        for bad in ("http://localhost:8000", "http://127.0.0.1/", "http://192.168.1.10/admin", "file:///etc/passwd",
                    "ftp://example.com", "http://10.0.0.5"):
            self.assertFalse(R.is_public_url(bad), bad)

    def test_docx_writer_is_valid(self):
        p = R.markdown_to_docx("# T\n## Part\n- **bold** item\nplain & <text>\n| a | b |\n|---|---|\n| 1 | 2 |",
                               self.tmp / "x.docx", title="T")
        from core.tools.registry import ToolResult
        v = R.verify_document({}, ToolResult(True, {"path": str(p)}))
        self.assertTrue(v.ok, v.detail)
        with zipfile.ZipFile(p) as z:
            self.assertIn("&amp; &lt;text&gt;", z.read("word/document.xml").decode())


class ProjectAnalysisTests(_Env):
    def test_analyze_skips_secrets_and_writes_outside_the_project(self):
        proj = self.tmp / "myapp"
        (proj / "src").mkdir(parents=True)
        (proj / "node_modules" / "junk").mkdir(parents=True)
        (proj / "README.md").write_text("# MyApp\nA todo app.", encoding="utf-8")
        (proj / "main.py").write_text("import os\nKEY='AIzaSyA1234567890abcdefghijklmnopqrstuv'\nprint('hi')\n",
                                      encoding="utf-8")
        (proj / "src" / "util.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (proj / ".env").write_text("GOOGLE_API_KEY=supersecretvalue", encoding="utf-8")
        (proj / "node_modules" / "junk" / "a.js").write_text("x", encoding="utf-8")
        seen = {}

        def llm(prompt, tier="normal", **kw):
            seen["prompt"], seen["tier"] = prompt, tier
            return "# myapp — project analysis\n## What it is\nA small todo app.\n## Risks & bugs\n- hard-coded key" + "x" * 200
        R.set_backends(llm=llm)
        before = sorted(p.name for p in proj.rglob("*"))
        r = R.t_project_analyze({"path": str(proj)}, ToolContext())
        self.assertTrue(r.ok, r.error)
        self.assertEqual(seen["tier"], "deep")
        self.assertNotIn("supersecretvalue", seen["prompt"])
        self.assertNotIn("AIzaSyA1234567890", seen["prompt"])      # redacted inside main.py
        self.assertNotIn("node_modules", seen["prompt"])
        self.assertIn("main.py", r.data["reviewed_files"])
        self.assertEqual(sorted(p.name for p in proj.rglob("*")), before)   # project untouched
        self.assertTrue(Path(r.data["path"]).exists())
        self.assertNotIn(str(proj), r.data["path"])
        from memory.identity import PersonalModel
        self.assertEqual(PersonalModel().match_project(str(proj / "main.py")), "myapp")

    def test_no_path_asks(self):
        r = R.t_project_analyze({}, ToolContext())
        self.assertFalse(r.ok)
        self.assertTrue(r.data.get("needs_input"))


BROKEN = {
    "index.html": "<!doctype html><html lang='en'><head><meta name='viewport' content='width=device-width'>"
                  "<title>Site</title><link rel='stylesheet' href='styles.css'></head><body><main><div>"
                  "<h1>Hi</h1></main><script src='app.js'></script><img src='logo.png'></body></html>",
    "styles.css": "body { color: red; }\nmain { padding: 1rem; ",
    "app.js": "function go( { console.log('x') }",
}
FIXED = {
    "index.html": "<!doctype html><html lang='en'><head><meta name='viewport' content='width=device-width'>"
                  "<title>Site</title><link rel='stylesheet' href='styles.css'></head><body><main><div>"
                  "<h1>Hi</h1></div></main><script src='app.js'></script></body></html>",
    "styles.css": "body { color: red; }\nmain { padding: 1rem; }\n",
    "app.js": "function go() { console.log('x'); }\ngo();\n",
}


def as_files(d):
    return "\n".join(f"===== FILE: {k} =====\n{v}" for k, v in d.items())


class TeamTests(_Env):
    def test_testing_agent_finds_real_problems(self):
        ws = self.tmp / "w"
        ws.mkdir()
        T.write_files(ws, BROKEN)
        rep = T.run_tests(ws, {"files": list(BROKEN) + ["about.html"]})
        text = " ".join(rep["errors"])
        self.assertFalse(rep["passed"])
        self.assertIn("about.html: listed in the spec", text)
        self.assertIn("<div>", text)
        self.assertIn("logo.png", text)
        self.assertIn("styles.css", text)
        self.assertIn("app.js", text)
        T.write_files(ws, FIXED)
        (ws / "about.html").write_text("<!doctype html><html lang='en'><head><title>a</title></head><body></body>"
                                       "</html>", encoding="utf-8")
        rep2 = T.run_tests(ws, {"files": list(FIXED) + ["about.html"]})
        self.assertTrue(rep2["passed"], rep2["errors"])

    def test_workspace_sandbox(self):
        ws = self.tmp / "w"
        ws.mkdir()
        written, rejected = T.write_files(ws, {"../evil.html": "x", "C:/Windows/x.js": "x", "ok.exe": "x",
                                               "sub/ok.html": "<p>ok</p>"})
        self.assertEqual(written, ["sub/ok.html"])
        self.assertEqual(len(rejected), 3)
        self.assertFalse((self.tmp / "evil.html").exists())

    def test_build_me_a_website_full_team(self):
        calls = []

        def structured(prompt, schema, tier):
            calls.append(("structured", schema.get("required", [])[0]))
            if "files" in schema["properties"] and "entry" in schema["properties"]:
                return {"title": "Kishorre Portfolio", "kind": "website", "summary": "Portfolio for a developer",
                        "files": ["index.html", "styles.css", "app.js"], "entry": "index.html",
                        "sections": ["hero", "projects"], "acceptance": ["index.html links styles.css"],
                        "needs_web_research": False}
            if "palette" in schema["properties"]:
                return {"mood": "calm", "palette": {"primary": "#0b6efd", "background": "#0d1117"},
                        "fonts": {"heading": "Inter", "body": "Inter"}, "layout": ["hero", "grid"]}
            if len([c for c in calls if c == ("structured", "verdict")]) == 1:
                return {"verdict": "fix", "summary": "Broken build", "issues": [
                    {"file": "index.html", "problem": "unclosed div", "fix": "close it"}]}
            return {"verdict": "pass", "summary": "Looks good", "issues": []}

        def llm(prompt, tier="normal", **kw):
            calls.append(("llm", tier))
            return as_files(BROKEN) if "Implement the whole project" in prompt else as_files(FIXED)
        T.set_structured(structured)
        R.set_backends(llm=llm)
        o, reg = self.orch()
        plan = TaskPlanner(reg, use_llm=False).plan("Build me a website for my portfolio with a projects "
                                                    "grid and a contact section")
        self.assertEqual([s.agent for s in plan.steps],
                         ["manager", "research", "ui", "coding", "testing", "review", "webdev", "webdev"])
        # run only the build steps here (web.run/web.open need Playwright + a live server;
        # they are covered end-to-end in test_phase6_webdev)
        build = [s for s in plan.steps if s.tool.startswith("team.")]
        t = o.submit_steps("website", build, background=False)
        self.assertEqual(t.state, TaskState.COMPLETED, t.error)
        rv = t.steps[-1].output
        self.assertTrue(rv["passed"])
        self.assertEqual(rv["rounds"], 1)                        # one fix round was needed
        ws = Path(rv["dir"])
        for f in ("spec.json", "design.json", "research.json", "test_report.json", "review.md", "index.html"):
            self.assertTrue((ws / f).exists(), f)
        self.assertTrue(str(ws).startswith(str(self.tmp / "Documents" / "JARVIS Projects")))
        self.assertIn("</div></main>", (ws / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(t.steps[-1].verification["status"], "VERIFIED")

    def test_review_that_cannot_fix_fails_verification(self):
        T.set_structured(lambda p, s, tier: (
            {"title": "X", "kind": "website", "summary": "x", "files": ["index.html", "app.js"], "entry": "index.html"}
            if "entry" in s["properties"] else {"palette": {"primary": "#000"}, "fonts": {}, "layout": []}
            if "palette" in s["properties"] else {"verdict": "fix", "summary": "still broken", "issues": []}))
        R.set_backends(llm=lambda p, tier="normal", **k: as_files({"index.html": BROKEN["index.html"],
                                                                     "app.js": BROKEN["app.js"]}))
        o, reg = self.orch()
        steps = [s for s in TaskPlanner(reg, use_llm=False).plan("build me a website").steps if s.tool != "file.open"]
        t = o.submit_steps("website", steps, background=False)
        self.assertEqual(t.state, TaskState.FAILED)
        self.assertIn("problem(s) remain", t.error)                 # honest: not reported as done


if __name__ == "__main__":
    unittest.main()
