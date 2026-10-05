"""
core/research.py — long-running knowledge work that runs as a background task.

    "Research the best free vector databases and write me a report"
      s1 research.collect   sub-queries → web search → fetch pages → excerpts   (stores sources.json)
      s2 research.analyze   deep-tier synthesis with [n] citations              (stores analysis.md)
      s3 document.write     Markdown → .docx / .md / .html / .txt               (verified: real file)
      → orchestrator verifies every step, then notifies: "Report ready: …"

    "Analyze my entire software project"
      s1 project.analyze    scan (structure, languages, LOC, stack, README) →
                            sample key files → deep-tier review → report .md

Everything a step produces is STORED in a workspace folder
(Documents/JARVIS Research/<topic>/ …) so a long task survives a restart and
"continue the research from yesterday" has something to continue from; each
result is also recorded as an episode.

COST / PRIVACY (stated plainly)
  * Search uses DuckDuckGo (free, no key); Gemini grounding is only a fallback.
  * Page fetches: http(s) only, public hosts only (no localhost / LAN /
    link-local — nothing on your network is ever probed), 12 s timeout,
    600 KB cap, HTML reduced to text locally.
  * project.analyze sends EXCERPTS of your source code to the configured AI
    provider (Gemini free tier by default; Google's free tier may use
    submitted content to improve its products). Secrets are never sent:
    .env*, keys, certificates, credential/secret/token-named files and
    config/api_keys.json are skipped, and every excerpt passes through
    secret_store.redact(). Nothing is written into your project folder.

Model calls go through the gateway tiers: sub-queries "fast", synthesis and
code review "deep" (Flash + high thinking budget on the free tier).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import time
import urllib.request
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from core.tools.registry import ToolResult

# ── injectable backends (tests replace these) ────────────────────────────────
_BACKENDS: dict[str, Optional[Callable]] = {"search": None, "fetch": None, "llm": None, "fetch_html": None,
                                             "screenshot": None}


def set_backends(search=None, fetch=None, llm=None, fetch_html=None, screenshot=None) -> None:
    _BACKENDS.update(search=search, fetch=fetch, llm=llm, fetch_html=fetch_html, screenshot=screenshot)


DEPTH = {"quick": (2, 4, 2), "standard": (3, 5, 4), "deep": (5, 6, 7)}   # queries, results/query, pages
PAGE_BYTES = 600_000
EXCERPT_CHARS = 1600


# ═════════════════════════════════════════════════════════════════════════════
# Workspace
# ═════════════════════════════════════════════════════════════════════════════

def documents_dir() -> Path:
    try:
        from actions.file_controller import _get_documents
        d = Path(_get_documents())
        if d.exists():
            return d
    except Exception:
        pass
    return Path.home() / "Documents"


def slugify(text: str, n: int = 48) -> str:
    s = re.sub(r"[^\w\s-]", "", str(text or "")).strip()
    s = re.sub(r"[\s_-]+", " ", s)[:n].strip()
    return s or "untitled"


def workspace(kind: str, name: str, reuse: bool = False) -> Path:
    root = documents_dir() / ("JARVIS Research" if kind == "research" else "JARVIS Projects")
    base = root / slugify(name)
    p = base
    if not reuse:
        i = 2
        while p.exists() and any(p.iterdir()):
            p = base.with_name(f"{base.name} ({i})")
            i += 1
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    os.replace(tmp, path)
    return path


def _extras(ctx) -> dict:
    return getattr(ctx, "extras", None) or {}


def _progress(ctx, msg: str) -> None:
    fn = _extras(ctx).get("progress")
    if fn:
        try:
            fn(msg)
        except Exception:
            pass


def _stopped(ctx) -> bool:
    fn = _extras(ctx).get("should_stop")
    try:
        return bool(fn and fn())
    except Exception:
        return False


def _record_episode(kind: str, title: str, summary: str = "", paths=(), project=None, tags=()):
    try:
        from memory.episodic import EpisodicStore
        EpisodicStore().add(kind, title, summary, project=project, refs={"paths": list(paths)},
                            tags=tags, outcome="done")
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
# LLM, search, fetch
# ═════════════════════════════════════════════════════════════════════════════

def llm(prompt: str, tier: str = "normal", **kw) -> str:
    if _BACKENDS["llm"]:
        return _BACKENDS["llm"](prompt, tier=tier, **kw)
    from core.ai_gateway import get_gateway
    return get_gateway().text(prompt, tier=tier, **kw)


def web_search(query: str, n: int = 5) -> list[dict]:
    if _BACKENDS["search"]:
        return _BACKENDS["search"](query, n)
    out: list[dict] = []
    try:
        from actions.web_search import _ddg_search
        out = [r for r in _ddg_search(query, max_results=n) if r.get("url")]
    except Exception:
        out = []
    if not out:
        try:           # fallback: a grounded answer is still a (single, unlinked) source
            from actions.web_search import _gemini_search
            txt = _gemini_search(query)
            if txt:
                out = [{"title": f"Web answer: {query}", "snippet": txt[:EXCERPT_CHARS], "url": ""}]
        except Exception:
            pass
    return out


def is_public_url(url: str) -> bool:
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        host = u.hostname
        if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
            return False
        for info in socket.getaddrinfo(host, None)[:4]:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe"}
    BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "td", "th", "blockquote", "pre", "dd", "dt", "figcaption"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.title = ""
        self._in_title = False
        self.blocks: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self._buf.append(data)

    def _flush(self):
        t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if len(t) > 40:
            self.blocks.append(t)
        self._buf = []

    def close(self):
        super().close()
        self._flush()


def html_to_text(html: str) -> tuple[str, list[str]]:
    p = _TextExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    return re.sub(r"\s+", " ", p.title).strip(), p.blocks


def fetch_page(url: str, timeout: float = 12.0) -> tuple[str, list[str]]:
    """(title, text blocks). Raises on non-public URL / non-text content."""
    if _BACKENDS["fetch"]:
        return _BACKENDS["fetch"](url)
    if not is_public_url(url):
        raise PermissionError("not a public http(s) address")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (JARVIS research assistant)",
                                               "Accept": "text/html,text/plain;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        if not any(t in ctype for t in ("text/html", "text/plain", "xhtml")):
            raise ValueError(f"unsupported content ({ctype.split(';')[0] or 'unknown'})")
        final = r.geturl()
        if final != url and not is_public_url(final):
            raise PermissionError("redirected to a non-public address")
        raw = r.read(PAGE_BYTES)
        cs = r.headers.get_content_charset() or "utf-8"
    text = raw.decode(cs, errors="ignore")
    if "text/plain" in ctype:
        return "", [b.strip() for b in re.split(r"\n\s*\n", text) if len(b.strip()) > 40]
    return html_to_text(text)


class _OutlineExtractor(HTMLParser):
    """Structure of a page for DESIGN inspiration: headings, nav labels, button
    / call-to-action texts, section count — not its prose."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.headings, self.nav, self.ctas = "", [], [], []
        self.sections = 0
        self._stack: list[str] = []
        self._buf: list[str] = []
        self._skip = 0
        self._in_nav = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag == "nav":
            self._in_nav += 1
        if tag in ("section", "article"):
            self.sections += 1
        if tag in ("title", "h1", "h2", "h3", "button", "a"):
            self._stack.append(tag)
            self._buf = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if tag == "nav" and self._in_nav:
            self._in_nav -= 1
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()
            t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if not t or len(t) > 120:
                return
            if tag == "title":
                self.title = t
            elif tag in ("h1", "h2", "h3"):
                self.headings.append(f"{tag}: {t}")
            elif tag == "button" or (tag == "a" and len(t) < 28 and not self._in_nav and
                                     re.search(r"(start|try|get|sign|join|download|book|contact|learn|explore|"
                                               r"demo|free|buy|chat)", t, re.I)):
                self.ctas.append(t)
            elif tag == "a" and self._in_nav and len(t) < 28:
                self.nav.append(t)

    def handle_data(self, data):
        if not self._skip and self._stack:
            self._buf.append(data)


def fetch_html(url: str, timeout: float = 12.0) -> str:
    """Raw HTML of a PUBLIC page (same guards as fetch_page)."""
    if _BACKENDS.get("fetch_html"):
        return _BACKENDS["fetch_html"](url)
    if _BACKENDS.get("fetch"):                 # test mode with a fake fetcher: never touch the network
        raise RuntimeError("offline (test backends)")
    if not is_public_url(url):
        raise PermissionError("not a public http(s) address")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (JARVIS research assistant)",
                                               "Accept": "text/html;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype:
            raise ValueError(f"unsupported content ({ctype.split(';')[0] or 'unknown'})")
        if r.geturl() != url and not is_public_url(r.geturl()):
            raise PermissionError("redirected to a non-public address")
        raw = r.read(PAGE_BYTES)
        cs = r.headers.get_content_charset() or "utf-8"
    return raw.decode(cs, errors="ignore")


def page_outline(html: str) -> dict:
    p = _OutlineExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    colors = Counter(c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", html)).most_common(8)
    fonts = Counter(f.strip().strip("'\"") for f in re.findall(r"font-family\s*:\s*([^;\"'}]+)", html)
                    ).most_common(4)
    gfonts = re.findall(r"fonts\.googleapis\.com/css2?\?family=([A-Za-z+]+)", html)
    return {"title": p.title, "headings": p.headings[:14], "nav": list(dict.fromkeys(p.nav))[:10],
            "ctas": list(dict.fromkeys(p.ctas))[:8], "sections": p.sections,
            "colors": [c for c, _ in colors], "fonts": [f for f, _ in fonts] + [g.replace("+", " ") for g in gfonts[:3]]}


def screenshot_page(url: str, path: Path, width: int = 1366, height: int = 860) -> bool:
    """Viewport screenshot of a public page via headless Chromium (Playwright),
    when it is installed. False when it isn't — never an error."""
    if _BACKENDS.get("screenshot"):
        return bool(_BACKENDS["screenshot"](url, path))
    if _BACKENDS.get("fetch"):                 # test mode: no browser, no network
        return False
    if not is_public_url(url):
        return False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(args=["--no-sandbox"])
            try:
                pg = b.new_context(viewport={"width": width, "height": height}, java_script_enabled=True,
                                   accept_downloads=False).new_page()
                pg.goto(url, wait_until="domcontentloaded", timeout=20000)
                pg.wait_for_timeout(1500)
                pg.screenshot(path=str(path))
            finally:
                b.close()
        return path.exists() and path.stat().st_size > 2000
    except Exception:
        return False


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower())
            if w not in {"the", "and", "for", "with", "that", "this", "from", "what", "best", "are", "how"}}


def best_excerpt(blocks: list[str], topic: str, limit: int = EXCERPT_CHARS) -> str:
    q = _words(topic)
    scored = sorted(enumerate(blocks), key=lambda ib: (-len(q & _words(ib[1])), ib[0]))
    chosen, used = [], 0
    for i, b in scored:
        if used + len(b) > limit:
            if not chosen:
                chosen.append((i, b[:limit]))
            break
        chosen.append((i, b))
        used += len(b) + 1
    return " ".join(b for _, b in sorted(chosen))


def plan_queries(topic: str, n: int) -> list[str]:
    base = [topic]
    try:
        raw = llm(f"Write {n} diverse web search queries (one per line, no numbering, no quotes) that "
                  f"together cover this research topic well — include one for recent 2026 information and "
                  f"one for comparisons or criticism where relevant.\nTopic: {topic}",
                  tier="fast", temperature=0.4, max_output_tokens=300)
        lines = [re.sub(r"^[\s\-\*\d.)]+", "", l).strip().strip('"') for l in (raw or "").splitlines()]
        base += [l for l in lines if 4 < len(l) < 160]
    except Exception:
        base += [f"{topic} overview", f"{topic} comparison 2026", f"{topic} pros and cons"]
    seen, out = set(), []
    for q in base:
        k = q.lower()
        if k not in seen:
            seen.add(k)
            out.append(q)
    return out[:n]


# ═════════════════════════════════════════════════════════════════════════════
# Tools
# ═════════════════════════════════════════════════════════════════════════════

def t_research_collect(args: dict, ctx) -> ToolResult:
    topic = str(args.get("topic") or "").strip()
    if not topic:
        return ToolResult.failure("What should I research?")
    depth = str(args.get("depth") or "standard").lower()
    nq, per_q, pages = DEPTH.get(depth, DEPTH["standard"])
    if args.get("max_sources"):
        pages = max(1, min(int(args["max_sources"]), 10))
    ws = Path(args["dir"]) if args.get("dir") else workspace("research", topic)
    _progress(ctx, f"Planning searches for \"{topic}\"…")
    queries = plan_queries(topic, nq)
    results: list[dict] = []
    seen_urls = set()
    for i, q in enumerate(queries, 1):
        if _stopped(ctx):
            return ToolResult.failure("Stopped.")
        _progress(ctx, f"Searching ({i}/{len(queries)}): {q}")
        for r in web_search(q, per_q):
            key = (r.get("url") or r.get("title") or "").split("#")[0].rstrip("/").lower()
            if key and key not in seen_urls:
                seen_urls.add(key)
                results.append({**r, "query": q})
    if not results:
        return ToolResult.failure("The web searches returned nothing — I may be offline.", retryable=True)
    # prefer domain diversity for the pages we actually read
    domains, ordered = Counter(), []
    for r in results:
        d = urlparse(r.get("url") or "").netloc
        ordered.append((domains[d], len(ordered), r))
        domains[d] += 1
    ordered.sort(key=lambda x: (x[0], x[1]))
    sources, fetched = [], 0
    for _, _, r in ordered:
        if _stopped(ctx):
            return ToolResult.failure("Stopped.")
        src = {"n": len(sources) + 1, "title": (r.get("title") or "").strip()[:160], "url": r.get("url", ""),
               "snippet": (r.get("snippet") or "").strip()[:500], "query": r.get("query", ""), "excerpt": ""}
        if fetched < pages and src["url"]:
            _progress(ctx, f"Reading {urlparse(src['url']).netloc}…")
            try:
                title, blocks = fetch_page(src["url"])
                src["excerpt"] = best_excerpt(blocks, topic)
                src["title"] = src["title"] or title[:160]
                fetched += 1
            except Exception as e:
                src["fetch_error"] = f"{type(e).__name__}"[:60]
        if src["excerpt"] or src["snippet"]:
            sources.append(src)
        if len(sources) >= pages + 6:
            break
    data = {"topic": topic, "collected": datetime.now().isoformat(timespec="seconds"),
            "queries": queries, "sources": sources}
    path = ws / "sources.json"
    _write_text(path, json.dumps(data, indent=2, ensure_ascii=False))
    md = [f"# Sources — {topic}", ""] + [f"{s['n']}. [{s['title'] or s['url']}]({s['url']})"
                                          + (" — read" if s["excerpt"] else " — snippet only")
                                          for s in sources]
    _write_text(ws / "sources.md", "\n".join(md))
    return ToolResult(True, {"dir": str(ws), "path": str(path), "count": len(sources), "pages_read": fetched,
                             "queries": queries,
                             "sources": [{"n": s["n"], "title": s["title"], "url": s["url"]} for s in sources]},
                      f"Collected {len(sources)} sources ({fetched} pages read) for \"{topic}\".")


def _load_sources(args) -> tuple[dict, Path]:
    p = args.get("path") or (Path(args["dir"]) / "sources.json" if args.get("dir") else None)
    if not p:
        raise FileNotFoundError("No collected sources were given.")
    p = Path(str(p))
    if p.is_dir():
        p = p / "sources.json"
    return json.loads(p.read_text(encoding="utf-8")), p.parent


def t_research_analyze(args: dict, ctx) -> ToolResult:
    try:
        data, ws = _load_sources(args)
    except Exception as e:
        return ToolResult.failure(f"I couldn't read the collected sources ({type(e).__name__}).")
    topic = str(args.get("topic") or data.get("topic") or "the topic")
    focus = str(args.get("focus") or "").strip()
    srcs = data.get("sources") or []
    if not srcs:
        return ToolResult.failure("There were no sources to analyse.")
    blocks, used = [], 0
    for s in srcs:
        body = s.get("excerpt") or s.get("snippet") or ""
        chunk = f"[{s['n']}] {s.get('title', '')} ({s.get('url', '')})\n{body}\n"
        if used + len(chunk) > 42000:
            break
        blocks.append(chunk)
        used += len(chunk)
    _progress(ctx, f"Analysing {len(blocks)} sources (deep reasoning)…")
    prompt = (
        f"You are a careful research analyst. Using ONLY the numbered sources below, write a Markdown "
        f"research brief on: {topic}.\n" + (f"Focus: {focus}\n" if focus else "") +
        "Structure: '# <title>', '## Summary' (4-6 sentences), '## Key findings' (bullets, each ending "
        "with citations like [2] or [1][4]), '## Comparison' (a Markdown table if the topic compares "
        "options, otherwise omit), '## Recommendations', '## Open questions / caveats'. "
        "Cite only source numbers that exist; say when sources disagree or are thin. Do not add a "
        "sources list — it is appended automatically.\n\nSOURCES:\n" + "\n".join(blocks)
    )
    try:
        text = (llm(prompt, tier="deep", timeout=150.0) or "").strip()
    except Exception as e:
        from core.secret_store import redact
        return ToolResult.failure(f"The analysis model failed: {redact(e)[:160]}", retryable=True)
    if len(text) < 80:
        return ToolResult.failure("The analysis came back empty.", retryable=True)
    valid = {s["n"] for s in srcs}
    bad = sorted({int(n) for n in re.findall(r"\[(\d{1,3})\]", text) if int(n) not in valid})
    if bad:                          # self-check: strip citations to sources that don't exist
        text = re.sub(r"\[(\d{1,3})\]", lambda m: m.group(0) if int(m.group(1)) in valid else "", text)
    text += "\n\n## Sources\n" + "\n".join(f"{s['n']}. {s.get('title') or s.get('url')} — {s.get('url')}"
                                          for s in srcs if s.get("url") or s.get("title"))
    path = _write_text(ws / "analysis.md", text)
    summary = ""
    m = re.search(r"##\s*Summary\s*\n+(.+?)(?:\n##|\Z)", text, re.S)
    if m:
        summary = re.sub(r"\s+", " ", m.group(1)).strip()[:700]
    title = (re.search(r"^#\s+(.+)$", text, re.M) or [None, topic])[1]
    _record_episode("artifact", f"Research brief: {topic}", summary[:300], paths=[str(path)], tags=["research"])
    return ToolResult(True, {"analysis": text, "summary": summary or text[:600], "path": str(path),
                             "dir": str(ws), "title": title, "invalid_citations_removed": bad},
                      f"Analysis ready ({len(text):,} characters, {len(valid)} sources).")


# ═════════════════════════════════════════════════════════════════════════════
# Documents — dependency-free .docx writer
# ═════════════════════════════════════════════════════════════════════════════

def _xml(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _runs(text: str) -> str:
    out = []
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if not part:
            continue
        rpr = "<w:rPr><w:b/></w:rPr>" if i % 2 else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{_xml(part)}</w:t></w:r>')
    return "".join(out)


def markdown_to_docx(md: str, path: Path, title: str = "") -> Path:
    import zipfile
    paras = []
    if title:
        paras.append(f'<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr>{_runs(title)}</w:p>')
    for line in md.replace("\r\n", "\n").split("\n"):
        s = line.rstrip()
        if not s.strip():
            continue
        if re.match(r"^\s*\|?\s*:?-{3,}", s):
            continue                                            # table separator row
        m = re.match(r"^(#{1,3})\s+(.*)$", s)
        if m:
            if title and len(m.group(1)) == 1 and m.group(2).strip() == title.strip():
                continue
            style = {1: "Heading1", 2: "Heading2", 3: "Heading3"}[len(m.group(1))]
            paras.append(f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{_runs(m.group(2))}</w:p>')
            continue
        m = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$", s)
        if m:
            paras.append(f'<w:p><w:pPr><w:pStyle w:val="ListParagraph"/><w:ind w:left="360"/></w:pPr>'
                         f'{_runs("• " + m.group(1))}</w:p>')
            continue
        if s.strip().startswith("|"):
            cells = [c.strip() for c in s.strip().strip("|").split("|")]
            paras.append(f"<w:p>{_runs('  |  '.join(cells))}</w:p>")
            continue
        paras.append(f"<w:p>{_runs(s.strip())}</w:p>")
    body = "".join(paras) or "<w:p/>"
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W}><w:body>{body}'
           f'<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1200" w:right="1200" '
           f'w:bottom="1200" w:left="1200"/></w:sectPr></w:body></w:document>')

    def st(sid, name, size, bold=True, color="1F3864"):
        return (f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>'
                f'<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>'
                f'<w:rPr>{"<w:b/>" if bold else ""}<w:color w:val="{color}"/><w:sz w:val="{size}"/></w:rPr>'
                f'</w:style>')
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {W}>'
              f'<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>'
              f'<w:sz w:val="22"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="100"/>'
              f'</w:pPr></w:pPrDefault></w:docDefaults>'
              f'<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
              + st("Title", "Title", 44) + st("Heading1", "heading 1", 32) + st("Heading2", "heading 2", 28)
              + st("Heading3", "heading 3", 24) +
              '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>'
              '<w:basedOn w:val="Normal"/></w:style></w:styles>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'wordprocessingml.styles+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>')
    drels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
             'styles" Target="styles.xml"/></Relationships>')
    tmp = path.with_name(path.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/_rels/document.xml.rels", drels)
    os.replace(tmp, path)
    return path


def _md_to_html(md: str, title: str) -> str:
    import html as _h
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
           f"content='width=device-width, initial-scale=1'><title>{_h.escape(title)}</title><style>"
           "body{font-family:Segoe UI,system-ui,sans-serif;max-width:860px;margin:40px auto;padding:0 20px;"
           "line-height:1.6;color:#1b2430}h1,h2,h3{color:#0b3d62}code{background:#eef3f8;padding:2px 4px}"
           "</style></head><body>"]
    in_list = False
    for line in md.replace("\r\n", "\n").split("\n"):
        s = line.rstrip()
        m = re.match(r"^(#{1,3})\s+(.*)$", s)
        li = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$", s)
        if in_list and not li:
            out.append("</ul>")
            in_list = False
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{_h.escape(m.group(2))}</h{n}>")
        elif li:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _h.escape(li.group(1))) + "</li>")
        elif s.strip():
            out.append("<p>" + re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _h.escape(s.strip())) + "</p>")
    if in_list:
        out.append("</ul>")
    out.append("</body></html>")
    return "\n".join(out)


def t_document_write(args: dict, ctx) -> ToolResult:
    content = str(args.get("content") or "").strip()
    if not content:
        return ToolResult.failure("There is no content to write.")
    title = str(args.get("title") or "").strip()
    if not title:
        m = re.search(r"^#\s+(.+)$", content, re.M)
        title = m.group(1).strip() if m else "JARVIS document"
    fmt = str(args.get("format") or "docx").lower().lstrip(".")
    if fmt not in ("docx", "md", "txt", "html"):
        fmt = "docx"
    folder = args.get("folder") or args.get("dir")
    if folder:
        p = Path(str(folder))
        if not p.is_absolute():
            from core.tools.builtin import resolve_folder
            p = resolve_folder(str(folder))
    else:
        p = documents_dir() / "JARVIS Research"
    p.mkdir(parents=True, exist_ok=True)
    name = slugify(args.get("filename") or title, 60)
    target = p / f"{name}.{fmt}"
    i = 2
    while target.exists():
        target = p / f"{name} ({i}).{fmt}"
        i += 1
    if fmt == "docx":
        markdown_to_docx(content, target, title=title)
    elif fmt == "html":
        _write_text(target, _md_to_html(content, title))
    elif fmt == "txt":
        _write_text(target, re.sub(r"[*#`]+", "", content))
    else:
        _write_text(target, content)
    _record_episode("artifact", f"Wrote {target.name}", title, paths=[str(target)], tags=["document"])
    return ToolResult(True, {"path": str(target), "name": target.name, "format": fmt,
                             "size": target.stat().st_size},
                      f"Saved \"{title}\" as {target.name} in {p.name}.")


def verify_document(args, result):
    from core import verification as V
    if not result.ok:
        return V.failed(result.error)
    path = result.data.get("path")
    v = V.file_valid(path, min_bytes=60)
    if not v.ok:
        return v
    if str(path).endswith(".docx"):
        import zipfile
        try:
            with zipfile.ZipFile(path) as z:
                body = z.read("word/document.xml").decode("utf-8", "ignore")
            words = len(re.findall(r"<w:t[^>]*>([^<]+)", body))
            if words < 1:
                return V.failed("The document has no text in it.")
            return V.verified(f"Valid Word document with {words} text runs.")
        except Exception as e:
            return V.failed(f"The .docx is not a valid Word file ({type(e).__name__}).")
    return V.verified(f"{Path(path).name} written ({Path(path).stat().st_size:,} bytes).")


# ═════════════════════════════════════════════════════════════════════════════
# Project analysis
# ═════════════════════════════════════════════════════════════════════════════

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".env", "dist",
             "build", ".next", ".idea", ".vscode", ".mypy_cache", ".pytest_cache", "target", "bin", "obj",
             ".gradle", "coverage", ".cache", "site-packages"}
SECRET_FILE = re.compile(r"(^\.env|\.pem$|\.key$|\.p12$|\.pfx$|id_rsa|id_ed25519|credential|secret|token|"
                         r"api_keys\.json$|\.keystore$|\.jks$)", re.I)
LANG = {".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript",
        ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".java": "Java", ".kt": "Kotlin", ".cs": "C#",
        ".cpp": "C++", ".c": "C", ".h": "C/C++ header", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
        ".php": "PHP", ".swift": "Swift", ".dart": "Dart", ".sql": "SQL", ".sh": "Shell", ".ps1": "PowerShell",
        ".vue": "Vue", ".svelte": "Svelte", ".json": "JSON", ".md": "Markdown", ".yml": "YAML", ".yaml": "YAML"}
MANIFESTS = {"requirements.txt": "pip", "pyproject.toml": "Python project", "setup.py": "setuptools",
             "package.json": "Node.js", "Cargo.toml": "Rust/Cargo", "go.mod": "Go modules", "pom.xml": "Maven",
             "build.gradle": "Gradle", "Gemfile": "Bundler", "composer.json": "Composer",
             "Dockerfile": "Docker", "docker-compose.yml": "Docker Compose", "pubspec.yaml": "Flutter"}
MAX_FILES = 6000


def resolve_project_path(args: dict, ctx) -> Optional[Path]:
    raw = str(args.get("path") or args.get("choice") or "").strip().strip('"')
    if raw:
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        if p.is_dir():
            return p
        try:                                      # a project name the user uses
            from memory.identity import PersonalModel
            m = PersonalModel()
            name = m.match_project(raw)
            if name and m.projects()[name].get("path") and Path(m.projects()[name]["path"]).is_dir():
                return Path(m.projects()[name]["path"])
        except Exception:
            pass
        try:
            from core.tools.builtin import resolve_folder
            return resolve_folder(raw)
        except Exception:
            return None
    # nothing given → the project the context points at
    try:
        from memory.identity import PersonalModel
        m = PersonalModel()
        req = str(_extras(ctx).get("task_request") or "")
        name = m.match_project(req)
        if not name:
            from memory.episodic import EpisodicStore
            name = EpisodicStore().last_project()
        if name and m.projects().get(name, {}).get("path"):
            p = Path(m.projects()[name]["path"])
            if p.is_dir():
                return p
    except Exception:
        pass
    return None


def scan_project(root: Path) -> dict:
    files, loc = [], Counter()
    langs = Counter()
    skipped_secret = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if len(files) >= MAX_FILES:
                break
            p = Path(dirpath) / fn
            if SECRET_FILE.search(fn):
                skipped_secret += 1
                continue
            ext = p.suffix.lower()
            try:
                size = p.stat().st_size
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            files.append((rel, size, ext))
            if ext in LANG and size < 2_000_000:
                try:
                    with open(p, "rb") as f:
                        n = sum(1 for _ in f)
                    loc[LANG[ext]] += n
                    langs[LANG[ext]] += 1
                except OSError:
                    pass
    top = sorted({f.split("/")[0] + ("/" if "/" in f else "") for f, _, _ in files})[:40]
    stack = sorted({v for k, v in MANIFESTS.items() if (root / k).exists()})
    readme = ""
    for cand in ("README.md", "readme.md", "README.txt", "README", "readme.txt"):
        if (root / cand).is_file():
            readme = (root / cand).read_text(encoding="utf-8", errors="ignore")[:3000]
            break
    return {"path": str(root), "name": root.name, "files": len(files), "loc": sum(loc.values()),
            "languages": dict(loc.most_common(8)), "stack": stack, "top_level": top, "readme": readme,
            "skipped_secret_files": skipped_secret, "file_list": files}


def _key_files(scan: dict, budget: int = 48000) -> list[tuple[str, str]]:
    root = Path(scan["path"])
    entry_names = {"main.py", "app.py", "__main__.py", "index.js", "index.ts", "server.js", "main.ts",
                   "manage.py", "setup.py", "package.json", "requirements.txt", "pyproject.toml",
                   "index.html", "App.tsx", "App.jsx", "main.go", "main.rs", "Program.cs"}
    code = [(rel, size) for rel, size, ext in scan["file_list"] if ext in LANG and ext not in (".md", ".json")
            or rel.split("/")[-1] in entry_names]
    prio = sorted(code, key=lambda x: (x[0].split("/")[-1] not in entry_names, x[0].count("/"), -x[1]))
    out, used = [], 0
    from core.secret_store import redact
    for rel, size in prio:
        if used >= budget:
            break
        try:
            txt = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        head = redact(txt[: min(6000, budget - used)])
        out.append((rel, head))
        used += len(head)
    return out


def t_project_scan(args: dict, ctx) -> ToolResult:
    root = resolve_project_path(args, ctx)
    if root is None:
        return ToolResult(False, {"needs_input": True}, "", "Which folder is the project in? (say the path "
                                                            "or the folder name)")
    s = scan_project(root)
    try:
        from memory.identity import PersonalModel
        PersonalModel().register_project(args.get("name") or root.name, str(root),
                                         stack=list(s["languages"])[:5] + s["stack"], source="task")
    except Exception:
        pass
    langs = ", ".join(f"{k} ({v:,} lines)" for k, v in list(s["languages"].items())[:4])
    msg = f"{s['name']}: {s['files']:,} files, {s['loc']:,} lines of code — {langs or 'no source files'}."
    return ToolResult(True, {k: v for k, v in s.items() if k != "file_list"}, msg)


def t_project_analyze(args: dict, ctx) -> ToolResult:
    root = resolve_project_path(args, ctx)
    if root is None:
        return ToolResult(False, {"needs_input": True}, "", "Which folder is the project in? (say the path "
                                                            "or the folder name)")
    _progress(ctx, f"Scanning {root.name}…")
    s = scan_project(root)
    if s["files"] == 0:
        return ToolResult.failure(f"{root} has no readable files.")
    try:
        from memory.identity import PersonalModel
        PersonalModel().register_project(args.get("name") or root.name, str(root),
                                         stack=list(s["languages"])[:5] + s["stack"], source="task")
    except Exception:
        pass
    if _stopped(ctx):
        return ToolResult.failure("Stopped.")
    files = _key_files(s)
    focus = str(args.get("focus") or "").strip()
    tree = "\n".join(s["top_level"])
    excerpt = "\n\n".join(f"### {rel}\n```\n{body}\n```" for rel, body in files)
    _progress(ctx, f"Reviewing {len(files)} key files with deep reasoning…")
    prompt = (
        "You are a senior software architect reviewing a project. Write a Markdown report with sections: "
        "'# <project> — project analysis', '## What it is', '## Architecture' (components and how they "
        "talk), '## Tech stack', '## Strengths', '## Risks & bugs' (concrete, file-referenced), "
        "'## Security notes', '## Recommended next steps' (prioritised, actionable). Be specific; say "
        "when something is inferred from partial excerpts.\n"
        + (f"Focus requested by the user: {focus}\n" if focus else "")
        + f"\nPROJECT: {s['name']} — {s['files']} files, {s['loc']} LOC, languages {s['languages']}, "
          f"manifests {s['stack']}\nTOP LEVEL:\n{tree}\n\nREADME (excerpt):\n{s['readme'][:2500]}\n\n"
          f"KEY FILES (excerpts):\n{excerpt}"
    )
    try:
        text = (llm(prompt, tier="deep", timeout=180.0) or "").strip()
    except Exception as e:
        from core.secret_store import redact
        return ToolResult.failure(f"The analysis model failed: {redact(e)[:160]}", retryable=True)
    if len(text) < 200:
        return ToolResult.failure("The analysis came back empty.", retryable=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H%M")
    out_dir = workspace("research", f"{s['name']} analysis", reuse=True)
    path = _write_text(out_dir / f"{slugify(s['name'])} analysis {stamp}.md",
                       text + f"\n\n---\nScanned {s['files']} files ({s['loc']:,} LOC); reviewed "
                              f"{len(files)} key files; skipped {s['skipped_secret_files']} secret-looking "
                              f"files. Generated by JARVIS {stamp}.\n")
    m = re.search(r"##\s*What it is\s*\n+(.+?)(?:\n##|\Z)", text, re.S)
    summary = re.sub(r"\s+", " ", m.group(1)).strip()[:600] if m else text[:500]
    _record_episode("artifact", f"Analysed project {s['name']}", summary[:300], paths=[str(path)],
                    project=args.get("name") or s["name"], tags=["project-analysis"])
    return ToolResult(True, {"analysis": text, "summary": summary, "path": str(path), "project": s["name"],
                             "files": s["files"], "loc": s["loc"], "reviewed_files": [r for r, _ in files]},
                      f"Analysed {s['name']} ({s['files']:,} files, {s['loc']:,} lines). Report saved as "
                      f"{path.name}.")


# ═════════════════════════════════════════════════════════════════════════════
# Registration
# ═════════════════════════════════════════════════════════════════════════════

def register(reg) -> None:
    from core import verification as V
    from core.permissions import RiskLevel
    from core.tools.registry import ToolSpec
    S, L, M = RiskLevel.SAFE, RiskLevel.LOW_RISK, RiskLevel.MEDIUM_RISK
    STR = {"type": "string"}

    def obj(props, req=()):
        return {"type": "object", "properties": props, "required": list(req)}

    def _v_collect(a, r):
        if not r.ok:
            return V.failed(r.error)
        v = V.path_exists(r.data.get("path"), "file")
        if not v.ok:
            return v
        return V.verified(f"{r.data.get('count')} sources stored.") if r.data.get("count") else \
            V.failed("No sources were collected.")

    def _v_analyze(a, r):
        if not r.ok:
            return V.failed(r.error)
        v = V.path_exists(r.data.get("path"), "file")
        return v if not v.ok else V.text_nonempty(r.data.get("analysis"), "analysis")

    reg.register(ToolSpec("research.collect", "Research a topic on the web: plan sub-queries, search, read the "
                          "best pages and store numbered sources.", t_research_collect,
                          obj({"topic": STR, "depth": STR, "max_sources": {"type": "integer"}, "dir": STR},
                              ["topic"]),
                          {"dir": "str", "path": "str", "count": "int"}, agent="research", risk=L,
                          retry_safe=True, verify=_v_collect))
    reg.register(ToolSpec("research.analyze", "Synthesise collected sources into a cited research brief "
                          "(deep reasoning).", t_research_analyze,
                          obj({"path": STR, "dir": STR, "topic": STR, "focus": STR}),
                          {"analysis": "str", "summary": "str", "path": "str"}, agent="research", risk=S,
                          retry_safe=True, verify=_v_analyze))
    reg.register(ToolSpec("document.write", "Write Markdown content to a new .docx / .md / .html / .txt file "
                          "(never overwrites).", t_document_write,
                          obj({"content": STR, "title": STR, "format": STR, "folder": STR, "dir": STR,
                               "filename": STR}, ["content"]),
                          {"path": "str"}, agent="file", risk=L, verify=verify_document))
    reg.register(ToolSpec("project.scan", "Scan a software project folder: size, languages, stack, structure "
                          "(read-only).", t_project_scan, obj({"path": STR, "name": STR, "choice": STR}),
                          {"path": "str", "files": "int"}, agent="coding", risk=S))
    reg.register(ToolSpec("project.analyze", "Analyse a whole software project (architecture, risks, next "
                          "steps) and save a report. Sends code excerpts, never secrets, to the AI model.",
                          t_project_analyze, obj({"path": STR, "name": STR, "focus": STR, "choice": STR}),
                          {"analysis": "str", "path": "str"}, agent="coding", risk=L, retry_safe=True,
                          verify=_v_analyze))
