# Voxize

Voice-to-text overlay for Linux/Wayland/GNOME. Python + GTK4, single-process-per-invocation.

## Claude Code plugins

This project expects the [superpowers](https://github.com/obra/superpowers) plugin, primarily used for code reviewing:

```
/plugin install superpowers@superpowers-marketplace
```

## Quick reference

```sh
nix develop                      # enter dev shell (all system deps)
uv run python -m voxize          # dictation overlay (bind to a global hotkey)
uv run voxize-tui                # terminal client (Textual); prints the transcript on exit
uv run python -m voxize.meeting  # meeting recorder (mic + system → stereo WAV)
uv run python -m voxize.backend  # headless backend (stdio JSON Lines; spawned by either frontend)
```

OpenAI API key must be in GNOME Keyring: `secret-tool store --label='OpenAI API Key' service openai key api`

The meeting recorder needs no API key — it produces a WAV only, for offline
transcription/diarization with tools like WhisperX.

## Architecture

State machine drives: INITIALIZING -> RECORDING -> TRANSCRIBING -> CLEANING -> READY (+ CANCELLED, ERROR).

**Three-phase transcription:**
1. **Live preview** (RECORDING) — `gpt-4o-mini-transcribe` via realtime WS. Cheap, throwaway, gives visual feedback.
2. **Batch transcription** (TRANSCRIBING) — `gpt-transcribe` via `POST /audio/transcriptions`. Accurate, authoritative.
3. **Cleanup** (CLEANING) — `gpt-5.6-luna` text reformatting. Light touchup of already-clean batch output.

**Threads during recording (backend process):**
1. Backend main loop (GLib): protocol I/O, state, callbacks. The GUI process has its own GTK main thread.
2. sounddevice callback thread (audio capture -> WAV + asyncio queue)
3. asyncio daemon thread (`voxize-ws`) — WebSocket send/receive (live preview)

Thread bridge (inside the backend): `GLib.idle_add` (worker -> main loop), `call_soon_threadsafe` (main loop -> asyncio).

**The backend is a separate process.** `python -m voxize.backend` runs on a plain `GLib.MainLoop` and is driven over stdio by `voxize.protocol` JSON Lines (commands on stdin, events on stdout). The GTK window and the Textual terminal client (`voxize-tui`) are both subprocess clients of it. Only `voxize/gui/` touches GTK4, and frontends import only `protocol`, `config`, `_trace` and `frontend` (shared, gi-free client plumbing), plus the GTK-side `clipboard` and `mode_switcher`. **The dividing rule:** the backend owns domain facts and the estimates derived from them; frontends own presentation and interaction (see `docs/architecture-decision-headless-backend.md`). Tests enforce both directions: `tests/test_backend_stdio.py::test_backend_never_imports_gtk_or_frontends` for the backend (no Gtk/Gdk/Adw, `voxize.gui` or `voxize.tui`), `tests/test_import_rules.py` for the frontends (no backend modules; `frontend/` and `tui/` import no `gi`).

### Module map

| Module | Role |
|---|---|
| `protocol.py` | JSON Lines message types, encode/decode, `PROTOCOL_VERSION` — shared by backend and frontends |
| `backend/session.py` | Dictation session: orchestrates lifecycle, wires providers, emits protocol events |
| `backend/presenter.py` | Pipeline progress model, stall detection, transcript stage handoffs, failure fallback, `had_content` |
| `backend/stdio.py` | `claim_stdout()`: moves the real stdout to a private fd for protocol lines and points fd 1 at stderr |
| `backend/server.py` | Stdio server: private stdout fd, stdin watch, `protocol.jsonl`, EOF/signal/close lifecycle |
| `gui/__main__.py` | GUI entry point: spawns the backend subprocess before importing GTK, then runs `VoxizeApp` |
| `gui/app.py` | GTK Application: protocol client, keys, signals, clipboard |
| `gui/ui.py` | Dictation window widgets: toolbar strip, transcript card, status row, level/progress strip |
| `gui/context.py` | Focused-window detection → `cwd` + glossary paths for `start` |
| `gui/backend_client.py` | `BackendPipe` on GLib watches: reads events, sends commands, logs and echoes its stderr |
| `frontend/state.py` | `ClientState`: a frontend's mirror of the backend's state; terminal states are final |
| `frontend/client.py` | `ClientCore`: hello/version check, sticky frontend failure, backend-lost, close-once |
| `frontend/pipe.py` | `BackendPipe`: send, line framing, stderr tail, lost report — no event loop |
| `frontend/session_log.py` | `SessionLog`: buffers frontend log lines until `session`, then `gui.log` / `tui.log` |
| `tui/__main__.py` | TUI entry point: spawns the backend before importing Textual, prints the transcript on exit |
| `tui/app.py` | Textual app: protocol client, keys, cancel confirm, vocabulary, clipboard, signals |
| `tui/card.py` | Pure presentation model of the card (the GUI's strings and rules, no toolkit) |
| `tui/widgets.py` / `tui/tui.tcss` | Card widgets and styles (full screen, capped at 88×24) |
| `tui/backend.py` | `BackendPipe` on asyncio readers; backend stderr to `tui.log` only |
| `tui/deliver.py` | Clipboard chain (wl-copy → xclip/xsel → OSC 52), the stdout write, and `claim_terminal_stdout` / `write_output` (keep Textual's size detection working when stdout is piped) |
| `state.py` | State machine (pure logic, no GTK) |
| `_audio_const.py` | Audio format constants (sample rate, channels, WAV header) — no heavy deps |
| `audio.py` | `AudioCapture` (sounddevice) + `WavWriter` (crash-safe WAV) + `LevelMeter` (passive RMS) |
| `transcribe.py` | Live preview: realtime WS client (`gpt-4o-mini-transcribe`, `server_vad`, throwaway) |
| `batch.py` | Batch transcription: `POST /audio/transcriptions` (`gpt-transcribe`, streaming) |
| `cleanup.py` | GPT-5.6 Luna text cleanup (Responses API, streaming) |
| `mock.py` | Mock providers for testing without API calls |
| `prompt.py` | Vocabulary file reading + the single prompt builder |
| `recover.py` | Generates per-session `recover.sh` for batch re-transcription |
| `clipboard.py` | Gdk.Clipboard (Wayland-native, best-effort); GUI-side, also imported by the meeting recorder |
| `mode_switcher.py` | Dictate / Meeting pill toggle, shared by the dictation window and the meeting welcome screen (GTK, frontend-side) |
| `ducking.py` | Per-app volume ducking via `pw-dump` + `wpctl` (snapshot on RECORD, restore on exit) |
| `config.py` | User config loaded once from `$XDG_CONFIG_HOME/voxize/voxize.toml` (frozen dataclass, synchronous reads) |
| `storage.py` | XDG state directory, session retention (configurable count + age) |
| `lock.py` | Mic lock via `fcntl.flock()` |
| `checks.py` | Startup dependency validation |
| `gui/dictation.css` | The dictation window's only custom CSS (~35 lines); everything else is stock libadwaita |
| `style.css` | Meeting recorder CSS theme (not loaded by the dictation app — both define `.status-dot`) |
| `meeting/` | Meeting recorder package — separate Gtk.Application, dual-stream capture (mic + system) to stereo WAV via two `parec` subprocesses. No API calls. Shares `audio.py`/`lock.py`/`storage.py` with the dictation app and owns `style.css`; entry point `python -m voxize.meeting`. |

## Key technical decisions

- **Three-phase transcription** — realtime WS is unreliable for continuous dictation (server_vad over-segments, semantic_vad under-segments). Live preview is throwaway; batch transcription on the full WAV is the source of truth.
- **`gpt-4o-mini-transcribe` for live preview** — 50% cheaper than full model, accuracy doesn't matter. `server_vad` with fast startup (audio before WS, burst drains at full speed).
- **`gpt-transcribe` for batch** — processes entire audio in one pass. Billed per second of audio ($0.0045/min), not per token, so `usage` reports `{"seconds": N}`. `WHISPER.txt` is sent verbatim as `prompt` — the old `gpt-4o-transcribe` prohibition (it echoed the prompt instead of transcribing) does not apply to this model, measured 0 leaks in 12 calls. The `keywords` parameter is deliberately **not** used: measured no benefit over `prompt`, and it rejects the entire request on a formatting violation.
- **Vocabulary guidance is two-layer, both required** — `prompt` on batch fixes phonetically distinct errors ("Superbase" → Supabase, reliable). It does **not** reliably fix homophones ("Loop" → Luup, 2/3 at best). The cleanup model's `<vocabulary-guidance>` block is the backstop for that class and must stay. Prose context in `WHISPER.txt` measurably helps the hard cases over a bare `Glossary:` line.
- **GPT-5.6 Luna for cleanup** — batch transcript is already clean, cleanup is light touchup. Luna handles the explicit rules-based prompt well.
- **Warmup pings are billable and prime the prompt cache** — during RECORDING the app pings every 45s to keep the TLS socket warm. The *first* ping carries the full cleanup developer message so OpenAI caches the ~1400-token prefix before the user stops talking (measured: cleanup goes from `cached=0` to `cached=1385`). Later pings send a 7-token request — re-sending the payload would cost roughly a cleanup call each time for no extra benefit. Both the ping and `Cleanup._run` build that message via `cleanup.build_cache_prefix_input()`; a byte of divergence silently drops the cache to zero, so they must never construct it independently. **This is a latency trade, not a cost saving.** The first ping fires immediately on entering RECORDING (not at 45s), so every session primes. Priming cannot reduce total spend — you pay for the prefix twice, once as the ping writes it and again as cleanup reads it cached. Measured on a real 12.8s session: pings $0.0003 + cleanup $0.0001 = $0.0004, against ~$0.00034 for an unprimed cleanup alone. Net **+$0.00006/session, ~$0.02/month**. What it buys is moving prefill off the post-stop critical path, and that TTFT gain is still within run-to-run noise.
- **WS failure is non-fatal** — during WARMING or RECORDING, WS errors show an inline notice but audio continues. User can still stop and get batch transcription. Only mic failure is fatal.
- **Fast startup** — sounddevice (~2s via numpy) and the openai SDK (~300ms) are both deferred to the bootstrap background thread. The window is visible within ~300ms; audio capture starts once sounddevice loads. Audio format constants live in `_audio_const.py` so that `batch.py` can reference them without pulling in sounddevice. Chunks queue, burst-drain when WS ready. If burst corrupts server_vad, live preview is garbled but batch fixes everything.
- **Thread bridge over gbulb** — asyncio in daemon thread + `GLib.idle_add`. Simpler, no extra dependency.
- **WAV placeholder header** — crash-safe; most tools read to EOF when size field is wrong.
- **Pruning at termination, not startup** — avoids progressive session loss during crash loops.
- **`finalize_wav()` separate from `stop()`** — signal handlers fix WAV header without risking deadlock on the audio stream.
- **Clipboard: batch then cleanup** — the backend emits `result` events (batch first, then cleaned); the GUI writes the clipboard on each one, so the first write is accurate batch text, overwritten by the cleanup result. No live preview junk in clipboard. The clipboard mechanics stay in `voxize/clipboard.py` (the meeting recorder imports it).

## Design documents

- `docs/design.md` — **immutable** original specification. Never edit.
- `docs/journal.md` — living record of implementation decisions and deviations. Append-only, chronological.
- `docs/architecture-decision-layer-shell-and-dual-frontend.md` — why gtk4-layer-shell was dropped.
- `docs/architecture-decision-headless-backend.md` — why the backend is a headless stdio process and the GUI is a client of it; the dividing rule.
- `docs/debugging-guide.md` — VAD pacing experiments, debug.log format, diagnostic patterns.

## Conventions

### Commits

[Conventional Commits](https://www.conventionalcommits.org/): `feat`, `fix`, `docs`, `refactor`, etc. Always ask before committing. Journal entries amend into feature commits, not separate.

### Linting

```sh
uv run ruff check src/
uv run black --check src/
```

- Only `E501` is globally ignored (Black handles line length).
- All other suppressions use inline `# noqa: XXXX` with justification.

### Tests

```sh
uv run pytest
```

Presenter unit tests (fake clock); stdio contract tests that spawn `python -m voxize.backend` with `VOXIZE_MOCK=1` (`VOXIZE_MOCK_FAST=1` shrinks the fixed mock delays); in-process `Session` tests that cover each `VOXIZE_MOCK_OUTCOME`; GUI client, app and context tests; Textual TUI tests (`run_test`, headless) with fakes and against a mock backend; and the import-rule tests.

### Running ad-hoc NixOS tools

```sh
nsx <package> -- <command>   # e.g., nsx gdb -- gdb ...
```

### CSS

Libadwaita uses CSS custom properties (`--name` / `var(--name)`), not `@define-color`. Override libadwaita's own variables (`--view-fg-color`, `--destructive-bg-color`, etc.) on widgets to avoid specificity fights.

### GTK4/PyGObject patterns

- `gi.require_version('Gdk', '4.0')` in every module that imports Gdk.
- `GLib.idle_add` for all cross-thread UI updates.
- `GLib.unix_signal_add` over `signal.signal` in GTK apps.
- Blocking work (WS join, subprocess) must go to background threads — never block the GTK main thread.

## Session data

`~/.local/state/voxize/<ISO-timestamp>/` — `audio.wav`, `live_transcript.txt`, `transcription.txt`, `cleaned.txt`, `prompt.txt`, `ws_events.jsonl`, `batch_events.jsonl`, `cleanup_events.jsonl`, `debug.log`, `protocol.jsonl`, `gui.log`, `tui.log`, `recover.sh`.

`protocol.jsonl` is every protocol message in and out, timestamped. `gui.log` / `tui.log` is the frontend's own log, including (GUI) focus/Enter routing diagnostics; `debug.log` stays backend-only.

`prompt.txt` is the vocabulary guidance as it stood at record time, verbatim — absent when the session had none. Guidance lives outside the session (a project's `WHISPER.txt`, a per-app or per-Slack-channel glossary) and gets edited, so without this there is no way to tell later whether a term was mis-transcribed because it was missing from the glossary or in spite of being there. `prompt.py:build_transcription_prompt()` is the single builder behind this file, the `prompt` sent to batch, and the one baked into `recover.sh`; they must not construct it independently or the archive stops describing what was actually sent.

Meeting recorder sessions live in the same parent directory with a `-meeting` suffix on the timestamp (e.g. `2026-05-22T11-05-13-meeting/`). Contents: `recording.opus` (the deliverable — Opus 48 kbps stereo, L=mic, R=system) and `debug.log`. The intermediate `recording.wav` is moved to the system trash post-compression and only stays if compression fails; in that case a `compress_error.txt` records why.

Retention is bucketed: each app prunes only its own bucket at close. Buckets are detected from the trailing `-<tag>` on the directory name — dictation writes the `default` bucket (no suffix), the meeting recorder writes `meeting`. Heavy dictation use can't evict meetings, and vice versa. Defaults in `[storage]` (keep 500 newest AND drop > 14 days, strict/OR) apply to every bucket; per-bucket overrides go in `[storage.<bucket>]` and inherit any missing keys from the defaults. Set either key to `0` to disable that limit for the bucket; both `0` disables pruning entirely. Strays (directories whose names don't fit the scheme) are never pruned and get logged. Pruning trashes via `Gio.File.trash()` (recoverable from the system trash).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `VOXIZE_AUTOCLOSE` | `30` | Seconds of focused READY state before the window auto-closes. Counts only while focused: blur stops it, refocus restarts it from the full duration. `0` to disable. |
