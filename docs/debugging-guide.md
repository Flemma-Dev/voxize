# Voxize Debugging Guide

This document captures hard-won debugging lessons from the initial implementation sessions. It is designed to be read by a future session with blank context — every observation, root cause, and diagnostic technique is explained from first principles.

## 1. Architecture Overview (for debugging context)

Voxize has **three threads** that must cooperate:

```
GTK main thread          sounddevice callback thread       asyncio thread (voxize-ws)
─────────────────        ──────────────────────────        ─────────────────────────
UI rendering             Fires every 40ms                  WebSocket send/receive
State transitions        Writes PCM to WAV                 send_loop: queue → WS
GLib.idle_add callbacks  Posts chunks to asyncio queue      receive_loop: WS → GLib.idle_add
                         via call_soon_threadsafe
```

**Data flow for audio:**
```
Microphone → sounddevice callback → WAV file (always)
                                  → asyncio queue → send_loop → WebSocket → OpenAI Realtime API
```

**Data flow for transcription:**
```
OpenAI Realtime API → receive_loop → GLib.idle_add → GTK thread → UI text area
```

**Key files:**
- `transcribe.py` — WebSocket client, send/receive loops, asyncio thread
- `audio.py` — Microphone capture, WAV writer, sounddevice callback
- `app.py` — Orchestration, state transitions, provider lifecycle
- `ui.py` — GTK4 widgets, state-driven updates
- `state.py` — Pure state machine (INITIALIZING → RECORDING → CLEANING → READY)

## 2. Session Directory Structure

Each recording session creates a directory under `$XDG_STATE_HOME/voxize/` (typically `~/.local/state/voxize/`) named by ISO timestamp:

```
2026-03-27T19-44-29/
├── audio.wav           # Full PCM recording (always complete, crash-safe)
├── ws_events.jsonl     # Every WebSocket event received from OpenAI
├── debug.log           # Trace-level log from all threads (added in Phase 4)
├── transcription.txt   # Raw transcript from realtime API
└── cleaned.txt         # Post-cleanup text from GPT-5.4 Mini
```

**`audio.wav`** is the ground truth. It uses a placeholder-header technique: the RIFF/WAV header is written at open time with size `0xFFFFFFFF`, PCM data is appended and flushed on every chunk, and the header is fixed on finalize. On crash, the PCM data is intact — the header just has wrong sizes. Any audio tool can recover it.

**`ws_events.jsonl`** logs every event *received* from the OpenAI Realtime API. It does NOT log sent events (audio chunks). One JSON object per line.

**`debug.log`** has trace-level logging from all threads with millisecond timestamps and thread names. This is the primary debugging tool.

## 3. The OpenAI Realtime API and VAD

### How it works

The OpenAI Realtime Transcription API (`wss://api.openai.com/v1/realtime?intent=transcription`) uses **server-side Voice Activity Detection (VAD)**. The client sends raw PCM audio chunks, and the server:

1. Buffers incoming audio
2. Runs VAD to detect speech start/stop boundaries
3. Auto-commits the audio buffer when the VAD detects a turn boundary
4. Transcribes the committed segment
5. Streams delta tokens back to the client

### Current configuration

```json
{
  "input_audio_format": "pcm16",
  "input_audio_transcription": { "model": "gpt-4o-transcribe", "language": "en" },
  "turn_detection": {
    "type": "semantic_vad",
    "eagerness": "low"
  },
  "input_audio_noise_reduction": { "type": "near_field" }
}
```

We use `semantic_vad` instead of the default `server_vad`. Semantic VAD detects speech boundaries based on semantic understanding of the user's utterance (whether they appear to have finished speaking), rather than silence-based timing. With `eagerness: "low"`, it waits for the user to finish speaking before chunking — ideal for dictation.

### Why semantic_vad: the server_vad delivery pacing problem

**This is the single most important debugging insight in this entire document.**

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
    result = client.audio.transcriptions.create(model="gpt-4o-transcribe", file=f)
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

If `qsize` drops to 0 within 1-2 seconds of WS connection, pacing may be too fast. If it takes 5-10 seconds, pacing is appropriate for the VAD.

## 4. The `_draining` Flag (transcript delta suppression)

`self._draining` in `transcribe.py` controls whether transcription deltas are posted to the GTK thread during the stop/drain phase. When `stop()` is called:

1. `_draining = True` — receive loop still accumulates text into `self._transcript` but does NOT call `GLib.idle_add(self._on_delta, delta)`
2. `_running = False` — send_audio stops queuing new chunks
3. Signal done → send loop drains remaining queue → commit → wait for trailing events
4. Thread joins → `stop()` returns the accumulated transcript

**Why suppress UI deltas during drain?** The drain phase happens AFTER the state transitions to CLEANING. If deltas were posted to the UI, they would appear in the text area during the "Finishing..." phase, which was the original desired behavior. However, an attempt to remove the `_draining` suppression coincided with a zero-audio delivery failure. While code analysis shows the receive loop changes cannot affect the send path (they are independent asyncio coroutines), the suppression was restored as a precaution.

**If you want to re-enable delta streaming during drain in the future:**
1. The `_awaiting_cleanup` race is structurally resolved: `show_transcript_for_cleanup` runs AFTER the drain completes (posted via `GLib.idle_add` from `_stop_providers`), so all stale delta idle callbacks fire before `_awaiting_cleanup` is armed.
2. The zero-audio issue was later traced to VAD burst pacing, not the `_draining` change.
3. It should be safe to remove the `if not self._draining:` guards in the receive loop.

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

**Current architecture:** `_begin_cleanup()` spawns a `voxize-stop` background thread that calls `audio.stop()` then `transcription.stop()`. The GTK thread stays responsive. The CLEANING state shows "Finishing..." with a spinner.

### WebSocket error during recording (degraded mode)

**Symptom:** Error banner appears at the bottom during recording. Transcript text stays visible.

**What's happening:** The WebSocket connection dropped. `_on_ws_error` cancelled the transcription but kept audio capture running. The WAV file is the safety net. The user can close the window — the audio is preserved.

**The WAV is always complete.** Audio capture is the last thing to stop, and the WAV writer flushes on every chunk. Even if the WebSocket dies, the API key is wrong, or the network drops, the microphone keeps recording to disk.

### Empty cleanup result

**Symptom:** Cleaned text is empty or very short.

**Check:** `cleaned.txt` in the session directory. If it's empty, the GPT-5.4 Mini API call failed or returned no content. Check `debug.log` for `_run: complete, cleaned_len=0`.

The raw transcript is always copied to the clipboard BEFORE cleanup starts (as a safety net). The user always has the raw text even if cleanup fails.

## 6. Debug Log Format and Key Patterns

### Format

```
HH:MM:SS.mmm ThreadName     module.name message
```

Thread names:
- `MainThread` — GTK main thread
- `voxize-ws` — asyncio/WebSocket thread
- `Dummy-1` — sounddevice callback thread (Python names portaudio threads "Dummy-N")
- `voxize-stop` — background thread for stopping providers
- `voxize-cleanup` — background thread for GPT-5.4 Mini API call
- `voxize-teardown` — background thread for async teardown (cancel path)

### Healthy session timeline

```
# Initialization
_initialize: file logging active
_initialize: creating providers
_initialize: starting transcription WS
start: launching WS thread
_initialize: starting audio capture
wav open: path=...
audio start: rate=24000 ch=1 dtype=int16 blocksize=960
audio start: stream active
_initialize: transitioning to RECORDING
transition: INITIALIZING -> RECORDING allowed=True

# WS connection + burst drain
_session: connecting to WS url=wss://...
_session: WS connected                          ← ~0.7-1.2s after audio start
_session: configure sent
_send_loop: first chunk sent                    ← burst drain begins
_recv: type=transcription_session.created
_recv: type=transcription_session.updated
send_audio: chunk 25 queued (qsize=12)          ← backlog visible
_send_loop: chunk 25 sent (qsize=9)             ← draining
_send_loop: chunk 50 sent (qsize=0)             ← caught up (may take longer at 1.1x)

# Real-time transcription
_recv: type=input_audio_buffer.speech_started   ← VAD detected speech
_recv: type=input_audio_buffer.speech_stopped
_recv: type=input_audio_buffer.committed
_recv: type=conversation.item.input_audio_transcription.delta item_id=... delta_len=5
_recv: type=conversation.item.input_audio_transcription.completed

# User presses Stop
transition: RECORDING -> CLEANING allowed=True
_begin_cleanup: mock=False
_stop_providers: stopping audio
audio stop: total_chunks=237
wav finalize: data_bytes=456960
_stop_providers: stopping transcription
Stop called, queue backlog: 0 chunks            ← should be 0 if real-time flow
_signal_done: signalling event loop
Send loop exited: 238 chunks sent (9.5s audio)  ← should ≈ total_chunks
_receive_loop: exit_reason=cancelled events=12 transcript_len=19
_stop_providers: transcription stopped, transcript_len=19

# Cleanup
_start_cleanup: transcript_len=19 empty=False
start: transcript_len=19
_run: calling API model=gpt-5.4-mini
_run: first delta received, len=6
_run: complete, cleaned_len=19
transition: CLEANING -> READY allowed=True
```

### Red flags to look for

| Pattern | Meaning |
|---------|---------|
| `Send loop exited: 0 chunks sent` | Send loop never ran — WS connection issue |
| `exit_reason=connection-closed` | WebSocket dropped during send |
| `exit_reason=cancelled` on send loop | Send loop was cancelled (timeout or cancel) |
| `speech_started` missing entirely | VAD never detected speech — pacing issue or silence |
| `transcript_len=0` | No transcript — check for VAD issues or empty audio |
| `_stop_providers: stopping transcription` takes >10s | Thread join timeout — asyncio thread stuck |
| `qsize` stays high long after WS connected | Send loop is slow or stuck |
| `audio stop: total_chunks` >> `chunks sent` | Audio was captured but not sent |

## 7. Batch Transcription for Verification

When the realtime API gives unexpected results, batch-transcribe the WAV to verify the audio content:

```bash
uv run python -c "
from openai import OpenAI
from voxize.checks import get_api_key
client = OpenAI(api_key=get_api_key('openai'))
with open('SESSION_DIR/audio.wav', 'rb') as f:
    result = client.audio.transcriptions.create(model='gpt-4o-transcribe', file=f)
print(repr(result.text))
"
```

If batch returns full text but realtime missed it: **VAD pacing issue** (see Section 3).
If batch also returns empty/wrong text: **audio quality issue** (mic level, noise, wrong device).

## 8. The Buffer Drain Progress Bar

A thin 3px `Gtk.ProgressBar` at the bottom of the window shows the startup backlog draining.

### How it works

1. When the send loop sends its first chunk, it captures `peak_backlog = qsize + 1`
2. On each subsequent paced chunk (while `qsize > 0` and `peak_backlog > 5`), it posts `fraction = 1.0 - remaining / peak_backlog` to the GTK thread via `GLib.idle_add`
3. When the queue empties after a drain, it posts `fraction = 1.0`
4. The UI sets the progress bar fraction — orange fill grows left-to-right
5. At fraction=1.0, the `.caught-up` CSS class switches to green
6. After 600ms, the bar resets to fraction=0 (transparent)

### Design decisions

- **Always in layout, never show/hidden** — avoids layout shifts. The trough uses Adwaita's `.osd` class which has `background-color: transparent`. At fraction=0 the bar is invisible.
- **One-shot** — A `_buffer_bar_done` flag prevents the bar from reappearing after the initial drain. Set on completion or on any state transition away from RECORDING.
- **Threshold** — Only triggers when `peak_backlog > 5` chunks. Transient 1-2 chunk jitter during real-time flow is normal and ignored.
- **Stale callback guard** — The `_buffer_bar_done` flag is set in `_on_state_change` for all transitions, preventing send loop callbacks that arrive during CLEANING from re-showing the bar.

## 9. Signal Handling

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

All UI updates from background threads go through `GLib.idle_add(callback, *args)`. This marshals the call to the GTK main thread. The callback runs on the next main loop iteration.

**Caution:** `GLib.idle_add` is fire-and-forget. Multiple calls queue up. Callbacks may arrive after the state has changed (stale callbacks). Always guard with state checks or flags.

### asyncio queue bridge

Audio flows from the sounddevice thread to the asyncio thread via:
```python
self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, chunk)
```

`call_soon_threadsafe` writes to the event loop's self-pipe, waking it up. The callback runs on the asyncio thread. The queue is an `asyncio.Queue` (not thread-safe for direct access, but safe when accessed via event loop callbacks).

### Provider reference nulling

When stopping or tearing down, `app.py` grabs references to providers and nulls the instance variables on the GTK thread:
```python
audio = self._audio
self._audio = None
transcription = self._transcription
self._transcription = None
```

Then passes the local references to a background thread. This prevents races where a callback on the GTK thread accesses a half-torn-down provider.

## 12. Historical Pacing Values and Results

For future reference, here are all pacing values that were tried:

| Pace delay | Rate | Drain time (18 chunks) | VAD result |
|-----------|------|----------------------|------------|
| 0.008 (8ms) | 5x | ~0.4s | **Broken** — zero audio or truncated segments |
| 0.027 (27ms) | 1.5x | ~1.5s | **Broken** — truncated segments, session-wide corruption |
| 0.04 (40ms) | 1x | Never drains | Works but buffer grows forever |
| 0.036 (36ms) | 1.1x | ~7s | Testing (expected to work — 65ms cumulative drift) |

The OpenAI cookbook suggests 5x pacing for file transcription, but this is inappropriate for live streaming where the burst-to-realtime transition confuses the VAD. The key insight: **even small bursts can permanently corrupt the VAD's state for the entire session**.
