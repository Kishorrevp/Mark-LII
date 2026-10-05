"""Phase 7 — the pocket line (Telegram) and standing orders.

Verification level: GENUINE EXECUTION of core/telegram_bot.py with a fake
Telegram HTTP layer (no network), and of the bridge against a real
JarvisCore(persist=False). Telegram's real servers are NOT contacted here —
that needs the user's own bot token; the wire format used (sendMessage,
getUpdates, getFile, sendAudio) is the documented Bot API.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import telegram_bot as T                                     # noqa: E402

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawX"


class Resp:
    def __init__(self, payload=None, content=b""):
        self._p, self.content = payload, content

    def json(self):
        return self._p


class FakeHttp:
    """Records every Bot API call; answers like Telegram does."""
    def __init__(self):
        self.calls, self.fail, self.updates = [], set(), []

    def post(self, url, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(data or {}), bool(files), url))
        if method in self.fail:
            return Resp({"ok": False, "description": "Bad Request: chat not found"})
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return Resp({"ok": True, "result": ups})
        if method == "getFile":
            return Resp({"ok": True, "result": {"file_path": "voice/file_7.oga"}})
        return Resp({"ok": True, "result": {"message_id": len(self.calls)}})

    def get(self, url, timeout=None):
        self.calls.append(("download", {}, False, url))
        return Resp(content=b"OggS fake voice")

    def texts(self, chat=None):
        return [d.get("text") for m, d, _, _ in self.calls if m == "sendMessage"
                and (chat is None or d.get("chat_id") == chat)]


def msg(chat, text=None, uid=1, date=None, ctype="private", **extra):
    m = {"chat": {"id": chat, "type": ctype}, "date": date if date is not None else int(time.time())}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": uid, "message": m}


class Clock:
    def __init__(self, t=None):
        self.t = time.time() if t is None else t

    def __call__(self):
        return self.t


def make_pocket(handle=lambda t: f"echo: {t}", **kw):
    http, logs = FakeHttp(), []
    tmp = Path(tempfile.mkdtemp()) / "telegram.json"
    clock = kw.pop("clock", Clock())
    kw.setdefault("reminder_log", None)         # never the real ~/.jarvis receipts
    p = T.TelegramPocket(token_fn=lambda: TOKEN, handle_text=handle, state_path=tmp, http=http,
                         log=logs.append, clock=clock, **kw)
    return p, http, logs, clock


def pair(p, chat=42):
    code = p.pairing.mint()
    assert p.handle_update(msg(chat, code, uid=900)).startswith("Paired")
    return code


# ═════════════════════════════════════════════════════════════════════════════
class RoutingTableTests(unittest.TestCase):
    TABLE = [
        ("I'm going out, only message me if it's urgent", "standing_order"),
        ("Heading to a meeting. Text me only if something's on fire", "standing_order"),
        ("Going to sleep — don't message me unless it's an emergency", "standing_order"),
        ("ping me when the download finishes", "standing_order"),
        ("message me in case the build breaks", "standing_order"),
        ("only bug me if the server goes down", "standing_order"),
        ("Telegram me if anything breaks", "standing_order"),
        ("don't text me", "quiet"),
        ("never call me during dinner", "quiet"),
        ("text me now", "message_now"),
        ("Jarvis, message me a summary", "message_now"),
        ("send me an update please", "message_now"),
        ("I'm back", "back"),
        ("back at my desk", "back"),
        ("tell me when it's done", "none"),
        ("call me Tony", "none"),
        ("send me the report file", "none"),
        ("remind me to text mom at six", "none"),
        ("what's the weather", "none"),
        ("I have a call at 5", "none"),
        ("", "none"),
    ]

    def test_table(self):
        for text, want in self.TABLE:
            self.assertEqual(T.route(text), want, text)

    def test_acks(self):
        self.assertIn("meeting", T.ack_for("off to a meeting, only text me if urgent"))
        self.assertIn("won't message", T.ack_for("don't text me", "quiet"))
        self.assertIn("off", T.ack_for("I'm back", "back"))


class PhoneIntentTests(unittest.TestCase):
    TABLE = [
        ("stop", ("stop", "stop")),
        ("Please stop everything", ("stop", "stop everything")),
        ("remember that my locker is 42", ("remember", "my locker is 42")),
        ("Jarvis, don't forget the milk", ("remember", "the milk")),
        ("note: buy milk", ("remember", "buy milk")),
        ("status", ("status", "status")),
        ("what are you doing?", ("status", "what are you doing?")),
        ("answer: the blue one", ("answer", "the blue one")),
        ("can you open notepad?", ("task", "open notepad?")),
        ("find the budget file", ("task", "find the budget file")),
        ("look up flights to Delhi", ("task", "look up flights to Delhi")),
        ("what's the weather in Chennai?", ("task", "what's the weather in Chennai?")),
        ("What is a black hole?", ("ask", "What is a black hole?")),
        ("notepad is slow today", ("ask", "notepad is slow today")),
        ("will you marry me", ("ask", "marry me")),
    ]

    def test_table(self):
        for text, want in self.TABLE:
            self.assertEqual(T.phone_intent(text), want, text)


class PhoneTextTests(unittest.TestCase):
    def test_memory_receipt(self):
        s = ("[MEMORY_SAVED id=m1 verified=yes at=2026-10-01T10:00] Stored preference/coffee = black. Confirm in "
             "ONE short sentence what you will remember, in the user's language.")
        self.assertEqual(T.phone_text(s), "Stored preference/coffee = black.")

    def test_alert_and_selfcheck(self):
        self.assertEqual(T.phone_text("[SYSTEM_ALERT] RAM is at 97% — nearly exhausted. Warn the user in their "
                                      "language and suggest freeing memory."), "RAM is at 97% — nearly exhausted.")
        self.assertEqual(T.phone_text("Opened Notepad.\n[SELF-CHECK: verified — window found]"),
                         "Opened Notepad. (Self-check: verified — window found)")
        self.assertEqual(T.phone_text("[MEMORY_NOT_SAVED] It looks like a password. Tell the user plainly it was "
                                      "NOT saved and why. Never say it was remembered."), "It looks like a password.")


class PairingTests(unittest.TestCase):
    def test_code_shape_expiry_and_tries(self):
        c = Clock(1000.0)
        pc = T.PairingCode(c)
        code = pc.mint()
        self.assertEqual(len(code), 6)
        self.assertTrue(all(ch in T._ALPHABET for ch in code))
        self.assertFalse(set("O0I1L") & set(code))
        for _ in range(4):
            self.assertFalse(pc.check("ZZZZZZ"))
        self.assertTrue(pc.check(code.lower()), "case-insensitive, still valid after 4 misses")
        self.assertIsNone(pc.check(code), "one use only")
        code = pc.mint()
        for _ in range(5):
            pc.check("ZZZZZZ")
        self.assertIsNone(pc.check(code), "five wrong guesses kill the code")
        code = pc.mint()
        c.t += T.CODE_TTL + 1
        self.assertIsNone(pc.check(code), "expired after ten minutes")


class PocketTests(unittest.TestCase):
    def test_stranger_refused_once_then_silence_and_no_answers(self):
        seen = []
        p, http, _, _ = make_pocket(handle=lambda t: seen.append(t) or "secret answer")
        self.assertEqual(p.handle_update(msg(7, "hello", uid=1)), "Sorry — this is a private assistant.")
        self.assertIsNone(p.handle_update(msg(7, "what's my schedule", uid=2)))
        self.assertIsNone(p.handle_update(msg(7, voice={"file_id": "x"}, uid=3)))
        self.assertEqual(seen, [], "a stranger never reaches the brain")
        self.assertEqual(http.texts(7), ["Sorry — this is a private assistant."])
        self.assertNotIn("getFile", [c[0] for c in http.calls], "no free transcription for strangers")

    def test_pairing_then_commands_reach_the_brain(self):
        seen = []
        p, http, logs, _ = make_pocket(handle=lambda t: seen.append(t) or "ok from desk")
        p.handle_update(msg(42, "WRONG1", uid=1))          # a typo first: refused once …
        pair(p, 42)                                         # … but the right code still pairs
        self.assertTrue(p.paired)
        self.assertEqual(p.handle_update(msg(42, "find my notes", uid=2)), "ok from desk")
        self.assertEqual(seen, ["find my notes"])
        self.assertEqual(p.handle_update(msg(99, "hi", uid=3)), "Sorry — this is a private assistant.")
        self.assertIsNone(p.handle_update(msg(-5, "hi", uid=4, ctype="group")), "groups are ignored")
        self.assertNotIn(TOKEN, json.dumps(logs))
        self.assertTrue(all(TOKEN.split(":")[1] not in str(c[1]) for c in http.calls))

    def test_stale_messages_are_not_acted_on(self):
        seen = []
        p, http, _, clock = make_pocket(handle=lambda t: seen.append(t) or "x")
        pair(p)
        old = int(clock.t - T.STALE_S - 60)
        r = p.handle_update(msg(42, "delete the folder", uid=5, date=old))
        self.assertIn("offline", r)
        self.assertIsNone(p.handle_update(msg(42, "and this", uid=6, date=old)), "one notice, not a flood")
        self.assertEqual(seen, [])

    def test_voice_note_is_transcribed_on_the_pc(self):
        heard = []
        p, http, _, _ = make_pocket(handle=lambda t: heard.append(t) or "answered",
                                    transcribe=lambda path: "open the budget" if Path(path).exists() else "")
        pair(p)
        self.assertEqual(p.handle_update(msg(42, uid=8, voice={"file_id": "abc", "file_size": 2000})), "answered")
        self.assertEqual(heard, ["open the budget"])
        methods = [c[0] for c in http.calls]
        self.assertIn("getFile", methods)
        self.assertIn("download", methods)

    def test_voice_note_without_whisper_says_so(self):
        p, http, _, _ = make_pocket()
        pair(p)
        self.assertIn("couldn't transcribe", p.handle_update(msg(42, uid=9, voice={"file_id": "abc"})))

    def test_standing_order_arms_and_sends_nothing_else(self):
        p, http, _, _ = make_pocket(handle=lambda t: self.fail("must not reach the brain"))
        pair(p)
        before = len(http.texts())
        r = p.handle_update(msg(42, "I'm going out, only message me if it's urgent", uid=10))
        self.assertIn("urgent", r)
        self.assertTrue(p.armed)
        self.assertEqual(len(http.texts()), before + 1, "only the acknowledgement")
        p.handle_update(msg(42, "I'm back", uid=11))
        self.assertFalse(p.armed)

    def test_urgent_line_cooldown_and_dedupe(self):
        p, http, _, clock = make_pocket()
        pair(p)
        self.assertFalse(p.urgent("CPU 99%", card="a1"), "not armed → nothing")
        p.arm(True)
        self.assertTrue(p.urgent("CPU 99%", card="a1"))
        self.assertFalse(p.urgent("RAM 97%", card="a2"), "ten-minute cooldown")
        clock.t += T.URGENT_COOLDOWN + 1
        self.assertFalse(p.urgent("CPU 99%", card="a1"), "the same card never twice")
        self.assertTrue(p.urgent("RAM 97%", card="a2"))
        self.assertEqual(sum(1 for t in http.texts() if t.startswith("URGENT")), 2)

    def test_send_dedupe_and_failure_is_reported(self):
        p, http, logs, _ = make_pocket()
        pair(p)
        self.assertTrue(p.send("hello", card="c1"))
        self.assertFalse(p.send("hello", card="c1"))
        http.fail.add("sendMessage")
        self.assertFalse(p.send("again", card="c2"))
        self.assertTrue(any("send failed" in m for m in logs))
        self.assertNotIn(TOKEN, " ".join(logs))

    def test_transport_error_never_leaks_the_token(self):
        logs = []

        class Boom:
            def post(self, url, **kw):
                raise ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")
        p = T.TelegramPocket(token_fn=lambda: TOKEN, handle_text=lambda t: "", http=Boom(), log=logs.append,
                             state_path=Path(tempfile.mkdtemp()) / "t.json")
        with self.assertRaises(RuntimeError) as cm:
            p.poll_once(timeout=0)
        self.assertNotIn(TOKEN, str(cm.exception))
        p._state["chat"] = 42
        self.assertFalse(p.send("x"))
        self.assertNotIn(TOKEN, " ".join(logs))

    def test_poll_advances_offset_before_handling(self):
        p, http, _, _ = make_pocket(handle=lambda t: "fine")
        pair(p)
        http.updates = [msg(42, "status?", uid=100), msg(42, "again", uid=101)]
        self.assertEqual(p.poll_once(timeout=0), 2)
        self.assertEqual(p._state["offset"], 102)
        self.assertEqual(json.loads(p.path.read_bytes().decode())["offset"], 102)
        self.assertIn(b"\r\n", p.path.read_bytes(), "state file is CRLF like the rest of the project")

    def test_pair_shows_code_only_on_the_hud(self):
        shown = []
        p, http, _, _ = make_pocket(on_code=shown.append)
        ok, sentence = p.pair()
        self.assertTrue(ok)
        self.assertEqual(len(shown), 1)
        self.assertNotIn(shown[0], sentence, "the model never sees the code")
        p2, _, _, _ = make_pocket(on_code=lambda c: 1 / 0)
        ok, sentence = p2.pair()
        self.assertFalse(ok)
        self.assertEqual(p2.pairing.code, "", "a code nobody saw is not left live")


class ReminderRelayTests(unittest.TestCase):
    def test_generated_script_leaves_a_receipt(self):
        """Runs the real generated reminder script, with only the notification
        side effects (pop-up, beep, notify-send) stubbed out."""
        import builtins
        from actions import reminder as R
        tmp = Path(tempfile.mkdtemp())
        orig = R._scripts_dir
        R._scripts_dir = lambda: tmp
        try:
            path = R._write_notify_script("JARVISReminder_test", "call the bank", "linux")
        finally:
            R._scripts_dir = orig
        real_import, ran = builtins.__import__, []

        def fake_import(name, *a, **k):
            if name in ("plyer", "win10toast", "winsound"):
                raise ImportError(name)
            if name == "subprocess":
                return SimpleNamespace(run=lambda *x, **y: ran.append(x))
            return real_import(name, *a, **k)
        g = {"__file__": str(path), "__name__": "__main__",
             "__builtins__": dict(vars(builtins), __import__=fake_import)}
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), g)
        rec = json.loads((tmp / "fired.jsonl").read_text(encoding="utf-8").strip())
        self.assertEqual((rec["task"], rec["message"]), ("JARVISReminder_test", "call the bank"))
        self.assertTrue(ran, "the desktop notification was still attempted")
        self.assertFalse(path.exists(), "the script still deletes itself")

    def test_relay_only_while_armed_and_only_once(self):
        log = Path(tempfile.mkdtemp()) / "fired.jsonl"
        p, http, _, clock = make_pocket(reminder_log=log)
        pair(p)
        log.write_text(json.dumps({"task": "old", "message": "old one", "at": clock.t}) + "\n", encoding="utf-8")
        self.assertEqual(p.relay_reminders(), 0, "first run only marks the end — history is never replayed")

        def add(text):
            with open(log, "a", encoding="utf-8") as f:
                f.write(text)
        add(json.dumps({"task": "r1", "message": "call the bank", "at": clock.t}) + "\n")
        self.assertEqual(p.relay_reminders(), 0, "at the desk the pop-up on the PC is enough")
        p.arm(True)
        add(json.dumps({"task": "r2", "message": "pay rent", "at": clock.t}) + "\r\n")
        add(json.dumps({"task": "r3", "message": "stale", "at": clock.t - 3 * T.STALE_S}) + "\n")
        add('{"task": "r4", "message": "half')
        self.assertEqual(p.relay_reminders(), 1)
        self.assertEqual(http.texts()[-1], "Reminder: pay rent")
        self.assertEqual(p.relay_reminders(), 0, "never twice; a half-written line waits")
        add('-written", "at": %f}\n' % clock.t)
        self.assertEqual(p.relay_reminders(), 1)
        self.assertEqual(http.texts()[-1], "Reminder: half-written")
        log.write_text("", encoding="utf-8")
        add(json.dumps({"task": "r5", "message": "after a reset", "at": clock.t}) + "\n")
        self.assertEqual(p.relay_reminders(), 1, "a replaced file is read from the start")

    def test_first_reminder_ever_is_not_lost(self):
        log = Path(tempfile.mkdtemp()) / "fired.jsonl"           # no reminder has fired yet
        p, http, _, clock = make_pocket(reminder_log=log)
        pair(p)
        p.arm(True)
        self.assertEqual(p.relay_reminders(), 0)
        log.write_text(json.dumps({"task": "r1", "message": "first ever", "at": clock.t}) + "\n", encoding="utf-8")
        self.assertEqual(p.relay_reminders(), 1)
        self.assertEqual(http.texts()[-1], "Reminder: first ever")


# ═════════════════════════════════════════════════════════════════════════════
class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # never touch the real long-term memory file ("remember that…" writes through it)
        from memory import memory_manager
        cls._tmp = tempfile.TemporaryDirectory()
        cls._mem_path = memory_manager.MEMORY_PATH
        memory_manager.MEMORY_PATH = Path(cls._tmp.name) / "memory" / "long_term.json"
        from core.jarvis_core import JarvisCore
        cls.core = JarvisCore(persist=False, use_llm_planner=False)

    @classmethod
    def tearDownClass(cls):
        from memory import memory_manager
        memory_manager.MEMORY_PATH = cls._mem_path
        cls._tmp.cleanup()

    def setUp(self):
        self.core.kill.release(by="test")
        self.bridge = self.core.pocket_bridge
        self.pocket, self.http, self.logs, self.clock = make_pocket(handle=self.bridge.handle_text,
                                                                    brief=self.bridge.desk_status)
        self.bridge.pocket = self.pocket
        pair(self.pocket)

    def tearDown(self):
        self.bridge.pocket = None
        self.core.kill.release(by="test")

    def test_tool_is_registered_low_risk(self):
        reg = self.core.registry
        self.assertTrue(reg.has("pocket"))
        self.assertEqual(self.core.permissions.decide("pocket", {"action": "status"}, {"origin": "voice"}).decision.name,
                         "ALLOW")
        self.assertEqual(self.core.permissions.decide("pocket", {"action": "away"}, {"origin": "voice"}).decision.name,
                         "ALLOW")

    def test_phone_origin_is_stricter_and_cannot_lift_the_stop(self):
        d = self.core.permissions.decide("file_controller", {"action": "move", "path": "a", "destination": "b"},
                                         {"origin": "telegram"})
        d_voice = self.core.permissions.decide("file_controller", {"action": "move", "path": "a", "destination": "b"},
                                               {"origin": "voice"})
        self.assertEqual(d_voice.risk.name, "MEDIUM_RISK")
        self.assertEqual(d_voice.decision.name, "ALLOW", "at the desk a file move just runs")
        self.assertEqual(d.decision.name, "CONFIRM", "from the phone it waits for the HUD")
        self.core.kill.engage("test")
        self.core.orchestrator._release_on_user_command("telegram")
        self.assertTrue(self.core.kill.engaged(), "a phone message never lifts the desk's emergency stop")
        r = self.bridge.handle_text("open notepad")
        self.assertIn("emergency stop", r)

    def test_task_from_phone_uses_telegram_origin(self):
        sub = []
        real = self.core.orchestrator
        self.core.orchestrator = SimpleNamespace(
            submit=lambda text, origin, language: sub.append((text, origin)) or SimpleNamespace(id="T9"),
            status=real.status, answer=real.answer)
        try:
            r = self.bridge.handle_text("Jarvis, find the budget file")
        finally:
            self.core.orchestrator = real
        self.assertEqual(sub, [("find the budget file", "telegram")])
        self.assertIn("task T9", r)

    def test_task_events_reach_the_phone_once(self):
        task = SimpleNamespace(id="T1", origin="telegram", request="find the budget file")
        self.bridge.on_task_event(task, "completed", "Found it.\n[SELF-CHECK: verified — file exists]")
        self.bridge.on_task_event(task, "completed", "Found it.\n[SELF-CHECK: verified — file exists]")
        self.bridge.on_task_event(task, "planned", "Plan: 1 step")
        texts = [t for t in self.http.texts() if t.startswith("Task T1")]
        self.assertEqual(texts, ["Task T1 finished (find the budget file): Found it. "
                                 "(Self-check: verified — file exists)"])
        self.bridge.on_task_event(task, "permission", "Delete x? Please confirm on the HUD.")
        self.assertIn("can't approve", self.http.texts()[-1])

    def test_desk_tasks_only_reach_the_phone_as_urgent(self):
        desk = SimpleNamespace(id="T2", origin="voice", request="sync photos")
        self.bridge.on_task_event(desk, "failed", "Disk full.")
        self.assertFalse(any("T2" in t for t in self.http.texts()), "not armed → nothing")
        self.pocket.arm(True)
        self.bridge.on_task_event(desk, "completed", "Done.")
        self.bridge.on_task_event(desk, "failed", "Disk full.")
        urgent = [t for t in self.http.texts() if t.startswith("URGENT")]
        self.assertEqual(len(urgent), 1)
        self.assertIn("T2", urgent[0])

    def test_system_alert_is_cleaned_and_rate_limited(self):
        self.pocket.arm(True)
        self.assertTrue(self.core.pocket_urgent("[SYSTEM_ALERT] RAM is at 97% — nearly exhausted. Warn the user in "
                                                "their language and suggest freeing memory."))
        self.assertEqual(self.http.texts()[-1], "URGENT — RAM is at 97% — nearly exhausted.")
        self.assertFalse(self.core.pocket_urgent("[SYSTEM_ALERT] CPU 99%."))

    def test_remember_status_answer_ask_stop(self):
        from memory import memory_manager
        real = Path(self._mem_path).resolve()
        self.assertNotEqual(Path(memory_manager.MEMORY_PATH).resolve(), real)
        before = real.read_bytes() if real.exists() else None
        r = self.bridge.handle_text("remember that my locker number is 42")
        self.assertTrue(r.startswith("Saved."), r)
        self.assertNotIn("the user", r)
        self.assertIn("locker_number", json.loads(Path(memory_manager.MEMORY_PATH).read_bytes())["notes"])
        self.assertEqual(real.read_bytes() if real.exists() else None, before, "real memory untouched")
        self.assertIn("not working", self.bridge.handle_text("status").lower())
        self.assertIn("No task from here", self.bridge.handle_text("answer: blue"))
        real = self.core.think
        self.core.think = lambda a: f"About {a['question']}.\n[THINK: quick model. Speak a concise version.]"
        try:
            self.assertEqual(self.bridge.handle_text("what is a black hole?"), "About what is a black hole?.")
        finally:
            self.core.think = real
        r = self.bridge.handle_text("stop")
        self.assertTrue(r.startswith("Stopped"))
        self.assertTrue(self.core.kill.engaged(), "stopping from the phone always works")

    def test_desk_standing_order_backstop(self):
        line = self.core.pocket_standing_order("I'm heading out, only message me if it's urgent")
        self.assertIn("ON", line)
        self.assertTrue(self.pocket.armed)
        self.assertEqual(self.core.pocket_standing_order("only text me if it's urgent"), "", "no change, no log")
        self.assertIn("off", self.core.pocket_standing_order("I'm back"))
        n = len(self.http.texts())
        self.assertIn("Brief sent", self.core.pocket_standing_order("text me now"))
        self.assertEqual(self.core.pocket_standing_order("text me now"), "", "one brief a minute")
        self.assertEqual(len(self.http.texts()), n + 1)
        self.assertTrue(self.http.texts()[-1].startswith("Desk brief"))

    def test_voice_tool_actions(self):
        shown = []
        self.pocket.on_code = shown.append
        r = self.core.pocket({"action": "pair"})
        self.assertIn("[POCKET_CODE_SHOWN]", r)
        self.assertNotIn(shown[0], r)
        self.assertIn("[POCKET_ARMED]", self.core.pocket({"action": "away"}))
        self.assertTrue(self.pocket.armed)
        self.assertIn("[POCKET_SENT]", self.core.pocket({"action": "send", "text": "leaving now"}))
        self.http.fail.add("sendMessage")
        self.clock.t += 61
        r = self.core.pocket({"action": "send"})
        self.assertIn("NOT sent", r)
        self.assertNotIn("[POCKET_SENT]", r)
        self.assertIn("[POCKET_UNPAIRED]", self.core.pocket({"action": "unpair"}))
        self.assertFalse(self.pocket.paired)
        self.assertFalse(self.pocket.armed, "unpairing also turns the urgent line off")

    def test_not_set_up_is_said_plainly(self):
        self.bridge.pocket = None
        self.assertIn("isn't set up", self.core.pocket({"action": "status"}))
        self.assertIn("isn't set up", self.core.pocket({"action": "pair"}))

    def test_start_is_dormant_without_a_token(self):
        from core import secret_store
        import os
        saved = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        tmp = Path(tempfile.mkdtemp())
        secret_store.set_base_dir(tmp)
        try:
            self.bridge.pocket = None
            self.assertEqual(self.core.start_pocket(state_path=tmp / "t.json", http=FakeHttp()), "")
            self.assertFalse(self.bridge.pocket.status()["listening"])
        finally:
            secret_store.set_base_dir(None)
            if saved is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = saved

    def test_direct_tools_return_sentences_not_reprs(self):
        out = self.core.learning({"action": "list"})
        self.assertNotIn("ToolResult(", out)
        self.assertNotIn("ToolResult(", self.core.reference({"action": "list"}))


class WiringTests(unittest.TestCase):
    def _src(self, rel):
        return (ROOT / rel).read_bytes().decode("utf-8")

    def test_main_and_ui(self):
        m = self._src("main.py")
        for s in ('"name": "pocket"', 'elif name == "pocket":', "self._core.start_pocket",
                  "self._core.pocket_urgent, alert", "self._core.pocket_standing_order, full_in",
                  'if cmd == "/pocket":'):
            self.assertIn(s, m)
        self.assertIn('("/pocket", "PHONE LINE")', self._src("ui_mission.py"))
        self.assertIn("pocket action='pair'", self._src("core/prompt.txt"))
        for rel in ("core/telegram_bot.py", "tests/test_phase7_pocket.py"):
            raw = (ROOT / rel).read_bytes()
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), rel + " must be CRLF")


if __name__ == "__main__":
    unittest.main()
