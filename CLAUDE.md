# Voxize

Voice-to-text overlay for Linux/Wayland/GNOME. Python + GTK4, single-process-per-invocation.

## Claude Code plugins

This project expects the [superpowers](https://github.com/obra/superpowers) plugin, primarily used for code reviewing:

```
/plugin install superpowers@superpowers-marketplace
```

## Quick reference

```sh
nix develop              # enter dev shell (all system deps)
uv run python -m voxize  # launch (bind to a global hotkey)
```

OpenAI API key must be in GNOME Keyring: `secret-tool store --label='OpenAI API Key' service openai key api`

## Architecture

State machine drives: INITIALIZING -> RECORDING -> TRANSCRIBING -> CLEANING -> READY (+ CANCELLED, ERROR).

**Three-phase transcription:**
1. **Live preview** (RECORDING) — `gpt-4o-mini-transcribe` via realtime WS. Cheap, throwaway, gives visual feedback.
2. **Batch transcription** (TRANSCRIBING) — `gpt-4o-transcribe` via `POST /audio/transcriptions`. Accurate, authoritative.
3. **Cleanup** (CLEANING) — `gpt-5.4-nano` text reformatting. Light touchup of already-clean batch output.

**Threads during recording:**
1. GTK main thread (UI)
2. sounddevice callback thread (audio capture -> WAV + asyncio queue)
3. asyncio daemon thread (`voxize-ws`) — WebSocket send/receive (live preview)

Thread bridge: `GLib.idle_add` (worker -> GTK), `call_soon_threadsafe` (GTK -> asyncio).

**Backend modules do NOT import GTK.** Only `app.py` and `ui.py` touch GTK4. Backend modules use `GLib`/`Gio` for thread-safe callbacks and I/O. This separation is a seam for a future GNOME Shell extension frontend.

### Module map

| Module | Role |
|---|---|
| `app.py` | GTK Application, orchestrates lifecycle, wires providers |
| `ui.py` | Widgets, CSS loading, text area, header bar |
| `state.py` | State machine (pure logic, no GTK) |
| `audio.py` | `AudioCapture` (sounddevice) + `WavWriter` (crash-safe WAV) + `LevelMeter` (passive RMS) |
| `transcribe.py` | Live preview: realtime WS client (`gpt-4o-mini-transcribe`, `server_vad`, throwaway) |
| `batch.py` | Batch transcription: `POST /audio/transcriptions` (`gpt-4o-transcribe`, streaming) |
| `cleanup.py` | GPT-5.4 Nano text cleanup (Responses API, streaming) |
| `mock.py` | Mock providers for testing without API calls |
| `prompt.py` | Focused-window detection, WHISPER.txt loading |
| `recover.py` | Generates per-session `recover.sh` for batch re-transcription |
| `clipboard.py` | Gdk.Clipboard (Wayland-native, best-effort) |
| `storage.py` | XDG state directory, session rotation (keep 8) |
| `lock.py` | Mic lock via `fcntl.flock()` |
| `checks.py` | Startup dependency validation |
| `style.css` | GTK4/libadwaita CSS theme |

## Key technical decisions

- **Three-phase transcription** — realtime WS is unreliable for continuous dictation (server_vad over-segments, semantic_vad under-segments). Live preview is throwaway; batch transcription on the full WAV is the source of truth.
- **`gpt-4o-mini-transcribe` for live preview** — 50% cheaper than full model, accuracy doesn't matter. `server_vad` with fast startup (audio before WS, burst drains at full speed).
- **`gpt-4o-transcribe` for batch** — full model processes entire audio in one pass. No `prompt` parameter — `gpt-4o-transcribe` hallucinates the prompt text instead of transcribing. Vocabulary guidance is handled by the cleanup model's `<vocabulary-guidance>` block.
- **GPT-5.4 Nano for cleanup** — batch transcript is already clean, cleanup is light touchup. Nano handles the explicit rules-based prompt well.
- **WS failure is non-fatal** — during RECORDING, WS errors show a banner but audio continues. User can still stop and get batch transcription. Only mic failure is fatal.
- **Fast startup** — audio capture starts before WS connects. Chunks queue, burst-drain when WS ready. If burst corrupts server_vad, live preview is garbled but batch fixes everything.
- **Thread bridge over gbulb** — asyncio in daemon thread + `GLib.idle_add`. Simpler, no extra dependency.
- **WAV placeholder header** — crash-safe; most tools read to EOF when size field is wrong.
- **Pruning at termination, not startup** — avoids progressive session loss during crash loops.
- **`finalize_wav()` separate from `stop()`** — signal handlers fix WAV header without risking deadlock on the audio stream.
- **Clipboard: batch then cleanup** — first clipboard write at batch completion (accurate text), overwritten by cleanup result. No live preview junk in clipboard.

## Design documents

- `docs/design.md` — **immutable** original specification. Never edit.
- `docs/journal.md` — living record of implementation decisions and deviations. Append-only, chronological.
- `docs/architecture-decision-layer-shell-and-dual-frontend.md` — why gtk4-layer-shell was dropped.
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

`~/.local/state/voxize/<ISO-timestamp>/` — `audio.wav`, `live_transcript.txt`, `transcription.txt`, `cleaned.txt`, `ws_events.jsonl`, `batch_events.jsonl`, `cleanup_events.jsonl`, `debug.log`, `recover.sh`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `VOXIZE_AUTOCLOSE` | `30` | Seconds before overlay auto-closes in READY state. `0` to disable. |
