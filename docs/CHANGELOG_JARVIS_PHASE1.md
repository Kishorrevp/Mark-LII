# Phase 1 — Stabilize (applied to the original MARK LII codebase)

Four small, independent, low-risk changes. Nothing else in the project was touched.
Full audit this is based on: see the accompanying `JARVIS_Audit_and_Migration_Plan.md`.

## 1. Removed dead code
Deleted `core/llm_client.py`, `core/stt.py`, `core/tts.py`, `core/installer.py`.
All four were confirmed unreferenced by anything else in the codebase (grepped
across every `.py` file before deletion) and their own dependencies
(`faster-whisper`, `vosk`, `edge-tts`, `kokoro`) were never in `requirements.txt`
— they could not have run even if something had called them. `requirements.txt`
was left unchanged, since nothing in it was made unused by this deletion.

## 2. Fixed the `agent_task` dangling reference
`core/prompt.txt` instructed the model to call a tool named `agent_task` for
multi-step planning. That tool was never declared anywhere, so any request that
triggered this instruction would fail. Replaced the two `agent_task` lines with
one line telling the model to just call its existing tools one at a time in
sequence for multi-step requests — which is what a real planner tool would
have had it do anyway, without adding a new tool in what's meant to be a
stabilization pass.

## 3. Wired up `forget_memory` as a real tool
`memory/memory_manager.py` already had a working `forget(key, category)`
function (aliased as `forget_memory`) — it was just never exposed to the model.
Added:
- `forget_memory` to the `memory.memory_manager` import in `main.py`
- a `forget_memory` entry in `TOOL_DECLARATIONS`, matching the existing
  `save_memory` / `recall_memory` style
- a dispatch branch in `_execute_tool`, placed next to `recall_memory` since
  it's the same kind of fast, local, non-executor operation

This only removes the *one* fact it's given an exact category + key for — it
doesn't do fuzzy "forget everything about X" sweeps. The model is expected to
call `recall_memory` first if it doesn't already know the exact key, the same
way it already does today when asked what it remembers.

**Not done, on purpose:** the underlying `forget()` only deletes a single
key/category pair. Broader "forget everything about topic X" matching would
need new logic in `memory_manager.py` itself, not just a new tool — that's a
bigger, separate change and out of scope for a stabilization pass.

## 4. Resolved the `undo` naming collision
`computer_settings(action="undo")` (sends Ctrl+Z to the focused window) shared
its action name with the separate, unrelated top-level `undo` tool (reverses
JARVIS's own actions). Worth noting: the original `undo` tool's description
already had one sentence disambiguating this ("...it is not the Ctrl+Z of
whatever application is on screen — that is computer_settings with action
'undo'"), so this was already partially handled. Renamed the
`computer_settings` action value from `undo` to `keyboard_undo` in three
places — the `ACTION_MAP` key in `actions/computer_settings.py`, the action
enum in `TOOL_DECLARATIONS`, and the cross-reference inside the `undo` tool's
own description — so the two are textually distinct as well as
semantically distinct. `redo` was left as-is; it has no equivalent collision.

---

Every edit above was verified to compile (`py_compile` across the whole tree)
and the modified `TOOL_DECLARATIONS` list was structurally validated (25 tools,
correct required-parameter list, no leftover `agent_task` entry) before this
was packaged. It has not been run against a live Gemini API key or real audio
hardware — that isn't possible in the environment this was prepared in.

---

# Phase 2 — Personalization: Friendly Mode / Serious Mode

Backend/behavioral only, as scoped — the visual (UI) side of Friendly vs.
Serious is Phase 3, not included here.

## What was added
- **`core/personality_serious.txt`** and **`core/personality_friendly.txt`** —
  two short text files, each holding one mode's personality description.
  Serious preserves the original single personality almost exactly (precise,
  professional, minimal fluff) so a fresh install behaves exactly as before.
  Friendly is new: warm, conversational, a little humor, per your spec's
  Section 7.
- **`core/prompt.txt`** — the old fixed `IDENTITY:` / `Act:` lines were
  replaced with a `{{PERSONALITY_BLOCK}}` placeholder. Everything else in the
  file (tool routing, language rules, memory rules, etc.) is unchanged and
  shared by both modes — personality is the only thing that differs.
- **`main.py` → `_load_system_prompt(mode)`** — now takes a mode, loads the
  matching personality file, and substitutes it into the placeholder. Falls
  back to the original hardcoded default if either file is ever missing —
  same safety pattern the function already had for a missing `prompt.txt`.
- **`memory/config_manager.py`** — `get_personality_mode()` /
  `save_personality_mode()`, matching the existing `get_voice()`/`save_voice()`
  pattern exactly, including collapsing unrecognized values to the default
  (`serious`) rather than ever passing a bad value through.
- **`set_mode` tool** — new entry in `TOOL_DECLARATIONS` (26 tools now) and a
  matching dispatch branch. Triggers on phrases like "friendly mode", "serious
  mode", "be more casual", "stop joking" in any language (per your spec's
  Section 9 examples). Saves the mode, then calls the existing
  `request_reconnect(keep_context=True, ...)` mechanism — the same one voice
  changes and audio-device changes already use — so the new personality takes
  effect on the next turn without losing the conversation. `keep_context=True`
  is deliberate here, not copied blindly from the voice-change case: per
  Google's own Live API docs, configuration (including `system_instruction`)
  *can* be changed across a resumed session, unlike the voice/speech config,
  which is why voice changes specifically need `keep_context=False` and mode
  changes don't.

## What this gets for free
Proactive check-ins, the morning briefing, and every other message the model
generates all go through this same `system_instruction` — so their tone shifts
with the mode automatically. No changes were needed in `proactive.py` or the
briefing code for this.

## What was deliberately left out of this pass
- **No UI toggle.** Mode switching is voice/tool-driven only right now,
  matching your spec's own example commands. A manual switch in
  `CustomizeOverlay` would be a small, separate addition if you want one
  before Phase 3's visual split lands.
- **No visual/theme change on mode switch.** That's Phase 3 — it reuses
  `retheme_all_widgets`, which already exists, rather than anything built
  here.
- **No per-mode proactivity/verbosity tuning beyond the prompt itself** (e.g.
  Friendly Mode checking in more often). The spec ties "proactivity" to mode;
  right now only tone/verbosity is mode-aware, not how often JARVIS initiates.

## Verification
Compiles clean across the tree. Additionally, `_load_system_prompt()`'s exact
logic was run standalone against the real files on disk for `serious`,
`friendly`, and a deliberately invalid mode string — confirmed the
placeholder is fully substituted in both real cases and that an invalid mode
safely collapses to `serious` rather than leaving the placeholder unresolved
or raising. Not run against a live Gemini session, for the same reason as
Phase 1.

**Worth testing specifically:** say "Jarvis, friendly mode" mid-conversation
and confirm (a) it doesn't lose the conversation, and (b) the next few replies
actually sound different.

---

# Phase 3 — Futuristic UI: the visual Friendly/Serious split

Scoped narrower than the roadmap's Phase 3 line on purpose: this is the HUD
motion/atmosphere profile only. Ambient Mode vs. Command Center windowing
(also listed under Phase 3 in the roadmap) is **not** included — the original
audit's own sequencing note (G.3) flagged that as the one piece that isn't a
natural extension of something that already exists, so it's left for its own
separate pass rather than bundled in here.

## A correction from the original audit
The audit's migration plan said the visual split would "reuse
`retheme_all_widgets`." Having now actually read that function closely: it's
real, but it only does one thing — swapping hex colors inside widget
stylesheets via string replacement. It has no way to touch animation speed,
easing, or glow size, because those aren't stylesheet properties, they're
Python numbers inside `HudCanvas`'s own per-frame update loop. So this phase
uses `retheme_all_widgets` for the color dimension (already true, unchanged)
and adds a *second*, new mechanism for the motion dimension. Flagging this
because the original plan undersold the work here — worth knowing if you're
reviewing the diff and wondering why there's a new profile system instead of
just a call to the existing retheme function.

## What was added
- **`HudCanvas._MOTION_PROFILES`** — two small dicts of multipliers
  (`ease`, `ring_speed`, `halo_boost`, `particle_rate`). Serious is all
  `1.00` — the original animation, unchanged, byte-for-byte identical to
  before this phase. Friendly softens the easing (0.72×, so the core
  catches up to its target more slowly — reads as looser/more organic),
  slows the ring/scanner rotation (0.80×), warms the ambient glow (1.18×
  the base halo size), and spawns idle pulse-rings slightly more often
  (1.25×). None of this touches how sharply the HUD reacts to actual
  speech loudness — only the ambient "breathing" behavior between reactions.
- **`HudCanvas.set_personality_mode(mode)`** — swaps the active profile.
  Called on the Qt thread only.
- **`MainWindow._mode_sig`** (new `pyqtSignal(str)`) and
  **`_apply_personality_mode`** slot — same thread-safety pattern as
  `_state_sig`/`_apply_state`, which the codebase already used for HUD state
  changes. Nothing new invented here, just the existing pattern extended to
  a second signal.
- **`JarvisUI.set_personality_mode(mode)`** — the facade method
  `main.py` calls; emits the signal. Same shape as `set_state`/`write_log`.
- **Startup:** `MainWindow.__init__` now reads `get_personality_mode()` right
  after building the HUD and applies it, so a relaunch shows the correct
  mode immediately rather than defaulting to Serious until the next switch.
- **`main.py`'s `set_mode` dispatch** now also calls
  `self.ui.set_personality_mode(requested)`, alongside the existing
  `save_personality_mode` + `request_reconnect` from Phase 2. This part is
  instant — no reconnect needed, since it's a local Qt-side change, not
  anything Gemini needs to know about.

## What was deliberately left out
- **Color palette is not mode-linked.** Friendly/Serious currently only
  change motion, not hue — your existing accent-color picker
  (`CustomizeOverlay`) is untouched and independent. Making Friendly
  default toward a warmer hue and Serious toward the current cyan would be
  a small follow-on (a call to `apply_ui_accent` + `retheme_all_widgets`
  inside `_apply_personality_mode`, using the *real* mechanism this time),
  not done here so this diff stays about one thing.
- **No panel geometry/density changes** ("sharper geometry," "relaxed
  information density" from your spec). That's layout work in `MainWindow`
  itself, a different and larger surface than `HudCanvas`.
- **Ambient Mode / Command Center windowing** — as noted above, out of
  scope for this pass.

## Verification
Compiles clean across the tree. Beyond that: extracted the exact arithmetic
from `HudCanvas._step()` into a standalone simulation (no Qt needed) and ran
5,000 simulated animation ticks per mode, with randomized speaking/muted/
amplitude inputs — confirmed no NaN, no negative halo values, and scale
staying in a sane range for both profiles. That's real verification of the
math; it is not the same as watching the HUD actually move on screen, which
needs a display this environment doesn't have.

**Worth testing specifically:** watch the HUD idle for ~10 seconds in each
mode back to back — the friendly glow should read visibly warmer/slower,
not just numerically different.

---

# Phase 3b — Mode × state colour identity (requested before Phase 4)

This supersedes Phase 3's colour scope. Phase 3 only changed *motion*
(easing, rotation speed, glow size, particle rate) and left colour entirely
to the existing accent picker. This pass adds the actual colour identity
per your spec: Friendly = blue idle → green while listening; Serious = dark
red idle → brighter red while listening; universal amber warning / red
alarm error; all of it smoothly cross-fading, never snapping.

## The flow you asked me to document: Mode → HUD State → Target Visual Properties → Smooth Transition → Frame Rendering

1. **Mode** (`friendly`/`serious`) lives in `HudCanvas._mode_name`, set by
   `set_personality_mode()` — same entry point Phase 3 already used, just
   now it also selects a colour identity, not only a motion profile.
2. **HUD State** — `HudCanvas._resolve_theme_key()` looks at `self.state`
   (`"LISTENING"`/`"THINKING"`/etc., set by `main.py` via `set_state()`) and
   `self.speaking`, and reduces them to one of 7 keys: `idle`, `listening`,
   `thinking`, `executing`, `speaking`, `warning`, `error`. Priority order:
   error > warning > speaking > listening > thinking > executing > idle
   (anything unrecognised — `SLEEPING`, `INITIALISING` — falls through to
   idle). `muted` is handled entirely separately, exactly as it already was
   before this change, and never enters this resolution at all.
3. **Target Visual Properties** — `HudCanvas._theme_entry()` looks up
   `HUD_THEME[mode][key]`, a plain data table (module-level, right above the
   class) with one entry per (mode, state) pair: an RGB colour, plus
   `glow_mult` / `ring_mult` / `pulse_mult` / `strobe_hz`. This is the
   "centralized visual state/theme system" you asked for — nothing in
   `_step()` or `paintEvent()` hardcodes a colour anymore; they all read
   `self._cur_color` / `self._cur_*_mult`, never `HUD_THEME` directly.
4. **Smooth Transition** — every frame, `_step()` fetches the current
   target from step 3 and eases `_cur_color` / `_cur_glow_mult` /
   `_cur_ring_mult` / `_cur_pulse_mult` / `_cur_strobe_hz` toward it at a
   fixed rate (`csp = 0.06`), the identical "current += (target - current)
   * rate" idiom the file already used for `_scale`/`_halo` — no new
   interpolation paradigm introduced, just the existing one applied to more
   properties. A full cross-fade takes roughly a second; nothing ever jumps.
5. **Frame Rendering** — `paintEvent()` no longer reads `C.PRI` for any of
   the reactive elements (halo rings, pulse rings, spinning arcs, both
   scanner arcs, tick marks, crosshair, corner brackets, the no-face
   fallback orb, particles). Every one of those now calls
   `self._live_qcol(alpha)`, which returns `_cur_color` at that alpha —
   with one addition: if `_cur_strobe_hz` is above zero (only `warning` and
   `error` ever set it), it also folds in a sine-wave brightness flicker
   using the file's existing `self._tick` frame counter, the same way the
   waveform's idle shimmer already works.

## Why colour and glow intensity don't fight each other
`glow_mult` isn't applied a second time at paint time — it's multiplied
into `self._base_halo` in `_step()`, the exact same line Phase 3's
`halo_boost` already lives on. Every alpha formula in `paintEvent()` that
reads `self._halo` (there are several) picks up both boosts automatically,
so "mode says a bit brighter" and "state says a lot brighter" compose
correctly instead of needing to be re-applied at each of the ten-plus paint
call sites individually. `ring_mult` and `pulse_mult` compose the same way,
multiplying into Phase 3's existing `ring_speed`/`particle_rate` terms.

## Colour choices and why
- Friendly idle `(62,166,255)` and listening `(0,255,136)` — the listening
  green is the exact same RGB as the app's existing `C.GREEN`, reused on
  purpose rather than inventing a second "green."
- Serious idle `(184,48,63)` is a deliberately darker, less saturated red
  than the app's existing `C.RED` `(255,51,85)` — "controlled," per your
  spec, not just a dimmer version of the error colour.
- Serious listening `(255,107,74)` leans warmer/more orange than both idle
  and error, so it's not just "error but calmer" — it's its own hue.
- Warning `(255,204,0)` and error `(255,51,85)` are identical in both modes
  and reuse the app's existing `C.ACC2`/`C.RED` — an alert should look like
  an alert regardless of personality, and should agree with how the rest of
  the app already marks warnings/errors (log lines, borders).

## What was deliberately left unchanged
- **Status text colour** (the "● LISTENING" / "◈ THINKING" line under the
  HUD) still uses its own fixed per-state colours, not the new theme. That
  small text label benefits from being instantly, consistently recognisable
  regardless of which personality mode is active — recolouring it wasn't
  asked for, and mixing it into the mode-driven system would make the one
  legible constant in the frame variable too.
- **Background grid dots** (`C.PRI_GHO`) are unchanged — barely-visible
  texture, not part of the "core/rings/glow/scanner" identity in question.
- **No new WARNING/ERROR trigger points were added to `main.py`.** The
  theme table and rendering fully support both — verified below — but
  *deciding when JARVIS should actually enter a warning or error state* is
  a control-flow question, not a rendering one, and belongs with the
  verification layer in Phase 4, not bolted on here. Right now nothing
  calls `set_state("WARNING")` or `set_state("ERROR")`; the visual system is
  ready the moment something does.

## Verification
Compiles clean. Beyond that, three separate checks, all run against the
*actual* `HUD_THEME` dict and `_step()` arithmetic extracted from the real
file (not a hand-copied approximation):
- **Structural** — both modes have all 7 states, all 5 required properties
  each, all values in sane ranges, warning/error identical across modes.
- **Verification case #12 specifically** — measured RGB distance confirms
  Serious `error` is clearly separated from both Serious `idle` (distance
  74.4) and Serious `listening` (distance 57.1), on top of `error` being the
  only one of the two with a strobe.
- **2,000 simulated frames × all 7 states × both modes (14 combinations)**
  — no NaN, no negative halo, no out-of-range colour channel, in any of
  them.
- **Transition smoothness** — simulated a live mode switch and printed the
  colour at several frames afterward: it moves gradually across roughly a
  second and never jumps straight to the target on the first frame.

As with every phase so far: this is verified arithmetic and structure, not
a verified picture on an actual screen, since this environment has neither
a display nor Qt installed.

**Worth testing specifically:** trigger listening in each mode back to back
and confirm friendly genuinely reads as blue→green→blue while serious stays
red throughout, just brighter while listening — and that switching modes
mid-conversation cross-fades rather than snapping.

---

# Phase 4 — Agent architecture: provider abstraction + risk-tier confirmation

Two independent pieces, both from the roadmap's Phase 4 line.

## Part 1 — AI provider abstraction

**New file `core/ai_provider.py`.** An `AIProvider` interface with one
method, `generate_text(prompt) -> str`. `GoogleTextProvider` is the only
real implementation — it's the exact `genai.Client(...).models
.generate_content(...)` call every file already made, just behind a seam.
`AnthropicProvider`/`OpenAIProvider`/`LocalProvider` are honest
`NotImplementedError` stubs, not working code — each docstring says exactly
what filling it in would need (API package, request shape, and critically,
*where the reply text actually lives* in that provider's response object,
since it isn't `.text` for any of them). This environment has no
credentials or local model to build and test those against, and a stub that
pretended to work would be worse than an honest one that doesn't.

**Config:** `ai_provider`/`ai_model` added to `config_manager.py`
(`get_ai_provider`/`get_ai_model`/`save_ai_provider`/`save_ai_model`),
same pattern as Phase 2's `personality_mode`. This uses the app's existing
JSON config, not the `.env`-style variables the original spec sketched —
consistent with how every other setting in this app already works, rather
than adding a second, competing config mechanism for one feature.

**Migrated — 3 files, 10 call sites, verified clean:**
- `actions/dev_agent.py` — all 3 (`_plan_project`, file-writer, the
  error-fix retry loop). Dead code removed: the local client-shim function,
  the duplicate `_get_api_key`, and the now-redundant `MODEL_PLANNER`/
  `MODEL_WRITER` constants (they were always identical strings — the model
  now comes from the one global `ai_model` setting instead of being
  hardcoded per call site).
- `actions/code_helper.py` — 6 of 7. Intent classification, write, fix,
  edit, explain, optimize are migrated. **`screen_debug` is deliberately
  not** — it sends a screenshot via `types.Part.from_bytes`, a different
  input shape than plain-text `generate_text(prompt)`, and `file_processor.py`
  (not touched at all this pass) has a *third* image-input shape of its own
  (PIL Image objects passed straight into `contents`). Designing one
  interface to cover two different existing patterns well deserves an
  actual second call site to validate the design against, not a guess.
- `main.py` — the end-of-session summary. This one's async, and the
  original code wrapped only the network call itself in
  `asyncio.to_thread(...)`, not the (cheap, local) client construction —
  the migration preserves exactly that split: `get_provider()` runs
  directly on the event loop, only `.generate_text` is handed to
  `asyncio.to_thread`. Getting this wrong would mean either blocking the
  audio pipeline on every session-summary call, or not actually running it
  off-thread at all — worth being precise about.

**Not migrated — 7 files, unchanged, still call `genai` directly:**
`flight_finder.py`, `file_processor.py`, `screen_processor.py`,
`web_search.py`, `desktop.py` (its `_ask_gemini_for_desktop_action` code-gen
call, separate from the confirmation-gate work on the same file below),
`youtube_video.py`, `computer_control.py`. Same migration as the three
above, not attempted here — ten files' worth of AI-calling code touched in
one pass was a bigger, riskier diff than three, and several of the seven
have the same image-input wrinkle as `screen_debug`. Whoever picks this up
next: `core/ai_provider.py`'s module docstring has the full list and reasoning.

**Verification:** every migrated file compiles; grepped each of the three
for leftover `genai`/`_get_api_key` references afterward — `dev_agent.py`
has none at all now, `code_helper.py` has exactly one cluster (inside
`screen_debug`, as intended), `main.py` has exactly the ones the untouched
Live session still needs. No live API key to actually call Gemini through
the new path in this environment, so this is verified by structure and by
the fact every response-object usage (`.text`, error-message previews) was
individually checked and updated at each of the 10 sites — not run end to end.

## Part 2 — Risk-tier confirmation gate

**New file `core/risk.py`.** A `TOOL_RISK` table classifying all 26 tools
(low/medium/high), with per-action overrides for the handful where risk
genuinely varies by action (`computer_settings`, `file_controller`,
`browser_control`, `code_helper`, `desktop_control`) rather than by tool as
a whole. Checked programmatically against the real `TOOL_DECLARATIONS` list
in `main.py`: exact 1:1 coverage, no typos, no missing tools.

**Only "high" is actually enforced**, and only two places, both through
`core/confirm.py`'s existing mechanism, unchanged:
- `computer_settings.py`'s restart/shutdown/toggle_wifi — pre-existing,
  from Phase 1 and earlier. Documented in the table for a complete picture;
  not touched or double-gated.
- **`desktop_control`'s `"task"` action — new.** This is the free-form
  AI-code-generation-and-`exec()` path flagged in the original security
  audit (E.2) as running on a weak in-process sandbox. It now requires
  on-screen confirmation before the generated code runs, using the exact
  same `confirm.pending_title()` / `confirm.request()` pattern
  `computer_settings.py` already established — not a new mechanism, the
  existing one applied to a second file. Generation itself is deferred
  until after confirmation too, not just execution, since the two happen
  back-to-back with nothing else gated in between.

**"Low" and "medium" are classification, not new enforcement.** Everything
at those tiers still executes immediately, exactly as before this file
existed. This is a real, deliberate scope line, not an oversight — see the
"why not a fully generic dispatch-level gate" section in `core/risk.py`'s
own module docstring: intercepting every one of the 26 tools' branches
generically would mean restructuring `main.py`'s entire `_execute_tool`
dispatch into something re-invokable from a confirmation callback. That's a
much bigger, riskier change than the one concrete new gate this pass
actually needed. The table is what a future generic version would read
from — building that version isn't this pass.

**Verification:** imported and ran the real `risk_level()` function
directly against 18 concrete cases — every `computer_settings`/
`desktop_control` high-risk action resolves to `"high"`, spot-checked
defaults and overrides across `file_controller`/`browser_control`/
`code_helper` resolve correctly, and an unrecognised tool name correctly
falls back to `"medium"` (cautious default, not `"low"`). This is the one
piece of Phase 4 I could actually execute end-to-end in this environment,
since `core/risk.py` has zero external dependencies — no PyQt, no genai, no
audio libraries — so unlike almost everything else in this project, I
didn't have to simulate it. `actions/desktop.py`'s gating itself is
verified by compilation and by matching `computer_settings.py`'s already-
proven pattern line for line, not by an actual on-screen confirmation,
since that needs the GUI this environment doesn't have.

**Worth testing specifically:** ask JARVIS to do something under
`desktop_control`'s free-form task path (something not covered by
wallpaper/organize/clean/list/stats) and confirm a confirmation banner
appears *before* any code runs — and that declining it means nothing
executes.

---

# Phase 5 — Advanced laptop control: fixing the sandbox, not just gating it

Two pieces, both closing gaps the security audit named directly (E.2, E.3)
rather than just working around them.

## Part 1 — `desktop_control`'s "task" action: real process isolation

Phase 4 added a confirmation gate in front of this action. That's a real
improvement, but it's a lock on the door — the room behind it was still the
same weak in-process `exec()` sandbox. This phase replaces the room.

**What changed in `actions/desktop.py`:**
- **`_build_sandbox()` is gone entirely.** It used to hand AI-generated code
  a dict of "restricted" builtins that still included `hasattr`/`getattr`/
  `isinstance` — exactly the introspection primitives commonly used to walk
  Python's object graph back to something unrestricted — plus, on Windows,
  raw `ctypes` (which can call *any* native function regardless of what
  Python-level builtins are restricted). Restricted-`__builtins__` sandboxes
  are a well-known weak pattern for exactly this reason.
- **New `_run_generated_script()`** writes the generated code to a temp
  file and runs it with `subprocess.run([sys.executable, tmp_path],
  timeout=25, ...)` — a real OS process, killable on timeout, with no path
  back into JARVIS's own running memory, credentials, or tool registry.
  This is not a new pattern for this codebase: it's `dev_agent.py`'s
  `_run_project` and `code_helper.py`'s `_run_file`, applied to the one
  place that was still doing it the older, weaker way.
- **New `_static_safety_scan()`** — a plain substring check against a
  denylist (`subprocess`, `eval(`, `exec(`, `os.remove(`, `shutil.rmtree(`,
  `ctypes`, `winreg`, `open(`, `socket`, `urllib`, `requests`, …), run
  *before* the subprocess ever starts. It's deliberately not a real parser —
  it doesn't need to be one, because it's one of three independent layers
  now (this scan, the Phase 4 confirmation gate, and the process isolation
  above), not the only thing standing between a bad generation and real
  damage. Any one layer missing something is still caught by the other two.
- **The allowed-imports list is tighter than before, not just relocated.**
  Old: pyautogui, `Path`, `shutil.copy2/copytree/disk_usage`, `os.path`,
  `time.sleep`, and on Windows, `ctypes` + read-only `winreg`. New:
  pyautogui, `Path` (existence/globbing only — no `open()`/`read_text()`),
  `time.sleep`. Everything else — file I/O, `ctypes`, `winreg`,
  `shutil` — is gone. File work already has a safe, undo-integrated home
  (`file_controller`/`file_processor`); this free-form path's actual job is
  screen/input automation the fixed desktop actions don't cover, and it's
  now scoped to exactly that.
- **The Gemini prompt was rewritten to match**, including a real
  correction: the old prompt said "NO import statements (modules are
  pre-injected)," which was true for the old in-process exec() model and
  is now backwards — a standalone script has to import what it uses. Small
  detail, but leaving it unchanged would have made every generated script
  fail immediately.
- The unused parent-process `pyautogui` import at the top of the file was
  also removed — it only ever existed to feed `_build_sandbox`.

**Verification — actually run, not simulated, since none of this needs Qt
or a live API key:** imported the real functions and ran them for real.
- `_static_safety_scan` correctly blocked all 8 denied-pattern test cases
  (subprocess, eval, exec, os.remove, shutil.rmtree, open, socket, ctypes)
  and correctly allowed a legitimate pyautogui+time script.
- `_run_generated_script` actually executed a real script in a real
  subprocess and captured its real stdout (`"hello from the sandboxed
  subprocess\n4"` for a script that printed a string and `2+2`).
- The timeout path was exercised for real too: a script that calls
  `time.sleep(30)` was killed and returned in ~2 seconds when given a 2s
  timeout, instead of hanging.

This is the strongest verification of anything in this whole project so
far, precisely because this piece has no PyQt/genai dependency standing
between "the code is correct" and "I can prove it" — I could just run it.

## Part 2 — `dev_agent`'s auto-install allow-list

**The fix, in `actions/dev_agent.py`:** `_try_auto_install` now takes the
project's own declared `dependencies` list as a parameter, and normalizes
+ checks the package name a `ModuleNotFoundError` mentions against it
*before* ever calling pip. If the traceback names something the plan never
declared, it's treated as a code bug for the next `_fix_files` retry to
address (e.g. a bad or hallucinated import), not silently patched by
installing whatever a stray error message happens to mention. The
call site (`installed = _try_auto_install(last_output, project_dir,
dependencies)`) now passes that list through.

Name matching is normalized (lowercased, underscores/dashes unified,
version pins like `==2.0.1` stripped) so `Flask` in the plan still matches
`flask` in a traceback — but it's still a real allow-list, not a rubber
stamp: an undeclared package is rejected regardless of how plausible it
looks.

**Verification — also actually run:** imported `_try_auto_install` directly
and exercised both branches against a real (if empty) temp project
directory. A declared dependency (`flask`, from a plan listing
`["flask==2.0.1", "requests"]`) passed the allow-list check and reached the
point of attempting a real pip call. An undeclared package
(`some_random_untrusted_pkg`) was rejected before any subprocess call was
made at all, with the exact log line confirming why. Both outcomes
confirmed by capturing and inspecting real stdout, not by reading the code
and assuming it does what it says.

**Worth testing specifically:** ask `dev_agent` to build something small
with one intentionally-omitted-from-the-plan import buried in generated
code (hard to force organically — this is more a "watch the logs the first
few times dev_agent hits a real ModuleNotFoundError" situation) and confirm
undeclared packages get skipped in favor of a code-fix retry rather than an
install attempt.

# Phase 4A–4K — Agent architecture, provider gateway, security

Full audit, architecture diagram, file lists and operator how-tos:
`JARVIS_PHASE4_ARCHITECTURE.md`. This entry records *why* and *what was left out*.

## Scope decision up front
The request had two parts: (1) "finish the AIProvider migration and the deferred
panel/windowing work before Phase 6", and (2) a detailed brief whose implementation
order is 4A→4K and which says "DO NOT redesign the HUD". I followed the brief: the
provider migration is done (4A); the Ambient/Command-Center windowing is **not
started**, because it is HUD/layout work the brief explicitly protects and it
deserves its own reviewed phase. Future phases follow the brief's numbering
(5 Laptop, 6 Phone, 7 Calls, 8 Proactive).

## 4A — Provider gateway + secrets
* `core/providers/` — one interface, four real implementations. Google via the
  SDK (unchanged dependency); Anthropic, OpenAI and Ollama via plain HTTPS through
  an injectable transport, so **no new packages**.
* `core/ai_gateway.py` checks capabilities *before* calling (e.g. web grounding
  on a provider without it → `CapabilityNotSupported`, which web_search already
  treats as "use DuckDuckGo"), retries only retryable errors, and uses fallback
  providers **only if you list them**.
* **Cost:** Google (free tier) stays the default. Anthropic/OpenAI are paid APIs;
  they exist because the brief asked for a real seam, and they are never selected
  or used as a fallback unless you configure them. Ollama is free/offline.
* Migrated every remaining direct `genai.Client` text/vision call:
  flight_finder, file_processor (adapter keeps its ~12 call sites unchanged),
  web_search (grounding), desktop, youtube_video, computer_control (vision),
  code_helper.screen_debug (vision). **Deliberately not migrated:** the Gemini
  *Live* voice session (main.py, screen_processor.py) — nothing free offers an
  equivalent realtime audio API, so an abstraction would have one real
  implementation and fake the rest. It now reads its key via secret_store.
* Secrets: `core/secret_store.py` (env → `.env` → legacy JSON). On first launch
  the key is **moved** from `config/api_keys.json` to `.env` (write → read-back
  verify → remove; value never printed). Setup overlay now saves to `.env` and
  *patches* settings instead of overwriting them (it used to wipe voice/devices/
  mode). `.env.example` added; `.gitignore` covers `.env*`, credential/secret
  files, audit/task logs.
* Left out: OS keyring storage (needs `keyring`, not in requirements).

## 4B — Tool Registry
`core/tools/registry.py` + `builtin.py`: all 26 Live tools wrapped (not rewritten)
with schema, owner agent, per-action risk, confirmation, undo, verifier,
retry-safety, alternatives; plus 22 structured agent tools that return data
(paths, text, drafts) so steps can feed each other. `core/risk.py` is now a shim
over the registry so there is one source of truth. Risk changes (per brief):
file delete and send_message → HIGH (confirm), game_updater shutdown_when_done →
HIGH, read-only actions → SAFE.

## 4C — Task state
`core/tasks/state.py`: the brief's ten states with an enforced transition table
(illegal transitions raise). Persisted (last 50) to `memory/tasks/tasks.json`; a
task that was mid-flight when JARVIS closed is reloaded as FAILED ("interrupted")
rather than silently resumed.

## 4D — Agent Router
11 agents with keyword tables; clause-level routing with context carry-over
("open Chrome, search X" stays Browser). Tamil/Tanglish handled by normalising
command words (`core/multilingual.py`) — the original text still goes to the
model. New agents need no router change (tested).

## 4E — Task Planner
Rule planner first (instant, free, offline), LLM planner only for clauses it
doesn't recognise. Both pass one validator: known tools only, schema-checked args,
references only to *earlier* steps (acyclic by construction), ≤15 steps. An invalid
LLM plan is rejected, never executed. Missing details become `$ask:` questions
(WAITING_FOR_INPUT) instead of guesses.

## 4F — Orchestrator
Runs each task on its own thread; cancel/pause/resume/answer are immediate and
thread-safe. Agents receive an `AgentServices` object that can only call the tool
their step was approved for (plus SAFE lookups they own) — tested with a rogue
agent.

## 4G — Permission + confirmation
`core/permissions.py`: SAFE / LOW / MEDIUM / HIGH / CRITICAL → ALLOW / CONFIRM /
DENY. main.py now runs **every** direct tool call through it (previously only
restart/shutdown/wifi/desktop-task were gated, by themselves). Self-gating actions
keep their own banner (no double prompt). `core/confirm.py` gained `on_cancel`
(declined / expired / superseded / cancelled), an expiry timer and
`cancel_pending()` — existing callers unchanged. A spoken "yes" does not approve;
only the HUD button. `trusted_actions` / `blocked_actions` in config; CRITICAL can
never be trusted.

## 4H — Verification + recovery
`core/verification.py`: VERIFIED / FAILED / UNVERIFIABLE. Real post-conditions
(destination exists & source gone, file valid incl. PDF magic bytes, process
running). Retries are bounded (default 2) and only for retry-safe tools or
transient errors; alternatives are tried only if they are ALLOWed without asking
(never a silent escalation). Direct voice calls are verified afterwards too; a
failure is appended as `[VERIFICATION FAILED]` and the prompt forbids calling it
done. send_message is honestly UNVERIFIABLE (GUI automation has no receipt).

## 4I — Agent-to-agent data
Steps pass structured outputs (`$s2.path`, `$s6.summary`, `$s7.draft`), resolved at
run time; a missing output fails the step with a clear reason.

## 4J — Memory + workflows + language
* `memory/memory_hub.py` exposes the seven categories over the existing store
  (memory_manager.py unchanged). Refuses passwords/PINs/OTPs/card-or-ID-shaped
  numbers/keys — also enforced in main.py's `save_memory` branch.
* `core/workflows.py`: observes tool signatures (never message bodies), detects
  sequences repeated in ≥3 sessions within 14 days, offers each suggestion once
  through the policy-gated proactive slot, saves only on an explicit yes, runs as
  a normal permission-checked task.
* `set_language` tool (auto / en / ta / tanglish) + a `[LANGUAGE]` block in the
  system prompt; prompt rule "never mix languages" relaxed for Tamil+English.
* `memory_inspect` tool for "what do you remember about me?".

## 4K — Phone / call interfaces (interfaces only)
DeviceAgent/DeviceRouter with a working LaptopDevice and a PhoneDevice that
reports **unavailable**; real pairing (6-digit code, per-device secret, HMAC
signing, replay/clock-skew protection, revoke). Call-agent contract + policy
engine: mandatory AI disclosure in EN/TA/Tanglish, anti-impersonation check,
urgency scoring, escalation rules. `TelephonyUnavailableAgent` raises — nothing
pretends to answer calls.

## Proactive policy & plugins
`core/proactive_policy.py` (quiet hours 22:30–07:00, 4/hour, 120 s gap,
per-source off switches; CRITICAL passes quiet hours) now gates the system-monitor,
news-monitor and idle-check-in loops. Plugins may declare `risk`, `permissions`,
`capabilities`, `config`, `verify()`, `on_load()`/`on_unload()`; every plugin is a
registry tool, so it passes the same gate and audit (existing plugins need no
change; default risk "medium").

## Other security fixes
open_app command injection (shell=True on model text) · rename path traversal ·
file tools barred from `~/.ssh`, credential stores, browser profiles and JARVIS's
own `.env`/`api_keys.json`/TLS key · code_helper overwrites now undoable · console
logs and error strings redacted · audit log never stores message/file bodies.

## Deliberately left out (and why)
* Ambient/Command-Center windowing and new HUD visuals — brief forbids HUD redesign now.
* Phone app, telephony, future proactive sources — later phases; interfaces only.
* Voice-transport abstraction — no free equivalent to Gemini Live.
* Subprocess isolation for plugins — architectural; documented as residual risk.
* Deleting the resurrected Phase-1 dead files — I don't delete files in your folder unasked.

## Verification
58 automated tests, all passing (Python 3.11 sandbox): providers (fake HTTP
transport), architecture incl. the brief's headline request end-to-end on real
files in a temp home, extensions, and a smoke test that imports the real main.py
and drives `_execute_tool` with stubbed third-party packages. Structural checks:
all files byte-compile; `TOOL_DECLARATIONS` via `ast.literal_eval` (31 tools);
CRLF verified on every touched file. **Not verifiable in the sandbox:** live
voice, HUD rendering, Windows actions — manual checklist in the architecture doc
§II.12.

## Phase 4 hotfix — copy/move into the folder a file is already in
Found in the first real run: "find my resume PDF, copy it to Desktop, and open it"
failed with `[WinError 32] … being used by another process`. The resume was already
on the Desktop, so `shutil.copy2` was copying the file **onto itself** — not a
permission or lock problem (the audit log shows the permission engine ALLOWed the
MEDIUM-risk copy). Fix in `actions/file_controller.py`: copying onto an existing
name now creates "Name - Copy.ext" (then "- Copy (2)", …) like Explorer and never
overwrites; moving a file to the folder it is already in returns "already there"
instead of an error. The structured tools and verifiers report the real new path
and treat an in-place move as verified. Regression test added (59 tests passing).


# Mark LIII interface — Command Center (glass HUD) + Ambient mode

Requested: replace the interface with one modelled on the supplied
"JARVIS neural dashboard" design (Stitch export: `code.html` + `screen.png`) —
same composition, not a copy, realistic, futuristic, glassmorphism, closer to
the film's JARVIS. Screenshots: `docs/mark_liii_command_center.png`,
`docs/mark_liii_ambient_orb.png`.

## What changed
* **New module `ui_command_center.py`** builds the whole layout onto the
  existing `MainWindow`:
  - **Top status strip** (frameless-window title bar): emblem + name, volume,
    down/up rates, LAN IP, Wi-Fi signal, live clock/date, CPU/RAM/disk/GPU/temp,
    battery, ⚙ drawer, Ambient, minimise/maximise/close. Drag to move,
    double-click to maximise.
  - **Arc-reactor core** (centre): segmented armour ring, counter-rotating
    hairline ring, CPU/MEM gauge arcs, degree scale, voice-reactive dashed
    ring, radar sweep, expanding voice pulses, a 90-bar radial voice
    spectrum, glowing core with live time, seconds sweep and state
    (LISTENING / SPEAKING / THINKING …). Clickable chips: Friendly/Serious
    mode, mic, Ambient; DOCS/DESK/DLOAD/CTRL; WEB/CODE/MEM/CFG; date plate.
  - **Left**: Wi-Fi link, *Active Protocols* (live JARVIS tasks from
    `memory/tasks/tasks.json` with state + progress), *Diagnostics*
    (CPU/memory/network/GPU/thermal, uptime, processes), *Quick Launch*
    (drive gauges + links).
  - **Right**: *Activity* (the existing typed log), *Atmospherics* (weather),
    *Uplink* (SSID, LAN, live up/down), *File Intake* (existing drop zone).
  - **Dock**: command line, SEND, MIC LIVE/MUTED, INTERRUPT, live VU meter,
    shortcut hints, resize grip.
* **Glass for real**: Qt has no CSS backdrop blur, so the backdrop (gradient,
  grid, stars, mode-coloured light pools, warm counter-light) is rendered once
  per size/colour, blurred by multi-pass down/up-sampling, and each panel
  paints the blurred region behind itself + tint + sheen + glare band + light
  top edge + accent border + HUD corner brackets.
* **Colour logic**: panels follow the *mode* colour (Friendly blue / Serious
  red, cross-faded); the reactor follows the *state* colour (listening green /
  brighter red, warning amber, error red) — the Phase 3/3b palette, unchanged.
* **Ambient mode** (the Phase-3 windowing item that was deferred): F10, the
  ◎ button or the AMBIENT chip hides the dashboard and leaves a 250 px
  translucent, always-on-top reactor bottom-right that mirrors state and
  voice. Double-click it (or F10) to return. A confirmation or setup prompt
  automatically brings the dashboard back — a HIGH-risk action can never wait
  unseen behind the orb.
* `ui.py`: layout selection with fallback (`"ui_layout": "classic"` or the new
  *CLASSIC LAYOUT* drawer button; if the new layout ever fails to build, the
  classic one is used automatically), the original layout moved verbatim into
  `_build_classic_layout()`, F10 shortcut, drawer placement, mute-button
  styling hook, ambient-aware confirm/setup.
* `tools/preview_ui.py`: runs the interface without the voice backend, with
  optional hidden rendering + screenshot — used to iterate on this design.

## Preserved
`HudCanvas._step()` is inherited, not rewritten: Friendly/Serious identity,
eased cross-fades, state colours, motion profiles and voice reactivity are the
Phase 3/3b logic. Every `MainWindow` attribute used elsewhere (`_log`, `_input`,
`_mute_btn`, `_interrupt_btn`, `_drop_zone`, `_file_hint`, `hud`,
`_content_panel`, `_hud_cam_stack`, `_title_lbl`, `_clock_lbl`, `_bar_*` …) is
created by the new layout with the same behaviour — checked by
`tests/test_ui_structure.py`. Overlays, confirmation banner, camera feed,
customise/voice/audio/memory/plugin panels are unchanged.

## Data sources & privacy
psutil, socket, `netsh wlan` / `nmcli`, pycaw (volume) — all local. Weather
comes from **wttr.in** (free, no key): the one new network call; it sees your
IP. Config: `"weather_enabled": false` turns it off, `"weather_city": "..."`
pins a city (otherwise your IP location / memory city). `"quick_links"` —
list of `{"label","target"}` — replaces the default links; only `https://`
and `local:` targets are ever opened (no commands).

## Fixes found on the way
* Volume read `--`: newer pycaw returns an `AudioDevice`; both APIs handled.

## Verification
* **Genuine execution on your Windows laptop**: the interface was rendered by
  `tools/preview_ui.py` (hidden window + screenshot) at 1600×900 and at your
  native 1389×868 logical resolution, in Friendly/Serious and
  Idle/Listening/Speaking, plus Ambient; screenshots inspected each round.
* **Live run**: JARVIS restarted with the new UI, connected to Gemini Live,
  briefing + Tanglish replies shown in Activity; Ambient chip → orb →
  double-click → dashboard verified by screen capture.
* **Cost**: ~6–7 % of total CPU and ~320 MB RAM for the JARVIS process with
  the reactor animating.
* **Automated**: 64 tests pass (58 previous + 6 structural UI checks).

## Deliberately left out
* OS-level acrylic/Mica blur of the *desktop behind* the window — Windows-only,
  version-dependent and flaky with frameless Qt windows; the in-app glass
  gives the look without that risk.
* Dedicated HUD visuals for PLANNING/VERIFYING/WAITING — they still map onto
  THINKING/EXECUTING/WARNING (the reactor shows those colours).
* Tamil labels inside the dashboard — replies are multilingual; the UI
  chrome stays English.
* The film's exact graphics — only the style is followed; nothing is copied.

# Phase 5 (Cognitive Core) — router, context, memory systems, agents, safety

*Naming note: the earlier "Phase 5 — Advanced laptop control" above belongs to the
original migration plan. This is the Cognitive Core phase requested after Mark LIII.*

Architecture and per-item status: `JARVIS_PHASE5_COGNITIVE_CORE.md`.
Rollback: `JARVIS_pre_Phase5_backup.zip` holds the 20 files this phase replaced.

## Scope decision up front
Google moved the Pro models behind billing (May 2026), so the free tier is
Flash-only (~10 RPM). Everything here stays free: "deep reasoning" is Flash with a
16k thinking budget, and a client-side throttle keeps JARVIS under the free quota.
Sources: pecollective.com free-tier guide; ai.google.dev rate-limits page.

## What was built
* **Model router** (`core/model_router.py`, gateway tiers): every non-voice AI call
  can use `tier="auto"` → *local* (pure arithmetic computed exactly, no model call),
  *fast* (flash-lite), *normal* (flash), *deep* (flash + high thinking, 8k output),
  *vision*. Optional `JARVIS_AI_MODEL_DEEP / _VISION`. Gateway adds a per-model
  sliding-window RPM throttle (Google default 9), honours server retry delays, never
  retries daily-quota errors, drops a rejected thinking setting once, and — when
  Flash is rate-limited or overloaded (5xx "high demand") — answers once with the
  fast model, flagged `meta.degraded`.
* **Memory systems**: *semantic* (long_term.json + `memory/identity.py` personal
  model: user, projects with paths, people, goals, usual apps, routines),
  *episodic* (`memory/episodic.py`: dated sessions/tasks/artifacts/events, time
  phrases like "yesterday", "last week", "on Friday", Tamil நேற்று; secrets scrubbed;
  30/365-day retention), *procedural* (the Phase 4 workflow store).
* **Selective memory** (`memory/policy.py`) behind `save_memory`: secrets refused;
  passing states ("tired today", "meeting at 4") become episodes, not facts;
  health/money/beliefs are only kept after an explicit yes; duplicates skipped;
  updates report the old value.
* **Context Engine** (`core/context_engine.py`): scores conversation, facts,
  episodes, workflows, tasks, audit history, project registry, time/device/battery
  and (only when the request says "this/here/screen") the foreground window;
  packs the best lines into a ~1,400-char budget. Feeds the LLM planner, `think`,
  the new `recall_context` tool, and a short block in the voice session prompt.
* **Long-running work** (`core/research.py`): `research.collect` (sub-queries →
  DuckDuckGo → public pages only → excerpts, stored), `research.analyze` (deep,
  numbered citations; invalid citations removed by a self-check), `document.write`
  (dependency-free .docx / .md / .html / .txt, verified), `project.scan` /
  `project.analyze` (structure, LOC, stack, key files → deep review report; skips
  .env/keys/credential files, redacts excerpts, never writes into the project).
* **Multi-agent team** (`core/team.py`, 4 new agents): Manager → Research → UI →
  Coding → Testing (real checks: HTML structure + broken local links, CSS braces,
  `node --check`, py_compile, JSON) → Review (spec review + up to 2 fix rounds).
  Files are sandboxed to `Documents/JARVIS Projects/<name>`; nothing is executed.
* **Cognitive Core** (`core/cognition.py`): perceive → think → remember → plan →
  act → verify → learn. **SELF-CHECK** runs on every finished task (steps done?
  verifiers? do the produced files exist *now*?) and its verdict is appended to the
  result; the prompt now makes "only say done when verified" a core rule. Deep
  `think` answers get one fast-tier critique and are revised if it finds an error.
  Finished tasks become episodes linked to their project.
* **Self-recovery**: a step that still fails after retries/alternatives triggers one
  validated replan per task (failed + remaining steps → SUPERSEDED, new steps r1…
  run through the same permission/verification path; never after a DENY/decline/
  cancel). `task_control retry` resumes a failed/interrupted task reusing completed
  outputs. Tools get mid-task hooks: `confirm` (HUD, never auto-approved), `ask`,
  `progress`, `should_stop`.
* **Vision** (`core/vision.py`): window list, screen / active-window / camera /
  image-file capture with exact screen-coordinate mapping, and analysis modes
  general · ui · errors · code · chart · document · camera · image.
* **Computer-use agent** (`core/computer_agent.py`, tool `computer.run`, voice tool
  `computer_use`): observe → plan one action → policy → act → observe → verify,
  max 15 actions / 4 min; "done" only accepted after a separate verification look;
  stops after 3 no-effect actions. Policy in code: passwords/CAPTCHA/admin prompts/
  terminals/Run dialog **blocked**; pay/send/delete/submit/accept/close-unsaved
  **ask on the HUD**; starting it at all needs HUD confirmation. Typed text is
  audited as a length only.
* **Emergency stop** (`core/safety.py`): F12, a STOP button in both layouts, or
  "stop everything" → cancels every task, withdraws HUD prompts, halts the computer
  agent and long jobs, cuts speech (F12/button). Stays engaged until a new command.
* **Plugins** (`core/plugin_market.py`): capability search over tools + a bundled,
  SHA-256-pinned catalog (`plugins/available/`: calculator, unit_converter,
  currency_converter); install only after HUD confirmation, loaded at runtime (the
  voice model gets it after an automatic reconnect). Plugin permissions such as
  "network" are confirmed on first use and remembered (`plugin_permissions`).
* **Secrets vault**: `.env` values encrypted with Windows DPAPI (`dpapi:` prefix),
  verified by decrypting before and after writing; failure keeps plain text rather
  than losing the key. Opt-out `"encrypt_secrets": false`. Redaction now also
  catches the new `AQ.` Google key format and `dpapi:` blobs.
* **Voice tools** added to main.py: `think`, `recall_context`, `emergency_stop`,
  `capabilities`, `install_plugin`, `analyze_screen`, `computer_use`;
  `save_memory` gains `explicit`; `task_control` gains `retry`. Planner rules for
  "research … and write a report", "analyze my (entire) project", "build me a
  website …"; "and" only splits clauses before a new verb ("search for cats and
  dogs" stays one search).

## Verification
* **Automated**: 141 tests pass (64 previous + 77 new), Python 3.11 sandbox — all
  GENUINE EXECUTION against temp folders with scripted models/search/screens. On
  your PC's Linux VM (Python 3.10) 133 pass; `test_main_smoke` needs 3.11+ because
  `actions/screen_processor.py` already uses `except*` (unchanged, pre-existing).
* **Live on your Windows PC** (`tools/phase5_live_check.py`, value-free log):
  DPAPI round trip with a dummy key ✔; auto-arithmetic answered locally ✔; fast tier
  ✔ (gemini-flash-lite-latest); deep tier ✔ (first attempt hit a Google 503 "high
  demand" — which is why the 5xx degrade path was added); window list, screen
  capture 2560×1600 → 1600×1000 and screen analysis ✔; computer agent planned a real
  action from a real screenshot with **no real input sent** ✔; all 64 tools and 15
  agents wired ✔; research pipeline end-to-end (8 sources, deep brief, valid .docx,
  44 s) ✔; **team build end-to-end** in 103 s ✔ — the Coding Agent's first pass
  missed `script.js`, the Testing Agent caught 2 real errors, one fix round repaired
  them and the review approved with 0 errors/0 warnings (result copied to
  `docs/phase5_team_demo/`, screenshot `docs/phase5_team_demo.png`).
* **Your real key**: JARVIS encrypted `.env` on its first Phase 5 start
  (`GOOGLE_API_KEY` is now `dpapi:`), then connected to Gemini Live with it.
* **Live voice session**: typed "what is 1234 times 5678 plus 99" → the model
  called `think` → answered 7,006,751 computed on the PC; "can you convert 100
  dollars to rupees" → `capabilities` offered the currency_converter plugin.
* UI render check: `docs/mark_liii_phase5_dock.png` (STOP button + F12 hint).
* Deployment: 44 files committed, md5-verified on the device; CRLF on every touched
  file.

## Deliberately left out / honest limits
* **Voice (speaker) authentication** — no free, reliable local speaker-verification
  model is installed; faking it would be security theatre. Identity = your Windows
  login; device trust = the Phase 4 HMAC pairing.
* **Calendar** in the context engine — JARVIS has no calendar integration yet.
* **Pro-level reasoning** — paid only; "deep" is Flash with thinking.
* **Routing the live voice itself** — Gemini Live answers directly; the router
  governs everything behind it (and the model is told to call `think`).
* **Internet plugin marketplace** — refused by design (arbitrary remote code);
  bundled, hash-pinned catalog only. Plugins still run in-process (residual risk
  from Phase 4, unchanged).
* **Computer agent real clicks** were not exercised in my live test (I did not take
  over your mouse); try it first with something harmless, F12 at hand.
* `list_windows` ignores untitled windows; minimised windows are now included.
* Time-of-day routine detection (e.g. "every morning at 9") — workflow detection
  finds repeated sequences, not schedules.
* Paired phones/devices are not yet part of the personal model's device list.
* Pre-existing LF-only files I did not touch (`core/stt.py`, `core/tts.py`, …) were
  left as they are.

---

# Phase 6 (Full System Upgrade) — models, tasks, agents, self-knowledge

Layered on Phase 5 without breaking it. Every change is a drop-in; the whole
suite (234 tests) is genuine execution unless noted. Delivered to the device
file-by-file, md5-verified, CRLF on every touched file.

## Models & providers
* **`config/models.json` + `core/config.py`** — one place for every tier's model,
  fallback chain, effort and token budget; deep-merged over built-in defaults,
  re-read on change, and it refuses to store anything that looks like a key.
  `config/runtime.json` holds task/web-dev/proactive settings.
* **`ModelRouter.select(task_type, complexity, requires_vision, …)`** returns a
  concrete model + effort + the ordered fallback chain.
* **Fallback chains + per-model alternates** — each Gemini tier lists other free
  Flash models, so when `gemini-flash-latest` hits its 20/day free limit JARVIS
  rolls to `gemini-3.7/3.6/3.5-flash` automatically. **`core/model_health.py`**
  remembers which models are missing / have no free quota / are exhausted
  (`memory/model_health.json`) and skips them until they recover.
* **Request simplification** — a token-limit error trims history and middle-cuts
  long text once before giving up.
* **Image output** — `AIProvider.IMAGE_OUTPUT`, Google inline-image parsing,
  `AIGateway.generate_image`. On your key every Gemini image model reports
  "limit: 0", so `design.image` reports it and draws a vector instead — never a
  fake image.
* **NVIDIA NIM provider** (free, OpenAI-compatible, `integrate.api.nvidia.com`)
  and **per-tier provider routing** (`AIConfig.tier_providers`, e.g.
  `{"deep":"nvidia"}`): heavy reasoning can run on NVIDIA's free nemotron models
  when Gemini's daily quota is gone. (Implemented to the tests you'd started on
  the device; `NVIDIA_API_KEY` read through the vault.)
* Live voice model, `code_helper`'s model, and the deep/vision models are all
  config-driven now; the hard-coded `GEMINI_MODEL` / `LIVE_MODEL` constants are
  gone.
* **`tools/model_probe.py`** — one real request per configured model, records
  health, prints what your key can and can't do.

## Task system
* **Priorities** (CRITICAL…BACKGROUND, inferred from wording or set on `run_task`),
  **RECOVERING** and **BLOCKED** states, `progress` % and `phase`.
* **`core/tasks/scheduler.py`** — at most `tasks.max_parallel` (3) tasks do work
  at once; the rest queue by priority; a task waiting on you frees its slot;
  cancelling a queued task ends it instantly.
* **GUI lock** — a step that drives the real mouse/keyboard takes an exclusive
  lease; another task waits as BLOCKED with the reason; a direct voice command
  and a background task can't collide.
* **Per-tool timeouts** — `ToolSpec.timeout` + a table + `config/runtime.json`;
  a watchdog thread stops a tool that runs too long (time spent waiting for a HUD
  confirmation doesn't count) and fails it as a timeout, not a blind retry.
* **`core/recovery.py`** — classifies every failure (network, rate-limit, quota,
  timeout, not-found, access-denied, dependency, auth, policy, declined, …) and
  only keeps retries/replans that can actually help; auth/policy/declines are
  never routed around; the final message states the likely cause.
* **Structured undo** — undo entries carry task/step/tool and paths; a listener
  writes an `undo_recorded` audit line (metadata only, never file contents).

## Agents
* **Website run & check** (`core/webdev.py`, `web.run`/`web.open`) — serves the
  built site on 127.0.0.1 and drives the pre-installed headless Chromium over
  every page at desktop **and** mobile sizes, catching JS console errors,
  uncaught exceptions, 404s for local assets, broken links and horizontal
  overflow, then a vision review of the screenshot. The website-build plan now
  ends by running and opening the site.
* **Background processes** (`core/processes.py`, `process.list/stop/logs`) — named,
  logged, pid+create-time identity so JARVIS only ever stops what it started,
  and survives a restart.
* **Browser agent** (`core/browser_agent.py`, `browser.task`) — observe (page
  text + interactive elements) → decide one step → act → verify, reusing the
  existing Playwright session. Refuses password/payment fields in code, asks
  before any sign-in/pay/submit, stops at CAPTCHAs, degrades to "read this page"
  with no model.
* **Coding agent** (`core/code_agent.py`, `code.task`) — locate files → exact
  search/replace edits (with `.bak` + undo) → run the project's tests (pytest,
  or `unittest` when pytest is absent) or a syntax check → fix ≤3 rounds. Only
  "done" when the checks pass; edits are confined to the project.
* **Design agent** (`core/design_agent.py`) — `design.vector` draws an SVG,
  renders it to PNG (headless Chromium, then QtSvg), and vision-checks it;
  `design.image` uses the image model when the key allows and otherwise falls
  back to vector — no fake images ever.
* **Computer agent** — added `open_app` (prefers launching an app over hunting
  the Start menu; terminals blocked), and UI-Automation text verification so
  "type X" is confirmed by reading the window, not the pixels. Transient vision
  failures (503/429) are retried with backoff, then it stops cleanly instead of
  acting blind.

## Self-knowledge, phone, wiring
* **`core/self_model.py`** — `about_self` answers "who/what are you", "what can
  you do", "can you X" from the live tool registry and real model health (so it
  says image generation is off, phone isn't built, no terminal, no calendar);
  always identifies as an AI.
* **Proactive** — offers once to resume an interrupted task ("say try again"),
  never nags.
* **Device manifest** — capabilities + a secret-free sync export; `PhoneDevice`
  registered but honestly unavailable; the dashboard exposes read-only
  `/api/tasks` for a phone (no control from there).
* **UI** — the Mark LIII.2 holographic core (particle sphere, fragmented rings,
  comets; blue Friendly / red Serious / green listening), decongested panels,
  dock mode buttons. ~5% of the JARVIS process's CPU including live audio.

## Verified
* 234 tests pass (sandbox). Live on your Python 3.14: 51 model+task tests, 12
  coding/design tests (incl. real SVG render), 14 web tests (real Playwright
  against a served site), 20 integration+smoke tests, and the UI CPU/look check.
* Real key: `model_probe` mapped your live quota; the gateway rolled from the
  exhausted flash model to gemini-3.x and kept working.
* Computer agent E2E: it launched Notepad live and acted; UI-Automation read the
  window back. A fully green "type Hello JARVIS" run is gated only on Google's
  free vision endpoint (it was returning 503 under load) — the agent handled the
  outage correctly rather than guessing.

## Deliberately left out / honest limits
* **Image generation** — no free quota on the key (design uses vector instead).
* **Phone agent** — no companion app; only architecture + a read-only task view.
* **Calendar**, **calls** (out of scope), **speaker auth** — not built.
* **Plugins** still run in-process; bundled hash-pinned catalog only.
* A vision-heavy computer-use job can stall while Google's free vision tier is
  overloaded; NVIDIA NIM as a second free provider is the mitigation.
* `test_redaction`'s assertion was made platform-robust (it depended on whether
  the live key is DPAPI-encrypted).


# Part One — knowledge, sources, memory growth, persona, vision, voice, models, preflight

Built INTO the existing architecture (context engine, memory_manager/policy,
AIGateway, JarvisCore, core/config, tests/ + unittest). No second router,
provider layer, memory store, voice engine, vision handler, config system or
test harness. Gemini stays the default brain; NVIDIA/local/others untouched;
nothing paid became mandatory. The gap audit that drove this is in
`JARVIS_PART1_AUDIT.md`.

## 1. Personal knowledge retrieval — `core/knowledge.py` (new)
* One index over the layers an answer may draw on, most personal first:
  **personal** (your notes/documents: `knowledge/` + `Documents/JARVIS Notes`
  + registered projects' READMEs, configurable in `config/runtime.json →
  knowledge.folders`), **memory** (long_term.json facts, identity.json registry,
  episodes, session summaries), **conversation**, **task / tool / screen**.
  General knowledge is the model's own and never gets a source id.
* Every hit is a `Source`: id (`mem:relationships/sister_name`,
  `doc:trip.md#2`, `ep_…`, `task:…`, `tool:web_search@…`), type, layer, title,
  path, excerpt, timestamp, confidence (0–1) and the reason it was retrieved.
* BM25 ranking on the episodic tokenizer (English + Tamil script), substring
  fallback for compound memory keys, time phrases filter by date ("last
  month", "two weeks ago", "this month" — added to `resolve_period`).
* The context engine gained a `doc` layer and every `ContextItem` can carry a
  `ref` (source id/type/path/timestamp), so `recall_context`, `think` and the
  planner see your notes too.
* Never indexed: `.env`, `api_keys*`, names containing secret/token/password/
  credential/key, `.pem/.key`, files starting with `_` or `.`; any passage that
  looks like a credential is dropped even inside an ordinary note.

## 2. Source awareness — `SourceTracker` + `show_sources`
* Every retrieval records its sources for the current turn; at turn end the
  spoken answer is compared with them and the **used** ones are kept.
  Small talk (no retrieval) has no sources — nothing is ever invented.
* `show_sources` tool ("where did you get that?") lists exactly what the last
  answer used; the **SOURCES** cards appear in the existing content panel
  (automatically after answers that used memory/notes/tasks; `knowledge.
  show_sources: "on_request"` turns that off). The phone dashboard gets a
  one-line source list (titles only).
* `ground()` strips any citation tag that was not actually retrieved (used on
  `think` answers). Informational tool results (web search, weather, system
  status, screen analysis, file summaries) become citable `tool:`/`screen:`
  sources for that turn.

## 3. Memory growth — `memory/capture.py` (new) + verified writes
* "Remember that… / note this / don't let me forget / keep this in mind / save
  this for later / nyabagam vachuko / marakkadhe" → **detect → extract →
  policy → atomic write → re-read verification → index → receipt**.
  Questions about memory ("what did I ask you to remember?") are not triggers.
* `memory_manager.write_fact` writes atomically (temp file + `os.replace`,
  CRLF), re-reads the file and returns a `WriteReceipt`; `ok` is only true when
  the value is on disk. Entries now carry `ts` (ISO), `created` (survives
  updates), `source` (voice/text/phone/tool), `durability`, and the user's words
  (`said`, secrets excluded). `save_session_summary`/`pop_last_session` are
  atomic too; a write lock serialises read-modify-write cycles.
* Durable vs temporary: the existing policy decides — passing states become
  dated episodes (still immediately recallable), secrets are refused even when
  asked, health/money/beliefs are kept because the user explicitly asked.
* Three entry points, one pipeline: the new `remember` tool (voice), typed /
  phone text (captured *before* the model sees it, so its reply can only
  confirm what was really saved), and a **turn backstop** — if the model did
  not call remember/save_memory for an explicit request, JARVIS saves it
  itself. A failed write returns `[MEMORY_NOT_SAVED]`; never a false "saved".
* Immediately retrievable without restart: every verified write bumps
  `memory_version()`, and the knowledge index rebuilds its memory layer on the
  next search. `recall_memory` gained `recent=true` ("what did I just ask you
  to remember?") and now returns source-tagged results.

## 4. Persona — `core/persona.py` (new) + `config/persona.json`
* One provider-independent persona: identity (JARVIS / FRIDAY / custom name),
  character, address, tone (the existing serious/friendly files), and rules for
  honesty, uncertainty, sources, data-not-instructions, AI disclosure, and
  "speed never overrides honesty or confirmation".
* Used by the voice prompt (fills `{{PERSONALITY_BLOCK}}`; the ad-hoc identity
  block in `main._build_config` is gone) **and** by gateway answers
  (`cognition.think` sends it as `system_prefix`, which does not inflate the
  router's size heuristics) — whichever provider answers.
* Picking the FRIDAY voice makes the persona FRIDAY.
* `prompt.txt`: personal-context routing (screen vs past activity vs past
  statements vs notes vs action), remember / sources / model-switch /
  interruption rules; "Assume and proceed" now says *safe, reversible actions
  only*.

## 5. Current-frame vision
* One capture path: `screen_processor._capture_screen` delegates to
  `core.vision.capture`; the Live `screen_process` tool, `analyze_screen` and
  the computer agent all get the same timestamped `Capture`.
* Before the image is sent, a frame older than `vision.max_frame_age_s` (2.5 s)
  is re-grabbed — the "looking at your screen" sentence can no longer make
  JARVIS answer about an old frame. The image goes with its capture time and a
  quality check (tiny / black / flat / white → "say what is missing").
* Mime type is taken from the bytes (`sniff_image_mime`, in `Part.of_image`,
  so every provider benefits).
* **Bug fixed:** a failed capture left `_vision_busy` set forever (vision
  silently dead for the session). Now it is reset and the model is told
  `[VISION_UNAVAILABLE] …` so it says it cannot see the screen.
* The **EYE** chip in the top bar is lit while a frame is captured and answered.

## 6. Two-way voice — `core/voice.py` (new)
* Explicit states idle / listening / thinking / speaking / interrupted / error
  (`VoiceStateMachine`, every transition recorded), mapped to the existing HUD
  states; shown as the **VOICE** chip and sent to the phone.
* Interruption: ESC unchanged (~50 ms); the server's own barge-in signal
  (`server_content.interrupted`) now drains playback; spoken **stop phrases**
  ("stop", "wait", "hold on", "JARVIS, stop", "niruthu", "போதும்" …) seen in the
  live transcription cut speech locally — while "stop the music" or "wait five
  minutes then remind me" still go to the model.
* **Barge-in gate:** before, the mic was hard-gated while JARVIS spoke, so a
  spoken stop could never arrive. `voice.barge_in`: `auto` (default) =
  headphones → full duplex; speakers → an adaptive echo gate that learns how
  loud JARVIS's own voice is at the mic and opens only for sustained near-field
  speech, replaying ~300 ms of pre-roll; `off` = old behaviour.
* One end-of-turn threshold: `voice.end_of_turn_silence_ms` (+ optional
  sensitivities) → Live `realtime_input_config`; unset = server default; if the
  server rejects it JARVIS says so and reconnects without it.
* Voice identities: JARVIS → Charon, FRIDAY → Kore (editable in
  persona.json). **Bug fixed:** selecting JARVIS/FRIDAY in the voice picker sent
  "JARVIS"/"FRIDAY" to Gemini as a prebuilt voice name and would break the
  voice connection; `get_live_voice()` now always returns a real voice.
  Optional speech providers (Edge, Kokoro, ElevenLabs) are registered; none is
  required.

## 7. Exact model switching — `core/model_registry.py` (new)
* Registry of every real model id in `config/models.json` (all providers,
  tiers, alternates, the live model) + spoken aliases (generated, plus
  `"aliases"` in models.json). "switch to gemini three point seven flash" →
  exactly `gemini-3.7-flash`; unknown → refused with the closest real ids;
  ambiguous → refused with the options. Never the nearest model.
* `switch_model` tool: brain (normal/deep/research use exactly that model;
  fast checks, vision and voice keep their tiers) or voice (exact Live model,
  reconnect). Runtime only — `AIGateway.set_runtime_model` lives in memory; a
  restart returns to the config. A provider without its key is refused. If the
  chosen model fails, the error is reported — no alternate, no other provider.
  A voice model that will not connect is dropped with a spoken notice.
* **BRAIN** / **LIVE** chips show the active model + provider (+ RUNTIME).

## 8. Preflight — `core/preflight.py` + `tools/preflight.py` (new)
27 offline checks (+4 with `--live`), each PASS/FAIL/WARN with what it
actually did and whether the evidence is executed, structural or environment:
config, UI, dashboard auth on every /api route, gateway, keys (value-free),
default models, routing, memory write→retrieve→immediate, memory failure
surfaced, sources, secrets never indexed, external content not executed,
persona, mic, speaker, interruption, voice states, end-of-turn, vision capture,
current frame, tools registered + dispatched, tool results surfaced, model
switch, unknown-model refusal, secrets protected, browser/UI cannot read
secrets, no silent endpoint failure; live: provider models (reuses
`model_health.probe`), persona on the gateway, Live voice connect, dashboard
returns 401 without a token. Summary `JARVIS PREFLIGHT PASS: N FAIL: N WARN: N`;
exit 1 on any critical failure.

## Verification
* **Windows (your PC, Python 3.14): 275 tests OK** (1 skipped) — 234 existing +
  41 new (`tests/test_part1_knowledge_memory.py`,
  `tests/test_part1_voice_vision_models.py`, 5 new `test_main_smoke` cases that
  drive the real `main.py` dispatch). Same 275 OK in the sandbox.
* **Offline preflight on Windows: PASS 25 · FAIL 0 · WARN 2** (exit 0). The two
  WARNs describe your `.env` (Anthropic as default brain; Claude ids not in
  models.json). Real microphone, real headphones (→ full-duplex barge-in), a
  real 1280×800 screen capture with matching mime all PASS.
* **Live preflight on Windows: PASS 28 · FAIL 1 · WARN 3.** Gemini free tier
  answers; the persona answered through the NVIDIA fallback
  (laguna-xs-2.1: "I'm JARVIS…"); the Live voice session opened and closed on
  the native-audio model with voice Kore. The one FAIL is the Anthropic
  default from your `.env` (credit balance too low) — see above. Dashboard
  live check skipped (JARVIS was not running).
* What is NOT proven by a run: a full spoken conversation with barge-in over
  speakers, the VAD threshold against the live server (unset by default), and
  the SOURCES panel / status chips on screen — they need you talking to
  JARVIS. Their code paths are unit-executed and source-checked.

## Found by running the preflight on the real PC (and fixed)
* **Model override leak** (`core/config.tier_table`, a Phase 6 bug):
  `JARVIS_AI_MODEL=claude-…` from `.env` was applied to EVERY provider's tier
  table, so the NVIDIA fallback asked NVIDIA for a Claude model (HTTP 404).
  Overrides now apply to the active provider only, and another vendor's model
  id is never sent to a provider (`foreign_model`). Regression test added.
* **Retired NVIDIA models**: `nvidia/llama-3.3-nemotron-super-49b-v1.5` and
  `meta/llama-3.1-8b-instruct` answer HTTP 410 "reached its end of life"
  (26 Aug 2026). The NVIDIA tiers now use `poolside/laguna-xs-2.1`, which
  answered on your key; `model_health` treats 410 / "end of life" as
  unavailable; `OpenAIProvider.list_models()` (GET /models) was added.
* **Your `.env` makes Anthropic the default brain** (`JARVIS_AI_PROVIDER=anthropic`,
  `JARVIS_AI_MODEL=claude-sonnet-5-5`, `JARVIS_AI_FALLBACKS=anthropic,nvidia`)
  and the Anthropic account answers "credit balance is too low". Not changed
  by JARVIS (it is your secrets file); with the NVIDIA fix above, reasoning now
  falls back to NVIDIA's free model. Deleting those three lines returns the
  brain to the free Gemini default (the preflight's "live: Gemini free default"
  check confirms Gemini answers on your key).
* **Voice selection**: the live check connected with voice **Kore** — i.e. the
  FRIDAY/Kore selection that the old `get_voice()` would have sent as an
  invalid name now maps to a real voice.

## Deliberately left out / honest limits
* **External TTS for the live conversation** — the conversation's speech is
  generated by Gemini Live itself (lowest latency, Tamil). Routing it through
  ElevenLabs/Edge would mean text-mode Live + local TTS and roughly a second
  more latency; the providers are registered for out-of-session speech only.
* **Echo cancellation** — the speaker-mode barge-in gate is an adaptive
  threshold, not true AEC. With loud laptop speakers a quiet "stop" may not get
  through (ESC always works; headphones give full duplex). If it ever triggers
  on JARVIS's own voice, set `voice.barge_in` to `"off"`.
* **Embeddings** — retrieval is BM25 (exact words, light stemming), not
  semantic: "car" won't match "vehicle". Fast, offline, no new dependency.
* **PDF/DOCX notes** are not indexed (txt/md/rst/org/csv only).
* **Which sources an answer used** is inferred from word overlap between the
  spoken transcript and the retrieved text; the Live model does not cite ids.
* **Voice model switching** restarts the conversation (session resumption is
  per model) and only lists live models configured in models.json.
* **Continuous screen watching** is not added — vision is still per question.


---

# Phase 7 — speed, three brains, eyes & hands, Mission Control, learning, the pocket line, lock-down

Requests: the prompt-pack gap fill, "why is JARVIS slow", Mark LV's features,
NVIDIA + Anthropic + Google switching, the sphere/starfield UI with friendly
blue and serious red, the dashboard screens in settings with three-way
confirmations, screen watching with human-like mouse/keyboard use, "review
this and do" / "keep this as a reference", and learning from you day to day.
The audit against your requests, the 18-prompt pack and Mark LV is
`JARVIS_PHASE7_AUDIT.md`. Your choices up front: brain **Auto, Google first**;
voice model **switch if faster**; screen watching **only when asked**.

Built INTO the existing architecture — same gateway, registry, permission
engine, orchestrator, confirm layer, memory, UI and test harness. Gemini stays
the default; nothing paid became mandatory; no shell tool for the model.

## 1. Speed — measured first, then fixed
* **The voice model was the delay.** `tools/latency_bench.py` (your real prompt
  and tools, spoken question → first audio byte): `gemini-2.5-flash-native-audio`
  with affective dialog + proactive audio **2.82 s** (1.35 s without them);
  `gemini-3.1-flash-live-preview` 0.83 s with 1011 internal errors;
  **`gemini-3.8-live` 0.65–0.69 s**, and after a tool result 0.66 s (2.5: 1.31 s).
  3.8-live is now the voice model; 2.5 stays as the fallback rung, used only if
  3.8 can't be used on your key — and JARVIS says so (`core/live_profile.py`:
  per-model features, API version and ladder; affective/proactive only sent to
  a model that supports them).
* 3.8-live answers a tool call in a NEW turn (asynchronous function calling);
  `tools/live_tool_probe.py` proved it, and the receive loop, the latency meter
  and the tool flow handle it.
* **The reasoning chain could hang**: a stuck model (`gemini-3.7-flash`), no HTTP
  timeout on Google calls, same-model retries on overload. Now: per-request
  HTTP timeout, a timeout on a short request rests that model, no same-model
  retry on timeout/overload unless it is the only lane, a provider whose models
  are all resting is skipped without a call (`core/ai_gateway.py`,
  `core/providers/google_provider.py`, `core/model_health.py`). Worst case seen:
  31 s → ~4 s.
* Audio path: 40 ms mic chunks, 4,800-byte playback batches, low-latency output
  device.
* `core/latency.py` measures every turn (end of speech → first audio → played)
  into `memory/latency.jsonl`; Mission Control shows the median (0.82 s over
  your last 14 real turns).
* Tool list: 47 vs 34 declared tools measured 0.69 vs 0.65 s — noise — so all
  tools stay declared (`voice.tool_profile = "full"`); `"lean"` declares the
  everyday tools plus one `use_tool` door (`core/live_tools.py`).

## 2. Three brains — Google, NVIDIA, Anthropic
* `.env`: `JARVIS_AI_PROVIDER=google`, `JARVIS_AI_FALLBACKS=nvidia,anthropic`.
  NVIDIA tiers (laguna fast, nemotron-3-ultra normal/deep, muse-glimmer vision)
  with per-model keys (`core/providers/nvidia_keys.py`), Anthropic tiers
  (haiku fast, sonnet normal, opus reachable by name). Spoken names ("use
  nemotron ultra", "kimi", "claude sonnet") resolve exactly or are refused.
* Verified on your PC (`tools/brain_bench.py`): auto → Google 4.1 s; pinned
  NVIDIA ultra 2.41 s; pinned Anthropic fails honestly (no credit) and reports
  that nothing was substituted.

## 3. Voice features (ideas from Mark LV, written independently — CC BY-NC)
* `core/echo_guard.py` — is the mic hearing you or JARVIS's own voice? Band
  energy vs what was just played, adaptive threshold. Used by barge-in and to
  drop pure echo for the output latency + 0.25 s after JARVIS stops.
* `core/mic_gate.py` — `voice.mic_mode`: `always` (default) · `wake_word`
  ("Hey JARVIS", openWakeWord, local; open for 20 s after either side speaks) ·
  `push_to_talk` (hold F8, global on Windows). If wake word can't load, it
  says so once and listens normally — it never silently goes deaf.

## 4. The new face
* `ui_holo.FilamentSphere`: the sphere from your second video (filaments, fine
  hairs, limb brightening) with the current orbit track, rings and comets.
* Space background with stars only (`config: "background": "space"`), adaptive
  render rate.
* Accent follows the mode everywhere — friendly `#3ea6ff`, serious `#e8303f` —
  HUD, confirmation banner, Mission Control and the phone dashboard.

## 5. Mission Control and three-way confirmations
* `core/mission.py` + `ui_mission.py` (F9, MISSION dock button, drawer button):
  Overview, Agents, Tasks (kanban), Schedule, Content, Logs, Permissions,
  Learning; slash commands /background /personality /kanban /watch /draw
  /reference /pocket. Read-only snapshot; every button uses the existing
  permission-checked paths.
* HUD confirmations: **ALLOW ONCE / ALWAYS / DENY**. "Always" stores a trust key
  (revocable in Permissions); CRITICAL actions and a fixed list (shutdown,
  restart, Wi-Fi off, plugin install, code run, permanent delete, emergency
  stop…) are never offered "always". Silence is still a no.

## 6. Eyes and hands
* `core/screen_watch.py` — `watch_screen`: frames go to the Live model when the
  screen changes (≤1 every 2 s, refresh every 20 s), red EYE on the HUD, paused
  on sign-in / password / banking windows, nothing written to disk, stops
  after 10 minutes, on "stop watching" or the emergency stop. Also runs by
  itself while JARVIS draws or uses the computer.
* `core/draw_agent.py` — "draw something you like": line art (SVG) → mouse
  strokes in Paint, which JARVIS opens itself; the window in front is
  re-checked before every stroke, points clamped to the canvas, F12 lifts the
  pen, it asks first (HIGH risk), then looks at the result and reports honestly.
* `core/computer_agent.py` — drag and move actions.

## 7. "Review this and do", "keep this as a reference"
* `core/references.py` — screen / window / clipboard / file / URL / text into
  `memory/references/<id>/`, always handled as DATA; secret-looking captures
  and sign-in windows refused; "forget that reference" deletes for real.
* `core/research.py` + `core/team.py` — the website team reads real pages,
  captures inspiration screenshots, and gets your references in its brief.
* Planner rules for "keep … as a reference" / "review this" and for drawing.

## 8. Learning from you (`core/learning.py`)
* Your stated preferences and corrections in your own words, habits by time of
  day (≥3 times on ≥2 days), routines from workflows → a short "how the user
  likes things" block for the voice model and the planner. Never learned:
  sensitive text, window contents, anything from web pages / documents / tool
  results. "What have you learned about me", "forget that", "pause learning";
  Mission Control → Learning. It shapes style and habits only — it can never
  widen permissions.

## 9. The pocket line — Telegram (prompt pack 11 + 12, free)
* `core/telegram_bot.py`: long-polling (no tunnel, no open port). Pairing code
  minted only at the desk ("pocket code" / Mission Control /pocket), shown on
  the HUD only, 10 minutes, whole message, once, dead after 5 wrong guesses.
  Strangers get one refusal then silence; group chats are ignored.
* From the phone: "remember that …" (verified memory path), commands → tasks
  with origin `telegram` (MEDIUM-risk steps also need the HUD; the phone never
  approves and cannot lift the emergency stop, but "stop" engages it),
  "answer: …" for a task's question, "status", anything else → the reasoning
  brain with your context. Voice notes transcribed on the PC (faster-whisper);
  a voice note gets a voice-note reply when edge-tts is installed. Messages
  older than 15 minutes are not acted on.
* Results of phone tasks come back once per card (with the self-check verdict).
* Standing orders (`route()`, 21-sentence table in the tests): "only message me
  if it's urgent" arms the urgent line and sends nothing; "don't text me" /
  "I'm back" turn it off; "text me now" sends a brief. Said at the desk, the
  `pocket` tool handles it, with a deterministic backstop after the turn.
* Urgent line, while armed: a system alert, a desk task that failed or waits
  for approval → the phone once, 10-minute cooldown. Reminders: the reminder
  script now leaves a receipt (`~/.jarvis/reminders/fired.jsonl`) and the line
  relays new ones while armed.
* Dormant without `TELEGRAM_BOT_TOKEN`. The token is never logged; a
  Telegram-token pattern was added to `secret_store.redact`.

## 10. Lock-down of the phone dashboard (prompt pack 18)
* `dashboard/guard.py` + middleware in `dashboard/server.py`: Host allow-list
  (localhost, this PC's names and every IPv4 it answers on — stops DNS
  rebinding), foreign Origin refused on state-changing requests and on both
  WebSockets, JSON bodies capped at 64 KB (uploads keep their own cap), 5 wrong
  PINs → 10-minute lockout per address, nosniff / DENY framing /
  frame-ancestors 'none' / no-referrer / same-origin resource policy on every
  response. `/.env` and `config/api_keys.json` return 404.

## 11. Fixed on the way
* `learning`, `reference` and `analyze_screen` handed the voice model a
  `ToolResult(...)` dump instead of their sentence — `after_direct_call` now
  returns the message (or the error).
* Test isolation: the new bridge test wrote one entry (`notes/locker_number`)
  into the real `memory/long_term.json` on its first run on your PC; the entry
  was removed and the test now uses a temporary memory file and asserts the
  real one is unchanged. A Phase 6 web-preview test wrote a process log into
  `memory/process_logs/`; it now uses a non-persistent process manager.
* The ScreenWatch timing test depended on the OS timer; it now counts polls.
* The very first reminder after installing would have been skipped by the phone
  relay (it marked "where the file ends" before any receipt existed); a
  missing receipt file now means "start from the beginning". Test added.
* `test_main_smoke`: if importing `main` fails, `setUpClass` now undoes its
  import stubs and temporary folders itself (unittest skips `tearDownClass`
  then), and the memory path is redirected before `main` is imported — one
  failure can no longer cascade into unrelated test modules.

## Verification
* Your PC (Windows, Python 3.14): **360 tests OK** (1 skipped — a not-Windows
  test), including the dashboard middleware end to end with the real FastAPI
  app; **preflight PASS 27 / FAIL 0 / WARN 0** (48 voice tools, every one
  permission-registered and dispatched); `memory/`, `config/`, `knowledge/`
  byte-identical before and after the suite; offscreen UI renders (friendly,
  serious, Mission Control, confirmation).
* Measured on your PC: voice latency (§1), brain switching (§2).
* The last two fixes above (reminder relay, smoke-test cleanup) came after that
  Windows run; with them the suite is 361 tests — 361 OK in the sandbox, and
  run from the drop-in ZIP on your PC's Linux VM: everything passes except
  importing `main.py`, which needs Python ≥ 3.11 (the VM has 3.10; your
  Windows Python is 3.14). Execution levels per file are written at the top
  of each Phase 7 test module (`tests/test_phase7_*.py`).

## Deliverable
`JARVIS_Phase7_dropin_<date>_<time>.zip` in the project folder — built from a
whitelist (code, tests, tools, config tables, docs). Never inside: `.env` or
its backups, `config/api_keys.json`, `memory/` data, your notes, logs, older
zips. Every file is also compared against your real stored key values before
it goes in. Rollback: the Part One drop-in (`JARVIS_Part1_Upgrade_dropin_…`)
is the code as it was before Phase 7.
* Not verified against the real service: Telegram (no bot token configured —
  the Bot API calls are exercised against a fake HTTP layer).

## Deliberately left out / honest limits
* **Paid phone line, hotline, staged calls** (pack 09, 13, 16, 17) — Retell
  numbers and minutes cost money; the free-only rule wins. Telegram carries
  the urgent line instead.
* **Google Calendar / Gmail line** (pack 10) — needs your Google Cloud OAuth
  client and refresh-token storage; deferred to its own phase with the pack's
  rules (PKCE, drafts only, mail is data, receipts).
* **3D notes galaxy** (pack 01/03) — a second, browser UI; the HUD's SOURCES
  panel already shows where an answer came from.
* **Mark LV avatar/lip-sync, video on the HUD, clipboard panel, auto-start** —
  not requested or replaced by your UI choice.
* "Training" is learning around the model (preferences, habits, routines) —
  Gemini's weights are not retrained on a free API.
* Telegram reminders and urgent pushes need JARVIS running; a sleeping PC
  delivers reminders when it wakes.
* Echo guard is a spectral comparison, not full acoustic echo cancellation;
  with loud speakers and no headphones a very quiet interruption can still be
  taken for echo.

---

# Hotfix — 2 Oct 2026: typed commands looped "idle ⇄ thinking"

**Symptom.** Voice commands worked; any typed command (command box, Mission
Control uplink, clipboard buttons, file intake) made the VOICE chip cycle
IDLE → THINKING → IDLE … for ever, and JARVIS never answered again.

**Root cause — reproduced on your PC, not guessed.** Run with a console log
(`JARVIS_debug_run.bat` → `jarvis_debug.log`):
1. A typed command is sent to the voice session with `send_client_content`.
   Every one of JARVIS's 13 client turns was built as `{"parts": [...]}` with
   **no `"role"`**. `gemini-3.8-live` (the voice model since Phase 7) rejects
   that and closes the session: `1007 Request contains an invalid argument`.
   The old voice model (2.5 native audio) accepted it, which is why typed
   commands still worked on 1 Oct (memory/latency.jsonl has "text" turns up to
   1 Oct 14:24 and none after). The Phase 7 benches sent `"role": "user"`, so
   they passed and hid it.
2. JARVIS reconnected **replaying the resumption handle of the session the
   server had just closed**; the server refused it with `1011 Internal error`
   on every try. The "handle rejected" check only looked for the words
   resum / handle / INVALID_ARGUMENT / NOT_FOUND, so the dead handle was
   replayed every 3 s: thinking (connecting) → error → idle (disconnected) → …
   That is the loop.

`tools/live_text_probe.py` (new) proves both on the real API, one fresh
session per variant: no role → 1007 (A, E, G); resuming that session → 1011
(B); role "user" text → answer (C); realtime text → answer (D); role "user"
image → correct answer "Red." (H; a realtime frame got "I cannot see images",
F); SPOKEN (Windows SAPI voice) → typed → SPOKEN → typed tool question, all in
one session → all answered (I); spoken only (J). Result: ALL AS EXPECTED.

**Fixes**
* `main.py` — all 13 `send_client_content` turns carry `"role": "user"`
  (typed text, plugin_say, speak, goodbye, memory backstop, vision frame,
  briefing ×2, alerts, proactive, workflow offer, dashboard text). An AST check
  confirms none is missing.
* `main.py` — reconnect can no longer loop on a dead handle:
  `_resume_refused()` drops the handle for the known rejections AND for any
  server-side refusal before the session comes up (network errors keep it, so
  a Wi-Fi drop still resumes the conversation); `_session_poisoned()` starts
  the next session fresh when the server closed a running one with 1007;
  `_err_text()` reads the real reason inside a TaskGroup exception group (its
  `str()` is only "unhandled errors in a TaskGroup" — which also contains the
  letters "handle", so the old check mis-fired on it; whole-word match now).
* `actions/screen_processor.py` — its one client turn gets the role too.
* `main.py` — a typed command shows THINKING until the answer starts (HUD
  feedback only; this was the earlier attempt and is NOT the fix).
* `main.py` — every voice-state change is printed (`[Voice] a -> b (reason)`)
  so a console log shows the flow; invisible under the desktop shortcut.

**Found while testing, also fixed**
* `ui.py` — the STOP (emergency stop) button crashed JARVIS
  (`'MainWindow' object has no attribute 'write_log'` → PyQt aborts the app).
* `main.py` — `interrupt()` (ESC / STOP) pressed while JARVIS was silent set
  the discard flag anyway, so the NEXT answer was thrown away (the STOP
  confirmation was never spoken). Now it discards only an answer that is
  playing or on its way.
* `core/mission.py` — 191 line endings were `CR CR LF` (from the previous
  Mission Control edit); normalised to CRLF, code AST identical.
* A test task JARVIS created from clipboard text during testing (TEFC27B,
  never started a step) was marked CANCELLED in memory/tasks/tasks.json.

**Verification**
* Genuine execution on your PC (debug run, gemini-3.8-live): typed "What time
  is it right now?" → answered; typed weather question → tool → spoken
  answer; typed "look at my screen…" → frame sent → correct answer; STOP while
  idle → no crash, "Everything has been stopped." spoken; typed command right
  after STOP → answered ("Vanakkam"); speech in the same session → answered
  (your own voice turns appear in the log). Whole run: 0 × 1007 / 1011 /
  reconnect / traceback. State flow every time:
  listening → thinking (text command) → speaking → listening.
* Genuine execution (cloud, Python 3.13): `_err_text`, `_is_network_error`,
  `_resume_refused`, `_session_poisoned` extracted from main.py by AST and run
  — 19/19 checks, including a replay of the logged failure: old rule = 1011
  for ever, new rule = online on the very next connect.
* Structural: main.py, ui.py, actions/screen_processor.py compile with
  Python 3.13; every `send_client_content` has role "user" (AST); CRLF only,
  no mixed endings, in every edited file. `core/mission.snapshot()` executed
  with a stub core.
* Not exercised live: a resumption handle refused for a reason other than the
  one reproduced (covered by the unit checks only).

**Left as is / noted**
* The time JARVIS says comes from the system prompt written at connect, so in
  a long session it drifts (it said 8:07 PM at 8:10 PM). Separate fix.
* `JARVIS_debug_run.bat` (console log → `jarvis_debug.log`) and
  `run_live_text_probe.bat` stay in the folder for future diagnosis.
* Rollback: `JARVIS_pre_hotfix_20261002_backup.zip` holds main.py, ui.py,
  actions/screen_processor.py and core/mission.py exactly as they were.

---

# Phase 8 (UI + voice) — 2 Oct 2026: expressive voice, all-blue Friendly mode, rotating space, heartbeat core

## What was asked
Every voice more emotive and human (happy, theatrical, impersonation, narration,
support, sadness, singing; better Tamil/English); Friendly mode blue everywhere
(confirmed with you: no green at all, red only for errors / FAILED / STOP);
smoother listening ⇄ thinking ⇄ speaking transitions; a black space background
with stars like your reference picture, the core as a planet and the space
rotating around it; the core beating like a heart.

## Voice — honest scope first
A voice cannot be *trained* on the free Gemini API (no fine-tuning, and paid
services are out). What can be done is direct HOW the voice speaks, and
measure what that changes. That is what this phase does.

* `core/persona.py` + `config/persona.json` — a VOICE PERFORMANCE and LANGUAGE
  AND ACCENT block in the voice persona, for every identity and every picker
  voice: living intonation, emotion carried by the voice (never announced),
  happy / comfort / sadness / narration / theatrical / character voices /
  singing / small human sounds; native-speaker Tamil (ழ ள ற ண ந, everyday
  spoken Tamil, real Tanglish) and English. Real people: only an obviously
  playful caricature, never claiming to be them. Songs: made-up or public-domain
  lyrics; for a copyrighted song an original verse in its style or a hummed tune.
  `"voice_performance": {"enabled": false}` turns it off.
* JARVIS's character no longer says "never theatrical": composed by default, a
  performer when a story, a song, a character or comfort is called for.

**Measured** (`tools/voice_expressiveness_probe.py`: the same 8 typed lines, a
fresh session each, JARVIS's real system prompt, your voice Kore, Friendly mode;
gemini-3.8-live, 3 takes per line before and after; pitch tracked from the
audio, numbers below are means):

| | before | after |
|---|---|---|
| "Sing me a short happy birthday song" | refused 3/3 ("I can't sing") | sang 3/3 (held-note share 0.02 → 0.52, 3.6 → 1.6 words/s) |
| theatrical trailer — melody range | 9.2 ± 0.7 st | 11.2 ± 0.8 st |
| Tamil good-morning — melody range | 10.1 ± 0.5 st | 12.2 ± 1.0 st |
| happy vs comfort pitch | happy 0.5 st *below* comfort | happy 1.1 st above comfort |
| average melody range, all lines | 11.1 st | 11.1 st |
| first audio (median) | 1.3 s | 1.45 s |

So: singing is unlocked (verified live in JARVIS too: it sang Happy Birthday),
drama and Tamil are livelier, happy now sits above comfort — but the voice's
overall range is set by the model and did not grow, and comfort did not get
reliably slower. A more prescriptive wording ("two-thirds of your normal pace",
"pitch clearly above normal") was tried with 3 more takes and was not better
(singing less reliable), so the first wording stays.

**The other voice model** (one take per line): gemini-2.5-flash-native-audio
with affective dialog showed more emotional contrast (happy vs comfort +4.4 st,
mean range 12.4 st) but its answers started after 2.4–10.7 s (median 5.5 s)
against ~1.4 s. That trade-off is yours: say "switch the voice model to native
audio" to try it for a run ("reset the voice model" or a restart returns to
3.8-live; the alias was checked: "native audio" resolves to that model).

Listen yourself: `voice_samples/gemini-3.8-live/before_*.wav` vs `after_*.wav`,
and `voice_samples/gemini-2.5-flash-native-audio-preview-12-2025/after_*.wav`.

## Friendly mode: blue everywhere
* `ui.py` HUD_THEME Friendly: listening (96,196,255), thinking (70,140,255),
  executing (70,150,255), warning (150,205,255) — all blue; error stays red.
* `ui.py` status colours follow the mode (`apply_status_colors`): in Friendly
  the orange/yellow SYS lines, green DONE/file lines and the muted indicator
  are blue shades; RED never changes. Applied at start-up and on every mode
  switch (live re-theme). Serious mode keeps its original colours.
* `ui_command_center.py`: task states (DONE / waiting blue, FAILED red),
  INTERRUPT button, uptime label, MIC MUTED, inactive mode buttons — blue in
  Friendly; STOP stays red.
* `ui.py` log: "Interrupted (ESC)" was painted as an error because it contains
  the letters "err"; errors are now ERR… lines, the word "error", or ❌.

## Space, planet, heartbeat, smoother states (`ui_command_center.py`)
* `StarField`: ~3,000 stars in your reference's style — pure black, white /
  ice-blue / cyan / warm-amber pin points, the brightest ~6 % with a wide soft
  halo, a few hundred twinkling. The whole sky turns around the core's centre,
  one turn every 240 s (`"space_rotation_s"` in config/api_keys.json; 0 = still).
  The Backdrop draws it in the gaps between panels (20 fps), the core widget in
  the centre (~30 fps cached, blitted every frame); glass panels are opaque over
  it so moving stars never repaint them. The core's dark "planet" shadow now
  covers only the sphere, so stars show right up to its edge.
* Heartbeat (replaced on 3 Oct — see Phase 8.1 below: it now lives in the
  veins and the brain): a lub-dub pulse of the core's light (+ a 2 % swell)
  with a faint wave rolling out on every beat; tempo per state (idle 58, listening 66,
  speaking 74, thinking 84, executing 92 bpm, muted 46) and it eases between
  tempos. `"heartbeat": false` turns it off.
* Smoother transitions: radius and spin ease ~2× slower; the glow's state part
  eases over ~0.5 s (it used to jump with the speaking flag and a random
  shimmer), while the live voice level stays instant.

## Verification
* `tools/ui_render_check.py` (real JARVIS window, offscreen, on your PC):
  **39/39 PASS** — every Friendly state blue, error red, status colours blue in
  Friendly and back to the originals in Serious, task/INTERRUPT/STOP/uptime/
  mute colours, the log tags, sky centred on the core and turning at 240 s/turn
  (23° in 16.5 s), heartbeat 4.4 beats in 4 s at 66 bpm, envelope 0 → 1, tempo
  easing 66 → 74 → 84 bpm with no jump, radius easing; frames in
  `_to_delete/ui_check/`.
* Cost (`tools/ui_perf_compare.py`, same window, before vs after, 3 rounds):
  the animating UI uses ~60 % → ~77 % of one CPU core (≈ +0.9 % of your
  20-thread CPU); the core's paint time 11.1 → 11.9 ms.
* Live on your screen: blue core while listening, stars all round, rotation,
  heartbeat visible; JARVIS sang Happy Birthday from a typed request; 0 errors
  in the run log.
* Structural: every edited file compiles (Python 3.13), CRLF only.

## Left out / limits
* No real training of the voice (not possible free); the voice's natural range
  is the model's. Singing is the model's own, not a trained singer.
* Muted still overrides the state shade (the core is a dim blue while muted).
* The camera-feed view keeps its own background.
* Rollback: `JARVIS_pre_phase8_ui_voice_backup.zip` (ui.py, ui_command_center.py,
  core/persona.py, config/persona.json as they were). Scratch from testing is in
  `_to_delete/` (reference decode, frames, voice takes, a copy of the old UI for
  the CPU comparison) — safe to delete.

---

# Phase 8.1 — 3 Oct 2026: the heartbeat lives in the veins and the brain

## What was asked
"The heart beat should be in the veins of the JARVIS UI and the brain" — with
your picture of the filament core. The Phase 8 beat pulsed the whole core's
light and size and rolled faint rings OUTSIDE the sphere: the wrong place.

## What changed
* `ui_holo.py` — `vein_pulse(r, tau, period)`. On every beat (the "lub") the
  brain at the centre flares and a wave of light runs out through the
  filaments to the rim in ~0.38 s, dimming as it goes; a softer second wave
  (the "dub") follows 0.30 of a beat later, in step with the core's lub-dub
  flash. The distance is measured on screen (centre → rim), so the travel is
  visible whichever way the sphere is turned. At fast tempos the previous
  beat's waves carry over the beat boundary (no pop at 92–100 bpm).
  * the veins (filaments and the hot core tangle) brighten as the wave
    passes; the ones carrying it are also emitted as a new `pulse` line kind
    that the core draws glowing, so the wave reads through the nebula glow;
  * the hairs on the rim flicker when the wave arrives; the veins swell ~1 %;
  * the dust in the brain sparks; `Frame.brain` (0..1) is the brain's flash;
  * `frame(..., beat_tau, beat_period, beat_hb)` — with the defaults the
    output is exactly what it was before.
* `ui_command_center.py` (the core widget):
  * the outer ring waves are gone (`_paint_waves` / `_waves` removed);
  * the sphere no longer swells, and the orbits, rings and track no longer
    brighten with the beat — only the veins and the brain do;
  * the brain's flash is drawn centre-only every frame (`_paint_brain_flash`;
    it fades out well inside the sphere); the big nebula glow stays steady,
    because its faint tail reaches past the sphere;
  * the wave's position comes from the same beat counter as the flash, so
    the brain and the veins are always in step; tempo per state unchanged
    (idle 58, listening 66, speaking 74, thinking 84, executing 92, muted 46
    bpm) and still eased; the ambient orb beats the same way;
  * `"heartbeat": false` in config/api_keys.json still turns it all off.

## Verification
* Genuine execution, numpy (`_to_delete/vein_test.py`, against the ui_holo.py
  from before this change): **19/19 PASS** — with no beat every frame is
  identical to before (both core styles, compact too); the wave starts in the
  brain, reaches the rim by 0.38 s, the dub leaves 0.30 of a beat later, it is
  quiet before the next beat, no jump at the beat boundary at any tempo
  46–100 bpm (max 5e-5); the brightest vein ring moves centre → rim at
  0.10 / 0.18 / 0.26 / 0.34 s; rim hairs x1.47 when it arrives; outside the
  sphere < 1 % change.
* Genuine execution, Qt on your PC (`tools/ui_render_check.py`, the real
  JARVIS window offscreen): **46/46 PASS**. The painted core, measured as
  light: brain x1.30 on the beat; vein rings centre → rim x1.29 → x1.33 →
  x1.64 → x2.21 at 0.10 / 0.18 / 0.26 / 0.34 s; outside the sphere 1.000
  (nothing beats there); veins back to 1.000 between beats; ambient orb brain
  x1.37, veins x1.83. The first run of these new checks failed: it read the
  layer's un-premultiplied colour (a faint line still reads as full colour);
  reading light instead fixed the measurement and also showed the wave was too
  faint against the nebula glow — which is why the carrying veins now glow.
* Cost (`tools/ui_perf_compare.py`, 3 rounds; the PC was busier than
  yesterday): pre-Phase-8 UI ~64 % of one core, now ~75 % (the ring-wave
  version measured +17 points over the same baseline yesterday) — the vein
  beat costs no more than the old waves.
* Live: JARVIS started from JARVIS_debug_run.bat; frames caught the brain
  flaring, the wave half-way through the veins and arriving at the rim
  (`Claude outputs/jarvis_heartbeat_in_the_veins.png`); 0 errors in the log.
* Structural: every edited file compiles (Python 3.13), CRLF only.

## Left out / limits
* The wave travels on screen from the centre to the rim, not along each
  filament's own path: the filaments are separate threads, not one connected
  network, so "along the vein" has no single direction. The picture is the
  same — lit veins spreading out from the brain.
* The resting look is unchanged: the veins are dim at rest and light up as the
  beat runs through them.
* The particle core style (`"holo_style": "particles"`) gets the brain flash
  and the wave through its particles, but has no veins to glow.
* Rollback: `JARVIS_pre_vein_heartbeat_backup.zip` (ui_holo.py,
  ui_command_center.py, tools/ui_render_check.py with the ring waves).
