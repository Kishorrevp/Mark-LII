"""
tools/latency_bench.py — measure how fast each Gemini Live voice model answers
on YOUR key, with JARVIS's real system prompt and tool declarations.

    python tools/latency_bench.py                       # default model set, 2 rounds
    python tools/latency_bench.py --models gemini-3.8-live --rounds 3
    python tools/latency_bench.py --lean                # also try the lean tool set

For every model it opens a real Live session (audio out, transcription on,
JARVIS's prompt + tools) and measures:

    connect          session set-up time
    text→audio       a typed question → first audio byte back
    tool call        a question that needs a tool → the tool call arrives
    tool→audio       tool result sent → first audio byte back
    speech→audio     a SPOKEN question (Windows SAPI voice, streamed in 40 ms
                     chunks at real-time pace, then silence) → first audio
                     byte. Includes the server's end-of-turn wait — the delay
                     you actually feel.

Prints a table and writes a value-free copy to tools/latency_bench.log.
Costs a handful of free-tier Live requests. Never prints a key.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_MODELS = ["models/gemini-3.8-live", "models/gemini-2.5-flash-native-audio-preview-12-2025",
                  "models/gemini-3.1-flash-live-preview"]
CHUNK = 640                  # 40 ms at 16 kHz
RATE = 16000


def _tool_decls(lean: bool = False) -> list:
    tree = ast.parse((ROOT / "main.py").read_bytes().decode("utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "TOOL_DECLARATIONS" for t in node.targets):
            decl = ast.literal_eval(node.value)
            break
    else:
        return []
    if lean:
        try:
            from core.live_tools import select_declarations
            return select_declarations(decl, profile="lean")
        except Exception:
            pass
    return decl


def _system_text() -> str:
    from core import persona
    tmpl = (ROOT / "core" / "prompt.txt").read_bytes().decode("utf-8")
    return tmpl.replace("{{PERSONALITY_BLOCK}}", persona.compose("serious", surface="voice"))


def _speech_pcm(text: str) -> bytes:
    """16 kHz mono PCM of `text` spoken by the Windows SAPI voice ('' elsewhere)."""
    if os.name != "nt":
        return b""
    try:
        import comtypes.client as cc
        path = os.path.join(tempfile.gettempdir(), "jarvis_bench_q.wav")
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
            if w.getframerate() != RATE or w.getnchannels() != 1:
                return b""
            return w.readframes(w.getnframes())
    except Exception as e:
        print(f"(speech synthesis unavailable: {type(e).__name__})")
        return b""


def _config(types, model: str, tools: list, system: str, variant: str = ""):
    cfg = dict(response_modalities=["AUDIO"], output_audio_transcription={}, input_audio_transcription={},
               system_instruction=system, tools=[{"function_declarations": tools}] if tools else None,
               session_resumption=types.SessionResumptionConfig(handle=None),
               context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
               speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                   prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon"))))
    if "native-audio" in model and "plain" not in variant:   # what production sends to the 2.5 model
        cfg["enable_affective_dialog"] = True
        cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)
    cfg = {k: v for k, v in cfg.items() if v is not None}
    return types.LiveConnectConfig(**cfg)


async def _until(session, *, want: str, t0: float, timeout: float = 25.0, stop_on_complete: bool = True):
    """Read the stream until `want` ('audio' | 'tool' | 'done'). Returns (seconds, extra).
    Hard deadline: a server that sends nothing at all can't hang the bench.
    stop_on_complete=False keeps waiting past a turn_complete (models with
    asynchronous function calling close the calling turn first, then answer)."""
    try:
        return await asyncio.wait_for(_until_inner(session, want=want, t0=t0, timeout=timeout,
                                                   stop_on_complete=stop_on_complete), timeout + 2)
    except asyncio.TimeoutError:
        return None, "timeout"


async def _until_inner(session, *, want: str, t0: float, timeout: float = 25.0, stop_on_complete: bool = True):
    end = time.monotonic() + timeout
    while time.monotonic() < end:              # session.receive() ends at every turn_complete
        got = await _until_turn(session, want=want, t0=t0, end=end, stop_on_complete=stop_on_complete)
        if got is not None:
            return got
    return None, "timeout"


async def _until_turn(session, *, want: str, t0: float, end: float, stop_on_complete: bool):
    """One turn of the stream. Returns a result, or None to keep reading the next turn."""
    first_audio, text = None, []
    async for r in session.receive():
        now = time.monotonic()
        if r.data and first_audio is None:
            first_audio = now - t0
            if want == "audio":
                return first_audio, r
        sc = r.server_content
        if sc is not None and sc.output_transcription and sc.output_transcription.text:
            text.append(sc.output_transcription.text)
        if r.tool_call and want == "tool":
            return now - t0, r.tool_call
        if sc is not None and sc.turn_complete:
            if want == "done":
                return now - t0, "".join(text)
            if want in ("audio", "tool") and stop_on_complete:
                return None, "".join(text)
        if now > end:
            return None, "timeout"
    return None


async def _drain_turn(session, timeout: float = 25.0):
    try:
        await asyncio.wait_for(_drain_inner(session, timeout), timeout + 2)
    except asyncio.TimeoutError:
        pass


async def _drain_inner(session, timeout: float = 25.0):
    end = time.monotonic() + timeout
    async for r in session.receive():
        sc = r.server_content
        if sc is not None and sc.turn_complete:
            return
        if r.tool_call:
            from google.genai import types
            await session.send_tool_response(function_responses=[
                types.FunctionResponse(id=fc.id, name=fc.name, response={"result": "ok"})
                for fc in r.tool_call.function_calls])
        if time.monotonic() > end:
            return


async def bench_model(model: str, rounds: int, tools: list, system: str, speech: bytes, variant: str = "") -> dict:
    from google import genai
    from google.genai import types
    from core.secret_store import get_secret
    out = {"model": model, "connect": [], "text_audio": [], "tool_call": [], "tool_audio": [],
           "speech_audio": [], "errors": []}
    api = "v1alpha" if ("native-audio" in model and "plain" not in variant) else "v1beta"
    client = genai.Client(api_key=get_secret("GOOGLE_API_KEY"), http_options={"api_version": api})
    for rnd in range(rounds):
        t0 = time.monotonic()
        try:
            async with client.aio.live.connect(model=model, config=_config(types, model, tools, system,
                                                                            variant)) as s:
                out["connect"].append(time.monotonic() - t0)
                # typed question
                t = time.monotonic()
                await s.send_client_content(turns={"role": "user", "parts": [{"text":
                                            "What is two plus two? Answer in three words."}]}, turn_complete=True)
                sec, _ = await _until(s, want="audio", t0=t)
                if sec is not None:
                    out["text_audio"].append(sec)
                await _drain_turn(s)
                # tool question
                t = time.monotonic()
                await s.send_client_content(turns={"role": "user", "parts": [{"text":
                                            "How busy is my computer right now? Check the system status."}]},
                                            turn_complete=True)
                sec, call = await _until(s, want="tool", t0=t)
                if sec is not None and call is not None and not isinstance(call, str):
                    out["tool_call"].append(sec)
                    t = time.monotonic()
                    await s.send_tool_response(function_responses=[
                        types.FunctionResponse(id=fc.id, name=fc.name,
                                               response={"result": "CPU 14%, RAM 41%, GPU 3%, 52°C."})
                        for fc in call.function_calls])
                    sec2, _ = await _until(s, want="audio", t0=t, stop_on_complete=False)
                    if sec2 is not None:
                        out["tool_audio"].append(sec2)
                    await _drain_turn(s)
                else:
                    await _drain_turn(s)
                # spoken question (real-time pace, then silence) — includes end-of-turn detection
                if speech:
                    for i in range(0, len(speech), CHUNK * 2):
                        await s.send_realtime_input(audio=types.Blob(data=speech[i:i + CHUNK * 2],
                                                                     mime_type="audio/pcm;rate=16000"))
                        await asyncio.sleep(0.04)
                    t_end = time.monotonic()
                    silence = b"\x00\x00" * CHUNK

                    async def feed_silence():
                        for _ in range(100):           # up to 4 s of silence
                            await s.send_realtime_input(audio=types.Blob(data=silence,
                                                                         mime_type="audio/pcm;rate=16000"))
                            await asyncio.sleep(0.04)
                    feeder = asyncio.create_task(feed_silence())
                    sec, _ = await _until(s, want="audio", t0=t_end, timeout=12)
                    feeder.cancel()
                    if sec is not None:
                        out["speech_audio"].append(sec)
        except Exception as e:
            from core.secret_store import redact
            out["errors"].append(redact(f"{type(e).__name__}: {e}")[:160])
    return out


def _fmt(xs):
    return f"{statistics.median(xs):5.2f}s" if xs else "   —  "


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--lean", action="store_true", help="also measure with the lean tool set")
    ap.add_argument("--plain", action="store_true",
                    help="also measure the native-audio model WITHOUT affective dialog / proactive audio")
    a = ap.parse_args(argv)
    system = _system_text()
    variants = [("full", _tool_decls(False))]
    if a.lean:
        variants.append(("lean", _tool_decls(True)))
    if a.plain:
        variants.append(("plain", _tool_decls(True)))
    speech = _speech_pcm("JARVIS, what time is it?")
    lines = [f"JARVIS Live latency bench — {time.strftime('%Y-%m-%d %H:%M')} — system prompt {len(system):,} chars"
             + (f"; spoken probe {len(speech) / 32000:.1f}s" if speech else "; no spoken probe")]
    lines.append(f"{'model':<52}{'tools':<12}{'connect':>9}{'text→aud':>10}{'tool call':>10}{'tool→aud':>10}"
                 f"{'speech→aud':>12}")
    results = []
    for label, tools in variants:
        size = len(json.dumps(tools))
        for m in [x if x.startswith("models/") else f"models/{x}" for x in a.models]:
            if label == "plain" and "native-audio" not in m:
                continue
            try:
                r = asyncio.run(asyncio.wait_for(bench_model(m, a.rounds, tools, system, speech, label),
                                                 160 * a.rounds))
            except asyncio.TimeoutError:
                r = {"model": m, "connect": [], "text_audio": [], "tool_call": [], "tool_audio": [],
                     "speech_audio": [], "errors": ["the whole run timed out"]}
            r["variant"] = label
            r["tool_chars"] = size
            results.append(r)
            line = (f"{m.replace('models/', ''):<52}{label + ' ' + str(len(tools)):<12}{_fmt(r['connect']):>9}"
                    f"{_fmt(r['text_audio']):>10}{_fmt(r['tool_call']):>10}{_fmt(r['tool_audio']):>10}"
                    f"{_fmt(r['speech_audio']):>12}")
            lines.append(line + (f"   errors: {'; '.join(r['errors'])[:120]}" if r["errors"] else ""))
            print(lines[-1], flush=True)
    text = "\n".join(lines)
    print("\n" + text)
    try:
        (ROOT / "tools" / "latency_bench.log").write_bytes((text.replace("\n", "\r\n") + "\r\n").encode("utf-8"))
        (ROOT / "tools" / "latency_bench.json").write_bytes(json.dumps(results, indent=1).replace(
            "\n", "\r\n").encode("utf-8"))
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
