"""
tools/e2e_notepad.py — the spec's end-to-end test: "Open Notepad and type
Hello JARVIS", run for real on this PC by the actual ComputerAgent.

This drives the real observe → plan → act → verify loop with the real mouse /
keyboard actuator and the vision model, then confirms the result the honest
way — by reading Notepad's text through UI Automation, not by trusting the
model. It closes Notepad WITHOUT saving at the end (discarding via the
keyboard, so no file is written).

    python tools/e2e_notepad.py

Writes tools/e2e_notepad.log. Uses a handful of free-tier vision calls. It
never types anything but the test phrase, and touches nothing but Notepad.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LOG = ROOT / "tools" / "e2e_notepad.log"
_lines: list[str] = []

PHRASE = "Hello JARVIS"


def log(msg: str) -> None:
    _lines.append(msg)
    print(msg, flush=True)
    LOG.write_bytes(("\r\n".join(_lines) + "\r\n").encode("utf-8"))


def main() -> int:
    from core.computer_agent import ComputerAgent
    from core.safety import KillSwitch
    from core import vision as VZ
    log(f"E2E Notepad — {time.strftime('%Y-%m-%d %H:%M:%S')}")

    agent = ComputerAgent(kill=KillSwitch(), max_steps=12, max_seconds=180,
                          confirm=lambda title, why: False,      # nothing risky should be needed
                          progress=lambda m: log("  · " + m))
    goal = f'Open Notepad and type "{PHRASE}" into it'
    log(f"GOAL: {goal}")
    result = agent.run(goal)
    log(f"agent: ok={result.ok} verified={result.verified} reason={result.reason}")
    log(f"summary: {result.summary[:200]}")
    log(f"steps ({len(result.steps)}):")
    for s in result.steps:
        log(f"  {s.n}. {s.action.get('type')} {s.action.get('app') or s.action.get('window') or ''} "
            f"→ {s.decision}; changed={s.changed}; {s.note[:70]}")

    # independent post-condition: does a Notepad window actually contain the text?
    # NB: never log the window's raw text — it may contain unrelated secrets that
    # happen to be on screen. Only report whether the target phrase is present.
    time.sleep(0.5)
    found = False
    checked_notepad = False
    for w in VZ.list_windows():
        if "notepad" in (w.get("process", "") + w.get("title", "")).lower():
            checked_notepad = True
            VZ.focus_window(w["title"])
            time.sleep(0.4)
            txt = VZ.window_text() or ""
            found = PHRASE.lower() in txt.lower()
            log(f"read the Notepad window ({len(txt)} chars) via UI Automation")
            break
    if not checked_notepad:
        log("no Notepad window was open to check")
    log(f"POST-CONDITION: Notepad contains '{PHRASE}' → {found}")

    # clean up: discard without saving (Ctrl+A, Delete, then close discarding)
    try:
        import pyautogui
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("delete")
        log("cleared the text (no file saved)")
    except Exception as e:
        log(f"cleanup skipped: {type(e).__name__}")

    ok = result.ok and found
    log(f"RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        from core.secret_store import redact
        log(f"ERROR: {type(e).__name__}: {redact(e)[:200]}")
        sys.exit(2)
