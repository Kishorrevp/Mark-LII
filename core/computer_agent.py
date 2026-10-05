"""
core/computer_agent.py — a true computer-use agent.

    goal ─► OBSERVE (screenshot + window list)
             └► UNDERSTAND + PLAN (vision model: next single action, why, risk,
                                   what should change)
                 └► POLICY (block / confirm / allow)            ◄── kill switch
                     └► ACT (click · type · key · scroll · focus window)
                         └► OBSERVE again → VERIFY (did the screen change as
                            expected?) → CONTINUE / RECOVER / STOP
             … until the model says "done" — which is only accepted after a
             separate yes/no verification look at the final screen.

LIMITS: 15 actions and 4 minutes per run (config computer_max_steps), one
action per model turn, 0.6–1.5 s settle time between acting and looking.

SAFETY POLICY (applied to every single action, in code — not left to the model)
  BLOCKED outright (JARVIS stops and tells you to do it yourself)
    · typing anything that looks like a password / key / card or ID number
    · password / PIN / credential fields, CAPTCHA and "I'm not a robot" checks
    · terminals, command prompts, PowerShell, the Run dialog (Win+R) — the
      agent never gets a shell through the GUI
    · UAC / administrator prompts, Windows Security & firewall settings
  ASK FIRST (HUD confirmation, never auto-approved, silence = no)
    · pay / buy / order / transfer / checkout / subscribe
    · send / post / publish / submit / reply-all / share
    · delete / remove / uninstall / format / empty trash / close unsaved
    · accept terms / sign / grant permissions / log out
    · anything the model itself marks risky or critical
  Everything else runs, and is written to the audit log (typed text is
  logged as a length, never the content).

The emergency stop (F12 / STOP / "stop everything") is checked before every
action and between the observe/act/verify phases; PyAutoGUI's own FAILSAFE
(slam the mouse into a screen corner) stays enabled as a second, physical
stop.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from core.safety import EmergencyStop, KillSwitch, kill_switch
from core.vision import Capture, capture as _capture, change_ratio, list_windows

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},            # continue | done | impossible | need_user
        "thought": {"type": "string"},
        "action": {"type": "object", "properties": {
            "type": {"type": "string"},          # click double_click right_click type key hotkey scroll wait focus_window
            "target": {"type": "string"},
            "point": {"type": "array", "items": {"type": "number"}},   # [y, x] 0-1000
            "to": {"type": "array", "items": {"type": "number"}},      # drag end [y, x] 0-1000
            "text": {"type": "string"},
            "keys": {"type": "array", "items": {"type": "string"}},
            "direction": {"type": "string"},
            "amount": {"type": "integer"},
            "window": {"type": "string"},
        }},
        "risk": {"type": "string"},              # safe | normal | risky | critical
        "risk_reason": {"type": "string"},
        "expected": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["status", "thought"],
}
VERIFY_SCHEMA = {"type": "object", "properties": {"done": {"type": "boolean"}, "evidence": {"type": "string"}},
                 "required": ["done", "evidence"]}

_BLOCK_WINDOWS = re.compile(r"(command prompt|cmd\.exe|powershell|windows terminal|\bterminal\b|git bash|"
                            r"\bbash\b|wsl|^run$|user account control|windows security|firewall|"
                            r"credential manager|registry editor|regedit)", re.I)
_BLOCK_TARGET = re.compile(r"(password|passcode|\bpin\b|otp|one[- ]time|captcha|i'?m not a robot|recaptcha|"
                           r"security code|cvv|card number|run as administrator|\buac\b)", re.I)
_CONFIRM = re.compile(r"\b(pay|payment|buy|purchase|order|checkout|check out|place order|transfer|send money|"
                      r"subscribe|donate|send|post|publish|submit|tweet|reply all|share|upload|delete|remove|"
                      r"uninstall|format|erase|empty (?:the )?(?:recycle bin|trash)|discard|don'?t save|"
                      r"close without saving|accept|agree|i agree|sign in with|authori[sz]e|allow access|grant|"
                      r"log ?out|sign ?out|factory reset|reset)\b", re.I)
_BLOCK_KEYS = [{"win", "r"}, {"win", "x"}, {"ctrl", "alt", "delete"}, {"ctrl", "shift", "esc"}]
_CONFIRM_KEYS = [{"alt", "f4"}, {"ctrl", "w"}, {"shift", "delete"}, {"ctrl", "enter"}]


@dataclass
class StepLog:
    n: int
    action: dict
    thought: str
    risk: str
    decision: str
    changed: Optional[float] = None
    note: str = ""


@dataclass
class RunResult:
    ok: bool
    summary: str
    steps: list = field(default_factory=list)
    verified: bool = False
    reason: str = ""


def classify(action: dict, window_title: str, model_risk: str = "") -> tuple[str, str]:
    """→ (decision, why): decision ∈ allow | confirm | block."""
    a = action or {}
    kind = str(a.get("type") or "").lower()
    text = str(a.get("text") or "")
    target = str(a.get("target") or "")
    blob = f"{target} {text} {a.get('window') or ''}"
    if kind in ("type", "click", "double_click", "key", "hotkey", "drag", "right_click") and \
            _BLOCK_WINDOWS.search(window_title or ""):
        return "block", f"'{window_title[:60]}' is a shell / security window — I never operate those"
    if kind == "focus_window" and _BLOCK_WINDOWS.search(a.get("window") or ""):
        return "block", "I don't operate terminals or security windows"
    if kind in ("open_app", "launch", "open_application"):
        app = str(a.get("app") or a.get("target") or a.get("window") or "")
        if _BLOCK_WINDOWS.search(app) or re.search(r"\b(cmd|command prompt|powershell|pwsh|terminal|wsl|"
                                                   r"regedit|registry|run)\b", app, re.I):
            return "block", "I don't open terminals, the Run dialog or security tools"
        return "allow", ""
    if _BLOCK_TARGET.search(blob):
        return "block", "that is a password / verification / admin step — please do it yourself"
    if kind == "type" and text:
        try:
            from core.secret_store import looks_like_secret
            from memory.memory_hub import is_sensitive
            if looks_like_secret(text) or is_sensitive(text)[0]:
                return "block", "the text looks like a password, key or account number"
        except Exception:
            pass
    if kind in ("key", "hotkey"):
        keys = {str(k).lower().replace("windows", "win").replace("control", "ctrl") for k in (a.get("keys") or [])}
        if any(k <= keys for k in _BLOCK_KEYS):
            return "block", "that shortcut opens a system dialog / shell"
        if any(k <= keys for k in _CONFIRM_KEYS):
            return "confirm", "that shortcut closes or submits something"
    if _CONFIRM.search(blob):
        return "confirm", f"'{(target or text)[:60]}' looks like an important action"
    if str(model_risk).lower() in ("risky", "critical", "high"):
        return "confirm", "the agent marked this step as risky"
    return "allow", ""


class PyAutoGUIActuator:
    """Real mouse/keyboard. Imported lazily so tests never touch the desktop."""

    def __init__(self):
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        self.p = pyautogui

    def do(self, a: dict, xy: Optional[tuple[int, int]]) -> str:
        k = a["type"]
        p = self.p
        if k in ("click", "double_click", "right_click"):
            if xy is None:
                raise ValueError("no position for the click")
            p.moveTo(xy[0], xy[1], duration=0.15)
            if k == "click":
                p.click()
            elif k == "double_click":
                p.doubleClick()
            else:
                p.rightClick()
            return f"{k} at {xy}"
        if k == "drag":                                   # Phase 7: press, move, release
            to = a.get("_to_xy")
            if xy is None or not to:
                raise ValueError("a drag needs a start point and an end point")
            p.moveTo(xy[0], xy[1], duration=0.15)
            p.dragTo(to[0], to[1], duration=0.6, button="left")
            return f"dragged {xy} → {tuple(to)}"
        if k == "move":
            if xy is None:
                raise ValueError("no position to move to")
            p.moveTo(xy[0], xy[1], duration=0.2)
            return f"moved the pointer to {xy}"
        if k == "type":
            text = str(a.get("text") or "")
            if xy is not None:
                p.click(xy[0], xy[1])
            if text.isascii():
                p.typewrite(text, interval=0.02)
            else:                                        # Tamil / emoji → clipboard paste
                import pyperclip
                old = None
                try:
                    old = pyperclip.paste()
                except Exception:
                    pass
                pyperclip.copy(text)
                p.hotkey("ctrl", "v")
                time.sleep(0.2)
                if old is not None:
                    try:
                        pyperclip.copy(old)
                    except Exception:
                        pass
            return f"typed {len(text)} characters"
        if k == "key":
            keys = [str(x).lower() for x in (a.get("keys") or [a.get("text") or "enter"])]
            p.press(keys[0])
            return f"pressed {keys[0]}"
        if k == "hotkey":
            keys = [str(x).lower() for x in (a.get("keys") or [])][:4]
            p.hotkey(*keys)
            return "pressed " + "+".join(keys)
        if k == "scroll":
            amt = max(1, min(int(a.get("amount") or 5), 25))
            if xy is not None:
                p.moveTo(xy[0], xy[1], duration=0.1)
            p.scroll(amt * 120 if str(a.get("direction", "down")).lower() == "up" else -amt * 120)
            return f"scrolled {a.get('direction', 'down')}"
        if k == "wait":
            time.sleep(min(float(a.get("amount") or 2), 5))
            return "waited"
        if k == "focus_window":
            from core.vision import focus_window
            return "focused" if focus_window(str(a.get("window") or a.get("target") or "")) else "window not found"
        if k in ("open_app", "launch", "open_application"):
            name = str(a.get("app") or a.get("target") or a.get("window") or "").strip()
            if not name:
                raise ValueError("no app name")
            return _launch_app(name)
        raise ValueError(f"unknown action '{k}'")


# Apps the computer agent may launch itself. A terminal/shell is never here — the
# agent must not get a command line through this path (classify() also blocks it).
_LAUNCHABLE = {
    "notepad": "notepad.exe", "wordpad": "write.exe", "write": "write.exe", "paint": "mspaint.exe",
    "mspaint": "mspaint.exe", "calculator": "calc.exe", "calc": "calc.exe", "explorer": "explorer.exe",
    "file explorer": "explorer.exe", "snipping tool": "snippingtool.exe", "settings": "ms-settings:",
    "edge": "msedge", "microsoft edge": "msedge", "chrome": "chrome", "google chrome": "chrome",
    "firefox": "firefox", "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
    "outlook": "outlook", "spotify": "spotify", "vlc": "vlc", "vs code": "code", "vscode": "code",
    "code": "code",
}


def _launch_app(name: str) -> str:
    import os
    import subprocess
    key = name.lower().strip()
    target = _LAUNCHABLE.get(key)
    if target is None:                       # allow a bare, single-token name (no path, no args, no shell metachars)
        if re.fullmatch(r"[A-Za-z0-9 ._-]{1,40}", name) and not re.search(
                r"\b(cmd|powershell|pwsh|wt|bash|terminal|wsl|regedit|run)\b", key):
            target = name
        else:
            return f"I won't launch '{name}' this way"
    try:
        if target.endswith(":"):             # a settings: URI
            os.startfile(target)             # type: ignore[attr-defined]
        elif target.endswith(".exe") or "\\" not in target:
            subprocess.Popen(["cmd", "/c", "start", "", target], shell=False,
                             creationflags=0x08000000)
        else:
            subprocess.Popen([target], creationflags=0x08000000)
        time.sleep(1.2)
        return f"launched {name}"
    except Exception as e:
        return f"couldn't launch {name}: {type(e).__name__}"


class ComputerAgent:
    def __init__(self, *, model: Optional[Callable[[str, Capture, dict], dict]] = None,
                 capture: Callable[[], Capture] = None, actuator=None, kill: KillSwitch | None = None,
                 confirm: Optional[Callable[[str, str], bool]] = None,
                 progress: Optional[Callable[[str], None]] = None,
                 should_stop: Optional[Callable[[], bool]] = None, audit=None,
                 max_steps: int = 15, max_seconds: float = 240.0, settle: float = 0.8,
                 sleep: Callable[[float], None] = time.sleep, windows: Callable[[], list] = list_windows):
        self._model = model or self._gateway_model
        self._capture = capture or (lambda: _capture("screen"))
        self._act = actuator
        self.kill = kill or kill_switch()
        self._confirm = confirm
        self._progress = progress or (lambda m: None)
        self._should_stop = should_stop or (lambda: False)
        self.audit = audit
        self.max_steps, self.max_seconds, self.settle = max_steps, max_seconds, settle
        self._sleep = sleep
        self._windows = windows

    # -- model -------------------------------------------------------------------
    @staticmethod
    def _gateway_model(prompt: str, cap: Capture, schema: dict) -> dict:
        from core.ai_gateway import get_gateway
        return get_gateway().generate(prompt, images=[(cap.data, cap.mime)], schema=schema,
                                      tier="computer_use", reasoning="low", temperature=0.2, timeout=60.0).json()

    def _check(self) -> None:
        self.kill.check()
        if self._should_stop():
            raise EmergencyStop("Stopped.")

    def _plan(self, goal, log, wins, active, no_effect, cap, tries: int = 3):
        """Call the vision model, tolerating a transient failure (a busy 5xx, a
        brief rate limit) with a short backoff. Returns the decision dict, or
        None when the model can't be reached — the caller stops cleanly rather
        than acting on the desktop without seeing it."""
        prompt = self._prompt(goal, log, wins, active, no_effect)
        for i in range(tries):
            self._check()
            try:
                return self._model(prompt, cap, ACTION_SCHEMA) or {}
            except EmergencyStop:
                raise
            except Exception as e:
                transient = any(s in str(e).lower() for s in ("503", "unavailable", "high demand", "429",
                                                              "timed out", "timeout", "temporarily"))
                if not transient or i == tries - 1:
                    if self.audit:
                        try:
                            from core.secret_store import redact
                            self.audit.record("computer_model_error", detail=redact(str(e))[:160])
                        except Exception:
                            pass
                    return None
                self._progress("The vision service is busy — waiting a moment…")
                self._sleep(2.0 * (i + 1))
        return None

    # -- main loop -----------------------------------------------------------------
    def run(self, goal: str) -> RunResult:
        goal = str(goal or "").strip()
        if not goal:
            return RunResult(False, "No goal given.")
        started = time.time()
        log: list[StepLog] = []
        no_effect = 0
        try:
            self._check()
            cap = self._capture()
            for n in range(1, self.max_steps + 1):
                self._check()
                if time.time() - started > self.max_seconds:
                    return RunResult(False, f"I ran out of time after {n - 1} actions.", log,
                                     reason="timeout")
                wins = []
                try:
                    wins = self._windows()
                except Exception:
                    pass
                active = next((w.get("title", "") for w in wins if w.get("active")), cap.title)
                self._progress(f"Looking at the screen (step {n})…")
                plan = self._plan(goal, log, wins, active, no_effect, cap)
                if plan is None:
                    return RunResult(False, "The vision model is unavailable right now (the service is busy or "
                                            "out of free quota), so I stopped instead of acting blindly.", log,
                                     reason="model-unavailable")
                status = str(plan.get("status") or "continue").lower()
                if status == "done":
                    ok, evidence = self._verify_done(goal)
                    if ok:
                        return RunResult(True, plan.get("message") or f"Done: {goal}. {evidence}", log,
                                         verified=True)
                    log.append(StepLog(n, {"type": "check"}, "claimed done", "safe", "not-verified",
                                       note=evidence))
                    cap = self._capture()
                    continue
                if status in ("impossible", "need_user"):
                    return RunResult(False, plan.get("message") or plan.get("thought") or "I can't do this "
                                     "on screen.", log, reason=status)
                action = dict(plan.get("action") or {})
                if not action.get("type"):
                    return RunResult(False, "The agent didn't propose an action.", log, reason="no-action")
                decision, why = classify(action, active, plan.get("risk", ""))
                entry = StepLog(n, self._safe_action(action), str(plan.get("thought", ""))[:200],
                                str(plan.get("risk", "")), decision, note=why)
                log.append(entry)
                self._audit(entry)
                if decision == "block":
                    return RunResult(False, f"I stopped: {why}.", log, reason="blocked")
                if decision == "confirm":
                    desc = self._describe(action)
                    if self._confirm is None or not self._confirm(f"Computer agent: {desc}", why):
                        entry.decision = "declined"
                        return RunResult(False, f"I stopped before \"{desc}\" — it needed your "
                                                f"confirmation and didn't get it.", log, reason="declined")
                    entry.decision = "approved"
                self._check()
                xy = cap.to_screen(action["point"]) if isinstance(action.get("point"), (list, tuple)) \
                    and len(action["point"]) == 2 else None
                if isinstance(action.get("to"), (list, tuple)) and len(action["to"]) == 2:
                    action["_to_xy"] = cap.to_screen(action["to"])
                self._progress(self._describe(action))
                actuator = self._act or PyAutoGUIActuator()
                self._act = actuator
                try:
                    entry.note = (entry.note + "; " if entry.note else "") + actuator.do(action, xy)
                except EmergencyStop:
                    raise
                except Exception as e:
                    entry.note += f"; action failed: {type(e).__name__}"
                self._sleep(self.settle if action["type"] != "wait" else 0.2)
                self._check()
                new = self._capture()
                entry.changed = round(change_ratio(cap, new), 3)
                no_effect = no_effect + 1 if entry.changed < 0.004 and action["type"] not in ("wait",) else 0
                cap = new
                if no_effect >= 3:
                    return RunResult(False, "Three actions in a row had no visible effect, so I stopped "
                                            "instead of guessing.", log, reason="no-effect")
            return RunResult(False, f"I reached the {self.max_steps}-action limit without finishing.", log,
                             reason="step-limit")
        except EmergencyStop as e:
            return RunResult(False, f"Stopped by the emergency stop. {len(log)} action(s) had run.", log,
                             reason="stopped")

    # -- helpers ---------------------------------------------------------------------
    def _verify_done(self, goal: str) -> tuple[bool, str]:
        """Self-verification: a fresh look, a yes/no question, evidence required.
        For a typing goal, also read the window's actual text (UI Automation) —
        a real post-condition, not just pixels."""
        self._check()
        # UIA cross-check for "type / write X" goals
        mtext = re.search(r"\b(?:typ(?:e|ing|ed)|writ(?:e|ing|ten)|enter(?:ing|ed)?)\b[^\"'`]*[\"'`]([^\"'`]{1,120})",
                          goal, re.I)
        if mtext:
            wanted = mtext.group(1).strip()
            try:
                from core.vision import window_text
                have = window_text()
                if wanted and have and wanted.lower() in have.lower():
                    return True, f"the window contains \"{wanted}\" (read via UI Automation)"
            except Exception:
                pass
        cap = self._capture()
        try:
            r = self._model(f"Look at this screen. Has this goal been fully accomplished: \"{goal}\"? "
                            "Answer done=true only if the screen clearly shows it; give the visible evidence.",
                            cap, VERIFY_SCHEMA) or {}
        except Exception as e:
            return False, f"verification look failed ({type(e).__name__})"
        return bool(r.get("done")), str(r.get("evidence") or "")[:200]

    def _prompt(self, goal: str, log: list, wins: list, active: str, no_effect: int) -> str:
        hist = "\n".join(f"{s.n}. {s.action.get('type')} {s.action.get('target') or s.action.get('keys') or ''}"
                         f" → {s.decision}; screen changed {s.changed}; {s.note[:80]}" for s in log[-6:]) or "none yet"
        win_txt = "; ".join(f"{w.get('title', '')[:50]}{' [ACTIVE]' if w.get('active') else ''}"
                            for w in wins[:12]) or "(unknown)"
        recover = ("\nYour last actions had NO visible effect — try a different approach (keyboard shortcut, "
                   "focus the right window, scroll to find the element, or report impossible)." if no_effect else "")
        return (
            "You control a Windows PC to achieve the user's goal, ONE action at a time. You see the current "
            "screenshot. Decide the single next action.\n"
            f"GOAL: {goal}\nACTIVE WINDOW: {active}\nOPEN WINDOWS: {win_txt}\nPREVIOUS ACTIONS:\n{hist}{recover}\n"
            "Rules: point = [y, x] normalised 0-1000 on THIS screenshot (centre of the element). Action types: "
            "click, double_click, right_click, drag (point = start, to = end; for sliders, moving things, drawing a "
            "line), move (hover at point), type (text; include point to click the field first), key (keys=[name]), "
            "hotkey (keys=[...]), scroll (direction up/down, amount 1-10), wait (amount seconds), focus_window "
            "(window = part of title), open_app (app = the application's name, e.g. Notepad — PREFER this over "
            "hunting the Start menu to open an app). status='done' only when the goal is visibly complete; 'need_user' for "
            "logins, passwords, CAPTCHAs or anything only the user should do; 'impossible' if it cannot be done. "
            "Mark risk 'critical' for payments, sending, deleting, submitting, accepting terms. Never use "
            "terminals or the Run dialog. expected = what should change on screen."
        )

    @staticmethod
    def _safe_action(a: dict) -> dict:
        out = {k: v for k, v in a.items() if k not in ("text", "_to_xy")}
        if a.get("text"):
            out["text_len"] = len(str(a["text"]))
        return out

    @staticmethod
    def _describe(a: dict) -> str:
        k = a.get("type")
        tgt = a.get("target") or ""
        if k == "type":
            return f"type {len(str(a.get('text') or ''))} characters" + (f" into {tgt}" if tgt else "")
        if k in ("key", "hotkey"):
            return "press " + "+".join(map(str, a.get("keys") or [a.get("text") or ""]))
        if k == "focus_window":
            return f"switch to {a.get('window') or tgt}"
        if k == "scroll":
            return f"scroll {a.get('direction', 'down')}"
        return f"{str(k).replace('_', ' ')} {tgt}".strip()

    def _audit(self, e: StepLog) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record("computer_action", step=e.n, action=e.action, risk=e.risk, decision=e.decision,
                              detail=e.note[:120])
        except Exception:
            pass


# ── tool ─────────────────────────────────────────────────────────────────────

_AGENT_FACTORY: Optional[Callable[..., ComputerAgent]] = None


def set_agent_factory(fn) -> None:
    """Test hook: build the agent with fake model / capture / actuator."""
    global _AGENT_FACTORY
    _AGENT_FACTORY = fn


def t_computer_run(args: dict, ctx) -> Any:
    from core.tools.registry import ToolResult
    goal = str(args.get("goal") or "").strip()
    if not goal:
        return ToolResult.failure("What should I do on the screen?")
    ex = getattr(ctx, "extras", None) or {}
    try:
        import json as _j
        from core.secret_store import legacy_config_path
        max_steps = int(_j.loads(legacy_config_path().read_text(encoding="utf-8")).get("computer_max_steps", 15))
    except Exception:
        max_steps = 15
    audit = None
    try:
        from core.audit import AuditLog
        audit = AuditLog()
    except Exception:
        pass
    agent = (_AGENT_FACTORY or ComputerAgent)(confirm=ex.get("confirm"), progress=ex.get("progress"),
                          should_stop=ex.get("should_stop"), audit=audit, max_steps=max(3, min(max_steps, 40)))
    r = agent.run(goal)
    data = {"verified": r.verified, "actions": len(r.steps), "reason": r.reason,
            "log": [{"n": s.n, "action": s.action, "decision": s.decision, "changed": s.changed} for s in r.steps]}
    if r.ok:
        return ToolResult(True, data, r.summary)
    return ToolResult(False, data, "", r.summary)


def register(reg) -> None:
    from core.permissions import RiskLevel
    from core.tools.registry import ToolSpec
    from core import verification as V
    reg.register(ToolSpec(
        "computer.run", "Operate the PC's screen with mouse and keyboard to reach a goal (observe → act → verify "
        "loop, max 15 actions). Asks before starting and before any important click.", t_computer_run,
        {"type": "object", "properties": {"goal": {"type": "string"}}, "required": ["goal"]},
        {"verified": "bool"}, agent="laptop", risk=RiskLevel.HIGH_RISK, requires_confirmation=True,
        confirm_text=lambda a: f"Let JARVIS use the mouse & keyboard to: {str(a.get('goal', ''))[:80]}",
        verify=lambda a, r: (V.verified("the final screen was checked") if r.data.get("verified")
                             else V.unverifiable("couldn't confirm on screen")) if r.ok else V.failed(r.error)))
