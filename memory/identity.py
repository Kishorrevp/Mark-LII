"""
memory/identity.py — the PERSONAL MODEL: who the user is, as JARVIS knows it.

    user          name, city, job, languages          ← long_term.json "identity"
    preferences   likes, style, habits                ← long_term.json "preferences"
    people        family, friends, colleagues         ← long_term.json "relationships" + people
    goals         plans, wishes                       ← long_term.json "wishes" + goals
    projects      name → path, description, stack,    ← identity.json (structured) +
                  last_active                           long_term.json "projects"
    devices       this PC + paired devices            ← device_context.json / pairing
    routines      saved workflows (procedural)        ← core/workflows.py
    frequent_apps usage counts                        ← identity.json (observed)

identity.json only holds what needs STRUCTURE (project paths, app usage,
devices). Plain facts stay in long_term.json so the existing save/recall/forget
tools and the dashboard keep working unchanged.

WHAT IS LEARNED WITHOUT ASKING — and why that is acceptable
    * app usage counts (name + count + last used) — so "open my usual editor"
      and the context engine can prefer what you actually use. No window
      titles, no content. Turn off with config "learn_usage": false; wipe
      with forget("frequent_apps").
    * a project's last_active time when JARVIS itself worked in that folder.
Everything else is either said by the user (save_memory, through
memory/policy.py) or approved by the user (projects registered by a task
the user started, workflows saved on a yes).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

SECTIONS = ("user", "preferences", "people", "goals", "projects", "devices", "routines", "frequent_apps")
_LT_MAP = {"identity": "user", "preferences": "preferences", "relationships": "people",
           "wishes": "goals", "projects": "projects", "notes": "notes"}
_WIN_PATH = re.compile(r"([A-Za-z]:[\\/][^\"'<>|\n]+|~[\\/][^\"'<>|\n]+|/(?:home|Users)/[^\"'<>|\n]+)")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _val(entry) -> str:
    return str(entry.get("value") if isinstance(entry, dict) else entry)


class PersonalModel:
    def __init__(self, base_dir: Path | None = None, workflows=None, memory_loader=None):
        if base_dir is None:
            from core.secret_store import base_dir as _bd
            base_dir = _bd()
        self.base = Path(base_dir)
        self.path = self.base / "memory" / "identity.json"
        self.workflows = workflows
        self._load_lt = memory_loader
        self._lock = threading.Lock()

    # -- storage -------------------------------------------------------------------
    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _write(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".identity.", dir=str(self.path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(d, indent=2, ensure_ascii=False).replace("\n", "\r\n"))
        os.replace(tmp, self.path)

    def _lt(self) -> dict:
        try:
            if self._load_lt:
                return self._load_lt() or {}
            from memory.memory_manager import load_memory
            return load_memory() or {}
        except Exception:
            return {}

    def _learn_usage(self) -> bool:
        try:
            from core.secret_store import legacy_config_path
            cfg = json.loads(legacy_config_path().read_text(encoding="utf-8"))
            return bool(cfg.get("learn_usage", True))
        except Exception:
            return True

    # -- projects --------------------------------------------------------------------
    def register_project(self, name: str, path: str | None = None, description: str = "",
                         stack: list[str] | None = None, source: str = "user",
                         aliases: list[str] | None = None) -> dict:
        name = str(name or "").strip()[:60]
        if not name:
            raise ValueError("project needs a name")
        with self._lock:
            d = self._read()
            projects = d.setdefault("projects", {})
            key = next((k for k in projects if k.lower() == name.lower()), name)
            p = projects.setdefault(key, {"added": _now(), "source": source})
            if path:
                p["path"] = str(path)
            if description:
                p["description"] = str(description)[:300]
            if stack:
                p["stack"] = sorted({str(s)[:30] for s in stack})[:12]
            if aliases:
                p["aliases"] = sorted({*p.get("aliases", []), *[str(a).lower()[:40] for a in aliases]})[:8]
            p["last_active"] = _now()
            self._write(d)
            return {"name": key, **p}

    def touch_project(self, name: str) -> None:
        with self._lock:
            d = self._read()
            p = (d.get("projects") or {}).get(name)
            if p is not None:
                p["last_active"] = _now()
                self._write(d)

    def projects(self) -> dict[str, dict]:
        """Structured registry merged with plain long-term 'projects' facts."""
        out: dict[str, dict] = {k: dict(v) for k, v in (self._read().get("projects") or {}).items()}
        for key, entry in (self._lt().get("projects") or {}).items():
            text = _val(entry)
            name = key.replace("_", " ").strip()
            if any(k.lower() == name.lower() for k in out):
                continue
            item = {"description": text[:300], "source": "memory"}
            m = _WIN_PATH.search(text)
            if m:
                item["path"] = m.group(1).strip().rstrip(".")
            out[name] = item
        return out

    def match_project(self, text_or_path: str) -> Optional[str]:
        """Which known project does this text / path refer to?"""
        s = str(text_or_path or "")
        if not s:
            return None
        low = s.lower().replace("\\", "/")
        best, best_len = None, 0
        for name, p in self.projects().items():
            path = str(p.get("path") or "").lower().replace("\\", "/").rstrip("/")
            if path and (low.startswith(path) or path in low) and len(path) > best_len:
                best, best_len = name, len(path)
                continue
            for alias in [name.lower(), *p.get("aliases", [])]:
                if len(alias) >= 3 and re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", low) \
                        and len(alias) > best_len:
                    best, best_len = name, len(alias)
            base = os.path.basename(path) if path else ""
            if base and len(base) >= 3 and re.search(rf"(?<![\w]){re.escape(base)}(?![\w])", low) \
                    and len(base) > best_len:
                best, best_len = name, len(base)
        return best

    # -- apps --------------------------------------------------------------------------
    def note_app(self, app: str) -> None:
        app = re.sub(r"\s+", " ", str(app or "")).strip().lower()[:40]
        if not app or not self._learn_usage():
            return
        with self._lock:
            d = self._read()
            apps = d.setdefault("frequent_apps", {})
            a = apps.setdefault(app, {"count": 0})
            a["count"] = int(a.get("count", 0)) + 1
            a["last"] = _now()
            if len(apps) > 60:            # keep the 60 most used
                for k in sorted(apps, key=lambda k: apps[k].get("count", 0))[: len(apps) - 60]:
                    apps.pop(k, None)
            self._write(d)

    def frequent_apps(self, n: int = 5) -> list[str]:
        apps = self._read().get("frequent_apps") or {}
        return [k for k, _ in sorted(apps.items(), key=lambda kv: (-kv[1].get("count", 0),
                                                                   kv[1].get("last", "")))][:n]

    # -- generic sections ------------------------------------------------------------
    def set(self, section: str, key: str, value: Any) -> None:
        if section not in ("devices", "routines", "goals", "people"):
            raise ValueError(f"use save_memory for {section}")
        with self._lock:
            d = self._read()
            d.setdefault(section, {})[str(key)[:60]] = {"value": value, "updated": _now()}
            self._write(d)

    def forget(self, section: str, key: str | None = None) -> str:
        with self._lock:
            d = self._read()
            if section not in d:
                return f"Nothing stored under {section}."
            if key is None:
                d.pop(section, None)
                self._write(d)
                return f"Cleared {section}."
            items = d.get(section) or {}
            hit = next((k for k in items if k.lower() == key.lower()), None)
            if hit is None:
                return f"No '{key}' under {section}."
            items.pop(hit)
            self._write(d)
            return f"Forgot {section}/{hit}."

    # -- views ---------------------------------------------------------------------------
    def profile(self) -> dict:
        lt = self._lt()
        own = self._read()
        out: dict[str, Any] = {s: {} for s in SECTIONS}
        for cat, section in _LT_MAP.items():
            for k, v in (lt.get(cat) or {}).items():
                if section == "projects":
                    continue
                out.setdefault(section, {})[k] = _val(v)
        for section in ("people", "goals", "devices", "routines"):
            for k, v in (own.get(section) or {}).items():
                out[section][k] = _val(v)
        out["projects"] = self.projects()
        out["frequent_apps"] = self.frequent_apps(8)
        if self.workflows is not None:
            try:
                out["routines"].update({w["name"]: " → ".join(s.get("tool", "?") for s in w.get("steps", []))
                                        for w in self.workflows.list()})
            except Exception:
                pass
        return out

    def facts(self) -> list[tuple[str, str, str]]:
        """Flat (section, key, value) list for relevance scoring."""
        out = []
        for section, items in self.profile().items():
            if isinstance(items, dict):
                for k, v in items.items():
                    if isinstance(v, dict):
                        v = "; ".join(f"{kk}: {vv}" for kk, vv in v.items()
                                      if kk in ("path", "description", "stack", "last_active"))
                    out.append((section, str(k), str(v)))
            elif isinstance(items, list) and items:
                out.append((section, section, ", ".join(map(str, items))))
        return out

    def user_name(self) -> str:
        u = self._lt().get("identity") or {}
        for k in ("name", "preferred_name", "nickname"):
            if k in u:
                return _val(u[k])
        return ""

    def summary_line(self, max_projects: int = 3) -> str:
        bits = []
        name = self.user_name()
        if name:
            bits.append(f"User: {name}")
        pr = sorted(self.projects().items(), key=lambda kv: kv[1].get("last_active", ""), reverse=True)
        if pr:
            bits.append("Projects: " + ", ".join(k for k, _ in pr[:max_projects]))
        apps = self.frequent_apps(4)
        if apps:
            bits.append("Usual apps: " + ", ".join(apps))
        return " · ".join(bits)
