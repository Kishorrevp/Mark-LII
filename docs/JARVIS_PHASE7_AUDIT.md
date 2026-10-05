# JARVIS Phase 7 — audit against your requests, the prompt pack and Mark LV

Written 1–2 Oct 2026 at the end of Phase 7, from the code as delivered and the
runs on your PC (Windows, Python 3.14): **360 tests OK (1 skipped: a
not-Windows test), preflight PASS 27 / FAIL 0 / WARN 0.** Two small fixes
came after that run (§5); with them the suite is 361 tests, all passing in the
sandbox and, from the drop-in ZIP, on your PC's Linux VM (except importing
`main.py`, which needs Python ≥ 3.11 — the VM has 3.10).

Status words: **Exceeds** (does what was asked and more), **Meets**,
**Partial** (part of it, the rest is named), **Not built** (with the reason).
"Verified" says how: *executed on your PC*, *executed in tests* (real code, fake
network/hardware), or *structural* (source/AST checks only).

---

## 1. Your nine requests

| # | You asked | Status | What exists now | Verified |
|---|---|---|---|---|
| 1 | Go through JARVIS and the prompt pack, fill the gaps, check meets/exceeds | Meets | This document (§2). Built from the pack in Phase 7: the Telegram pocket line, standing orders and the urgent line, reminder relay to the phone, the dashboard lock-down. Not built, with reasons: paid phone line (Retell), hotline, Google OAuth line, 3D notes galaxy. | executed (tests + PC) |
| 2 | Find why JARVIS answers slowly and make it as fast as possible | Exceeds | Root cause measured: the voice model. `gemini-2.5-flash-native-audio` with affective dialog + proactive audio took **2.82 s** from end of speech to first audio; `gemini-3.8-live` takes **0.65–0.69 s**; after a tool result 1.31 s → **0.66 s**. Second cause: the reasoning chain could hang (a stuck model, no HTTP timeout, same-model retries) — worst case 31 s → ~4 s. Plus 40 ms mic chunks, 4,800-byte playback batches, low-latency output device. Live meter: your last 14 real turns median **0.82 s** (Mission Control). | executed on your PC (`tools/latency_bench.py`, `memory/latency.jsonl`) |
| 3 | Port Mark LV's advanced features | Partial (by choice) | Written independently (Mark LV is CC BY-NC 4.0 — ideas only, no code copied): self-echo guard, wake word ("Hey JARVIS", local), push-to-talk, model ladder with timeouts + cooldowns, voice-model ladder, instant acknowledgement. Not taken: talking-head avatar/lip-sync (you chose the sphere UI), video-on-HUD (needs yt-dlp), clipboard panel, auto-start. | executed in tests |
| 4 | Link NVIDIA keys/models; switch Google ↔ Anthropic ↔ NVIDIA when needed, and test it | Meets | `.env`: `JARVIS_AI_PROVIDER=google`, `JARVIS_AI_FALLBACKS=nvidia,anthropic` (your choice: Google first). Pinned switching by voice ("use nemotron ultra"). Tested on your PC (`tools/brain_bench.py`): auto → Google 4.1 s; pinned NVIDIA ultra 2.41 s; pinned Anthropic **fails honestly** (the key has no credit) and says it did not substitute another model. NVIDIA's free tier was congested that evening (503 / timeouts) — the chain skips a resting provider without waiting. | executed on your PC |
| 5 | New UI: video-2 sphere + current orbits, star-only space background, friendly blue, serious red (dashboard too) | Meets | `ui_holo.FilamentSphere` (filaments, hairs, orbit track, rings, comets), starfield-only `Backdrop`, accent follows the mode in the HUD, confirmation banner, Mission Control and the phone dashboard. | executed on your PC (offscreen renders) |
| 6 | dashboard_feature screens into settings; confirmations "allow once / allow always / deny" | Meets | Mission Control (F9, MISSION dock button, drawer button): Overview, Agents, Tasks (kanban), Schedule, Content, Logs, Permissions (revoke "always"), Learning (forget); slash commands /background /personality /kanban /watch /draw /reference /pocket. Three-way HUD confirmation; CRITICAL actions and a fixed list (shutdown, plugin install, code run, permanent delete…) can never be "always". | executed in tests + PC render |
| 7 | Let JARVIS see the screen and use mouse/keyboard like a human ("draw something you like") | Meets | `watch_screen` (on request; on automatically while drawing / using the computer; red EYE; paused on sign-in/banking windows; stops after 10 min). `draw`: Paint opened by JARVIS, line art → mouse strokes, window re-checked before every stroke, F12 lifts the pen, asks first. `computer_use` gained drag/move. | executed in tests (fake mouse/screen/vision) |
| 8 | "Review this and do", "keep this as a reference" (e.g. a website with some AI's UI/UX + content from the web) | Meets | `reference` (screen / window / clipboard / file / URL / text) stored in `memory/references/`, used as DATA; the website team reads real pages, captures inspiration screenshots and gets your references in its brief. | executed in tests |
| 9 | "The model should train from me day to day — what I do, how I do it, how I'd like it done" | Meets (as learning, not model training) | `core/learning.py`: your stated preferences and corrections (your own words), habits by time of day (≥3 times on ≥2 days), routines (workflows) — fed into every conversation and the planner; list / forget / pause by voice or in Mission Control. Honest note: the Gemini model's weights are not retrained (not possible on a free API); JARVIS learns *around* the model. | executed in tests |

---

## 2. The prompt pack ("Build Your Own Jarvis", 18 prompts)

The pack builds a browser app with Claude Opus 5.5 as the brain. JARVIS is a
Windows desktop assistant with Gemini Live as the voice and a multi-provider
gateway as the brain (Anthropic is available but not default — your free-only
rule). Each prompt is mapped to what it is *for*.

### Part One — the foundation

| Prompt | Status | JARVIS equivalent / gap |
|---|---|---|
| 01 Galaxy & brain | Partial | **Brain from your notes: Exceeds** — `core/knowledge.py` indexes notes, memory, episodes, tasks (BM25, time phrases, secrets never indexed) and every answer can name its sources. **The 3D galaxy viewer is not built** — JARVIS's face is the PyQt HUD; a separate browser galaxy would be a second UI to keep secure. Candidate for a later phase. |
| 02 Voice both ways | Exceeds | Native-audio Gemini Live both ways (not Web Speech). The pack's FINISH_MS is `voice.end_of_turn_silence_ms`; stop words act instantly; status shows listening / thinking / speaking. Added: barge-in with echo guard, wake word, push-to-talk, 0.65 s replies. The `?mute=1` tab flag has no desktop meaning — the MIC button / mic modes cover it. |
| 03 Prove the source | Partial | SOURCES panel shows exactly which notes/memories an answer used; small talk shows none; invented citations are stripped. No camera fly-through (no galaxy). |
| 04 The butler | Meets | `core/persona.py` + `config/persona.json` + friendly / serious personality files, one place to rewrite the character. |
| 05 Total recall | Exceeds | "remember that…" → verified write (re-read) → searchable in the very next question → receipt; a failed write is said out loud, never a false "saved". |
| 06 Sight | Exceeds | Frame captured when you ask (re-grabbed if older than 2.5 s), mime sniffed from bytes, honest when the frame is unusable. Phase 7 adds continuous watching. |
| 07 Preflight | Meets | `tools/preflight.py` (27 offline checks, `--live` for real provider/voice calls), PASS/FAIL/WARN, non-zero exit on failure. |
| 08 Brain swap | Meets | Exact spoken model switching, runtime-only, refuses near-misses with the real alternatives — never the nearest match. |

### Part Two — mobile

| Prompt | Status | JARVIS equivalent / gap |
|---|---|---|
| 09 Phone line (Retell) | Not built | A phone number and per-minute calls are paid; your rule is free/local only. The urgent line reaches you on Telegram instead. |
| 10 Google line (Gmail/Calendar, drafts only) | Not built (deferred) | Needs a Google Cloud OAuth client you create, plus refresh-token storage — a real security surface. Queued for a later phase with the pack's rules (PKCE + state, drafts only, mail is data, receipts). |
| 11 The pocket (Telegram) | **Exceeds** | `core/telegram_bot.py`. Pairing exactly as specified (crypto code shown only on the HUD — never spoken, never given to the model; 10 min; whole message; once; dies after 5 wrong guesses). Strangers: one polite refusal, then silence, no transcription. Same brain and permission rules — **stricter**: phone tasks run as origin `telegram`, so MEDIUM-risk steps also wait for the HUD; the phone can never approve, and cannot lift the emergency stop (it can engage it). Voice notes transcribed on your PC with faster-whisper (free, installed) instead of a paid key; replies to a voice note come back as a voice note via edge-tts (free — **`pip install edge-tts`** on your PC, it is not installed yet; until then text only). Once-per-card dedupe. Added: group chats ignored, messages older than 15 min (PC was off) are not acted on, token redacted from every log. **Dormant until you put `TELEGRAM_BOT_TOKEN` in `.env`.** |
| 12 Standing order | Meets | `route()` with a 21-sentence routing table in the tests ("only bug me if it's on fire" included): standing order → urgent line armed, nothing sent; "don't text me" / "I'm back" → off; "text me now" → a brief; the noun "I have a call at 5" → nothing. Context acks (meeting / leaving / sleep). Urgent means once: 10-minute cooldown. The voice prompt is taught the same rule, and a desk-side backstop applies it even if the model forgets. |
| 13 Busy opener | Not built | Phone calls only (see 09). |
| 14 Do it from the phone | Partial | A Telegram instruction becomes a task; the result comes back once ("Task … finished … (Self-check: verified …)"). Call-transcript extraction is not applicable without calls. |
| 15 Reminders & briefing | Partial | Reminders are OS-scheduled (survive restarts) and, while the urgent line is armed, also reach your phone once. "Text me now" sends a desk brief. Not built: phone reminder *calls* and the scheduled morning briefing *call*. |
| 16 Hotline | Not built | Needs the paid phone line and a public tunnel into your PC; the lock-down prefers no public exposure. |
| 17 Stage the call | Not built | Calls only. |
| 18 Lock it down | Meets (adapted) | The phone dashboard must listen on the LAN (that is how the phone reaches it), so instead of binding 127.0.0.1: Host allow-list (stops DNS rebinding), foreign Origin refused on every state-changing request and WebSocket, body caps (64 KB JSON), 5 wrong PINs → 10-minute lockout, nosniff / no-framing / no-referrer / same-origin headers; `/.env` and `config/api_keys.json` are never served (404) — **end-to-end tested with the real FastAPI app on your PC**. Keys are DPAPI-encrypted in `.env`, redacted from logs (now including Telegram bot tokens). No shell tool exists for the model; a confirmation covers one action. chmod 600 has no Windows equivalent here — the files sit in your user profile and the key values are encrypted. |

---

## 3. Mark LV (FatihMakes, CC BY-NC 4.0)

| Mark LV feature | In JARVIS | Note |
|---|---|---|
| Self-echo guard | ✅ `core/echo_guard.py` | Band-energy comparison with what was just played; adaptive threshold. |
| Wake word "Hey Jarvis" | ✅ `core/mic_gate.py` | openWakeWord, local; falls back to always-listening **and says so** if it can't load. |
| Push-to-talk | ✅ `core/mic_gate.py` | Global on Windows (F8 default, configurable). |
| Model ladder (timeouts, cooldowns) | ✅ `core/ai_gateway.py` + `core/model_health.py` | Across three providers, not only Gemini. |
| Faster live engine | ✅ exceeds | Mark LV moved to 3.1 Flash Live; JARVIS measured 3.1 (0.83 s, internal errors) vs **3.8-live (0.65 s)**. |
| Instant acknowledgement | ✅ | Prompt rule: say one short line before a long tool runs. |
| Runtime self-knowledge, undo, real confirmations, audio device picker, session continuity, plugins | ✅ already in JARVIS (Phases 4–6) | Memory is inspected / forgotten by voice (`memory_inspect`, `forget_memory`); learned items in Mission Control → Learning. |
| Talking-head avatar + lip-sync | ✗ | You chose the filament sphere UI. |
| Video on the HUD | ✗ | Adds yt-dlp; `youtube_video` opens the browser instead. |
| Clipboard intelligence panel, auto-start | ✗ | Not requested; small, can be added later. |

---

## 4. The ChatGPT latency analysis you pasted — checked

| Claim | Measured on your PC | Verdict |
|---|---|---|
| Use gemini-3.8-live | 0.65–0.69 s vs 2.82 s | **Right** — done. |
| 43 tools ≈ 142,000 characters | Prompt 14,282 chars; all tool declarations ≈ 35,000 chars | **Wrong size** |
| Dynamic tool groups would help | 47 vs 34 tools: 0.69 vs 0.65 s (noise) | **No measurable gain** — all tools stay declared; a "lean" profile exists. |
| 40 ms mic chunks | done | Right (small gain). |
| Playback 9,600 → 4,800 bytes | done + low-latency output | Right (small gain). |
| Measure latency | `core/latency.py`, Mission Control median | Right — done. |
| Don't switch to local TTS | agreed | Native audio stays. |

---

## 5. Found and fixed during this audit

* **Direct tools returned a code dump to the model.** `learning`, `reference`
  and `analyze_screen` handed Gemini `ToolResult(ok=True, data=…)` instead of
  their sentence. Fixed in `JarvisCore.after_direct_call`; test added.
* **A new test wrote into your real memory.** The first run of the Telegram
  bridge test on your PC saved `notes/locker_number = 42` (source
  "telegram") into `memory/long_term.json`. That single entry was removed
  (atomic CRLF write, nothing else touched) and the test now uses a temporary
  memory file and asserts the real one is unchanged. An older Phase 6 test
  also wrote a harmless process log into `memory/process_logs/`; isolated too.
  The full suite now leaves `memory/`, `config/` and `knowledge/` byte-identical.
* **The first reminder ever would not have reached the phone** (the relay
  marked the end of a receipt file that did not exist yet). Fixed; test added.
* **One failing test could break others.** If `main.py` failed to import, the
  smoke test left its import stubs installed, and later tests saw fake
  modules. It now cleans up after itself.

## 6. Needs you

1. **Restart JARVIS** to use everything above (it loads code at start).
2. Optional — Telegram: create a bot with @BotFather, add
   `TELEGRAM_BOT_TOKEN=…` to `.env`, restart, say "pocket code".
   `pip install edge-tts` for voice-note replies.
3. Housekeeping: `athropic api key.txt` (a plain-text note named after an
   API key) and `.env.backup_phase7` (an older copy of `.env`; one key in it
   is not encrypted) are in the project folder — move them somewhere private
   or delete them. Neither is ever put in a drop-in ZIP. Anthropic has no
   credit on that key, so it stays the last fallback and fails honestly.
