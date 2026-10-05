"""
tools/live_text_probe.py - which way of sending TYPED TEXT (and a screen frame)
a Gemini Live voice model accepts in the middle of a conversation.

Why: on gemini-3.8-live a typed command closed the session with
"1007 Request contains an invalid argument", and every reconnect that replayed
the session-resumption handle then failed with "1011 Internal error" - the
idle/thinking loop. JARVIS streams the microphone (realtime input) all the
time, so each variant first streams 1.5 s of silence, exactly like the mic.

    python tools/live_text_probe.py                      # gemini-3.8-live
    python tools/live_text_probe.py --model models/gemini-2.5-flash-native-audio-preview-12-2025
    python tools/live_text_probe.py --only H,I          # just the given variants
    (or double-click run_live_text_probe.bat)

Result on 2 Oct 2026 (gemini-3.8-live, google-genai 2.22.0): a client turn
WITHOUT "role": "user" -> 1007 (A, E, G); resuming that session -> 1011 (B);
role "user" text / image and realtime text all answer (C, D, H), and spoken
and typed turns mix in one session (I, J).

Writes tools/live_text_probe.log. Never prints a key.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

Q1 = "What is two plus two? Answer in three words."
Q2 = "And what is three plus three? Answer in three words."
TOOLS = [{"name": "system_status", "description": "CPU, RAM, GPU usage of this PC.",
          "parameters": {"type": "OBJECT", "properties": {}, "required": []}}]
SILENCE = b"\x00\x00" * 640          # 40 ms at 16 kHz mono int16


def _speech_pcm(text: str) -> bytes:
    """16 kHz mono PCM of `text` spoken by the Windows SAPI voice (same as tools/latency_bench.py)."""
    import os
    import tempfile
    import wave
    if os.name != "nt":
        return b""
    try:
        import comtypes.client as cc
        path = os.path.join(tempfile.gettempdir(), "jarvis_text_probe_q.wav")
        voice = cc.CreateObject("SAPI.SpVoice")
        stream = cc.CreateObject("SAPI.SpFileStream")
        fmt = cc.CreateObject("SAPI.SpAudioFormat")
        fmt.Type = 18                      # SAFT16kHz16BitMono
        stream.Format = fmt
        stream.Open(path, 3, False)
        voice.AudioOutputStream = stream
        voice.Speak(text)
        stream.Close()
        with wave.open(path, "rb") as w:
            if w.getframerate() != 16000 or w.getnchannels() != 1:
                return b""
            return w.readframes(w.getnframes())
    except Exception as e:
        print(f"(speech synthesis unavailable: {type(e).__name__})")
        return b""


def _png() -> bytes:
    try:
        from PIL import Image
        im = Image.new("RGB", (320, 200), (220, 20, 20))
        b = io.BytesIO()
        im.save(b, "PNG")
        return b.getvalue()
    except Exception:
        return b""


class Run:
    def __init__(self, s, log, t0):
        self.s, self.log, self.t0 = s, log, t0
        self.audio = 0
        self.turns = 0
        self.said = []
        self.heard = []
        self.error = ""
        self.handle = None
        self.closed = False

    def ev(self, msg):
        self.log(f"    {time.monotonic() - self.t0:6.2f}s  {msg}")

    async def reader(self):
        from google.genai import types
        try:
            while True:
                async for r in self.s.receive():
                    sru = getattr(r, "session_resumption_update", None)
                    if sru is not None and getattr(sru, "resumable", False) and getattr(sru, "new_handle", None):
                        self.handle = sru.new_handle
                    if r.data:
                        self.audio += len(r.data)
                    sc = r.server_content
                    if sc is not None:
                        if sc.output_transcription and sc.output_transcription.text:
                            self.said.append(sc.output_transcription.text)
                        if sc.input_transcription and sc.input_transcription.text:
                            self.heard.append(sc.input_transcription.text)
                        if sc.turn_complete:
                            self.turns += 1
                            self.ev(f"turn_complete (audio so far {self.audio} B; said {''.join(self.said)[-80:]!r})")
                    if r.tool_call:
                        self.ev(f"tool_call {[fc.name for fc in r.tool_call.function_calls]}")
                        await self.s.send_tool_response(function_responses=[
                            types.FunctionResponse(id=fc.id, name=fc.name, response={"result": "CPU 10%"})
                            for fc in r.tool_call.function_calls])
        except Exception as e:
            from core.secret_store import redact
            self.error = redact(f"{type(e).__name__}: {e}")[:220]
            self.closed = True
            self.ev(f"SESSION CLOSED: {self.error}")

    async def mic(self, seconds):
        from google.genai import types
        n = int(seconds / 0.04)
        for _ in range(n):
            if self.closed:
                return
            await self.s.send_realtime_input(audio=types.Blob(data=SILENCE, mime_type="audio/pcm;rate=16000"))
            await asyncio.sleep(0.04)

    async def speech(self, pcm, timeout=15.0):
        from google.genai import types
        a0, t0 = self.audio, self.turns
        for i in range(0, len(pcm), 1280):
            if self.closed:
                return False
            await self.s.send_realtime_input(audio=types.Blob(data=pcm[i:i + 1280], mime_type="audio/pcm;rate=16000"))
            await asyncio.sleep(0.04)
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.closed:
            if self.audio > a0 and self.turns > t0:
                self.ev(f"heard: {''.join(self.heard)[-60:]!r}")
                return True
            await self.s.send_realtime_input(audio=types.Blob(data=SILENCE, mime_type="audio/pcm;rate=16000"))
            await asyncio.sleep(0.04)
        return False

    async def answer(self, timeout=15.0):
        """Wait for a spoken answer to finish (audio, then turn_complete) or a close."""
        a0, t0 = self.audio, self.turns
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.closed:
            if self.audio > a0 and self.turns > t0:
                return True
            await asyncio.sleep(0.05)
        return self.audio > a0 and not self.closed


def _config(types, handle=None):
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"], output_audio_transcription={}, input_audio_transcription={},
        system_instruction="You are JARVIS, a voice assistant. Be very brief.",
        tools=[{"function_declarations": TOOLS}],
        session_resumption=types.SessionResumptionConfig(handle=handle),
        context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon"))))


async def variant(client, model, name, plan, log, handle=None):
    from google.genai import types
    log(f"== {name}")
    t0 = time.monotonic()
    res = {"name": name, "ok": False, "detail": "", "handle": None}
    try:
        async with client.aio.live.connect(model=model, config=_config(types, handle)) as s:
            run = Run(s, log, t0)
            run.ev("connected")
            rd = asyncio.create_task(run.reader())
            ok = True
            for step in plan:
                kind = step[0]
                if run.closed:
                    break
                try:
                    if kind == "mic":
                        await run.mic(step[1])
                    elif kind == "cc":            # send_client_content (role or no role)
                        turns = {"parts": [{"text": step[1]}]}
                        if step[2]:
                            turns["role"] = "user"
                        await s.send_client_content(turns=turns, turn_complete=True)
                        run.ev(f"sent client_content text (role={'user' if step[2] else 'none'})")
                    elif kind == "speech":
                        got = await run.speech(step[1], 15.0)
                        run.ev(f"spoken answer: {'YES' if got else 'NO'}")
                        ok = ok and got
                    elif kind == "cc_image_role":
                        import base64
                        await s.send_client_content(turns={"role": "user", "parts": [
                            {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(step[1]).decode()}},
                            {"text": step[2]}]}, turn_complete=True)
                        run.ev("sent client_content image + text (role=user)")
                    elif kind == "cc_image":
                        import base64
                        await s.send_client_content(turns={"parts": [
                            {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(step[1]).decode()}},
                            {"text": step[2]}]}, turn_complete=True)
                        run.ev("sent client_content image + text")
                    elif kind == "rt_text":
                        await s.send_realtime_input(text=step[1])
                        run.ev("sent realtime_input text")
                    elif kind == "rt_image":
                        await s.send_realtime_input(video=types.Blob(data=step[1], mime_type="image/png"))
                        run.ev("sent realtime_input image")
                    elif kind == "answer":
                        got = await run.answer(step[1] if len(step) > 1 else 15.0)
                        run.ev(f"answer: {'YES' if got else 'NO'}")
                        ok = ok and got
                    elif kind == "wait":
                        await asyncio.sleep(step[1])
                except Exception as e:
                    from core.secret_store import redact
                    run.ev(f"send failed: {redact(f'{type(e).__name__}: {e}')[:200]}")
                    ok = False
                    break
            await asyncio.sleep(0.5)
            res["handle"] = run.handle
            res["ok"] = ok and not run.closed
            res["detail"] = run.error or ("".join(run.said)[-120:])
            rd.cancel()
    except Exception as e:
        from core.secret_store import redact
        res["detail"] = redact(f"connect: {type(e).__name__}: {e}")[:220]
        log(f"    connect failed: {res['detail']}")
    log(f"   RESULT {name}: {'PASS' if res['ok'] else 'FAIL'} - {res['detail']}")
    return res


async def main_async(model, log, only, speech):
    from google import genai
    from core.secret_store import get_secret
    import google.genai as gg
    log(f"google-genai {getattr(gg, '__version__', '?')}  model {model}")
    api = "v1alpha" if "native-audio" in model else "v1beta"
    client = genai.Client(api_key=get_secret("GOOGLE_API_KEY"), http_options={"api_version": api})
    png = _png()
    want = (lambda k: not only or k in only)
    results = []
    r1 = None
    if want("A") or want("B"):
        # what JARVIS did: mic audio, then client_content text without a role
        r1 = await variant(client, model, "A mic -> client_content (no role)  [old JARVIS typed path]",
                           [("mic", 1.5), ("cc", Q1, False), ("answer", 15)], log)
        results.append(r1)
    if want("B") and r1 and r1["handle"]:
        results.append(await variant(client, model, "B resume the handle of A",
                                     [("mic", 1.0), ("rt_text", Q2), ("answer", 15)], log, handle=r1["handle"]))
    if want("C"):
        results.append(await variant(client, model, "C mic -> client_content (role=user)",
                                     [("mic", 1.5), ("cc", Q1, True), ("answer", 15)], log))
    if want("D"):
        results.append(await variant(client, model, "D mic -> realtime_input text, twice, mic again",
                                     [("mic", 1.5), ("rt_text", Q1), ("answer", 15), ("mic", 1.0),
                                      ("rt_text", Q2), ("answer", 15), ("mic", 1.5)], log))
    if want("E"):
        results.append(await variant(client, model, "E client_content (no role) first, no mic before",
                                     [("cc", Q1, False), ("answer", 15), ("mic", 1.0)], log))
    if png and want("F"):
        results.append(await variant(client, model, "F mic -> realtime image + realtime text",
                                     [("mic", 1.0), ("rt_image", png),
                                      ("rt_text", "What colour fills the image I just sent? One word."),
                                      ("answer", 15), ("mic", 1.0)], log))
    if png and want("G"):
        results.append(await variant(client, model, "G mic -> client_content image + text  [old vision path]",
                                     [("mic", 1.0), ("cc_image", png, "What colour fills this image? One word."),
                                      ("answer", 15)], log))
    if png and want("H"):
        results.append(await variant(client, model, "H mic -> client_content image + text (role=user)  [fixed vision path]",
                                     [("mic", 1.0), ("cc_image_role", png, "What colour fills this image? One word."),
                                      ("answer", 15), ("mic", 1.0)], log))
    if want("I"):
        sp1, sp2 = speech.get("sp1", b""), speech.get("sp2", b"")
        if not (sp1 and sp2):
            log("== I skipped: no Windows speech synthesis")
        else:
            results.append(await variant(client, model,
                                         "I mixed: SPOKEN -> typed (role=user) -> SPOKEN -> typed tool question",
                                         [("mic", 1.0), ("speech", sp1), ("mic", 0.5),
                                          ("cc", Q1, True), ("answer", 15), ("mic", 0.5),
                                          ("speech", sp2), ("mic", 0.5),
                                          ("cc", "How busy is my computer right now? Check the system status.", True),
                                          ("answer", 20), ("mic", 1.0)], log))
    if want("J"):
        sp3 = speech.get("sp3", b"")
        if sp3:
            results.append(await variant(client, model, "J SPOKEN only (baseline)",
                                         [("mic", 1.0), ("speech", sp3), ("mic", 1.0)], log))
    # A, B, E, G send the OLD formats (no role / resuming a closed session):
    # they are the proof of the bug and are EXPECTED to be rejected by the server.
    expect_fail = {"A", "B", "E", "G"}
    log("== summary  (A, B, E, G = old formats, expected to FAIL; all others must PASS)")
    bad = 0
    for r in results:
        good = (not r["ok"]) if r["name"][:1] in expect_fail else r["ok"]
        bad += 0 if good else 1
        log(f"  {'PASS' if r['ok'] else 'FAIL'}  {'as expected' if good else 'UNEXPECTED'}  {r['name']}")
    log("ALL AS EXPECTED" if not bad else f"{bad} UNEXPECTED RESULT(S)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/gemini-3.8-live")
    ap.add_argument("--only", default="", help="comma list of variants, e.g. H,I (default: all)")
    a = ap.parse_args(argv)
    only = {x.strip().upper() for x in a.only.split(",") if x.strip()}
    model = a.model if a.model.startswith("models/") else f"models/{a.model}"
    lines = []

    def log(s):
        lines.append(s)
        print(s, flush=True)
    log(f"Live text probe - {time.strftime('%Y-%m-%d %H:%M')}")
    # Speech is synthesised BEFORE the event loop starts (as tools/latency_bench.py
    # does): SAPI/COM on the event-loop thread stalled the websocket.
    speech = {}
    if not only or {"I", "J"} & only:
        speech = {"sp1": _speech_pcm("JARVIS, what is five plus five?"),
                  "sp2": _speech_pcm("JARVIS, what is the capital of France?"),
                  "sp3": _speech_pcm("JARVIS, what is seven plus one?")}
        log("speech probes: " + ", ".join(f"{k} {len(v) / 32000:.1f}s" for k, v in speech.items()))
    try:
        asyncio.run(main_async(model, log, only, speech))
    finally:
        (ROOT / "tools" / "live_text_probe.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
