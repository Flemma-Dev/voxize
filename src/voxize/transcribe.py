"""OpenAI Realtime Transcription API — WebSocket client (live preview).

Runs an asyncio event loop in a background thread. Audio chunks are posted
from the sounddevice callback thread via loop.call_soon_threadsafe. Delta
text is delivered to the GTK main thread via GLib.idle_add.

This is a throwaway live preview — the batch transcription pass (batch.py)
produces the authoritative transcript. The live preview uses a cheaper model
and server_vad, and accuracy is not critical.

Lifecycle:
    t = RealtimeTranscription(api_key, session_dir)
    t.start(on_delta=..., on_error=...)
    # AudioCapture callback calls t.send_audio(chunk) on sounddevice thread
    t.stop()    # immediate close, no drain (live transcript is throwaway)
    t.cancel()  # same as stop
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
from collections.abc import Callable

import websockets

logger = logging.getLogger(__name__)


_WS_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
_MODEL = "gpt-4o-mini-transcribe"


class RealtimeTranscription:
    """Streams audio to OpenAI Realtime API for live preview."""

    def __init__(
        self,
        api_key: str,
        session_dir: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._session_dir = session_dir
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
        self._data_ready = threading.Event()

        self._transcript = ""
        self._current_item_id: str | None = None
        self._running = False
        self._cancelled = False

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
            on_delta(text)   -- transcription text arrived
            on_error(msg)    -- WS/API error (non-fatal during RECORDING)
            on_ready()       -- session configured, WS accepting audio
            on_speech(active) -- VAD speech_started (True) / speech_stopped (False)
        """
        self._on_delta = on_delta
        self._on_error = on_error
        self._on_ready = on_ready
        self._on_speech = on_speech
        self._running = True
        self._cancelled = False
        self._transcript = ""
        self._current_item_id = None

        self._loop = asyncio.new_event_loop()
        self._audio_queue = asyncio.Queue()
        self._done = asyncio.Event()
        self._data_ready = threading.Event()

        logger.debug("start: launching WS thread")
        self._thread = threading.Thread(target=self._run, daemon=True, name="voxize-ws")
        self._thread.start()

    def send_audio(self, chunk: bytes) -> None:
        """Post an audio chunk from any thread (typically sounddevice callback)."""
        if self._loop and self._running and self._audio_queue is not None:
            self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, chunk)

    def stop(self) -> None:
        """Stop immediately — the live transcript is throwaway.

        Signals cancellation and waits for transcript/usage to stabilise
        (tasks cancelled), but does NOT block on the WS close handshake
        which can take seconds.  The daemon thread finishes on its own.
        """
        logger.debug("stop: requested")
        self._running = False
        self._cancelled = True
        self._signal_done()
        self._data_ready.wait(timeout=2.0)

    def cancel(self) -> None:
        """Cancel immediately — same as stop for the live preview."""
        self.stop()

    @property
    def transcript(self) -> str:
        """Return the accumulated live preview transcript."""
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
            self._thread.join(timeout=5.0)
        self._thread = None
        self._loop = None

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session())
        except Exception:
            logger.exception("WebSocket thread crashed")
        finally:
            self._data_ready.set()  # safety net

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

                send_task.cancel()
                recv_task.cancel()
                try:  # noqa: SIM105
                    await recv_task
                except asyncio.CancelledError:
                    pass

                # Transcript + usage are stable — unblock stop() before
                # the WS close handshake (which can take seconds).
                self._data_ready.set()

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
            self._data_ready.set()  # ensure signal on error paths
            if self._log_file:
                self._log_file.close()
                self._log_file = None

    async def _configure(self, ws) -> None:
        """Configure session with server_vad on the mini model.

        This is a throwaway live preview — accuracy is not critical.
        server_vad may over-segment or garble text, especially after
        the startup burst, but the batch transcription pass produces
        the authoritative result.
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
                        },
                        "turn_detection": {
                            "type": "server_vad",
                        },
                        "input_audio_noise_reduction": {
                            "type": "near_field",
                        },
                    },
                }
            )
        )

    async def _send_loop(self, ws) -> None:
        """Send audio chunks to the WebSocket.

        Audio capture starts before the WS connects, so there may be
        a startup backlog. The send loop drains it at full speed, then
        continues with real-time chunks. Burst delivery may corrupt
        server_vad — that's acceptable for a throwaway preview.
        """
        chunks_sent = 0
        exit_reason = "unknown"
        try:
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
                            GLib.idle_add(self._on_delta, "\n")
                        self._current_item_id = item_id
                        self._transcript += delta
                        GLib.idle_add(self._on_delta, delta)

                elif etype == "conversation.item.input_audio_transcription.completed":
                    usage = event.get("usage")
                    if usage:
                        self._usage_input_tokens += usage.get("input_tokens", 0)
                        self._usage_output_tokens += usage.get("output_tokens", 0)
                    logger.debug("_recv: type=%s", etype)

                elif etype == "input_audio_buffer.speech_started":
                    logger.debug(
                        "_recv: type=%s audio_start_ms=%s",
                        etype,
                        event.get("audio_start_ms"),
                    )
                    if self._on_speech:
                        GLib.idle_add(self._on_speech, True)

                elif etype == "input_audio_buffer.speech_stopped":
                    logger.debug(
                        "_recv: type=%s audio_end_ms=%s",
                        etype,
                        event.get("audio_end_ms"),
                    )
                    if self._on_speech:
                        GLib.idle_add(self._on_speech, False)

                elif etype == "transcription_session.updated":
                    logger.debug("_recv: type=%s", etype)
                    if self._on_ready:
                        GLib.idle_add(self._on_ready)

                elif etype == "error":
                    error = event.get("error", {})
                    code = error.get("code", "")
                    msg = error.get("message", "Unknown API error")
                    logger.debug("_recv: type=%s code=%s", etype, code)
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
