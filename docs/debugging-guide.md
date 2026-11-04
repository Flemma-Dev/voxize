# Voxize Debugging Guide

This document captures hard-won debugging lessons from the initial implementation sessions. It is designed to be read by a future session with blank context — every observation, root cause, and diagnostic technique is explained from first principles.

## 1. Architecture Overview (for debugging context)

Voxize uses **three-phase transcription**: a throwaway live preview (realtime WS), an authoritative batch transcription (HTTP POST), and text cleanup. The live preview exists only for visual feedback — the batch pass on the full WAV is the source of truth.

**State machine:** `INITIALIZING → RECORDING → TRANSCRIBING → CLEANING → READY` (+ CANCELLED, ERROR)

**Two processes.** The GTK window (`python -m voxize`) spawns the headless backend (`python -m voxize.backend`) and talks to it over stdio JSON Lines. Everything below about threads, `debug.log` and the session files is the backend; the GUI process has its own GTK main thread and writes `gui.log`.

**Threads during recording (backend process):**

```
Backend main loop        sounddevice callback thread       asyncio thread (voxize-ws)
─────────────────        ──────────────────────────        ─────────────────────────
Protocol I/O (stdio)     Fires every 40ms                  WebSocket send/receive
State transitions        Writes PCM to WAV                 send_loop: queue → WS
GLib.idle_add callbacks  Posts chunks to asyncio queue      receive_loop: WS → GLib.idle_add
                         via call_soon_threadsafe           (live preview only — throwaway)
```

**Additional threads after recording:**
- `voxize-stop` — stops audio + cancels WS, then triggers batch
- `voxize-batch` — batch transcription API call (streaming)
- `voxize-cleanup` — GPT-5.6 Luna cleanup API call (streaming)

**Data flow:**
```
Phase 1 (RECORDING):
  Microphone → sounddevice callback → WAV file (always, crash-safe)
                                    → asyncio queue → WS → OpenAI Realtime API (mini model)
                                                           → live preview deltas → UI (throwaway)

Phase 2 (TRANSCRIBING):
  audio.wav → POST /audio/transcriptions (gpt-transcribe, streaming)
            → batch deltas → UI (replaces live preview)
            → transcription.txt + clipboard

Phase 3 (CLEANING):
  batch transcript → GPT-5.6 Luna (Responses API, streaming)
                   → cleanup deltas → UI (replaces batch text)
                   → cleaned.txt + clipboard (overwrites)
```

**Key files:**
- `transcribe.py` — Live preview: realtime WS client (gpt-4o-mini-transcribe, server_vad, throwaway)
- `batch.py` — Batch transcription: POST /audio/transcriptions (gpt-transcribe, streaming)
- `cleanup.py` — Text cleanup: GPT-5.6 Luna (Responses API, streaming)
- `audio.py` — Microphone capture, WAV writer, sounddevice callback
- `backend/session.py` — Orchestration, state transitions, provider lifecycle (was `app.py`)
- `backend/presenter.py` — Pipeline progress model, stall detection, transcript stage handoffs (was part of `ui.py`)
- `backend/server.py` — Stdio server: protocol I/O, `protocol.jsonl`, EOF/signal/close lifecycle
- `protocol.py` — JSON Lines message types shared with frontends
- `gui/app.py` — GTK application: protocol client, keys, signals
- `gui/ui.py` — GTK4 widgets rendering protocol events
- `state.py` — Pure state machine (no GTK)

## 2. Session Directory Structure

Each recording session creates a directory under `$XDG_STATE_HOME/voxize/` (typically `~/.local/state/voxize/`) named by ISO timestamp:

```
2026-04-02T12-47-31/
├── audio.wav              # Full PCM recording (always complete, crash-safe)
├── live_transcript.txt    # Live preview text (throwaway — for debugging only)
├── transcription.txt      # Batch transcript (authoritative result)
├── cleaned.txt            # Post-cleanup text from GPT-5.6 Luna
├── prompt.txt             # Vocabulary guidance as sent (absent if none)
├── ws_events.jsonl        # Every WS event from live preview (OpenAI Realtime API)
├── batch_events.jsonl     # Every event from batch transcription API
├── cleanup_events.jsonl   # Every event from cleanup API (Responses API)
├── debug.log              # Trace-level log from all backend threads
├── protocol.jsonl         # Every protocol message in and out (frontend <-> backend)
├── gui.log                # GUI-side log (absent for other frontends and for mock runs)
└── recover.sh             # Standalone batch re-transcription script
```

**`audio.wav`** is the ground truth. It uses a placeholder-header technique: the RIFF/WAV header is written at open time with size `0xFFFFFFFF`, PCM data is appended and flushed on every chunk, and the header is fixed on finalize. On crash, the PCM data is intact — the header just has wrong sizes. Any audio tool can recover it.

**`live_transcript.txt`** is the throwaway live preview text from the realtime WS. It may be garbled, incomplete, or have missing words — this is expected. Saved for debugging only; never used by the pipeline.

**`transcription.txt`** is the batch transcript from `gpt-transcribe`. This is the authoritative result — it should match the audio content accurately.

**`ws_events.jsonl`** logs every event *received* from the live preview WS. One JSON object per line. Useful for diagnosing WS connection issues but NOT for diagnosing transcript accuracy (the live preview is throwaway).

**`batch_events.jsonl`** logs every event from the batch transcription API (request, `transcript.text.delta`, `transcript.text.done` with usage). This is the key file for diagnosing batch accuracy issues.

**`debug.log`** has trace-level logging from all threads with millisecond timestamps and thread names. This is the primary debugging tool. Progress-bar lines start with `pipeline_progress:` and come from logger `voxize.backend.presenter`; `contrib/calibrate-progress-bar.py` reads them.

**`protocol.jsonl`** is every message the backend sent and received, timestamped, one JSON object per line. Use it to tell a backend problem (the event never went out) from a GUI problem (it went out and the window ignored it). Messages before `session` are buffered and written once the directory exists.

**`gui.log`** is the GUI process's own log: state mirror transitions, backend lost/stderr, and keyboard routing (focus-widget changes, every Enter with the focused and default widgets, `activate-default`, focus/default on entering READY). If Enter or focus misbehaves, look here, not in `debug.log`.

## 3. The OpenAI Realtime API and VAD (live preview only)

**Important context:** The realtime WS is used only for the throwaway live preview (`gpt-4o-mini-transcribe` with `server_vad`). The authoritative transcript comes from the batch API (Section 7). VAD issues in the live preview are expected and acceptable — do not spend time debugging them unless the live preview is completely non-functional (no text at all).

### How it works

The OpenAI Realtime Transcription API (`wss://api.openai.com/v1/realtime?intent=transcription`) uses **server-side Voice Activity Detection (VAD)**. The client sends raw PCM audio chunks, and the server:

1. Buffers incoming audio
2. Runs VAD to detect speech start/stop boundaries
3. Auto-commits the audio buffer when the VAD detects a turn boundary
4. Transcribes the committed segment
5. Streams delta tokens back to the client

### Current configuration (live preview)

```json
{
  "input_audio_format": "pcm16",
  "input_audio_transcription": { "model": "gpt-4o-mini-transcribe", "language": "en" },
  "turn_detection": { "type": "server_vad" },
  "input_audio_noise_reduction": { "type": "near_field" }
}
```

The live preview uses `gpt-4o-mini-transcribe` (cheaper) with `server_vad` (default params). No `prompt` (causes hallucinations). The preview is throwaway — accuracy issues are expected. Audio capture starts before the WS connects (fast startup), so there is a burst of queued chunks that drain at full speed. This may corrupt `server_vad`'s state, but that's acceptable.

### Historical: why neither VAD mode works for authoritative transcription

**This was the key insight that drove the three-phase architecture.**

The `server_vad` expects audio to arrive at approximately real-time rate (1 chunk per 40ms for our 24kHz/16-bit/mono/960-sample blocks). When audio is delivered faster than real-time (burst delivery), the `server_vad`'s internal clock loses alignment with the audio content. This causes:

1. **Truncated speech segments** — VAD detects speech start but fires speech_stopped too early
2. **Missed speech** — VAD fails to detect speech entirely in burst-delivered audio
3. **Session-wide corruption** — Once the VAD's state is corrupted by a burst, it may fail to detect speech correctly for the rest of the session, even after audio resumes real-time delivery

This was proven across multiple sessions with server_vad:

| Session | Pacing | Result |
|---------|--------|--------|
| 18:27 (pre-fast-startup) | Real-time | Full transcript captured |
| 18:33 | Real-time | Full transcript captured |
| 18:48 | Real-time | Full transcript captured |
| 18:57 | Real-time (user waited 2.4s before speaking) | Full transcript captured |
| 19:21 | 5x burst | Worked (user waited, speech in real-time region) |
| 19:26 | 5x burst | **Zero audio detected** — VAD completely dead |
| 19:44 | 1.5x burst | Only "Right?" from 6.6s recording — VAD caught 908ms |
| 20:13 | 1.5x burst | Only "That's pretty cool." from 9.5s — VAD caught 1.07s |

`semantic_vad` avoids this problem because it detects speech boundaries based on content understanding, not silence-based timing. The startup burst can be sent at full speed without corrupting the VAD state. No pacing, no VAD disable/re-enable, no manual commit of the burst — the burst and real-time audio flow as one continuous stream.

### Why bursts happen

With "fast startup", audio capture begins immediately while the WebSocket connects in the background (~0.7-1.2s). Audio chunks accumulate in an asyncio queue. When the WS connects, the send loop drains this backlog at full speed.

### Current approach: semantic_vad with burst passthrough

The send loop flushes the startup backlog at full speed, then continues with real-time chunks. Semantic VAD is enabled from the start and handles the entire stream — both burst and real-time — as one continuous audio input. No pacing delay, no manual commit, no VAD mode switching.

### Historical: server_vad pacing approaches (all failed)

For reference, these approaches were tried with server_vad before switching to semantic_vad:

| Approach | Result |
|----------|--------|
| 5x pacing (8ms per 40ms chunk) | VAD completely broken — zero speech detected or truncated to 1 word |
| 1.5x pacing (27ms) | VAD still corrupted — missed 4+ seconds mid-recording |
| 1.1x pacing (36ms) | VAD still corrupted — missed speech in the middle |
| No commit (VAD off, burst, re-enable VAD) | Startup audio ignored by VAD entirely |
| Manual commit (VAD off, burst, commit, re-enable VAD) | Works but cuts words at boundary, short silence hallucinations |

### How to diagnose VAD issues

**Step 1: Check `ws_events.jsonl` for speech events.**

A healthy session has this pattern:
```
transcription_session.created
transcription_session.updated
input_audio_buffer.speech_started    ← VAD detected speech
input_audio_buffer.speech_stopped    ← VAD detected silence
input_audio_buffer.committed         ← auto-committed by VAD
conversation.item.created
conversation.item.input_audio_transcription.delta  ← streaming text
conversation.item.input_audio_transcription.delta
...
conversation.item.input_audio_transcription.completed
```

A broken session looks like:
```
transcription_session.created
transcription_session.updated
error: input_audio_buffer_commit_empty   ← our explicit commit, buffer was empty
```

Or partially broken:
```
transcription_session.created
transcription_session.updated
input_audio_buffer.speech_started     ← VAD detected something
input_audio_buffer.speech_stopped     ← but only a tiny fragment
input_audio_buffer.committed
...deltas for a fragment only...
error: input_audio_buffer_commit_empty   ← rest of speech was missed
```

**Step 2: Check `audio_start_ms` and `audio_end_ms` in the speech events.**

These tell you exactly where in the audio stream the VAD detected speech. Compare with the total recording duration (from `debug.log`: `audio stop: total_chunks=N` → N × 40ms).

If `audio_start_ms` is within the first 1-2 seconds (the burst region), and the segment is very short, the burst corrupted the VAD.

If `audio_start_ms` is well past the burst region but the segment is still short, the VAD's state was permanently corrupted by the earlier burst.

**Step 3: Batch-transcribe the WAV to confirm speech exists.**

```python
from openai import OpenAI
from voxize.checks import get_api_key
client = OpenAI(api_key=get_api_key("openai"))
with open("path/to/audio.wav", "rb") as f:
    result = client.audio.transcriptions.create(model="gpt-transcribe", file=f)
print(result.text)
```

If batch transcription returns the full text but the realtime API missed it, the audio is fine — the VAD is the problem.

**Step 4: Check the send loop in `debug.log`.**

Look for:
```
_send_loop: first chunk sent                    ← WS connected, sending started
_send_loop: chunk 25 sent (qsize=9)             ← still draining backlog
_send_loop: chunk 50 sent (qsize=0)             ← caught up, real-time flow
Send loop exited: 238 chunks sent (9.5s audio)  ← total audio delivered
```

If chunks_sent matches total audio chunks (from `audio stop: total_chunks=N`), all audio was delivered. The issue is VAD-side, not send-side.

If chunks_sent is much less than total_chunks, audio was lost in the send path (check for `exit_reason=connection-closed` or `exit_reason=cancelled`).

**Step 5: Check the queue drain rate.**

In `debug.log`, look at the `qsize` values over time:
```
send_audio: chunk 25 queued (qsize=24)    ← large backlog
_send_loop: chunk 25 sent (qsize=9)       ← draining
_send_loop: chunk 50 sent (qsize=1)       ← almost caught up
_send_loop: chunk 75 sent (qsize=0)       ← real-time flow
```

If `qsize` drops to 0 within 1-2 seconds of WS connection, the startup burst drained normally.

## 4. The Stop Sequence (no drain)

In the three-phase architecture, the live preview is throwaway, so there is no drain phase. When the user presses Stop:

1. State transitions RECORDING → TRANSCRIBING
2. `_begin_transcribing` (idle callback): grabs audio + transcription references, nulls them, spawns `voxize-stop` thread
3. `voxize-stop` thread: calls `audio.stop()` (fast — finalizes WAV), then `transcription.stop()` (cancels WS immediately, no drain)
4. Saves `live_transcript.txt` and logs WAV size
5. Posts `_start_batch` back to the main loop via `GLib.idle_add`
6. `_start_batch`: creates `BatchTranscription`, starts streaming from `audio.wav`

**Key difference from the old architecture:** the old `stop()` blocked for up to 15 seconds draining the WS (send remaining chunks, commit, wait for trailing events). The new `stop()` just cancels — it takes milliseconds. The live transcript accumulated in `transcribe.py` is saved for debugging but not used by the pipeline.

**Historical:** The drain machinery (`_draining` flag, `_drain_complete` event, `_items_in_flight` counter, sentinel commit) was removed entirely. See journal Session 10 for the rationale.

## 5. Common Failure Modes

### "No speech detected" when there was speech

**Symptom:** App shows "No speech detected." but the user was talking.

**Diagnosis:**
1. Check `transcription.txt` — if missing or empty, the realtime API returned no transcript.
2. Batch-transcribe `audio.wav` — if it has text, the audio is fine.
3. Check `ws_events.jsonl` — look for `speech_started` events. If none: VAD issue (see Section 3).
4. Check `debug.log` — look at `Send loop exited: N chunks sent`. If N is 0 or very low, the send path failed. If N matches total audio chunks, it's a VAD issue.

### Truncated transcript (only first few words)

**Symptom:** Only the beginning of the sentence is captured.

**Diagnosis:**
1. Check `ws_events.jsonl` — look at `audio_start_ms` and `audio_end_ms` in speech events. A very short segment (< 2s) suggests the VAD committed too early.
2. Check the send loop drain in `debug.log` — if speech falls in the burst region (first 1-2s), it's the pacing issue.
3. If speech_started is well past the burst but the segment is short, the VAD's session-wide state may be corrupted. This was observed with 1.5x and 5x pacing.

### UI freeze during Stop

**Symptom:** GNOME offers to kill the app after pressing Stop.

**Root cause (historical, now fixed):** `transcription.stop()` blocks for up to ~15s (send drain + receive drain + thread join). If called on the GTK main thread, the UI freezes.

**Current architecture:** `_begin_cleanup()` spawns a `voxize-stop` background thread that calls `audio.stop()` then `transcription.stop()`. The main loop stays responsive. The CLEANING state shows "Finishing..." with a spinner.

### WebSocket error during recording (degraded mode)

**Symptom:** Error banner appears at the bottom during recording. Transcript text stays visible.

**What's happening:** The WebSocket connection dropped. `_on_ws_error` cancelled the transcription but kept audio capture running. The WAV file is the safety net. The user can close the window — the audio is preserved.

**The WAV is always complete.** Audio capture is the last thing to stop, and the WAV writer flushes on every chunk. Even if the WebSocket dies, the API key is wrong, or the network drops, the microphone keeps recording to disk.

### Empty cleanup result

**Symptom:** Cleaned text is empty or very short.

**Check:** `cleaned.txt` in the session directory. If it's empty, the GPT-5.4 Mini API call failed or returned no content. Check `debug.log` for `_run: complete, cleaned_len=0`.

The raw transcript is always copied to the clipboard BEFORE cleanup starts (as a safety net). The user always has the raw text even if cleanup fails.

### Stuck on "Listening..." — wrong audio device

**Symptom:** The overlay stays on "Listening..." indefinitely. No transcription text appears. The user is speaking but nothing happens.

**Diagnosis:**
1. Check `ws_events.jsonl` — if it only contains `transcription_session.created` and `transcription_session.updated` (no `speech_started` events), the API is receiving audio but not detecting speech.
2. Check `debug.log` — if `_send_loop` shows hundreds of chunks sent at real-time pace with `qsize=0`, the audio pipeline is working correctly. The problem is the audio content, not delivery.
3. Inspect the WAV file with `sox`:
   ```
   nix shell nixpkgs#sox -- sox ~/.local/state/voxize/<session>/audio.wav -n stat
   ```
4. Compare the stats against a known-good session:

| Metric | Working session | Wrong device |
|---|---|---|
| Maximum amplitude | 0.05 | 0.43 |
| RMS amplitude | 0.003 | 0.043 |
| Rough frequency | 3494 Hz | 57 Hz |
| Mean amplitude | ~0 (symmetric) | 0.007 (DC-biased) |

A rough frequency of ~57 Hz is electrical hum (mains noise), not human voice (85-300 Hz fundamental). A heavily asymmetric waveform (max amplitude far from zero, min amplitude near zero) indicates DC offset from the wrong input device.

**Root cause:** `sounddevice` uses the system default input device. If GNOME Sound Settings switches the default (e.g., Bluetooth headset connected/disconnected, monitor audio selected), Voxize captures from the wrong device. The audio is technically valid PCM — it records and sends correctly — but it contains no speech for the API to transcribe.

**Fix:** Check GNOME Settings > Sound > Input and ensure the correct microphone is selected before launching Voxize.

## 6. Debug Log Format and Key Patterns

### Format

```
HH:MM:SS.mmm ThreadName     module.name message
```

Thread names:
- `MainThread` — backend main loop (GLib); the GTK main thread belongs to the GUI process and logs to `gui.log`
- `voxize-ws` — asyncio/WebSocket thread (live preview)
- `Dummy-1` — sounddevice callback thread (Python names portaudio threads "Dummy-N")
- `voxize-stop` — background thread for stopping audio + WS, triggers batch
- `voxize-batch` — background thread for batch transcription API call
- `voxize-cleanup` — background thread for GPT-5.6 Luna cleanup API call
- `voxize-teardown` — background thread for async teardown (cancel path)

### Healthy session timeline

```
# Initialization (fast startup — audio before WS)
_initialize: file logging active
_initialize: creating providers
_initialize: starting audio capture
wav open: path=...
audio start: rate=24000 ch=1 dtype=int16 blocksize=960
audio start: stream active
_initialize: starting transcription WS
start: launching WS thread
_initialize: transitioning to RECORDING
transition: INITIALIZING -> RECORDING allowed=True

# WS connection + burst drain (live preview)
_session: connecting to WS url=wss://...
_session: WS connected                          ← ~0.7-1.2s after audio start
_session: configure sent
_send_loop: first chunk sent                    ← burst drain begins
_recv: type=transcription_session.created
_recv: type=transcription_session.updated
_send_loop: chunk 25 sent (qsize=0)             ← caught up

# Live preview deltas (throwaway — may be garbled)
_recv: type=input_audio_buffer.speech_started
_recv: type=conversation.item.input_audio_transcription.delta item_id=... delta_len=5
_recv: type=conversation.item.input_audio_transcription.completed

# User presses Stop → TRANSCRIBING
transition: RECORDING -> TRANSCRIBING allowed=True
_stop_and_batch: stopping audio
audio stop: complete
_stop_and_batch: audio stopped
_stop_and_batch: cancelling transcription
Send loop exited: 238 chunks sent (9.5s audio)
_stop_and_batch: transcription cancelled, live_transcript_len=42
_stop_and_batch: wav_size=456960 (9.5s audio)

# Batch transcription
_start_batch: session_dir=...
start: wav_path=...
_run: calling API model=gpt-transcribe wav_size=456960 (9.5s audio)
_run: first delta received, len=3
_run: usage input_tokens=95 output_tokens=19
_run: complete, transcript_len=42 events=22
_on_batch_done: transcript_len=42
transition: TRANSCRIBING -> CLEANING allowed=True

# Cleanup
_begin_cleanup: mock=False
show_transcript_for_cleanup: transcript_len=42
start: transcript_len=42
_run: calling API model=gpt-5.6-luna
_run: first delta received, len=6
_run: complete, cleaned_len=38
_on_cleanup_done: cleaned_len=38
transition: CLEANING -> READY allowed=True
_show_session_costs: live_usage={...} batch_usage={...} cleanup_usage={...}
show_session_costs: live=$0.0004 batch=$0.0012 cleanup=$0.0002
```

### Red flags to look for

| Pattern | Meaning |
|---------|---------|
| **Live preview (non-critical — throwaway)** | |
| `Send loop exited: 0 chunks sent` | WS send loop never ran — connection issue (preview only) |
| `exit_reason=connection-closed` on send loop | WS dropped — preview lost, batch still works |
| `_on_ws_error` during RECORDING | WS failed — banner shown, audio continues |
| **Batch transcription (critical — authoritative)** | |
| `_run: complete, transcript_len=0` | Batch returned empty — check audio.wav with sox |
| `_run: complete, transcript_len=` very small | Batch truncated — check batch_events.jsonl for `text_tokens` (prompt leak?) |
| `Batch transcription failed:` | API error — check batch_events.jsonl for details |
| `_on_batch_error:` | Batch failed, went to READY — user has WAV + recover.sh |
| **Audio pipeline** | |
| `audio stop: total_chunks` is 0 | Mic never captured — wrong device or permission |
| `wav_size` is ~44 bytes | WAV has header but no PCM data |
| **General** | |
| `_initialize: skipped` | User cancelled before init completed |
| `_start_batch:` followed by no `_run:` | Batch provider creation failed |
| `cleaned_len=` very small vs `transcript_len=` | Cleanup model dropped content — check cleanup_events.jsonl |

## 7. Batch Transcription (the authoritative path)

Batch transcription is now the core of the pipeline, not a verification tool. The `batch.py` module streams `audio.wav` to `POST /audio/transcriptions` with `gpt-transcribe` and `response_format=text`.

### How to diagnose batch issues

**Step 1: Check `batch_events.jsonl`.**

A healthy batch session looks like:
```
{"type": "request", "model": "gpt-transcribe", "wav_path": "...", "wav_size": 456960}
{"delta": "This ", "type": "transcript.text.delta", ...}
{"delta": "is a ", "type": "transcript.text.delta", ...}
...
{"text": "This is a test.", "type": "transcript.text.done", "usage": {"type": "duration", "seconds": 10}}
```

`gpt-transcribe` is billed by audio duration, so `usage` is
`{"type": "duration", "seconds": N}` — no token counts. Anything reporting
`input_tokens`/`output_tokens` means the model fell back to a token-billed
one (`gpt-4o-transcribe` era), and `_batch_cost` will return `None`.

The `request` event records the `prompt` sent (verbatim `WHISPER.txt`):
```
{"type": "request", "model": "gpt-transcribe", "wav_path": "...", "wav_size": 456960, "prompt": "Glossary: Luup, Supabase"}
```

A broken batch session may show:
```
{"type": "request", ...}
{"delta": "Voxize", "type": "transcript.text.delta", ...}
{"text": "Voxize", "type": "transcript.text.done", ...}
```

A transcript that is just the prompt text back means the decoder echoed the
prompt instead of transcribing. That was a reproducible `gpt-4o-transcribe`
bug (see the 2026-04 journal entries) and is why `prompt` was unused for
months. `gpt-transcribe` was measured clean on this — 0 leaks in 12 calls —
but if it ever regresses, dropping the `prompt` kwarg in `batch.py` restores
the old safe behavior at the cost of vocabulary accuracy.

**Step 2: Check the debug log for batch timing.**

```
_start_batch: session_dir=...
_run: calling API model=gpt-transcribe wav_size=456960 (9.5s audio)
_run: first delta received, len=3              ← should arrive within 2-5s
_run: usage seconds=10 reported=True           ← reported=False means we fell back to WAV duration
_run: complete, transcript_len=42 events=22
```

If `first delta` never appears, the API call is hanging — check network.

**Step 3: Compare `transcription.txt` with `recovered.txt`.**

Run `recover.sh` in the session directory. It uses curl to batch-transcribe independently. If `recovered.txt` matches `transcription.txt`, the batch pipeline is working correctly. If they differ, check whether a `prompt` was accidentally injected.

**Step 4: Verify the audio content.**

```bash
nix shell nixpkgs#sox -- sox SESSION_DIR/audio.wav -n stat
```

Check for: non-zero RMS amplitude (speech exists), rough frequency in human range (85-300 Hz fundamental, not 57 Hz mains hum), no DC bias.

### File size limit

The batch API has a 25 MB file limit. At 24kHz/16-bit/mono (48 KB/s), this allows ~9 minutes of audio. Typical dictation sessions are well under this.

## 8. Signal Handling

`GLib.unix_signal_add` for SIGTERM and SIGINT, registered in `do_activate`. The handler:
1. Calls `audio.finalize_wav()` — fixes WAV header sizes without stopping the stream
2. Releases the mic lock
3. Calls `self.quit()`

**Why `GLib.unix_signal_add` over `signal.signal`?** Python's signal handler requires the GIL, which may be held by GTK during event processing. `GLib.unix_signal_add` integrates with the GLib main loop.

**Why `finalize_wav()` is separate from `stop()`?** The signal handler should only fix the WAV header, not try to stop the sounddevice stream (which may deadlock if the audio callback holds the GIL).

## 10. Session Cleanup

`prune_sessions()` in `storage.py` deletes (via `Gio.File.trash()` — FreeDesktop trash, not `rm`) session directories beyond the most recent 8.

**Called at termination time, not startup.** Rationale: if a bug causes repeated startup crashes, doing cleanup at startup would progressively destroy session history. At termination, the app ran successfully, so pruning is safe.

## 11. Thread Safety Patterns

### GLib.idle_add

All UI updates from background threads go through `GLib.idle_add(callback, *args)`. This marshals the call to the main loop thread (the backend's GLib loop; the GUI process has its own GTK loop fed by the protocol client). The callback runs on the next main loop iteration.

**Caution:** `GLib.idle_add` is fire-and-forget. Multiple calls queue up. Callbacks may arrive after the state has changed (stale callbacks). Always guard with state checks or flags.

### asyncio queue bridge

Audio flows from the sounddevice thread to the asyncio thread via:
```python
self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, chunk)
```

`call_soon_threadsafe` writes to the event loop's self-pipe, waking it up. The callback runs on the asyncio thread. The queue is an `asyncio.Queue` (not thread-safe for direct access, but safe when accessed via event loop callbacks).

### Provider reference nulling

When stopping or tearing down, `Session` (`backend/session.py`) grabs references to providers and nulls the instance variables on the main-loop thread:
```python
audio = self._audio
self._audio = None
transcription = self._transcription
self._transcription = None
```

Then passes the local references to a background thread. This prevents races where a callback on the main-loop thread accesses a half-torn-down provider.

## 12. Historical Pacing Values and Results

For future reference, here are all pacing values that were tried:

| Pace delay | Rate | Drain time (18 chunks) | VAD result |
|-----------|------|----------------------|------------|
| 0.008 (8ms) | 5x | ~0.4s | **Broken** — zero audio or truncated segments |
| 0.027 (27ms) | 1.5x | ~1.5s | **Broken** — truncated segments, session-wide corruption |
| 0.04 (40ms) | 1x | Never drains | Works but buffer grows forever |
| 0.036 (36ms) | 1.1x | ~7s | Testing (expected to work — 65ms cumulative drift) |

The OpenAI cookbook suggests 5x pacing for file transcription, but this is inappropriate for live streaming where the burst-to-realtime transition confuses the VAD. The key insight: **even small bursts can permanently corrupt the VAD's state for the entire session**.

## 13. GTK4 UI Traps (BREATH overlay)

Every item here was a silent failure — GTK either logged nothing, or logged a
warning that is easy to filter away. All were settled by writing a throwaway
probe rather than by reasoning about what GTK "should" do. **If you doubt a
GTK rendering or input behaviour, write a 20-line probe and measure it.** That
is the single most useful habit for this file; assumptions cost several
rebuild cycles each.

### Rendering

1. **`box-shadow` renders *outside* the widget allocation.** It is not clipped.
   The overlay's halo is a plain `box-shadow` on `.card` landing in the card's
   28px transparent margin. An elaborate radial-gradient substitute was built
   on the assumption that it *was* clipped; it bled through the card as a
   blurry donut.
2. **GTK4 CSS has no `line-height`.** The transcript's 1.6 leading comes from
   `set_pixels_inside_wrap()` / `set_pixels_below_lines()`. The per-line height
   is *measured* at runtime, not hardcoded: it differs by several px between
   Source Serif 4 and any fallback, and a constant makes the six-line cap a lie.
3. **`text-decoration-color` and `text-decoration-style` are both ignored.**
   An underline always paints in the text colour, and `dotted` is a hard parse
   error surfacing only as `Gtk-WARNING ... unknown text decoration style
   value`. Use `border-bottom` on the label instead.
4. **An overlaid `GtkProgressBar`'s trough hides the bar beneath it.** The
   pipeline rail stacks two bars in a `Gtk.Overlay`; giving both an opaque
   trough silently covered the journey fill. Only the lower bar paints a track.
5. **`window.csd` carries libadwaita's own window shadow.** That is the source
   of a mystery "second outline" around a transparent window. Grep a dump of
   the stock libadwaita stylesheet for any node you are fighting.

### Sizing

6. **`GtkScrolledWindow` silently rejects `min > max`.** Setting min before
   raising max leaves the min at its **old value** with no exception, only
   `gtk_scrolled_window_set_min_content_height: assertion 'height <=
   max_content_height' failed`. Always
   `set_min_content_height(-1)` → `set_max_content_height(h)` →
   `set_min_content_height(h)`.
7. **`Gtk.Overlay` takes its size from `set_child()`; `add_overlay()` widgets
   do not contribute.** An empty child with the real content as an overlay
   collapses the window to ~30px.
8. **A wrapping `Gtk.TextView` has no useful natural height.**
   `set_propagate_natural_height(True)` settles at ~2 lines regardless of
   content, because height-for-width is not resolved at request time. Measure
   with `widget.measure(Gtk.Orientation.VERTICAL, width)` — **not**
   `get_iter_location()`, whose rect stops at the last glyph and omits trailing
   line spacing, leaving the viewport a few px short and producing a scrollbar
   for text that visibly fits. Bail out and re-queue while `get_width() <= 1`.
9. **A non-resizable window is required for the card to grow.** `set_resizable(False)`;
   a resizable window keeps its mapped size and ignores the child's grown request.

### Position and input

10. **The compositor centres a window once, at map time, using the size it has
    *then*.** With `org.gnome.mutter center-new-windows=true`, whatever height
    the window has at `present()` is what gets centred, and later growth is
    anchored to the top edge. Presenting while the state machine was still
    INITIALIZING mapped a 131px stub (transcript, meter and buttons all hidden)
    that jumped to 230px the moment the mic came up — landing the card 49px
    below centre before the user had spoken. `_prime_initial_size()` lays
    INITIALIZING out like WARMING so the mapped size is honest. It derives the
    height rather than calling `set_default_size` with a constant, because the
    height depends on the measured serif line height. On Wayland a window
    cannot reposition itself, so map time is the only chance to be centred.
11. **A read-only `Gtk.TextView` still takes focus and still handles Ctrl+C.**
    The window's key controller was in the default BUBBLE phase, so the
    TextView consumed the accelerator and copied its own empty selection.
    The controller is now `PropagationPhase.CAPTURE`; a live selection is
    handed back to the TextView so selection-copy still works.
12. **An invisible `Gtk.HeaderBar` gives no drag area.** Drag-to-move came for
    free when the HeaderBar *was* the visible status row. Once the status line
    moved into the card the titlebar was zero-height and the window could not
    be dragged at all. Wrap the row in a `Gtk.WindowHandle`; buttons inside
    still receive their clicks (verify with a real click, see below).

### 3a notes (stock libadwaita window)

Items 1 and 5 above describe the BREATH transparent window and no longer
apply to the dictation window. Two more:

13. **A present stock scrollbar sets a ~58px floor on `GtkScrolledWindow`.**
    With `set_max_content_height(34)` the scroller still allocated 58px, so
    a one-line transcript showed as two. Use `PolicyType.EXTERNAL` while the
    content fits and switch to `AUTOMATIC` only when it overflows.
14. **libadwaita 1.9's own `.caption` uses `line-height: 140%`**, so item 2's
    "no `line-height`" is out of date at least for labels. The TextView
    still sizes lines by measurement.

### Verifying UI behaviour headlessly

Screenshots are rendered by `scripts/generate_screenshots.py`
(`--all` for every state). Two things about the environment are load-bearing:

- **If `WAYLAND_DISPLAY` is set, `xvfb-run` is inert.** GTK prefers the Wayland
  backend and ignores the `DISPLAY` Xvfb exports, so captures go through the
  real compositor and the windows briefly appear on screen. Fine for capture.
- **It silently breaks anything driving input**, because `xdotool` talks to
  Xvfb while the app is on Wayland. A probe written this way read the *host*
  clipboard and reported a false result. Pin both to X11:

  ```sh
  nsx xvfb-run xdotool -- xvfb-run -a nix develop . --command bash -c \
    "WAYLAND_DISPLAY= GDK_BACKEND=x11 uv run python your_probe.py"
  ```

  Trap 11 was confirmed this way: seed the clipboard with a sentinel, send a
  real `ctrl+c`, read it back — then flip the phase to BUBBLE and watch the
  sentinel survive. **Reproduce the bug with the fix reverted**; a passing
  test alone does not prove you fixed the thing you think you did.

- **Do not over-filter the output.** Grep for `CRITICAL|WARNING|Traceback` at
  minimum. A narrow `saved|Traceback` filter once hid a `Gtk-CRITICAL` that was
  the root cause of a bug chased for four rounds.

- **Popovers render into their own `GdkSurface`**, so `Gtk.WidgetPaintable` on
  the toplevel never captures them. Render against the popover's own renderer
  (`_capture(popover, name, native=popover)`), and pop it up only once the
  toplevel is mapped.
