"""
core/audit.py — structured, redacted audit trail of what JARVIS did.

One JSON object per line in memory/audit/audit.jsonl:
    {"ts": "...", "event": "tool_call", "task_id": "...", "agent": "...",
     "tool": "...", "action": "...", "decision": "...", "risk": "...",
     "result": "ok|failed|pending|denied", "verification": "...", "detail": "..."}

Privacy rules applied to EVERY record before it is written:
  * secret_store.redact() — API keys, bearer tokens, password=... never land here
  * bodies are not logged: message text, file contents, code, typed text are
    replaced with "<N chars>" — the log says *that* a message was sent to Arun,
    not *what it said*
  * long strings truncated to 200 chars

Rotation: when the file passes 2 MB it is renamed to audit.1.jsonl (one
generation kept). Answers "what did you do?" via summarize_recent().
"""
from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from core.secret_store import redact

BODY_KEYS = {"content", "message_text", "text", "code", "value", "body", "draft", "summary",
             "instruction", "description_text", "keys", "password", "prompt"}
MAX_BYTES = 2_000_000


def _scrub(obj: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in list(obj.items())[:30]:
            if str(k).lower() in BODY_KEYS and isinstance(v, str):
                out[k] = f"<{len(v)} chars>"
            else:
                out[k] = _scrub(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, depth + 1) for v in list(obj)[:20]]
    if isinstance(obj, str):
        s = redact(obj)
        return s if len(s) <= 200 else s[:200] + "…"
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _scrub(str(obj), depth + 1)


class AuditLog:
    def __init__(self, path: Path | None = None, keep_in_memory: int = 300):
        if path is None:
            from core.secret_store import base_dir
            path = base_dir() / "memory" / "audit" / "audit.jsonl"
        self.path = Path(path)
        self._lock = threading.Lock()
        self._recent: deque = deque(maxlen=keep_in_memory)
        self._load_tail()

    def _load_tail(self) -> None:
        try:
            lines = self.path.read_bytes().decode("utf-8", "replace").splitlines()[-self._recent.maxlen:]
            for ln in lines:
                try:
                    self._recent.append(json.loads(ln))
                except Exception:
                    pass
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def record(self, event: str, **fields) -> dict:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event}
        rec.update(_scrub(fields))
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            self._recent.append(rec)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    old = self.path.with_name("audit.1.jsonl")
                    try:
                        if old.exists():
                            old.unlink()
                    except Exception:
                        pass
                    os.replace(self.path, old)
                with open(self.path, "ab") as f:
                    f.write((line + "\r\n").encode("utf-8"))
            except Exception:
                pass      # auditing must never break the action it describes
        return rec

    def recent(self, n: int = 20, event: str | None = None, task_id: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._recent)
        if event:
            items = [r for r in items if r.get("event") == event]
        if task_id:
            items = [r for r in items if r.get("task_id") == task_id]
        return items[-n:]

    def summarize_recent(self, n: int = 10) -> str:
        """Plain-language answer to "what did you do?" — most recent first."""
        items = [r for r in self.recent(200) if r.get("event") in ("tool_call", "task_finished",
                                                                    "permission")]
        if not items:
            return "I haven't carried out any actions yet this session."
        lines = []
        for r in reversed(items[-n:]):
            t = r.get("ts", "")[11:16]
            if r.get("event") == "task_finished":
                lines.append(f"{t} — task {r.get('task_id')} {str(r.get('result', '')).lower()}: "
                             f"{r.get('detail', '')}")
            elif r.get("event") == "permission":
                lines.append(f"{t} — asked permission for {r.get('tool')}"
                             f"{'.' + r['action'] if r.get('action') else ''}: {r.get('result')}")
            else:
                v = r.get("verification")
                vtxt = f", verification {v}" if v and v not in ("SKIPPED", None) else ""
                lines.append(f"{t} — {r.get('tool')}{'.' + r['action'] if r.get('action') else ''}"
                             f" → {r.get('result')}{vtxt}")
        return "Most recent first:\n" + "\n".join(lines)
