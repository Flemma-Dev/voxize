# Architecture Decision: A Headless Backend, Driven Over Stdio

**Date**: 2026-11-04
**Status**: Decided (stage 1 implemented: backend plus GTK client; stage 2, a Textual TUI, is next)
**Supersedes**: point 3 ("Don't over-engineer the separation") of `docs/architecture-decision-layer-shell-and-dual-frontend.md`
**Referenced from**: `docs/journal.md`, 2026-11-04

## Context

The author works mostly in tmux and Claude Code. A GTK window in the middle of the screen means leaving the terminal, waiting for the transcript, then copying it back by hand. A terminal-native frontend is the natural next step, and a GNOME Shell extension is still possible.

The March decision deferred a JSON Lines protocol on purpose: keep GTK out of the backend modules, wrap the state machine's callbacks when a second frontend shows up. That held while there was one frontend. It stopped holding once the second one had a concrete shape: `app.py` mixed orchestration with GTK, and `ui.py` owned domain logic (the pipeline progress model, stall detection, which text stands after a failure) that a TUI would have to copy.

## Decision

Voxize is a headless backend process, `python -m voxize.backend`, that frontends spawn and drive over stdio with JSON Lines (`voxize.protocol`, `PROTOCOL_VERSION = 1`). One backend process per dictation; no daemon.

The GTK window becomes a subprocess client of that backend. It dogfoods the protocol instead of reaching around it, so any gap in the protocol shows up as a missing feature in the window. The Textual TUI is the second client and the proof that the backend stands alone.

The backend runs on a plain `GLib.MainLoop`. It never imports Gtk, Gdk, Adw, `voxize.gui` or `voxize.tui`. Frontends import only `voxize.protocol`, `voxize.config`, `voxize._trace`, `voxize.clipboard` and `voxize.mode_switcher`. Tests enforce both directions (`tests/test_import_rules.py`).

## The dividing rule

**The backend owns domain facts and the estimates derived from them. Frontends own everything about how those are drawn, worded, or interacted with.** The backend knows nothing about windows, terminals, focus, or which frontend is talking to it; it receives a `cwd` and glossary file paths, nothing else.

| Concern | Owner | Notes |
|---|---|---|
| Session state machine | backend | `state.py`, unchanged |
| Pipeline progress model (predictions, segments, TTFB-wait interpolation, monotonic ratchet, early-upload catch-up, `pipeline_progress:` debug lines) | backend | moved from `ui.py` to `backend/presenter.py` |
| Stall detection (no progress for 3 s) | backend | sent as a boolean |
| Transcript stage handoffs (live, batch, cleaned) | backend | sent as stage-tagged text |
| Failure fallback (which text stands when batch or cleanup fails) | backend | |
| Live-preview-lost fact | backend | the notice wording is frontend |
| Mic level (dBFS) | backend | |
| Costs | backend | numbers only |
| Whether a session had content (for trash-on-switch) | backend | was `ui._had_first_text`, read by `app.py` |
| Stop/cancel validity per state | backend | stale commands are ignored, not errors |
| Status labels, tones, all message strings (placeholder, "No speech detected.", notices, "Still working…") | frontend | |
| Which actions are offered per state, which one Enter triggers | frontend | derived from `state` |
| Cancel confirmation ("Discard this dictation?" from RECORDING on) | frontend | the backend accepts `cancel` in any in-flight state |
| Auto-close countdown (focus-gated) | frontend | |
| Recording timer display | frontend | ticks from entering RECORDING, as before |
| dBFS to bar fraction, quiet/hot/clip zones | frontend | stateless mapping |
| Text-area height ratchet, 6/10 line caps, popovers, toasts, pulse animation | frontend | |
| Clipboard mechanics (Gdk, focus-deferred write, `wl-copy` salvage) | frontend | the backend only says what to deliver (`result` events) |
| Context detection (focused window, cwd, glossary paths) | frontend | a skill a frontend may or may not have |
| Spawning the meeting recorder (Ctrl+M) | frontend | |

## Consequences

- **Two processes per dictation.** The GUI spawns the backend before it imports GTK, so the backend's sounddevice preload and the GUI's GTK import run in parallel. Stdin EOF without `close` counts as `close {trash_if_empty: false}`, so a dead frontend never leaves an orphan holding the mic or apps ducked. A dead backend shows as an ERROR card ("Backend exited unexpectedly", plus the last stderr line when there is one).
- **Startup check failures now appear in the ERROR card.** A missing API key or tool used to print to stderr and exit before any window, invisible when launched from a hotkey. Now the backend reports `state ERROR` with the joined messages. Keeping exact parity would have meant holding the window until the backend's checks finished, which puts backend startup on the time-to-window path. This is the one deliberate visible change.
- **GUI diagnostics live in `gui.log`.** Focus and Enter routing (added 2026-11-04 for the unreproduced "Enter does nothing at READY" report) are GUI facts, so they are no longer in `debug.log`. `debug.log` and every `*_events.jsonl` stay backend-side and unchanged; `pipeline_progress:` lines keep their exact text but come from logger `voxize.backend.presenter`.
- **`protocol.jsonl` is new.** Every message in and out, timestamped, in the session directory. Messages before `session` are buffered and written once the directory exists.
- **Ctrl+M trash rule changed.** "Trash the session if no text was ever produced" now uses a sticky `had_content` in the presenter. The old `_had_first_text` flag reset on TRANSCRIBING, so it did not mean "ever had content". A frontend sends `close {trash_if_empty: true}`; the backend decides.
- **Backend stdout is private.** The backend `dup()`s fd 1 for protocol output and points fd 1 at stderr, so a stray `print` cannot corrupt the stream. Backend stderr is piped to the frontend and written to its log, never `/dev/null`.
- **`clipboard.py` stays at `voxize/clipboard.py`.** The design had it moving into `gui/`, but the meeting recorder imports it. `voxize._trace` and `voxize.mode_switcher` are likewise allowed frontend imports.
- **Tests exist now.** `uv run pytest`: presenter unit tests on a fake clock, contract tests that spawn the backend in mock mode for every `VOXIZE_MOCK_OUTCOME`, and the import-rule tests. `VOXIZE_MOCK_FAST=1` is a new knob that shrinks the fixed mock delays so those tests are quick.
- **Review found three lifecycle bugs, all fixed before merge.** A malformed command used to drop the backend's stdin watch, which left an orphan holding the mic after the frontend exited. A GUI handler exception used to drop the GUI's backend watch. The GUI's own ERROR (protocol mismatch, no `hello`) was overwritten by later backend events; terminal states are now final on the GUI side.

### GUI parity

Screenshots of every dictation state, built from the old tree and the new one through the mock providers: 22 of 24 pixel-identical. The two differences, `state-07b-working-cleanup` and `state-09-working-no-prediction`, are timing in the old harness: the old build's own TTFB-wait animation pushed the bar 3 px past the value the script set (the presenter owns that animation now), and the pulse block depends on how many 100 ms ticks ran before the capture. Old against old was unstable on the same two frames; new against new was identical across runs.

### Startup timing

**Not yet measured.** Two hypotheses, neither assumed:

1. The window appears no later than before. The GUI spawns the backend first and does not wait on it.
2. The mic may open earlier. The sounddevice import (~2 s via numpy) no longer shares a GIL with the GTK import.

How: old build and new build, three real sessions each, `VOXIZE_TRACE` for time-to-window and time-to-mic, reporting medians of three. Results will be added here and in the journal.

## Rejected alternatives

- **In-process GUI on the same event stream.** The GUI would consume the same events without a process boundary, but nothing would stop it from reaching around the stream into session internals. A subprocess makes side channels impossible, which is the point of dogfooding.
- **A shared Python presenter library.** Put the progress model and text stages in a module both frontends import. That works only for Python frontends, and a GNOME Shell extension (JavaScript) would reimplement it. The protocol carries the result of the logic instead.
- **A `--target` flag.** `--target gui|tui` to shape output per frontend would teach the backend about frontends, the one thing the dividing rule forbids.
- **A long-running daemon.** Out of scope. One process per dictation keeps lifecycle simple (mic lock, ducking, teardown) and matches how it is launched, from a hotkey. D-Bus would only make sense for a daemon, as the March decision already noted.
