# JARVIS Part One — pipeline trace & gap audit

Written before any Part One code was changed (2026-10-01), from the code as it
stood after Phase 6. Each area: what exists, what is partial / missing /
unreliable, and which files get extended (EXTEND > REFACTOR > REPLACE).

## The pipeline as it actually runs

```
USER  mic (sounddevice 16 kHz) ─┐                 text box / dashboard ─┐
                                ▼                                       ▼
INPUT  main._listen_audio → out_queue → session.send_realtime_input     _on_text_command / _process_dashboard_commands
       (mic GATED while JARVIS speaks)                                  → session.send_client_content
                                ▼
ROUTING  Gemini Live (native audio) decides: answer directly, or call one of
         ~45 TOOL_DECLARATIONS (+ plugins)
                                ▼
MODEL    Live model = voice brain (config tier "live").
         think / agents / vision / research → core.ai_gateway.AIGateway
         (tiers fast·normal·deep·vision·research·computer_use·image, per-tier
         providers, alternates, health, fallback chains)
                                ▼
TOOLS    main._execute_tool → JarvisCore.gate_direct_call (permissions/HUD
         confirm) → dispatch → JarvisCore.after_direct_call (audit + verifier
         → [VERIFICATION FAILED]/[UNVERIFIED] text) → FunctionResponse
                                ▼
RESPONSE Live model speaks; output/input transcription buffered; at
         turn_complete → log, short-term memory, dashboard broadcast,
         pending vision injection
                                ▼
VOICE/UI audio_in_queue (50 ms slices) → _play_audio → speaker;
         ui.set_state(LISTENING/THINKING/SPEAKING/EXECUTING/WARNING/ERROR/SLEEPING)
```

## Gap audit — the 8 areas (+ preflight)

| Area | Exists | Partial / missing / unreliable | Extend |
|---|---|---|---|
| 1 Knowledge / retrieval | `core/context_engine.py` layers: now, user, project, fact (identity), episode, workflow, task, audit, turn, window; `memory_manager.search_memory` (lexical over long_term.json) | **No personal-documents layer** (notes/docs never indexed). Context items carry only `source` + text — no id, path, timestamp, confidence, reason. `resolve_period` has no "last month". | new `core/knowledge.py` (index + layers + Source), `ContextItem.ref`, `memory/episodic.resolve_period` |
| 2 Source awareness | — | **Missing entirely.** Nothing records which retrieved items an answer used; no way to show them; nothing stops a fabricated citation. | `core/knowledge.SourceTracker`, `show_sources` tool, UI content panel (SOURCES) |
| 3 Personality | `core/prompt.txt` + `{{PERSONALITY_BLOCK}}` ← `personality_{serious,friendly}.txt`; identity/address built ad hoc in `main._build_config` | Split across 3 places; **voice-only** (gateway answers from `think` get no persona); no honesty/uncertainty/data-not-instructions rules; **no FRIDAY identity**; prompt says "Assume and proceed" without the safety qualifier. | new `core/persona.py` + `config/persona.json`; used by `main._load_system_prompt/_build_config`, `cognition.think` |
| 4 Persistent memory growth | save path: `save_memory` tool → `is_sensitive` → `MemoryHub.save_fact` → `memory/policy.py` (reject/ask/episodic/duplicate/store) → `update_memory` | **No natural-phrase intent** ("remember that…" depends on the model choosing to call save_memory); `save_memory` writes with non-atomic `write_text`; **no read-back verification** (prints "Saved" regardless); date-only timestamp; no provenance (voice/text) or id; "Saved" can be claimed when nothing was written. | `memory/memory_manager.write_fact` (atomic + verify + receipt + version), new `memory/capture.py` (intent + extraction), `remember` tool + turn backstop in `main.py` |
| 5 Current-screen vision | `screen_process` captures at call time (mss → JPEG); image injected after the next `turn_complete`; `core/vision.capture` for analyze_screen / computer agent | **Two capture paths** (screen_processor vs core.vision). Frame can be **stale by the time it is injected** (tool turn + speech). `_vision_busy` is **never reset when capture raises** → vision silently dead for the session. No quality check (blank/dark/tiny), mime trusted not sniffed, no capture-active indicator, capture failure surfaces as a generic tool error. | `core/vision.py` (captured_at, sniff_mime, assess, `fresh()`), `actions/screen_processor` delegates, `AIGateway._messages` sniffs mime, `main.py` screen_process |
| 6 Two-way voice | mic gating, ESC/`interrupt()` drains audio in 50 ms slices, session resumption, affective/proactive audio | Half-duplex: mic is **gated while speaking**, so a spoken "stop" cannot reach the model; `server_content.interrupted` **unhandled**; no end-of-turn (VAD) setting; HUD state set from ~20 places, no explicit interrupted/idle states; **`get_voice()` returns "JARVIS"/"FRIDAY" which are not Gemini prebuilt voices → would break the Live connect.** `core/tts.py` / `core/stt.py` exist but are unused. | new `core/voice.py` (state machine, stop phrases, barge-in gate, identities, provider registry); `memory/config_manager.resolve_live_voice`; `main.py` receive/listen/play loops |
| 7 Exact model switching | tier table + `AIGateway.generate(model=…)` pin | **Absent**: no registry of real ids + spoken aliases, no switch tool, no runtime override, no refusal path, UI does not show the active model. | new `core/model_registry.py`; `AIGateway.set_runtime_model`; `switch_model` tool; BRAIN chip |
| 8 Personal-context distinctions | context-engine intents (current/history/continuation/temporal) | "what did I tell you last month" has no period; no single classifier the tools/prompt agree on. | `core/knowledge.classify_personal_query`, persona routing rules |
| Preflight | `tools/phase5_live_check.py`, `tools/model_probe.py`, `tools/e2e_notepad.py` | Fragmented; no PASS/FAIL/WARN summary; no exit code; nothing checks memory round-trip, sources, interruption, model switch, secrets exposure. | new `core/preflight.py` + `tools/preflight.py` reusing `model_health.probe` |

## Rules carried into the work

- One pipeline: no second router, provider layer, memory store, voice engine,
  vision handler, config system or test harness. New modules plug into the
  existing ones (context engine, memory_manager/policy, AIGateway, JarvisCore,
  core/config, tests/ + unittest).
- Multi-provider stays: Gemini default, NVIDIA/local/others untouched; nothing
  paid becomes mandatory (ElevenLabs stays optional).
- Retrieved content is DATA, never instructions; secrets never indexed, logged
  or surfaced.
