"""
tools/live_tool_probe.py — how a Gemini Live model behaves around a tool call,
event by event, with timings. Used to check a voice model BEFORE JARVIS
switches to it: a model that never speaks after a tool result would break
every action.

    python tools/live_tool_probe.py                       # gemini-3.8-live
    python tools/live_tool_probe.py --model models/gemini-2.5-flash-native-audio-preview-12-2025

For each variant (plain FunctionResponse; scheduling=INTERRUPT; WHEN_IDLE) it
asks a question that needs a tool, answers the tool call, then records every
server event for 20 s: audio bytes, transcription text, turn_complete,
generation_complete, interrupted, tool calls. Writes tools/live_tool_probe.log.
Never prints a key.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TOOLS = [{"name": "system_status", "description": "CPU, RAM, GPU usage and temperature of this PC.",
          "parameters": {"type": "OBJECT", "properties": {}, "required": []}}]


async def probe(model: str, variant: str, log) -> None:
    from google import genai
    from google.genai import types
    from core.secret_store import get_secret
    api = "v1alpha" if "native-audio" in model else "v1beta"
    client = genai.Client(api_key=get_secret("GOOGLE_API_KEY"), http_options={"api_version": api})
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"], output_audio_transcription={},
        system_instruction="You are JARVIS, a voice assistant. Use tools when needed. Be brief.",
        tools=[{"function_declarations": TOOLS}])
    t0 = time.monotonic()

    def ev(msg):
        log(f"  {time.monotonic() - t0:6.2f}s  {msg}")
    try:
        async with client.aio.live.connect(model=model, config=cfg) as s:
            ev(f"connected ({variant})")
            await s.send_client_content(turns={"role": "user", "parts": [{"text":
                                        "How busy is my computer right now? Check the system status."}]},
                                        turn_complete=True)
            sent_response = False
            audio_after = 0
            end = time.monotonic() + 30

            async def reader():
                nonlocal sent_response, audio_after
                while time.monotonic() < end:          # receive() ends at each turn_complete
                    await read_turn()

            async def read_turn():
                nonlocal sent_response, audio_after
                async for r in s.receive():
                    if r.data:
                        if sent_response:
                            audio_after += len(r.data)
                        ev(f"audio {len(r.data)} B" + (" (after tool result)" if sent_response else ""))
                    sc = r.server_content
                    if sc is not None:
                        if sc.output_transcription and sc.output_transcription.text:
                            ev(f"says: {sc.output_transcription.text!r}")
                        if getattr(sc, "interrupted", False):
                            ev("interrupted")
                        if getattr(sc, "generation_complete", False):
                            ev("generation_complete")
                        if sc.turn_complete:
                            ev("turn_complete")
                    if r.tool_call:
                        names = [fc.name for fc in r.tool_call.function_calls]
                        ev(f"tool_call {names}")
                        fr = []
                        for fc in r.tool_call.function_calls:
                            kw = dict(id=fc.id, name=fc.name, response={"result": "CPU 14%, RAM 41%, GPU 3%."})
                            if variant in ("INTERRUPT", "WHEN_IDLE", "SILENT"):
                                try:
                                    kw["scheduling"] = getattr(types.FunctionResponseScheduling, variant)
                                except Exception as e:
                                    ev(f"(scheduling not supported by this SDK: {type(e).__name__})")
                            fr.append(types.FunctionResponse(**kw))
                        await s.send_tool_response(function_responses=fr)
                        sent_response = True
                        ev("tool response sent")
                    if getattr(r, "tool_call_cancellation", None):
                        ev("tool_call_cancellation")
                    if time.monotonic() > end:
                        return
            try:
                await asyncio.wait_for(reader(), 32)
            except asyncio.TimeoutError:
                pass
            ev(f"RESULT: {'SPOKE' if audio_after else 'SILENT'} after the tool result ({audio_after} B audio)")
    except Exception as e:
        from core.secret_store import redact
        ev(f"error: {redact(f'{type(e).__name__}: {e}')[:200]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/gemini-3.8-live")
    ap.add_argument("--variants", nargs="*", default=["plain", "INTERRUPT", "WHEN_IDLE"])
    a = ap.parse_args(argv)
    lines = []

    def log(s):
        lines.append(s)
        print(s, flush=True)
    model = a.model if a.model.startswith("models/") else f"models/{a.model}"
    log(f"Live tool probe — {model} — {time.strftime('%Y-%m-%d %H:%M')}")
    for v in a.variants:
        log(f"== variant {v}")
        asyncio.run(probe(model, v, log))
    try:
        (ROOT / "tools" / "live_tool_probe.log").write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
