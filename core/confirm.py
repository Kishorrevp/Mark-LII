"""
core/confirm.py — a confirmation the model cannot forge.

THE PROBLEM WITH THE OLD GATE
    computer_settings guarded shutdown and restart like this:

        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return "Please confirm by calling again with confirmed=yes."

    `confirmed` is a tool parameter, which means the *model* writes it. Nothing
    stops it from sending confirmed=yes on the first call, and nothing checks
    that a human was ever involved. It is a convention, not a gate — and its
    coverage was two actions, so deleting files and switching off the WiFi the
    assistant is talking over went through with no gate at all.

THE DESIGN HERE
    The confirmation token is issued by the *interface*, never by the model:

      1. An action calls `request(...)` with a callable that does the real work.
      2. This module hands the UI a banner with CONFIRM / CANCEL and returns
         IMMEDIATELY with a sentence for the model to say out loud.
      3. If — and only if — the user presses CONFIRM, the UI calls `resolve()`,
         which runs the stored callable off the Qt thread.

    Nothing blocks. The model keeps talking while the banner is up, so this
    costs no latency at all; in fact it is cheaper than the old gate, which
    burned two tool round trips (reject, then re-call) on every shutdown.

PHASE 7 — ALLOW ONCE / ALLOW ALWAYS / DENY
    The banner has three answers. "Allow always" adds the action to the user's
    trusted actions (config trusted_actions, revocable in Settings →
    Permissions) and then runs it; next time that action runs without asking.
    It is only OFFERED when the caller passes a `trust_key` and
    core.permissions.can_trust() agrees: never for a CRITICAL action, never
    for power actions / plugin installs, never for a mid-task click inside the
    computer agent. Silence is still a denial; the model still cannot press any
    of the three buttons.

WHAT BELONGS HERE AND WHAT DOES NOT
    Only genuinely irreversible things. Anything that can be reversed should be
    done at once and pushed onto core/undo.py instead — undo is faster than a
    question, and an assistant that asks before every action is one nobody uses.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# A pending confirmation is abandoned after this long. Chosen to outlast a
# normal "hang on, let me look at the screen" pause without leaving a live
# shutdown button sitting on the HUD for the rest of the day.
TIMEOUT_SECONDS = 90.0


@dataclass
class _Pending:
    key:     str
    title:   str
    detail:  str
    run:     Callable[[], str]
    at:      float
    # Phase 4G: the orchestrator needs to hear "no" as well as "yes" — a task
    # waiting for permission must be told it was declined, expired or
    # superseded so it can stop instead of hanging. None for legacy callers,
    # which keep exactly their old behaviour.
    on_cancel: Optional[Callable[[str], None]] = None
    timer:   Optional[threading.Timer] = None
    trust_key: str = ""          # Phase 7: what "Allow always" would trust ('' = not offered)


_pending: Optional[_Pending] = None
_lock = threading.Lock()

# Set once at startup by main.py. Signature: (title, detail) -> None for show,
# and () -> None for hide. Both are marshalled onto the Qt thread by the UI.
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb:  Optional[Callable[[str], None]] = None


def bind(show, hide, log=None) -> None:
    """Wire this module to the HUD. Called once from main.py at startup."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


def _notify_cancel(p: Optional[_Pending], reason: str) -> None:
    if p is None:
        return
    if p.timer is not None:
        try:
            p.timer.cancel()
        except Exception:
            pass
    if p.on_cancel is not None:
        try:
            p.on_cancel(reason)
        except Exception:
            pass


def _expire(key: str, at: float) -> None:
    """Timer callback: silence is never consent — an unanswered confirmation
    becomes a denial, and the banner comes down."""
    global _pending
    with _lock:
        p = _pending
        if p is None or p.key != key or p.at != at:
            return
        _pending = None
    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass
    _log(f"SYS: Confirmation expired — {p.title}")
    _notify_cancel(p, "expired")


def can_offer_always(trust_key: str) -> bool:
    if not trust_key:
        return False
    try:
        from core.permissions import can_trust
        return can_trust(trust_key)
    except Exception:
        return False


def request(key: str, title: str, detail: str, run: Callable[[], str],
            on_cancel: Optional[Callable[[str], None]] = None, trust_key: str = "") -> str:
    """Park an irreversible action behind the on-screen gate.

    Returns the sentence the tool should hand back to the model — phrased as an
    instruction so the assistant asks the user out loud in their own language,
    rather than reading an English string verbatim.

    `on_cancel(reason)` (optional) is called with "declined" | "expired" |
    "superseded" | "cancelled" | "unavailable" when the action will NOT run."""
    global _pending

    if _show_cb is None:
        # No interface bound (headless, or a very early call). Refuse rather
        # than silently performing something irreversible.
        if on_cancel:
            try:
                on_cancel("unavailable")
            except Exception:
                pass
        return (f"I cannot confirm '{title}' right now because the interface is "
                f"not available, so I have not done it.")

    now = time.monotonic()
    always = can_offer_always(trust_key)
    new = _Pending(key=key, title=title, detail=detail, run=run, at=now, on_cancel=on_cancel,
                   trust_key=trust_key if always else "")
    with _lock:
        old, _pending = _pending, new
    _notify_cancel(old, "superseded")

    try:
        try:
            _show_cb(title, detail, always)
        except TypeError:                     # a 2-argument UI (older build / tests)
            _show_cb(title, detail)
    except Exception as e:
        with _lock:
            if _pending is new:
                _pending = None
        _notify_cancel(new, "unavailable")
        return f"Could not ask for confirmation: {e}. Nothing was done."

    t = threading.Timer(TIMEOUT_SECONDS, _expire, args=(key, now))
    t.daemon = True
    new.timer = t
    t.start()

    _log(f"SYS: Awaiting confirmation — {title}")
    return (
        f"[CONFIRMATION_PENDING] I have put a confirmation on screen for: {title}. "
        f"Say ONE short sentence in the user's own language telling them you need "
        f"them to confirm it on the HUD before you do it. Do not claim it is done."
    )


def pending_allows_always() -> bool:
    with _lock:
        return bool(_pending is not None and _pending.trust_key)


def resolve(accepted: bool, always: bool = False) -> None:
    """Called by the UI when the user presses ALLOW ONCE (accepted),
    ALLOW ALWAYS (accepted, always=True) or DENY (not accepted).

    Runs the stored callable on a worker thread — this is invoked from the Qt
    thread, and shutting the machine down from inside a button handler would
    freeze the interface on its way out."""
    global _pending

    with _lock:
        p, _pending = _pending, None

    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass

    if p is None:
        return

    if p.timer is not None:
        try:
            p.timer.cancel()
        except Exception:
            pass

    if time.monotonic() - p.at > TIMEOUT_SECONDS:
        _log(f"SYS: Confirmation expired — {p.title}")
        _notify_cancel(p, "expired")
        return

    if not accepted:
        _log(f"SYS: Cancelled — {p.title}")
        _notify_cancel(p, "declined")
        return

    if always and p.trust_key:
        try:
            from core.permissions import add_trusted
            if add_trusted(p.trust_key):
                _log(f"SYS: Always allowed — {p.title} (revoke in Settings → Permissions)")
        except Exception as e:
            _log(f"ERR: Could not remember 'always' for {p.title} — {e}. Allowed once.")

    def _worker():
        try:
            result = p.run() or "Done."
            _log(f"SYS: Confirmed — {p.title}. {result}")
        except Exception as e:
            _log(f"ERR: {p.title} failed — {e}")

    threading.Thread(target=_worker, daemon=True,
                     name=f"confirm-{p.key}").start()


def cancel_pending(key: str | None = None, reason: str = "cancelled") -> bool:
    """Withdraw the pending confirmation (e.g. the user said "cancel that" by
    voice while the banner was up). Only ever removes — never approves."""
    global _pending
    with _lock:
        p = _pending
        if p is None or (key is not None and p.key != key):
            return False
        _pending = None
    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass
    _log(f"SYS: Confirmation withdrawn — {p.title}")
    _notify_cancel(p, reason)
    return True


def pending_key() -> str:
    with _lock:
        return _pending.key if _pending is not None else ""


def pending_title() -> str:
    """'' when nothing is waiting. Lets an action avoid stacking two banners."""
    with _lock:
        if _pending is None:
            return ""
        if time.monotonic() - _pending.at > TIMEOUT_SECONDS:
            return ""
        return _pending.title
