"""
core/model_router.py — "Which model should handle this?"

Every non-voice AI call can pass tier="auto" to the gateway; the router looks at
the request and picks one of five lanes:

    local   no model at all      pure arithmetic ("What's 25 x 42?") is computed
                                 exactly on the machine — instant, free, never wrong
    fast    flash-lite           greetings, trivia, definitions, conversions,
                                 classification, short look-ups
    normal  flash                writing (emails, messages, summaries), explanations,
                                 most everyday requests
    deep    flash + HIGH         analysing a whole project, debugging, comparisons,
            thinking budget      planning, research synthesis, anything long
    vision  flash + image        screenshots, photos, camera frames, "look at this"

FREE-TIER REALITY (checked Sept 2026)
    Google moved the Pro models behind billing, so the free tier is Flash-only.
    "deep" is therefore the same Flash model with a large thinking budget
    (16k thinking tokens) and a bigger output allowance — genuinely better at
    multi-step reasoning, still free. If the user explicitly configures a
    different deep model (JARVIS_AI_MODEL_DEEP, e.g. a local qwen3 via Ollama)
    it is used for this tier only. Nothing paid is ever auto-selected.

WHAT THIS DOES NOT ROUTE
    The live voice conversation runs on Gemini Live's native-audio model, which
    answers directly. The router governs everything *behind* the voice: the
    `think` tool, the planner, research, document analysis, vision analysis,
    team builds and ai.answer.

The router is heuristic, deterministic and dependency-free (unit-tested with
real execution). It is deliberately conservative: when unsure it picks
"normal", and it only answers locally when the whole request is arithmetic.
"""
from __future__ import annotations

import ast
import math
import operator
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

try:  # optional: Tamil / Tanglish keyword mapping
    from core.multilingual import normalize as _ml_normalize
except Exception:  # pragma: no cover
    _ml_normalize = None

# Text-generation tiers served by AIGateway.generate(). "image" (image OUTPUT)
# is served by AIGateway.generate_image(); "live" is the voice session's model
# (main.py) — both are configured in config/models.json like the others.
TIERS = ("fast", "normal", "deep", "vision", "research", "computer_use")
ALL_TIERS = TIERS + ("image", "live")
ALIASES = {"default": "normal", "": "normal", "quick": "fast", "lite": "fast",
           "reasoning": "deep", "think": "deep", "screen": "vision",
           "computer": "computer_use", "computer-use": "computer_use", "gui": "computer_use",
           "web": "research", "grounded": "research",
           # historical: tier="image" meant "a request that includes an image"
           "image": "vision", "picture": "vision"}


@dataclass
class RouteDecision:
    tier: str                         # fast | normal | deep | vision
    reason: str
    effort: Optional[str] = None      # None | "low" | "medium" | "high" (ReasoningEffort values)
    max_output_tokens: Optional[int] = None
    local_answer: Optional[str] = None
    signals: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"tier": "local" if self.local_answer is not None else self.tier,
                "reason": self.reason, "effort": self.effort,
                "max_output_tokens": self.max_output_tokens, "signals": list(self.signals)}


# ── Exact local arithmetic ───────────────────────────────────────────────────

_WORD_OPS = [
    (r"\bmultiplied\s+by\b", "*"), (r"\btimes\b", "*"), (r"\binto\b", "*"),
    (r"\bdivided\s+by\b", "/"), (r"\bover\b", "/"),
    (r"\bplus\b", "+"), (r"\band\b", "+"), (r"\bminus\b", "-"), (r"\bless\b", "-"),
    (r"\bmod(?:ulo)?\b", "%"),
    (r"\bto\s+the\s+power\s+of\b", "**"), (r"\braised\s+to\b", "**"), (r"\bpower\b", "**"),
    (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3"),
]
_PREFIX = re.compile(
    r"^\s*(?:hey\s+jarvis[,\s]*|jarvis[,\s]*)?(?:please\s+)?"
    r"(?:what(?:'s|\s+is|\s+are)|whats|how\s+much\s+is|calculate|compute|evaluate|solve|work\s+out|"
    r"tell\s+me)?\s*", re.I)
_SUFFIX = re.compile(r"\s*(?:=|equals?|is\s+what)?\s*[?.!]*\s*$", re.I)
_ALLOWED_CHARS = re.compile(r"^[\d\s.+\-*/%()]*$|^.*sqrt.*$")
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
        ast.Pow: operator.pow}


class _NotArithmetic(Exception):
    pass


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return Fraction(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(b) > 64 or abs(a) > 10 ** 12:
                raise _NotArithmetic("exponent too large")
            if isinstance(b, Fraction) and b.denominator != 1:
                return Fraction(float(a) ** float(b)) if a >= 0 else _raise()
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and b == 0:
            raise ZeroDivisionError
        r = _BIN[type(node.op)](a, b)
        if isinstance(r, Fraction) and (abs(r.numerator) > 10 ** 60 or r.denominator > 10 ** 60):
            raise _NotArithmetic("number too large")
        return r
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sqrt" \
            and len(node.args) == 1 and not node.keywords:
        v = _eval(node.args[0])
        if v < 0:
            raise _NotArithmetic("negative sqrt")
        r = Fraction(math.isqrt(int(v))) if v.denominator == 1 and math.isqrt(int(v)) ** 2 == v \
            else Fraction(math.sqrt(float(v)))
        return r
    raise _NotArithmetic(type(node).__name__)


def _raise():
    raise _NotArithmetic("complex result")


def _fmt(v: Fraction) -> str:
    if v.denominator == 1:
        n = v.numerator
        return f"{n:,}" if abs(n) >= 10000 else str(n)
    f = float(v)
    s = f"{f:.10g}"
    if "e" not in s and "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def solve_arithmetic(text: str) -> Optional[str]:
    """Return the exact result if `text` is *only* an arithmetic question, else None.

    >>> solve_arithmetic("What's 25 × 42?")
    '1050'
    """
    if not text or len(text) > 160:
        return None
    t = text.strip().lower()
    t = _PREFIX.sub("", t, count=1)
    t = _SUFFIX.sub("", t)
    t = t.replace("×", "*").replace("✕", "*").replace("÷", "/").replace("−", "-").replace("^", "**")
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)                    # 1,000 → 1000
    t = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s+", r"(\1/100)*", t)   # 15% of 200
    t = re.sub(r"\bsquare\s+root\s+of\s+(\d+(?:\.\d+)?)", r"sqrt(\1)", t)
    t = re.sub(r"(?<=\d)\s*x\s*(?=[\d(])", "*", t)                # 25 x 42
    t = re.sub(r"^(?:add|sum(?:\s+of)?)\s+(.+?)\s+(?:and|to|plus)\s+(.+)$", r"(\1)+(\2)", t)
    t = re.sub(r"^multiply\s+(.+?)\s+(?:by|and|with)\s+(.+)$", r"(\1)*(\2)", t)
    t = re.sub(r"^divide\s+(.+?)\s+by\s+(.+)$", r"(\1)/(\2)", t)
    t = re.sub(r"^subtract\s+(.+?)\s+from\s+(.+)$", r"(\2)-(\1)", t)
    t = re.sub(r"^(?:the\s+)?product\s+of\s+(.+?)\s+and\s+(.+)$", r"(\1)*(\2)", t)
    for pat, rep in _WORD_OPS:
        t = re.sub(pat, rep, t)
    t = t.strip()
    if not t or not _ALLOWED_CHARS.match(t):
        return None
    if re.search(r"[a-z]", t.replace("sqrt", "")):
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", t)
    if len(nums) < 2 and "sqrt" not in t:
        return None
    if not re.search(r"[+\-*/%]|sqrt", t):
        return None
    try:
        tree = ast.parse(t, mode="eval")
        return _fmt(_eval(tree))
    except ZeroDivisionError:
        return "undefined (division by zero)"
    except (_NotArithmetic, SyntaxError, ValueError, OverflowError, TypeError):
        return None


# ── Tier heuristics ──────────────────────────────────────────────────────────

_VISION = re.compile(
    r"\b(screenshot|screen ?shot|on (?:my|the) screen|this screen|look at (?:this|that|my)|"
    r"what do you see|photo|picture|image|camera|webcam|this chart|this graph|this diagram|"
    r"scan (?:this|the) (?:page|document)|what(?:'s| is) (?:this|that) (?:error|window|button))\b", re.I)
_DEEP_STRONG = re.compile(
    r"\b(entire|whole|full)\s+(?:[\w'-]+\s+){0,2}(?:project|codebase|code ?base|repo(?:sitory)?|app|application|system|"
    r"document|book|thesis|report)|\bcodebase\b|\bdeep[- ]dive\b|\bin[- ]depth\b|\bthorough(?:ly)?\b|"
    r"\bstep[- ]by[- ]step\b|\broot[- ]cause\b|\barchitecture\b|\bcomprehensive\b|"
    r"\bprove\b|\bproof\b|\bderive\b|\bresearch\b|\bstrategy\b|\broadmap\b", re.I)
_DEEP_SOFT = re.compile(
    r"\b(analy[sz]e|analysis|audit|review|evaluate|assess|compare|comparison|trade-?offs?|pros and cons|"
    r"debug|diagnose|investigate|optimi[sz]e|refactor|design|plan|explain why|why does|"
    r"reason(?:ing)?|complex|detailed|critique|improve)\b", re.I)
_OBJECTS = re.compile(
    r"\b(project|codebase|code|repo(?:sitory)?|folder|dataset|data|spreadsheet|logs?|architecture|"
    r"system|algorithm|business|market|stack ?trace|crash|bug|performance)\b", re.I)
_WRITING = re.compile(
    r"\b(write|draft|compose|email|e-mail|letter|essay|summari[sz]e|summary|rewrite|rephrase|"
    r"paraphrase|translate|explain|story|poem|reply|respond|message|post|caption|cover letter|"
    r"proposal|description|bio|speech|outline|notes)\b", re.I)
_FAST = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|good (?:morning|afternoon|evening|night)|ok(?:ay)?|yes|no)\b|"
    r"\b(what time|what day|what(?:'s| is) the date|define|definition of|meaning of|spell|synonym|antonym|"
    r"capital of|how many (?:days|hours|minutes|seconds|weeks)|convert|in (?:celsius|fahrenheit|km|miles|"
    r"kg|pounds|inches|cm|usd|inr|dollars|rupees)|who (?:is|was)|when (?:is|was)|where is|"
    r"true or false|yes or no|classify|which category|one word)\b", re.I)
_TA_DEEP = ("ஆய்வு", "பகுப்பாய்வு", "முழு", "விரிவாக", "ஆராய்", "விளக்கமாக")
_TA_WRITE = ("கடிதம்", "மின்னஞ்சல்", "எழுது", "கட்டுரை", "கவிதை")
_TA_VISION = ("படம்", "திரை", "புகைப்படம்", "கேமரா")


def normalize_tier(tier: Optional[str]) -> str:
    t = (tier or "").strip().lower()
    t = ALIASES.get(t, t)
    return t if t in TIERS or t == "auto" else "normal"


def route(prompt: str = "", *, images: int = 0, documents: int = 0, audio: int = 0,
          context_chars: int = 0, hint: Optional[str] = None,
          allow_local: bool = True) -> RouteDecision:
    """Pick a tier for one request. `hint` (a tier name) always wins."""
    text = prompt if isinstance(prompt, str) else str(prompt or "")
    signals: list[str] = []

    if hint and normalize_tier(hint) in TIERS:
        tier = normalize_tier(hint)
        eff = "high" if tier == "deep" else None
        return RouteDecision(tier, f"explicit tier '{tier}'", effort=eff,
                             max_output_tokens=8192 if tier == "deep" else None, signals=["hint"])

    low = text.lower()
    norm = low
    if _ml_normalize and re.search(r"[஀-௿]", text):
        try:
            norm = low + " " + _ml_normalize(text)
        except Exception:
            pass

    deep_strong = len(_DEEP_STRONG.findall(norm)) + sum(1 for w in _TA_DEEP if w in text)
    deep_soft = len(_DEEP_SOFT.findall(norm))
    has_object = bool(_OBJECTS.search(norm))
    size = len(text) + max(0, context_chars)
    if deep_strong:
        signals.append(f"deep-keywords:{deep_strong}")
    if deep_soft:
        signals.append(f"analysis-verbs:{deep_soft}")

    # 1. images → vision (reasoning bumped if the question is analytical)
    wants_vision = bool(_VISION.search(norm)) or any(w in text for w in _TA_VISION)
    if images:
        signals.append(f"images:{images}")
        eff = "medium" if (deep_strong or deep_soft >= 2) else None
        return RouteDecision("vision", "request includes an image", effort=eff, signals=signals)
    if wants_vision:
        signals.append("vision-keywords")
        return RouteDecision("vision", "request refers to the screen / an image "
                             "(caller must attach the capture)", signals=signals)

    # 2. exact arithmetic → answered locally, no model call
    if allow_local and not documents and not audio:
        ans = solve_arithmetic(text)
        if ans is not None:
            return RouteDecision("fast", "pure arithmetic — computed exactly on-device",
                                 local_answer=ans, signals=["arithmetic"])

    # 3. deep
    if documents:
        signals.append(f"documents:{documents}")
    if size > 6000:
        signals.append(f"long-input:{size}")
    if deep_strong or deep_soft >= 2 or size > 6000 or (documents and deep_soft) \
            or (deep_soft and (size > 1500 or has_object)):
        why = ("large input" if size > 6000 and not (deep_strong or deep_soft)
               else "multi-step analysis / reasoning requested")
        return RouteDecision("deep", why, effort="high", max_output_tokens=8192, signals=signals)

    # 4. writing / explanation → normal
    if _WRITING.search(norm) or any(w in text for w in _TA_WRITE) or documents or deep_soft:
        signals.append("writing-or-explanation")
        return RouteDecision("normal", "writing / explanation", signals=signals)

    # 5. short factual / chit-chat → fast
    words = len(re.findall(r"\S+", text))
    if _FAST.search(norm) and words <= 25:
        signals.append("quick-lookup")
        return RouteDecision("fast", "short factual / conversational request", signals=signals)
    if words <= 8 and size < 80:
        signals.append("short")
        return RouteDecision("fast", "short request", signals=signals)

    return RouteDecision("normal", "general request", signals=signals)


# ── ModelRouter.select — task-level choice with fallback chain ────────────────

TASK_TIERS = {
    # quick
    "chat": "fast", "greeting": "fast", "lookup": "fast", "classify": "fast", "extract": "fast",
    "critique": "fast", "title": "fast", "intent": "fast",
    # everyday
    "write": "normal", "email": "normal", "summarize": "normal", "explain": "normal",
    "translate": "normal", "answer": "normal", "plan": "normal", "general": "normal",
    # reasoning
    "analyze": "deep", "analysis": "deep", "debug": "deep", "code": "deep", "coding": "deep",
    "architecture": "deep", "compare": "deep", "review": "deep", "synthesis": "deep",
    "design_spec": "deep", "math_proof": "deep",
    # special lanes
    "research": "research", "web": "research",
    "vision": "vision", "screen": "vision", "ocr": "vision", "camera": "vision",
    "computer_use": "computer_use", "gui": "computer_use",
    "image": "image", "draw": "image", "image_generation": "image",
    "voice": "live", "live": "live",
}
_COMPLEXITY = {"low": 0, "simple": 0, "easy": 0, "medium": 1, "normal": 1, "auto": None, "": None,
               "high": 2, "complex": 2, "hard": 2}


@dataclass
class ModelChoice:
    tier: str                                  # the lane chosen
    model: str                                 # concrete model id ("" = provider default)
    effort: Optional[str] = None
    max_output_tokens: Optional[int] = None
    grounding: bool = False
    chain: list = field(default_factory=list)  # [(tier, model, effort), …] in order of use
    reason: str = ""
    local_answer: Optional[str] = None

    def as_dict(self) -> dict:
        return {"tier": self.tier, "model": self.model, "effort": self.effort,
                "max_output_tokens": self.max_output_tokens, "grounding": self.grounding,
                "chain": [list(c) for c in self.chain], "reason": self.reason,
                "local": self.local_answer is not None}


class ModelRouter:
    """select(task_type, complexity, requires_vision, …) → ModelChoice.

    The tier table comes from config/models.json (core/config.py). `resolve`
    maps "@normal"/"@fast"/"" to concrete ids; by default it uses the table's
    own normal/fast entries, the gateway passes the live provider's models."""

    def __init__(self, table: Optional[dict] = None, resolve=None, usable=None):
        if table is None:
            try:
                from core.config import tier_table
                table = tier_table()
            except Exception:
                table = {}
        self.table = table or {}
        self._resolve = resolve
        self._usable = usable or (lambda model: True)

    def model_of(self, tier: str) -> str:
        seen: set = set()
        t = tier
        while True:
            v = str((self.table.get(t) or {}).get("model") or "")
            if v.startswith("@") and v[1:] not in seen:
                seen.add(t)
                t = v[1:]
                continue
            if self._resolve is not None:
                return self._resolve(t, v)
            if not v and t != "fast" and t != "normal":
                return self.model_of("normal")         # empty = "same as normal"
            return v

    def chain(self, tier: str) -> list[tuple[str, str, Optional[str]]]:
        """Primary model, its alternates, then each fallback tier — de-duplicated,
        models the health record marks unusable skipped."""
        out: list = []
        seen: set = set()

        def add(t, model, eff):
            key = (model, eff)
            if key in seen:
                return
            seen.add(key)
            if model and not self._usable(model):
                return
            out.append((t, model, eff))
        cfg = self.table.get(tier) or {}
        add(tier, self.model_of(tier), cfg.get("effort"))
        for alt in self.alternates(tier):
            add(tier, alt, cfg.get("effort"))
        for fb in cfg.get("fallback") or []:
            fcfg = self.table.get(fb) or {}
            add(fb, self.model_of(fb), fcfg.get("effort"))
            for alt in self.alternates(fb):
                add(fb, alt, fcfg.get("effort"))
        return out

    def alternates(self, tier: str, _depth: int = 0) -> list[str]:
        """Other models for a tier; "@normal" tiers borrow normal's alternates."""
        cfg = self.table.get(tier) or {}
        if cfg.get("alternates"):
            return [str(a) for a in cfg["alternates"]]
        v = str(cfg.get("model") or "")
        return self.alternates(v[1:], _depth + 1) if v.startswith("@") and _depth < 4 else []

    def select(self, task_type: str = "auto", complexity: str = "auto", *, requires_vision: bool = False,
               requires_image_output: bool = False, requires_computer_use: bool = False,
               requires_web: bool = False, prompt: str = "", context_chars: int = 0,
               images: int = 0) -> ModelChoice:
        task = (task_type or "auto").strip().lower().replace("-", "_").replace(" ", "_")
        level = _COMPLEXITY.get((complexity or "auto").strip().lower())
        reason = ""
        local = None
        if requires_image_output or TASK_TIERS.get(task) == "image":
            tier, reason = "image", "image output requested"
        elif requires_computer_use or TASK_TIERS.get(task) == "computer_use":
            tier, reason = "computer_use", "operating the computer (screen in, actions out)"
        elif TASK_TIERS.get(task) == "live":
            tier, reason = "live", "real-time voice"
        elif requires_web or TASK_TIERS.get(task) == "research":
            tier, reason = "research", "needs current information from the web"
        elif requires_vision or images or TASK_TIERS.get(task) == "vision":
            tier, reason = "vision", "input includes an image / the screen"
        elif task in TASK_TIERS:
            tier, reason = TASK_TIERS[task], f"task type '{task}'"
        else:
            d = route(prompt, context_chars=context_chars, images=images)
            tier, reason, local = d.tier, d.reason, d.local_answer
        # complexity nudges the text lanes (and vision effort)
        if tier in ("fast", "normal", "deep") and level is not None and local is None:
            order = ("fast", "normal", "deep")
            want = order[level]
            if want != tier:
                reason += f"; complexity {complexity} → {want}"
                tier = want
        cfg = self.table.get(tier) or {}
        effort = cfg.get("effort")
        if tier == "vision" and level == 2:
            effort = effort or "medium"
        mot = cfg.get("max_output_tokens")
        return ModelChoice(tier=tier, model=self.model_of(tier), effort=effort,
                           max_output_tokens=int(mot) if mot else None,
                           grounding=bool(cfg.get("grounding")), chain=self.chain(tier),
                           reason=reason, local_answer=local)


def explain(prompt: str, **kw) -> str:
    """Human-readable one-liner (used by the `capabilities`/`think` tools)."""
    d = route(prompt, **kw)
    lane = "local (exact)" if d.local_answer is not None else d.tier
    return f"{lane}: {d.reason}"
