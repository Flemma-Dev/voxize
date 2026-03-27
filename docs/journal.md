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
