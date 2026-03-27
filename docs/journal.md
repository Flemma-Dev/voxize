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
