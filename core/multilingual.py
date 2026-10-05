"""
core/multilingual.py — English, Tamil, Tanglish (Tamil in Latin letters) and
mixed speech, for the parts of JARVIS that run *outside* the Live model.

The Live model already understands Tamil natively; nothing here translates
what the user says to it. This module exists for the deterministic layers —
router, rule-based planner, workflow triggers — which match on words. Those
layers need "Chrome open pannu", "இந்த file open பண்ணு" and "search this and
summarize பண்ணு" to reach the same intent as "open Chrome".

    detect_language(text)   → "en" | "ta" | "ta-latn" | "mixed"
    normalize(text)         → an English-keyword view of the command used for
                              matching ONLY (original text is kept for the
                              model and for replies)
    response_language()     → the user's explicit preference, or "auto"
    language_directive()    → the sentence injected into the Live system
                              prompt so replies follow that preference

Stdlib only.
"""
from __future__ import annotations

import json
import re

_TAMIL_RANGE = re.compile(r"[஀-௿]")

# Tamil-script verbs/words → English intent keyword
_TA_SCRIPT = {
    "திற": "open", "திறந்து": "open", "திறக்க": "open", "ஓபன்": "open",
    "மூடு": "close", "மூட": "close",
    "தேடு": "search", "தேடி": "search", "தேட": "search", "சர்ச்": "search",
    "அனுப்பு": "send", "அனுப்பி": "send", "அனுப்ப": "send",
    "நகர்த்து": "move", "மாற்று": "move", "மூவ்": "move",
    "அழி": "delete", "நீக்கு": "delete", "டிலீட்": "delete",
    "பெயர் மாற்று": "rename", "ரீநேம்": "rename",
    "சுருக்கு": "summarize", "சுருக்கம்": "summary", "சம்மரி": "summarize",
    "படி": "read", "வாசி": "read",
    "நினைவூட்டு": "remind", "ஞாபகப்படுத்து": "remind",
    "பதிவிறக்கு": "download", "டவுன்லோட்": "download",
    "காட்டு": "show", "பார்": "show",
    "நிறுத்து": "stop", "ரத்து": "cancel",
    "கோப்பு": "file", "ஃபைல்": "file", "போல்டர்": "folder", "கோப்புறை": "folder",
    "இந்த": "this", "அந்த": "that", "என்": "my", "என்னுடைய": "my",
    "மற்றும்": "and", "அப்புறம்": "then", "பிறகு": "then",
    "செய்": "", "பண்ணு": "", "பண்ணுங்க": "", "பண்ணி": "", "செய்யுங்கள்": "",
}

# Tanglish (Latin-script Tamil) → English intent keyword
_TANGLISH = {
    "pannu": "", "pannunga": "", "panni": "", "pannitu": "", "pannidu": "", "pannu da": "",
    "sei": "", "seiyu": "", "podu": "", "kudu": "", "da": "", "di": "", "ma": "", "pa": "",
    "thira": "open", "thirakku": "open", "thiranthu": "open",
    "moodu": "close", "mudu": "close",
    "thedu": "search", "thedi": "search", "theda": "search",
    "anuppu": "send", "anupu": "send", "anuppi": "send",
    "azhi": "delete", "neekku": "delete",
    "padi": "read", "vaasi": "read",
    "kaattu": "show", "kaatu": "show", "paaru": "show", "paru": "show",
    "niruthu": "stop", "nirutthu": "stop",
    "apram": "then", "apparam": "then", "piragu": "then", "aprom": "then",
    "matrum": "and", "um": "",
    "enoda": "my", "ennoda": "my", "en": "my", "intha": "this", "indha": "this", "antha": "that",
    "yenna": "what", "enna": "what", "enga": "where", "eppo": "when",
    "nyabagam": "remind", "gnabagam": "remind",
}

_TANGLISH_MARKERS = {"pannu", "pannunga", "panni", "pannitu", "thedu", "anuppu", "enoda", "ennoda",
                     "intha", "indha", "apram", "apparam", "da", "paaru", "kaattu", "enna", "sollu",
                     "irukku", "venum", "vendam", "romba", "konjam", "seri", "illa"}

SUPPORTED_RESPONSE_LANGUAGES = {
    "auto": "Reply in the same language and style the user is using right now "
            "(English, Tamil, Tanglish, or a mix).",
    "en": "Always reply in English, even if the user speaks Tamil.",
    "ta": "Always reply in Tamil (Tamil script when writing), even if the user speaks English.",
    "tanglish": "Always reply in Tanglish — conversational Tamil mixed with English words, "
                "as spoken in Tamil Nadu.",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[\w஀-௿']+", (text or "").lower())


def detect_language(text: str) -> str:
    t = text or ""
    words = _words(t)
    if not words:
        return "en"
    ta_script = sum(1 for w in words if _TAMIL_RANGE.search(w))
    latin = len(words) - ta_script
    tanglish = sum(1 for w in words if w in _TANGLISH_MARKERS)
    if ta_script and latin:
        return "mixed"
    if ta_script:
        return "ta"
    if tanglish >= 1 and (tanglish / max(1, len(words)) >= 0.15 or tanglish >= 2):
        return "ta-latn"
    return "en"


def normalize(text: str) -> str:
    """Map Tamil/Tanglish command words to English keywords for matching.
    Unknown words pass through unchanged (English words inside mixed speech,
    app names, file names)."""
    out: list[str] = []
    for w in _words(text):
        if w in _TA_SCRIPT:
            m = _TA_SCRIPT[w]
        elif w in _TANGLISH:
            m = _TANGLISH[w]
        else:
            # Tamil-script word with an attached suffix: try prefix match
            m = w
            if _TAMIL_RANGE.search(w):
                for k, v in _TA_SCRIPT.items():
                    if len(k) >= 2 and w.startswith(k):
                        m = v
                        break
        if m:
            out.append(m)
    return " ".join(out)


def _config() -> dict:
    try:
        from core.secret_store import legacy_config_path
        return json.loads(legacy_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def response_language() -> str:
    v = str(_config().get("response_language", "auto") or "auto").strip().lower()
    return v if v in SUPPORTED_RESPONSE_LANGUAGES else "auto"


def save_response_language(lang: str) -> str:
    lang = (lang or "").strip().lower()
    aliases = {"english": "en", "tamil": "ta", "தமிழ்": "ta", "thanglish": "tanglish",
               "tamil english": "tanglish", "mixed": "tanglish", "automatic": "auto", "default": "auto"}
    lang = aliases.get(lang, lang)
    if lang not in SUPPORTED_RESPONSE_LANGUAGES:
        raise ValueError(f"Unsupported language '{lang}'. Choose: auto, en, ta, tanglish.")
    try:
        from memory.config_manager import _patch_config
        _patch_config(response_language=lang)
    except Exception:
        from core.secret_store import legacy_config_path
        p = legacy_config_path()
        cfg = _config()
        cfg["response_language"] = lang
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    return lang


def language_directive() -> str:
    lang = response_language()
    return ("[LANGUAGE]\n"
            "The user speaks English, Tamil, Tanglish (Tamil written/spoken with English words) "
            "or a mix — understand all of them naturally; never ask them to switch.\n"
            f"Reply language preference: {lang}. {SUPPORTED_RESPONSE_LANGUAGES[lang]}\n\n")
