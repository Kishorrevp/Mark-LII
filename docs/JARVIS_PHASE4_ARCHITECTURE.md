# JARVIS — Phase 4A–4K: Audit, Architecture & Operator Guide

This document has two halves:

* **Part I — Audit** (done before any code changed): what the codebase looked like, what was missing, what was unsafe, and the order chosen.
* **Part II — Final deliverable**: the architecture now in place, every file touched, test results, known limits, and how-to guides (providers, agents, tools, permissions, verification).

The detailed per-change reasoning is in `CHANGELOG_JARVIS_PHASE1.md` → "Phase 4A–4K".

> **About the phase numbers.** Your earlier plan called the remaining work "Phases 6–8" (from the Section-45 audit). This brief renumbers the future work as Phase 5 = Advanced Laptop Agent, 6 = Smartphone, 7 = Call Agent, 8 = Proactive Intelligence, and asks for the core architecture first as "4A–4K". I followed this brief's numbering. Phase 5's earlier *subprocess-sandbox* work is kept as it is.

---

# Part I — Audit (read-only pass)

## I.1 Architecture before this phase

```
                 ┌──────────────────────────── ui.py (PyQt6, 4.3k lines) ────────────────────────────┐
 mic ──────────► │ HUD (Phase 3/3b colours+motion) · log · content panel · confirm banner · settings │
                 └──────────────▲───────────────────────────────────────────────▲────────────────────┘
                                │ set_state / write_log / show_confirm          │ on_text_command
 main.py (2.1k lines) ──────────┴───────────────────────────────────────────────┴──────────────────
  JarvisLive ── Gemini Live session (native audio, tools=TOOL_DECLARATIONS[26] + plugins)
     │  _execute_tool(): one big if/elif → calls actions/*.py directly
     │  background loops: system monitor · news monitor · proactive check-ins · dashboard relay
     ▼
  actions/*.py (20 modules)  ── each opened config/api_keys.json and built its own genai.Client
     ├─ file_controller (undo-aware)      ├─ browser_control (playwright)   ├─ computer_settings (self-gated restart/shutdown/wifi)
     ├─ desktop (subprocess sandbox, gated "task")  ├─ send_message (GUI automation, NO gate)
     ├─ code_helper / dev_agent (via core/ai_provider.get_provider — Google only)
     └─ web_search · youtube · flight_finder · file_processor · screen_processor · computer_control · …
  core/: confirm.py (HUD gate) · undo.py (undo stack) · risk.py (3-tier table, mostly unenforced)
         ai_provider.py (Google real; Anthropic/OpenAI/Local = NotImplementedError stubs)
         plugin_loader.py (drop-in plugins, no risk metadata) · audio_devices.py
  memory/: memory_manager.py (long_term.json, categories identity/preferences/projects/…) · config_manager.py
  dashboard/server.py: FastAPI remote dashboard (HTTPS, phone mic relay, AES)
```

## I.2 Feature inventory (existing, preserved)

| Area | What exists | Where |
|---|---|---|
| Voice | Gemini Live native audio, session resumption, affective dialog, interrupt, device picker | main.py, core/audio_devices.py |
| HUD | Friendly/Serious motion + colour identity, state colours, cross-fades, voice-reactive waveform | ui.py (Phase 3/3b) |
| Tools | 26 Live tools (apps, web, files, browser, desktop, code, settings, messages, reminders, vision, games, flights, memory, mode, undo) | main.py TOOL_DECLARATIONS, actions/ |
| Safety | HUD confirmation (model can't forge), undo stack, desktop "task" subprocess sandbox + static scan, dev_agent install allow-list | core/confirm.py, core/undo.py, actions/desktop.py, actions/dev_agent.py |
| Memory | Long-term JSON store, prompt-budgeted core block, recall search, session summaries | memory/memory_manager.py |
| Proactive | System monitor alerts, topic monitor, idle check-ins, morning briefing | main.py loops, actions/proactive.py |
| Plugins | Discovery, validation, collision detection, enable/disable | core/plugin_loader.py |
| Dashboard | Remote HTTPS dashboard, phone mic relay | dashboard/ |

## I.3 Missing features (per this brief)

Tool registry · task state machine · agent router · task planner · orchestrator · central permission engine (5 levels) · result verification · controlled recovery · agent-to-agent structured data · categorised memory API · learned workflows · Tanglish/Tamil-aware command matching · device (laptop/phone) interfaces · call-agent interfaces · proactive quiet-hours/rate-limit policy · structured audit trail.

## I.4 Partially implemented

| Item | State found |
|---|---|
| AIProvider abstraction | Only `generate_text(prompt)`; Google only; 3 NotImplementedError stubs; 7+ files still built their own `genai.Client` |
| Risk tiers | `core/risk.py` table existed but only "high" was enforced, and only by two actions gating themselves |
| Confirmation | Worked, but no "declined/expired" callback (a waiting task couldn't learn it was refused); no active expiry |
| Plugins | No risk/permissions/lifecycle metadata; ran without any permission check |
| Multilingual | Live model handles Tamil natively; nothing deterministic did; prompt said "never mix two languages" (contradicts Tanglish) |
| Dead code | Phase 1 deleted `core/llm_client.py`, `stt.py`, `tts.py`, `installer.py` — they are **back in your folder** (a drop-in zip can't delete files). Still unreferenced. Left in place — see Part II.10 |

## I.5 Gemini-specific code found

`main.py` (Live), `actions/screen_processor.py` (legacy Live vision session), and direct `genai.Client` text/vision calls in `flight_finder`, `file_processor`, `web_search`, `desktop`, `youtube_video`, `computer_control`, `code_helper.screen_debug`; key reads in 11 files + `ui.py` + `dashboard/server.py` + `memory/config_manager.py`.

## I.6 Security issues found

| # | Issue | Severity | Status |
|---|---|---|---|
| S1 | API key stored in `config/api_keys.json` next to ordinary settings; read by 11+ files | High | **Fixed** — `.env`/env via `core/secret_store.py`, auto-migration |
| S2 | Setup overlay overwrote the whole settings file (lost voice, devices, mode) when saving a key | Medium | **Fixed** |
| S3 | `open_app` ran model-supplied names with `shell=True` → command injection (`notepad.exe & del …`) | High | **Fixed** |
| S4 | `rename_file` joined `new_name` unchecked → path traversal out of the safe root | High | **Fixed** |
| S5 | File tools could read/write `~/.ssh`, credential stores, browser profiles, and JARVIS's own `.env`/`api_keys.json`/TLS key (→ read aloud or sent to a model) | High | **Fixed** (deny-list) |
| S6 | `send_message` sent with no confirmation | High | **Fixed** (HIGH_RISK → HUD confirm) |
| S7 | File delete had no confirmation | Medium | **Fixed** (HIGH_RISK per brief; still recycle-bin + undo) |
| S8 | `game_updater shutdown_when_done` powered the PC off with no gate | Medium | **Fixed** |
| S9 | `code_helper` overwrote user files with model output, no undo | Medium | **Fixed** (undo snapshot) |
| S10 | Console logs printed raw tool args (message bodies, memory values) | Medium | **Fixed** (redacted) |
| S11 | Memory would store passwords/OTPs/card numbers if asked | Medium | **Fixed** |
| S12 | A pending HUD confirmation never actively expired | Low | **Fixed** (timer → denial) |
| S13 | Plugins run arbitrary code in-process at import | High (inherent) | **Documented, not fixable in-process** — calls now gated/audited; see II.10 |
| S14 | `browser_control` click/type/fill_form and `computer_control` can drive any site/app (MEDIUM, no confirm) — a purchase or settings change via GUI can't be recognised generically | Medium | **Documented** — pending page-aware CRITICAL detection (Phase 5) |
| S15 | Dashboard-originated commands are treated like local voice | Low | **Pending** — device layer supports `origin="remote"`; dashboard not rewired yet |
| S16 | `dev_agent._open_vscode` uses `shell=True` with a fixed argv | Low | Reviewed, left (no model-controlled text reaches it) |

## I.7 Circular-import risks

`main.py` imports everything; nothing may import `main.py` or `ui.py`. New modules follow that: the composition root (`core/jarvis_core.py`) is imported *by* main.py and receives the UI via dependency injection (callbacks). Action modules are imported lazily inside tool handlers, so building the registry never imports PyQt/pyautogui/genai.

## I.8 Implementation order chosen

Exactly the brief's order (4A → 4K), with tests run after each stage (see Part II.8).

---

# Part II — Final deliverable

## II.1 Updated architecture

```
 User (voice / text, English · Tamil · Tanglish)
   │
   ▼
 Gemini Live (main.py) — realtime voice transport, intentionally Gemini-specific
   │ tool call
   ▼
 JarvisCore (core/jarvis_core.py) — composition root, dependency-injected
   ├─ single tool call ─► PermissionEngine ─► ALLOW → action · CONFIRM → HUD banner · DENY
   │                     └─► after: Verification + AuditLog + Workflow observer
   └─ run_task ─► AgentRouter ─► TaskPlanner (rules → LLM fallback → validator)
                                   │
                                   ▼
                        Orchestrator (Agent Manager) + TaskStore (state machine)
                                   │ per step
                    ┌──────────────┼──────────────────────────────┐
                    ▼              ▼                              ▼
             PermissionEngine   Specialist Agent            Verification
             (5 risk levels,    (General · Laptop · Browser   (path exists/absent,
              trusted actions,   · File · Coding · Research    file valid, process,
              HUD confirmation)  · Communication · System      unverifiable ≠ done)
                                 · Vision · Reminder · Plugin)
                                   │ AgentServices (only the approved tool)
                                   ▼
                              ToolRegistry  (single source of truth: 48 tools + plugins)
                                   │ lazy adapters
                                   ▼
                              actions/*.py (unchanged behaviour) · plugins/*.py
   Shared: AIGateway → {Google (default, free) · Local/Ollama (free) · Anthropic (paid, opt-in) · OpenAI (paid, opt-in)}
           secret_store (.env) · AuditLog · MemoryHub (7 categories) · WorkflowStore · NotificationPolicy
           DeviceRouter (Laptop ✔ / Phone interface) · PairingManager · CallPolicyEngine (interfaces)
   HUD: task states mapped onto existing HUD states (THINKING / EXECUTING / WARNING / ERROR / LISTENING) — no HUD code changed
```

## II.2 Files created

| File | Purpose |
|---|---|
| `core/secret_store.py` | Key resolution (env → `.env` → legacy), atomic `.env` writes, migration, redaction |
| `core/providers/base.py` | Provider interface, capabilities, request/response model, errors, HTTP seam |
| `core/providers/google_provider.py` | Gemini (text, stream, JSON schema, tools, vision, audio, PDF, thinking, grounding, token count) |
| `core/providers/anthropic_provider.py` | Claude Messages API over HTTPS (paid, opt-in) |
| `core/providers/openai_provider.py` | OpenAI / any OpenAI-compatible server (paid when api.openai.com) |
| `core/providers/local_provider.py` | Ollama (free, offline) |
| `core/ai_gateway.py` | The gateway: config, capability checks, retries, opt-in fallbacks |
| `core/permissions.py` | RiskLevel (5), PermissionEngine, trusted/blocked actions |
| `core/verification.py` | VERIFIED / FAILED / UNVERIFIABLE checks |
| `core/audit.py` | Redacted JSONL audit trail, "what did you do?" |
| `core/tools/registry.py` | ToolSpec / ToolResult / ToolRegistry + schema validation |
| `core/tools/builtin.py` | Registers 26 legacy tools + 22 structured agent tools |
| `core/tasks/state.py` | TaskState machine, Step, Task, persistent TaskStore |
| `core/agents/base.py` | Agent, AgentServices (the only door an agent has), AgentRegistry |
| `core/agents/builtin.py` | The 11 specialist agents |
| `core/agents/router.py` | Clause-level router (EN/TA/Tanglish) |
| `core/agents/planner.py` | Rule planner + LLM planner + plan validator |
| `core/agents/orchestrator.py` | Agent Manager: execution, permission waits, input waits, retries, alternatives, verification, cancel/pause/resume |
| `core/jarvis_core.py` | Composition root + direct-call PermissionGate + plugin→registry bridge |
| `core/multilingual.py` | Language detection, Tamil/Tanglish normalisation, reply-language preference |
| `core/workflows.py` | Learned-workflow detection, suggestion, save, run |
| `core/proactive_policy.py` | Quiet hours, rate limits, per-source switches, ProactiveSource registry |
| `core/devices/base.py` | DeviceAgent interface, LaptopDevice, PhoneDevice (unavailable), DeviceRouter |
| `core/devices/pairing.py` | 6-digit pairing, per-device secrets, HMAC signing, replay protection, revoke |
| `core/calls/interfaces.py` | Call Agent contract + policy engine (disclosure, anti-impersonation, urgency, escalation) |
| `memory/memory_hub.py` | 7-category memory facade, sensitive-data refusal, inspect/summarize |
| `.env.example` | Placeholders only |
| `tests/` (4 files) | 58 unit/integration tests |
| `JARVIS_PHASE4_ARCHITECTURE.md` | This document |

## II.3 Files modified

`main.py` (imports, 5 new tools, central gate, post-call verification/audit, language block, proactive policy, secret migration, redacted logs, short-term context) · `ui.py` (`_check_config`, `_on_setup_done` only) · `core/ai_provider.py` (facade over gateway) · `core/confirm.py` (on_cancel, expiry timer, cancel_pending) · `core/plugin_loader.py` (risk/permissions/config/verify/lifecycle) · `core/risk.py` (shim over the registry) · `core/prompt.txt` · `memory/config_manager.py` · `dashboard/server.py` (key lookup only) · `actions/`: `file_controller.py` (find_paths, traversal + sensitive-path fixes), `file_processor.py` (gateway adapter, `extract_text`), `web_search.py`, `flight_finder.py`, `youtube_video.py`, `desktop.py`, `computer_control.py`, `code_helper.py` (gateway + undo), `computer_settings.py` (dead key reader removed), `screen_processor.py` (key via secret_store), `open_app.py` (injection fix) · `plugins/_template.py` · `.gitignore`.

## II.4 Files intentionally left untouched

HUD rendering and all Phase 3/3b visuals in `ui.py` (only the two setup/config methods changed) · `core/undo.py` · `core/audio_devices.py` · `memory/memory_manager.py` · `actions/browser_control.py`, `send_message.py`, `reminder.py`, `system_monitor.py`, `proactive.py`, `background_monitor.py`, `game_updater.py`, `weather_report.py`, `dev_agent.py` · `dashboard/static/*` · `core/personality_*.txt` · `requirements.txt` (no new dependencies: providers use `requests`, already required).

## II.5 Features implemented (working in code, covered by tests)

Provider gateway with 4 real providers · secrets in `.env` + migration + redaction · Tool Registry (48 tools + plugins, full metadata) · 5-level Permission Engine used by **every** direct tool call, task step, workflow step and plugin · HUD confirmation with decline/expiry callbacks (silence = no) · Task state machine with persistence and honest restart handling · Router (EN/TA/Tanglish, multi-agent, extensible) · Planner (rules + validated LLM fallback, dependencies, `$ask` questions) · Orchestrator (retries bounded & only when safe, alternatives only if ALLOWed, verification, cancel/pause/resume/answer, "what are you doing?") · Structured agent-to-agent outputs (`$s3.path`, `$s6.summary`) · Verification that refuses to call a fake success "done" · Audit log & "what did you do?" · Memory hub (7 categories, no secrets) · Learned workflows (suggest once, save only on yes, run through permissions) · Reply-language preference (`set_language`) · Proactive quiet hours/rate limits wired into all three existing loops · Plugin risk/permissions/lifecycle/verify · Device & pairing layer · Call-agent policy layer.

## II.6 Features still pending (not faked)

* **Phone companion app & transport** (Phase 6) — `PhoneDevice` reports unavailable.
* **Call Agent telephony** (Phase 7) — `TelephonyUnavailableAgent` raises; only policy logic exists.
* **Future proactive sources** (calendar, project, notification monitors — Phase 8) — base class + registry only.
* ~~Ambient / Command-Center panel windowing~~ — **done** in the follow-up "Mark LIII interface" phase (`ui_command_center.py`; see CHANGELOG).
* **HUD visuals for PLANNING/VERIFYING/WAITING** — states are mapped onto existing HUD states; dedicated visuals would be a HUD change (deferred, as instructed).
* **Voice transport abstraction** — Gemini Live stays Gemini-specific (no free equivalent to abstract over).
* **Remote-origin tagging for dashboard commands** (S15), **page-aware CRITICAL detection for browser/GUI automation** (S14), **OS keyring storage**.
* **Calendar integration** — Reminder agent uses the existing reminder action only.

## II.7 Security issues fixed

S1–S12 in the table in I.6. Plus: provider error messages and audit records pass through `redact()`; the permission engine denies unknown tools and schema-invalid arguments; agents can only call the tool their step was approved for (enforced in `AgentServices`, tested).

## II.8 Tests performed & results

Run from the project root: `python -m unittest discover -s tests -t .`

| Suite | Tests | Level |
|---|---|---|
| `test_providers.py` | 16 | Genuine execution; network replaced by a fake transport replaying documented API shapes; Google via fake client |
| `test_architecture.py` | 22 | Genuine execution — real planner, router, permissions, orchestrator and **real file tools** on a temporary home folder (spec request end-to-end: file actually renamed + moved, send blocked until approval, decline → CANCELLED, ambiguous file → WAITING_FOR_INPUT, fake success → verification FAILED, bounded retry, alternative tool, cancel, pause/resume, state sequence, traversal blocked) |
| `test_extensions.py` | 15 | Genuine execution of memory hub, workflows, multilingual, proactive policy, pairing/HMAC, device router, call policy, plugin integration |
| `test_main_smoke.py` | 5 | Imports the **real main.py** and drives `_execute_tool` (gate → HUD banner, secret refusal, run_task/task_control/memory_inspect, system prompt contains the language block). Missing third-party packages are stubbed in the sandbox — see caveat below |
| **Total** | **58 — all passing** in the build sandbox (Python 3.11) | |

Structural validation (no execution): every `.py` file byte-compiles; `TOOL_DECLARATIONS` parsed with `ast.literal_eval` (31 tools); every declared Live tool is present in the registry (otherwise the gate would deny it — tested).

**Not verified here, needs your machine:** real Gemini Live voice, microphone, HUD rendering, PyQt event loop, Windows-specific actions. The sandbox has no PyQt6/google-genai/PortAudio and no network to install them. See II.12 for a 5-minute manual check.

## II.9 Known limitations

* Rule planner covers common verbs (find, rename, move, copy, delete, open, read, summarise, search, download, message/send); anything else goes to the LLM planner (uses the free Gemini tier by default, ~1 extra request per unusual task).
* `send_message` is GUI automation — delivery can't be confirmed; JARVIS now says "sent, not confirmed" instead of "done".
* App-launch verification is best-effort (process-name match); a miss is reported as *unverified*, not as failure.
* Only one HUD confirmation can be pending at a time (a second request supersedes the first and the first is told so).
* Paid providers were verified only against documented request/response shapes (no keys available); Ollama likewise.

## II.10 Left in place on purpose

* `core/llm_client.py`, `core/stt.py`, `core/tts.py`, `core/installer.py` — unreferenced leftovers that Phase 1 removed; I did not delete files from your folder. Safe to delete by hand.
* Plugin in-process execution (S13) — true isolation needs a subprocess plugin host; out of scope.

## II.11 How to configure provider / model

Edit `.env` in the project folder (copy from `.env.example`), or `config/api_keys.json` keys `ai_provider`, `ai_model`, `ai_model_fast`, `ai_effort`, `ai_fallbacks`. Environment/`.env` wins.

```ini
GOOGLE_API_KEY=...            # required (voice uses Gemini Live)
JARVIS_AI_PROVIDER=google     # google | local | anthropic | openai
JARVIS_AI_MODEL=              # blank = gemini-flash-latest for google
JARVIS_AI_EFFORT=             # none | low | medium | high
```

## II.12 How to run JARVIS (and verify this phase)

1. Unzip over your folder (drop-in), `pip install -r requirements.txt` (unchanged).
2. `python main.py`. First launch prints `[Secrets] migrated Google API key from config/api_keys.json to .env` — the key now lives in `.env`.
3. Quick manual checks (≈5 min): say "what are you doing?" → "not working on any task"; say "delete test.txt on my desktop" → HUD banner appears, nothing deleted until CONFIRM; say "find my <some file> PDF and move it to my Documents folder" → JARVIS plans, moves, reports verified result; say "cancel that" during a long task; say "Chrome open pannu"; say "what did you do?".
4. Tests: `python -m unittest discover -s tests -t .`

## II.13 How to switch AI providers

Free & offline: install Ollama, `ollama pull qwen3:8b`, then `JARVIS_AI_PROVIDER=local` and `JARVIS_AI_MODEL=qwen3:8b`. Paid (only if you choose): set `ANTHROPIC_API_KEY` + `JARVIS_AI_PROVIDER=anthropic` + `JARVIS_AI_MODEL=<model id>`. Voice keeps using Gemini Live either way. Optional safety net: `JARVIS_AI_FALLBACKS=google`.

## II.14 How to add a new agent

```python
# core/agents/builtin.py (or your own module)
class HomeAgent(Agent):
    name = "home"; title = "Home Agent"
    keywords = {"lights": 5, "thermostat": 5, "fan": 3}
    tool_prefixes = ("home.",)
# register it in default_agents(): AgentRegistry([... , HomeAgent()])
```
The router uses its keywords automatically; the orchestrator dispatches any tool whose `agent="home"` to it.

## II.15 How to add a new tool

```python
reg.register(ToolSpec(
    "home.lights", "Turn lights on/off.", handler=lambda args, ctx: ToolResult.success("Lights on", state="on"),
    input_schema={"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]},
    agent="home", risk=RiskLevel.LOW_RISK, retry_safe=True,
    verify=lambda args, res: V.verified("bridge acknowledged") if res.ok else V.failed(res.error)))
```
Add it in `core/tools/builtin.py`, or ship it as a plugin (`plugins/_template.py` — `risk`, `permissions`, `verify()`, `on_load()`). To expose it to Gemini Live directly as well, add a declaration to `TOOL_DECLARATIONS` and a dispatch branch in `main.py`.

## II.16 How permissions work

`PermissionEngine.decide(tool, args, context)` → **ALLOW / CONFIRM / DENY**. Risk comes from the tool's spec (per-action where it varies). SAFE/LOW/MEDIUM run (MEDIUM actions are undoable); HIGH needs the HUD CONFIRM button; CRITICAL always needs it and can't be trusted-listed; unknown tools and invalid args are denied; remote/workflow-untrusted origins confirm from MEDIUM. Configure in `config/api_keys.json`: `"trusted_actions": ["send_message"]`, `"blocked_actions": ["file.delete"]`. A spoken "yes" never approves — only the button. An unanswered confirmation expires as a **no**.

## II.17 How task verification works

Each tool may carry `verify(args, result) → VERIFIED | FAILED | UNVERIFIABLE`. The orchestrator enters VERIFYING after each such step: FAILED → retry if the tool is retry-safe, else the task FAILS and says what was and wasn't done; UNVERIFIABLE → the step completes but the final report says "I could not independently confirm …". At the end the task itself passes through VERIFYING (every step must be DONE) before COMPLETED. Direct voice tool calls get the same verifier afterwards; a failed check is appended to the result as `[VERIFICATION FAILED]`, and the prompt instructs JARVIS never to call that done.
