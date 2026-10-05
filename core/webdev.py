"""
core/webdev.py — run a website JARVIS built, check it for real, and open it.

team.test does static checks (HTML structure, CSS/JS syntax, local links, a
head that has a title / viewport / lang). This adds the dynamic half: it
actually SERVES the site on 127.0.0.1 and drives a real headless browser over
it, so the website-development agent verifies what a person would see, not
just that the files parse.

    webdev.run(dir)   1. start a local preview server (core/processes.py),
                         bound to 127.0.0.1, inside the workspace
                      2. open every page in a headless browser and collect:
                         · JavaScript console errors and uncaught exceptions
                         · failed network requests (404 for a local asset,
                           broken <img>/<script>/<link>)
                         · links to pages that don't exist
                         · desktop (1366×850) AND mobile (390×844) screenshots,
                           plus whether the layout overflows horizontally on
                           mobile (a common responsive bug)
                      3. optional VISION review of the desktop screenshot
                         (vision tier) — "does this look like a finished
                         <what the spec asked for>, any obvious visual bugs?"
                      4. return a structured report; the caller (team review /
                         the coding fix loop) fixes real errors and re-runs.
                      The server keeps running (named "preview-<folder>") so the
                      user can look; webdev.open() brings it up in their real
                      browser, and process.stop stops it.

Playwright drives the pre-installed headless Chromium. If Playwright or a
browser engine is unavailable, run() says so honestly and the static tests
still stand — it never pretends the site was checked.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from core.tools.registry import ToolResult

DESKTOP = (1366, 850)
MOBILE = (390, 844)
PAGE_TIMEOUT_MS = 15000


def _playwright_ready() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
    except Exception:
        return False, "Playwright isn't installed (pip install playwright)."
    return True, ""


def _pages(ws: Path) -> list[str]:
    return sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*.html"))


def _check_site(base: str, ws: Path, pages: list[str], shot_dir: Path,
                stop=lambda: False) -> dict:
    """Drive a headless browser over every page. Real errors only."""
    from playwright.sync_api import sync_playwright

    console_errors: list[str] = []
    bad_requests: list[str] = []
    page_errors: list[str] = []
    broken_links: list[str] = []
    mobile_overflow: list[str] = []
    shots: dict[str, str] = {}
    seen_links: set[str] = set()

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(args=["--no-sandbox"])
        except Exception as e:
            raise RuntimeError(f"couldn't launch the headless browser: {str(e)[:160]}")
        try:
            for rel in pages:
                if stop():
                    break
                url = base + rel
                for label, (w, h) in (("desktop", DESKTOP), ("mobile", MOBILE)):
                    ctx = browser.new_context(viewport={"width": w, "height": h})
                    page = ctx.new_page()
                    page.on("console", lambda m: console_errors.append(f"{rel}: {m.text[:200]}")
                            if m.type == "error" else None)
                    page.on("pageerror", lambda e: page_errors.append(f"{rel}: {str(e)[:200]}"))

                    def on_response(resp, rel=rel):
                        try:
                            if resp.status >= 400 and resp.request.resource_type in (
                                    "image", "script", "stylesheet", "font", "fetch", "document"):
                                u = resp.url.split("?")[0]
                                bad_requests.append(f"{rel}: {resp.status} for {u.replace(base, '') or u}")
                        except Exception:
                            pass
                    page.on("response", on_response)
                    try:
                        page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
                    except Exception as e:
                        page_errors.append(f"{rel}: page did not load ({str(e)[:120]})")
                        ctx.close()
                        continue
                    if label == "mobile":
                        try:
                            sw = page.evaluate("document.documentElement.scrollWidth")
                            if isinstance(sw, (int, float)) and sw > w + 4:
                                mobile_overflow.append(f"{rel}: content is {int(sw)}px wide on a {w}px screen "
                                                       f"(horizontal scroll)")
                        except Exception:
                            pass
                    else:
                        try:
                            for a in page.eval_on_selector_all(
                                    "a[href]", "els => els.map(e => e.getAttribute('href'))") or []:
                                if not a or a.startswith(("http", "mailto:", "tel:", "#", "//", "javascript:")):
                                    continue
                                target = (ws / rel).parent / a.split("#")[0].split("?")[0]
                                key = target.as_posix()
                                if key in seen_links:
                                    continue
                                seen_links.add(key)
                                if a.split("#")[0] and not target.exists():
                                    broken_links.append(f"{rel}: link to '{a}' (no such page)")
                        except Exception:
                            pass
                        try:
                            shot = shot_dir / f"{rel.replace('/', '_')}.{label}.png"
                            page.screenshot(path=str(shot), full_page=True)
                            shots[rel] = str(shot)
                        except Exception:
                            pass
                    ctx.close()
        finally:
            browser.close()

    def dedup(xs):
        return list(dict.fromkeys(xs))
    errors = dedup(page_errors + console_errors + bad_requests + broken_links)
    return {"errors": errors, "warnings": dedup(mobile_overflow), "shots": shots,
            "pages_checked": len(pages)}


def run(folder: str, spec: Optional[dict] = None, vision: bool = True, stop=lambda: False,
        progress=lambda m: None) -> dict:
    ws = Path(folder).expanduser().resolve()
    if not ws.is_dir():
        return {"ok": False, "ran": False, "error": f"No folder at {ws}"}
    ok, why = _playwright_ready()
    if not ok:
        return {"ok": False, "ran": False, "error": why}
    pages = _pages(ws)
    if not pages:
        return {"ok": False, "ran": False, "error": "There are no .html pages to run."}

    from core.processes import get_manager
    pm = get_manager()
    progress("Starting a local preview server…")
    try:
        proc = pm.start_preview(str(ws))
    except Exception as e:
        return {"ok": False, "ran": False, "error": f"couldn't start the preview server: {str(e)[:160]}"}
    base = proc.url
    shot_dir = ws / ".jarvis_preview"
    shot_dir.mkdir(exist_ok=True)
    progress(f"Opening {len(pages)} page(s) in a headless browser (desktop + mobile)…")
    try:
        report = _check_site(base, ws, pages, shot_dir, stop=stop)
    except Exception as e:
        return {"ok": False, "ran": False, "url": base, "error": str(e)[:200],
                "note": "The site is being served, but I couldn't drive the browser over it."}
    report.update({"ok": not report["errors"], "ran": True, "url": base, "process": proc.name})

    entry = (spec or {}).get("entry") if spec else None
    entry = entry if entry in pages else pages[0]
    if vision and report["shots"].get(entry) and not stop():
        progress("Looking at the rendered page…")
        try:
            report["vision"] = _vision_review(report["shots"][entry], spec)
            probs = report["vision"].get("problems") or []
            report["warnings"] = list(report.get("warnings", [])) + [f"visual: {p}" for p in probs[:5]]
        except Exception as e:
            report["vision"] = {"error": type(e).__name__}
    return report


def _vision_review(shot_path: str, spec: Optional[dict]) -> dict:
    from core.ai_gateway import get_gateway
    from core.providers.base import CapabilityNotSupported, ProviderError
    want = ""
    if spec:
        want = f" It is meant to be: {spec.get('title', '')} — {str(spec.get('summary') or spec.get('goal') or '')[:300]}."
    prompt = ("You are the Review Agent looking at a screenshot of a web page JARVIS built." + want +
              " Point out only real visual problems a user would notice: unstyled/overlapping content, "
              "invisible text (contrast), a broken layout, obviously missing images, placeholder text left in. "
              "If it looks like a finished, styled page, say so.")
    schema = {"type": "object", "properties": {
        "looks_finished": {"type": "boolean"},
        "problems": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"}}, "required": ["looks_finished", "summary"]}
    data = Path(shot_path).read_bytes()
    try:
        r = get_gateway().generate(prompt, images=[(data, "image/png")], schema=schema, tier="vision",
                                   timeout=60.0)
        return r.json() if r.data is None else r.data
    except (ProviderError, CapabilityNotSupported) as e:
        return {"error": f"vision unavailable ({type(e).__name__})"}


# ── tools ────────────────────────────────────────────────────────────────────

def t_web_run(args: dict, ctx) -> ToolResult:
    folder = str(args.get("dir") or args.get("folder") or "")
    spec = None
    sp = Path(folder) / "spec.json" if folder else None
    if sp and sp.is_file():
        try:
            spec = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            spec = None
    rep = run(folder, spec=spec, vision=bool(args.get("vision", True)),
              stop=ctx.extras.get("should_stop", lambda: False),
              progress=ctx.extras.get("progress", lambda m: None))
    if not rep.get("ran"):
        return ToolResult.failure(rep.get("error") or "The site could not be run.",
                                  retryable=True, ran=False)
    errs, warns = rep.get("errors", []), rep.get("warnings", [])
    msg = (f"Ran the site at {rep['url']} ({rep['pages_checked']} page(s)): "
           + ("no errors" if not errs else f"{len(errs)} error(s)")
           + (f", {len(warns)} warning(s)" if warns else "") + ".")
    if errs:
        msg += " First: " + "; ".join(errs[:3])
    return ToolResult(rep["ok"], {"url": rep["url"], "errors": errs, "warnings": warns,
                                  "shots": rep.get("shots", {}), "vision": rep.get("vision", {}),
                                  "process": rep.get("process"), "ran": True,
                                  "passed": rep["ok"]}, msg)


def t_web_open(args: dict, ctx) -> ToolResult:
    folder = str(args.get("dir") or args.get("folder") or "")
    from core.processes import get_manager
    pm = get_manager()
    try:
        proc = pm.start_preview(folder) if folder else None
    except Exception as e:
        return ToolResult.failure(str(e)[:160])
    if proc is None:
        procs = [p for p in pm.list() if p.recipe == "preview"]
        if not procs:
            return ToolResult.failure("No preview is running. Build or run the site first.")
        proc = procs[-1]
    try:
        from actions.browser_control import browser_control
        browser_control(action="open", url=proc.url, player=ctx.player)
    except Exception:
        import webbrowser
        webbrowser.open(proc.url)
    return ToolResult(True, {"url": proc.url}, f"Opened {proc.url} in your browser.")


def verify_web_run(args, result):
    from core import verification as V
    if not result.ok:
        return V.failed("; ".join(result.data.get("errors", [])[:3]) or result.error)
    if not result.data.get("ran"):
        return V.unverifiable("The site could not be run in a browser here.")
    return V.verified(f"Ran clean in a headless browser ({len(result.data.get('warnings') or [])} warning(s)).")


def register(reg) -> None:
    from core.permissions import RiskLevel
    from core.tools.registry import ToolSpec
    STR = {"type": "string"}
    reg.register(ToolSpec(
        "web.run", "Run a website JARVIS built: serve it locally and drive a real headless browser over every "
        "page (desktop + mobile) to catch JavaScript errors, 404s, broken links, mobile overflow, and review "
        "how it looks. Use after building or changing a site.", t_web_run,
        {"type": "object", "properties": {"dir": STR, "vision": {"type": "boolean"}}, "required": ["dir"]},
        {"url": "str", "passed": "bool"}, agent="webdev", risk=RiskLevel.LOW_RISK, retry_safe=True,
        timeout=240, verify=verify_web_run))
    reg.register(ToolSpec(
        "web.open", "Open the running preview of a site JARVIS built in the user's real browser.", t_web_open,
        {"type": "object", "properties": {"dir": STR}, "required": []},
        {"url": "str"}, agent="webdev", risk=RiskLevel.LOW_RISK, timeout=30))
