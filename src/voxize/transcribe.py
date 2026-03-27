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

from typing import Callable

import websockets

logger = logging.getLogger(__name__)


_WS_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
_MODEL = "gpt-4o-transcribe"


class RealtimeTranscription:
    """Streams audio to OpenAI Realtime API and accumulates transcript."""

    def __init__(self, api_key: str, session_dir: str | None = None) -> None:
        self._api_key = api_key
        self._session_dir = session_dir
        self._on_delta: Callable[[str], None] | None = None
        self._on_error: Callable[[str], None] | None = None

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

    # ── Public interface ──

    def start(
        self,
        *,
        on_delta: Callable[[str], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """Start background thread, connect WebSocket, begin receiving events."""
        self._on_delta = on_delta
        self._on_error = on_error
        self._running = True
        self._cancelled = False
        self._draining = False
        self._transcript = ""
        self._current_item_id = None

        self._loop = asyncio.new_event_loop()
        self._audio_queue = asyncio.Queue()
        self._done = asyncio.Event()

        self._thread = threading.Thread(target=self._run, daemon=True, name="voxize-ws")
        self._thread.start()

    def send_audio(self, chunk: bytes) -> None:
        """Post an audio chunk from any thread (typically sounddevice callback)."""
        if self._loop and self._running and self._audio_queue is not None:
            self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, chunk)

    def stop(self) -> str:
        """Stop gracefully: drain buffered audio, commit, wait for events.

        Sets _draining so the receive loop still accumulates transcript text
        but stops posting deltas to the GTK thread. Returns the accumulated
        transcript. Blocks the caller for at most ~15s.
        """
        self._draining = True
        self._running = False
        self._cancelled = False
        self._signal_done()
        self._join()
        return self._transcript

    def cancel(self) -> str:
        """Cancel immediately: close WebSocket without waiting.

        Returns whatever transcript was accumulated so far.
        """
        self._draining = True
        self._running = False
        self._cancelled = True
        self._signal_done()
        self._join()
        return self._transcript

    # ── Thread management ──

    def _signal_done(self) -> None:
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
                await self._configure(ws)

                send_task = asyncio.create_task(self._send_loop(ws))
                recv_task = asyncio.create_task(self._receive_loop(ws))

                # Block until stop() or cancel() signals
                await self._done.wait()

                if not self._cancelled:
                    # Let the send loop drain remaining buffered audio to the
                    # None sentinel (no new chunks arrive — _running is False).
                    try:
                        await asyncio.wait_for(send_task, timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        send_task.cancel()

                    # Commit the audio buffer so the API processes any trailing speech
                    try:
                        await ws.send(json.dumps({
                            "type": "input_audio_buffer.commit",
                        }))
                    except Exception:
                        pass
                    # Wait for final delta/completed events to drain
                    try:
                        await asyncio.wait_for(asyncio.shield(recv_task), timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                else:
                    send_task.cancel()

                recv_task.cancel()
                try:
                    await recv_task
                except asyncio.CancelledError:
                    pass

                self._ws = None

        except websockets.exceptions.InvalidStatusCode as e:
            msg = f"WebSocket rejected (HTTP {e.status_code})"
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

    async def _configure(self, ws) -> None:
        await ws.send(json.dumps({
            "type": "transcription_session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": _MODEL,
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                },
                "input_audio_noise_reduction": {
                    "type": "near_field",
                },
            },
        }))

    async def _send_loop(self, ws) -> None:
        """Drain audio queue and send base64-encoded chunks to WebSocket.

        When the queue has a backlog (buffered audio from before WS connected),
        chunks are paced at ~5x real-time (8ms sleep per 40ms chunk) to avoid
        overwhelming the server VAD. Once caught up, chunks flow at real-time
        rate with no artificial delay.

        Pacing follows OpenAI's own cookbook recommendation: their example sends
        128ms chunks with 25ms sleeps (~5x real-time) with the comment "Add
        pacing to ensure real-time transcription."
        """
        # Each chunk is 40ms of audio. At 5x real-time: 40ms / 5 = 8ms pacing.
        _PACE_DELAY = 0.008

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self._audio_queue.get(), timeout=0.1,
                    )
                except asyncio.TimeoutError:
                    # No chunk available — exit if we're shutting down,
                    # otherwise keep waiting for live mic chunks.
                    if not self._running:
                        break
                    continue
                if chunk is None:  # sentinel from stop/cancel
                    break
                audio_b64 = base64.b64encode(chunk).decode("ascii")
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": audio_b64,
                }))
                # Pace when draining a backlog (queue > 0 means more buffered)
                if not self._audio_queue.empty():
                    await asyncio.sleep(_PACE_DELAY)
        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _receive_loop(self, ws) -> None:
        """Receive WebSocket events and dispatch delta text to GTK thread."""
        from gi.repository import GLib
        try:
            async for raw in ws:
                # Log every event for debugging
                if self._log_file:
                    try:
                        self._log_file.write(raw if isinstance(raw, str) else raw.decode())
                        self._log_file.write("\n")
                        self._log_file.flush()
                    except Exception:
                        pass

                event = json.loads(raw)
                etype = event.get("type", "")

                if etype == "conversation.item.input_audio_transcription.delta":
                    item_id = event.get("item_id", "")
                    delta = event.get("delta", "")
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
                    pass  # already accumulated via deltas

                elif etype == "error":
                    error = event.get("error", {})
                    code = error.get("code", "")
                    msg = error.get("message", "Unknown API error")
                    # Non-fatal errors: empty buffer commit from VAD auto-processing
                    if code == "input_audio_buffer_commit_empty":
                        logger.debug("Ignored non-fatal API error: %s", msg)
                    else:
                        logger.error("API error: %s", msg)
                        if self._on_error:
                            GLib.idle_add(self._on_error, msg)

                # Silently ignore expected lifecycle events
        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed:
            pass
