"""
core/undo.py — one shared undo stack for every action that changes state.

WHY THIS EXISTS
    A voice assistant misunderstands. When it does, "sorry" is not a remedy —
    the file is already in another folder and the brightness is already at 10%.
    Before this module the only recovery was to fix it by hand.

    The alternative — asking "are you sure?" before everything — is worse. Every
    confirmation is a round trip to the model and back, and an assistant that
    checks with you before turning the volume down is one you stop talking to.

    So: act immediately, remember how to reverse it, and let the user say
    "undo". Confirmation is then reserved for the handful of actions that
    genuinely cannot be reversed (see core/confirm.py).

HOW AN ACTION OPTS IN
    Actions do not have to know anything about this file's internals. They
    capture the "before" state and hand back a zero-argument callable:

        from core.undo import push_undo
        old = volume_get()
        volume_set(new)
        push_undo(f"volume → {new}%", lambda: volume_set(old))

    Only the action itself can know that the reverse of "move A to B" is "move
    B to A", which is why this cannot be fully centralised. What IS central is
    the stack, the ordering, the thread safety and the tool the model calls.

STRUCTURED RECORDS (Phase 6)
    push_undo(label, fn, kind="move", paths=[...], before=…, after=…) — the
    extra keywords are optional metadata. When the action runs inside a task
    step, the task id, step id and tool are attached automatically
    (undo_context, set by the orchestrator). records() returns the metadata
    (never the closure, never file contents) for the HUD / dashboard, and a
    listener (JarvisCore → audit log "undo_recorded") keeps a durable trail of
    what was reversible — the closures themselves live only in memory.

COST
    Pushing is a list append behind a lock: microseconds. Nothing in here runs
    until the user actually asks to undo something. This module cannot make the
    assistant slower.
"""

from __future__ import annotations

import contextvars
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

# How many reversible operations we keep. Ten is roughly "this conversation":
# far enough back to catch a mistake you noticed a few commands later, short
# enough that a closure holding a file's old contents cannot pile up in RAM.
MAX_DEPTH = 10


@dataclass
class _Entry:
    label:   str                    # human sentence, spoken back to the user
    undo:    Callable[[], str]      # returns a short result string
    at:      float = field(default_factory=time.monotonic)
    meta:    dict = field(default_factory=dict)   # kind, paths, before/after, task_id, step_id, tool, when


_stack: list[_Entry] = []
_lock = threading.Lock()
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("jarvis_undo_ctx", default={})
_listener: Optional[Callable[[dict], None]] = None
_SAFE_META = ("kind", "paths", "before", "after", "target", "count")


@contextmanager
def undo_context(**info):
    """Attach task/step/tool ids to every push_undo in this block (same thread)."""
    token = _ctx.set({**_ctx.get(), **{k: v for k, v in info.items() if v}})
    try:
        yield
    finally:
        _ctx.reset(token)


def set_listener(fn: Optional[Callable[[dict], None]]) -> None:
    global _listener
    _listener = fn


def records() -> list[dict]:
    """Most recent first: label + metadata of every undoable operation."""
    with _lock:
        return [{"label": e.label, **e.meta} for e in reversed(_stack)]


def push_undo(label: str, undo_fn: Callable[[], str], **meta) -> None:
    """Record that `label` just happened and `undo_fn()` reverses it.

    Called from action handlers, which run in executor threads — hence the
    lock. Never raises: a broken undo registration must not take down the
    action that actually succeeded."""
    if not callable(undo_fn):
        return
    try:
        clean = {k: (v if isinstance(v, (int, float, bool)) or v is None else
                     [str(x)[:260] for x in v][:20] if isinstance(v, (list, tuple)) else str(v)[:260])
                 for k, v in meta.items() if k in _SAFE_META}
        info = {**_ctx.get(), **clean, "when": datetime.now().isoformat(timespec="seconds")}
        with _lock:
            _stack.append(_Entry(label=str(label)[:120], undo=undo_fn, meta=info))
            # Drop the oldest rather than refusing the newest: the recent past
            # is what people ask to undo.
            while len(_stack) > MAX_DEPTH:
                _stack.pop(0)
        if _listener is not None:
            try:
                _listener({"label": str(label)[:120], **info})
            except Exception:
                pass
    except Exception as e:                                  # pragma: no cover
        print(f"[Undo] push failed: {e}")


def can_undo() -> bool:
    with _lock:
        return bool(_stack)


def peek() -> str:
    """Label of the operation that `undo_last()` would reverse, or ''."""
    with _lock:
        return _stack[-1].label if _stack else ""


def history() -> list[str]:
    """Most recent first — used by the UI panel and the `undo` tool's list mode."""
    with _lock:
        return [e.label for e in reversed(_stack)]


def undo_last() -> str:
    """Reverse the most recent reversible operation.

    The entry is popped *before* running so a failing undo cannot be retried
    forever against a world that has already moved on (the file the user is
    trying to restore may have been deleted by something else since)."""
    with _lock:
        entry = _stack.pop() if _stack else None

    if entry is None:
        return ("There is nothing to undo. I only track things I changed myself — "
                "files I moved or wrote, and settings I adjusted.")

    try:
        detail = entry.undo() or ""
    except Exception as e:
        return f"Could not undo '{entry.label}': {e}"

    return f"Undone: {entry.label}." + (f" {detail}" if detail else "")


def clear() -> None:
    """Forget the stack. Called when the app shuts down so closures holding old
    file contents do not outlive the session."""
    with _lock:
        _stack.clear()
