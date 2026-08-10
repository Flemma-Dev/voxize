"""Batch transcription via OpenAI Audio API.

Sends an audio file to POST /audio/transcriptions with streaming. Runs
a synchronous OpenAI SDK call in a daemon thread. Delta text is posted
to the GTK main thread via GLib.idle_add.

Lifecycle:
    b = BatchTranscription(client, prompts=..., session_dir=...)
    b.start(wav_path=..., on_delta=..., on_complete=..., on_error=...)
    b.cancel()
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from voxize._audio_const import CHANNELS, SAMPLE_RATE, WAV_HEADER_SIZE
from voxize.prompt import build_transcription_prompt

if TYPE_CHECKING:
    from openai import OpenAI

    from voxize.prompt import PromptSource

logger = logging.getLogger(__name__)

_MODEL = "gpt-transcribe"
_WAV_BYTE_RATE = SAMPLE_RATE * CHANNELS * 2


class _ProgressReader(io.RawIOBase):
    """Wraps a file object to report read progress via a callback."""

    def __init__(
        self,
        fp: io.RawIOBase,
        total: int,
        on_progress: Callable[[float], None],
    ) -> None:
        self._fp = fp
        self._total = total
        self._read = 0
        self._on_progress = on_progress

    def readinto(self, b):
        n = self._fp.readinto(b)
        if n and n > 0:
            self._read += n
            self._on_progress(self._read / self._total if self._total else 1.0)
        return n

    def readable(self):
        return True

    @property
    def name(self):
        return self._fp.name


class BatchTranscription:
    """Transcribes a WAV file via the OpenAI batch transcription API."""

    def __init__(
        self,
        client: OpenAI,
        prompts: list[PromptSource] | None = None,
        session_dir: str | None = None,
    ) -> None:
        self._client = client
        self._prompts = prompts or []
        self._session_dir = session_dir
        self._thread: threading.Thread | None = None
        self._cancelled = False
        self._usage: dict[str, int] | None = None

    def _build_prompt(self) -> str | None:
        """Return the guidance content sent as the API's ``prompt``.

        Delegates to ``prompt.build_transcription_prompt`` so this, the
        archived ``prompt.txt``, and recover.sh cannot drift apart.

        The separate ``keywords`` field was measured to add nothing over
        this on real session audio (see the 2026-07-31 journal entry), and
        it rejects the entire request on a formatting violation, so it is
        deliberately unused.

        gpt-4o-transcribe could not be given a prompt at all — it transcribed
        the prompt text instead of the audio. gpt-transcribe does not.
        """
        return build_transcription_prompt(self._prompts)

    def start(
        self,
        wav_path: str,
        on_delta: Callable[[str], None],
        on_complete: Callable[[str], None],
        on_error: Callable[[str], None] | None = None,
        on_upload_progress: Callable[[float], None] | None = None,
    ) -> None:
        """Start batch transcription in a background thread.

        Args:
            wav_path: Path to the audio file to transcribe.
            on_delta: Called on GTK thread with each streaming text chunk.
            on_complete: Called on GTK thread with the full transcript.
            on_error: Called on GTK thread with error message on failure.
            on_upload_progress: Called on GTK thread with fraction [0, 1].
        """
        logger.debug("start: wav_path=%s", wav_path)
        self._cancelled = False
        self._thread = threading.Thread(
            target=self._run,
            args=(wav_path, on_delta, on_complete, on_error, on_upload_progress),
            daemon=True,
            name="voxize-batch",
        )
        self._thread.start()

    @property
    def usage(self) -> dict[str, int] | None:
        """Return batch usage, or None if unavailable.

        gpt-transcribe is billed by audio duration, so this is
        ``{"seconds": N}`` rather than the token counts the gpt-4o-transcribe
        era reported.
        """
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
        on_upload_progress: Callable[[float], None] | None = None,
    ) -> None:
        from gi.repository import GLib

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

        accumulated: list[str] = []
        raw_file = None
        wav_file = None
        t_call = 0.0
        t_first_delta = 0.0

        try:
            file_size = os.path.getsize(wav_path)
            if wav_path.endswith(".wav"):
                audio_duration = (file_size - WAV_HEADER_SIZE) / _WAV_BYTE_RATE
            else:
                audio_duration = None
            prompt = self._build_prompt()
            logger.debug(
                "_run: calling API model=%s file=%s size=%d (%.1fs audio) prompt_len=%s",
                _MODEL,
                os.path.basename(wav_path),
                file_size,
                audio_duration or 0,
                len(prompt) if prompt else 0,
            )
            _log_event(
                {
                    "type": "request",
                    "model": _MODEL,
                    "wav_path": wav_path,
                    "wav_size": file_size,
                    "prompt": prompt,
                }
            )

            raw_file = open(wav_path, "rb")  # noqa: SIM115 — closed in finally
            if on_upload_progress:
                wav_file = _ProgressReader(
                    raw_file,
                    file_size,
                    lambda frac: GLib.idle_add(on_upload_progress, frac),
                )
            else:
                wav_file = raw_file
            t_call = time.monotonic()
            extra = {"prompt": prompt} if prompt else {}
            # `with` ensures stream.close() fires before we hand off to the
            # cleanup phase, so the pooled TLS socket is returned to httpx
            # before Cleanup's thread asks for one.
            with self._client.audio.transcriptions.create(
                model=_MODEL,
                file=wav_file,
                response_format="text",
                stream=True,
                **extra,
            ) as stream:
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
                        return
                    if event.type == "transcript.text.delta":
                        text = event.delta
                        accumulated.append(text)
                        if first_delta:
                            t_first_delta = time.monotonic()
                            logger.debug(
                                "_run: first delta received, len=%d", len(text)
                            )
                            first_delta = False
                        GLib.idle_add(on_delta, text)
                    elif event.type == "transcript.text.done":
                        usage = getattr(event, "usage", None)
                        if usage:
                            # gpt-transcribe reports
                            # {"type": "duration", "seconds": N}. Fall back to
                            # the WAV-derived duration if the field is missing
                            # so the cost line degrades rather than disappears.
                            seconds = getattr(usage, "seconds", None)
                            fallback = round(audio_duration) if audio_duration else 0
                            self._usage = {
                                "seconds": int(
                                    seconds if seconds is not None else fallback
                                )
                            }
                            logger.debug(
                                "_run: usage seconds=%d reported=%s",
                                self._usage["seconds"],
                                seconds is not None,
                            )

            if not self._cancelled:
                transcript = "".join(accumulated)
                t_end = time.monotonic()
                ttft_ms = int((t_first_delta - t_call) * 1000) if t_first_delta else 0
                streaming_ms = (
                    int((t_end - t_first_delta) * 1000) if t_first_delta else 0
                )
                total_ms = int((t_end - t_call) * 1000)
                billed_secs = self._usage["seconds"] if self._usage else 0
                logger.debug(
                    "phase_timing: ttft_ms=%d streaming_ms=%d total_ms=%d "
                    "billed_seconds=%d chars=%d",
                    ttft_ms,
                    streaming_ms,
                    total_ms,
                    billed_secs,
                    len(transcript),
                )
                logger.debug(
                    "_run: complete, transcript_len=%d events=%d",
                    len(transcript),
                    events_received,
                )
                GLib.idle_add(on_complete, transcript)

        except Exception as e:
            if not self._cancelled:
                msg = f"Batch transcription failed: {e}"
                logger.error(msg, exc_info=True)
                _log_event({"type": "error", "message": msg})
                if on_error:
                    GLib.idle_add(on_error, msg)
                else:
                    transcript = "".join(accumulated)
                    GLib.idle_add(on_complete, transcript)
        finally:
            if wav_file:
                wav_file.close()
            if raw_file and raw_file is not wav_file:
                raw_file.close()
            if log_file:
                log_file.close()
