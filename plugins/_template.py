"""
Drop-in JARVIS plugin template.

Copy this file, rename it (no leading underscore), fill in PLUGIN and run().
No other file needs to change — JARVIS discovers this automatically at startup.
"""

PLUGIN = {
    "name": "my_plugin",                     # snake_case, unique, ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
    "description": (
        "One or two sentences Gemini uses to decide when to call this tool. "
        "Be explicit about trigger phrases and, if it could be confused with "
        "another tool, say which tool NOT to use instead (see game_updater's "
        "description in main.py for the pattern)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "example_arg": {"type": "STRING", "description": "What this argument means"},
        },
        "required": [],   # omit or leave empty for a zero-argument tool
    },
    # ── Optional (Phase 4). Plugins go through the same permission engine,
    # audit log and verification as core tools — they cannot bypass them.
    "risk": "medium",                 # safe | low | medium | high | critical
                                      #   high/critical → user must press CONFIRM on the HUD
    # "requires_confirmation": True,  # force a confirmation regardless of risk
    "permissions": [],                # what it touches, shown in the Plugin Manager:
                                      #   e.g. ["network"], ["filesystem"], ["messaging"]
    "capabilities": [],               # free-form tags, e.g. ["smart_home"]
    "config": {},                     # defaults passed to on_load(config)
}


# Optional lifecycle hooks:
# def on_load(config: dict) -> None:      # once at startup; raise to disable the plugin
#     ...
# def on_unload() -> None:                # once at shutdown
#     ...
#
# Optional result verification (JARVIS won't claim success if this says no):
# def verify(parameters: dict, result_text: str) -> bool | tuple[bool, str]:
#     return True

def run(parameters: dict, player=None, session_memory=None) -> str:
    """
    parameters: dict of the args Gemini extracted, matching PLUGIN['parameters'].
    player: the JarvisUI instance — use player.write_log(f"JARVIS: ...") to log,
            same as actions/*.py. May be None.
    session_memory: reserved, usually None today (core tools mostly pass None too).
    Return a short natural-language string — this is spoken back to the user.
    Never raise: catch your own errors and return a spoken error string instead
    (the loader also catches exceptions as a second safety net, but don't rely on it).
    """
    example_arg = parameters.get("example_arg", "")
    try:
        result_text = f"Did the thing with {example_arg}."
    except Exception as e:
        return f"Sir, my_plugin failed: {e}"
    if player:
        try:
            player.write_log(f"JARVIS: {result_text}")
        except Exception:
            pass
    return result_text
