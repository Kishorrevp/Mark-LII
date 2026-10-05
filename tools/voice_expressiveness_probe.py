"""
tools/voice_expressiveness_probe.py - does the Phase 8 VOICE PERFORMANCE
direction make JARVIS's real voice more expressive? Measured, not assumed.

Each line below is sent as a typed turn (role "user") to a FRESH Live session
built with JARVIS's real system prompt (core/prompt.txt + the persona), the
same voice and the same mode JARVIS uses, once WITHOUT the direction
("before") and once WITH it ("after"). The spoken answer is saved as a WAV
(voice_samples/...) so you can listen, and measured:

    pitch range   semitones between the 10th and 90th percentile of the
                  voice's pitch - higher = livelier melody, lower = flatter
    pitch median  Hz - a happy voice should sit higher than a comforting one
    loudness range dB between quiet and loud voiced moments
    pace          words per second (comfort should be slower than happy)
    held notes    share of voiced time in steady-pitch stretches >= 0.2 s
                  (singing holds notes; speech glides)
    first audio   seconds from the question to the first sound

    python tools/voice_expressiveness_probe.py                 # 3.8-live before/after + 2.5 after
    python tools/voice_expressiveness_probe.py --models models/gemini-3.8-live

Writes tools/voice_expressiveness_probe.log and .json. Never prints a key.
Dependency: numpy (already used by JARVIS).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np  # noqa: E402

SR = 24000
LINES = [
    ("neutral", "What is the capital of France? One sentence."),
    ("happy", "I just got my first job offer! Tell me how you feel about it, in two sentences."),
    ("comfort", "I failed my exam today and I feel really low."),
    ("narration", "Narrate the opening two sentences of a spooky story about an old lighthouse."),
    ("theatrical", "Announce the arrival of the new Iron Man suit like a dramatic movie trailer, in two sentences."),
    ("character", "Say 'all systems are online' the way a pirate captain would."),
    ("singing", "Sing me a short happy birthday song."),
    ("tamil", "தமிழில் எனக்கு ஒரு சந்தோஷமான காலை வணக்கம் சொல்லுங்க, இரண்டு வரிகளில்."),
]


# ── the system prompt JARVIS really uses (main._load_system_prompt, without main's imports) ──
def system_prompt(mode: str, with_direction: bool) -> str:
    from core import persona as P
    real_cfg = P._cfg

    def cfg_before():
        c = real_cfg()
        c["voice_performance"] = dict(c.get("voice_performance") or {}, enabled=False)
        for ident in (c.get("identities") or {}).values():   # the character text as it was before
            ch = str(ident.get("character", ""))
            ch = re.sub(r"Understated confidence by default.*$", "Understated confidence; never theatrical.", ch)
            ident["character"] = ch
        return c
    try:
        if not with_direction:
            P._cfg = cfg_before
        block = P.compose(mode, surface="voice")
    finally:
        P._cfg = real_cfg
    template = (ROOT / "core" / "prompt.txt").read_text(encoding="utf-8")
    return template.replace("{{PERSONALITY_BLOCK}}", block)


# ── measurements ──
def _f0_track(x: np.ndarray, sr: int = SR):
    """Per 10 ms hop: (rms, f0 or nan) using normalised autocorrelation (60-500 Hz)."""
    win, hop = int(0.040 * sr), int(0.010 * sr)
    lo, hi = int(sr / 500), int(sr / 60)
    rms, f0 = [], []
    for s in range(0, max(0, len(x) - win), hop):
        fr = x[s:s + win]
        fr = fr - fr.mean()
        e = float(np.sqrt(np.mean(fr * fr)))
        rms.append(e)
        if e < 1e-4:
            f0.append(np.nan)
            continue
        ac = np.correlate(fr, fr, mode="full")[win - 1:]
        ac = ac / (ac[0] + 1e-9)
        seg = ac[lo:hi]
        k = int(np.argmax(seg))
        if seg[k] < 0.45:
            f0.append(np.nan)
            continue
        lag = lo + k
        if 1 <= k < len(seg) - 1:                       # parabolic refinement
            a, b, c = seg[k - 1], seg[k], seg[k + 1]
            d = 0.5 * (a - c) / (a - 2 * b + c + 1e-12)
            lag = lag + d
        f0.append(sr / lag)
    return np.array(rms), np.array(f0)


def measure(pcm: bytes, transcript: str) -> dict:
    if not pcm:
        return {}
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    rms, f0 = _f0_track(x)
    if not len(rms):
        return {}
    thr = 0.12 * np.percentile(rms, 95)
    voiced = (rms > thr) & ~np.isnan(f0)
    active = rms > thr
    out = {"seconds": round(len(x) / SR, 2), "voiced_ratio": round(float(voiced.mean()), 3)}
    if voiced.sum() >= 10:
        fv = f0[voiced]
        st = 12 * np.log2(fv / np.median(fv))
        out["pitch_median_hz"] = round(float(np.median(fv)), 1)
        out["pitch_range_st"] = round(float(np.percentile(st, 90) - np.percentile(st, 10)), 2)
        out["pitch_std_st"] = round(float(np.std(st)), 2)
        rv = rms[voiced]
        out["loudness_range_db"] = round(float(20 * np.log10((np.percentile(rv, 90) + 1e-9) /
                                                              (np.percentile(rv, 10) + 1e-9))), 1)
        # held notes: runs of voiced frames whose pitch stays within +-0.5 st for >= 0.2 s
        semis = np.full(len(f0), np.nan)
        semis[voiced] = 12 * np.log2(f0[voiced] / 100.0)
        held, run = 0, []
        for v in semis:
            if np.isnan(v) or (run and abs(v - np.median(run)) > 0.5):
                if len(run) >= 20:
                    held += len(run)
                run = [] if np.isnan(v) else [v]
            else:
                run.append(v)
        if len(run) >= 20:
            held += len(run)
        out["held_note_ratio"] = round(held / max(1, int(voiced.sum())), 3)
    if active.any():
        idx = np.where(active)[0]
        talk_s = (idx[-1] - idx[0] + 1) * 0.010
        words = len(re.findall(r"[\w஀-௿']+", transcript or ""))
        out["words"] = words
        out["words_per_s"] = round(float(words / talk_s), 2) if talk_s > 0.3 else None
    return out


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)


# ── one line, one fresh session ──
async def speak_once(client, model: str, system: str, voice: str, text: str, enhanced: bool) -> dict:
    from google.genai import types
    cfg = dict(response_modalities=["AUDIO"], output_audio_transcription={}, system_instruction=system,
               speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                   prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))))
    if enhanced:
        cfg["enable_affective_dialog"] = True
    res = {"pcm": b"", "said": "", "first_audio_s": None, "error": ""}
    t_sent = None
    try:
        async with client.aio.live.connect(model=model, config=types.LiveConnectConfig(**cfg)) as s:
            await s.send_client_content(turns={"role": "user", "parts": [{"text": text}]}, turn_complete=True)
            t_sent = time.monotonic()
            pcm, said = bytearray(), []

            async def read():
                async for r in s.receive():
                    if r.data:
                        if res["first_audio_s"] is None:
                            res["first_audio_s"] = round(time.monotonic() - t_sent, 2)
                        pcm.extend(r.data)
                    sc = r.server_content
                    if sc is not None:
                        if sc.output_transcription and sc.output_transcription.text:
                            said.append(sc.output_transcription.text)
                        if sc.turn_complete and pcm:
                            return
            try:
                await asyncio.wait_for(read(), 40)
            except asyncio.TimeoutError:
                res["error"] = "timeout"
            res["pcm"], res["said"] = bytes(pcm), "".join(said).strip()
    except Exception as e:
        from core.secret_store import redact
        res["error"] = redact(f"{type(e).__name__}: {e}")[:200]
    return res


async def main_async(models: list, log, repeat: int = 1, only_lines=None, only_conds=None) -> dict:
    from google import genai
    from core.secret_store import get_secret
    try:
        from memory.config_manager import get_live_voice, get_personality_mode
        voice, mode = get_live_voice(), get_personality_mode()
    except Exception:
        voice, mode = "Charon", "friendly"
    log(f"voice {voice} · mode {mode}")
    key = get_secret("GOOGLE_API_KEY")
    results = {"voice": voice, "mode": mode, "runs": []}
    plan = []
    for m in models:
        if "native-audio" in m:
            plan.append((m, "after", True))            # what production would send to the 2.5 model
        else:
            plan += [(m, "before", False), (m, "after", False)]
    if only_conds:
        plan = [x for x in plan if x[1] in only_conds]
    lines = [(lb, tx) for lb, tx in LINES if not only_lines or lb in only_lines]
    for model, cond, enhanced in plan:
        short = model.replace("models/", "")
        api = "v1alpha" if enhanced else "v1beta"
        client = genai.Client(api_key=key, http_options={"api_version": api})
        system = system_prompt(mode, with_direction=(cond == "after"))
        log(f"== {short} · {cond} the voice direction" + (" · affective dialog" if enhanced else ""))
        for rep_i in range(1, repeat + 1):
            for label, text in lines:
                i = [lb for lb, _ in LINES].index(label) + 1
                r = await speak_once(client, model, system, voice, text, enhanced)
                m = measure(r["pcm"], r["said"])
                # single takes are the listening samples; repeat runs (statistics) go to _to_delete
                wav = (ROOT / "voice_samples" / short / f"{cond}_{i}_{label}.wav" if repeat == 1
                       else ROOT / "_to_delete" / "voice_takes" / short / f"{cond}_{i}_{label}_take{rep_i}.wav")
                if r["pcm"]:
                    write_wav(wav, r["pcm"])
                row = {"model": short, "cond": cond, "line": label, "take": rep_i, "first_audio_s": r["first_audio_s"],
                       "error": r["error"], "said": r["said"][:160], **m,
                       "wav": str(wav.relative_to(ROOT)) if r["pcm"] else ""}
                results["runs"].append(row)
                log(f"  {label:<10} pitch range {m.get('pitch_range_st', '-'):>5} st · median "
                    f"{m.get('pitch_median_hz', '-'):>6} Hz · loudness {m.get('loudness_range_db', '-'):>5} dB · "
                    f"pace {m.get('words_per_s', '-')} w/s · held {m.get('held_note_ratio', '-')} · "
                    f"first audio {r['first_audio_s']} s{' · ERROR ' + r['error'] if r['error'] else ''}")
                log(f"             said: {r['said'][:110]!r}")
                await asyncio.sleep(1.0)
    return results


def summarize(res: dict, log) -> None:
    runs = [r for r in res["runs"] if r.get("pitch_range_st") is not None]

    def get(model, cond, line, key):
        vals = [r.get(key) for r in runs if r["model"] == model and r["cond"] == cond and r["line"] == line
                and r.get(key) is not None]
        return float(np.mean(vals)) if vals else None

    def spread(model, cond, line, key):
        vals = [r.get(key) for r in runs if r["model"] == model and r["cond"] == cond and r["line"] == line
                and r.get(key) is not None]
        return (float(np.mean(vals)), float(np.std(vals)), len(vals)) if vals else None
    log("== summary")
    for model in sorted({r["model"] for r in runs}):
        for cond in ("before", "after"):
            rows = [r for r in runs if r["model"] == model and r["cond"] == cond and r["line"] != "neutral"]
            if not rows:
                continue
            pr = np.mean([r["pitch_range_st"] for r in rows])
            ld = np.mean([r["loudness_range_db"] for r in rows])
            fa = [r["first_audio_s"] for r in rows if r.get("first_audio_s") is not None]
            log(f"  {model:<48} {cond:<6} mean pitch range {pr:5.2f} st · loudness range {ld:5.1f} dB · "
                f"first audio {np.median(fa) if fa else float('nan'):.2f} s (median)")
            hap, com = get(model, cond, "happy", "pitch_median_hz"), get(model, cond, "comfort", "pitch_median_hz")
            hp, cp = get(model, cond, "happy", "words_per_s"), get(model, cond, "comfort", "words_per_s")
            if hap and com:
                log(f"      happy vs comfort: pitch {12 * np.log2(hap / com):+.1f} st, pace "
                    f"{hp if hp is not None else float('nan'):.2f} vs {cp if cp is not None else float('nan'):.2f} w/s")
            per_line = []
            for ln, _ in LINES:
                sp = spread(model, cond, ln, "pitch_range_st")
                if sp:
                    per_line.append(f"{ln} {sp[0]:.1f}" + (f"+-{sp[1]:.1f}" if sp[2] > 1 else ""))
            log("      pitch range by line (st): " + " · ".join(per_line))
            sh, nh = get(model, cond, "singing", "held_note_ratio"), get(model, cond, "neutral", "held_note_ratio")
            if sh is not None and nh is not None:
                log(f"      held notes: singing {sh:.2f} vs neutral speech {nh:.2f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1, help="takes per line (averaged in the summary)")
    ap.add_argument("--lines", default="", help="comma list of line labels (default: all)")
    ap.add_argument("--conds", default="", help="before,after (default: both)")
    ap.add_argument("--models", nargs="*",
                    default=["models/gemini-3.8-live", "models/gemini-2.5-flash-native-audio-preview-12-2025"])
    a = ap.parse_args(argv)
    lines = []

    def log(s):
        lines.append(s)
        print(s, flush=True)
    log(f"Voice expressiveness probe - {time.strftime('%Y-%m-%d %H:%M')}")
    res = {}
    try:
        res = asyncio.run(main_async([m if m.startswith("models/") else f"models/{m}" for m in a.models], log,
                                     max(1, a.repeat),
                                     {x.strip() for x in a.lines.split(",") if x.strip()} or None,
                                     {x.strip() for x in a.conds.split(",") if x.strip()} or None))
        summarize(res, log)
    finally:
        (ROOT / "tools" / "voice_expressiveness_probe.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if res:
            (ROOT / "tools" / "voice_expressiveness_probe.json").write_text(
                json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
