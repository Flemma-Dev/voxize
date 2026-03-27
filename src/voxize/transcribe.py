"""OpenAI Realtime Transcription API — WebSocket client.

Runs an asyncio event loop in a background thread. Audio chunks are posted
from the sounddevice callback thread via loop.call_soon_threadsafe. Delta
text is delivered to the GTK main thread via GLib.idle_add.

Lifecycle:
    t = RealtimeTranscription(api_key)
    t.start(on_delta=..., on_ready=..., on_error=...)
    # AudioCapture callback calls t.send_audio(chunk) on sounddevice thread
    transcript = t.stop()   # graceful: commit + brief wait
    transcript = t.cancel() # immediate close
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading

from typing import Callable

import websockets

logger = logging.getLogger(__name__)

_WS_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
_MODEL = "gpt-4o-transcribe"


class RealtimeTranscription:
    """Streams audio to OpenAI Realtime API and accumulates transcript."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._on_delta: Callable[[str], None] | None = None
        self._on_ready: Callable[[], None] | None = None
        self._on_error: Callable[[str], None] | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws = None
        self._audio_queue: asyncio.Queue | None = None
        self._done: asyncio.Event | None = None

        self._transcript = ""
        self._running = False
        self._cancelled = False

    # ── Public interface ──

    def start(
        self,
        *,
        on_delta: Callable[[str], None],
        on_ready: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """Start background thread, connect WebSocket, begin receiving events."""
        self._on_delta = on_delta
        self._on_ready = on_ready
        self._on_error = on_error
        self._running = True
        self._cancelled = False
        self._transcript = ""

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
        """Stop gracefully: commit audio buffer, wait briefly for final events.

        Returns the accumulated transcript. Blocks the caller (GTK thread)
        for at most ~2 seconds while the async thread shuts down.
        """
        self._running = False
        self._cancelled = False
        self._signal_done()
        self._join()
        return self._transcript

    def cancel(self) -> str:
        """Cancel immediately: close WebSocket without waiting.

        Returns whatever transcript was accumulated so far.
        """
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
            self._thread.join(timeout=5.0)
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

                if self._on_ready:
                    GLib.idle_add(self._on_ready)

                send_task = asyncio.create_task(self._send_loop(ws))
                recv_task = asyncio.create_task(self._receive_loop(ws))

                # Block until stop() or cancel() signals
                await self._done.wait()

                # Stop sending audio
                send_task.cancel()

                if not self._cancelled:
                    # Commit the audio buffer so the API processes any trailing speech
                    try:
                        await ws.send(json.dumps({
                            "type": "input_audio_buffer.commit",
                        }))
                    except Exception:
                        pass
                    # Brief wait for final delta/completed events
                    try:
                        await asyncio.wait_for(asyncio.shield(recv_task), timeout=1.5)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass

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
            },
        }))

    async def _send_loop(self, ws) -> None:
        """Drain audio queue and send base64-encoded chunks to WebSocket."""
        try:
            while self._running:
                try:
                    chunk = await asyncio.wait_for(
                        self._audio_queue.get(), timeout=0.1,
                    )
                except asyncio.TimeoutError:
                    continue
                if chunk is None:  # sentinel from stop/cancel
                    break
                audio_b64 = base64.b64encode(chunk).decode("ascii")
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": audio_b64,
                }))
        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _receive_loop(self, ws) -> None:
        """Receive WebSocket events and dispatch delta text to GTK thread."""
        from gi.repository import GLib
        try:
            async for raw in ws:
                event = json.loads(raw)
                etype = event.get("type", "")

                if etype == "conversation.item.input_audio_transcription.delta":
                    delta = event.get("delta", "")
                    if delta:
                        self._transcript += delta
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
