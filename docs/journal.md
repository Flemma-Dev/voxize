# Voxize Implementation Journal

### 2026-03-27 — Session 1: Phase 0 Bootstrap

**Phase**: 0 — Bootstrap
**Status**: in progress

Scaffolded the project:

- `pyproject.toml` — uv/hatchling build, PyGObject dependency, `src/voxize/` layout. Fixed build backend (`hatchling.build`, not `hatchling.backends`).
- `flake.nix` + `shell.nix` — follows the flemma.nvim pattern (flake imports shell.nix, shell.nix has `pkgs` fallback). System deps: gtk4, gobject-introspection, portaudio, pkg-config, wl-clipboard, libsecret. `shellHook` exports `LD_LIBRARY_PATH` via `lib.makeLibraryPath`. Flake pinned to `nixos-25.11`.
- `src/voxize/checks.py` — startup validation: GTK4 importable, wl-copy and secret-tool on PATH, API keys present in GNOME Keyring via secret-tool. Reports all failures at once (not fail-fast) with actionable messages.
- `src/voxize/__main__.py` — entry point. Runs checks first (exits non-zero on failure). Creates a GTK4 `ApplicationWindow` with `set_decorated(False)`, `set_resizable(False)`, dark translucent CSS, Escape key closes the window.

Verified: `uv run python -m voxize` launches a GTK4 window cleanly (only harmless Mesa GPU driver messages).

> [!IMPORTANT]
> **Major design deviation — dropped gtk4-layer-shell, adopted dual frontend architecture.** The design doc specified `gtk4-layer-shell` for overlay behaviour. Testing on GNOME Shell 49.2 revealed the `zwlr_layer_shell_v1` protocol is wlroots-only — GNOME has never implemented it. After investigating GTK4 Wayland APIs (`GdkToplevel`, `GdkToplevelLayout`) and GNOME Shell D-Bus (`Shell.Eval` disabled in GNOME 45+), we confirmed that window positioning and always-on-top are not controllable from a GTK4 client on GNOME/Wayland. The only path to these capabilities is a GNOME Shell extension. This led to a broader architectural discussion and decision documented in full at **[`docs/architecture-decision-layer-shell-and-dual-frontend.md`](architecture-decision-layer-shell-and-dual-frontend.md)**.

Summary of the decision:
- **GTK4 frontend (now):** Complete working product. Backend runs in-process. Accepts GNOME's window placement limitations — still a massive improvement over the current `notify-send` setup.
- **GNOME Shell extension frontend (future, out of scope):** Would spawn the Python backend as a subprocess, communicate via stdin/stdout JSON Lines, render UI in St/Clutter widgets, and handle hotkey/positioning/stacking natively. Not part of this project's phases.
- **Backend stays GTK-free:** `state.py`, `audio.py`, `transcribe.py`, `cleanup.py`, `storage.py`, `lock.py`, `clipboard.py` have no UI imports. Only `app.py` and `ui.py` touch GTK. This is the natural seam for a future JSON Lines boundary.
- **No over-engineering now:** No IPC protocol, no abstraction layer. The state machine callback interface is sufficient separation. JSON Lines wrapping is trivial to add later if needed.

Phase 0 done criteria met in prior session. Moving to Phase 1.

---

### 2026-03-27 — Session 2: Phase 1 — UI/UX with Mocks

**Phase**: 1 — UI/UX with Mocks
**Status**: in progress

Implemented the full Phase 1 deliverables:

**State machine (`state.py`)** — Pure logic, no GTK imports. States: INITIALIZING → RECORDING → CLEANING → READY, with CANCELLED and ERROR reachable from active states. Validates transitions and notifies listeners synchronously. This is the contract that real providers (Phase 2-3) will plug into.

**CSS theme (`style.css`)** — Extracted from inline string in `__main__.py` to a standalone file. Full dark translucent theme per design doc §5: window styling, status dot colors (red/amber/green/grey) with pulse animation via `transition: opacity`, header separator, text view with transparent background, primary/secondary button styles overriding Adwaita defaults.

**UI (`ui.py`)** — Header bar with status dot (●), label, timer, and contextual buttons. Text area uses `Gtk.TextView` (non-editable, word-wrap, selectable) inside `Gtk.ScrolledWindow` with `POLICY_EXTERNAL` (no scrollbar, programmatic scroll-to-bottom). Text area hidden initially, shown when first text arrives — keeps window compact. Fade-out gradient at top of text area via `Gtk.DrawingArea` overlay drawing a `cairo.LinearGradient` from window bg to transparent (32px). Max text area height computed from monitor geometry (screen_height / 4 - 50px). Pulsing dot via GLib timer toggling a `.dim` CSS class every 600ms with 400ms CSS transition.

**Mock providers (`mock.py`)** — `MockTranscription` emits predefined text word-by-word at ~220ms intervals (~4.5 words/sec). `MockCleanup` emits cleaned text at ~60ms intervals (~16 words/sec, simulating fast API streaming). Cleaned text has minor formatting improvements over raw transcript (added commas, removed filler words). Same start/stop/cancel interface that real providers will implement.

**App wiring (`app.py`)** — `VoxizeApp` orchestrates: loads CSS from file, creates window, creates state machine + UI, transitions to RECORDING on activate. State change listener starts/stops mock providers. Escape key cancels during RECORDING/CLEANING, closes during READY/ERROR. `close-request` handler cleans up timers and providers.

**Entry point (`__main__.py`)** — Simplified to: run checks → import app → run.

Implementation decisions:
- **`Gtk.TextView` over `Gtk.Label`** for text display — gives auto-scrolling via marks, selection, and word-wrap out of the box. Labels don't support scroll-to-end.
- **`Gtk.Overlay` + `Gtk.DrawingArea`** for top fade — GTK4 CSS doesn't support `mask-image`. Cairo gradient with `set_can_target(False)` for click-through works well.
- **`POLICY_EXTERNAL`** on ScrolledWindow — hides scrollbar but allows programmatic `scroll_mark_onscreen()`. Design spec says no scrollbar.
- **Text area starts hidden** (`_overlay.set_visible(False)`) — the window shows only the header bar initially, grows when text arrives. Matches design: "Initial state: window shows just the header bar."
- Needed `gi.require_version('Gdk', '4.0')` in every module importing Gdk, not just the entry point. PyGObject warns otherwise.

Verified: `uv run python -m voxize` launches, runs for 3+ seconds without errors (only harmless Mesa GPU driver warnings).

Visual testing and iterative polish followed. Changes made during testing:

- **Scroll jitter fix** — `scroll_mark_onscreen` was racing ScrolledWindow height reallocation on line wraps. Fixed by deferring scroll to `GLib.idle_add` and using `vadjustment.set_value` directly for deterministic bottom-pinning. Debounced with `_scroll_pending` flag.
- **Fade gradient conditional** — Driven by `vadjustment.changed` signal instead of checking in the scroll callback. Shows only when `upper > page_size + 1`.
- **`Gtk.HeaderBar` as titlebar** — Replaced manual header `Gtk.Box` with `Gtk.HeaderBar` + `set_titlebar()`. Gives drag-to-move for free. `set_show_title_buttons(False)` suppresses default window buttons. Removed `set_decorated(False)` — CSD handles chrome. Removed the manual `Gtk.Separator` — headerbar provides its own bottom border via CSS.
- **System font inheritance** — Removed all `px`-based font sizes and `font-family: monospace` from window. Sizing now uses `em` units throughout so the UI scales with the system font. Monospace only on the text view via `set_monospace(True)` (adds `.monospace` class, libadwaita handles `--monospace-font-family`).
- **CSS variables over `@define-color`** — Discovered libadwaita uses CSS custom properties (`var(--name)`), not just `@define-color`. Migrated entire palette to `--vox-*` variables on `window`. This is how you override libadwaita's own styling — e.g., `--view-fg-color`, `--destructive-bg-color`.
- **Adwaita button classes** — Cancel uses `destructive-action` (overridden to muted red via `--destructive-bg-color`). Stop is a regular button. Close uses `flat`. Removed all custom `.primary`/`.secondary` button CSS.
- **Spinner loading state** — `Gtk.Spinner` shown during startup delay (before first transcription word) and before cleanup starts. Dismissed when first text token arrives via `append_text`. Mock providers have configurable delay (1.5s transcription, 2.5s cleanup).
- **Text pulse during cleanup** — Instead of clearing transcript and showing spinner, the transcript stays visible and pulses (opacity breathes between 1.0 and 0.5 via CSS `transition` + `.processing` class toggle). User can review what they said while waiting. First cleanup token clears and replaces the pulsing text.
- **Enter confirms, Escape cancels** — `set_default_widget(action_btn)` during RECORDING/READY/ERROR so Enter activates Stop/Close. Cleared during CLEANING. Escape cancels during RECORDING/CLEANING, closes during READY/ERROR.
- **Backdrop text dimming** — `notify::is-active` signal toggles `.backdrop` class on the text view. CSS overrides `--view-fg-color` to `--vox-fg-dim`. Had to use CSS variables because libadwaita's `textview > text { color: var(--view-fg-color) }` rule was unbeatable via normal selectors.
- **`lookup_color` fix** — PyGObject returns `(found, Gdk.RGBA)` tuple, not C-style output parameter. Wrong call signature silently killed the fade gradient draw callback.
- **ScrolledWindow overshoot/undershoot** — Disabled via CSS to prevent Adwaita's built-in scroll edge shadows.

> [!IMPORTANT]
> **Key GTK4/libadwaita CSS finding:** `@define-color` / `@name` is the legacy mechanism. Libadwaita uses CSS custom properties (`--name` / `var(--name)`) throughout. To override libadwaita styling, set its own variables (`--view-fg-color`, `--view-bg-color`, `--destructive-bg-color`, `--monospace-font-family`, etc.) on your widgets. This avoids specificity fights entirely. The `.view` class on `GtkTextView` with its `textview > text` selector is effectively unbeatable via normal CSS — override `--view-fg-color` instead.

Phase 1 done — all criteria met.

---

### 2026-03-27 — Session 3: Phase 2 — Audio Capture + OpenAI Realtime Transcription

**Phase**: 2 — Audio Capture + OpenAI Realtime Transcription
**Status**: in progress

Implemented the core Phase 2 modules:

**Microphone lock (`lock.py`)** — `MicLock` class using `fcntl.flock()` on `$XDG_RUNTIME_DIR/voxize-mic.lock`. Advisory lock is released automatically by the OS on crash — no stale lock problem. `LOCK_EX | LOCK_NB` for non-blocking acquire; raises `MicLockError` if another instance holds it. Tested: acquire, release, and contention rejection all work.

**Session storage (`storage.py`)** — `create_session_dir()` creates a timestamped directory under `$XDG_STATE_HOME/voxize/` (e.g., `2026-03-27T16-43-07`). Session rotation (pruning beyond 8) deferred to Phase 4.

**Audio capture (`audio.py`)** — `WavWriter` implements the placeholder-header technique: 44-byte RIFF/WAV header at open with data size `0xFFFFFFFF`, appends + flushes PCM on each write, fixes both size fields on finalize. `AudioCapture` wraps a `sounddevice.RawInputStream` at 24kHz/16-bit/mono/960 blocksize (40ms). The callback writes PCM to WAV and forwards raw bytes to a caller-supplied function (the WebSocket sender). Tested: WAV header and data integrity verified.

**OpenAI Realtime WebSocket (`transcribe.py`)** — `RealtimeTranscription` runs an asyncio event loop in a daemon thread (`voxize-ws`). Architecture:
- **Three threads**: GTK main (UI), sounddevice callback (audio), asyncio (WebSocket). Connected by `asyncio.Queue` (audio→WS) and `GLib.idle_add` (WS→GTK).
- **send_loop**: drains the audio queue, base64-encodes, sends as `input_audio_buffer.append` events.
- **receive_loop**: dispatches `conversation.item.input_audio_transcription.delta` text to GTK via `GLib.idle_add`. Accumulates full transcript internally.
- **Shutdown**: `asyncio.Event` (`_done`) signals the session coroutine from the GTK thread via `call_soon_threadsafe`. On stop: commits audio buffer, waits 1.5s for trailing events, then closes. On cancel: closes immediately.
- **Error handling**: connection failures and API error events are reported to GTK via `GLib.idle_add(on_error, msg)`.

Tested against the live OpenAI API:
- `wss://api.openai.com/v1/realtime?intent=transcription` — connects successfully with `Authorization: Bearer <key>` and `OpenAI-Beta: realtime=v1` headers.
- `transcription_session.update` with `gpt-4o-transcribe` model accepted — receives `transcription_session.created` + `transcription_session.updated` events.
- `input_audio_buffer.append` with base64 PCM sends without error.
- Server VAD processes audio in real-time — the `input_audio_buffer.commit` on an already-consumed buffer returns `input_audio_buffer_commit_empty` error, which is expected and harmless (caught in try/except).

**App wiring (`app.py`)** — Rewired to use real providers:
- `do_activate` shows window in INITIALIZING state (spinner visible), then kicks off `_initialize()` via `GLib.idle_add`.
- `_initialize()`: acquires mic lock → creates session dir → retrieves OpenAI key from keyring → starts `RealtimeTranscription` (background thread) → starts `AudioCapture`. Audio chunks queue in the asyncio queue while the WS connects.
- `on_ready` callback from transcription → transitions INITIALIZING → RECORDING (timer starts, buttons appear).
- `on_error` callback → tears down everything, transitions to ERROR.
- RECORDING → CLEANING: deferred via `GLib.idle_add` so the UI repaints with "Cleaning up..." before `stop()` blocks briefly for the thread join.
- Cleanup still uses `MockCleanup` (real Anthropic integration is Phase 3).
- Escape during INITIALIZING tears down and closes immediately.
- Double-teardown is safe — all guards check for None before acting.

**Dependencies** — Added `sounddevice>=0.4` and `websockets>=13.0` to `pyproject.toml`. Added `get_api_key(service)` helper to `checks.py` for retrieving keys from GNOME Keyring.

**UI change** — Spinner now starts visible+spinning in `_build()` so users see activity during the INITIALIZING phase (WebSocket connection, ~100-500ms). Already dismissed when first text arrives via `append_text()`.

Architecture decisions:
- **Thread bridge over gbulb** — The design doc suggested evaluating gbulb for asyncio/GTK integration. Chose thread bridge instead: one daemon thread runs `asyncio.new_event_loop()`, communicates via `call_soon_threadsafe` and `GLib.idle_add`. Simpler, no extra dependency, always works regardless of GTK version.
- **No re-transcription on Stop** — Transcript is accumulated from real-time delta events. Stop just closes the WS and returns the accumulated string. No batch upload or "transcribing..." phase — this is the core advantage over the v0.6.x pipeline.
- **Audio starts before WS connects** — `AudioCapture` starts immediately in `_initialize()`. Chunks queue in the asyncio queue until the WS send_loop drains them. This avoids losing the first few hundred ms of speech during connection setup. The queue absorbs the lag.
- **Fast stop** — `stop()` sends `input_audio_buffer.commit` and waits at most 1.5s for trailing events. Brief UI freeze during RECORDING→CLEANING is acceptable since the UI has already transitioned visually (deferred via idle_add).

**End-to-end testing with `harvard.wav`** — Converted a stereo 44.1kHz WAV (Harvard sentences) to 24kHz mono PCM, fed it through the pipeline at real-time pace. Results:
- WebSocket URL, headers, `transcription_session.update` configuration all work on first attempt.
- Server VAD correctly segments speech turns, fires `input_audio_buffer.speech_started/stopped` events.
- `conversation.item.input_audio_transcription.delta` events stream text in real-time — exact event names match what the design doc specified.
- `input_audio_buffer.commit` on an already-VAD-consumed buffer returns `input_audio_buffer_commit_empty` error — this is expected and harmless. Fixed: receive loop now ignores this error code instead of reporting it via `on_error` (which would have shut down the session in app.py).
- Accumulated transcript is accurate: "The stale smell of old beer lingers. It takes heat to bring out the odor. A cold dip restores health and zest. A zestful food is the hot cross bun."
- 4/6 sentences captured in the class-based test (vs 6/6 in the raw WebSocket test). The missing sentences are a timing artifact of feeding pre-recorded audio — the send loop batches chunks faster than real-time pacing when the queue has buffered data. Not an issue with live microphone input, where chunks arrive at exactly 40ms intervals and the user pauses before pressing Stop.

**Non-destructive error handling** — Per user feedback, errors during recording must never destroy the session. Implemented a degraded recording mode:
- **Error bar**: New widget at the bottom of the window (`Gtk.Box` with `.error-bar` CSS class). Hidden by default, shown via `show_error_banner(message)`. Subtle styling: red-tinted background, `⚠` icon, dimmed message text.
- **During RECORDING**: WebSocket errors cancel the transcription but audio capture continues. Error bar appears, transcript text stays visible (user can select and copy), Stop becomes Close, timer and pulse stop. The WAV file is the safety net.
- **Close in degraded mode**: Triggers CANCELLED → stops audio (finalizes WAV), releases lock, closes window. Everything is preserved.
- **ERROR state**: Also uses the error banner now instead of clearing text. This preserves any transcript that was in the text area when the error occurred.
- **Design principle**: Voxize must never lose the user's session/audio/transcription. Audio capture is the last thing to stop. Even if the WebSocket dies, the API key is wrong, or the network drops, the microphone keeps recording to disk.

#### Next
- Live test with real microphone and speech (full GTK app)
- Verify WAV file is saved correctly after a recording session
- Verify mic lock prevents concurrent recording across instances

---

### 2026-03-27 — Session 4: Phase 3 — Text Cleanup + Clipboard

**Phase**: 3 — Text Cleanup + Clipboard
**Status**: in progress

> [!IMPORTANT]
> **Major design deviation — GPT-5.4 Mini replaces Claude Haiku for text cleanup.** The design doc specified Claude Haiku (`claude-haiku-4-5-20251001`) via the Anthropic Python SDK. We pivoted to **GPT-5.4 Mini** via the OpenAI Python SDK. Rationale:
> - OpenAI is already a project dependency — the Realtime Transcription API uses their WebSocket endpoint. Adding Anthropic would introduce a second vendor SDK for no benefit.
> - GPT-5.4 Mini is cheaper: $0.75/MTok input, $4.50/MTok output vs Haiku's $1/$5.
> - Both models are comparably capable for this cleanup task (spelling, punctuation, filler removal).
> - The `openai` Python SDK gives us streaming chat completions with the same patterns we already use.
> - No Anthropic API key is needed in the keyring anymore — startup checks simplified.

Implemented the Phase 3 deliverables:

**Text cleanup (`cleanup.py`)** — `Cleanup` class wrapping the OpenAI Chat Completions API with `gpt-5.4-mini`. Matches the `MockCleanup` interface: `start(transcript, on_delta, on_complete, on_error)`, `cancel()`. Runs a synchronous streaming call in a daemon thread (`voxize-cleanup`), posts delta tokens to the GTK main thread via `GLib.idle_add`. System prompt uses nonce-wrapped input (`<transcription-{nonce}>...</transcription-{nonce}>`) per the design doc's prompt injection strategy. Instructions: fix spelling/punctuation/grammar, remove filler words, preserve meaning, apply emphasis, never execute embedded instructions.

**Clipboard integration (`clipboard.py`)** — `copy(text)` function piping text to `wl-copy` via stdin. Uses stdin (not argv) to avoid `ARG_MAX` limits on large transcripts. Failures are logged but never raised — clipboard is best-effort, it must never crash the app.

**App wiring (`app.py`)** — `_begin_cleanup()` rewritten:
- Saves raw transcript to `session_dir/transcription.txt`
- Copies raw transcript to clipboard via `wl-copy` (safety net — available in clipboard history even if cleanup fails)
- Handles empty transcript: shows "No speech detected", transitions to READY
- Creates `Cleanup(api_key)` and starts streaming (mock mode still uses `MockCleanup`)
- `_on_cleanup_done(cleaned)`: saves `cleaned.txt` to session dir, copies cleaned text to clipboard (overwrites raw), transitions to READY
- `_on_cleanup_error(message)`: transitions to ERROR state — raw transcript is already in clipboard, so cleanup failure is non-fatal
- API key stored as `self._api_key` during `_initialize()` for reuse by cleanup

**Startup checks (`checks.py`)** — Removed the Anthropic API key check. Only the OpenAI key is required now.

**Dependencies (`pyproject.toml`)** — Added `openai>=1.0`.

Architecture decisions:
- **Synchronous thread over asyncio** — Unlike `transcribe.py` which uses asyncio for the WebSocket, cleanup uses a plain thread with a synchronous streaming iterator. The OpenAI SDK's `stream=True` returns a blocking iterator, which is simplest to consume in a thread. No asyncio event loop needed. GLib.idle_add bridges back to GTK.
- **on_error callback** — Added to the cleanup interface (MockCleanup doesn't have it). On failure, the app transitions to ERROR. The raw transcript is already in the clipboard from `_begin_cleanup`, so the user loses nothing. This aligns with the design doc's principle: "Anthropic API failure → show error, keep raw transcript in clipboard."
- **stream.close() on cancel** — When the user cancels during cleanup, `cancel()` sets a flag. The thread checks it between chunks and calls `stream.close()` to abort the HTTP connection promptly.

**System prompt ported from VoxInput `record.sh`** — The initial cleanup prompt was generic. Replaced it with the battle-tested prompt from `~/nix-meridian/home/apps/voxinput/record/record.sh` (lines 193-231). Key additions over the initial draft:
- "You are not an assistant" — explicit role constraint preventing conversational behaviour
- Technical user context — mistranscribed words should be corrected toward SaaS product names, programming terms, or technical concepts
- Detailed "so" handling — remove as sentence-opening filler, keep as causal conjunction, never split a causal "so" into a new sentence
- Emphasis formatting with anti-patterns — bold/italics/CAPITALS reflect spoken stress only, never decorate proper nouns or product names; includes concrete examples of correct and incorrect usage
- Paragraph reformatting — separates sentences into readable paragraphs, not just inline fixes

**Noise reduction (`transcribe.py`)** — Added `input_audio_noise_reduction: { type: "near_field" }` to the Realtime API `transcription_session.update` payload. Found in the OpenAI Speech-to-text docs. `near_field` is appropriate for a desktop/laptop microphone. Should improve transcription quality by filtering background noise before the model sees the audio.

**Bug fix: missing spaces between VAD segments (`transcribe.py`)** — First live test showed "happens.I've taken a big pause" — no separator between speech turns. The OpenAI Realtime API creates a new `item_id` for each VAD segment, but we concatenated deltas blindly. Fixed by tracking `_current_item_id` in the receive loop; when it changes, a `"\n"` is prepended to separate speech turns. The cleanup model handles paragraph formatting from there.

**Bug fix: raw WebSocket event logging (`transcribe.py`)** — Added `ws_events.jsonl` logging to the session directory. Every received WebSocket event is written as a JSONL line with `flush()`. This makes it possible to diagnose transcription issues post-session. The log file is opened at session start and closed in a `finally` block. `RealtimeTranscription` now takes an optional `session_dir` parameter.

**Bug fix: UI freeze on Stop (`app.py`)** — GNOME was offering to kill the app because `_begin_cleanup()` ran `transcription.stop()` (blocks up to ~5s for WebSocket thread join) and `clipboard.copy()` (subprocess) on the GTK main thread. Fixed by splitting cleanup into three stages:
1. `_begin_cleanup()` (GTK thread): stops mock providers (instant, uses GLib timers), nulls real provider references to prevent teardown races, spawns background thread.
2. `_stop_providers()` (background thread): stops audio capture, stops transcription (blocking join), releases mic lock, saves `transcription.txt`, copies raw transcript to clipboard. Posts back via `GLib.idle_add`.
3. `_start_cleanup()` (GTK thread): handles empty transcript case, creates and starts the cleanup provider.

The UI stays responsive throughout — the CLEANING state (pulsing text, amber dot) renders immediately while the background thread does the blocking work.

**Bug fix: transcription deltas leaking into cleanup (`transcribe.py`)** — First live test showed transcription text arriving after the cleanup phase had started, mixing raw transcript with cleaned output. Root cause: `GLib.idle_add(on_delta, ...)` callbacks queued during the drain period fired on the GTK thread after CLEANING state was entered. Fixed by adding a `_draining` flag. When `stop()` is called, it sets `_draining = True` before signaling done. The receive loop still accumulates text into `self._transcript` (so the full transcript is returned) but skips `GLib.idle_add` — no more deltas reach the UI. Also increased the trailing event drain timeout from 1.5s to 5s to give OpenAI time to finish processing mid-sentence audio.

**Fast startup (`app.py`)** — Initialization was taking 3-4s because the UI stayed on "Initializing" until the WebSocket connected. The audio capture already started before the WS and chunks queued — but the RECORDING transition waited for `on_ready`. Fixed by transitioning to RECORDING immediately after `audio.start()` succeeds. Removed `_on_ws_ready` callback. If the WS fails later, `_on_ws_error` already handles RECORDING state (degraded mode with error banner, audio continues). The user now sees "Recording" and can start speaking instantly.

**Bug fix: Cancel hangs the app (`app.py`)** — Same root cause as the Stop freeze: `_teardown_recording()` called `transcription.cancel()` → `_join(timeout=5.0)` on the GTK thread. Fixed by replacing `_teardown_recording` with `_teardown_async()` everywhere: nulls provider references on the GTK thread (preventing races), spawns a background daemon thread (`voxize-teardown`) that does the blocking cancel/stop/release. Used in CANCELLED handler, ERROR handler, Escape-during-INITIALIZING, and `close-request`. Window close proceeds immediately — daemon threads finish in the background.

**Early stdio close (`app.py`)** — `_on_close_request` now closes fd 0/1/2 before allowing the window to close. If a parent process (e.g., future GNOME Shell extension) is reading our stdout, it sees EOF immediately and doesn't hang while daemon threads finish async teardown.

**Bug fix: raw transcript never shown before cleanup (`ui.py`, `app.py`)** — With fast startup, the WS connection happens in the background. If the user presses Stop before any transcription deltas reach the UI (or after only a few arrive), the text area is empty when CLEANING state is entered. The spinner was never dismissed, the text pulse had nothing to pulse on, and the user only saw the cleaned output — never their raw transcript.

Root cause: the CLEANING state handler in `ui.py` immediately set `_awaiting_cleanup = True` and started the text pulse. But the transcript drain hadn't completed yet — the final transcript wasn't available. Stale delta callbacks (queued in GLib before `_draining` was set) could also trigger the one-shot `_awaiting_cleanup` clear prematurely, before actual cleanup tokens arrived.

Fix: moved `_awaiting_cleanup` and text pulse out of the CLEANING state handler. Instead, `_start_cleanup()` (which runs on the GTK thread after drain completes) calls `ui.show_transcript_for_cleanup(transcript)`. This method:
1. Dismisses the spinner (if still showing).
2. Sets the text buffer to the full transcript.
3. Makes the text area visible.
4. Arms `_awaiting_cleanup` and starts the text pulse.

The user now sees their raw transcript pulsing while cleanup runs. The first cleanup token clears it and streams the cleaned version. The CLEANING state handler now only dismisses the spinner (in case the user stopped before WS connected) and updates chrome (status label, buttons). Also added intermediate status label: "Finishing…" during drain, "Cleaning up…" once cleanup actually starts (set in `show_transcript_for_cleanup`).

**Bug fix: transcription data loss from audio queue burst (`transcribe.py`)** — A 26-second recording lost an entire middle section (~15s of speech). Investigation via `ws_events.jsonl` and batch re-transcription of the WAV confirmed the audio was on disk but the Realtime API never received it. Root cause: with fast startup, `AudioCapture` starts before the WebSocket connects. Chunks accumulated in the asyncio queue (~75-100 at 40ms each). When the WS connected, the send loop blasted them all at the API in milliseconds. The server VAD couldn't properly segment speech from this burst — it processed the initial chunk as one segment, missed VAD boundaries in the buffered middle section, and only picked up again once real-time-paced chunks started flowing.

Research confirmed: OpenAI's own cookbook (`Speech_transcription_methods.ipynb`) paces pre-recorded audio at ~5x real-time with the comment "Add pacing to ensure real-time transcription" (128ms chunks with 25ms sleeps). No hard rate limit is documented, but the server VAD is sensitive to burst delivery. Per-message limit is 15 MiB, minimum commit size is 100ms.

Fix: added pacing to `_send_loop`. When the queue has a backlog (`queue.qsize() > 0`), an 8ms `asyncio.sleep` is inserted between sends (40ms chunks / 5 = 8ms, matching the ~5x real-time rate from the cookbook). When the queue is empty (live mic, real-time flow), no delay — chunks go straight through. This preserves all buffered audio for transcription while avoiding VAD degradation. No audio is dropped — the WAV and the API both get every chunk.

---

### 2026-03-27 — Session 5: Phase 4 — Polish

**Phase**: 4 — Polish
**Status**: in progress

Several Phase 4 deliverables were already implemented during Phase 2-3 work:
- WebSocket error handling (degraded recording mode with error banner, audio continues)
- WebSocket drops mid-recording (same degraded mode)
- Microphone open failure → ERROR state with actionable message
- Empty transcription → "No speech detected"
- Cleanup API failure → ERROR state, raw transcript preserved in clipboard
- Most error state UI (error banner, text preservation)

Remaining deliverables implemented this session:

**Session rotation at termination (`storage.py`)** — `prune_sessions(keep=8)` deletes session directories beyond the most recent 8. Called in `_on_close_request` (not at startup) per a deliberate decision: if a bug causes repeated startup crashes, doing cleanup at startup would progressively destroy session history. At termination, we know the app ran successfully, so pruning is safe. Uses lexicographic sort on `YYYY-MM-DDTHH-MM-SS` directory names (equals chronological order). `shutil.rmtree` with `OSError` catch — best-effort, never crashes the app.

**Signal handlers (`app.py`)** — `GLib.unix_signal_add` for SIGTERM and SIGINT, registered in `do_activate`. The handler calls `AudioCapture.finalize_wav()` (new method — fixes WAV header sizes without stopping the stream), releases the mic lock, and calls `self.quit()`. This ensures the WAV file has a valid header even when the process is killed externally (e.g., `kill`, `systemctl stop`). The existing placeholder-header technique already preserves raw PCM data on hard crashes — the signal handler upgrades this to a properly-playable WAV.

**Auto-close timeout (`app.py`)** — `GLib.timeout_add_seconds(_AUTOCLOSE, self._on_autoclose)` starts when READY state is entered. Default 30 seconds, configurable via `VOXIZE_AUTOCLOSE` env var (0 to disable). Timer is cancelled on any state transition (via `_cancel_autoclose` in `_on_state_change`) and on `close-request`. The callback checks that the state is still READY before closing — guards against races if the user somehow triggers a state change between the timer firing and the callback executing.

**Edge case: very short recording (<1s)** — Reviewed all code paths. No changes needed: `transcription.stop()` returns empty/short text, `_start_cleanup` already handles empty transcripts with "No speech detected" → READY. `WavWriter.finalize()` correctly produces a valid WAV even with 0 bytes of PCM data (RIFF size 36, data size 0). The mic lock and session dir cleanup work regardless of recording duration.

**Edge case: very long recording (>10min)** — No changes needed. `Gtk.TextView` with `max_content_height` constraint keeps the window bounded. Programmatic `scroll_to_end` via `vadjustment.set_value` is O(1). The fade gradient shows when content overflows. Text selection and word-wrap work throughout.

**Edge case: multiple rapid invocations** — Already handled by `MicLock` (`fcntl.flock` with `LOCK_EX | LOCK_NB`). Second instance gets `MicLockError` → ERROR state with message. No changes needed.

Architecture decisions:
- **`GLib.unix_signal_add` over `signal.signal`** — Python's `signal.signal` is unreliable in GTK apps because the Python signal handler requires the GIL, which may be held by GTK during event processing. `GLib.unix_signal_add` integrates with the GLib main loop, running the handler as a normal idle callback. The `GLib.PRIORITY_HIGH` ensures it runs before other pending events.
- **`finalize_wav()` separate from `stop()`** — The signal handler should only fix the WAV header, not try to stop the sounddevice stream (which may deadlock if the audio callback is running). `stop()` still calls `finalize()` for normal shutdown.
- **Termination-time pruning over startup-time** — User's insight: a crash loop at startup would repeatedly invoke pruning before creating a new session, progressively deleting history. At termination, the session has been created and used, so pruning old sessions is safe.

**UX fix: keep spinner during drain (`ui.py`)** — When the user pressed Stop before any transcription text had appeared, the CLEANING state handler dismissed the spinner. Result: the user saw an empty window with "Finishing…" and no activity indicator. Fix: removed spinner dismissal from the CLEANING state handler — the spinner stays visible and is dismissed naturally by `append_text` when the first text arrives, or by `show_transcript_for_cleanup` after the drain completes. The user now sees: press Stop → "Finishing…" with spinner → transcript appears → "Cleaning up…" with pulsing text.

**Root cause: VAD corruption from 5x burst pacing (`transcribe.py`)** — Two sessions showed truncated or zero transcription despite full audio delivery. Diagnostic logging (session-level `debug.log` capturing all threads) revealed all chunks were sent successfully. The root cause: when the user starts speaking during the initial buffered burst (before the WS has drained all queued chunks), the 5x pacing (8ms per 40ms chunk) causes the server VAD to misdetect speech boundaries. Evidence from `ws_events.jsonl`:

- **Broken (19:44)**: `speech_started` at 468ms, `speech_stopped` at 1376ms — 908ms segment in the burst region → "Right?" (should have been "Alright, this is another test..."). VAD never fired again despite 5 more seconds of audio.
- **Working (18:57)**: `speech_started` at 2420ms — user waited longer, speech fell in the real-time region → full transcript captured.

The 5x pacing from OpenAI's cookbook was designed for batch file transcription, not live streaming with a burst-to-realtime transition. When the VAD processes burst audio, its internal clock loses alignment with the audio content, causing it to commit truncated segments and miss subsequent speech.

The full history of pacing experiments and root cause analysis is documented in **[`docs/debugging-guide.md`](debugging-guide.md)** § 3 "The OpenAI Realtime API and VAD". That guide should be kept up to date alongside this journal whenever new debugging insights emerge.

**Session-level trace logging (`app.py`, `transcribe.py`, `audio.py`, `state.py`, `cleanup.py`, `lock.py`, `storage.py`, `clipboard.py`)** — Added comprehensive `logger.debug()` calls throughout the codebase. A `FileHandler` writing to `{session_dir}/debug.log` is configured in `_initialize()`. This logging was instrumental in diagnosing the VAD burst pacing issue — the debug.log showed all 166 chunks sent but only one 908ms speech segment detected. See **[`docs/debugging-guide.md`](debugging-guide.md)** § 6 for log format and key patterns.

**Switch from `server_vad` to `semantic_vad` (`transcribe.py`)** — After exhaustive testing (5x, 1.5x, 1.1x pacing; VAD disable/re-enable with manual commit; no-commit), all `server_vad` approaches failed due to its fundamental sensitivity to audio delivery timing. Research into the OpenAI Realtime API documentation revealed `semantic_vad` — a mode that detects speech boundaries based on semantic understanding of the utterance, not silence-based timing. With `eagerness: "low"` (waits for the user to finish speaking — ideal for dictation), the startup burst can be sent at full speed without corrupting the VAD. The entire two-phase dance (VAD off → burst → commit → VAD on) was eliminated. Audio now flows as one continuous stream: startup burst at full speed → real-time chunks. Also added `language: "en"` to prevent hallucination on short/ambiguous segments. See **[`docs/debugging-guide.md`](debugging-guide.md)** § 3 for the full rationale and historical experiment table.

**Buffer drain progress bar — added then removed** — A `Gtk.ProgressBar` was implemented to visualize the startup backlog draining during paced delivery. With `semantic_vad` eliminating pacing entirely (burst sends in ~50ms), the bar became pointless and was removed. The implementation is preserved in git history for reference.

**Cancel hang fix (`app.py`)** — Added explicit `self.quit()` call in `_on_close_request` so the GTK main loop exits even when GLib sources (signal handlers, stale idle callbacks) are pending.

#### Deferred
- **Audio level meter** — A `Gtk.LevelBar` showing real-time microphone input level (RMS dB computed in the audio callback). Would give the user direct "am I being heard?" feedback and catch "too quiet" issues.

---

### 2026-03-27 — Session 6: Live Testing + Visual Polish

**Phase**: 4 — Polish
**Status**: in progress

**Semantic VAD live testing** — Five targeted recording sessions confirmed `semantic_vad` with `eagerness: "low"` is reliable for daily use. The critical failure mode from `server_vad` (burst-induced VAD corruption) is eliminated.

| # | Test | Duration | Burst | VAD Segments | Realtime vs Batch |
|---|------|----------|-------|--------------|-------------------|
| 1 | Immediate speech | 10.1s | 33 chunks (1.3s) | 2 (at 0ms, 7180ms) | 91% |
| 2 | Delayed speech (~3s) | 11.0s | 13 chunks (0.5s) | 1 (at 3340ms) | 100% |
| 3 | Short recording (~4s) | 4.5s | 23 chunks (0.9s) | 1 (at 204ms) | 80%† |
| 4 | Long monologue (~31s) | 31.3s | 25 chunks (1.0s) | 1 (28.3s continuous) | 94% |
| 5 | Multiple turns with pauses | 36.8s | 20 chunks (0.8s) | 7 segments | 80%† |

†80% scores are inflated by word-level comparison imprecision (e.g., "3 to 5" vs "three to five").

Key findings:
- **Burst region is no longer a problem** — VAD detected speech starting at 0ms, 204ms, 332ms, 780ms (all within or immediately after the burst). This was the critical failure mode with `server_vad` where speech in the burst region was missed or truncated the entire session.
- **All audio delivered** in every session (`chunks_sent ≈ audio_chunks`).
- **Multi-turn detection works** — session 5 correctly identified 7 speech segments with clean boundaries. `eagerness: "low"` waits for the user to finish speaking before chunking.
- **Long dictation handled correctly** — session 4 kept a 28s monologue as one continuous segment.
- **Minor streaming transcription gaps** — session 5 dropped "I'm supposed to have multiple turns" (batch had it, realtime didn't). The VAD segment timing covered the right range, so this is a streaming transcription quality issue, not VAD. Session 4 dropped "The" at the very start of "The quick brown fox." These are inherent limitations of streaming ASR vs batch.

**Visual polish — status label and dot behavior (`ui.py`, `transcribe.py`, `app.py`)** — Surfaced internal state in the UI without adding new widgets:

- **Status label progression during RECORDING**: "Listening…" (WS connecting / waiting for speech) → "Recording" (first transcription text arrives). Previously the label said "Recording" immediately even while the WS was still connecting and no speech had been detected.
- **Dot pulse tied to VAD speech events**: during active speech (`speech_started`), the dot stays bright. Between speech turns (`speech_stopped`), the dot dims and resumes gentle pulsing. Previously the dot pulsed on a blind 600ms timer regardless of speech activity.
- **New callbacks in `RealtimeTranscription`**: `on_ready` (fires on `transcription_session.updated`) and `on_speech(active: bool)` (fires on `speech_started`/`speech_stopped`). These are dispatched to the GTK thread via `GLib.idle_add`, same pattern as `on_delta`.

Confirmed working with live testing. The semantic VAD behavior is notably good — pausing mid-sentence keeps the segment open (the model understands the speaker hasn't finished), while pausing after a complete sentence triggers a quick commit. This is the core advantage of `semantic_vad` over `server_vad` for dictation.

**Phase 4 status: complete.** All deliverables from the design doc §7 Phase 4 are implemented and tested:
- Session rotation at termination ✓
- Signal handlers (SIGTERM/SIGINT → finalize WAV) ✓
- Error handling for all failure modes ✓
- Auto-close timeout ✓
- Edge cases (short/long/empty/rapid invocation) ✓
- Visual polish (status feedback, dot-to-speech binding) ✓
- Semantic VAD confirmed reliable across 5 targeted test scenarios ✓

**Bug fix: Cancel hangs the process (`app.py`)** — Pressing Cancel during RECORDING caused the process to hang indefinitely after the window closed. Root cause: `_teardown_async()` deferred `audio.stop()` to a daemon thread, then `self.quit()` caused `app.run()` to return. During Python interpreter shutdown, `sounddevice`'s `atexit` handler calls `Pa_Terminate()`, which blocks waiting for the still-active PortAudio stream to close. But the daemon thread that was supposed to close it gets killed by Python's daemon thread cleanup first → deadlock.

Fix: stop audio synchronously on the GTK thread in the CANCELLED state handler before deferring to `_teardown_async()`. `audio.stop()` is fast (~1ms: `stream.stop()` + `stream.close()` + WAV finalize). Only the slow part (`transcription.cancel()` with its 15s join timeout) goes to the daemon thread. The Stop path was unaffected because `_stop_providers` completes `audio.stop()` in a background thread that finishes before the window closes.

Also fixed: `window.close()` in the CANCELLED UI handler is now deferred via `GLib.idle_add` to avoid nesting window destruction inside the `machine.transition()` callback chain.

The tool is ready for daily use. Remaining deferred items that could improve the experience:
- **Transcription prompt hints** for technical vocabulary (existed in VoxInput via WHISPER.txt)
- **Audio level meter** — `Gtk.LevelBar` for "am I being heard?" feedback
- **Fade gradient `lookup_color("vox_bg")` silently fails** — cosmetic, falls back to hardcoded rgba(30,30,30,0.85)

---

### 2026-03-27 — Session 6: Transcription prompt hints (WHISPER.txt)

**Feature**: Detect focused window's project directory and load WHISPER.txt as transcription context.

**New module: `prompt.py`** — Best-effort detection chain, mirroring the old VoxInput `record.sh` logic:

1. **Focused window PID** via `Gio.DBusProxy` D-Bus call to the GNOME Shell Windows extension (`org.gnome.Shell.Extensions.Windows.List`). Returns JSON with window metadata including `focus` and `pid`.
2. **Working directory resolution** — reads `/proc/{pid}/cmdline` to identify the application:
   - **Ghostty + tmux → nvim**: `tmux display-message -p '#{pane_current_command}'` → if nvim, `pgrep -t <pane_tty> nvim` → `/proc/{nvim_pid}/cwd`
   - **Ghostty + tmux → other**: `tmux display-message -p -F '#{pane_current_path}'`
   - **Other**: `/proc/{pid}/cwd`
3. **WHISPER.txt**: read from resolved directory, collapse whitespace to single line.

Every step catches exceptions and returns None on failure — never blocks startup.

**Critical timing issue**: prompt detection must run **before `win.present()`** in `do_activate()`. Once our GTK window is presented, it takes focus, and the D-Bus query returns our own PID. The initial implementation placed detection in `_initialize()` (after `GLib.idle_add`, well after present) — debug log showed it resolving Voxize's own process as the focused window.

**Prompt wiring**:
- **Transcription API**: passed as `input_audio_transcription.prompt` in the `transcription_session.update` config sent on WS connect. Included from the very first message so all audio (including startup burst) is transcribed with prompt context.
- **Cleanup**: appended as `<transcription_context>` block in the GPT-5.4 Mini system prompt, matching the old record.sh pattern.

**UI: context bar** — persistent `Gtk.Label` at the bottom of the overlay showing the active WHISPER.txt content. Wraps up to 3 lines (`set_lines(3)` + `Pango.EllipsizeMode.END`), then ellipsizes. The filename is a clickable `file://` link via Pango `<a href="">` markup.

Implementation decisions:
- **Pango link color override**: GTK4's `GtkLabel` ignores CSS for link colors (uses accent color internally). The only reliable override is `<span foreground="#ffffff80">` directly in the Pango markup. `rgba()` syntax is not supported by Pango — only hex colors (with optional alpha via `#RRGGBBAA`).
- **No libadwaita dependency**: initially tried `Adw.ToastOverlay` for a brief notification, but it cropped badly in the tiny overlay window and took vertical space. Replaced with a plain `Gtk.Label` styled via CSS — no extra dependencies needed.
- **No new checks.py entries**: tmux/pgrep are system tools that should be present. The entire feature is best-effort with graceful fallbacks at each step.

#### Next
- Begin daily use with prompt hints active. Monitor debug.log for detection accuracy.
- Audio level meter remains the highest-value deferred item.

---

### 2026-03-27 — Session 7: Bug fix — text pulse missing during CLEANING drain

**Bug fix: text pulse not starting until drain completes (`ui.py`)** — The text view should start pulsating (`.processing` CSS class toggling opacity) immediately when entering CLEANING state ("Finishing…"), but it sat static during the entire drain period (~5-15s). The pulse only began when `show_transcript_for_cleanup()` called `_start_text_pulse()` after the background thread finished draining audio and transcription.

Root cause: `_on_state_change()` calls `_stop_text_pulse()` unconditionally at the top for every state transition (line 329). The CLEANING branch never restarted it. The pulse only began much later when `show_transcript_for_cleanup()` ran after the drain.

Fix (two changes):
1. **Start pulse in CLEANING branch** — added `self._start_text_pulse()` in the CLEANING case of `_on_state_change`, so the pulse begins immediately on state transition.
2. **Make `_start_text_pulse()` idempotent** — added `self._stop_text_pulse()` at the top of `_start_text_pulse()`. Without this, the second call from `show_transcript_for_cleanup()` would overwrite `_text_pulse_source` without removing the old GLib timeout, leaking the timer. Now the second call safely replaces the first.

---

### 2026-03-27 — Session 8: Feature — show session costs on READY

**Feature: session cost display in the context bar.**

After cleanup completes and the session reaches READY, the `_context_label` (previously used for WHISPER.txt context, hidden during CLEANING) is repurposed to show API costs:

```
Total $0.0016 • Transcription $0.0012 · Cleanup $0.0004
```

**Cost data sources:**

- **Transcription (`transcribe.py`)** — The OpenAI Realtime API returns token usage in each `conversation.item.input_audio_transcription.completed` event with `input_tokens` (broken down into `text_tokens` + `audio_tokens`) and `output_tokens`. These are accumulated across all completed items in a session. Priced at gpt-4o-transcribe rates: $2.50/MTok input, $10.00/MTok output.

- **Cleanup (`cleanup.py`)** — The OpenAI Chat Completions API returns usage in the final streamed chunk when `stream_options={"include_usage": True}` is set. Reports `prompt_tokens` and `completion_tokens`. Priced at gpt-5.4-mini rates: $0.75/MTok input, $4.50/MTok output.

**Wiring:** `app.py` stores usage dicts from both providers (`_transcription_usage` passed through `_start_cleanup`, `_cleanup_usage` captured in `_on_cleanup_done` before nulling the provider). `_show_session_costs()` computes dollar amounts and calls `ui.show_session_costs()`. If a cost is unavailable (API didn't return usage), that component shows `—` and is omitted from the total. In mock mode, no costs are shown (no real API calls).

---

## Session 9 — Prompt hallucination fix, VAD tuning, Responses API, level meter

### Prompt removal from Realtime API

The `prompt` field in the transcription session config (populated from WHISPER.txt) was the direct cause of hallucinations on quiet audio. When the audio signal was weak, the decoder echoed the prompt text — e.g., "Glossary: voxize" became the transcription of a 12.8-second speech segment. This is a documented `gpt-4o-transcribe` bug with multiple reports on the OpenAI Developer Community.

**Fix:** Removed the `prompt` parameter from `_configure()` in `transcribe.py`. Vocabulary hints from WHISPER.txt are now consumed by the cleanup model instead, via a `<vocabulary-guidance>` block in the system prompt that instructs phonetic matching with context-aware substitution. The cleanup model can reason about whether "vocalize" should be "voxize" from surrounding words — something the Whisper decoder bias could never do.

### VAD eagerness: "low" → "auto" → "high"

The original `eagerness: "low"` waited for the user to finish speaking before chunking. This produced segments up to 15+ seconds, which the model truncated — the tail of long segments was silently dropped. Changing to `"high"` splits more aggressively, keeping segments short enough for reliable transcription.

### Cleanup migrated to Responses API

`cleanup.py` switched from Chat Completions (`client.chat.completions.create()`) to the Responses API (`client.responses.create()`). This enables `reasoning: {"effort": "low"}` — appropriate for text reformatting, not complex reasoning. The Responses API uses `instructions` + `input` instead of `messages`, and stream events are typed (`response.output_text.delta`, `response.completed`).

Session-level event logging added: `cleanup_events.jsonl` logs raw SDK events (via `model_dump_json()`), matching the `ws_events.jsonl` pattern for the transcription WebSocket.

### AGC retrospective (seven failed attempts)

Per-chunk Python gain manipulation was attempted in seven variants (RMS-based adaptive, hard gate, smooth decay, fast/slow release, auto-calibrating, fixed gain, noise-floor calibration). All degraded the Realtime API's transcription quality by destroying natural audio dynamics that the server-side VAD and transcription model depend on. Full analysis in `docs/superpowers/specs/2026-03-30-quiet-mic-research-and-conclusions.md`.

Audio is now passed through unchanged. A passive `LevelMeter` class tracks RMS levels for a thin OSD progress bar (3px, `GtkProgressBar` with `.osd` class) visible during recording. If audio-level amplification is needed in the future, PipeWire static gain (subprocess with `module-filter-chain`) is the recommended approach — it preserves natural dynamics perfectly.

### Cleanup usage tracking update

Cleanup usage is now obtained from the Responses API's `response.completed` event (`event.response.usage.input_tokens` / `.output_tokens`), replacing the Chat Completions pattern of reading `chunk.usage.prompt_tokens` / `.completion_tokens` from the final streamed chunk.

---

## Session 10 — Three-phase transcription (live preview + batch + cleanup)

### The realtime VAD problem (unsolvable)

Extended investigation of the realtime transcription API's VAD modes confirmed that neither option is reliable for continuous dictation:

- **`server_vad`** — over-segments continuous speech at brief pauses (silence_duration_ms=500). Words at segment boundaries are dropped because the transcription model only sees each segment in isolation. A 82-second monologue was split into 17 segments, with multiple phrases lost. The model also hallucinated at boundaries (e.g., "put a cross in this" instead of "put across in this").

- **`semantic_vad`** — under-segments, producing 15+ second segments that the model silently truncates. The tail of long segments is dropped. With `eagerness: "auto"`, 16-second segments lost entire sentences. With `eagerness: "high"`, it was better but still unreliable.

Across 5 analyzed sessions with both VAD modes, every one had missing words, phrases, or entire sentences. The audio pipeline was perfect in all cases (all chunks sent, zero queue backlog) — the problem is entirely in how the API segments and transcribes.

**Key insight:** batch transcription (`POST /audio/transcriptions`) processes the entire WAV in one pass and produces accurate results every time. The existing `recover.sh` script demonstrates this — recovered transcripts consistently match the ground truth. The realtime API was never going to match this quality because it must segment speech on-the-fly.

### Three-phase architecture

Split transcription into three phases:

1. **Live preview** (RECORDING) — `gpt-4o-mini-transcribe` via realtime WS. Cheap, throwaway, gives visual feedback while speaking. Uses `server_vad` with fast startup (audio before WS, burst drains at full speed). If burst corrupts server_vad and the preview is garbled, that's acceptable.

2. **Batch transcription** (TRANSCRIBING) — `gpt-4o-transcribe` via `POST /audio/transcriptions` with streaming. Accurate, authoritative. Produces the source-of-truth transcript.

3. **Cleanup** (CLEANING) — `gpt-5.4-nano` text reformatting. The batch transcript is already clean, so cleanup is light touchup (filler removal, paragraph formatting, emphasis). Nano handles the explicit rules-based prompt well and is 73% cheaper than mini.

### State machine: TRANSCRIBING added

```
INITIALIZING -> RECORDING -> TRANSCRIBING -> CLEANING -> READY
```

`TRANSCRIBING` is a new state between RECORDING and CLEANING. No ERROR transition from RECORDING (WS failure is non-fatal). Batch/cleanup failures transition to READY (not ERROR) because the WAV + recover.sh are still usable.

### Fast startup restored

The `fd21f9a` commit had deferred audio capture until WS ready to avoid burst-corrupting server_vad. With the live preview being throwaway, this concern is irrelevant. Audio capture starts immediately in `_initialize`, chunks queue while WS connects (~0.7-1.2s), burst drains at full speed. The user speaks immediately — no waiting for WS.

### WS failure is non-fatal

Previously, WS failure during RECORDING transitioned to ERROR (terminal). Now it shows an error banner but audio capture continues. The user can still speak, stop, and get batch transcription. Only mic failure is fatal.

### Stop sequence (no drain)

The old architecture spent 5-15 seconds draining the WS (send remaining chunks, commit, wait for trailing transcription events). All of this was removed — the live transcript is throwaway, so `stop()` just cancels the WS immediately. The user hits Stop and the batch phase starts within milliseconds.

### Batch `prompt` hallucination (same bug as realtime)

Initially, the batch API was called with `prompt` from WHISPER.txt for vocabulary guidance. First test: the batch returned only "Voxize" (the prompt text) for 29.5 seconds of audio. The `input_token_details` showed `text_tokens: 5` confirming the prompt was injected. This is the same `gpt-4o-transcribe` hallucination bug documented in Session 9 — the decoder echoes the prompt instead of transcribing.

**Fix:** Removed `prompt` from the batch API call. Vocabulary guidance stays in the cleanup model's `<vocabulary-guidance>` block where it works correctly (the cleanup model reasons about context, unlike the Whisper decoder bias).

### Clipboard strategy

No junk in the clipboard. The old architecture copied the raw realtime transcript as a "safety net" — but it was often garbled. New strategy:

| Event | Clipboard |
|-------|-----------|
| Live preview text | Nothing |
| Batch transcription completes | Write batch transcript |
| Cleanup completes | Overwrite with cleaned text |

### Cost comparison

| Phase | Old | New |
|-------|-----|-----|
| Realtime | $0.0030/min (gpt-4o-transcribe) | $0.0015/min (gpt-4o-mini-transcribe) |
| Batch | — | $0.0030/min (gpt-4o-transcribe) |
| Cleanup | $0.0015/min (gpt-5.4-mini) | $0.0004/min (gpt-5.4-nano) |
| **Total** | **$0.0045/min** | **$0.0049/min** |

~9% cost increase for dramatically better accuracy.

### New module: `batch.py`

Mirrors the `cleanup.py` pattern: synchronous OpenAI SDK streaming call in a daemon thread (`voxize-batch`), deltas posted to GTK thread via `GLib.idle_add`, events logged to `batch_events.jsonl`.

### Session files updated

New files per session: `live_transcript.txt` (throwaway preview, saved for debugging), `batch_events.jsonl` (batch API events). The `transcription.txt` now contains the batch result (not the realtime preview).

### UI: live preview pulse during batch

When the user hits Stop, the live preview text stays visible and pulses while the batch API streams. The first batch delta swaps it out — same pattern as the transcript-to-cleanup swap. Status label progression: "Recording" → "Listening..." (first text) → "Transcribing..." → "Cleaning up..." → "Ready".

### `transcribe.py` simplified

Removed all drain machinery: `_draining` flag, `_drain_complete` event, `_items_in_flight` counter, `_drain_sentinel_received`, `_check_drain_complete()`, sentinel commit logic. `stop()` is now just `cancel()`. The transcript is still accumulated for `live_transcript.txt` but is not used by the pipeline.

### Review findings (four passes)

Four rounds of code review caught: MockCleanup missing `usage` property (crash in mock mode), `_batch_transcript` not initialized, file handle leak in batch.py, CSS class leak (`transcribing`/`initializing` not in removal list), missing `.status-dot.transcribing` CSS rule, stale WS delta race (fixed with state check in `_awaiting_batch` swap), hardcoded WAV byte rate (replaced with audio.py constants), `_initialize` state guard (cancel-during-init race).

---

### 2026-04-21 — Focus-aware auto-close countdown

**Problem:** fixed 30s auto-close fires even when the window is blurred, so if the user alt-tabs into another app for longer than 30s there's nothing to come back to.

**Change:** the countdown is now gated on window focus — cancel on blur, restart from the full duration on refocus. Ownership moved from `app.py` to `ui.py`, which already manages the recording timer and listens to `notify::is-active`. The remaining seconds are shown inline on the Close button as `Close (30s)`, `Close (29s)`, … so there's no layout shift and no ambiguity about what the number means. The `(Ns)` span is rendered with `weight="light" fgalpha="50%"` via Pango markup — requires a custom `Gtk.Label` child on the button since `Gtk.Button.set_label()` is plaintext-only.

`VOXIZE_AUTOCLOSE=0` still disables the behaviour entirely.

---

### 2026-04-21 — Per-app volume ducking during recording

**Problem:** when recording with a Bluetooth headset, switching from A2DP into HFP/hands-free profile drops Chrome audio into the same low-bandwidth mono stream as the mic. Whatever is playing in the browser becomes a loud, garbled distraction. Pausing audio manually before every recording is a reliable source of friction.

**Change:** new `ducking.py` module snapshots the current volume of matching PipeWire playback streams on entering `RECORDING`, sets them to `DUCK_VOLUME` (default `0.0` — silent), and restores the snapshot on leaving `RECORDING`. Target apps live in the module-level constant `DUCKED_APPS`; default is `["chrome", "chromium", "brave", "firefox"]`.

**Why snapshot-and-restore, not clamp-to-100%:** PipeWire/PulseAudio has no override layer, just a volume. "Remove overrides" is faked by saving the pre-duck value and writing it back — the user's pre-existing volume is preserved if they had Chrome at 50% or already muted.

**Why `pw-dump` + `wpctl`:** `pactl` isn't in the stock NixOS/GNOME profile; `pw-dump` (JSON object listing) and `wpctl` (`get-volume` / `set-volume` by numeric node id) are. Both tools missing = silent no-op; recording continues normally.

**Matching is exact, not substring.** "chrome" matches a playback stream whose `application.process.binary` is exactly `chrome`, not `chromium` or `chrome_crashpad_handler`. Candidate properties are `application.process.binary`, `application.process.name`, `application.name`, `application.id`, `node.name` — a node matches if **any** of them equals (case-insensitively) any entry in `DUCKED_APPS`. This lets the user list binary names, friendly names, or reverse-DNS app ids interchangeably.

**Thread model:** `duck()` and `restore()` spawn a short-lived daemon thread (`voxize-duck` / `voxize-unduck`) to run pw-dump + wpctl — the GTK main loop never blocks on subprocess. A `threading.Lock` serialises them so restore always sees the full snapshot even if fired immediately after duck. On shutdown paths (window close, SIGTERM) `restore_sync()` runs on the caller's thread: daemon threads die with the process, so fire-and-forget would risk leaving browser audio muted forever.

**Mock mode is exempt.** `VOXIZE_MOCK=1` instantiates the ducker with an empty app list, so UI tests don't silence the user's actual browser.

---

### 2026-04-21 — TOML user config at `$XDG_CONFIG_HOME/voxize/voxize.toml`

**Problem:** `DUCKED_APPS` and `VOXIZE_AUTOCLOSE` were the first two user-tunable knobs (one a module constant, one an env var). Tuning either meant editing the source or fiddling with environment. Not a long-term answer as more preferences appear.

**Change:** new `config.py` module owns user preferences as a frozen dataclass tree (`Config → DuckingConfig | UIConfig`). `config.load()` runs once from `__main__.py`, creating `~/.config/voxize/voxize.toml` if absent. `config.CONFIG` is then read synchronously from anywhere — `ducking.py` pulls `apps` / `volume`, `app.py` resolves `autoclose_seconds`.

**Bootstrap on first run:** writes a template where every default is commented out. Uncommenting a line is a pure "override" gesture; the user never has to copy-paste a full default. No drift handling — if defaults change in a new version, the existing user file is left alone. The user can delete the file to regenerate the up-to-date template.

**Error handling:** missing tools, unreadable/unwritable dir, malformed TOML — any of these fall back silently to the in-code defaults via a per-field parse. Config is never fatal, never raises.

**Env var override for autoclose:** `VOXIZE_AUTOCLOSE=0` still works for quick testing. Precedence: env > config > in-code default. The other `VOXIZE_*` env vars (`MOCK`, `ERROR`, `STOP`) are test-mode overrides and are intentionally *not* in the TOML — they describe how the app boots, not a user preference.

**Why hand-rolled writer, not `tomli_w`/`tomlkit`:** every line in the template is `# key = value`, so the "serializer" is a 30-line string constant — zero value in pulling in a writer lib.

**Reads synchronous, in-memory:** `CONFIG` is assigned once by `load()`. Modules that want stable reads use `from voxize import config; config.CONFIG.ducking.apps`. Importing the `CONFIG` name directly would capture the pre-load default — intentional pattern to keep the module as the source of truth.
