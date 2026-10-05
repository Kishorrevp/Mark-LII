"""
core/agents/planner.py — turns a request into an explicit, dependency-aware
list of steps over registered tools.

    "Find my final year project PDF, rename it to Healthcare_Project_Final.pdf,
     move it to my Projects folder, summarize it, and prepare a message for Arun."
    →
      s1 file.find              name="final year project", extension=pdf
      s2 file.select            matches=$s1.matches            (verify correct file)   ← s1
      s3 file.rename            path=$s2.path, new_name=...                            ← s2
      s4 file.move              path=$s3.path, destination=Projects                    ← s3
      s5 document.extract_text  path=$s4.path                                          ← s4
      s6 document.summarize     text=$s5.text                                          ← s5
      s7 message.prepare        recipient=Arun, content=$s6.summary                    ← s6
      s8 message.send           recipient=Arun, text=$s7.draft  (HIGH → permission)    ← s7

TWO PLANNERS, ONE VALIDATOR
  1. Rule planner (default, instant, offline, free): recognises the common
     verbs — find, rename, move, copy, delete, open, read, summarise,
     search, download, message/send — in English, Tamil and Tanglish (via
     core/multilingual.normalize). Tracks "it" (the last file) and "that"
     (the last text) so later clauses refer to earlier results.
  2. LLM planner (fallback when a clause isn't recognised): asks the
     configured AI provider for a JSON plan restricted to the registry's
     tool catalog.

  Every plan — rules or LLM — goes through validate_plan(): unknown tools
  rejected, args checked against each tool's input schema ($refs allowed),
  references must point to EARLIER steps (so the graph is acyclic by
  construction), max 15 steps. An invalid LLM plan is rejected, never run.

MISSING DETAILS
  A value the user didn't give ("rename it" — to what?) becomes
  "$ask:What should I rename it to?". The orchestrator stops at that step in
  WAITING_FOR_INPUT and asks, instead of guessing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from core.multilingual import detect_language, normalize
from core.tasks.state import Step
from core.tools.registry import ToolRegistry

MAX_STEPS = 15
REF_RE = re.compile(r"\$(s\d+)\.([A-Za-z_][\w]*)")

_EXTS = {"pdf": "pdf", "pdfs": "pdf", "docx": "docx", "word": "docx", "doc": "docx", "pptx": "pptx",
         "ppt": "pptx", "powerpoint": "pptx", "presentation": "pptx", "excel": "xlsx", "xlsx": "xlsx",
         "spreadsheet": "xlsx", "csv": "csv", "txt": "txt", "text file": "txt", "image": "png",
         "png": "png", "jpg": "jpg", "jpeg": "jpg", "photo": "jpg", "zip": "zip", "mp4": "mp4",
         "video": "mp4", "mp3": "mp3"}
_FILLER = r"(?:my|the|a|an|this|that|our|jarvis|please|file|document)"
_PRONOUN = re.compile(r"^(?:it|this|that|them|the file|this file|that file|the document|the pdf|"
                      r"this pdf|the doc)$", re.I)


@dataclass
class Plan:
    steps: list = field(default_factory=list)
    source: str = "rules"            # rules | llm
    ok: bool = True
    reason: str = ""
    unhandled: list = field(default_factory=list)
    language: str = "en"

    def summary(self) -> str:
        return " → ".join(s.description for s in self.steps)

    def to_dict(self) -> dict:
        return {"source": self.source, "ok": self.ok, "reason": self.reason,
                "steps": [{"id": s.id, "tool": s.tool, "agent": s.agent, "args": s.args,
                           "depends_on": s.depends_on, "description": s.description}
                          for s in self.steps], "unhandled": self.unhandled}


# ═════════════════════════════════════════════════════════════════════════════

def refs_in(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, str):
        out.update(m.group(1) for m in REF_RE.finditer(value))
    elif isinstance(value, dict):
        for v in value.values():
            out |= refs_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out |= refs_in(v)
    return out


def validate_plan(plan: Plan, registry: ToolRegistry, prior_ids: tuple | list = ()) -> list[str]:
    """`prior_ids` = steps that already completed (a recovery plan may use their outputs)."""
    errors: list[str] = []
    if not plan.steps:
        return ["The plan has no steps."]
    if len(plan.steps) > MAX_STEPS:
        errors.append(f"Plan has {len(plan.steps)} steps (max {MAX_STEPS}).")
    seen: list[str] = list(prior_ids)
    for s in plan.steps:
        if s.id in seen:
            errors.append(f"Duplicate step id {s.id}.")
        if len(s.id) > 12 or not re.fullmatch(r"[A-Za-z]\w*", s.id or ""):
            errors.append(f"Bad step id '{s.id}'.")
        spec = registry.get(s.tool)
        if spec is None:
            errors.append(f"{s.id}: unknown tool '{s.tool}'.")
            seen.append(s.id)
            continue
        if getattr(spec.handler, "_live_only", False):
            errors.append(f"{s.id}: tool '{s.tool}' only works directly in the voice session.")
        for e in registry.validate_args(s.tool, s.args, allow_refs=True):
            errors.append(f"{s.id} ({s.tool}): {e}")
        refs = refs_in(s.args)
        for dep in set(s.depends_on) | refs:
            if dep not in seen:
                errors.append(f"{s.id}: depends on '{dep}', which is not an earlier step.")
        # references imply dependencies
        for r in refs:
            if r not in s.depends_on:
                s.depends_on.append(r)
        seen.append(s.id)
    return errors


# ═════════════════════════════════════════════════════════════════════════════
# Rule planner
# ═════════════════════════════════════════════════════════════════════════════

# "and" only separates clauses when a new command verb follows it — so
# "search for cats and dogs" / "a website with a gallery and a contact form"
# stay one clause, while "move it and summarize it" still splits (Phase 5).
_VERBS = (r"find|locate|search|google|look|research|investigate|open|launch|start|run|rename|move|copy|"
          r"delete|remove|read|summari[sz]e|sum|send|message|text|email|write|make|create|prepare|draft|"
          r"generate|download|put|save|play|close|remind|set|tell|show|analy[sz]e|review|audit|build|scan|"
          r"compare|explain|translate|check|convert|turn|export|share|give|keep|draw|sketch|paint|study")
_SPLIT = re.compile(rf"\s*(?:,\s*(?:and\s+)?|;|\band then\b|\bthen\b|\bafter that\b|\band\b(?=\s+(?:{_VERBS})\b))\s*",
                    re.I)


class _Builder:
    def __init__(self, registry: ToolRegistry, agent_of: Callable[[str], str]):
        self.steps: list[Step] = []
        self.reg = registry
        self.agent_of = agent_of
        self.file_ref: Optional[str] = None      # "$sN.path"
        self.file_step: Optional[str] = None
        self.text_ref: Optional[str] = None      # "$sN.<field>"
        self.text_step: Optional[str] = None
        self.last_download: Optional[Step] = None
        self.research_dir: Optional[str] = None  # "$sN.dir" of a research workspace

    def add(self, tool: str, args: dict, desc: str, deps: list | None = None) -> Step:
        sid = f"s{len(self.steps) + 1}"
        deps = list(deps or [])
        for r in refs_in(args):
            if r not in deps:
                deps.append(r)
        st = Step(id=sid, description=desc, tool=tool, args=args, agent=self.agent_of(tool),
                  depends_on=deps)
        self.steps.append(st)
        return st

    def set_file(self, st: Step, field: str = "path"):
        self.file_ref, self.file_step = f"${st.id}.{field}", st.id

    def set_text(self, st: Step, field: str):
        self.text_ref, self.text_step = f"${st.id}.{field}", st.id


def _clean_target(s: str) -> str:
    s = re.sub(rf"^(?:{_FILLER}\s+)+", "", s.strip(), flags=re.I)
    s = re.sub(r"\s+(?:file|document|folder)$", "", s, flags=re.I)
    return s.strip(" .!?\"'")


def _project_hint(text: str) -> str:
    """Pull a project path or name out of a request, e.g. 'in my Mark-LII project'
    or 'in C:\\Users\\me\\app'. Empty → let code.task resolve it from context."""
    m = re.search(r"\b(?:in|for|of|on|to|inside)\s+(?:my|the)?\s*"
                  r"(?P<name>[\"']?[A-Za-z]:[\\/][^\"'\n]+[\"']?|~[\\/][^\"'\n]+|"
                  r"[\w][\w .\-]{1,60}?)\s*(?:project|codebase|code ?base|repo(?:sitory)?|folder|app)\b",
                  text, re.I)
    if m:
        return m.group("name").strip().strip("\"'")
    m = re.search(r"\b(?:project|repo(?:sitory)?|folder)\s+(?:called|named)\s+"
                  r"(?P<name>[\"']?[\w .\-]{1,60}[\"']?)", text, re.I)
    return m.group("name").strip().strip("\"'") if m else ""


def _split_ext(what: str) -> tuple[str, str]:
    """"final year project PDF" → ("final year project", "pdf")."""
    words = what.split()
    ext = ""
    kept = []
    for w in words:
        lw = w.lower().strip(".,")
        if lw in _EXTS and not ext:
            ext = _EXTS[lw]
            continue
        m = re.match(r"^(.+)\.(pdf|docx|pptx|xlsx|csv|txt|png|jpg|zip|mp4|mp3)$", w, re.I)
        if m:
            kept.append(m.group(1))
            ext = ext or m.group(2).lower()
            continue
        kept.append(w)
    name = _clean_target(" ".join(kept))
    return name, ext


def _add_find(b: _Builder, what: str) -> None:
    name, ext = _split_ext(what)
    label = (name + (f" {ext.upper()}" if ext else "")).strip() or "the file"
    f = b.add("file.find", {k: v for k, v in {"name": name, "extension": ext}.items() if v},
              f"Find {label}")
    sel = b.add("file.select", {"matches": f"${f.id}.matches"}, f"Verify the correct file ({label})")
    b.set_file(sel)


def _need_file(b: _Builder, target: str) -> Optional[str]:
    """Return a $ref for the file a clause is about — "it" → last file; a
    named file → add find+select steps for it."""
    t = _clean_target(target or "")
    if not t or _PRONOUN.match(t) or _PRONOUN.match(target.strip()):
        return b.file_ref
    _add_find(b, t)
    return b.file_ref


def _add_team_build(b: "_Builder", goal: str, open_result: bool = True, run_site: bool = True) -> None:
    """Manager → Research → UI → Coding → Testing → Review → (run the site) → open."""
    p = b.add("team.plan", {"goal": goal}, "Manager Agent: plan the build")
    d = f"${p.id}.dir"
    b.add("team.research", {"dir": d}, "Research Agent: gather the facts")
    b.add("team.design", {"dir": d}, "UI Agent: design the look and layout")
    b.add("team.implement", {"dir": d}, "Coding Agent: write the code")
    b.add("team.test", {"dir": d}, "Testing Agent: check the code")
    rv = b.add("team.review", {"dir": d}, "Review Agent: review, fix and approve")
    b.set_file(rv, "entry")
    if run_site:
        # actually run it in a browser and open the running preview
        b.add("web.run", {"dir": d}, "Web Dev Agent: run the site and check it in a browser")
        b.add("web.open", {"dir": d}, "Open the running site in your browser")
    elif open_result:
        b.add("file.open", {"path": f"${rv.id}.entry"}, "Open the result")


def _rule_clause(b: _Builder, raw: str, norm: str) -> bool:
    """Try to turn one clause into steps. Returns False if not recognised."""
    c = norm.strip().rstrip(".!?")
    c_orig = raw.strip().rstrip(".!?")
    low = c.lower()
    low = re.sub(r"^(?:jarvis|please|can you|could you|pls)\s+", "", low)
    low = re.sub(r"^(?:jarvis|please)\s+", "", low)

    # ── URL present: open or download ──────────────────────────────────────
    url_m = re.search(r"https?://\S+", c_orig)

    # ── download ───────────────────────────────────────────────────────────
    m = re.match(r"^download\s+(?P<what>.*)$", low)
    if m:
        if url_m:
            st = b.add("browser.download", {"url": url_m.group(0)}, "Download the file")
        elif b.text_ref:
            _, ext = _split_ext(m.group("what"))
            u = b.add("text.extract_url", {"text": b.text_ref, **({"extension": ext} if ext else {})},
                      "Pick the download link from the results")
            st = b.add("browser.download", {"url": f"${u.id}.url"}, f"Download {m.group('what') or 'the file'}")
        else:
            st = b.add("browser.download", {"url": "$ask:Which link should I download?"},
                       "Download the file")
        b.last_download = st
        b.set_file(st)
        return True

    # ── rename ─────────────────────────────────────────────────────────────
    m = re.match(r"^rename\s+(?P<what>.*?)(?:\s+(?:to|as|into)\s+(?P<new>.+))?$", c_orig, re.I)
    if m and low.startswith("rename"):
        ref = _need_file(b, m.group("what") or "it")
        if not ref:
            return False
        new = (m.group("new") or "").strip().strip("\"'")
        st = b.add("file.rename", {"path": ref, "new_name": new or "$ask:What should I rename it to?"},
                   f"Rename it to {new}" if new else "Rename the file")
        b.set_file(st)
        return True

    # ── move / put ─────────────────────────────────────────────────────────
    m = re.match(r"^(?:(?:jarvis|please)\s+)*(?:move|put|place|shift)\s+(?P<what>.*?)"
                 r"(?:\s+(?:to|into|in|inside)\s+(?P<dest>.+))?$", c_orig, re.I)
    if m:
        dest = _clean_target(m.group("dest") or "")
        if b.last_download is not None and b.file_step == b.last_download.id and dest:
            # "download X and put it in Downloads" → download straight there
            b.last_download.args["destination"] = dest
            b.last_download.description += f" into {dest}"
            return True
        ref = _need_file(b, m.group("what") or "it")
        if not ref:
            return False
        st = b.add("file.move", {"path": ref, "destination": dest or "$ask:Which folder should I move it to?"},
                   f"Move it to {dest}" if dest else "Move the file")
        b.set_file(st)
        return True

    # ── copy ───────────────────────────────────────────────────────────────
    m = re.match(r"^(?:(?:jarvis|please)\s+)*copy\s+(?P<what>.*?)(?:\s+(?:to|into|in)\s+(?P<dest>.+))?$",
                 c_orig, re.I)
    if m:
        ref = _need_file(b, m.group("what") or "it")
        if not ref:
            return False
        dest = _clean_target(m.group("dest") or "")
        st = b.add("file.copy", {"path": ref, "destination": dest or "$ask:Which folder should I copy it to?"},
                   f"Copy it to {dest}" if dest else "Copy the file")
        return True

    # ── delete ─────────────────────────────────────────────────────────────
    m = re.match(r"^(?:delete|remove|trash|erase)\s+(?P<what>.*)$", low)
    if m:
        ref = _need_file(b, m.group("what") or "it")
        if not ref:
            return False
        b.add("file.delete", {"path": ref}, "Delete the file (needs your confirmation)")
        b.file_ref = None
        return True

    # ── find ───────────────────────────────────────────────────────────────
    m = re.match(r"^(?:find|locate|look for|get|search for|search)\s+(?P<what>.+)$", low)
    if m and (any(e in m.group("what").split() for e in _EXTS) or re.search(r"\bmy\b|\bfile\b", m.group("what"))
              or re.search(r"\.\w{2,4}\b", m.group("what"))):
        orig = re.match(r"^(?:\w+\s+){0,3}?(?:find|locate|look for|get|search for|search)\s+(?P<w>.+)$",
                        c_orig, re.I)
        _add_find(b, orig.group("w") if orig else m.group("what"))
        return True

    # ── Phase 5: project analysis ──────────────────────────────────────────
    m = re.match(r"^(?P<verb>analy[sz]e|review|audit|assess|scan|inspect|examine|study)\s+(?:my\s+|the\s+|our\s+|this\s+)?"
                 r"(?:entire\s+|whole\s+|full\s+|complete\s+)?(?P<name>[\w .'-]*?)\s*"
                 r"(?:software\s+|code\s+|coding\s+)?(?:project|codebase|code base|repo(?:sitory)?|source code)"
                 r"(?P<rest>.*)$", low)
    if m:
        pm = re.search(r"\b(?:at|in|from|called|named)\s+(?P<p>.+)$", c_orig, re.I)
        name = re.sub(r"\b(?:entire|whole|full|complete|software|my|the|our)\b", "", m.group("name") or "").strip()
        target = (pm.group("p").strip().strip('"') if pm else "") or name
        focus = ""
        fm = re.search(r"\b(?:focus(?:ing)? on|for)\s+(?P<f>.+)$", m.group("rest") or "")
        if fm and not pm:
            focus = fm.group("f").strip()
        tool = "project.scan" if m.group("verb") == "scan" else "project.analyze"
        args = {"path": target} if target else {}
        if focus:
            args["focus"] = focus
        st = b.add(tool, args, ("Scan" if tool == "project.scan" else "Analyse") +
                   (f" the {target} project" if target else " the project"))
        if tool == "project.analyze":
            b.set_text(st, "analysis")
            b.set_file(st, "path")
        return True

    # ── Phase 5: long-running research (collect → analyse) ───────────────
    m = re.match(r"^(?P<depth>quick(?:ly)?\s+|deep(?:ly)?\s+|thorough(?:ly)?\s+|in[- ]depth\s+)?"
                 r"(?:research|investigate|do (?:a |some )?(?:deep |quick )?research (?:on|about|into)|"
                 r"deep[- ]dive (?:into|on)|study up on)\s+(?:about\s+|on\s+|into\s+)?(?P<q>.+)$", low)
    if m and not re.match(r"^(?:it|this|that)$", m.group("q").strip()):
        mq = re.search(r"(?:research|investigate|research (?:on|about|into)|deep[- ]dive (?:into|on)|study up on)"
                       r"\s+(?:about\s+|on\s+|into\s+)?(?P<q>.+)$", c_orig, re.I)
        topic = (mq.group("q") if mq else m.group("q")).strip()
        d = (m.group("depth") or "").strip().lower()
        depth = "quick" if d.startswith("quick") else ("deep" if d else "standard")
        if re.search(r"\b(?:in[- ]depth|thoroughly|in detail|deeply)\b", low):
            depth = "deep"
        col = b.add("research.collect", {"topic": topic, "depth": depth}, f"Collect sources on {topic}")
        an = b.add("research.analyze", {"path": f"${col.id}.path", "topic": topic},
                   f"Analyse the sources on {topic}")
        b.set_text(an, "analysis")
        b.set_file(an, "path")
        b.research_dir = f"${col.id}.dir"
        return True

    # ── Phase 5: write the result into a document ─────────────────────────
    m = re.match(r"^(?:write|make|create|prepare|generate|draft|put|save|turn|export)\s+(?:it|that|this|them|"
                 r"the results?|everything)?\s*(?:up\s+)?(?:in(?:to)?\s+|as\s+)?(?:a\s+|an\s+|the\s+)?"
                 r"(?P<fmt>word|docx|markdown|md|html|text|txt|pdf)?\s*(?:file\s+)?"
                 r"(?P<kind>report|document|doc|brief|notes|write-?up|summary document|file)\b"
                 r"(?:\s+(?:about|on)\s+(?P<about>.+?))?(?:\s+(?:as|in)\s+(?:a\s+)?(?P<fmt2>word|docx|markdown|md|"
                 r"html|text|txt|pdf)(?:\s+file)?)?$", low)
    if m:
        fmt_word = (m.group("fmt") or m.group("fmt2") or "docx").lower()
        fmt = {"word": "docx", "docx": "docx", "markdown": "md", "md": "md", "html": "html", "text": "txt",
               "txt": "txt", "pdf": "docx"}.get(fmt_word, "docx")
        if m.group("about") and not b.text_ref:
            ma = re.search(r"\b(?:about|on)\s+(?P<a>.+?)(?:\s+(?:as|in)\s+(?:a\s+)?(?:word|docx|markdown|md|html|"
                           r"text|txt|pdf)(?:\s+file)?)?$", c_orig, re.I)
            topic = (ma.group("a") if ma else m.group("about")).strip()
            col = b.add("research.collect", {"topic": topic}, f"Collect sources on {topic}")
            an = b.add("research.analyze", {"path": f"${col.id}.path", "topic": topic},
                       f"Analyse the sources on {topic}")
            b.set_text(an, "analysis")
            b.research_dir = f"${col.id}.dir"
        if not b.text_ref:
            return False
        args = {"content": b.text_ref, "format": fmt}
        if getattr(b, "research_dir", None):
            args["folder"] = b.research_dir
        st = b.add("document.write", args, f"Write the {m.group('kind')} ({fmt.upper()})")
        b.set_file(st, "path")
        return True

    # ── Phase 5: multi-agent build ───────────────────────────────────────
    if re.match(r"^(?:build|make|create|develop|design|code|generate)\s+(?:me\s+|us\s+)?", low) and \
            re.search(r"\b(website|web ?site|web ?page|landing page|portfolio|web app|homepage|blog site|"
                      r"personal site|site)\b", low):
        goal = re.sub(r"^(?:jarvis[, ]+|please\s+)+", "", c_orig, flags=re.I).strip()
        _add_team_build(b, goal)
        return True

    # ── Phase 6: change existing code (fix / add / refactor a project) ─────
    mcode = re.match(r"^(?:jarvis[, ]+|please\s+)*(?:can you\s+)?"
                     r"(?P<verb>fix|debug|add|implement|refactor|change|update|improve|optimi[sz]e|rename|"
                     r"remove|delete)\b(?P<rest>.*)$", low)
    if mcode and re.search(r"\b(bug|error|feature|function|method|class|test|code|project|module|file|"
                           r"in (?:my|the) (?:project|code|app|repo|codebase|script))\b", low) and \
            not re.search(r"\b(website|web ?site|landing page|portfolio)\b", low):
        req = re.sub(r"^(?:jarvis[, ]+|please\s+)+", "", c_orig, flags=re.I).strip()
        args = {"request": req}
        proj = _project_hint(c_orig)
        if proj:
            args["project"] = proj
        st = b.add("code.task", args, "Coding Agent: make the change and verify it")
        b.set_file(st, "path")
        return True

    # ── Phase 7: references — "keep this as a reference", "review this" ────
    mref = re.match(r"^(?:keep|save|store|use|take|mark|treat)\s+(?P<what>.+?)\s+(?:as|for)\s+(?:an?\s+|my\s+)?"
                    r"(?:reference|inspiration|example)\b", low) or \
        re.match(r"^(?:review|look at|study|check out|refer to|go through)\s+(?P<what>this|that|it|my screen|"
                 r"the (?:screen|page|site|website|window|clipboard|design|ui|layout|document|file|link))\s*$", low)
    if mref:
        what = mref.group("what").strip()
        url = re.search(r"https?://\S+", c_orig)
        path = re.search(r"[A-Za-z]:[\\/][^\s\"']+", c_orig)
        target = (url.group(0).rstrip(".,)") if url else path.group(0) if path else
                  "clipboard" if "clipboard" in what or "copied" in what else
                  "window" if "window" in what else "screen")
        b.add("reference", {"action": "save", "target": target, "note": c_orig[:200]},
              f"Keep {'the ' + target if target in ('screen', 'window', 'clipboard') else target[:50]} as a reference")
        return True

    # ── Phase 7: draw ON SCREEN with the mouse (Paint) ────────────────────
    mpaint = re.match(r"^(?:jarvis[, ]+|please\s+)*(?:can you\s+)?(?:draw|sketch|paint|doodle)\s+(?:me\s+)?"
                      r"(?P<what>.*)$", low)
    if mpaint and not re.search(r"\b(logo|icon|illustration|image|svg|poster|banner|vector|png|jpe?g|file|"
                                r"website|web ?site|diagram|chart|graph|avatar|emblem|badge)\b", low) and (
            re.search(r"\b(?:in|on|using|with)\s+(?:ms\s+|microsoft\s+)?paint\b|\bon (?:the |my )?screen\b|"
                      r"\bwith (?:the )?mouse\b", low)
            or re.search(r"\b(?:something|anything)\b", low)
            or not re.search(r"\b(picture|drawing|artwork|graphic)\b", low)):
        subj = re.sub(r"\b(?:in|on|using|with)\s+(?:ms\s+|microsoft\s+)?paint\b|\bon (?:the |my )?screen\b|"
                      r"\bwith (?:the )?mouse\b|\bfor me\b", "", mpaint.group("what")).strip(" .,!")
        subj = re.sub(r"^(?:a|an|the|some)\s+", "", subj)
        st = b.add("draw.paint", {"subject": subj}, f"Draw {subj[:50] or 'something'} in Paint")
        return True

    # ── Phase 6: draw / make a picture ────────────────────────────────────
    mdraw = re.match(r"^(?:jarvis[, ]+|please\s+)*(?:can you\s+)?"
                     r"(?:draw|sketch|illustrate|design|make|create|generate)\s+(?:me\s+)?(?:a|an|the|some)?\s*"
                     r"(?P<what>.*)$", low)
    if mdraw and re.search(r"\b(logo|icon|illustration|picture|image|drawing|graphic|poster|svg|artwork|"
                           r"vector|banner|avatar|emblem|badge)\b", low) and \
            not re.search(r"\b(website|web ?site|landing page|app|program|script|function)\b", low):
        what = re.sub(r"^(?:jarvis[, ]+|please\s+)+", "", c_orig, flags=re.I).strip()
        vector = bool(re.search(r"\b(svg|vector|logo|icon|diagram)\b", low))
        tool = "design.vector" if vector else "design.image"
        st = b.add(tool, {"prompt": what}, f"{'Draw a vector' if vector else 'Generate an image'} for: {what[:60]}")
        b.set_file(st, "svg" if vector else "path")
        return True

    # ── search the web / research ──────────────────────────────────────────
    m = re.match(r"^(?:search|google|look up|research|find out)\s+(?:the\s+web\s+|online\s+|internet\s+)?"
                 r"(?:for\s+|about\s+|on\s+)?(?P<q>.*)$", low)
    if m:
        q = m.group("q").strip()
        mq = re.match(r"^(?:(?:jarvis|please)\s+)*(?:search|google|look up|research|find out)\s+"
                      r"(?:the\s+web\s+|online\s+|internet\s+)?(?:for\s+|about\s+|on\s+)?(?P<q>.*)$",
                      c_orig, re.I)
        if mq and mq.group("q").strip().lower() == q:
            q = mq.group("q").strip()
        if not q or q in ("this", "it", "that", "the web", "web"):
            q = "$ask:What should I search for?"
        st = b.add("web.search", {"query": q}, f"Search the web for {q}" if not q.startswith("$ask")
                   else "Search the web")
        b.set_text(st, "text")
        return True

    # ── summarise ──────────────────────────────────────────────────────────
    m = re.match(r"^(?:summari[sz]e|summary of|give me a summary of|sum up)\s*(?P<what>.*)$", low)
    if m:
        what = m.group("what").strip()
        is_file_target = (not what or _PRONOUN.match(what)) and b.file_ref and \
            (b.file_step is not None and (b.text_step is None or int(b.file_step[1:]) > int(b.text_step[1:])))
        if is_file_target:
            ex = b.add("document.extract_text", {"path": b.file_ref}, "Read the document")
            sm = b.add("document.summarize", {"text": f"${ex.id}.text", "path": b.file_ref},
                       "Summarise the document")
            b.set_text(sm, "summary")
            return True
        if b.text_ref:
            sm = b.add("document.summarize", {"text": b.text_ref}, f"Summarise {what or 'the results'}")
            b.set_text(sm, "summary")
            return True
        if what:
            ref = _need_file(b, what)
            if ref:
                ex = b.add("document.extract_text", {"path": ref}, "Read the document")
                sm = b.add("document.summarize", {"text": f"${ex.id}.text", "path": ref},
                           "Summarise the document")
                b.set_text(sm, "summary")
                return True
        return False

    # ── read / show a file's text ──────────────────────────────────────────
    m = re.match(r"^read\s+(?P<what>.*)$", low)
    if m and b.file_ref and (not m.group("what") or _PRONOUN.match(m.group("what"))):
        st = b.add("document.extract_text", {"path": b.file_ref}, "Read the document")
        b.set_text(st, "text")
        return True

    # ── message / send ─────────────────────────────────────────────────────
    m = re.match(r"^(?:prepare|draft|write|compose|make)?\s*(?:a\s+|an\s+)?(?:whatsapp\s+|telegram\s+)?"
                 r"(?:message|msg|text|note|reply)\s+(?:for|to)\s+(?P<who>[\w .'-]+?)(?:\s+(?:on|via)\s+"
                 r"(?P<pl>\w+))?$", low)
    m2 = re.match(r"^send\s+(?:it|this|that|the summary|the draft|a message|the message)?\s*(?:to\s+)?"
                  r"(?P<who>[\w .'-]+?)(?:\s+(?:on|via)\s+(?P<pl>\w+))?$", low)
    mm = m or m2
    if mm and mm.group("who"):
        who = mm.group("who").strip().title()
        platform = (mm.group("pl") or "whatsapp").lower()
        content = b.text_ref or "$ask:What should the message say?"
        about = ""
        prep = b.add("message.prepare", {"recipient": who, "content": content, "platform": platform,
                                         **({"about": about} if about else {})},
                     f"Prepare a message for {who}")
        b.add("message.send", {"recipient": who, "text": f"${prep.id}.draft", "platform": platform},
              f"Send it to {who} (needs your confirmation)")
        return True

    # ── open ───────────────────────────────────────────────────────────────
    m = re.match(r"^(?:open|launch|start|run)\s+(?P<what>.+)$", low)
    m_tail = re.match(r"^(?P<what>.+?)\s+open$", low)          # "chrome open" (Tanglish order)
    mo = m or m_tail
    if mo:
        what = mo.group("what").strip()
        if url_m or re.match(r"^[\w-]+\.(?:com|org|net|in|io|dev|edu|gov)\b", what):
            b.add("browser.open", {"url": url_m.group(0) if url_m else what}, f"Open {what}")
            return True
        if _PRONOUN.match(what) or what in ("file", "this file", "that file"):
            if not b.file_ref:
                return False
            b.add("file.open", {"path": b.file_ref}, "Open the file")
            return True
        name = re.sub(r"^(?:the\s+)?", "", what)
        name = re.sub(r"\s+(?:app|application)$", "", name)
        b.add("app.open", {"app_name": name}, f"Open {name}")
        return True

    return False


class TaskPlanner:
    def __init__(self, registry: ToolRegistry, agent_of: Callable[[str], str] | None = None,
                 llm: Optional[Callable[[str, dict], Any]] = None, use_llm: bool = True):
        """llm(prompt, schema) -> parsed JSON. Defaults to the AI gateway."""
        self._reg = registry
        self._agent_of = agent_of or (lambda tool: (registry.get(tool).agent if registry.get(tool) else "general"))
        self._llm = llm
        self._use_llm = use_llm

    # -- public -------------------------------------------------------------
    def plan(self, request: str, context: str = "") -> Plan:
        """`context` = the Context Engine's minimal relevant context (Phase 5);
        only the LLM planner sees it."""
        lang = detect_language(request)
        rule = self.rule_plan(request)
        rule.language = lang
        if rule.ok and not rule.unhandled:
            return rule
        if self._use_llm:
            llm_plan = self.llm_plan(request, context=context)
            llm_plan.language = lang
            if llm_plan.ok:
                return llm_plan
            if not rule.steps:
                return llm_plan
        if rule.unhandled:
            rule.ok = False
            rule.reason = ("I couldn't work out how to do this part: \"" + rule.unhandled[0] +
                           "\". Could you rephrase it or ask for it separately?")
        return rule

    def rule_plan(self, request: str) -> Plan:
        lang = detect_language(request)
        text = request
        if lang != "en":
            # keep original tokens for names while normalising command words
            text = normalize(request)
        b = _Builder(self._reg, self._agent_of)
        whole = re.sub(r"^(?:jarvis[, ]+|please\s+|can you\s+|could you\s+)+", "", request.strip(), flags=re.I)
        if re.match(r"^(?:build|make|create|develop|design|code|generate)\s+(?:me\s+|us\s+)?", whole, re.I) and \
                re.search(r"\b(website|web ?site|web ?page|landing page|portfolio|web app|homepage|blog site|"
                          r"personal site)\b", whole, re.I):
            goal = re.sub(r"[,\s]+(?:and\s+)?(?:then\s+)?(?:open|show) (?:it|me)(?: to me)?[.!]?$", "", whole,
                          flags=re.I).strip()
            _add_team_build(b, goal)
            plan = Plan(steps=b.steps, source="rules")
            errs = validate_plan(plan, self._reg)
            if errs:
                plan.ok, plan.reason = False, "; ".join(errs[:3])
            return plan
        clauses_n = [c for c in _SPLIT.split(text) if c and c.strip()]
        clauses_o = [c for c in _SPLIT.split(request) if c and c.strip()] if lang == "en" else clauses_n
        if len(clauses_o) != len(clauses_n):
            clauses_o = clauses_n
        unhandled = []
        for raw, norm in zip(clauses_o, clauses_n):
            if not _rule_clause(b, raw, norm):
                if re.fullmatch(r"\s*(?:jarvis|please|thanks|thank you|now)\s*", norm, re.I):
                    continue
                unhandled.append(raw.strip())
        plan = Plan(steps=b.steps, source="rules", unhandled=unhandled)
        errs = validate_plan(plan, self._reg) if plan.steps else ["no steps"]
        if errs:
            plan.ok = False
            plan.reason = "; ".join(errs[:3])
        return plan

    # -- LLM ---------------------------------------------------------------
    PLAN_SCHEMA = {
        "type": "object",
        "properties": {
            "steps": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "tool": {"type": "string"},
                    "args_json": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "description", "tool", "args_json"],
            }},
            "cannot_do": {"type": "string"},
        },
        "required": ["steps"],
    }

    def llm_plan(self, request: str, context: str = "") -> Plan:
        catalog = self._reg.catalog(planner_only=True)
        prompt = (
            "You are the task planner of a desktop voice assistant. Turn the user's request into "
            "an ordered list of tool calls. Use ONLY tools from this catalog:\n"
            + json.dumps(catalog, ensure_ascii=False) +
            "\n\nRules:\n"
            "- Step ids are s1, s2, ... in order.\n"
            "- args_json is a JSON object string of the tool's args.\n"
            "- To use an earlier step's output, write \"$sN.field\" as the value (e.g. \"$s2.path\", "
            "\"$s5.summary\", \"$s1.matches\").\n"
            "- After file.find ALWAYS add file.select to pick the verified file.\n"
            "- Never invent a value the user did not give; write \"$ask:<question>\" instead.\n"
            "- Sending messages: message.prepare then message.send.\n"
            "- If the request cannot be done with these tools, return no steps and explain in cannot_do.\n"
            + (f"\nRelevant context (use it to fill in names/paths the user implies; never invent):\n"
               f"{context[:1500]}\n" if context else "")
            + f"\nUser request (may be English, Tamil or Tanglish): {request}"
        )
        try:
            data = self._llm(prompt, self.PLAN_SCHEMA) if self._llm else self._gateway_llm(prompt)
        except Exception as e:
            return Plan(ok=False, source="llm", reason=f"Planner model unavailable: {type(e).__name__}")
        steps = []
        for i, sd in enumerate((data or {}).get("steps") or []):
            try:
                args = sd.get("args")
                if args is None:
                    args = json.loads(sd.get("args_json") or "{}")
            except Exception:
                return Plan(ok=False, source="llm", reason=f"Step {i + 1} had invalid arguments.")
            tool = str(sd.get("tool", ""))
            steps.append(Step(id=str(sd.get("id") or f"s{i + 1}"), description=str(sd.get("description", tool)),
                              tool=tool, args=args if isinstance(args, dict) else {},
                              agent=self._agent_of(tool) if self._reg.get(tool) else "general",
                              depends_on=[str(d) for d in (sd.get("depends_on") or [])]))
        plan = Plan(steps=steps, source="llm")
        if not steps:
            plan.ok = False
            plan.reason = str((data or {}).get("cannot_do") or "I don't have a way to do that yet.")
            return plan
        errs = validate_plan(plan, self._reg)
        if errs:
            plan.ok = False
            plan.reason = "The generated plan was invalid, so I did not run it: " + "; ".join(errs[:3])
        return plan

    # -- self-recovery (Phase 5) -------------------------------------------------
    def recover(self, request: str, done: list, failed: Step, error: str, context: str = "") -> Plan:
        """A step failed after retries and alternatives. Ask for a DIFFERENT way
        to finish the remaining work. The new plan:
          * uses ids r1, r2, … and may reference completed steps' outputs ($sN.field)
          * is validated exactly like any plan (unknown tools / bad args → rejected)
          * may not simply repeat the failed call
        Returns Plan(ok=False) when no alternative exists or the LLM is off."""
        if not self._use_llm:
            return Plan(ok=False, source="recovery", reason="recovery planner disabled")
        catalog = self._reg.catalog(planner_only=True)
        done_desc = [{"id": s.id, "tool": s.tool, "did": s.description,
                      "outputs": sorted((s.output or {}).keys())[:8]} for s in done]
        prompt = (
            "You are the self-recovery planner of a desktop assistant. A step of a task failed even "
            "after retries. Analyse WHY it failed and produce a NEW plan that finishes the user's "
            "request a different way (another tool, different arguments, an extra lookup step first).\n"
            f"Tool catalog: {json.dumps(catalog, ensure_ascii=False)}\n"
            f"Original request: {request}\n"
            f"Completed steps (do NOT repeat them; use their outputs as \"$sN.field\"): "
            f"{json.dumps(done_desc, ensure_ascii=False)}\n"
            f"Failed step: {json.dumps({'id': failed.id, 'tool': failed.tool, 'args': failed.args, 'did': failed.description}, ensure_ascii=False, default=str)}\n"
            f"Failure: {str(error)[:400]}\n"
            + (f"Context: {context[:800]}\n" if context else "")
            + "Rules: step ids r1, r2, …; args_json is a JSON object string; never invent values the user "
              "did not give (use \"$ask:<question>\"); do not retry the identical failed call; if there is "
              "no sensible alternative return no steps and explain in cannot_do."
        )
        try:
            data = self._llm(prompt, self.PLAN_SCHEMA) if self._llm else self._gateway_llm(prompt, tier="deep")
        except Exception as e:
            return Plan(ok=False, source="recovery", reason=f"Recovery planner unavailable: {type(e).__name__}")
        steps = []
        for i, sd in enumerate((data or {}).get("steps") or []):
            try:
                args = sd.get("args")
                if args is None:
                    args = json.loads(sd.get("args_json") or "{}")
            except Exception:
                return Plan(ok=False, source="recovery", reason=f"Recovery step {i + 1} had invalid arguments.")
            tool = str(sd.get("tool", ""))
            sid = str(sd.get("id") or f"r{i + 1}")
            if not sid.startswith("r"):
                sid = f"r{i + 1}"
            steps.append(Step(id=sid, description=str(sd.get("description", tool)), tool=tool,
                              args=args if isinstance(args, dict) else {},
                              agent=self._agent_of(tool) if self._reg.get(tool) else "general",
                              depends_on=[str(d) for d in (sd.get("depends_on") or [])]))
        plan = Plan(steps=steps, source="recovery")
        if not steps:
            plan.ok = False
            plan.reason = str((data or {}).get("cannot_do") or "No alternative approach.")
            return plan
        if steps[0].tool == failed.tool and steps[0].args == failed.args:
            plan.ok = False
            plan.reason = "The recovery plan only repeated the failed step."
            return plan
        errs = validate_plan(plan, self._reg, prior_ids=[s.id for s in done])
        if errs:
            plan.ok = False
            plan.reason = "The recovery plan was invalid: " + "; ".join(errs[:3])
        return plan

    @staticmethod
    def _gateway_llm(prompt: str, tier: str = "default"):
        from core.ai_gateway import get_gateway
        return get_gateway().structured(prompt, TaskPlanner.PLAN_SCHEMA, temperature=0.1, timeout=45.0,
                                         tier=tier)
