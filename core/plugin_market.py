"""
core/plugin_market.py — "Do I have a way to do this? If not, can I get one?"

    need ("convert 100 dollars to rupees")
      └─► capability search
            ├─ a core tool or an installed plugin covers it → use it
            ├─ a plugin in the BUNDLED CATALOG covers it   → offer it: name, what it
            │                                                does, the permissions it
            │                                                declares → install ONLY on
            │                                                the user's HUD confirmation
            └─ nothing                                     → say so honestly

WHERE PLUGINS COME FROM
    Only plugins/available/ — files shipped with JARVIS, each pinned by SHA-256
    in plugins/available/catalog.lock.json. install() refuses a file whose hash
    does not match. There is deliberately NO download-from-the-internet
    marketplace: installing arbitrary remote code would hand an unknown author
    the same power as JARVIS itself, which the security rules forbid.
    Adding your own plugin is still one step: drop a .py into plugins/.

AFTER INSTALL
    The plugin is loaded immediately (no restart): validated by the normal
    plugin loader, registered as a tool behind the same permission engine /
    audit / verification as core tools, and its declared permissions
    ("network", "filesystem", …) are confirmed on first use and then
    remembered. The voice model sees the new tool after its next reconnect,
    which JARVIS triggers automatically; tasks can use it at once.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Optional

from core.tools.registry import ToolResult


def plugins_dir() -> Path:
    from core.secret_store import base_dir
    return base_dir() / "plugins"


def catalog_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "plugins" / "available"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def read_meta(path: Path) -> Optional[dict]:
    """PLUGIN dict read with ast.literal_eval — the file is NOT executed."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PLUGIN" for t in node.targets):
            try:
                meta = ast.literal_eval(node.value)
                return meta if isinstance(meta, dict) else None
            except Exception:
                return None
    return None


def build_lock(directory: Path | None = None) -> dict:
    """Release step: pin every bundled plugin's hash (run when the catalog changes)."""
    d = directory or catalog_dir()
    lock = {}
    for p in sorted(d.glob("*.py")):
        meta = read_meta(p)
        if meta and meta.get("name"):
            lock[meta["name"]] = {"file": p.name, "sha256": _sha(p), "version": meta.get("version", "")}
    (d / "catalog.lock.json").write_bytes(json.dumps(lock, indent=2).replace("\n", "\r\n").encode("utf-8"))
    return lock


def catalog() -> list[dict]:
    d = catalog_dir()
    try:
        lock = json.loads((d / "catalog.lock.json").read_text(encoding="utf-8"))
    except Exception:
        lock = {}
    out = []
    for p in sorted(d.glob("*.py")):
        meta = read_meta(p)
        if not meta or not meta.get("name"):
            continue
        pin = lock.get(meta["name"], {})
        out.append({"name": meta["name"], "file": p.name, "description": str(meta.get("description", "")),
                    "capabilities": list(meta.get("capabilities") or []),
                    "permissions": list(meta.get("permissions") or []), "risk": meta.get("risk", "medium"),
                    "version": meta.get("version", ""), "pinned": pin.get("sha256") == _sha(p),
                    "installed": (plugins_dir() / p.name).exists()})
    return out


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "to", "of", "for", "and", "in", "on", "my", "me", "can", "you", "please", "i",
         "want", "need", "how", "do", "is", "it", "this", "that", "with", "from", "into", "jarvis"}
_SYN = {"dollars": "currency", "rupees": "currency", "euro": "currency", "usd": "currency", "inr": "currency",
        "money": "currency", "km": "units", "miles": "units", "kg": "units", "celsius": "temperature",
        "fahrenheit": "temperature", "calculate": "math", "calculation": "math", "sum": "math",
        "multiply": "math", "percent": "percentage"}


def _terms(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(str(text or "").lower()):
        if w in _STOP or len(w) < 2:
            continue
        out.add(w)
        if w in _SYN:
            out.add(_SYN[w])
        if len(w) > 4 and w.endswith("s"):
            out.add(w[:-1])
    return out


def _score(need: set[str], name: str, desc: str, caps: list) -> float:
    cap_terms = set()
    for c in caps:
        cap_terms |= _terms(c)
    name_terms = _terms(name.replace("_", " ").replace(".", " "))
    desc_terms = _terms(desc)
    return 3 * len(need & cap_terms) + 2 * len(need & name_terms) + 0.5 * len(need & desc_terms)


class PluginMarket:
    def __init__(self, tool_registry=None, plugin_registry=None,
                 register_record: Optional[Callable] = None, on_installed: Optional[Callable[[str], None]] = None):
        self.tools = tool_registry
        self.plugins = plugin_registry
        self._register_record = register_record
        self._on_installed = on_installed

    # -- search ----------------------------------------------------------------
    def find(self, need: str, limit: int = 3) -> dict:
        n = _terms(need)
        have, avail = [], []
        if self.tools is not None:
            for spec in self.tools.list():
                s = _score(n, spec.name, spec.description, list(getattr(spec, "capabilities", []) or []))
                if s >= 3:
                    have.append((s, {"name": spec.name, "description": spec.description[:140],
                                     "source": getattr(spec, "source", "core")}))
        for c in catalog():
            if c["installed"]:
                continue
            s = _score(n, c["name"], c["description"], c["capabilities"])
            if s >= 3:
                avail.append((s, c))
        have.sort(key=lambda x: -x[0])
        avail.sort(key=lambda x: -x[0])
        return {"installed": [h for _, h in have[:limit]], "available": [a for _, a in avail[:limit]]}

    # -- install ---------------------------------------------------------------
    def install(self, name: str) -> ToolResult:
        entry = next((c for c in catalog() if c["name"] == name), None)
        if entry is None:
            return ToolResult.failure(f"There is no plugin called '{name}' in my catalog.")
        src = catalog_dir() / entry["file"]
        if not entry["pinned"]:
            return ToolResult.failure(f"'{name}' does not match its pinned checksum — I won't install it.")
        dst_dir = plugins_dir()
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / entry["file"]
        if self.tools is not None and self.tools.has(name):
            return ToolResult(True, {"name": name, "path": str(dst), "already": True},
                              f"The {name} plugin is already installed.")
        if not dst.exists():
            tmp = dst.with_name(dst.name + ".part")
            shutil.copyfile(src, tmp)
            if _sha(tmp) != _sha(src):
                tmp.unlink(missing_ok=True)
                return ToolResult.failure("The copy did not match the original — install aborted.")
            os.replace(tmp, dst)
        loaded = self._load(dst)
        if isinstance(loaded, str):
            return ToolResult.failure(f"Installed the file, but it failed to load: {loaded}")
        if self._on_installed:
            try:
                self._on_installed(name)
            except Exception:
                pass
        perms = entry["permissions"]
        return ToolResult(True, {"name": name, "path": str(dst), "permissions": perms},
                          f"Installed the {name} plugin" + (f" (it will ask once for: {', '.join(perms)})"
                                                            if perms else "") + ". It's ready to use.")

    def _load(self, path: Path):
        import importlib.util
        import sys
        from core.plugin_loader import _validate
        mod_name = f"plugins.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except Exception as e:
            sys.modules.pop(mod_name, None)
            return f"import failed ({type(e).__name__})"
        rec = _validate(module, path.name)
        if not rec.valid:
            return rec.error
        if self.tools is not None and self.tools.has(rec.name):
            return f"name '{rec.name}' is already taken"
        if rec.on_load is not None:
            try:
                rec.on_load(dict(rec.config))
            except Exception as e:
                return f"on_load failed ({type(e).__name__})"
        if self.plugins is not None and hasattr(self.plugins, "add"):
            self.plugins.add(rec)
        if self._register_record is not None and self.tools is not None:
            self._register_record(self.tools, self.plugins, rec)
        return rec


# ── tools (bound by JarvisCore) ──────────────────────────────────────────────
_MARKET: dict = {"m": None}


def bind(market: PluginMarket) -> None:
    _MARKET["m"] = market


def _m() -> PluginMarket:
    return _MARKET["m"] or PluginMarket()


def t_plugin_find(args: dict, ctx) -> ToolResult:
    need = str(args.get("need") or "").strip()
    if not need:
        return ToolResult.failure("What do you need to be able to do?")
    r = _m().find(need)
    if r["installed"]:
        names = ", ".join(h["name"] for h in r["installed"])
        return ToolResult(True, {**r, "verdict": "have"}, f"I can already do that with: {names}.")
    if r["available"]:
        c = r["available"][0]
        perms = f" It needs: {', '.join(c['permissions'])}." if c["permissions"] else " It needs no special access."
        return ToolResult(True, {**r, "verdict": "installable", "suggest": c["name"]},
                          f"I don't have that yet, but my plugin catalog has '{c['name']}': {c['description'][:160]}"
                          f"{perms} Shall I install it? (I'll ask for confirmation on the HUD.)")
    return ToolResult(True, {**r, "verdict": "none"},
                      "I don't have a tool or plugin for that, and there's none in my catalog. It could be "
                      "added as a plugin file in the plugins folder.")


def t_plugin_install(args: dict, ctx) -> ToolResult:
    return _m().install(str(args.get("name") or "").strip())


def register(reg) -> None:
    from core.permissions import RiskLevel
    from core.tools.registry import ToolSpec
    from core import verification as V
    STR = {"type": "string"}

    def _perm_text(a):
        c = next((x for x in catalog() if x["name"] == a.get("name")), None)
        p = ", ".join(c["permissions"]) if c and c["permissions"] else "no special access"
        return f"Install plugin '{a.get('name', '?')}' ({p})"

    reg.register(ToolSpec("plugin.find", "Check whether JARVIS can already do something, or which catalog plugin "
                          "would add that ability.", t_plugin_find,
                          {"type": "object", "properties": {"need": STR}, "required": ["need"]},
                          {"verdict": "str"}, agent="plugin", risk=RiskLevel.SAFE, retry_safe=True))
    reg.register(ToolSpec("plugin.install", "Install a plugin from JARVIS's bundled, checksum-pinned catalog "
                          "(always asks first).", t_plugin_install,
                          {"type": "object", "properties": {"name": STR}, "required": ["name"]},
                          {"name": "str"}, agent="plugin", risk=RiskLevel.HIGH_RISK, requires_confirmation=True,
                          confirm_text=_perm_text,
                          verify=lambda a, r: (V.path_exists(r.data.get("path"), "file") if r.ok
                                               else V.failed(r.error))))
