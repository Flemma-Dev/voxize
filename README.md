# Voxize

Voice-to-text overlay for Linux/Wayland/GNOME.

> **Actively Evolving.** Voxize is under active development. Pin a commit if you need a stable target. See the [implementation journal](docs/journal.md) for progress and design decisions.

- **Global hotkey opens a translucent overlay** that floats above your workspace. Start speaking immediately.
- **Real-time streaming transcription** via the OpenAI Realtime API (`gpt-4o-transcribe`). Text appears as you talk.
- **AI text cleanup** via GPT-5.4 Mini — fixes spelling, punctuation, and filler words while preserving meaning.
- **Clipboard output** — cleaned text is copied automatically. Raw transcript is preserved in clipboard history as a fallback.
- **Crash-safe audio** — WAV streamed to disk from the first sample. Audio survives process crashes.
- **Session archive with cost tracking** — last 8 sessions stored under `~/.local/state/voxize/` with audio, transcripts, and per-session API costs.

## Screenshots

_TBD_

<!-- TODO: capture screenshots -->
<!--

<img alt="Recording state" src="docs/screenshots/recording.png" width="420">

Live transcription streaming in as the user speaks.

<img alt="Cleaning state" src="docs/screenshots/cleaning.png" width="420">

Transcript pulsing while GPT-5.4 Mini cleans up the text.

<img alt="Ready state" src="docs/screenshots/ready.png" width="420">

Cleaned text with session cost breakdown.
-->

## Requirements

| Requirement | Notes |
|---|---|
| NixOS (tested on 25.11) | NixOS-first — `flake.nix` + `shell.nix` handle all system deps |
| GNOME / Wayland | Mutter compositor; D-Bus used for focused-window detection |
| Python >= 3.11 | Managed by `uv` |
| OpenAI API key | Stored in GNOME Keyring via `secret-tool` |
| `wl-clipboard` | `wl-copy` for clipboard output |
| PortAudio | Audio capture backend |
| [Window Calls](https://extensions.gnome.org/extension/4724/window-calls/) (optional) | GNOME Shell extension for focused-window detection. Required for WHISPER.txt prompt hints; without it, prompt detection is skipped silently |

## Installation

> [!WARNING]
> There is no packaged install yet. The steps below are the development workflow — expect rough edges. Proper packaging will be considered once the project stabilizes.

Enter the dev shell (pulls all system dependencies):

```sh
nix develop
```

Store your OpenAI API key in the GNOME Keyring:

```sh
secret-tool store --label='OpenAI API Key' service openai key api
```

Run:

```sh
uv run python -m voxize
```

Bind this command to a global hotkey in your desktop settings (e.g., GNOME Settings > Keyboard > Custom Shortcuts).

## How it works

Voxize runs as a single Python process per invocation. A state machine drives the session through INITIALIZING, RECORDING, CLEANING, and READY. During recording, microphone audio streams over a WebSocket to the OpenAI Realtime API with semantic VAD (low eagerness, tuned for dictation). Transcription deltas appear live in the overlay. On stop, the accumulated transcript is sent to GPT-5.4 Mini for cleanup, which streams corrected text back into the same overlay. The final text is copied to the clipboard. See [docs/design.md](docs/design.md) for the original specification and [docs/journal.md](docs/journal.md) for implementation deviations and decisions made during development.

## Architecture

Backend modules (`state.py`, `audio.py`, `transcribe.py`, `cleanup.py`, `storage.py`, `lock.py`, `clipboard.py`, `prompt.py`) do not import GTK. They use `GLib`/`Gio` for thread-safe callbacks and I/O, but no widget code. Only `app.py` and `ui.py` touch GTK4. This separation exists as a seam for a future GNOME Shell extension frontend that would spawn the backend as a subprocess and communicate via stdin/stdout JSON Lines. See the [architecture decision record](docs/architecture-decision-layer-shell-and-dual-frontend.md) for context.

## Configuration

**WHISPER.txt** — Place a `WHISPER.txt` file in your working directory. On launch, Voxize resolves the focused window's CWD (via the [Window Calls](https://extensions.gnome.org/extension/4724/window-calls/) extension and `/proc`) and loads the file as transcription context, improving accuracy for domain-specific vocabulary. There is no upward search — the file must be in the exact directory the focused process is running from.

**`VOXIZE_AUTOCLOSE`** — Seconds before the overlay auto-closes in READY state. Default `30`. Set to `0` to disable.

**Session data** — `~/.local/state/voxize/`. Each session directory contains `audio.wav`, `transcription.txt`, `cleaned.txt`, `ws_events.jsonl`, and `debug.log`.

## License

[AGPL-3.0](LICENSE)
