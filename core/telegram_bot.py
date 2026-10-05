"""
core/telegram_bot.py — JARVIS in your pocket (Telegram), free.

Phase 7, from the prompt pack's Part Two ("The Pocket" + "The Standing
Order"), adapted to this codebase and its free-only rule (Telegram bots cost
nothing; no tunnel, no open port — JARVIS long-polls api.telegram.org).

    SET UP (once, by you): talk to @BotFather in Telegram, create a bot, and put
    its token in .env as TELEGRAM_BOT_TOKEN=… (encrypted with the other keys at
    the next start). JARVIS never asks for it in chat and never logs it. With
    no token this module stays asleep and says so when asked.

    PAIRING: at the desk say "JARVIS, pocket code" (or /pocket in Mission
    Control). A 6-character code from a cryptographic source appears on the
    HUD — never spoken, never sent to the model. Send it to your bot as the
    WHOLE message within ten minutes. It works once; five wrong guesses kill
    it. That private chat is now you. Everyone else gets ONE polite refusal and
    then silence — no answers, no free transcription. Group chats are ignored.

    FROM THE PHONE: the same brain and the same permission rules as the desk,
    but stricter: tasks run with origin "telegram", so MEDIUM-risk steps also
    need the HUD. The phone never presses ALLOW — anything needing approval
    waits at the desk and an unanswered request expires as a no.
        "remember that …"   saved through the verified memory path
        a command           a background task; the result comes back here
        "answer: …"         answers a question a phone task asked
        "status"            a short desk brief
        "stop"              the emergency stop (stopping is always allowed)
        anything else       answered by the reasoning brain with your context
    A voice note is transcribed on THIS PC (faster-whisper) and treated like
    the text; its reply also comes back as a voice note (edge-tts, free) when
    that is installed. Messages older than 15 minutes (the PC was off) are not
    acted on — JARVIS says so and asks you to resend.

    REPORTS: every push carries the id of the card it came from, so the same
    card never reaches the phone twice.

    REMINDERS: a reminder set at the desk still pops up on this PC; while the
    urgent-only line is armed (you said you're out) it also reaches the phone,
    once. (The reminder script leaves a receipt in ~/.jarvis/reminders/
    fired.jsonl; the running line relays new receipts every 15 s.)

    STANDING ORDERS: "I'm going out, only message me if it's urgent" arms the
    URGENT LINE and sends nothing now. An urgent desk event (a system alert, a
    desk task that failed or is waiting for approval) then reaches the phone
    ONCE — ten-minute cooldown. "Text me now" sends a brief at once. "Don't
    text me" / "I'm back" turn the line off. A conditional never sends
    anything by itself.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import queue
import re
import secrets
import string
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

API = "https://api.telegram.org"
ORIGIN = "telegram"
CODE_TTL = 600.0
CODE_TRIES = 5
URGENT_COOLDOWN = 600.0
STALE_S = 900.0
RELAY_EVERY_S = 15.0
REMINDER_LOG = Path.home() / ".jarvis" / "reminders" / "fired.jsonl"
_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "O0I1L")

NOT_SET_UP = ("Telegram isn't set up. To use it: create a bot with @BotFather in Telegram, put its token in .env "
              "as TELEGRAM_BOT_TOKEN=…, and restart JARVIS. Nothing else changes until then.")
HELP = ("I'm JARVIS, your assistant at the desk. From here you can: give me a task ('find the budget file'), "
        "ask a question, say 'remember that …', 'status', 'stop', or 'answer: …' when a task asks you something. "
        "Anything that needs approval waits for you at the desk. A voice note works too.")

# ── standing orders vs messages (pure; tested with a routing table) ──────────
_ME = r"(?:message|text|ping|telegram|notify|call|bug|buzz)"
_COND = re.compile(
    rf"\b{_ME}\s+me\s+(?:only\s+)?(?:if|when|unless|in case)\b"
    rf"|\bonly\s+{_ME}\s+me\b"
    r"|\bunless\s+(?:it'?s|it is|something'?s|something is)\s+(?:urgent|important|an emergency|on fire)\b"
    r"|\bonly\s+(?:if|when)\s+(?:it'?s\s+|it is\s+|something'?s\s+)?(?:urgent|important|an emergency|on fire)\b",
    re.I)
_QUIET = re.compile(r"\b(?:don'?t|do not|never)\s+(?:message|text|ping|telegram|notify|call|bug|buzz|bother|disturb)"
                    r"\s+me\b", re.I)
_BACK = re.compile(r"^\W*(?:jarvis\W+)?(?:i'?m back|i am back|i'?m home|i am home|(?:i'?m\s+)?back at (?:my|the) desk|"
                   r"stand down|you can (?:message|text) me (?:again|normally|any ?time))\b", re.I)
_NOW = re.compile(r"^\W*(?:jarvis\W+)?(?:please\s+)?(?:message|text|ping|telegram|send)\s+me"
                  r"(?:\s+(?:a|the|an)\s+(?:brief|briefing|update|summary|status)|\s+what'?s\s+(?:up|happening|going on))?"
                  r"(?:\s+(?:now|right now|please))*\W*$", re.I)


def route(text: str) -> str:
    """'standing_order' | 'quiet' | 'back' | 'message_now' | 'none' for something the user said."""
    t = str(text or "").strip()
    if not t:
        return "none"
    if _COND.search(t):
        return "standing_order"
    if _QUIET.search(t):
        return "quiet"
    if _BACK.search(t):
        return "back"
    if _NOW.match(t):
        return "message_now"
    return "none"


def ack_for(text: str, kind: str = "standing_order") -> str:
    if kind == "quiet":
        return "Understood — I won't message you unless you write to me first."
    if kind == "back":
        return "Welcome back. The urgent-only line is off."
    t = str(text or "").lower()
    if "meeting" in t:
        return "Very good. Have a good meeting — I'll only message you if it can't wait."
    if any(w in t for w in ("sleep", "bed", "night")):
        return "Sleep well. I'll only message you if it's urgent."
    if any(w in t for w in ("leaving", "going out", "heading out", "out for")):
        return "Understood — I'll hold the fort and only message you if it's urgent."
    return "Understood. I'll only message you if something urgent comes up."


# ── what a phone message asks for (pure; tested) ────────────────────────────
_LEAD = re.compile(r"^\W*(?:(?:hey|ok|okay)\s+)?(?:jarvis\W+)?(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?", re.I)
_REMEMBER = re.compile(r"^(?:remember|note|don'?t forget)(?:\s+that)?\s*[,:]?\s+(.+)$", re.I | re.S)
_STATUS = re.compile(r"^(?:status|brief(?:\s+me)?|update me|any updates|anything new|what(?:'s| is| are)\s+(?:you\s+"
                     r"(?:doing|working on|up to)|running|happening|going on)|how(?:'s| is)\s+it going)\W*$", re.I)
_STOP = re.compile(r"^(?:stop|stop everything|stop it|cancel everything|abort|halt|emergency stop)\W*$", re.I)
_ANSWER = re.compile(r"^answer\s*[:\-]\s*(.+)$", re.I | re.S)
_TASK = re.compile(
    r"^(?:find|locate|search|google|look\s+(?:up|for)|research|investigate|open|launch|start|run|rename|move|copy|"
    r"delete|remove|read|summari[sz]e|send|message|text|email|write|make|create|prepare|draft|generate|download|"
    r"save|play|close|remind|set|analy[sz]e|review|audit|build|scan|check|convert|export|share|keep|draw|sketch|"
    r"paint|study|turn|mute|unmute|pause|resume|lock|schedule|install|update|clean|organi[sz]e|back\s+up|backup|"
    r"take|print|add|list|show\s+my|get)\b", re.I)
_LIVE_DATA = re.compile(r"\b(?:weather|forecast|news|headlines|price|stock|score|exchange rate|traffic)\b", re.I)


def phone_intent(text: str) -> tuple[str, str]:
    """→ (kind, body). kind: stop | remember | status | answer | task | ask."""
    raw = str(text or "").strip()
    body = _LEAD.sub("", raw, count=1).strip() or raw
    if _STOP.match(body):
        return "stop", body
    m = _ANSWER.match(body)
    if m:
        return "answer", m.group(1).strip()
    m = _REMEMBER.match(body)
    if m:
        return "remember", m.group(1).strip()
    if _STATUS.match(body):
        return "status", body
    if _TASK.match(body):
        return "task", body
    if _LIVE_DATA.search(body):          # live facts need the web, not the model's memory
        return "task", body
    return "ask", body


# ── desk text → phone text (tags and model directives removed) ──────────────
_TAG = re.compile(r"\[([A-Z][A-Z_ -]{2,})(?::|=|\s)?([^\]]*)\]")
_DIRECTIVE = re.compile(r"\b(?:the user|user's|users'|in their language)\b|^(?:Say|Confirm briefly|Briefly "
                        r"(?:tell|inform|say)|Never (?:say|claim|pretend))\b", re.I)
_THINK = re.compile(r"\s*\[THINK:[^\]]*\]\s*$")


def phone_text(text: str) -> str:
    """Tool/desk sentences → something to show a person on the phone."""
    def tag(m):
        if m.group(1).replace(" ", "-").startswith("SELF-CHECK"):
            return f"(Self-check: {m.group(2).strip().lstrip(':').strip()})"
        return ""
    s = _TAG.sub(tag, str(text or "")).replace("\r", " ").replace("\n", " ").strip()
    parts = re.split(r"(?<=[.!?])\s+", s)
    keep = [p for p in parts if p.strip() and not _DIRECTIVE.search(p.strip())]
    return re.sub(r"\s{2,}", " ", " ".join(keep)).strip()


def _h(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8", "replace")).hexdigest()[:10]


def _redact(text) -> str:
    try:
        from core.secret_store import redact
        return redact(text)
    except Exception:
        return "(details withheld)"


class PairingCode:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self.code = ""
        self.expires = 0.0
        self.tries = 0

    def mint(self) -> str:
        self.code = "".join(secrets.choice(_ALPHABET) for _ in range(6))
        self.expires = self._clock() + CODE_TTL
        self.tries = 0
        return self.code

    def clear(self) -> None:
        self.code = ""

    def check(self, message: str) -> Optional[bool]:
        """True = paired · False = wrong guess · None = no code is live."""
        if not self.code or self._clock() > self.expires or self.tries >= CODE_TRIES:
            self.code = ""
            return None
        if secrets.compare_digest(str(message or "").strip().upper().encode(), self.code.encode()):
            self.code = ""
            return True
        self.tries += 1
        if self.tries >= CODE_TRIES:
            self.code = ""
        return False


class TelegramPocket:
    def __init__(self, *, token_fn: Callable[[], str], handle_text: Callable[[str], str],
                 transcribe: Optional[Callable[[str], str]] = None, speak: Optional[Callable[[str], bytes]] = None,
                 brief: Optional[Callable[[], str]] = None, on_code: Optional[Callable[[str], None]] = None,
                 state_path: Optional[Path] = None, http=None, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, reminder_log: Optional[Path] = REMINDER_LOG):
        self._token_fn = token_fn
        self.reminder_log = Path(reminder_log) if reminder_log else None
        self._handle = handle_text
        self._transcribe = transcribe
        self._speak = speak
        self._brief = brief
        self.on_code = on_code
        self._log = log
        self._clock = clock
        self._http = http
        if state_path is None:
            from core.secret_store import base_dir
            state_path = base_dir() / "memory" / "telegram.json"
        self.path = Path(state_path)
        self.pairing = PairingCode(clock)
        self._lock = threading.Lock()
        self._state = self._load()
        self._refused: set = set(self._state.get("refused") or [])
        self._running = False
        self._outbox: "Optional[queue.Queue]" = None
        self._stale_noted = 0.0

    # ── state ───────────────────────────────────────────────────────────────
    def _load(self) -> dict:
        base = {"chat": None, "offset": 0, "sent": [], "urgent_armed": False, "urgent_last": 0.0, "refused": []}
        try:
            data = json.loads(self.path.read_bytes().decode("utf-8"))
            if isinstance(data, dict):
                base.update(data)
        except Exception:
            pass
        return base

    def _save(self) -> None:
        """Caller holds self._lock (or is single-threaded)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._state["refused"] = sorted(self._refused)[-200:]
            self._state["sent"] = list(self._state.get("sent") or [])[-500:]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(json.dumps(self._state, indent=1).replace("\n", "\r\n").encode("utf-8"))
            tmp.replace(self.path)
        except Exception:
            pass

    @property
    def paired(self) -> bool:
        return bool(self._state.get("chat"))

    @property
    def armed(self) -> bool:
        return bool(self._state.get("urgent_armed"))

    @property
    def configured(self) -> bool:
        try:
            return bool(self._token_fn())
        except Exception:
            return False

    def status(self) -> dict:
        return {"configured": self.configured, "paired": self.paired, "urgent_armed": self.armed,
                "listening": self._running, "voice_notes_in": self._transcribe is not None,
                "voice_notes_out": self._speak is not None}

    def status_text(self) -> str:
        if not self.configured:
            return NOT_SET_UP
        s = ("Telegram is set up" + (" and listening" if self._running else " but not listening")
             + ("; your phone is paired" if self.paired else "; no phone is paired yet (say 'pocket code')")
             + ("; the urgent-only line is ON." if self.armed else "; the urgent-only line is off."))
        if self._transcribe is None:
            s += " Voice notes from the phone need faster-whisper on this PC."
        return s

    # ── HTTP ────────────────────────────────────────────────────────────────
    def _call(self, method: str, data: Optional[dict] = None, files: Optional[dict] = None,
              timeout: float = 35.0):
        tok = self._token_fn()
        if not tok:
            raise RuntimeError("no TELEGRAM_BOT_TOKEN")
        http = self._http
        if http is None:
            import requests as http
        try:
            r = http.post(f"{API}/bot{tok}/{method}", data=data, files=files, timeout=timeout)
            j = r.json()
        except Exception as e:                  # never str(e): a transport error can carry the URL (token)
            raise RuntimeError(f"Telegram {method} failed ({type(e).__name__})") from None
        if not isinstance(j, dict) or not j.get("ok"):
            desc = str((j or {}).get("description") or "error") if isinstance(j, dict) else "bad reply"
            raise RuntimeError(f"Telegram {method}: {_redact(desc)[:120]}")
        return j.get("result")

    # ── sending ─────────────────────────────────────────────────────────────
    def send(self, text: str, card: str = "", voice: bool = False, chat: Optional[int] = None) -> bool:
        """Text (+ voice note) to the paired chat, now. `card` makes it once-only.
        True only when Telegram accepted the text."""
        chat = chat or self._state.get("chat")
        if not chat or not str(text or "").strip():
            return False
        with self._lock:
            if card and card in (self._state.get("sent") or []):
                return False
            if card:
                self._state.setdefault("sent", []).append(card)
                self._save()
        try:
            self._call("sendMessage", {"chat_id": chat, "text": str(text)[:4000]})
        except Exception as e:
            self._log(f"[Telegram] send failed: {_redact(e)[:160]}")
            return False
        if voice and self._speak is not None:
            try:
                audio = self._speak(str(text)[:900])
                if audio:
                    self._call("sendAudio", {"chat_id": chat, "title": "JARVIS"},
                               files={"audio": ("jarvis.mp3", audio, "audio/mpeg")})
            except Exception as e:
                self._log(f"[Telegram] voice note skipped: {_redact(e)[:120]}")
        return True

    def post(self, text: str, card: str = "", voice: bool = False) -> None:
        """Like send(), but never blocks the caller once the line is running
        (task and alert events must not wait on the network)."""
        if self._outbox is None:
            self.send(text, card=card, voice=voice)
            return
        try:
            self._outbox.put_nowait((text, card, voice))
        except queue.Full:
            self._log("[Telegram] outbox full — a message was dropped")

    def urgent(self, text: str, card: str) -> bool:
        """An urgent desk event: reaches the phone once, only while the line is armed."""
        with self._lock:
            if not self._state.get("urgent_armed") or not self._state.get("chat"):
                return False
            if card and card in (self._state.get("sent") or []):
                return False
            if self._clock() - float(self._state.get("urgent_last") or 0) < URGENT_COOLDOWN:
                return False
            self._state["urgent_last"] = self._clock()
            self._save()
        self.post("URGENT — " + str(text), card=card)
        return True

    def relay_reminders(self) -> int:
        """Reminders that fired on this PC → the phone, while the urgent line is
        armed. Each receipt is read once (a byte offset is kept); the first run
        only marks where the file ends, so old reminders are never replayed."""
        p = self.reminder_log
        if p is None:
            return 0
        try:
            size = p.stat().st_size
        except OSError:                              # nothing has fired yet: read from the start later
            with self._lock:
                if self._state.get("reminder_offset") is None:
                    self._state["reminder_offset"] = 0
                    self._save()
            return 0
        with self._lock:
            off = self._state.get("reminder_offset")
            if off is None:
                self._state["reminder_offset"] = size
                self._save()
                return 0
            off = int(off) if int(off) <= size else 0          # the file was replaced: start over
        if off >= size:
            return 0
        try:
            with open(p, "rb") as fh:
                fh.seek(off)
                chunk = fh.read(size - off)
        except OSError:
            return 0
        end = chunk.rfind(b"\n") + 1                            # only whole lines; a half-written one waits
        if end <= 0:
            return 0
        with self._lock:
            self._state["reminder_offset"] = off + end
            self._save()
        sent = 0
        for raw in chunk[:end].split(b"\n"):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8"))
                at = float(rec.get("at") or 0)
                text = str(rec.get("message") or "").strip()
            except Exception:
                continue
            if not text or self._clock() - at > 2 * STALE_S or not (self.armed and self.paired):
                continue                             # the pop-up on the PC was enough
            self.post(f"Reminder: {text}", card=f"reminder-{rec.get('task', '')}-{int(at)}")
            sent += 1
        return sent

    def brief_text(self) -> str:
        try:
            body = phone_text(self._brief()) if self._brief else ""
        except Exception:
            body = ""
        when = time.strftime("%H:%M", time.localtime(self._clock()))
        return (f"Desk brief ({when}): {body or 'Nothing to report.'} "
                f"Urgent-only line: {'on' if self.armed else 'off'}.")

    def arm(self, on: bool = True) -> bool:
        """→ True when the state changed."""
        with self._lock:
            changed = bool(self._state.get("urgent_armed")) != bool(on)
            self._state["urgent_armed"] = bool(on)
            self._save()
        return changed

    def pair(self) -> tuple[bool, str]:
        """Mint a code and show it on the HUD. → (shown, a sentence for the model — never the code)."""
        if not self.configured:
            return False, NOT_SET_UP
        if self.on_code is None:
            return False, "There is no screen here to show a pairing code on."
        code = self.pairing.mint()
        try:
            self.on_code(code)
        except Exception:
            self.pairing.clear()                 # never leave a live code nobody saw
            return False, "I couldn't show the pairing code on the HUD, so no code is active."
        return True, ("[POCKET_CODE_SHOWN] A one-time pairing code is on the HUD (you do not see it — never invent "
                      "one). Tell the user to send it to their JARVIS bot in Telegram, as the whole message, within "
                      "ten minutes.")

    def unpair(self) -> None:
        with self._lock:
            self._state["chat"] = None
            self._state["urgent_armed"] = False
            self._save()

    # ── receiving ───────────────────────────────────────────────────────────
    def handle_update(self, upd: dict) -> Optional[str]:
        """One update → the reply sent (or None)."""
        msg = upd.get("message") or {}
        chat_info = msg.get("chat") or {}
        chat = chat_info.get("id")
        if chat is None or chat_info.get("type", "private") != "private":
            return None                           # groups and channels are never "you"
        text = str(msg.get("text") or "").strip()
        if self._state.get("chat") != chat:
            ok = self.pairing.check(text) if text else None
            if ok is True:
                with self._lock:
                    self._state["chat"] = chat
                    self._refused.discard(chat)
                    self._save()
                self._log("[Telegram] paired with a new chat")
                reply = "Paired. This chat is now your line to JARVIS. Send 'help' to see what works from here."
                self._reply(chat, reply)
                return reply
            if chat not in self._refused:          # one polite refusal per stranger, then silence
                with self._lock:
                    self._refused.add(chat)
                    self._save()
                reply = "Sorry — this is a private assistant."
                self._reply(chat, reply)
                return reply
            return None
        date = msg.get("date")
        try:
            age = self._clock() - float(date) if date else 0.0
        except (TypeError, ValueError):
            age = 0.0
        if age > STALE_S:
            if self._clock() - self._stale_noted < 60:
                return None
            self._stale_noted = self._clock()
            when = time.strftime("%H:%M", time.localtime(float(date)))
            reply = (f"I was offline when you sent that ({when}), so I haven't acted on it. "
                     f"Send it again if you still want it.")
            self._reply(chat, reply)
            return reply
        voiced = False
        if not text and (msg.get("voice") or msg.get("audio")):
            voiced = True
            text = self._voice_to_text(msg.get("voice") or msg.get("audio"))
            if not text:
                reply = ("I couldn't transcribe that voice note on the PC (faster-whisper is missing or it failed). "
                         "Please type it.")
                self._reply(chat, reply)
                return reply
        if not text:
            reply = "I can read text and voice notes here — not photos or files yet."
            self._reply(chat, reply)
            return reply
        r = route(text)
        if text.lower() in ("/start", "/help", "help"):
            reply = HELP
        elif r == "standing_order":
            self.arm(True)
            reply = ack_for(text)
        elif r in ("quiet", "back"):
            self.arm(False)
            reply = ack_for(text, r)
        elif r == "message_now":
            reply = self.brief_text()
        else:
            try:
                reply = str(self._handle(text) or "").strip() or "I don't have an answer for that."
            except Exception as e:
                reply = f"That failed at the desk: {_redact(e)[:160]}"
        self.send(reply, card=f"reply-{upd.get('update_id')}", voice=voiced, chat=chat)
        return reply

    def _reply(self, chat, text: str) -> None:
        try:
            self._call("sendMessage", {"chat_id": chat, "text": text})
        except Exception as e:
            self._log(f"[Telegram] reply failed: {_redact(e)[:160]}")

    def _voice_to_text(self, media: dict) -> str:
        if self._transcribe is None:
            return ""
        try:
            if int(media.get("file_size") or 0) > 20 * 1024 * 1024:
                return ""
            info = self._call("getFile", {"file_id": media.get("file_id")}) or {}
            tok = self._token_fn()
            http = self._http
            if http is None:
                import requests as http
            data = http.get(f"{API}/file/bot{tok}/{info.get('file_path')}", timeout=60).content
            if not data or len(data) > 20 * 1024 * 1024:
                return ""
            fd, name = tempfile.mkstemp(prefix="jarvis_tg_", suffix=".ogg")
            p = Path(name)
            try:
                with open(fd, "wb") as fh:
                    fh.write(data)
                return str(self._transcribe(str(p)) or "").strip()
            finally:
                try:
                    p.unlink()
                except Exception:
                    pass
        except Exception as e:
            self._log(f"[Telegram] voice note failed: {type(e).__name__}")
            return ""

    def poll_once(self, timeout: int = 25) -> int:
        res = self._call("getUpdates", {"offset": int(self._state.get("offset") or 0), "timeout": timeout,
                                        "allowed_updates": json.dumps(["message"])}, timeout=timeout + 10)
        n = 0
        for upd in res or []:
            with self._lock:                       # advance first: an update is never handled twice
                self._state["offset"] = int(upd.get("update_id", 0)) + 1
                self._save()
            try:
                self.handle_update(upd)
            except Exception as e:
                self._log(f"[Telegram] update failed: {type(e).__name__}")
            n += 1
        return n

    def start(self) -> bool:
        if self._running or not self.configured:
            return False
        self._running = True
        self._outbox = queue.Queue(maxsize=200)

        def sender():
            next_relay = 0.0
            while self._running:
                if time.monotonic() >= next_relay:
                    next_relay = time.monotonic() + RELAY_EVERY_S
                    try:
                        self.relay_reminders()
                    except Exception as e:
                        self._log(f"[Telegram] reminder relay failed: {type(e).__name__}")
                try:
                    text, card, voice = self._outbox.get(timeout=1.0)
                except queue.Empty:
                    continue
                try:
                    self.send(text, card=card, voice=voice)
                except Exception as e:
                    self._log(f"[Telegram] send failed: {type(e).__name__}")

        def loop():
            backoff = 2.0
            while self._running:
                try:
                    self.poll_once()
                    backoff = 2.0
                except Exception as e:
                    self._log(f"[Telegram] {_redact(e)[:120]} — retrying in {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 120.0)
        threading.Thread(target=sender, daemon=True, name="telegram-out").start()
        threading.Thread(target=loop, daemon=True, name="telegram").start()
        self._log("[Telegram] pocket line listening" + ("" if self.paired else " (not paired yet — say 'pocket code')"))
        return True

    def stop(self) -> None:
        self._running = False


# ═════════════════════════════════════════════════════════════════════════════
# The bridge: the phone uses the desk's own paths (JarvisCore)
# ═════════════════════════════════════════════════════════════════════════════
_PHONE_KINDS = ("completed", "failed", "cancelled", "permission", "question")


class PocketBridge:
    def __init__(self, core):
        self.core = core
        self.pocket: Optional[TelegramPocket] = None

    # phone → desk
    def handle_text(self, text: str) -> str:
        kind, body = phone_intent(text)
        core = self.core
        if kind == "stop":
            out = core.emergency_stop("telegram")
            return "Stopped. " + phone_text(out) if out else "Stopped."
        if kind == "remember":
            out = str(core.remember({"text": body}, source="telegram"))
            ok = out.startswith("[MEMORY_SAVED")
            return ("Saved. " if ok else "Not saved. ") + phone_text(out)
        if kind == "status":
            return self.desk_status()
        if kind == "answer":
            waiting = [t for t in core.store.active()
                       if getattr(t, "origin", "") == ORIGIN and t.state.value == "WAITING_FOR_INPUT"]
            if not waiting:
                return "No task from here is waiting for an answer."
            return phone_text(core.orchestrator.answer(body, waiting[-1].id))
        if kind == "task":
            return self._task(body)
        out = str(core.think({"question": body}))
        return _THINK.sub("", out).strip()

    def _task(self, text: str) -> str:
        core = self.core
        kill = getattr(core, "kill", None)
        try:
            engaged = bool(kill is not None and kill.engaged())
        except Exception:
            engaged = False
        if engaged:
            return ("The emergency stop is on at the desk, so I won't start anything from here. It lifts when you "
                    "give a command at the desk.")
        wf = None
        if any(w in text.lower() for w in ("workflow", "routine")):
            try:
                wf = core.workflows.find(text)
            except Exception:
                wf = None
        if wf:
            core.workflows.mark_used(wf["id"])
            task = core.orchestrator.submit_steps(f"workflow: {wf['name']}", core.workflows.to_steps(wf),
                                                  origin=ORIGIN)
        else:
            lang = "en"
            try:
                lang = core.router.route(text).language
            except Exception:
                pass
            task = core.orchestrator.submit(text, origin=ORIGIN, language=lang)
        core.audit.record("task_submitted", task_id=task.id, detail=text[:120], via="telegram")
        return (f"On it — task {task.id}. I'll message you here when it finishes. Anything that needs approval "
                f"waits for you at the desk.")

    def desk_status(self) -> str:
        try:
            return phone_text(self.core.orchestrator.status(None))
        except Exception:
            return ""

    # desk → phone
    def on_task_event(self, task, kind: str, message: str) -> None:
        p = self.pocket
        if p is None or not p.paired or kind not in _PHONE_KINDS:
            return
        body = phone_text(message)
        req = str(getattr(task, "request", "") or "")[:80]
        if getattr(task, "origin", "") == ORIGIN:
            head = {"completed": f"Task {task.id} finished ({req})",
                    "failed": f"Task {task.id} failed ({req})",
                    "cancelled": f"Task {task.id} stopped ({req})",
                    "permission": f"Task {task.id} needs your approval at the desk",
                    "question": f"Task {task.id} has a question"}[kind]
            tail = {"permission": " The phone can't approve it — it waits for the HUD, and an unanswered request "
                                  "expires as a no.",
                    "question": " Reply with 'answer: …' to answer it."}.get(kind, "")
            p.post((f"{head}: {body}" if body else head + ".") + tail,
                   card=f"task-{task.id}-{kind}-{_h(message)}")
        elif kind in ("failed", "permission"):
            what = "failed" if kind == "failed" else "is waiting for your approval at the desk"
            p.urgent(f"Task {task.id} ({req}) {what}." + (f" {body}" if body else ""),
                     card=f"urgent-{task.id}-{kind}-{_h(message)}")

    def urgent_alert(self, text: str) -> bool:
        p = self.pocket
        if p is None:
            return False
        body = phone_text(text)
        return bool(body) and p.urgent(body, card=f"alert-{_h(body)}-{int(p._clock() // URGENT_COOLDOWN)}")

    def standing_order(self, text: str) -> str:
        """Something said at the desk: arm / disarm the urgent line or send a brief.
        → a HUD log line when something changed, else ''."""
        p = self.pocket
        if p is None or not p.paired:
            return ""
        r = route(text)
        if r == "standing_order":
            return "📱 Urgent-only line ON — only urgent events reach your phone." if p.arm(True) else ""
        if r in ("quiet", "back"):
            return "📱 Urgent-only line off." if p.arm(False) else ""
        if r == "message_now":
            ok = p.send(p.brief_text(), card=f"brief-{int(p._clock() // 60)}", voice=True)
            return "📱 Brief sent to your phone." if ok else ""
        return ""


_CURRENT: dict = {"bridge": None}


def set_bridge(b: Optional[PocketBridge]) -> None:
    _CURRENT["bridge"] = b


def get_bridge() -> Optional[PocketBridge]:
    return _CURRENT["bridge"]


# ── the voice/HUD tool ──────────────────────────────────────────────────────
def t_pocket(args: dict, ctx):
    from core.tools.registry import ToolResult
    b = get_bridge()
    p = b.pocket if b is not None else None
    action = str(args.get("action") or "status").strip().lower()
    if p is None or not p.configured:
        if action in ("status", "check"):
            return ToolResult(True, {"configured": False}, NOT_SET_UP)
        return ToolResult.failure(NOT_SET_UP)
    if action in ("status", "check"):
        return ToolResult(True, p.status(), p.status_text())
    if action in ("pair", "code", "connect", "link"):
        ok, msg = p.pair()
        return ToolResult(True, {}, msg) if ok else ToolResult.failure(msg)
    if action in ("away", "arm", "urgent_only", "standing_order"):
        p.arm(True)
        extra = ("" if p.paired else " No phone is paired yet, so nothing can reach the user until they pair "
                                     "(say 'pocket code').")
        return ToolResult(True, {"armed": True}, "[POCKET_ARMED] Urgent-only line is on: nothing is sent now; one "
                                                 "urgent event can reach the phone, then a ten-minute pause." + extra)
    if action in ("back", "disarm", "quiet", "off"):
        p.arm(False)
        return ToolResult(True, {"armed": False}, "[POCKET_OFF] The urgent-only line is off.")
    if action in ("send", "brief", "message", "text"):
        if not p.paired:
            return ToolResult.failure("No phone is paired yet — the user should say 'pocket code' first.")
        note = str(args.get("text") or "").strip()
        body = p.brief_text() + (f"\nNote: {note[:500]}" if note else "")
        if p.send(body, card=f"brief-{int(p._clock() // 60)}", voice=True):
            return ToolResult(True, {"sent": True}, "[POCKET_SENT] Telegram accepted the brief.")
        return ToolResult.failure("The brief was NOT sent (Telegram refused it, the network failed, or one already "
                                  "went out this minute). Say so; do not claim it was sent.")
    if action in ("unpair", "forget", "disconnect"):
        p.unpair()
        return ToolResult(True, {}, "[POCKET_UNPAIRED] The phone is unpaired; it can't reach JARVIS until a new code "
                                    "is used.")
    return ToolResult.failure(f"Unknown pocket action '{action}'. Use status, pair, away, back, send or unpair.")


def register(reg) -> None:
    from core.permissions import RiskLevel
    from core.tools.registry import ToolSpec
    reg.register(ToolSpec(
        "pocket", "The user's Telegram line: status | pair (code shown on the HUD) | away (urgent-only) | back | "
        "send (a brief now) | unpair.", t_pocket,
        {"type": "object", "properties": {"action": {"type": "string"}, "text": {"type": "string"}},
         "required": []}, {"sent": "bool"}, agent="general",
        risk=lambda a: RiskLevel.SAFE if str(a.get("action") or "status").lower() in ("status", "check")
        else RiskLevel.LOW_RISK, planner_visible=False))


# ── optional free adapters (detected without importing heavy packages) ─────
def _have(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def edge_tts_mp3(text: str, voice: str = "en-GB-RyanNeural") -> bytes:
    import asyncio
    import edge_tts

    async def run():
        buf = bytearray()
        async for ch in edge_tts.Communicate(text, voice).stream():
            if ch.get("type") == "audio":
                buf.extend(ch["data"])
        return bytes(buf)
    return asyncio.run(run())


_WHISPER: dict = {"m": None, "lock": threading.Lock()}


def whisper_file(path: str) -> str:
    with _WHISPER["lock"]:
        if _WHISPER["m"] is None:
            from faster_whisper import WhisperModel
            _WHISPER["m"] = WhisperModel("base", device="cpu", compute_type="int8")
        segs, _ = _WHISPER["m"].transcribe(path, beam_size=1, vad_filter=True)
        return " ".join(s.text for s in segs).strip()


def default_transcriber() -> Optional[Callable[[str], str]]:
    return whisper_file if _have("faster_whisper") else None


def default_speaker() -> Optional[Callable[[str], bytes]]:
    return edge_tts_mp3 if _have("edge_tts") else None
