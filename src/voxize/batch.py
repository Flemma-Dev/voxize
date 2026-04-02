"""Batch transcription via OpenAI Audio API.

Sends a WAV file to POST /audio/transcriptions with streaming. Runs a
synchronous OpenAI SDK call in a daemon thread. Delta text is posted to
the GTK main thread via GLib.idle_add.

Lifecycle:
    b = BatchTranscription(api_key, session_dir=...)
    b.start(wav_path=..., on_delta=..., on_complete=..., on_error=...)
    b.cancel()
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable

from voxize.audio import CHANNELS, SAMPLE_RATE, WAV_HEADER_SIZE

logger = logging.getLogger(__name__)

_MODEL = "gpt-4o-transcribe"
_WAV_BYTE_RATE = SAMPLE_RATE * CHANNELS * 2


class BatchTranscription:
    """Transcribes a WAV file via the OpenAI batch transcription API."""

    def __init__(
        self,
        api_key: str,
        session_dir: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._session_dir = session_dir
        self._thread: threading.Thread | None = None
        self._cancelled = False
        self._usage: dict[str, int] | None = None

    def start(
        self,
        wav_path: str,
        on_delta: Callable[[str], None],
        on_complete: Callable[[str], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """Start batch transcription in a background thread.

        Args:
            wav_path: Path to the WAV file to transcribe.
            on_delta: Called on GTK thread with each streaming text chunk.
            on_complete: Called on GTK thread with the full transcript.
            on_error: Called on GTK thread with error message on failure.
        """
        logger.debug("start: wav_path=%s", wav_path)
        self._cancelled = False
        self._thread = threading.Thread(
            target=self._run,
            args=(wav_path, on_delta, on_complete, on_error),
            daemon=True,
            name="voxize-batch",
        )
        self._thread.start()

    @property
    def usage(self) -> dict[str, int] | None:
        """Return batch usage (input/output tokens), or None if unavailable."""
        return self._usage

    def cancel(self) -> None:
        """Cancel batch transcription. The thread will exit on the next chunk."""
        logger.debug("cancel: requested")
        self._cancelled = True

    def _run(
        self,
        wav_path: str,
        on_delta: Callable[[str], None],
        on_complete: Callable[[str], None],
        on_error: Callable[[str], None] | None,
    ) -> None:
        from gi.repository import GLib
        from openai import OpenAI

        log_file = None
        if self._session_dir:
            try:
                log_file = open(  # noqa: SIM115
                    os.path.join(self._session_dir, "batch_events.jsonl"), "w"
                )
            except Exception:
                logger.debug("Failed to open batch_events.jsonl", exc_info=True)

        def _log_event(event) -> None:
            if log_file:
                try:
                    log_file.write(
                        event.model_dump_json()
                        if hasattr(event, "model_dump_json")
                        else json.dumps(event)
                    )
                    log_file.write("\n")
                    log_file.flush()
                except Exception:
                    pass

        client = OpenAI(api_key=self._api_key)
        accumulated: list[str] = []
        wav_file = None

        try:
            wav_size = os.path.getsize(wav_path)
            audio_duration = (wav_size - WAV_HEADER_SIZE) / _WAV_BYTE_RATE
            logger.debug(
                "_run: calling API model=%s wav_size=%d (%.1fs audio)",
                _MODEL,
                wav_size,
                audio_duration,
            )
            _log_event(
                {
                    "type": "request",
                    "model": _MODEL,
                    "wav_path": wav_path,
                    "wav_size": wav_size,
                }
            )

            wav_file = open(wav_path, "rb")  # noqa: SIM115 — closed in finally
            try:
                stream = client.audio.transcriptions.create(
                    model=_MODEL,
                    file=wav_file,
                    response_format="text",
                    stream=True,
                )
            except Exception:
                wav_file.close()
                raise

            first_delta = True
            events_received = 0
            for event in stream:
                _log_event(event)
                events_received += 1
                if self._cancelled:
                    logger.debug(
                        "_run: cancelled during streaming, events=%d",
                        events_received,
                    )
                    stream.close()
                    return
                if event.type == "transcript.text.delta":
                    text = event.delta
                    accumulated.append(text)
                    if first_delta:
                        logger.debug("_run: first delta received, len=%d", len(text))
                        first_delta = False
                    GLib.idle_add(on_delta, text)
                elif event.type == "transcript.text.done":
                    usage = getattr(event, "usage", None)
                    if usage:
                        self._usage = {
                            "input_tokens": getattr(usage, "input_tokens", 0),
                            "output_tokens": getattr(usage, "output_tokens", 0),
                        }
                        logger.debug(
                            "_run: usage input_tokens=%d output_tokens=%d",
                            self._usage["input_tokens"],
                            self._usage["output_tokens"],
                        )

            if not self._cancelled:
                transcript = "".join(accumulated)
                logger.debug(
                    "_run: complete, transcript_len=%d events=%d",
                    len(transcript),
                    events_received,
                )
                GLib.idle_add(on_complete, transcript)

        except Exception as e:
            if not self._cancelled:
                msg = f"Batch transcription failed: {e}"
                logger.error(msg)
                _log_event({"type": "error", "message": msg})
                if on_error:
                    GLib.idle_add(on_error, msg)
                else:
                    transcript = "".join(accumulated)
                    GLib.idle_add(on_complete, transcript)
        finally:
            if wav_file:
                wav_file.close()
            if log_file:
                log_file.close()
