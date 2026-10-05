"""
core/code_agent.py — the Coding Agent as an edit-and-verify loop.

    code.task(request, project=…)   change code in an EXISTING project and prove it works:

      1. LOCATE   scan the project, pick the handful of files the request is
                  about (core/research.scan_project + a relevance pass).
      2. EDIT     ask the deep model for exact search/replace edit blocks
                  (not whole-file rewrites), so a change is small and auditable.
      3. APPLY    each edit must match exactly once; before writing, the file's
                  current bytes are kept for undo (push_undo, kind="edit") and a
                  .bak is written. New files are created inside the project only.
      4. TEST     run the project's own tests if it has them (pytest / npm test /
                  a named command), else a syntax check of every file touched
                  (py_compile / node --check / json parse).
      5. FIX      if tests fail, feed the failure back for another edit — at most
                  `max_rounds` (default 3) rounds — then stop and report honestly.

    Nothing is executed except the project's declared test command (and only
    with a visible, workspace-scoped, timeout-bounded run); source files outside
    the project are never touched; secrets are never read into the prompt
    (research.scan_project already skips .env / api_keys.json).

"Done" means the tests (or, if there are none, the syntax checks) passed after
the edits — reported as VERIFIED; if they still fail after the last round the
task FAILS with the remaining error, never a false success.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from core import research as R
from core.tools.registry import ToolResult

MAX_ROUNDS = 3
MAX_EDIT_FILES = 12
CODE_EXT = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".md", ".txt", ".svg",
            ".go", ".rs", ".java", ".cs", ".rb", ".php", ".sh", ".yml", ".yaml", ".toml", ".cfg", ".ini"}
MAX_FILE_BYTES = 500_000

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "edits": {"type": "array", "items": {"type": "object", "properties": {
            "file": {"type": "string"},
            "action": {"type": "string", "enum": ["replace", "create", "append"]},
            "find": {"type": "string"},
            "replacement": {"type": "string"},
        }, "required": ["file", "action"]}},
        "done": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["edits"],
}


def _relevant_files(scan: dict, request: str, budget: int = 40000) -> list[tuple[str, str]]:
    """Files most likely relevant to the request (name + content keyword hits),
    with their current text, within a token budget."""
    root = Path(scan["path"])
    words = {w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", request.lower()) if len(w) > 2}
    scored: list[tuple[float, str, int]] = []
    for rel, size, ext in scan["file_list"]:
        if ext not in CODE_EXT or size > MAX_FILE_BYTES:
            continue
        name = rel.lower()
        score = sum(2 for w in words if w in name)
        scored.append((score, rel, size))
    # keyword hits inside file contents for the top-by-name and entry files
    from core.secret_store import redact
    out: list[tuple[str, str]] = []
    used = 0
    prio = sorted(scored, key=lambda x: (-x[0], x[2]))
    for _score, rel, _size in prio:
        if used >= budget or len(out) >= MAX_EDIT_FILES:
            break
        try:
            txt = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = sum(1 for w in words if w in txt.lower())
        if _score == 0 and hits == 0 and len(out) >= 3:
            continue
        clip = redact(txt[: min(12000, budget - used)])
        out.append((rel, clip))
        used += len(clip)
    return out


def _apply_edit(root: Path, edit: dict) -> tuple[bool, str]:
    rel = str(edit.get("file") or "").replace("\\", "/").lstrip("/")
    action = str(edit.get("action") or "replace")
    if ".." in rel.split("/") or Path(rel).is_absolute():
        return False, f"{rel}: unsafe path"
    target = (root / rel).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        return False, f"{rel}: outside the project"
    if target.suffix.lower() and target.suffix.lower() not in CODE_EXT:
        return False, f"{rel}: {target.suffix} files aren't edited"
    from core.undo import push_undo
    if action == "create":
        if target.exists():
            return False, f"{rel}: already exists (use replace)"
        body = str(edit.get("replacement") or "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        push_undo(f"created {rel}", lambda p=target: (p.unlink() if p.exists() else None, f"Removed {rel}.")[1],
                  kind="create", paths=[str(target)])
        return True, f"created {rel}"
    if not target.is_file():
        return False, f"{rel}: no such file"
    original = target.read_text(encoding="utf-8", errors="ignore")
    if action == "append":
        new = original + ("\n" if not original.endswith("\n") else "") + str(edit.get("replacement") or "")
    else:
        find = str(edit.get("find") or "")
        if not find:
            return False, f"{rel}: a replace needs 'find' text"
        n = original.count(find)
        if n == 0:
            # tolerate leading/trailing-whitespace drift
            stripped = find.strip("\n")
            n = original.count(stripped) if stripped else 0
            find = stripped if n == 1 else find
        if n == 0:
            return False, f"{rel}: the text to replace wasn't found"
        if n > 1:
            return False, f"{rel}: the text to replace appears {n} times (make it unique)"
        new = original.replace(find, str(edit.get("replacement") or ""), 1)
    if new == original:
        return False, f"{rel}: no change"
    bak = target.with_suffix(target.suffix + ".bak")
    try:
        shutil.copy2(target, bak)
    except Exception:
        bak = None
    target.write_text(new, encoding="utf-8")
    push_undo(f"edited {rel}", lambda p=target, o=original: (p.write_text(o, encoding="utf-8"),
                                                             f"Reverted {rel}.")[1],
              kind="edit", paths=[str(target)])
    return True, f"edited {rel}" + (f" (backup {bak.name})" if bak else "")


def _pytest_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("pytest") is not None
    except Exception:
        return False


def _test_command(root: Path) -> Optional[list[str]]:
    has_py_tests = ((root / "pytest.ini").exists() or (root / "tests").is_dir()
                    or any(root.glob("test_*.py")) or any(root.glob("*/test_*.py")))
    if has_py_tests:
        # prefer pytest (collects plain test_* functions); fall back to the stdlib
        # unittest runner when pytest isn't installed, so unittest projects still run.
        if _pytest_available():
            return [sys.executable, "-m", "pytest", "-q", "-x"]
        return [sys.executable, "-m", "unittest", "discover", "-s", str(root), "-p", "test_*.py"]
    pkg = root / "package.json"
    if pkg.exists():
        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8")) or {}).get("scripts", {})
            if "test" in scripts and shutil.which("npm"):
                return ["npm", "test", "--silent"]
        except Exception:
            pass
    return None


def _syntax_check(root: Path, files: list[str]) -> list[str]:
    import py_compile
    node = shutil.which("node")
    errs: list[str] = []
    for rel in files:
        p = root / rel
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        try:
            if ext == ".py":
                with tempfile.TemporaryDirectory() as td:
                    py_compile.compile(str(p), cfile=str(Path(td) / "x.pyc"), doraise=True)
            elif ext in (".js", ".mjs") and node:
                r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True, timeout=20)
                if r.returncode != 0:
                    errs.append(f"{rel}: {(r.stderr or r.stdout).strip().splitlines()[-1][:200]}")
            elif ext in (".json", ".webmanifest"):
                json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            errs.append(f"{rel}: {str(getattr(e, 'msg', e)).strip()[:200]}")
    return errs


def _run_tests(root: Path, cmd: list[str], stop=lambda: False) -> tuple[bool, str, bool]:
    """(passed, output, ran). ran=False when there was no test command."""
    if not cmd:
        return True, "", False
    import os
    # Don't let cached .pyc from an earlier fix round hide a change made this round
    # (fast successive edits can share an mtime second): never write/read stale bytecode.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for pc in root.rglob("__pycache__"):
        try:
            shutil.rmtree(pc, ignore_errors=True)
        except Exception:
            pass
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=180, env=env,
                              creationflags=(0x08000000 if sys.platform == "win32" else 0))
    except subprocess.TimeoutExpired:
        return False, "the tests timed out after 180s", True
    except Exception as e:
        return False, f"couldn't run the tests: {type(e).__name__}", True
    from core.secret_store import redact
    out = redact((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode == 0, out[-4000:], True


def t_code_task(args: dict, ctx) -> ToolResult:
    request = str(args.get("request") or args.get("task") or "").strip()
    if not request:
        return ToolResult.failure("Tell me what to change in the code.")
    root = R.resolve_project_path(args, ctx)
    if root is None:
        return ToolResult(False, {"needs_input": True}, "",
                          "Which project should I change? (say the folder path or name)")
    stop = ctx.extras.get("should_stop", lambda: False)
    progress = ctx.extras.get("progress", lambda m: None)
    max_rounds = max(1, min(int(args.get("max_rounds") or MAX_ROUNDS), 5))
    progress(f"Reading {root.name}…")
    scan = R.scan_project(root)
    if scan["files"] == 0:
        return ToolResult.failure(f"{root} has no readable files.")
    files = _relevant_files(scan, request)
    if not files:
        return ToolResult.failure("I couldn't find source files related to that in the project.")
    test_cmd = _test_command(root)
    touched: set[str] = set()
    rounds_done, notes = 0, []
    last_test_out = ""
    for rnd in range(max_rounds):
        if stop():
            return ToolResult.failure("Stopped.")
        rounds_done = rnd + 1
        current = {rel: (root / rel).read_text(encoding="utf-8", errors="ignore")
                   for rel, _ in files if (root / rel).is_file()}
        excerpt = "\n\n".join(f"===== FILE: {rel} =====\n{body[:12000]}" for rel, body in current.items())
        fix_ctx = ("" if rnd == 0 else
                   f"\n\nThe previous edits did not pass. Test/҃check output:\n{last_test_out[:2500]}\n"
                   "Fix the cause with more edits.")
        prompt = (
            "You are JARVIS's Coding Agent working IN AN EXISTING PROJECT. Make the smallest set of exact "
            "edits that satisfy the request, then stop. Return JSON: edits = a list of {file, action, find, "
            "replacement}. action 'replace' replaces the EXACT unique 'find' text with 'replacement' (include "
            "enough surrounding lines to be unique); 'create' makes a new file (replacement = full content); "
            "'append' adds to the end. Only touch files inside the project. Keep the project's existing style. "
            "Set done=true when the request is fully handled.\n"
            f"REQUEST: {request}\n"
            f"PROJECT: {scan['name']} — languages {scan['languages']}, stack {scan['stack']}\n"
            f"FILES:\n{excerpt}{fix_ctx}")
        progress("Working out the edits…" if rnd == 0 else f"Fixing (round {rnd + 1}/{max_rounds})…")
        from core.ai_gateway import get_gateway
        try:
            plan = get_gateway().generate(prompt, schema=EDIT_SCHEMA, tier="deep", reasoning="medium",
                                          max_output_tokens=16000, timeout=180.0).json()
        except Exception as e:
            from core.secret_store import redact
            return ToolResult.failure(f"The coding model failed: {redact(e)[:160]}", retryable=True)
        edits = plan.get("edits") or []
        if not edits and rnd == 0:
            return ToolResult.failure("The model proposed no edits for that request.")
        applied, failed = [], []
        for e in edits[:MAX_EDIT_FILES]:
            ok, msg = _apply_edit(root, e)
            (applied if ok else failed).append(msg)
            if ok:
                touched.add(str(e.get("file")).replace("\\", "/").lstrip("/"))
        notes.append(f"Round {rnd + 1}: {len(applied)} edit(s) applied"
                     + (f", {len(failed)} rejected ({'; '.join(failed[:2])})" if failed else ""))
        if not applied and failed:
            return ToolResult.failure("None of the proposed edits could be applied: " + "; ".join(failed[:3]))
        # verify
        progress("Running the tests…" if test_cmd else "Checking syntax…")
        passed, out, ran = _run_tests(root, test_cmd, stop)
        if not ran:
            serrs = _syntax_check(root, sorted(touched))
            passed, out = (not serrs), "; ".join(serrs)
        last_test_out = out
        notes.append(f"  → {'tests passed' if ran and passed else 'checks passed' if passed else 'FAILED'}"
                     + (f": {out[:160]}" if not passed else ""))
        if passed and plan.get("done", True):
            entry = sorted(touched)[0] if touched else ""
            msg = (f"Coding Agent: {plan.get('summary') or request} — {len(touched)} file(s) changed, "
                   + ("tests pass." if ran else "syntax checks pass."))
            return ToolResult(True, {"dir": str(root), "files": sorted(touched), "tested": ran,
                                     "passed": True, "path": str(root / entry) if entry else str(root),
                                     "rounds": rounds_done, "summary": plan.get("summary", "")}, msg)
        if passed and not plan.get("done", True):
            continue                      # model wants another round even though checks pass
    return ToolResult.failure(
        f"After {rounds_done} round(s) the change still isn't passing: {last_test_out[:200]}. "
        f"Files changed: {', '.join(sorted(touched)) or 'none'} (originals are in .bak files / undo).",
        files=sorted(touched))


def verify_code_task(args, result):
    from core import verification as V
    if not result.ok:
        return V.failed(result.error)
    if not result.data.get("passed"):
        return V.failed("checks did not pass")
    if result.data.get("tested"):
        return V.verified(f"Tests pass after editing {len(result.data.get('files') or [])} file(s).")
    return V.unverifiable("Edited and syntax-checked, but the project has no test suite to prove behaviour.")


def register(reg) -> None:
    from core.permissions import RiskLevel
    from core.tools.registry import ToolSpec
    STR = {"type": "string"}
    reg.register(ToolSpec(
        "code.task", "Change code in an EXISTING project and verify it: find the right files, make exact edits "
        "(with backups + undo), then run the project's tests (or syntax-check) and fix up to 3 rounds. Use for "
        "'fix the bug in…', 'add X to my project', 'refactor…'. For building a NEW project from scratch use the "
        "team build instead.", t_code_task,
        {"type": "object", "properties": {"request": STR, "project": STR, "path": STR,
                                          "max_rounds": {"type": "integer"}}, "required": ["request"]},
        {"dir": "str", "passed": "bool", "files": "list"}, agent="coding", risk=RiskLevel.MEDIUM_RISK,
        retry_safe=False, timeout=900, verify=verify_code_task))
