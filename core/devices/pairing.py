"""
core/devices/pairing.py — secure device pairing and message authentication
for the future phone companion (and any other remote device).

WHAT IS IMPLEMENTED (and tested)
    start_pairing()      → a 6-digit code shown on the laptop HUD, valid 5 min,
                           single use, max 5 wrong attempts
    complete_pairing()   → the device proves it saw the code; both sides get a
                           per-device 256-bit secret; the device gets an id
    sign() / verify()    → HMAC-SHA256 over (device_id, timestamp, nonce, body)
                           with ±60 s clock window and nonce replay protection
    revoke()             → the device can no longer authenticate

WHAT IS NOT (Phase 6)
    The transport (how the phone reaches the laptop — the dashboard's HTTPS
    websocket is the likely carrier), and the phone app itself.

STORAGE
    Paired-device secrets are in memory/devices.json (gitignored via
    memory/*.json). They must be readable unattended to verify requests, so
    the same caveat as .env applies: protected by file permissions, not by
    encryption at rest. The pairing code is never stored in plain text — only
    its SHA-256.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CODE_TTL = 300
MAX_ATTEMPTS = 5
CLOCK_SKEW = 60
NONCE_TTL = 120


class PairingError(RuntimeError):
    pass


@dataclass
class _PendingPair:
    code_hash: str
    expires: float
    attempts: int = 0


class PairingManager:
    def __init__(self, path: Path | None = None, clock=time.time, persist: bool = True):
        if path is None and persist:
            from core.secret_store import base_dir
            path = base_dir() / "memory" / "devices.json"
        self.path = Path(path) if path else None
        self._persist = persist and self.path is not None
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: Optional[_PendingPair] = None
        self._devices: dict[str, dict] = {}
        self._nonces: dict[str, float] = {}
        self._load()

    def _load(self):
        if not self._persist:
            return
        try:
            self._devices = json.loads(self.path.read_text(encoding="utf-8")).get("devices", {})
        except Exception:
            self._devices = {}

    def _save(self):
        if not self._persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps({"devices": self._devices}, indent=1).encode("utf-8"))
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    # -- pairing ----------------------------------------------------------------
    def start_pairing(self) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._lock:
            self._pending = _PendingPair(hashlib.sha256(code.encode()).hexdigest(),
                                         self._clock() + CODE_TTL)
        return code

    def complete_pairing(self, code: str, device_name: str, kind: str = "phone") -> dict:
        with self._lock:
            p = self._pending
            if p is None:
                raise PairingError("No pairing in progress.")
            if self._clock() > p.expires:
                self._pending = None
                raise PairingError("The pairing code expired.")
            p.attempts += 1
            if p.attempts > MAX_ATTEMPTS:
                self._pending = None
                raise PairingError("Too many wrong codes — start pairing again.")
            if not hmac.compare_digest(hashlib.sha256(str(code).strip().encode()).hexdigest(), p.code_hash):
                raise PairingError("Wrong pairing code.")
            self._pending = None
            device_id = f"{kind}-{secrets.token_hex(4)}"
            secret = secrets.token_hex(32)
            self._devices[device_id] = {"name": str(device_name)[:60], "kind": kind, "secret": secret,
                                        "paired_at": int(self._clock()), "revoked": False}
            self._save()
        return {"device_id": device_id, "secret": secret}

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            d = self._devices.get(device_id)
            if not d:
                return False
            d["revoked"] = True
            d["secret"] = ""
            self._save()
            return True

    def devices(self) -> list[dict]:
        return [{"device_id": k, "name": v["name"], "kind": v["kind"], "revoked": v["revoked"],
                 "paired_at": v["paired_at"]} for k, v in self._devices.items()]

    # -- message authentication ---------------------------------------------------
    @staticmethod
    def _mac(secret: str, device_id: str, ts: int, nonce: str, body: bytes) -> str:
        msg = b"\n".join([device_id.encode(), str(ts).encode(), nonce.encode(),
                          hashlib.sha256(body).hexdigest().encode()])
        return hmac.new(bytes.fromhex(secret), msg, hashlib.sha256).hexdigest()

    @staticmethod
    def sign(device_id: str, secret: str, body: bytes, ts: int | None = None,
             nonce: str | None = None) -> dict:
        ts = int(ts if ts is not None else time.time())
        nonce = nonce or secrets.token_hex(12)
        return {"device_id": device_id, "ts": ts, "nonce": nonce,
                "sig": PairingManager._mac(secret, device_id, ts, nonce, body)}

    def verify(self, headers: dict, body: bytes) -> str:
        """Returns the device_id if authentic; raises PairingError otherwise."""
        device_id = str(headers.get("device_id", ""))
        d = self._devices.get(device_id)
        if not d or d.get("revoked") or not d.get("secret"):
            raise PairingError("Unknown or revoked device.")
        try:
            ts = int(headers.get("ts"))
        except Exception:
            raise PairingError("Bad timestamp.")
        now = self._clock()
        if abs(now - ts) > CLOCK_SKEW:
            raise PairingError("Request timestamp outside the allowed window.")
        nonce = str(headers.get("nonce", ""))
        if len(nonce) < 8:
            raise PairingError("Missing nonce.")
        with self._lock:
            for n, t in list(self._nonces.items()):
                if now - t > NONCE_TTL:
                    del self._nonces[n]
            key = f"{device_id}:{nonce}"
            if key in self._nonces:
                raise PairingError("Replayed request.")
            expected = self._mac(d["secret"], device_id, ts, nonce, body)
            if not hmac.compare_digest(expected, str(headers.get("sig", ""))):
                raise PairingError("Bad signature.")
            self._nonces[key] = now
        return device_id
