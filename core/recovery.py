"""
core/recovery.py — classify a failure, then decide how to recover.

    classify(error) → Failure(kind, retry, replan, ask_user, hint, backoff)

Every failed step is classified before the orchestrator decides what to do:

    kind              retry   replan  what the user hears
    ─────────────────────────────────────────────────────────────────────────
    network           yes     yes     "the network looks unavailable"
    rate_limit        yes*    yes     "the AI service is busy / over its free quota"
    quota_exhausted   no      yes     "today's free AI quota for that model is used up"
    timeout           yes     yes     "it took too long"
    not_found         no      yes     "I couldn't find that file / app / page"
    ui_not_found      yes     yes     "the button/window I needed wasn't on screen"
    access_denied     no      yes     "Windows refused access (permissions / file in use)"
    invalid_input     no      no      "something in the request doesn't look right" → ask
    dependency        no      yes     "a required program/package isn't installed"
    auth              no      no      "the API key was rejected"
    policy            no      no      blocked by JARVIS's safety rules — never routed around
    declined          no      no      you said no — never routed around
    cancelled         no      no      stopped by you
    internal          no      yes     a bug in JARVIS
    unknown           as the tool says   yes

    * rate limits are retried only after the server's back-off.

Retries are still bounded by the tool's own retry-safety (a non-idempotent
action such as "move a file" is never blindly repeated); classification can
only REMOVE retries that cannot help, never add unsafe ones.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Kind(str, Enum):
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    UI_NOT_FOUND = "ui_not_found"
    ACCESS_DENIED = "access_denied"
    INVALID_INPUT = "invalid_input"
    DEPENDENCY = "dependency"
    AUTH = "auth"
    POLICY = "policy"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Failure:
    kind: Kind
    retry: bool            # repeating the same call can plausibly help
    replan: bool           # a different approach can plausibly help
    ask_user: bool         # the user must supply something
    hint: str              # plain-language cause for the final message
    backoff: float = 0.0   # extra seconds before a retry

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "retry": self.retry, "replan": self.replan,
                "ask_user": self.ask_user, "hint": self.hint}


_RULES: list[tuple[Kind, re.Pattern]] = [
    (Kind.CANCELLED, re.compile(r"\b(cancel+ed|emergency stop|stopped by (?:the )?user|^stopped\.?$)", re.I)),
    (Kind.DECLINED, re.compile(r"\b(declined|you said no|permission (?:declined|expired)|not approved)\b", re.I)),
    (Kind.POLICY, re.compile(r"\b(blocked by (?:policy|jarvis)|not allowed|policy|denied by rule|"
                             r"refus(?:e|ed) to|forbidden action|is blocked)\b", re.I)),
    (Kind.AUTH, re.compile(r"(api key not valid|authentication failed|unauthori[sz]ed|\b401\b|invalid api key|"
                           r"PERMISSION_DENIED.*key|check the api key)", re.I)),
    (Kind.QUOTA_EXHAUSTED, re.compile(r"(PerDay|per[ _-]day|daily (?:quota|limit)|limit:\s*0\b|quota.{0,40}exhaust)",
                                      re.I)),
    (Kind.RATE_LIMIT, re.compile(r"(\b429\b|rate[ _-]?limit|RESOURCE_EXHAUSTED|too many requests|quota|"
                                 r"free-tier limit)", re.I)),
    (Kind.TIMEOUT, re.compile(r"(timed out|time ?out|deadline exceeded|took too long)", re.I)),
    (Kind.NETWORK, re.compile(r"(connection(?:error| refused| reset| aborted)|failed to establish|"
                              r"name resolution|getaddrinfo|no internet|network (?:is )?unreachable|"
                              r"\b50[234]\b|UNAVAILABLE|temporarily unavailable|ssl ?error|max retries exceeded)",
                              re.I)),
    (Kind.DEPENDENCY, re.compile(r"(no module named|modulenotfounderror|is not installed|pip install|"
                                 r"not recognized as an internal or external command|command not found|"
                                 r"executable .{0,40} not found|playwright.{0,40}(?:missing|install))", re.I)),
    (Kind.ACCESS_DENIED, re.compile(r"(access is denied|permission denied|permissionerror|winerror 5\b|"
                                    r"being used by another process|read-only file system|operation not permitted)",
                                    re.I)),
    (Kind.UI_NOT_FOUND, re.compile(r"(element not found|couldn'?t find the (?:button|field|window|element)|"
                                   r"no such (?:element|window)|not visible on screen|window .{0,30} not found)",
                                   re.I)),
    (Kind.NOT_FOUND, re.compile(r"(no such file|file ?not ?found|filenotfounderror|does not exist|"
                                r"couldn'?t find|could not find|not found|\b404\b|no match)", re.I)),
    (Kind.INVALID_INPUT, re.compile(r"(invalid (?:argument|input|value|path)|missing required|should be (?:a |an )?"
                                    r"(?:string|integer|number)|must be one of|valueerror|bad request|\b400\b)",
                                    re.I)),
    (Kind.INTERNAL, re.compile(r"(internal error|traceback|attributeerror|typeerror|keyerror|indexerror|"
                               r"nameerror|raised \w+error)", re.I)),
]

_TABLE = {
    Kind.NETWORK: (True, True, False, "the network looks unavailable", 2.0),
    Kind.RATE_LIMIT: (True, True, False, "the AI service is busy or over its per-minute free limit", 5.0),
    Kind.QUOTA_EXHAUSTED: (False, True, False, "today's free AI quota for that model is used up", 0.0),
    Kind.TIMEOUT: (True, True, False, "it took too long", 1.0),
    Kind.NOT_FOUND: (False, True, True, "I couldn't find what it needed (file, app or page)", 0.0),
    Kind.UI_NOT_FOUND: (True, True, False, "the window or button I needed wasn't on screen", 1.5),
    Kind.ACCESS_DENIED: (False, True, False, "Windows refused access (permissions, or the file is in use)", 0.0),
    Kind.INVALID_INPUT: (False, False, True, "something in the request doesn't look right", 0.0),
    Kind.DEPENDENCY: (False, True, False, "a required program or package isn't installed", 0.0),
    Kind.AUTH: (False, False, False, "the API key was rejected", 0.0),
    Kind.POLICY: (False, False, False, "JARVIS's safety rules block that action", 0.0),
    Kind.DECLINED: (False, False, False, "you declined it", 0.0),
    Kind.CANCELLED: (False, False, False, "it was stopped", 0.0),
    Kind.INTERNAL: (False, True, False, "an internal error in JARVIS", 0.0),
    Kind.UNKNOWN: (True, True, False, "", 0.0),
}


def classify(error, *, retryable_hint: bool | None = None) -> Failure:
    """error: str, Exception, or a ToolResult-like object with .error."""
    if hasattr(error, "error") and not isinstance(error, BaseException):
        text = str(getattr(error, "error", "") or "")
        if retryable_hint is None:
            retryable_hint = bool(getattr(error, "retryable", False))
    else:
        text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error or "")
    kind = Kind.UNKNOWN
    for k, rx in _RULES:
        if rx.search(text):
            kind = k
            break
    retry, replan, ask, hint, backoff = _TABLE[kind]
    if kind == Kind.UNKNOWN:
        retry = bool(retryable_hint)
    return Failure(kind, retry, replan, ask, hint, backoff)


def failure_for(kind) -> Failure | None:
    """The Failure record for a stored kind string ("network", …)."""
    try:
        k = Kind(kind)
    except ValueError:
        return None
    retry, replan, ask, hint, backoff = _TABLE[k]
    return Failure(k, retry, replan, ask, hint, backoff)


def explain(failure: Failure) -> str:
    """A short clause for the final message, e.g. ' (cause: the network looks unavailable)'."""
    return f" (Likely cause: {failure.hint}.)" if failure.hint else ""
