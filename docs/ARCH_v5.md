# JARVIS — Phase 5: Cognitive Core

Builds on the Phase 4 agent architecture (`JARVIS_PHASE4_ARCHITECTURE.md`) and the
Mark LIII interface. Change log with verification details:
`CHANGELOG_JARVIS_PHASE1.md` → "Phase 5 (Cognitive Core)".

## 1. The loop

```
 voice / text ─► Gemini Live (native audio) ──tool call──► JarvisCore
                                                             │
   PERCEIVE   core/context_engine.py   time · device · battery · foreground window*
                                       · running tasks        (*only for "this/here/screen")
   THINK      core/model_router.py     local exact maths │ fast │ normal │ deep │ vision
   REMEMBER   memory/*                 semantic · episodic · procedural (budgeted, relevant only)
   PLAN       core/agents/planner.py   rules first, LLM + context second, always validated
   ACT        core/agents/orchestrator.py + 15 agents + 64 tools, permission-gated
   VERIFY     per-step verifiers + core/cognition.py SELF-CHECK ("did I actually do it?")
   LEARN      episode + project activity + app usage; workflows offered, never saved silently
                                                             │
   SAFETY     core/safety.py kill switch (F12 · STOP · "stop everything") over all of it
```

## 2. Status of every requested item

| # | Request | Status | Where |
|---|---------|--------|-------|
| 1 | True model router (fast / normal / deep / vision) | ✅ + local exact maths, free-tier throttle, 5xx/429 degrade | `core/model_router.py`, `core/ai_gateway.py` |
| 2 | Context engine (who, what, project, discussion, computer, tasks) | ✅ | `core/context_engine.py` |
| 3 | Personal identity layer, selective | ✅ user, projects+paths, people, goals, prefs, usual apps, routines · ⚠ devices = this PC only | `memory/identity.py`, `memory/policy.py` |
| 4 | Long-running background tasks | ✅ research → collect → analyse → store → document → verify → notify; project analysis | `core/research.py` |
| 5 | Multi-agent collaboration | ✅ Manager, Research, UI, Coding, Testing, Review | `core/team.py`, `core/agents/builtin.py` |
| 6 | Cognitive core | ✅ | `core/cognition.py`, `core/jarvis_core.py` |
| 7 | Real context engine (conversation, memory, task, app, screen, device, time, projects, prefs, previous actions) | ✅ · ❌ calendar (no integration exists) | `core/context_engine.py` |
| 8 | Semantic / episodic / procedural memory | ✅ | `memory/memory_hub.py` |
| 9 | Workflow learning (observe → recognise → ask → approve → store) | ✅ (Phase 4 store, now procedural memory + context) · ❌ time-of-day schedules | `core/workflows.py` |
| 10 | Stronger vision | ✅ screen, window, camera, image files; modes ui/errors/code/chart/document/camera · no offline OCR | `core/vision.py` |
| 11 | Computer-use agent | ✅ observe → plan → act → observe → verify, code-level safety policy | `core/computer_agent.py` |
| 12 | Plugin ecosystem (find / install / request permission) | ✅ bundled SHA-256-pinned catalog, HUD-confirmed install, first-use permission grants · ❌ internet marketplace (by design) | `core/plugin_market.py`, `plugins/available/` |
| 13 | Security architecture | ✅ capability permissions, action approval, DPAPI vault, workspace sandbox, audit, emergency stop · ⚠ identity = Windows login (no voice-ID) · ⚠ plugins run in-process | see §5 |
| 14 | Self-verification before "Done" | ✅ SELF-CHECK on every task, critique on deep answers, computer agent verify-look, team review | `core/cognition.py` |
| 15 | Self-recovery | ✅ task replan (once), retry-from-failure, model degrade, no-effect detection, team fix rounds | `core/agents/orchestrator.py` |

## 3. Model lanes (free tier)

| lane | model | used for |
|------|-------|----------|
| local | none — exact `Fraction` arithmetic | "What's 25 × 42?", "15% of 2400", "sqrt of 144" |
| fast | gemini-flash-lite-latest | chit-chat, look-ups, sub-queries, critiques |
| normal | gemini-flash-latest | writing, explanations, planning JSON |
| deep | gemini-flash-latest + 16k thinking, 8k output | whole-project analysis, research synthesis, debugging, comparisons |
| vision | gemini-flash-latest + image | screen/camera/image analysis, computer agent |

Config (`.env` or `config/api_keys.json`): `JARVIS_AI_MODEL_DEEP` / `ai_model_deep`,
`JARVIS_AI_MODEL_VISION` / `ai_model_vision`, `JARVIS_AI_RPM` / `ai_rpm` (0 = off),
`JARVIS_AI_RATE_WAIT` / `ai_rate_wait` (max seconds to wait for a slot, default 20).

## 4. Memory

* **Semantic** — `memory/long_term.json` (unchanged format) + `memory/identity.json`
  (structured: projects with paths, app usage). App usage is the only thing learned
  without being told; switch off with `"learn_usage": false`.
* **Episodic** — `memory/episodes.jsonl`: `{ts, kind, title, summary, project, refs,
  outcome}`; sessions, tasks, artifacts, events, decisions. No transcripts, no message
  bodies, secrets scrubbed. Events kept 30 days, the rest 365, 3,000 max.
* **Procedural** — `memory/workflows.json`.
* **Policy** on every `save_memory`: reject secrets / vague values · passing states →
  episode · health, money, beliefs → ask first (`explicit=true` only on a yes) ·
  duplicates skipped · updates report the old value.

## 5. Safety model

| layer | what it does |
|-------|--------------|
| Permission engine | risk levels, HUD confirmation for HIGH/CRITICAL, user blocks/trusts (Phase 4) |
| Capability permissions | plugin needs ("network", …) confirmed on first use, then `plugin_permissions` |
| Inline confirmation | tools can ask mid-step (`ctx.extras["confirm"]`) — HUD only, silence = no |
| Computer agent policy | blocked: passwords, CAPTCHAs, admin/UAC, terminals, Run dialog · ask: pay, send, delete, submit, accept, close unsaved · every action audited (typed text as length) |
| Workspace sandbox | team builds write only inside `Documents/JARVIS Projects/<name>`; nothing generated is executed |
| Network hygiene | research fetches public http(s) hosts only (no localhost/LAN), 600 KB cap |
| Secrets vault | Windows DPAPI for `.env`; verified before and after every write |
| Audit | + computer actions, recoveries, self-checks, emergency stops, plugin installs |
| Emergency stop | F12 / STOP / "stop everything": tasks, HUD prompts, computer agent, long jobs, speech |

## 6. Try it (voice or the command box)

* "What's 1234 times 5678 plus 99?" → `think`, answered exactly on the PC.
* "Research the best free vector databases and write a report." → background task,
  a cited brief and a .docx in `Documents/JARVIS Research/…`.
* "Analyse my JARVIS project." → report in `Documents/JARVIS Research/…`.
* "Build me a website for my portfolio." → six agents; opens the result when approved.
* "Continue the project we worked on yesterday." → `recall_context` first.
* "What's this error on my screen?" → `analyze_screen` mode errors.
* "Can you convert currencies?" → offers the plugin; install needs HUD CONFIRM.
* "Stop everything." / F12 → emergency stop.
* "Try again." → retries the last failed task from where it failed.

## 7. Files

New: `core/model_router.py`, `core/context_engine.py`, `core/cognition.py`,
`core/safety.py`, `core/research.py`, `core/team.py`, `core/vision.py`,
`core/computer_agent.py`, `core/plugin_market.py`, `memory/episodic.py`,
`memory/identity.py`, `memory/policy.py`, `plugins/available/*`,
`tools/phase5_live_check.py`, `tests/test_phase5_*.py` (7 files).
Changed: `core/ai_gateway.py`, `core/providers/base.py`, `core/agents/{orchestrator,
planner,builtin}.py`, `core/tasks/state.py`, `core/tools/{builtin,registry}.py`,
`core/permissions.py`, `core/secret_store.py`, `core/plugin_loader.py`,
`core/jarvis_core.py`, `core/prompt.txt`, `memory/memory_hub.py`, `main.py`, `ui.py`,
`ui_command_center.py`, `actions/code_helper.py`, two existing tests.
