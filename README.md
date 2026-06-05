# Voxize

Voice-to-text overlay for Linux — built for devs who talk to AI.

<img src="assets/dictation-ready.png" alt="Voxize — clean text ready to paste" width="520">

I built Voxize because I spend most of my day talking to AI — writing prompts for Claude Code, describing bugs, explaining architecture decisions. Typing all of that was the bottleneck. With Voxize, I press a hotkey, speak naturally, and get clean text in my clipboard 2x faster than I could type it.

What you're reading right now was dictated, not typed.

## How it works

Press your global hotkey and a translucent overlay appears on top of whatever you're working on. Start speaking — your words stream in live:

<img src="assets/dictation-recording.png" alt="Live transcription streaming as you speak" width="520">

When you're done, hit **Stop**. Voxize runs the full audio through OpenAI's batch transcription for maximum accuracy, then cleans up filler words and punctuation with a lightweight AI pass. The result goes straight to your clipboard — paste and move on.

Three phases, one hotkey:

1. **Live preview** — real-time streaming gives you visual feedback as you speak (via `gpt-4o-mini-transcribe`)
2. **Batch transcription** — full audio processed in one pass, dramatically more accurate than real-time segmentation (via `gpt-4o-transcribe`)
3. **AI cleanup** — fixes filler words, punctuation, and formatting (via `gpt-5.4-nano`)

The live preview is throwaway — just visual feedback. The real transcription happens after you stop, and it's [dramatically more accurate](https://blog.angeloff.name/post/2026/05/15/speeding-up-voxize-a-cautionary-tale-about-speech-benchmarks/).

## Why it nails technical terms

OpenAI's transcription models are surprisingly well-aligned with programming vocabulary. Terms like `subprocess`, `WebSocket`, `asyncio`, and `JWT` come through accurately without any hints.

For project-specific jargon, drop a `WHISPER.txt` file in your working directory:

```
Glossary: worktree, subagent, GLib.idle_add, libadwaita, PyGObject
```

Voxize detects the focused window's working directory (via the [Window Calls](https://extensions.gnome.org/extension/4724/window-calls/) GNOME Shell extension) and loads the file as vocabulary guidance. Domain-specific terms that would normally get mangled come through clean. The content is passed directly to the cleanup model, so it can be any free-form instructions — not just a glossary.

## Integrations

Voxize plugs into your desktop environment to work seamlessly. All of these are optional — Voxize degrades gracefully without them — but the experience is much better with them.

- **[Window Calls](https://extensions.gnome.org/extension/4724/window-calls/)** (GNOME Shell extension) — this is how Voxize knows *where* you're working. It detects the focused window's PID, resolves its working directory (even through Ghostty → tmux → nvim chains), and loads your `WHISPER.txt`. Also used for always-on-top in the meeting recorder. Without it, vocabulary guidance is silently skipped.
- **[PipeWire](https://pipewire.org/)** — the audio backbone. Dictation captures via [PortAudio](https://www.portaudio.com/)/sounddevice, meeting recording via `pw-cat --record` (two streams: mic + system audio). Volume ducking uses `pw-dump` and `wpctl` to silence your browser while recording. Ships with modern GNOME.
- **[FFmpeg](https://ffmpeg.org/)** — the meeting recorder uses `ffmpeg` to compress WAV → Opus after recording and to downmix stereo to mono before transcription. `ffprobe` reads recording duration. Not needed for dictation.
- **[ElevenLabs Scribe](https://elevenlabs.io/docs/api-reference/speech-to-text/speech-to-text)** — powers the meeting recorder's post-recording transcription with speaker diarization. Not needed for dictation or for recording meetings — only for transcribing them.
- **[GNOME Keyring](https://wiki.gnome.org/Projects/GnomeKeyring)** — API keys are stored securely via `secret-tool`, not environment variables or config files.

## Getting started

> [!NOTE]
> Voxize is Linux/Wayland/GNOME only. Pin a commit if you need a stable target.

### NixOS package

An example NixOS package is available at [StanAngeloff/nix-meridian](https://github.com/StanAngeloff/nix-meridian/blob/trunk/pkgs/voxize/package.nix). Add it to your system configuration and bind `voxize` to a global hotkey — no dev shell needed.

### Development workflow

1. Clone and enter the dev shell (all system deps handled by Nix):

   ```sh
   git clone https://github.com/Flemma-Dev/voxize.git
   cd voxize
   nix develop
   ```

2. Store your OpenAI API key in the GNOME Keyring:

   ```sh
   secret-tool store --label='OpenAI API Key' service openai key api
   ```

3. Run:

   ```sh
   uv run python -m voxize
   ```

To bind Voxize to a global hotkey (GNOME Settings → Keyboard → Custom Shortcuts):

```sh
nix develop /path/to/voxize --command bash -c "cd /path/to/voxize && uv run python -m voxize"
```

> [!TIP]
> Use [nix-direnv](https://github.com/nix-community/nix-direnv) to cache the dev shell — avoids the cold-start cost on every hotkey press.

## Meeting recorder

Voxize also includes a meeting recorder that captures both your microphone and system audio into a stereo Opus file — left channel mic, right channel system.

```sh
uv run python -m voxize.meeting
```

<img src="assets/meeting-welcome.png" alt="Meeting session list" width="400">

After recording, a built-in workbench lets you transcribe with speaker diarization, rename speakers, and generate meeting titles — all without leaving the app. You can supply key terms before transcribing to improve accuracy on domain-specific vocabulary.

**Why ElevenLabs for meetings, not OpenAI?** Dictation prompts are short, technical, and latency-sensitive — OpenAI's `gpt-4o-transcribe` excels there, especially with `WHISPER.txt` vocabulary guidance. Meetings are longer, more conversational, and need speaker diarization — ElevenLabs' Scribe v2 ranks [#1 across 49 models](https://artificialanalysis.ai/articles/aa-wer-v2) with a 2.3% word error rate, nearly twice as accurate as OpenAI's batch model. Voxize uses the best tool for each job.

Recording needs no API key. To enable transcription, store your ElevenLabs key in the GNOME Keyring:

```sh
secret-tool store --label='ElevenLabs API Key' service elevenlabs key api
```

## Configuration

Voxize reads `$XDG_CONFIG_HOME/voxize/voxize.toml` on startup. A commented template with all defaults is created on first run — uncomment any line to override.

Key settings:

- **Volume ducking** — automatically quiets Chrome, Firefox, and Brave while recording
- **Auto-close** — overlay closes after 30s of inactivity in the ready state. Override with `VOXIZE_AUTOCLOSE=0` to disable
- **Session retention** — 500 sessions / 14 days by default, configurable per-app (dictation and meetings prune independently)

Session data lives in `~/.local/state/voxize/` — audio, transcripts, costs, and debug logs for each session.

## License

[AGPL-3.0](LICENSE)
