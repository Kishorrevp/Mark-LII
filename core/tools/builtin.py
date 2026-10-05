"""
core/tools/builtin.py — registers every existing capability in the ToolRegistry.

Two layers, both thin wrappers — nothing here re-implements an action:

1. LEGACY TOOLS (the 26 tools Gemini Live already calls directly). Same names,
   same parameters, same action functions. Registering them gives the central
   permission gate, audit log and verifier one description of each. Their
   per-action risk comes from core/risk.py's table, mapped onto the 5-level
   scale, with the Phase 4 changes the spec asked for:
       file_controller.delete           MEDIUM → HIGH  (confirm first)
       send_message                     MEDIUM → HIGH  (confirm first)
       game_updater(shutdown_when_done) LOW    → HIGH  (it powers the PC off)
   Tools that are bound to the live voice session (screen_process,
   close_camera, set_mode, shutdown_jarvis, undo, memory tools) are registered
   for permission/audit purposes but hidden from the planner.

2. STRUCTURED AGENT TOOLS (file.find, file.move, document.summarize,
   message.prepare, browser.download, ...). These return structured data
   (paths, text, drafts) so one agent's output can feed the next, and each
   carries a real verifier. They call the same functions the legacy tools do
   (file_controller.move_file, send_message, open_app, ...).

Action modules are imported lazily inside handlers, so building the registry
never imports pyautogui / Qt / google-genai.
"""
from __future__ import annotations

import importlib
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from core.permissions import RiskLevel
from core.tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec
from core import verification as V

S, L, M, H, C = (RiskLevel.SAFE, RiskLevel.LOW_RISK, RiskLevel.MEDIUM_RISK,
                 RiskLevel.HIGH_RISK, RiskLevel.CRITICAL)


def _mod(name: str):
    return importlib.import_module(name)


def _call_action(module: str, func: str, **kw) -> Callable[[dict, ToolContext], Any]:
    """Adapter: handler(args, ctx) → actions.<module>.<func>(parameters=args, ...)."""
    def handler(args: dict, ctx: ToolContext):
        fn = getattr(_mod(module), func)
        call_kw = {"parameters": args}
        import inspect
        params = inspect.signature(fn).parameters
        if "player" in params:
            call_kw["player"] = ctx.player
        if "response" in params:
            call_kw["response"] = None
        if "speak" in params and "speak" in kw:
            call_kw["speak"] = ctx.speak
        if "session_memory" in params:
            call_kw["session_memory"] = None
        return fn(**call_kw)
    return handler


def _action_risk(default: RiskLevel, table: dict, extra: Callable[[dict], RiskLevel | None] | None = None):
    def fn(args: dict) -> RiskLevel:
        if extra:
            r = extra(args)
            if r is not None:
                return r
        a = str(args.get("action", "") or "").strip().lower()
        return table.get(a, default)
    return fn


def _obj(props: dict, required: list | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}


def _live_only(name: str):
    def handler(args, ctx):
        return ToolResult.failure(f"'{name}' only works directly in the voice session, "
                                  f"not inside a multi-step task.")
    handler._live_only = True       # validate_plan refuses these inside tasks
    return handler


# ═════════════════════════════════════════════════════════════════════════════
# 1. Legacy tools
# ═════════════════════════════════════════════════════════════════════════════

def _legacy_file_verify(args: dict, result: ToolResult) -> V.VerificationResult:
    """Post-condition check for file_controller's state-changing actions,
    reconstructed from the same path resolution file_controller uses."""
    if not result.ok:
        return V.failed(result.error or "The action reported a failure.")
    fc = _mod("actions.file_controller")
    action = str(args.get("action", "")).lower()
    try:
        base = fc._resolve_path(args.get("path", "desktop"))
        name = args.get("name", "")
        target = (base / name) if name else base
        if action == "move":
            dst = fc._resolve_path(args.get("destination", ""))
            final = (dst / target.name) if dst.is_dir() else dst
            r1 = V.path_exists(final)
            if not r1.ok or final.resolve() == target.resolve():
                return r1
            return V.path_absent(target)
        if action == "copy":
            dst = fc._resolve_path(args.get("destination", ""))
            return V.path_exists((dst / target.name) if dst.is_dir() else dst)
        if action == "rename":
            r1 = V.path_exists(target.parent / str(args.get("new_name", "")))
            return r1 if not r1.ok else V.path_absent(target)
        if action == "delete":
            return V.path_absent(target)
        if action in ("create_file", "write"):
            return V.path_exists(target, "file")
        if action == "create_folder":
            return V.path_exists(target, "dir")
    except Exception as e:
        return V.unverifiable(f"Could not verify: {type(e).__name__}")
    return V.VerificationResult(V.VStatus.SKIPPED, "Read-only action.")


def _send_message_verify(args: dict, result: ToolResult) -> V.VerificationResult:
    if not result.ok:
        return V.failed(result.error or "Sending reported a failure.")
    return V.unverifiable("The message was typed and sent through the app's window; the app gives "
                          "no delivery receipt, so delivery is not confirmed.")


def _open_app_verify(args: dict, result: ToolResult) -> V.VerificationResult:
    if not result.ok:
        return V.failed(result.error or "Launch reported a failure.")
    return V.process_running(str(args.get("app_name", "")), wait_s=2.0)


def register_legacy_tools(reg: ToolRegistry) -> None:
    fc_table = {"list": S, "read": S, "find": S, "largest": S, "disk_usage": S, "info": S,
                "create_file": M, "create_folder": M, "write": M, "move": M, "copy": M,
                "rename": M, "organize_desktop": M, "delete": H}
    browser_table = {"click": M, "type": M, "fill_form": M, "smart_click": M, "smart_type": M,
                     "press": M, "get_text": S, "get_url": S, "screenshot": S, "list_browsers": S}
    code_table = {"explain": S, "screen_debug": L, "auto": M, "write": M, "edit": M, "optimize": M,
                  "run": M, "build": M}
    desktop_table = {"stats": S, "list": S, "current_wallpaper": S, "wallpaper": L, "wallpaper_url": L,
                     "organize": M, "clean": M, "task": H}
    settings_table = {"restart": H, "shutdown": H, "toggle_wifi": H, "screenshot": S}
    control_table = {"screenshot": S, "wait": S, "move": L, "scroll": L, "focus_window": L}
    game_table = {"list": S, "download_status": S, "schedule_status": S, "cancel_schedule": L,
                  "update": M, "install": M, "schedule": M}
    proc_table = {"describe": L, "ocr": L, "summarize": L, "extract_text": L, "info": S, "analyze": L,
                  "explain": L, "stats": S, "validate": S}

    def game_extra(args):
        if str(args.get("shutdown_when_done", "")).lower() in ("true", "1", "yes"):
            return H
        return None

    specs = [
        ToolSpec("open_app", "Open/launch an application on this computer.",
                 _call_action("actions.open_app", "open_app"),
                 _obj({"app_name": STR}, ["app_name"]), agent="laptop", risk=L,
                 verify=_open_app_verify, retry_safe=True),
        ToolSpec("web_search", "Search the web / news / research / prices.",
                 _call_action("actions.web_search", "web_search"),
                 _obj({"query": STR, "mode": STR, "items": {"type": "array"}, "aspect": STR}),
                 agent="research", risk=S, retry_safe=True),
        ToolSpec("system_status", "CPU, RAM, disk, battery status.",
                 lambda a, c: str(_mod("actions.system_monitor").get_system_status()),
                 _obj({}), agent="system", risk=S, retry_safe=True),
        ToolSpec("weather_report", "Weather for a city.",
                 _call_action("actions.weather_report", "weather_action"),
                 _obj({"city": STR}), agent="research", risk=S, retry_safe=True),
        ToolSpec("send_message", "Send a chat message (WhatsApp, Telegram, ...) via the desktop app.",
                 _call_action("actions.send_message", "send_message"),
                 _obj({"receiver": STR, "message_text": STR, "platform": STR},
                      ["receiver", "message_text"]),
                 agent="communication", risk=H, verify=_send_message_verify,
                 confirm_text=lambda a: f"Send {a.get('platform') or 'WhatsApp'} message to "
                                        f"{a.get('receiver', '?')}"),
        ToolSpec("reminder", "Set a reminder.", _call_action("actions.reminder", "reminder"),
                 _obj({"date": STR, "time": STR, "message": STR}), agent="reminder", risk=L),
        ToolSpec("youtube_video", "Play / summarise / inspect YouTube videos.",
                 _call_action("actions.youtube_video", "youtube_video"),
                 _obj({"action": STR, "query": STR, "save": BOOL, "region": STR, "url": STR}),
                 agent="browser", risk=L),
        ToolSpec("screen_process", "Look at the screen or camera (voice session only).",
                 _live_only("screen_process"), _obj({"angle": STR, "text": STR}),
                 agent="vision", risk=S, planner_visible=False),
        ToolSpec("close_camera", "Close the camera stream.", _live_only("close_camera"), _obj({}),
                 agent="vision", risk=S, planner_visible=False),
        ToolSpec("computer_settings", "Volume, brightness, windows, keyboard shortcuts, power.",
                 _call_action("actions.computer_settings", "computer_settings"),
                 _obj({"action": STR, "description": STR, "value": {}}),
                 agent="system", risk=_action_risk(L, settings_table),
                 self_gated={"restart", "shutdown", "toggle_wifi"}),
        ToolSpec("browser_control", "Drive the web browser (go_to, search, click, type, ...).",
                 _call_action("actions.browser_control", "browser_control"),
                 _obj({"action": STR, "browser": STR, "url": STR, "query": STR, "engine": STR,
                       "selector": STR, "text": STR, "description": STR, "direction": STR,
                       "amount": {}, "key": STR, "path": STR, "incognito": {}, "clear_first": {}}),
                 agent="browser", risk=_action_risk(L, browser_table)),
        ToolSpec("file_controller", "List/find/read/create/move/copy/rename/delete files.",
                 _call_action("actions.file_controller", "file_controller"),
                 _obj({"action": STR, "path": STR, "destination": STR, "new_name": STR,
                       "content": STR, "name": STR, "extension": STR, "count": {}}, ["action"]),
                 agent="file", risk=_action_risk(M, fc_table), undoable=True,
                 verify=_legacy_file_verify,
                 confirm_text=lambda a: f"{str(a.get('action', '')).title()} "
                                        f"{a.get('name') or a.get('path', '')}"),
        ToolSpec("desktop_control", "Wallpaper, desktop organise/clean, scripted desktop tasks.",
                 _call_action("actions.desktop", "desktop_control"),
                 _obj({"action": STR, "path": STR, "url": STR, "mode": STR, "task": STR}),
                 agent="laptop", risk=_action_risk(L, desktop_table), self_gated={"task"}),
        ToolSpec("code_helper", "Write / edit / explain / run code.",
                 _call_action("actions.code_helper", "code_helper", speak=True),
                 _obj({"action": STR, "description": STR, "language": STR, "output_path": STR,
                       "file_path": STR, "code": STR, "args": STR, "timeout": {}}),
                 agent="coding", risk=_action_risk(M, code_table)),
        ToolSpec("dev_agent", "Build a small multi-file project.",
                 _call_action("actions.dev_agent", "dev_agent", speak=True),
                 _obj({"description": STR, "language": STR, "project_name": STR, "timeout": {}}),
                 agent="coding", risk=M),
        ToolSpec("computer_control", "Direct mouse/keyboard control.",
                 _call_action("actions.computer_control", "computer_control"),
                 _obj({"action": STR}), agent="laptop", risk=_action_risk(M, control_table)),
        ToolSpec("game_updater", "Steam/Epic game updates and scheduling.",
                 _call_action("actions.game_updater", "game_updater", speak=True),
                 _obj({"action": STR, "platform": STR, "game_name": STR, "app_id": STR,
                       "hour": {}, "minute": {}, "shutdown_when_done": {}}),
                 agent="laptop", risk=_action_risk(L, game_table, game_extra)),
        ToolSpec("flight_finder", "Search flights.", _call_action("actions.flight_finder", "flight_finder"),
                 _obj({"origin": STR, "destination": STR, "date": STR}), agent="research", risk=S),
        ToolSpec("manage_monitor", "Add/remove/list monitored news topics.",
                 _live_only("manage_monitor"),
                 _obj({"action": STR, "topic": STR}), agent="research", risk=L, planner_visible=False),
        ToolSpec("shutdown_jarvis", "Quit JARVIS (not the computer).", _live_only("shutdown_jarvis"),
                 _obj({}), agent="system", risk=L, planner_visible=False),
        ToolSpec("file_processor", "Analyse / convert / summarise a file of any type.",
                 _call_action("actions.file_processor", "file_processor", speak=True),
                 _obj({"file_path": STR, "action": STR, "instruction": STR}),
                 agent="file", risk=_action_risk(M, proc_table)),
        ToolSpec("save_memory", "Remember a fact.", _live_only("save_memory"),
                 _obj({"category": STR, "key": STR, "value": STR}), agent="general", risk=L,
                 planner_visible=False),
        ToolSpec("recall_memory", "Search memory.", _live_only("recall_memory"), _obj({"query": STR}),
                 agent="general", risk=S, planner_visible=False),
        ToolSpec("forget_memory", "Forget a fact.", _live_only("forget_memory"),
                 _obj({"category": STR, "key": STR}), agent="general", risk=M, planner_visible=False),
        ToolSpec("set_mode", "Friendly / serious mode.", _live_only("set_mode"),
                 _obj({"mode": {"type": "string", "enum": ["friendly", "serious"]}}),
                 agent="general", risk=L, planner_visible=False),
        ToolSpec("undo", "Undo the last change.", _live_only("undo"), _obj({"action": STR}),
                 agent="general", risk=L, planner_visible=False),
        # Part One — knowledge, sources, memory capture, model switching
        ToolSpec("remember", "Remember what the user explicitly asked to remember (verified write).",
                 _live_only("remember"), _obj({"text": STR, "category": STR, "key": STR, "value": STR}),
                 agent="general", risk=L, planner_visible=False),
        ToolSpec("search_knowledge", "Search the user's notes, documents and memory (with sources).",
                 _live_only("search_knowledge"), _obj({"query": STR, "scope": STR}),
                 agent="general", risk=S, planner_visible=False),
        ToolSpec("show_sources", "Show which stored sources the last answer used.",
                 _live_only("show_sources"), _obj({}), agent="general", risk=S, planner_visible=False),
        ToolSpec("switch_model", "Switch the reasoning or voice model to an exact model (runtime only).",
                 _live_only("switch_model"), _obj({"model": STR, "scope": STR, "action": STR}),
                 agent="general", risk=L, planner_visible=False),
    ]
    for s in specs:
        s.source = "action"
        s.live_exposed = True
        # legacy tools are exposed to the planner only through the structured
        # tools below, except the ones without a structured equivalent
        if s.name in ("file_controller", "send_message", "open_app", "web_search"):
            s.planner_visible = False
        reg.register(s)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Structured agent tools
# ═════════════════════════════════════════════════════════════════════════════

def _fc():
    return _mod("actions.file_controller")


def _resolve_file(path: str) -> Path:
    fc = _fc()
    p = fc._resolve_path(str(path))
    if not fc._is_safe_path(p):
        raise PermissionError(f"Access denied: {p}")
    return p


def resolve_folder(name: str) -> Path:
    """Folder names as people say them: "Downloads", "my Projects folder",
    "desktop/reports", or an absolute path. Searches home, Desktop, Documents
    (two levels) for a folder with that name. Never invents one."""
    fc = _fc()
    raw = re.sub(r"(?i)^(my|the)\s+|\s+(folder|directory)$", "", str(name or "").strip()).strip()
    if not raw:
        raise ValueError("No destination folder given.")
    direct = fc._resolve_path(raw)
    if direct.is_absolute() and direct.exists() and direct.is_dir():
        if not fc._is_safe_path(direct):
            raise PermissionError(f"Access denied: {direct}")
        return direct
    target = raw.lower()
    roots = [Path.home(), fc._get_desktop(), fc._get_documents(), fc._get_downloads()]
    seen = set()
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for depth_glob in ("*", "*/*"):
            try:
                for d in root.glob(depth_glob):
                    try:
                        if d.is_dir() and d.name.lower() == target:
                            r = d.resolve()
                            if r not in seen and fc._is_safe_path(r):
                                seen.add(r)
                                candidates.append(r)
                    except OSError:
                        continue
            except OSError:
                continue
        if candidates:
            break
    if not candidates:
        raise FileNotFoundError(f"I couldn't find a folder named '{raw}'.")
    return candidates[0]


def _file_info(p: Path) -> dict:
    try:
        st = p.stat()
        return {"path": str(p), "name": p.name, "size": st.st_size, "modified": st.st_mtime}
    except OSError:
        return {"path": str(p), "name": p.name}


def t_file_find(args, ctx):
    fc = _fc()
    name = str(args.get("name", "") or "")
    ext = str(args.get("extension", "") or "")
    roots = [args["path"]] if args.get("path") else ["desktop", "documents", "downloads", "home"]
    seen, matches = set(), []
    for r in roots:
        try:
            for p in fc.find_paths(name=name, extension=ext, path=r,
                                   max_results=int(args.get("max_results", 20)), token_match=True):
                rp = str(p.resolve())
                if rp not in seen:
                    seen.add(rp)
                    matches.append(_file_info(p))
        except (PermissionError, FileNotFoundError):
            continue
    words = [w for w in fc._norm_name(name).split() if len(w) > 2]
    if not matches and len(words) > 1:
        # Nothing contains every word ("old resume" vs "resume.docx"): retry
        # word by word. file.select will ask if this yields several candidates.
        for w in sorted(words, key=len, reverse=True):
            for r in roots:
                try:
                    for p in fc.find_paths(name=w, extension=ext, path=r, max_results=10,
                                           token_match=True):
                        rp = str(p.resolve())
                        if rp not in seen:
                            seen.add(rp)
                            matches.append(_file_info(p))
                except (PermissionError, FileNotFoundError):
                    continue
            if matches:
                break
    matches.sort(key=lambda m: -(m.get("modified") or 0))
    msg = f"Found {len(matches)} matching file(s)." if matches else \
        f"No file matching '{name or ext}' was found."
    return ToolResult(True, {"matches": matches, "count": len(matches), "query": name}, msg)


def t_file_select(args, ctx):
    """'Verify correct file': exactly one candidate → that file. Several →
    ask the user (never guess between two plausible files)."""
    matches = args.get("matches") or []
    choice = args.get("choice")
    if isinstance(matches, dict):
        matches = matches.get("matches") or []
    if choice not in (None, ""):
        try:
            idx = int(str(choice).strip()) - 1
            if 0 <= idx < len(matches):
                m = matches[idx]
                return ToolResult(True, {"path": m["path"], "name": m.get("name")},
                                  f"Using {m.get('name')}.")
        except ValueError:
            for m in matches:
                if str(choice).lower() in str(m.get("name", "")).lower():
                    return ToolResult(True, {"path": m["path"], "name": m.get("name")},
                                      f"Using {m.get('name')}.")
    if not matches:
        return ToolResult.failure("No matching file was found, so there is nothing to work on.")
    if len(matches) == 1:
        m = matches[0]
        return ToolResult(True, {"path": m["path"], "name": m.get("name")}, f"Found {m.get('name')}.")
    options = [f"{i + 1}. {m.get('name')} ({Path(m['path']).parent.name})" for i, m in enumerate(matches[:6])]
    return ToolResult(False, {"needs_input": True, "options": options, "matches": matches[:6]},
                      "", "I found several matching files: " + "; ".join(options)
                      + ". Which one should I use?")


def t_file_rename(args, ctx):
    src = _resolve_file(args["path"])
    new_name = str(args["new_name"]).strip()
    if src.suffix and not Path(new_name).suffix:
        new_name += src.suffix                       # "rename it to Final" keeps .pdf
    out = _fc().rename_file(str(src), new_name=new_name)
    res = _legacy_to_result(out)
    res.data.update({"path": str(src.parent / new_name), "old_path": str(src), "name": new_name})
    return res


def _verify_rename(args, res: ToolResult):
    if not res.ok:
        return V.failed(res.error)
    r = V.path_exists(res.data.get("path"))
    return r if not r.ok else V.path_absent(res.data.get("old_path"))


def t_file_move(args, ctx):
    src = _resolve_file(args["path"])
    dest = resolve_folder(args["destination"])
    out = _fc().move_file(str(src), destination=str(dest))
    res = _legacy_to_result(out)
    same = (dest / src.name).resolve() == src.resolve()
    res.data.update({"path": str(dest / src.name), "old_path": None if same else str(src),
                     "folder": str(dest)})
    return res


def _verify_move(args, res: ToolResult):
    if not res.ok:
        return V.failed(res.error)
    r = V.path_exists(res.data.get("path"))
    if not r.ok or not res.data.get("old_path"):      # already in place → nothing left behind
        return r
    return V.path_absent(res.data.get("old_path"))


def t_file_copy(args, ctx):
    src = _resolve_file(args["path"])
    dest = resolve_folder(args["destination"])
    out = _fc().copy_file(str(src), destination=str(dest))
    res = _legacy_to_result(out)
    # copy_file names the copy "X - Copy.ext" when X already exists there
    m = re.search(r"→ [^/\\]+/(.+)$", str(out or ""))
    name = m.group(1).strip() if m else src.name
    res.data.update({"path": str(dest / name), "source": str(src)})
    return res


def t_file_delete(args, ctx):
    src = _resolve_file(args["path"])
    out = _fc().delete_file(str(src))
    res = _legacy_to_result(out)
    res.data.update({"deleted": str(src)})
    return res


def t_file_read(args, ctx):
    p = _resolve_file(args["path"])
    text = _fc().read_file(str(p), max_chars=int(args.get("max_chars", 4000)))
    return _legacy_to_result(text, text_key="text")


def t_folder_create(args, ctx):
    fc = _fc()
    out = fc.create_folder(str(args.get("path", "desktop")), name=str(args.get("name", "")))
    res = _legacy_to_result(out)
    base = fc._resolve_path(str(args.get("path", "desktop")))
    res.data["path"] = str(base / args["name"]) if args.get("name") else str(base)
    return res


def t_doc_extract(args, ctx):
    p = _resolve_file(args["path"])
    text = _mod("actions.file_processor").extract_text(p, int(args.get("max_chars", 50000)))
    if not text.strip():
        return ToolResult(True, {"text": "", "path": str(p), "chars": 0, "extractable": False},
                          f"No extractable text in {p.name} (it may be scanned).")
    return ToolResult(True, {"text": text, "path": str(p), "chars": len(text), "extractable": True},
                      f"Read {len(text):,} characters from {p.name}.")


def t_doc_summarize(args, ctx):
    from core.ai_gateway import get_gateway
    text = str(args.get("text") or "")
    source = str(args.get("path") or args.get("source") or "")
    gw = get_gateway()
    lang = str(args.get("language") or "the same language as the document")
    instr = (f"Summarise this document in 5-7 concise bullet points in {lang}. "
             "Start with a one-sentence overview. No preamble.")
    if not text.strip() and source:
        p = _resolve_file(source)
        if p.suffix.lower() == ".pdf":
            # Scanned PDF → native document input where the provider supports it
            summary = gw.generate(instr, documents=[(p.read_bytes(), "application/pdf")],
                                  tier="auto").text
        else:
            text = _mod("actions.file_processor").extract_text(p)
            summary = gw.text(instr + "\n\n" + text[:60000], tier="auto") if text.strip() else ""
    else:
        summary = gw.text(instr + "\n\n" + text[:60000], tier="auto") if text.strip() else ""
    summary = (summary or "").strip()
    if not summary:
        return ToolResult.failure("There was no text to summarise.")
    return ToolResult(True, {"summary": summary, "source": source}, summary)


def t_text_answer(args, ctx):
    from core.ai_gateway import get_gateway
    out = get_gateway().text(str(args["prompt"]), system=str(args.get("system", "")),
                             tier=str(args.get("tier") or "auto"))
    return ToolResult(bool(out.strip()), {"text": out}, out, "" if out.strip() else "Empty answer.")


def t_msg_prepare(args, ctx):
    recipient = str(args["recipient"]).strip()
    content = str(args.get("content") or "").strip()
    subject = str(args.get("about") or "").strip()
    if not content:
        return ToolResult.failure("There is nothing to put in the message.")
    lead = f"Hi {recipient}, " + (f"here's a quick summary of {subject}:" if subject else "")
    draft = (lead + "\n" + content).strip()
    if len(draft) > 1500:
        draft = draft[:1490].rstrip() + "…"
    return ToolResult(True, {"draft": draft, "recipient": recipient,
                             "platform": str(args.get("platform") or "whatsapp")},
                      f"Draft for {recipient} is ready ({len(draft)} characters).")


def t_msg_send(args, ctx):
    sm = _mod("actions.send_message")
    out = sm.send_message(parameters={"receiver": args["recipient"], "message_text": args["text"],
                                      "platform": args.get("platform") or "whatsapp"},
                          response=None, player=ctx.player, session_memory=None)
    res = _legacy_to_result(out)
    if res.ok and "sent" not in (out or "").lower():
        res = ToolResult.failure(out or "Send did not report success.")
    res.data.update({"recipient": args["recipient"], "platform": args.get("platform") or "whatsapp"})
    return res


def t_app_open(args, ctx):
    out = _mod("actions.open_app").open_app(parameters={"app_name": args["app_name"]},
                                            response=None, player=ctx.player)
    res = _legacy_to_result(out)
    res.data["app_name"] = args["app_name"]
    return res


def t_web_search(args, ctx):
    out = _mod("actions.web_search").web_search(parameters={"query": args["query"],
                                                            "mode": args.get("mode", "search")},
                                                player=ctx.player)
    res = _legacy_to_result(out, text_key="text")
    if res.ok and (not out or out.startswith("No results") or out.startswith("Search failed")):
        res = ToolResult.failure(out or "No results.", retryable=True)
    return res


def t_browser_open(args, ctx):
    url = str(args["url"]).strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if urlparse(url).scheme not in ("http", "https"):
        return ToolResult.failure("Only http/https links can be opened.")
    out = _mod("actions.browser_control").browser_control(parameters={"action": "go_to", "url": url},
                                                         player=ctx.player)
    res = _legacy_to_result(out)
    res.data["url"] = url
    return res


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._\- ()\[\]]+")
MAX_DOWNLOAD = 200 * 1024 * 1024


def t_browser_download(args, ctx):
    """Download a URL into a folder (Downloads by default). https/http only,
    size-capped, filename sanitised, never overwrites (adds ' (1)')."""
    import requests
    url = str(args["url"]).strip()
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        return ToolResult.failure("Only http/https URLs can be downloaded.")
    dest = resolve_folder(args.get("destination") or "downloads")
    name = str(args.get("filename") or "").strip() or unquote(Path(u.path).name) or "download"
    name = _SAFE_FILENAME.sub("_", Path(name).name).strip(" .") or "download"
    target = dest / name
    i = 1
    while target.exists():
        target = dest / f"{Path(name).stem} ({i}){Path(name).suffix}"
        i += 1
    tmp = target.with_name(target.name + ".part")
    try:
        with requests.get(url, stream=True, timeout=30, allow_redirects=True) as r:
            if r.status_code >= 400:
                return ToolResult.failure(f"Download failed: HTTP {r.status_code}.",
                                          retryable=r.status_code >= 500)
            total = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(65536):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD:
                        raise ValueError("file is larger than the 200 MB download limit")
                    f.write(chunk)
        os.replace(tmp, target)
    except Exception as e:
        try:
            tmp.unlink()
        except Exception:
            pass
        return ToolResult.failure(f"Download failed: {type(e).__name__}: {str(e)[:120]}",
                                  retryable=isinstance(e, (requests.ConnectionError, requests.Timeout)))
    try:
        from core.undo import push_undo
        push_undo(f"downloaded {target.name}",
                  lambda p=target: (p.unlink() if p.exists() else None, f"Removed {p.name}.")[1],
                  kind="download", paths=[str(target)])
    except Exception:
        pass
    return ToolResult(True, {"path": str(target), "name": target.name, "size": target.stat().st_size},
                      f"Downloaded {target.name} to {dest.name}.")


def t_system_status(args, ctx):
    s = _mod("actions.system_monitor").get_system_status()
    return ToolResult(True, {"text": str(s)}, str(s))


def t_reminder(args, ctx):
    out = _mod("actions.reminder").reminder(parameters=args, response=None, player=ctx.player)
    return _legacy_to_result(out)


def t_screen_describe(args, ctx):
    from core.ai_gateway import get_gateway
    sp = _mod("actions.screen_processor")
    img, mime = sp._capture_screen()
    text = get_gateway().vision(str(args.get("question") or "Describe what is on this screen."),
                                img, mime, tier="vision")
    return ToolResult(bool(text.strip()), {"description": text}, text,
                      "" if text.strip() else "No description returned.")


def _legacy_to_result(out, text_key: str = "text") -> ToolResult:
    from core.tools.registry import normalise_result
    r = normalise_result(out)
    if text_key != "text" and "text" in r.data:
        r.data[text_key] = r.data.pop("text")
    return r


def _nonempty(key: str, what: str):
    def fn(args, res: ToolResult):
        if not res.ok:
            return V.failed(res.error)
        return V.text_nonempty(res.data.get(key), what)
    return fn


def register_structured_tools(reg: ToolRegistry) -> None:
    P = STR
    reg.register(ToolSpec("file.find", "Find files by (partial) name and/or extension. Searches Desktop, "
                           "Documents, Downloads, then home unless `path` is given.",
                           t_file_find, _obj({"name": P, "extension": P, "path": P, "max_results": INT}),
                           {"matches": "list[{path,name,size,modified}]", "count": "int"},
                           agent="file", risk=S, retry_safe=True))
    reg.register(ToolSpec("file.select", "Pick the one correct file from file.find matches (asks the user "
                          "if several match).", t_file_select,
                          _obj({"matches": {"type": "array"}, "choice": {}}, ["matches"]),
                          {"path": "str"}, agent="file", risk=S,
                          verify=lambda a, r: V.path_exists(r.data.get("path"), "file") if r.ok
                          else V.failed(r.error), retry_safe=True))
    reg.register(ToolSpec("file.rename", "Rename a file (new_name is a plain file name).", t_file_rename,
                          _obj({"path": P, "new_name": P}, ["path", "new_name"]), {"path": "str"},
                          agent="file", risk=M, undoable=True, verify=_verify_rename,
                          verification_required=True))
    reg.register(ToolSpec("file.move", "Move a file into a folder (folder name or path).", t_file_move,
                          _obj({"path": P, "destination": P}, ["path", "destination"]), {"path": "str"},
                          agent="file", risk=M, undoable=True, verify=_verify_move,
                          verification_required=True, alternatives=[]))
    reg.register(ToolSpec("file.copy", "Copy a file into a folder.", t_file_copy,
                          _obj({"path": P, "destination": P}, ["path", "destination"]), {"path": "str"},
                          agent="file", risk=M, undoable=True,
                          verify=lambda a, r: V.path_exists(r.data.get("path")) if r.ok else V.failed(r.error),
                          verification_required=True))
    reg.register(ToolSpec("file.delete", "Delete a file (to the Recycle Bin).", t_file_delete,
                          _obj({"path": P}, ["path"]), {"deleted": "str"}, agent="file", risk=H,
                          undoable=True, verification_required=True,
                          verify=lambda a, r: V.path_absent(r.data.get("deleted")) if r.ok else V.failed(r.error),
                          confirm_text=lambda a: f"Delete {Path(str(a.get('path', ''))).name}"))
    reg.register(ToolSpec("file.read", "Read a text file.", t_file_read, _obj({"path": P, "max_chars": INT},
                          ["path"]), {"text": "str"}, agent="file", risk=S, retry_safe=True))
    reg.register(ToolSpec("file.create_folder", "Create a folder.", t_folder_create,
                          _obj({"path": P, "name": P}), {"path": "str"}, agent="file", risk=M, undoable=True,
                          verify=lambda a, r: V.path_exists(r.data.get("path"), "dir") if r.ok else V.failed(r.error)))
    reg.register(ToolSpec("document.extract_text", "Extract the text of a PDF/DOCX/PPTX/text file.",
                          t_doc_extract, _obj({"path": P, "max_chars": INT}, ["path"]),
                          {"text": "str", "chars": "int"}, agent="file", risk=S, retry_safe=True))
    reg.register(ToolSpec("document.summarize", "Summarise text (or a document at `path`).",
                          t_doc_summarize, _obj({"text": P, "path": P, "language": P}),
                          {"summary": "str"}, agent="file", risk=S, retry_safe=True,
                          verify=_nonempty("summary", "summary")))
    reg.register(ToolSpec("ai.answer", "Answer / write / transform text with the AI model.", t_text_answer,
                          _obj({"prompt": P, "system": P, "tier": P}, ["prompt"]), {"text": "str"},
                          agent="general", risk=S, retry_safe=True, verify=_nonempty("text", "answer")))
    reg.register(ToolSpec("message.prepare", "Prepare (draft, do NOT send) a message to someone.",
                          t_msg_prepare, _obj({"recipient": P, "content": P, "about": P, "platform": P},
                                              ["recipient", "content"]),
                          {"draft": "str"}, agent="communication", risk=S,
                          verify=_nonempty("draft", "draft")))
    reg.register(ToolSpec("message.send", "Send a prepared message (always asks for confirmation first).",
                          t_msg_send, _obj({"recipient": P, "text": P, "platform": P}, ["recipient", "text"]),
                          {"recipient": "str"}, agent="communication", risk=H, requires_confirmation=True,
                          verify=_send_message_verify,
                          confirm_text=lambda a: f"Send the message to {a.get('recipient', '?')}"
                                                 f" via {a.get('platform') or 'WhatsApp'}"))
    reg.register(ToolSpec("app.open", "Open an application.", t_app_open, _obj({"app_name": P}, ["app_name"]),
                          {"app_name": "str"}, agent="laptop", risk=L, verify=_open_app_verify,
                          retry_safe=True))
    reg.register(ToolSpec("web.search", "Search the web and return a text answer.", t_web_search,
                          _obj({"query": P, "mode": P}, ["query"]), {"text": "str"}, agent="research",
                          risk=S, retry_safe=True, verify=_nonempty("text", "search result")))
    reg.register(ToolSpec("browser.open", "Open a web page in the browser.", t_browser_open,
                          _obj({"url": P}, ["url"]), {"url": "str"}, agent="browser", risk=L,
                          retry_safe=True))
    reg.register(ToolSpec("browser.download", "Download a file from a URL into a folder (default Downloads).",
                          t_browser_download, _obj({"url": P, "destination": P, "filename": P}, ["url"]),
                          {"path": "str"}, agent="browser", risk=M, undoable=True, retry_safe=True,
                          verify=lambda a, r: V.file_valid(r.data.get("path")) if r.ok else V.failed(r.error),
                          verification_required=True))
    reg.register(ToolSpec("system.status", "CPU / RAM / disk / battery.", t_system_status, _obj({}),
                          {"text": "str"}, agent="system", risk=S, retry_safe=True))
    reg.register(ToolSpec("reminder.create", "Create a reminder (date YYYY-MM-DD, time HH:MM).", t_reminder,
                          _obj({"date": P, "time": P, "message": P}, ["message"]), None,
                          agent="reminder", risk=L))
    reg.register(ToolSpec("screen.describe", "Look at the current screen and describe/answer about it.",
                          t_screen_describe, _obj({"question": P}), {"description": "str"},
                          agent="vision", risk=S, retry_safe=True,
                          verify=_nonempty("description", "screen description")))




# ── Added: open a file with its default application, and URL extraction ──────

def t_file_open(args, ctx):
    """Open a document with the OS default app. No shell: os.startfile on
    Windows, argv-list subprocess elsewhere — the path can't inject commands."""
    import platform
    import subprocess
    p = _resolve_file(args["path"])
    if not p.exists():
        return ToolResult.failure(f"'{p.name}' does not exist.")
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(str(p))                      # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as e:
        return ToolResult.failure(f"Could not open {p.name}: {type(e).__name__}")
    return ToolResult(True, {"path": str(p)}, f"Opened {p.name}.")


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def t_extract_url(args, ctx):
    text = str(args.get("text") or "")
    ext = str(args.get("extension") or "").lower().lstrip(".")
    urls = [u.rstrip(".,;") for u in _URL_RE.findall(text)]
    if ext:
        preferred = [u for u in urls if urlparse(u).path.lower().endswith("." + ext)]
        urls = preferred or urls
    if not urls:
        return ToolResult.failure("I couldn't find a link to download in the results.")
    return ToolResult(True, {"url": urls[0], "all": urls[:10]}, f"Found link: {urls[0]}")


def register_extra_tools(reg: ToolRegistry) -> None:
    reg.register(ToolSpec("file.open", "Open a file with its default application.", t_file_open,
                          _obj({"path": STR}, ["path"]), {"path": "str"}, agent="file", risk=L,
                          verify=lambda a, r: V.unverifiable("The OS does not report whether the viewer "
                                                             "opened the file.") if r.ok else V.failed(r.error)))
    reg.register(ToolSpec("text.extract_url", "Pull the first http(s) link (optionally with a given file "
                          "extension) out of some text, e.g. search results.", t_extract_url,
                          _obj({"text": STR, "extension": STR}, ["text"]), {"url": "str"},
                          agent="general", risk=S, retry_safe=True))


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_legacy_tools(reg)
    register_structured_tools(reg)
    register_extra_tools(reg)
    # Phase 5 — long-running research / project analysis / multi-agent builds
    from core import computer_agent, plugin_market, research, team, vision
    plugin_market.register(reg)
    research.register(reg)
    team.register(reg)
    vision.register(reg)
    computer_agent.register(reg)
    # Phase 6 — background processes, running/checking built websites, web agent,
    # the edit-and-verify coding agent, and the design (raster/vector) agent
    from core import browser_agent, code_agent, design_agent, processes, webdev
    processes.register(reg)
    webdev.register(reg)
    browser_agent.register(reg)
    code_agent.register(reg)
    design_agent.register(reg)
    # Phase 7 — drawing with the mouse in Paint, and user references
    from core import draw_agent, learning, references, telegram_bot
    draw_agent.register(reg)
    references.register(reg)
    learning.register(reg)
    telegram_bot.register(reg)          # the pocket line (dormant without TELEGRAM_BOT_TOKEN)
    return reg
