"""
core/browser_agent.py — a careful web agent: observe → act → verify.

Given a goal ("find the price of the Pro plan on their pricing page and tell
me", "on this form, fill in name and message from what I gave you"), the agent
drives a REAL browser through the existing browser session
(actions/browser_control.py, its Playwright persistent context on the user's
profile) one small step at a time:

    OBSERVE   read the page: title, url, visible text (trimmed) and a numbered
              list of the interactive elements (links, buttons, inputs, selects)
              with their label / placeholder / type.
    DECIDE    a normal-tier model picks ONE next action from a fixed menu:
              click <n> · type <n> "<text>" · select <n> "<option>" · scroll ·
              goto <url> · read · done "<answer>" · give_up "<why>".
    ACT       the action runs against that element.
    VERIFY    observe again; the loop continues until "done", a step budget, or
              the emergency stop.

SAFETY (code-level, not just prompt)
  * It NEVER types into a password field, nor into anything that looks like a
    card number / CVC / SSN — those elements are marked "SENSITIVE — blocked"
    in the observation and the type action refuses them, telling the user to
    fill that field themselves.
  * It NEVER clicks a control that submits a login or a payment ("pay", "buy",
    "place order", "sign in", "log in", "checkout", "subscribe", "delete
    account") without a HUD confirmation (ctx.extras["confirm"]). Silence = no.
  * It does not solve CAPTCHAs (it stops and tells the user).
  * Everything is audited via the normal tool path; typed text is logged by
    length, never content.

If no model is configured the agent still OBSERVES and reports, so it degrades
to "read this page for me" rather than pretending to act.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from core.tools.registry import ToolResult

MAX_STEPS = 12
SENSITIVE_FIELD = re.compile(r"pass(?:word|code)|\bpin\b|card ?number|cardnum|cc-?num|creditcard|"
                             r"cvc|cvv|security code|\bssn\b|social security|iban|routing|account ?number",
                             re.I)
SUBMIT_GUARD = re.compile(r"\b(pay|buy|purchase|place order|checkout|check out|order now|subscribe|"
                          r"sign in|log ?in|delete account|confirm payment|complete purchase|add payment)\b",
                          re.I)
CAPTCHA = re.compile(r"captcha|recaptcha|hcaptcha|i'?m not a robot|verify you(?:'re| are) human", re.I)

# JS that returns the page's interactive elements as a compact list.
_SNAPSHOT_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const label = el => {
    let t = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') ||
             el.getAttribute('name') || el.getAttribute('title') || el.alt || '').trim();
    if (!t && el.labels && el.labels[0]) t = el.labels[0].innerText.trim();
    return t.replace(/\s+/g, ' ').slice(0, 80);
  };
  const out = [];
  const sel = 'a[href], button, input, select, textarea, [role=button], [role=link], [role=tab]';
  for (const el of document.querySelectorAll(sel)) {
    if (!vis(el)) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    out.push({
      tag, type,
      role: el.getAttribute('role') || '',
      name: (el.getAttribute('name') || '').slice(0, 40),
      autocomplete: (el.getAttribute('autocomplete') || '').slice(0, 30),
      label: label(el),
      href: (el.getAttribute('href') || '').slice(0, 120)
    });
    if (out.length >= 80) break;
  }
  return {
    title: document.title,
    url: location.href,
    text: (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').trim().slice(0, 4000),
    elements: out
  };
}
"""

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "action": {"type": "string", "enum": ["click", "type", "select", "scroll", "goto", "read",
                                              "done", "give_up"]},
        "index": {"type": "integer"},
        "text": {"type": "string"},
        "answer": {"type": "string"},
    },
    "required": ["action"],
}


@dataclass
class Observation:
    title: str = ""
    url: str = ""
    text: str = ""
    elements: list = field(default_factory=list)   # dicts from _SNAPSHOT_JS

    def sensitive(self, el: dict) -> bool:
        if el.get("type") == "password":
            return True
        blob = f"{el.get('name', '')} {el.get('autocomplete', '')} {el.get('label', '')}"
        return bool(SENSITIVE_FIELD.search(blob)) and el.get("tag") in ("input", "textarea")

    def render(self) -> str:
        lines = [f"TITLE: {self.title}", f"URL: {self.url}", f"TEXT (trimmed): {self.text[:1500]}",
                 "INTERACTIVE ELEMENTS:"]
        for i, el in enumerate(self.elements):
            kind = el.get("tag")
            if kind == "input":
                kind = f"input:{el.get('type') or 'text'}"
            note = " — SENSITIVE, blocked (the user must fill this)" if self.sensitive(el) else ""
            lines.append(f"[{i}] {kind} \"{el.get('label') or el.get('name') or el.get('href')}\"{note}")
        return "\n".join(lines)


class BrowserAgent:
    def __init__(self, session=None, model=None, confirm=None, screenshot=None, max_steps: int = MAX_STEPS,
                 progress=lambda m: None, should_stop=lambda: False):
        self._session = session
        self._model = model            # (prompt, schema) -> dict
        self._confirm = confirm or (lambda title, detail="": False)
        self._screenshot = screenshot
        self.max_steps = max_steps
        self._progress = progress
        self._should_stop = should_stop
        self.steps: list[dict] = []

    # -- browser plumbing -----------------------------------------------------
    def _sess(self):
        if self._session is not None:
            return self._session
        from actions.browser_control import _registry
        return _registry.get(None)

    def _observe(self) -> Observation:
        sess = self._sess()

        async def snap(page):
            data = await page.evaluate(_SNAPSHOT_JS)
            return data
        data = sess.run(self._with_page(snap), timeout=30)
        return Observation(title=data.get("title", ""), url=data.get("url", ""),
                           text=data.get("text", ""), elements=data.get("elements", []))

    def _with_page(self, coro_fn):
        sess = self._sess()

        async def runner():
            page = await sess._get_page()
            return await coro_fn(page)
        return runner()

    def _do(self, obs: Observation, action: str, index: int, text: str) -> str:
        sess = self._sess()
        el = obs.elements[index] if 0 <= index < len(obs.elements) else None

        async def act(page):
            handles = await page.query_selector_all(
                "a[href], button, input, select, textarea, [role=button], [role=link], [role=tab]")
            vis = []
            for h in handles:
                try:
                    if await h.is_visible():
                        vis.append(h)
                except Exception:
                    pass
            target = vis[index] if el is not None and 0 <= index < len(vis) else None
            if action == "click":
                if target is None:
                    return "no such element"
                await target.click(timeout=8000)
                await page.wait_for_timeout(600)
                return "clicked"
            if action == "type":
                if target is None:
                    return "no such field"
                await target.fill(text, timeout=8000)
                return f"typed {len(text)} character(s)"
            if action == "select":
                if target is None:
                    return "no such select"
                await target.select_option(label=text)
                return f"selected {text}"
            return "unknown"
        return sess.run(self._with_page(act), timeout=40)

    # -- main loop ------------------------------------------------------------
    def run(self, goal: str, start_url: str = "") -> ToolResult:
        if start_url:
            from actions.browser_control import browser_control
            browser_control(parameters={"action": "go_to", "url": start_url})
        try:
            obs = self._observe()
        except Exception as e:
            return ToolResult.failure(f"I couldn't read the page: {str(e)[:160]}", retryable=True)
        if self._model is None:
            return ToolResult(True, {"title": obs.title, "url": obs.url, "observation": obs.render()},
                              f"On {obs.title or obs.url}: {obs.text[:400]}")
        history: list[str] = []
        for stepno in range(self.max_steps):
            if self._should_stop():
                return ToolResult.failure("Stopped.")
            if CAPTCHA.search(obs.text):
                return ToolResult.failure("There's a CAPTCHA / 'are you human' check on this page — I don't "
                                          "solve those. Please complete it yourself and I'll carry on.")
            prompt = (
                "You are JARVIS's browser agent. Achieve the user's goal by choosing ONE next action.\n"
                f"GOAL: {goal}\n\n{obs.render()}\n\n"
                "Recent steps:\n" + ("\n".join(history[-6:]) or "(none)") + "\n\n"
                "Reply as JSON: action = click|type|select|scroll|goto|read|done|give_up; index = the [n] of the "
                "element; text = what to type / option to select / url for goto; answer = your final answer for "
                "'done' or reason for 'give_up'. Use 'done' as soon as the goal is met. Never try to fill a field "
                "marked SENSITIVE; if the goal needs one, give_up and say the user must do it.")
            try:
                d = self._model(prompt, STEP_SCHEMA) or {}
            except Exception as e:
                return ToolResult.failure(f"The browser agent's model call failed: {type(e).__name__}",
                                          retryable=True)
            action = str(d.get("action") or "read").lower()
            index = int(d.get("index", -1)) if str(d.get("index", "")).lstrip("-").isdigit() else -1
            text = str(d.get("text") or "")
            self.steps.append({"action": action, "index": index, "reasoning": str(d.get("reasoning", ""))[:200]})

            if action == "done":
                ans = str(d.get("answer") or d.get("text") or "").strip()
                return ToolResult(True, {"title": obs.title, "url": obs.url, "answer": ans,
                                         "steps": len(self.steps)}, ans or f"Done — on {obs.url}.")
            if action == "give_up":
                return ToolResult.failure("I stopped: " + str(d.get("answer") or d.get("text") or "the goal "
                                          "couldn't be reached safely.")[:200])
            el = obs.elements[index] if 0 <= index < len(obs.elements) else None

            if action in ("type", "select") and el is not None and obs.sensitive(el):
                return ToolResult.failure("That field is a password or payment field — I won't type into it. "
                                          "Please fill it in yourself.")
            label = (el.get("label") or el.get("name") or el.get("href") or "") if el else ""
            if action == "click" and el is not None and SUBMIT_GUARD.search(label):
                if not self._confirm(f"Click \"{label[:60]}\"?",
                                     f"This looks like it signs in, pays, or submits on {obs.url}"):
                    return ToolResult.failure(f"I didn't click \"{label[:50]}\" — you didn't confirm it, and it "
                                              "looks like a sign-in/payment/submit action.")
            self._progress(f"{action} {label[:40]}" if label else action)
            try:
                if action == "goto":
                    from actions.browser_control import browser_control
                    browser_control(parameters={"action": "go_to", "url": text})
                    res = "navigated"
                elif action == "scroll":
                    sess = self._sess()
                    sess.run(self._with_page(lambda p: p.mouse.wheel(0, 900)), timeout=15)
                    res = "scrolled"
                elif action == "read":
                    res = "read the page"
                else:
                    res = self._do(obs, action, index, text)
            except Exception as e:
                res = f"error: {str(e)[:120]}"
            history.append(f"{stepno+1}. {action} [{index}] {label[:30]} → {res}")
            try:
                obs = self._observe()
            except Exception as e:
                history.append(f"(couldn't re-read the page: {str(e)[:80]})")
        return ToolResult(True, {"title": obs.title, "url": obs.url, "steps": len(self.steps),
                                 "observation": obs.render()},
                          f"I did {len(self.steps)} step(s) but didn't reach a clear finish. "
                          f"Now on: {obs.title or obs.url}.")


# ── tool ─────────────────────────────────────────────────────────────────────

def t_browser_task(args: dict, ctx) -> ToolResult:
    goal = str(args.get("goal") or "").strip()
    if not goal:
        return ToolResult.failure("Tell me what to do in the browser.")
    url = str(args.get("url") or "").strip()

    def model(prompt, schema):
        from core.ai_gateway import get_gateway
        return get_gateway().generate(prompt, schema=schema, tier="normal", temperature=0.1, timeout=60.0).json()

    agent = BrowserAgent(model=model, confirm=ctx.extras.get("confirm"),
                         progress=ctx.extras.get("progress", lambda m: None),
                         should_stop=ctx.extras.get("should_stop", lambda: False))
    try:
        return agent.run(goal, url)
    except Exception as e:
        from core.secret_store import redact
        return ToolResult.failure(f"The browser agent failed: {redact(e)[:160]}", retryable=True)


def register(reg) -> None:
    from core.permissions import RiskLevel
    from core.tools.registry import ToolSpec
    STR = {"type": "string"}
    reg.register(ToolSpec(
        "browser.task", "Do a multi-step task in the web browser by looking at the page and acting on it "
        "(find something, read a value, fill a non-sensitive form). It observes, acts one step at a time, "
        "and verifies. It refuses password/payment fields and asks before any sign-in/pay/submit.",
        t_browser_task,
        {"type": "object", "properties": {"goal": STR, "url": STR}, "required": ["goal"]},
        {"answer": "str"}, agent="browser", risk=RiskLevel.MEDIUM_RISK, timeout=240))
