"""OpenAI Realtime Transcription API — WebSocket client.

Runs an asyncio event loop in a background thread. Audio chunks are posted
from the sounddevice callback thread via loop.call_soon_threadsafe. Delta
text is delivered to the GTK main thread via GLib.idle_add.

Lifecycle:
    t = RealtimeTranscription(api_key, session_dir)
    t.start(on_delta=..., on_error=...)
    # AudioCapture callback calls t.send_audio(chunk) on sounddevice thread
    transcript = t.stop()   # graceful: drain queue, commit, wait for events
    transcript = t.cancel() # immediate close
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
from collections.abc import Callable
from typing import ClassVar

import websockets

logger = logging.getLogger(__name__)


_WS_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
_MODEL = "gpt-4o-transcribe"


class RealtimeTranscription:
    """Streams audio to OpenAI Realtime API and accumulates transcript."""

    def __init__(
        self,
        api_key: str,
        session_dir: str | None = None,
        prompt: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._session_dir = session_dir
        self._prompt = prompt
        self._on_delta: Callable[[str], None] | None = None
        self._on_error: Callable[[str], None] | None = None
        self._on_ready: Callable[[], None] | None = None
        self._on_speech: Callable[[bool], None] | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws = None
        self._audio_queue: asyncio.Queue | None = None
        self._done: asyncio.Event | None = None
        self._log_file = None

        self._transcript = ""
        self._current_item_id: str | None = None
        self._running = False
        self._cancelled = False
        self._draining = False

        # Accumulated usage from completed transcription events
        self._usage_input_tokens = 0
        self._usage_output_tokens = 0

    # ── Public interface ──

    def start(
        self,
        *,
        on_delta: Callable[[str], None],
        on_error: Callable[[str], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_speech: Callable[[bool], None] | None = None,
    ) -> None:
        """Start background thread, connect WebSocket, begin receiving events.

        Callbacks (all called on GTK thread via GLib.idle_add):
            on_delta(text)   — transcription text arrived
            on_error(msg)    — fatal API/connection error
            on_ready()       — session configured, accepting audio
            on_speech(active) — VAD speech_started (True) / speech_stopped (False)
        """
        self._on_delta = on_delta
        self._on_error = on_error
        self._on_ready = on_ready
        self._on_speech = on_speech
        self._running = True
        self._cancelled = False
        self._draining = False
        self._transcript = ""
        self._current_item_id = None

        self._loop = asyncio.new_event_loop()
        self._audio_queue = asyncio.Queue()
        self._done = asyncio.Event()

        self._chunk_count = 0

        logger.debug("start: launching WS thread")
        self._thread = threading.Thread(target=self._run, daemon=True, name="voxize-ws")
        self._thread.start()

    def send_audio(self, chunk: bytes) -> None:
        """Post an audio chunk from any thread (typically sounddevice callback)."""
        if self._loop and self._running and self._audio_queue is not None:
            self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, chunk)
            self._chunk_count += 1
            if self._chunk_count % 25 == 0:
                q = self._audio_queue
                logger.debug(
                    "send_audio: chunk %d queued (qsize=%d)",
                    self._chunk_count,
                    q.qsize() if q else -1,
                )

    def stop(self) -> str:
        """Stop gracefully: drain buffered audio, commit, wait for events.

        Sets _draining so the receive loop still accumulates transcript text
        but stops posting deltas to the GTK thread. Returns the accumulated
        transcript. Blocks the caller for at most ~15s.
        """
        self._draining = True
        self._running = False
        self._cancelled = False
        q = self._audio_queue
        logger.info("Stop called, queue backlog: %d chunks", q.qsize() if q else -1)
        self._signal_done()
        self._join()
        return self._transcript

    def cancel(self) -> str:
        """Cancel immediately: close WebSocket without waiting.

        Returns whatever transcript was accumulated so far.
        """
        logger.debug("cancel: requested")
        self._draining = True
        self._running = False
        self._cancelled = True
        self._signal_done()
        self._join()
        return self._transcript

    @property
    def usage(self) -> dict[str, int] | None:
        """Return accumulated transcription usage, or None if no data."""
        if self._usage_input_tokens == 0 and self._usage_output_tokens == 0:
            return None
        return {
            "input_tokens": self._usage_input_tokens,
            "output_tokens": self._usage_output_tokens,
        }

    # ── Thread management ──

    def _signal_done(self) -> None:
        logger.debug("_signal_done: signalling event loop")
        if self._loop and self._done is not None:
            self._loop.call_soon_threadsafe(self._done.set)
        if self._loop and self._audio_queue is not None:
            self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, None)

    def _join(self) -> None:
        if self._thread:
            # _session() can take up to 5s (send drain) + 5s (receive drain)
            self._thread.join(timeout=15.0)
        self._thread = None
        self._loop = None

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session())
        except Exception:
            logger.exception("WebSocket thread crashed")

    # ── Async session ──

    async def _session(self) -> None:
        from gi.repository import GLib

        # Open event log for debugging
        if self._session_dir:
            try:
                log_path = os.path.join(self._session_dir, "ws_events.jsonl")
                self._log_file = open(log_path, "w")  # noqa: SIM115
            except Exception:
                logger.exception("Failed to open ws_events.jsonl")

        logger.debug("_session: connecting to WS url=%s", _WS_URL)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "OpenAI-Beta": "realtime=v1",
        }
        try:
            async with websockets.connect(
                _WS_URL,
                additional_headers=headers,
                max_size=None,
            ) as ws:
                self._ws = ws
                logger.debug("_session: WS connected")
                await self._configure(ws)
                logger.debug("_session: configure sent")

                send_task = asyncio.create_task(self._send_loop(ws))
                recv_task = asyncio.create_task(self._receive_loop(ws))

                # Block until stop() or cancel() signals
                await self._done.wait()

                if not self._cancelled:
                    # Let the send loop drain remaining buffered audio to the
                    # None sentinel (no new chunks arrive — _running is False).
                    try:
                        await asyncio.wait_for(send_task, timeout=5.0)
                    except (TimeoutError, asyncio.CancelledError):
                        send_task.cancel()

                    # Commit the audio buffer so the API processes any trailing speech
                    try:  # noqa: SIM105
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "input_audio_buffer.commit",
                                }
                            )
                        )
                    except Exception:
                        pass
                    # Wait for final delta/completed events to drain
                    try:  # noqa: SIM105
                        await asyncio.wait_for(asyncio.shield(recv_task), timeout=5.0)
                    except (TimeoutError, asyncio.CancelledError):
                        pass
                else:
                    send_task.cancel()

                recv_task.cancel()
                try:  # noqa: SIM105
                    await recv_task
                except asyncio.CancelledError:
                    pass

                self._ws = None

        except websockets.exceptions.InvalidStatus as e:
            msg = f"WebSocket rejected (HTTP {e.response.status_code})"
            logger.error(msg)
            if self._on_error:
                GLib.idle_add(self._on_error, msg)
        except OSError as e:
            msg = f"Connection failed: {e}"
            logger.error(msg)
            if self._on_error:
                GLib.idle_add(self._on_error, msg)
        except Exception as e:
            msg = f"Transcription error: {e}"
            logger.error(msg)
            if self._on_error:
                GLib.idle_add(self._on_error, msg)
        finally:
            if self._log_file:
                self._log_file.close()
                self._log_file = None

    _VAD_CONFIG: ClassVar[dict[str, str]] = {
        "type": "semantic_vad",
        "eagerness": "low",
    }

    async def _configure(self, ws) -> None:
        """Configure session with semantic VAD enabled from the start.

        We use semantic_vad instead of server_vad to avoid the critical
        pacing sensitivity problem.  server_vad relies on silence-based
        timing to detect speech boundaries, and its internal clock loses
        alignment when audio is delivered faster than real-time (the
        startup burst).  This causes truncated segments, missed speech,
        or session-wide VAD corruption.

        semantic_vad detects speech boundaries based on semantic
        understanding of the user's utterance (whether they appear to
        have finished speaking), so it is not affected by delivery
        timing.  With eagerness="low", it waits for the user to finish
        speaking before chunking — ideal for dictation.

        The startup burst (audio buffered while the WS was connecting)
        is sent at full speed by the send loop, flowing into the same
        input audio buffer as the real-time audio that follows.  No
        manual commit, no VAD disable/re-enable dance, no artificial
        boundary between buffered and live audio.
        """
        await ws.send(
            json.dumps(
                {
                    "type": "transcription_session.update",
                    "session": {
                        "input_audio_format": "pcm16",
                        "input_audio_transcription": {
                            "model": _MODEL,
                            "language": "en",
                            **({"prompt": self._prompt} if self._prompt else {}),
                        },
                        "turn_detection": self._VAD_CONFIG,
                        "input_audio_noise_reduction": {
                            "type": "near_field",
                        },
                    },
                }
            )
        )

    async def _send_loop(self, ws) -> None:
        """Send audio chunks to the WebSocket.

        Semantic VAD is enabled from the start (set in _configure), so
        all audio — both the startup burst and real-time chunks — flows
        into the same input audio buffer as one continuous stream.

        The startup burst (buffered while the WS was connecting) is sent
        at full speed.  There is no manual commit and no VAD mode switch.
        The semantic VAD detects speech boundaries based on content, not
        delivery timing, so the burst does not corrupt its state.

        After the burst drains, chunks flow at mic-capture rate.
        """
        chunks_sent = 0
        exit_reason = "unknown"
        try:
            # ── Flush startup backlog at full speed ──
            startup = 0
            while not self._audio_queue.empty():
                try:
                    chunk = self._audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if chunk is None:
                    exit_reason = "sentinel"
                    return
                audio_b64 = base64.b64encode(chunk).decode("ascii")
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": audio_b64,
                        }
                    )
                )
                startup += 1
                chunks_sent += 1

            if startup > 0:
                logger.debug(
                    "_send_loop: burst sent %d startup chunks " "(%.1fs audio)",
                    startup,
                    startup * 0.04,
                )
            else:
                logger.debug("_send_loop: no startup backlog")

            # ── Continuous flow (startup + real-time, same VAD) ──
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self._audio_queue.get(),
                        timeout=0.1,
                    )
                except TimeoutError:
                    if not self._running:
                        exit_reason = "timeout/not-running"
                        break
                    continue
                if chunk is None:
                    exit_reason = "sentinel"
                    break
                audio_b64 = base64.b64encode(chunk).decode("ascii")
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": audio_b64,
                        }
                    )
                )
                chunks_sent += 1
                if chunks_sent == 1:
                    logger.debug("_send_loop: first chunk sent")
                elif chunks_sent % 25 == 0:
                    logger.debug(
                        "_send_loop: chunk %d sent (qsize=%d)",
                        chunks_sent,
                        self._audio_queue.qsize(),
                    )
        except asyncio.CancelledError:
            exit_reason = "cancelled"
        except websockets.exceptions.ConnectionClosed:
            exit_reason = "connection-closed"
        finally:
            logger.info(
                "Send loop exited: %d chunks sent (%.1fs audio)",
                chunks_sent,
                chunks_sent * 0.04,
            )
            logger.debug(
                "_send_loop: exit_reason=%s total=%d", exit_reason, chunks_sent
            )

    async def _receive_loop(self, ws) -> None:
        """Receive WebSocket events and dispatch delta text to GTK thread."""
        from gi.repository import GLib

        exit_reason = "end-of-stream"
        events_received = 0
        try:
            async for raw in ws:
                # Log every event for debugging
                if self._log_file:
                    try:
                        self._log_file.write(
                            raw if isinstance(raw, str) else raw.decode()
                        )
                        self._log_file.write("\n")
                        self._log_file.flush()
                    except Exception:
                        pass

                event = json.loads(raw)
                etype = event.get("type", "")
                events_received += 1

                if etype == "conversation.item.input_audio_transcription.delta":
                    item_id = event.get("item_id", "")
                    delta = event.get("delta", "")
                    logger.debug(
                        "_recv: type=%s item_id=%s delta_len=%d",
                        etype,
                        item_id,
                        len(delta),
                    )
                    if delta:
                        # Separate VAD speech turns with a newline
                        if self._current_item_id and item_id != self._current_item_id:
                            self._transcript += "\n"
                            if not self._draining:
                                GLib.idle_add(self._on_delta, "\n")
                        self._current_item_id = item_id
                        self._transcript += delta
                        # When draining, still accumulate but don't post to UI
                        if not self._draining:
                            GLib.idle_add(self._on_delta, delta)

                elif etype == "conversation.item.input_audio_transcription.completed":
                    item_id = event.get("item_id", "")
                    usage = event.get("usage")
                    if usage:
                        self._usage_input_tokens += usage.get("input_tokens", 0)
                        self._usage_output_tokens += usage.get("output_tokens", 0)
                    logger.debug("_recv: type=%s item_id=%s", etype, item_id)

                elif etype == "input_audio_buffer.speech_started":
                    logger.debug(
                        "_recv: type=%s audio_start_ms=%s",
                        etype,
                        event.get("audio_start_ms"),
                    )
                    if self._on_speech and not self._draining:
                        GLib.idle_add(self._on_speech, True)

                elif etype == "input_audio_buffer.speech_stopped":
                    logger.debug(
                        "_recv: type=%s audio_end_ms=%s",
                        etype,
                        event.get("audio_end_ms"),
                    )
                    if self._on_speech and not self._draining:
                        GLib.idle_add(self._on_speech, False)

                elif etype == "transcription_session.updated":
                    logger.debug("_recv: type=%s", etype)
                    if self._on_ready and not self._draining:
                        GLib.idle_add(self._on_ready)

                elif etype == "error":
                    error = event.get("error", {})
                    code = error.get("code", "")
                    msg = error.get("message", "Unknown API error")
                    logger.debug("_recv: type=%s code=%s", etype, code)
                    # Non-fatal errors: empty buffer commit from VAD auto-processing
                    if code == "input_audio_buffer_commit_empty":
                        logger.debug("Ignored non-fatal API error: %s", msg)
                    else:
                        logger.error("API error: %s", msg)
                        if self._on_error:
                            GLib.idle_add(self._on_error, msg)

                else:
                    logger.debug("_recv: type=%s", etype)

        except asyncio.CancelledError:
            exit_reason = "cancelled"
        except websockets.exceptions.ConnectionClosed:
            exit_reason = "connection-closed"
        finally:
            logger.debug(
                "_receive_loop: exit_reason=%s events=%d transcript_len=%d",
                exit_reason,
                events_received,
                len(self._transcript),
            )
