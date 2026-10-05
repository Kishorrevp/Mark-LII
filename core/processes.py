"""
core/processes.py — the background processes JARVIS itself starts.

Scope is deliberately narrow: JARVIS does NOT get a general shell. The only
processes it can start are built from fixed recipes in this file (today: a
local preview web server for a site inside a JARVIS workspace folder, bound
to 127.0.0.1). The model can ask to start a recipe, list what is running,
read a process's log, or stop one — and it can only ever stop processes it
started itself (pid AND creation time are checked, so a recycled pid that now
belongs to something else is never touched).

Every process is named, logged to a file, and remembered across a JARVIS
restart in memory/processes.json.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from core.tools.registry import ToolResult

IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


@dataclass
class ManagedProcess:
    name: str
    recipe: str
    pid: int
    created: float                    # psutil create_time — identity check
    cwd: str
    log: str
    port: int = 0
    url: str = ""
    started_at: float = 0.0


def workspace_roots() -> list[Path]:
    home = Path.home()
    docs = home / "Documents"
    roots = [docs / "JARVIS Projects", docs / "JARVIS Research", docs / "JARVIS Workspace",
             home / "Desktop" / "JarvisProjects"]
    try:
        from core import research
        roots.append(Path(research.documents_dir()))
    except Exception:
        pass
    return roots


def in_workspace(path, extra_roots: Optional[list] = None) -> bool:
    try:
        p = Path(path).expanduser().resolve()
    except Exception:
        return False
    for r in list(workspace_roots()) + list(extra_roots or []):
        try:
            rr = Path(r).resolve()
        except Exception:
            continue
        if p == rr or rr in p.parents:
            return True
    return False


def free_port(first: int = 8765, last: int = 8799) -> int:
    for port in range(first, last + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port between {first} and {last}.")


def wait_port(port: int, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


class ProcessManager:
    def __init__(self, state_path: Optional[Path] = None, persist: bool = True,
                 extra_roots: Optional[list] = None):
        if state_path is None and persist:
            from core.secret_store import base_dir
            state_path = base_dir() / "memory" / "processes.json"
        self.state_path = Path(state_path) if state_path else None
        self.persist = persist and self.state_path is not None
        self.extra_roots = list(extra_roots or [])
        self._procs: dict[str, ManagedProcess] = {}
        self._popen: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()
        self._load()

    # -- persistence -------------------------------------------------------------
    def _load(self) -> None:
        if not self.persist or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            for d in data.get("processes", []):
                mp = ManagedProcess(**d)
                if self._alive(mp):
                    self._procs[mp.name] = mp
        except Exception:
            pass

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps({"processes": [asdict(p) for p in self._procs.values()]}, indent=1)
            self.state_path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        except Exception:
            pass

    # -- identity ----------------------------------------------------------------
    @staticmethod
    def _alive(mp: ManagedProcess) -> bool:
        try:
            import psutil
            p = psutil.Process(mp.pid)
            return abs(p.create_time() - mp.created) < 1.0 and p.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    # -- recipes -----------------------------------------------------------------
    def start_preview(self, folder: str, name: str = "", port: int = 0, first: int = 8765,
                      last: int = 8799) -> ManagedProcess:
        """Serve a folder over http://127.0.0.1:<port>/ (Python's http.server)."""
        d = Path(folder).expanduser().resolve()
        if not d.is_dir():
            raise FileNotFoundError(f"No folder at {d}")
        if not in_workspace(d, self.extra_roots):
            raise PermissionError("I only serve folders inside a JARVIS workspace (Documents/JARVIS Projects, "
                                  "JARVIS Workspace, JARVIS Research or Desktop/JarvisProjects).")
        name = name or f"preview-{d.name}"[:60]
        with self._lock:
            old = self._procs.get(name)
            if old and self._alive(old):
                return old
        port = port or free_port(first, last)
        argv = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(d)]
        return self._spawn(name, "preview", argv, d, port, f"http://127.0.0.1:{port}/")

    def _spawn(self, name: str, recipe: str, argv: list, cwd: Path, port: int, url: str) -> ManagedProcess:
        import psutil
        logs = (self.state_path.parent if self.state_path else Path(os.path.expanduser("~"))) / "process_logs"
        if not self.persist:
            import tempfile
            logs = Path(tempfile.gettempdir()) / "jarvis_process_logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / f"{name}.log"
        fh = open(log, "wb")
        flags = (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if IS_WIN else 0
        p = subprocess.Popen(argv, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             creationflags=flags, env=dict(os.environ, PYTHONUNBUFFERED="1"))
        fh.close()
        mp = ManagedProcess(name=name, recipe=recipe, pid=p.pid, created=psutil.Process(p.pid).create_time(),
                            cwd=str(cwd), log=str(log), port=port, url=url, started_at=time.time())
        with self._lock:
            self._procs[name] = mp
            self._popen[name] = p
            self._save()
        if port and not wait_port(port, 10.0):
            self.stop(name)
            raise RuntimeError(f"The {recipe} server did not start (log: {self.logs(name, 10) or 'empty'}).")
        return mp

    # -- control -----------------------------------------------------------------
    def list(self) -> list[ManagedProcess]:
        with self._lock:
            for n in [n for n, mp in self._procs.items() if not self._alive(mp)]:
                self._procs.pop(n, None)
                self._popen.pop(n, None)
            self._save()
            return list(self._procs.values())

    def get(self, name: str) -> Optional[ManagedProcess]:
        with self._lock:
            mp = self._procs.get(name)
            if mp is None:                       # allow a unique prefix / folder name
                hits = [m for n, m in self._procs.items() if name.lower() in n.lower()]
                mp = hits[0] if len(hits) == 1 else None
            return mp

    def stop(self, name: str) -> bool:
        mp = self.get(name)
        if mp is None:
            return False
        try:
            import psutil
            if self._alive(mp):
                proc = psutil.Process(mp.pid)
                kids = proc.children(recursive=True)
                for k in kids:
                    try:
                        k.terminate()
                    except Exception:
                        pass
                proc.terminate()
                gone, alive = psutil.wait_procs([proc] + kids, timeout=3)
                for a in alive:
                    try:
                        a.kill()
                    except Exception:
                        pass
        except Exception:
            pass
        with self._lock:
            self._procs.pop(mp.name, None)
            p = self._popen.pop(mp.name, None)
            self._save()
        if p is not None:
            try:
                p.wait(2)
            except Exception:
                pass
        return True

    def stop_all(self, reason: str = "") -> str:
        names = [m.name for m in self.list()]
        for n in names:
            self.stop(n)
        return f"{len(names)} background process(es) stopped" if names else "no background processes"

    def logs(self, name: str, tail: int = 40) -> str:
        mp = self.get(name)
        if mp is None:
            return ""
        try:
            lines = Path(mp.log).read_bytes().decode("utf-8", "replace").splitlines()
        except Exception:
            return ""
        from core.secret_store import redact
        return redact("\n".join(lines[-max(1, int(tail)):]))


_PM: Optional[ProcessManager] = None
_PLOCK = threading.Lock()


def get_manager() -> ProcessManager:
    global _PM
    with _PLOCK:
        if _PM is None:
            _PM = ProcessManager()
            try:
                from core.safety import kill_switch
                kill_switch().register("background processes", _PM.stop_all)
            except Exception:
                pass
        return _PM


# ── tools ────────────────────────────────────────────────────────────────────

def t_process_list(args: dict, ctx) -> ToolResult:
    procs = get_manager().list()
    if not procs:
        return ToolResult(True, {"processes": []}, "No background processes are running.")
    msg = "; ".join(f"{p.name} ({p.recipe}{', ' + p.url if p.url else ''})" for p in procs)
    return ToolResult(True, {"processes": [asdict(p) for p in procs]}, f"Running: {msg}")


def t_process_stop(args: dict, ctx) -> ToolResult:
    name = str(args.get("name") or "").strip()
    pm = get_manager()
    if name.lower() in ("all", "*", "everything"):
        return ToolResult(True, {}, pm.stop_all().capitalize() + ".")
    mp = pm.get(name)
    if mp is None:
        return ToolResult.failure(f"I haven't started a process called '{name}'.")
    pm.stop(mp.name)
    return ToolResult(True, {"name": mp.name}, f"Stopped {mp.name}.")


def t_process_logs(args: dict, ctx) -> ToolResult:
    name = str(args.get("name") or "")
    text = get_manager().logs(name, int(args.get("tail") or 40))
    if not text:
        return ToolResult.failure(f"No log for '{name}'.")
    return ToolResult(True, {"log": text}, text[-1500:])


def register(reg) -> None:
    from core.permissions import RiskLevel
    from core.tools.registry import ToolSpec
    STR = {"type": "string"}
    reg.register(ToolSpec("process.list", "List background processes JARVIS started (preview servers).",
                          t_process_list, {"type": "object", "properties": {}, "required": []},
                          agent="webdev", risk=RiskLevel.SAFE, retry_safe=True, timeout=15))
    reg.register(ToolSpec("process.stop", "Stop a background process JARVIS started (by name, or 'all').",
                          t_process_stop, {"type": "object", "properties": {"name": STR}, "required": ["name"]},
                          agent="webdev", risk=RiskLevel.LOW_RISK, timeout=20))
    reg.register(ToolSpec("process.logs", "Show the last lines of a JARVIS background process's log.",
                          t_process_logs, {"type": "object", "properties": {"name": STR, "tail": {"type": "integer"}},
                                           "required": ["name"]},
                          agent="webdev", risk=RiskLevel.SAFE, retry_safe=True, timeout=15))
