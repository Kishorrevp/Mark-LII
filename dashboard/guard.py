"""
dashboard/guard.py — the dashboard's front door (Phase 7, "lock it down").

The phone dashboard has to listen on the LAN (the phone is another device),
so "bind 127.0.0.1 only" is not an option. What protects it instead:

  HOST     the Host header must be this PC: localhost / 127.0.0.1 / [::1] /
           its LAN address (with our port). Anything else is refused — that is
           what stops a DNS-rebinding page from talking to the dashboard.
  ORIGIN   a state-changing request (POST/PUT/PATCH/DELETE) that carries an
           Origin must come from one of those same hosts; a browser page on
           another site cannot drive the dashboard. Requests without an Origin
           (the app's own fetches on some browsers, tools) still need the
           bearer token every /api route checks.
  BODY     JSON bodies are capped (64 KB); only /api/upload may be large.
  LOGIN    five wrong PINs from one address within ten minutes → that address
           waits ten minutes (the pairing key is also one-time and expires).
  HEADERS  nosniff, no framing (X-Frame-Options DENY + frame-ancestors 'none'),
           no referrer, same-origin resource policy on every response.

Pure functions + a small limiter; dashboard/server.py installs them as
middleware. Tested by real execution.
"""
from __future__ import annotations

import time
from typing import Callable, Optional
from urllib.parse import urlparse

JSON_BODY_MAX = 64 * 1024
LOGIN_PATHS = ("/login", "/api/device-login")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}
_STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


def allowed_hosts(lan_ip: str, ports: tuple | list, extra: tuple | list = ()) -> set[str]:
    names = {"localhost", "127.0.0.1", "[::1]"}
    if lan_ip:
        names.add(lan_ip)
    names |= {str(x).strip().lower() for x in extra if str(x).strip()}
    out = set(names)
    for n in names:
        for p in ports:
            out.add(f"{n}:{p}")
    return out


def _host_only(value: str) -> str:
    return str(value or "").strip().lower()


def check(method: str, path: str, headers: dict, hosts: set[str], body_len: Optional[int] = None,
          upload_max: int = 500 * 1024 * 1024) -> tuple[bool, int, str]:
    """→ (ok, http status if refused, reason)."""
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    host = _host_only(h.get("host", ""))
    if host not in hosts:
        return False, 421, "unknown Host"
    if method.upper() in _STATE_CHANGING:
        origin = h.get("origin", "")
        if origin and origin != "null":
            o = urlparse(origin)
            if o.scheme not in ("http", "https") or _host_only(o.netloc) not in hosts:
                return False, 403, "cross-site request refused"
        elif origin == "null":
            return False, 403, "opaque origin refused"
    if body_len is not None:
        limit = upload_max if path.startswith("/api/upload") else JSON_BODY_MAX
        if body_len > limit:
            return False, 413, "request body too large"
    return True, 0, ""


class LoginLimiter:
    def __init__(self, max_fails: int = 5, window_s: float = 600.0, lock_s: float = 600.0,
                 clock: Callable[[], float] = time.monotonic):
        self.max_fails, self.window_s, self.lock_s = max_fails, window_s, lock_s
        self._clock = clock
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}

    def locked(self, ip: str) -> bool:
        until = self._locked.get(ip, 0.0)
        if until and self._clock() < until:
            return True
        self._locked.pop(ip, None)
        return False

    def fail(self, ip: str) -> None:
        now = self._clock()
        xs = [t for t in self._fails.get(ip, []) if now - t < self.window_s] + [now]
        self._fails[ip] = xs
        if len(xs) >= self.max_fails:
            self._locked[ip] = now + self.lock_s
            self._fails[ip] = []

    def ok(self, ip: str) -> None:
        self._fails.pop(ip, None)
