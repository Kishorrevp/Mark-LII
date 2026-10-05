"""
core/verification.py — check that an action actually happened.

A function returning without an exception is not proof. "Moved: report.pdf →
Projects/" is a string the action wrote about itself. These verifiers look
at the real world afterwards: does the destination file exist, is the old
path gone, is the process running, is the downloaded file a real PDF.

Three outcomes, never two:
    VERIFIED       the post-condition was observed
    FAILED         the post-condition was checked and is NOT true
    UNVERIFIABLE   there is no reliable way to check (e.g. a WhatsApp message
                   typed via GUI automation — nothing reports delivery). The
                   result must then be reported to the user as *unconfirmed*,
                   never as done.

Only stdlib + optional psutil. Safe to import anywhere.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


class VStatus(str, enum.Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    UNVERIFIABLE = "UNVERIFIABLE"
    SKIPPED = "SKIPPED"


@dataclass
class VerificationResult:
    status: VStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (VStatus.VERIFIED, VStatus.SKIPPED)

    @property
    def failed(self) -> bool:
        return self.status == VStatus.FAILED

    def to_dict(self) -> dict:
        return {"status": self.status.value, "detail": self.detail}


def verified(detail: str = "") -> VerificationResult:
    return VerificationResult(VStatus.VERIFIED, detail)


def failed(detail: str) -> VerificationResult:
    return VerificationResult(VStatus.FAILED, detail)


def unverifiable(detail: str) -> VerificationResult:
    return VerificationResult(VStatus.UNVERIFIABLE, detail)


# ── Primitive checks ─────────────────────────────────────────────────────────

def path_exists(path, kind: str = "any") -> VerificationResult:
    if not path:
        return failed("No path was reported by the action.")
    p = Path(str(path))
    if not p.exists():
        return failed(f"'{p.name}' does not exist at {p.parent}.")
    if kind == "file" and not p.is_file():
        return failed(f"'{p.name}' exists but is not a file.")
    if kind == "dir" and not p.is_dir():
        return failed(f"'{p.name}' exists but is not a folder.")
    return verified(f"'{p.name}' is present in {p.parent.name or p.parent}.")


def path_absent(path) -> VerificationResult:
    if not path:
        return failed("No path to check.")
    p = Path(str(path))
    if p.exists():
        return failed(f"'{p.name}' is still at {p.parent}.")
    return verified(f"'{p.name}' is no longer at the old location.")


_MAGIC = {
    ".pdf": [b"%PDF"],
    ".png": [b"\x89PNG"],
    ".jpg": [b"\xff\xd8\xff"], ".jpeg": [b"\xff\xd8\xff"],
    ".gif": [b"GIF8"],
    ".zip": [b"PK\x03\x04", b"PK\x05\x06"],
    ".docx": [b"PK\x03\x04"], ".xlsx": [b"PK\x03\x04"], ".pptx": [b"PK\x03\x04"],
}


def file_valid(path, min_bytes: int = 1) -> VerificationResult:
    """Exists, is a file, non-empty, and — for known types — starts with the
    right magic bytes (catches an HTML error page saved as 'paper.pdf')."""
    r = path_exists(path, "file")
    if not r.ok:
        return r
    p = Path(str(path))
    try:
        size = p.stat().st_size
    except Exception as e:
        return failed(f"Cannot stat '{p.name}': {type(e).__name__}")
    if size < min_bytes:
        return failed(f"'{p.name}' is empty ({size} bytes).")
    magics = _MAGIC.get(p.suffix.lower())
    if magics:
        try:
            with open(p, "rb") as f:
                head = f.read(8)
        except Exception as e:
            return failed(f"Cannot read '{p.name}': {type(e).__name__}")
        if not any(head.startswith(m) for m in magics):
            return failed(f"'{p.name}' is not a valid {p.suffix.lstrip('.').upper()} file "
                          f"(content doesn't match the file type).")
    return verified(f"'{p.name}' exists and is a valid file ({size:,} bytes).")


def process_running(name: str, wait_s: float = 0.0, poll: float = 0.5) -> VerificationResult:
    """Best-effort: is a process whose name contains `name` running?"""
    try:
        import psutil
    except Exception:
        return unverifiable("psutil is not installed, so I cannot check running processes.")
    needle = (name or "").lower().replace(".exe", "").strip()
    if not needle:
        return unverifiable("No process name to check.")
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            for proc in psutil.process_iter(["name"]):
                pname = (proc.info.get("name") or "").lower()
                if needle in pname:
                    return verified(f"'{proc.info.get('name')}' is running.")
        except Exception:
            return unverifiable("Could not enumerate processes.")
        if time.monotonic() >= deadline:
            break
        time.sleep(poll)
    return unverifiable(f"No process matching '{name}' found — the app may use a different "
                        f"process name, so this is not proof it failed.")


def text_nonempty(text, what: str = "result") -> VerificationResult:
    if isinstance(text, str) and text.strip():
        return verified(f"{what} produced ({len(text)} chars).")
    return failed(f"The {what} came back empty.")


# Result-string heuristics for legacy actions that report failure in prose.
_FAIL_MARKERS = ("could not", "couldn't", "failed", "error", "not found", "access denied",
                 "permission denied", "unknown action", "no destination", "not installed",
                 "cannot", "protected directory")


def legacy_result_failed(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(t.startswith(m) or f" {m}" in t[:80] for m in _FAIL_MARKERS)


Verifier = Callable[[dict, "object"], VerificationResult]
