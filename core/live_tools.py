"""
core/live_tools.py — what the voice model is told about, per session.

Every function declaration rides in the Live session's context on every turn.
Phase 6 sent all 43 (≈35,000 characters ≈ 9,000 tokens) whether or not the
conversation would ever touch them. That costs time on each reply and gives
the model more wrong tools to reach for.

Profiles (config/runtime.json → voice.tool_profile):
    full  (default)  every declaration, plus `use_tool` (so the prompt's routing
                     rules hold in both profiles)
    lean             the tools a voice conversation actually uses, with their
                     descriptions, plus ONE `use_tool` declaration that lists the
                     rest in a compact one-line catalogue. Nothing is lost:
                     use_tool runs any of them through the same permission gate.

Measured on the user's PC (tools/latency_bench.py, 1 Oct 2026): 47 vs 34
declarations → 0.65 vs 0.69 s speech-to-first-audio on gemini-3.8-live — no
measurable difference, so "full" is the default and "lean" is optional.

Dependency-free; tested by real execution.
"""
from __future__ import annotations

import json

# Always declared in the lean profile (order kept from main.TOOL_DECLARATIONS).
CORE = (
    # conversation, memory, knowledge
    "save_memory", "remember", "recall_memory", "recall_context", "search_knowledge", "show_sources",
    "think", "about_self", "switch_model", "set_mode", "set_language",
    # doing things
    "open_app", "web_search", "computer_settings", "browser_control", "file_controller", "reminder",
    "youtube_video", "send_message", "system_status", "weather_report",
    # eyes and hands
    "screen_process", "close_camera", "analyze_screen", "watch_screen", "computer_use", "draw",
    "reference",
    # tasks and safety
    "run_task", "task_control", "undo", "emergency_stop", "shutdown_jarvis",
)

# One line each for the catalogue inside use_tool (≤ 90 chars).
CATALOG_HINTS = {
    "file_processor": "analyse / summarise / convert a file (pdf, docx, xlsx, csv, image …)",
    "code_helper": "write, edit, explain or run a script",
    "dev_agent": "build a small multi-file program",
    "computer_control": "raw mouse/keyboard: click, type, hotkey, scroll, screenshot",
    "desktop_control": "wallpaper, organise desktop, list desktop",
    "game_updater": "Steam/Epic game updates and scheduled downloads",
    "flight_finder": "search flights between two cities on a date",
    "manage_monitor": "add / remove / list news topics watched in the background",
    "workflow": "list / save / run saved routines",
    "memory_inspect": "summarise everything remembered, by category",
    "forget_memory": "delete one stored fact (category + key)",
    "capabilities": "check whether JARVIS can do something; find a catalog plugin",
    "install_plugin": "install a plugin from the bundled catalog (HUD confirm)",
    "learning": "what JARVIS learned about how the user likes things / habits; forget; pause",
    "pocket": "the user's Telegram line: pair (code on HUD), status, away, back, send brief",
}

USE_TOOL_NAME = "use_tool"


def _catalog(names: list[str]) -> str:
    lines = []
    for n in names:
        hint = CATALOG_HINTS.get(n, "")
        lines.append(f"{n}: {hint}" if hint else n)
    return "; ".join(lines)


def use_tool_declaration(moved: list[dict]) -> dict:
    names = [d["name"] for d in moved]
    arg_hints = []
    for d in moved:
        props = list(((d.get("parameters") or {}).get("properties") or {}).keys())[:6]
        if props:
            arg_hints.append(f"{d['name']}({', '.join(props)})")
    return {
        "name": USE_TOOL_NAME,
        "description": (
            "Run one of JARVIS's less common tools by name. Tools: " + _catalog(names) + ". "
            "Arguments per tool: " + "; ".join(arg_hints) + ". Pass `args` as a JSON object string, "
            "e.g. {\"action\": \"summarize\", \"file_path\": \"C:/x.pdf\"}. The same permission rules apply."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "tool": {"type": "STRING", "description": "Tool name from the list."},
                "args": {"type": "STRING", "description": "JSON object with the tool's arguments."},
            },
            "required": ["tool"],
        },
    }


def select_declarations(declarations: list[dict], profile: str = "lean",
                        extra: list[dict] | None = None) -> list[dict]:
    """Declarations to send for `profile`. `extra` = plugin declarations
    (always behind use_tool in lean mode)."""
    decl = list(declarations) + list(extra or [])
    core, moved = [], []
    for d in decl:
        (core if d.get("name") in CORE else moved).append(d)
    if str(profile or "lean").lower() == "full":
        # every declaration, plus use_tool so the routing rules in the prompt hold either way
        return decl + ([use_tool_declaration(moved)] if moved else [])
    if moved:
        core.append(use_tool_declaration(moved))
    return core


def parse_use_tool(args: dict) -> tuple[str, dict]:
    """(inner tool name, inner args) from a use_tool call; ('', {}) if empty."""
    name = str(args.get("tool") or "").strip()
    raw = args.get("args")
    inner: dict = {}
    if isinstance(raw, dict):
        inner = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            inner = v if isinstance(v, dict) else {}
        except Exception:
            inner = {}
    return name, inner


def size(decls: list[dict]) -> int:
    return len(json.dumps(decls))
