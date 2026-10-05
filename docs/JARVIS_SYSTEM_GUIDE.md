# JARVIS — System Guide (Phase 6 + Part One + Phase 7)

What JARVIS is, what actually works today, and how the pieces fit. This is the
honest functional-status document: every capability is marked ✅ works,
⚠️ works with limits, or ❌ not built, with where it lives in the code.

Builds on `JARVIS_PHASE4_ARCHITECTURE.md` (agents, permissions) and
`JARVIS_PHASE5_COGNITIVE_CORE.md` (context, memory, cognition). Change details
are in `CHANGELOG_JARVIS_PHASE1.md` → "Phase 6", "Part One" and "Phase 7"; the
gap audits are `JARVIS_PART1_AUDIT.md` and `JARVIS_PHASE7_AUDIT.md` (your nine
Phase 7 requests, the 18-prompt pack and Mark LV, each marked meets / exceeds /
partial / not built). Check everything at once with
`python tools/preflight.py` (add `--live` for real provider/voice calls).

---

## 1. The shape of the system

```
 voice / text ─► Gemini Live (native audio) ──tool call──► JarvisCore
                                                              │
  PERCEIVE   context_engine        time · device · window · running tasks
  THINK      model_router          local maths │ fast │ normal │ deep │ vision │
             + config/models.json   research │ computer_use │ image │ live
             + model_health         (per-tier models, fallback chains, health)
  REMEMBER   memory/*              semantic · episodic · procedural · identity
  PLAN       agents/planner        rules first, LLM + context second, validated
  ACT        orchestrator + 16 agents + ~75 tools, permission-gated
             scheduler + gui lock  priorities, ≤3 parallel, one mouse/keyboard
  VERIFY     per-step verifiers + cognition SELF-CHECK ("did I actually do it?")
  RECOVER    recovery classifier   network/quota/timeout/… → retry / replan / ask
  LEARN      episodes · app usage · workflows (offered, never saved silently)
                                                              │
  SAFETY     safety.py kill switch (F12 · STOP · "stop everything") over all of it
```

- **One brain door** — every non-voice model call goes through `AIGateway`
  (`core/ai_gateway.py`). Callers say *what* they need (a tier, an image, a
  schema); the gateway picks the model, the provider, the fallback, and the
  throttle.
- **One capability table** — every tool is one `ToolSpec` in
  `core/tools/registry.py` (risk, confirmation, verifier, timeout, resources).
  The planner, permission engine, orchestrator, audit and HUD all read it.
- **One safety switch** — `core/safety.py`; F12 / "stop everything" cancels
  tasks, HUD prompts, the computer agent, long jobs, and background processes.

---

## 2. Functional status — every capability

### Models & providers
| Capability | Status | Where |
|---|---|---|
| Central model config (per-tier model, fallback chain, effort, tokens) | ✅ `config/models.json` | `core/config.py` |
| `ModelRouter.select(task_type, complexity, requires_vision, …)` | ✅ | `core/model_router.py` |
| Tiers: fast · normal · deep · vision · research · computer_use · image · live | ✅ | `core/config.py`, `core/ai_gateway.py` |
| Per-tier fallback chains + per-model alternates (survive daily quota) | ✅ | `core/ai_gateway.py` |
| Request **simplification** on token-limit errors (trim history/text once) | ✅ | `AIGateway._simplify` |
| **Model health** — remembers missing / no-quota / exhausted models per key | ✅ | `core/model_health.py`, `memory/model_health.json` |
| Image **generation** (`generate_image`) | ⚠️ code ✅, **no free quota on this key** → design falls back to vector | `AIGateway.generate_image`, `core/providers/google_provider.py` |
| **NVIDIA NIM** provider (free, OpenAI-compatible) + per-tier routing (`deep→nvidia`) | ✅ needs `NVIDIA_API_KEY` | `_nvidia` factory, `AIConfig.tier_providers` |
| Anthropic / OpenAI providers | ⚠️ supported, **never auto-selected** (paid) | `core/providers/*` |
| Model probe (`tools/model_probe.py`) — what YOUR key can actually do | ✅ | `tools/model_probe.py` |
| **Exact model switching** by voice ("switch to gemini 3.7 flash") — exact id or refusal with real alternatives; runtime only | ✅ | `core/model_registry.py`, `AIGateway.set_runtime_model`, `switch_model` |
| Active model shown in the UI (BRAIN / LIVE chips) | ✅ | `ui_command_center.StatusChips` |

Checked on your key (27 Sep 2026): `gemini-flash-latest` = 20 requests/day free,
then JARVIS rolls over to `gemini-3.7/3.6/3.5-flash`; every Gemini **image**
model returns "no free quota"; live voice = the native-audio model. Re-check any
time with `python tools/model_probe.py`.

### Tasks
| Capability | Status | Where |
|---|---|---|
| Priorities: CRITICAL · HIGH · NORMAL · LOW · BACKGROUND (inferred or set) | ✅ | `core/tasks/scheduler.py`, `core/tasks/state.py` |
| States incl. **RECOVERING** and **BLOCKED**, progress %, phase | ✅ | `core/tasks/state.py` |
| Scheduler: ≤ N parallel (default 3), priority queue, waiting task frees its slot | ✅ | `core/tasks/scheduler.py` |
| **GUI lock** — one job at a time drives the real mouse/keyboard | ✅ | `ResourceLocks`, orchestrator |
| Per-tool **timeout** watchdog (waiting for you doesn't count) | ✅ | `core/tools/registry.py`, orchestrator `_watchdog` |
| **Error classifier** (network/quota/timeout/not-found/auth/policy/…) → retry vs replan vs ask | ✅ | `core/recovery.py` |
| Structured **undo** records (task/step/tool, paths) + audit trail | ✅ | `core/undo.py` |
| Self-recovery replan (once), retry-from-failure, model degrade | ✅ | `core/agents/orchestrator.py` |

### Doing things
| Capability | Status | Where |
|---|---|---|
| Computer-use agent (observe → plan → act → verify), open_app, focus, **UIA text verify** | ✅ | `core/computer_agent.py`, `core/vision.py` |
| Website **build** team (manager→research→ui→coding→testing→review) | ✅ | `core/team.py` |
| Website **run & check** — serves locally, real headless browser, desktop+mobile, JS errors / 404s / broken links / mobile overflow / vision review | ✅ | `core/webdev.py` |
| Background **processes** (preview servers) — named, logged, stoppable, survive restart | ✅ | `core/processes.py` |
| Web **agent** — read pages / fill non-sensitive forms; refuses passwords/payments, asks before submit | ✅ | `core/browser_agent.py` |
| **Coding agent** `code.task` — edit existing project, backups+undo, run tests, fix ≤3 rounds | ✅ | `core/code_agent.py` |
| **Design** `design.vector` (SVG, rendered + vision-checked) · `design.image` (honest fallback) | ✅ | `core/design_agent.py` |
| Vision: screen · window · camera · files; modes ui/errors/code/chart/document | ✅ | `core/vision.py` |
| Files, apps, browser, system, reminders, research, messaging (send needs confirm) | ✅ (Phase 4/5) | `actions/*`, `core/tools/builtin.py` |

### Knowing things
| Capability | Status | Where |
|---|---|---|
| Personal model (user, projects+paths, people, goals, prefs, usual apps) | ✅ | `memory/identity.py` |
| **Self model** — "who are you / what can you do / can you X" from live tools + real model health | ✅ | `core/self_model.py` |
| Proactive: offer to resume **unfinished work** (once, not naggy) | ✅ | `core/self_model.py`, `core/proactive_policy.py` |
| Device manifest + capability list (sync-safe, no secrets) | ✅ | `JarvisCore.device_manifest` |
| **Personal knowledge**: your notes (`knowledge/`, `Documents/JARVIS Notes`, project READMEs) + memory + episodes + tasks, ranked, with time filters ("last month") | ✅ | `core/knowledge.py`, `search_knowledge` |
| **Sources**: what each answer actually used (id, path, date, confidence, why); SOURCES panel; never invented | ✅ | `SourceTracker`, `show_sources` |
| **"Remember that…"** → verified write (atomic, re-read), immediately recallable, provenance + timestamps | ✅ | `memory/capture.py`, `memory_manager.write_fact`, `remember` |
| Personal-context routing: "what am I looking at" vs "what was I doing" vs "what did I tell you" vs "do this" | ✅ | `classify_personal_query`, `prompt.txt` |
| Phone: read-only task view over the dashboard | ✅ endpoints | `dashboard/server.py` `/api/tasks` |
| Phone **agent** (run tasks on a phone) | ❌ companion app not built — `PhoneDevice` reports itself unavailable | `core/devices/base.py` |
| Calendar | ❌ no integration | — |

### Voice & personality
| Capability | Status |
|---|---|
| Wake word, English / Tamil / Tanglish, the holographic core UI, Friendly (blue) / Serious (red) / listening (green) | ✅ (Phase 5 + Mark LIII.2 UI) |
| One persona (JARVIS / FRIDAY / custom) for voice AND every gateway provider — honesty, uncertainty, sources, data-not-instructions, AI disclosure | ✅ `core/persona.py`, `config/persona.json` |
| JARVIS / FRIDAY voices → real Gemini voices (Charon / Kore) | ✅ `get_live_voice()` |
| Voice states idle/listening/thinking/speaking/interrupted/error (VOICE chip) | ✅ `core/voice.py` |
| Interrupt: ESC, server barge-in, spoken "stop / wait / hold on / JARVIS, stop" | ✅ (speakers: adaptive echo gate — see limits) |
| One end-of-turn threshold (`voice.end_of_turn_silence_ms`) | ✅ (unset = Gemini default) |
| Current-frame vision: re-grab if older than 2.5 s, capture time + quality check, EYE chip, honest "can't see" | ✅ `core/vision.py` |
| ElevenLabs / Edge / Kokoro speech | ⚠️ optional, out-of-session only — never required |

---

## 3. Safety model (unchanged rules, extended reach)

- **Permission engine** — risk levels; HIGH/CRITICAL need HUD CONFIRM; the model
  can never press it. Remote/phone origins tighten to MEDIUM.
- **Computer & browser agents** — block passwords, card/ID numbers, CAPTCHAs,
  terminals, the Run dialog, admin/UAC; ask first for pay/send/delete/submit.
- **No shell for the model** — there is no general terminal tool. The only
  processes JARVIS starts are fixed recipes (a local preview server bound to
  127.0.0.1 inside a workspace folder).
- **Secrets** — read only through `core/secret_store.py` (DPAPI-encrypted
  `.env`). Never printed to logs, UI, audit, or reports; `redact()` scrubs
  everything model-facing. Config files that look like they hold a key are
  ignored. Window text read for verification is checked, never logged.
- **GUI lock** — a background task and a direct voice command can't fight over
  the mouse and keyboard.
- **Emergency stop** — F12 / STOP / "stop everything".
- **Phase 7** — HUD confirmations are ALLOW ONCE / ALWAYS / DENY; "always" is
  revocable in Mission Control → Permissions and is never offered for CRITICAL
  actions. Telegram tasks run as origin `telegram` (MEDIUM also asks at the
  HUD); the phone can stop JARVIS but never approve or un-stop it. The phone
  dashboard refuses foreign Host / Origin, caps bodies, locks out PIN guessing.

---

## 4. Try it

- "What's 1234 × 5678 + 99?" → answered exactly on the PC, no model call.
- "Build me a portfolio website." → six agents build it, then JARVIS runs it in
  a real browser, checks it, and opens it.
- "Fix the bug in my Mark-LII project." → finds the files, edits, runs the
  tests, fixes up to three rounds — only says done if the tests pass.
- "Draw me a logo for JARVIS." → an SVG, rendered and checked by eye.
- "Open Notepad and type Hello JARVIS." → the computer agent does it and
  confirms by reading the window's text.
- "What can you do?" / "Can you generate images?" → an honest answer from the
  live tool list and your key's real limits.
- "Stop everything." / F12 → emergency stop.
- "Remember that my sister's name is Ayse." → saved, verified, then "What did I
  just ask you to remember?" answers from the store immediately.
- "What do my notes say about the Goa trip?" → searches your notes; "Where did
  you get that?" → shows the exact sources.
- "What am I looking at?" → a fresh screen capture, never an old frame.
- "Switch to Gemini 3.7 Flash." / "Use laguna." / "Which model are you using?"
  / "Go back to the default model."
- While JARVIS talks: "Stop." / "Hold on." (with headphones, or loud enough over
  speakers) or ESC.
- Out of Gemini quota for the day? Add `NVIDIA_API_KEY` and set
  `"tier_providers": {"deep": "nvidia"}` in `config/models.json` — deep
  reasoning then runs on NVIDIA's free NIM models.

### Phase 7
- **F9** (or MISSION in the dock) → Mission Control: brain, voice model, voice
  latency, tasks, schedule, logs, "always" list, what JARVIS learned.
- "Watch my screen." / "Stop watching." → red EYE while frames go to the voice
  model (paused on sign-in / banking windows; off after 10 minutes).
- "Draw something you like." / "Draw a cat." → Paint opens, ALLOW ONCE on the
  HUD, JARVIS draws with the mouse; F12 lifts the pen.
- "Keep this as a reference." → the screen (or "this file / this link /
  my clipboard") is kept; "Review this and build me a website like it, with
  content from the web" → the team uses it.
- "What have you learned about me?" / "Forget that." / "Pause learning."
- "Use nemotron ultra." / "Use Claude Sonnet." / "Go back to the default model."
- Mic modes in `config/runtime.json → voice.mic_mode`: `always` (default),
  `wake_word` ("Hey JARVIS"), `push_to_talk` (hold F8).
- Telegram (optional, free): bot from @BotFather → `TELEGRAM_BOT_TOKEN=…` in
  `.env` → restart → "pocket code" → send the HUD code to your bot. Then from
  the phone: "find the budget file", "remember that …", "status", "stop",
  "answer: …", a voice note. At the desk: "I'm going out, only message me if
  it's urgent" / "I'm back" / "text me now". `pip install edge-tts` adds
  voice-note replies.

---

## 5. Honest limits (Phase 6)

- **Image generation** — no free quota on your Gemini key; `design.image` says so
  and draws a vector instead. Works automatically if Google adds free image quota.
- **Free-tier load** — Google's free vision/flash endpoints return "503 high
  demand" under load; the computer agent and gateway retry and fall back, but a
  vision-heavy job can still stall until quota/load frees. NVIDIA NIM as a
  second free provider is the mitigation.
- **Phone** — architecture, pairing, capability vocabulary and a read-only task
  view exist; the companion app that would *run* tasks on a phone does not.
- **Calendar** — no integration.
- **Calls** — out of scope (the Call Agent was explicitly excluded).
- **Plugins run in-process** — bundled, hash-pinned catalog only; no internet
  marketplace (by design).
- **Voice (speaker) authentication** — not done; identity is your Windows login.
- `test_redaction` in a live checkout depends on whether your key is
  DPAPI-encrypted; the assertion was made platform-robust.
- **Part One limits** — retrieval is keyword (BM25), not semantic; PDF/DOCX
  notes are not indexed; "used sources" are inferred from word overlap with the
  spoken answer; speaker-mode barge-in is an adaptive threshold, not echo
  cancellation (`voice.barge_in: "off"` restores the old behaviour); switching
  the voice model restarts the conversation.
- **Phase 7 limits** — no paid phone line / hotline (Telegram instead); no
  Google Calendar/Gmail line yet; "learning" shapes behaviour around the model
  (Gemini is not retrained); Telegram pushes need JARVIS running; the echo
  guard is a spectral comparison, not full echo cancellation; drawing works in
  Microsoft Paint only.
