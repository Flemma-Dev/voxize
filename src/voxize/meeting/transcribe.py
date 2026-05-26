"""Post-meeting transcription + diarization via ElevenLabs Scribe v2.

Uploads the session's ``recording.opus`` (downmixed to mono) to the
ElevenLabs batch speech-to-text endpoint. Returns a speaker-labeled,
timestamped transcript with word-level detail.

On success three files are written to the session directory:

- ``transcript.txt``  — speaker-labeled turns with timestamps (LLM-ready)
- ``transcript.json`` — full word-level API response
- ``transcribe_params.json`` — parameters used (for re-opening the UI)

File presence is state: ``transcript.txt`` existing means "done".
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import gi
import httpx

logger = logging.getLogger(__name__)

gi.require_version("Secret", "1")

from gi.repository import Secret  # noqa: E402

_GENERIC_SCHEMA = Secret.Schema.new(
    "org.freedesktop.Secret.Generic",
    Secret.SchemaFlags.DONT_MATCH_NAME,
    {
        "service": Secret.SchemaAttributeType.STRING,
        "key": Secret.SchemaAttributeType.STRING,
    },
)

_API_URL = "https://api.elevenlabs.io/v1/speech-to-text"
_MODEL_ID = "scribe_v2"
_PROGRESS_TICK_S = 0.25
_FFMPEG_POLL_S = 0.25
_HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=300.0, pool=30.0)
_ABORT_POLL_S = 0.5


@dataclass
class TranscribeParams:
    num_speakers: int = 2
    keyterms: list[str] = field(default_factory=list)
    language_code: str = "eng"


@dataclass
class TranscribeResult:
    success: bool
    error_reason: str | None
    elapsed_s: float
    audio_duration_s: float | None


def transcribe_meeting(
    opus_path: str,
    session_dir: str,
    params: TranscribeParams,
    stop_event: threading.Event,
    on_progress: Callable[[str, float], None] | None = None,
) -> TranscribeResult:
    """Transcribe + diarize a meeting recording via ElevenLabs Scribe v2.

    ``stop_event`` is polled during ffmpeg downmix and the HTTP wait.
    ``on_progress(phase, elapsed_s)`` is called every ~250 ms so the UI
    can show a live timer with the current phase (``"downmix"`` or
    ``"transcribe"``).
    """
    start_t = time.monotonic()

    if not os.path.isfile(opus_path):
        return _result(False, "recording not found", start_t, None)

    try:
        api_key = _get_api_key()
    except RuntimeError as e:
        return _result(False, str(e), start_t, None)

    if stop_event.is_set():
        return _result(False, "aborted", start_t, None)

    phase: list[str] = ["downmix"]
    progress_stop = threading.Event()
    progress_thread = None
    if on_progress is not None:
        progress_thread = threading.Thread(
            target=_progress_loop,
            args=(on_progress, progress_stop, start_t, phase),
            daemon=True,
            name="meeting-transcribe-progress",
        )
        progress_thread.start()

    mono_path = None
    try:
        mono_path = _downmix_to_mono(opus_path, stop_event)
        if mono_path is None:
            return _result(False, "aborted", start_t, None)

        if stop_event.is_set():
            return _result(False, "aborted", start_t, None)

        phase[0] = "transcribe"

        logger.info(
            "transcribe: uploading %s (mono=%s speakers=%d lang=%s)",
            opus_path,
            mono_path,
            params.num_speakers,
            params.language_code,
        )

        result_data = _upload(mono_path, params, api_key, stop_event)
        if result_data is None:
            return _result(False, "aborted", start_t, None)

        audio_duration = result_data.get("audio_duration_secs")
        _save_results(session_dir, result_data, params)

        logger.info(
            "transcribe: success duration=%.1fs elapsed=%.1fs",
            audio_duration or 0.0,
            time.monotonic() - start_t,
        )
        return _result(True, None, start_t, audio_duration)

    except httpx.HTTPStatusError as e:
        reason = f"API error {e.response.status_code}"
        try:
            body = e.response.json()
            detail = body.get("detail", body.get("message", ""))
            if detail:
                reason = f"{reason}: {detail}"
        except Exception:
            pass
        logger.error("transcribe: %s", reason)
        return _result(False, reason, start_t, None)

    except httpx.HTTPError as e:
        reason = f"HTTP error: {e}"
        logger.error("transcribe: %s", reason)
        return _result(False, reason, start_t, None)

    except Exception:
        logger.exception("transcribe: unexpected error")
        return _result(False, "unexpected error", start_t, None)

    finally:
        if progress_thread is not None:
            progress_stop.set()
            progress_thread.join(timeout=1.0)
        if mono_path:
            with contextlib.suppress(OSError):
                os.unlink(mono_path)


# ── Internals ──


def _get_api_key() -> str:
    password = Secret.password_lookup_sync(
        _GENERIC_SCHEMA,
        {"service": "elevenlabs", "key": "api"},
        None,
    )
    if not password:
        raise RuntimeError(
            "ElevenLabs API key not found in keyring "
            "(set with: secret-tool store --label='ElevenLabs API Key' "
            "service elevenlabs key api)"
        )
    return password


def _downmix_to_mono(
    opus_path: str,
    stop_event: threading.Event,
) -> str | None:
    """Downmix stereo opus to mono via ffmpeg. Returns temp path or None if aborted."""
    fd, tmp_path = tempfile.mkstemp(suffix=".opus")
    os.close(fd)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-y",
        "-i",
        opus_path,
        "-ac",
        "1",
        "-c:a",
        "libopus",
        "-b:a",
        "48k",
        tmp_path,
    ]
    logger.debug("transcribe: downmix cmd=%s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as e:
        logger.error("transcribe: ffmpeg not found")
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise RuntimeError("ffmpeg not found — install ffmpeg") from e

    stderr_lines: list[str] = []
    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(proc, stderr_lines, "downmix"),
        daemon=True,
        name="meeting-downmix-err",
    )
    stderr_thread.start()

    aborted = _wait_for_subprocess(proc, stop_event, tmp_path)
    stderr_thread.join(timeout=1.0)

    if aborted:
        return None

    if proc.returncode != 0:
        tail = "\n".join(stderr_lines[-8:]) or "(no stderr)"
        logger.error("transcribe: downmix failed rc=%d\n%s", proc.returncode, tail)
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise RuntimeError(f"ffmpeg downmix failed (exit {proc.returncode})")

    logger.debug("transcribe: downmix done %s", tmp_path)
    return tmp_path


def _upload(
    mono_path: str,
    params: TranscribeParams,
    api_key: str,
    stop_event: threading.Event,
) -> dict | None:
    """Upload mono file to ElevenLabs. Returns response dict or None if aborted.

    The HTTP request runs in a daemon thread so ``stop_event`` can abort the
    wait without blocking until the server responds (which can take 5-20 min
    for long recordings).
    """
    additional_formats = json.dumps(
        [
            {
                "format": "txt",
                "include_speakers": True,
                "include_timestamps": True,
            },
        ]
    )

    fields: list[tuple[str, tuple[None, str] | tuple[str, bytes, str]]] = [
        ("model_id", (None, _MODEL_ID)),
        ("diarize", (None, "true")),
        ("no_verbatim", (None, "true")),
        ("tag_audio_events", (None, "true")),
        ("language_code", (None, params.language_code)),
        ("timestamps_granularity", (None, "word")),
        ("additional_formats", (None, additional_formats)),
    ]
    if params.num_speakers > 0:
        fields.append(("num_speakers", (None, str(params.num_speakers))))
    for term in params.keyterms:
        fields.append(("keyterms", (None, term)))

    result_holder: list = [None, None]  # [response_dict, exception]

    def do_request() -> None:
        try:
            with open(mono_path, "rb") as f:
                all_fields = [*fields, ("file", ("recording.opus", f, "audio/opus"))]
                resp = httpx.post(
                    _API_URL,
                    headers={"xi-api-key": api_key},
                    files=all_fields,
                    timeout=_HTTP_TIMEOUT,
                )
            resp.raise_for_status()
            result_holder[0] = resp.json()
        except BaseException as exc:
            result_holder[1] = exc

    http_thread = threading.Thread(
        target=do_request,
        daemon=True,
        name="meeting-transcribe-http",
    )
    http_thread.start()

    while http_thread.is_alive():
        if stop_event.is_set():
            logger.info("transcribe: abort during HTTP request")
            return None
        http_thread.join(timeout=_ABORT_POLL_S)

    if result_holder[1] is not None:
        raise result_holder[1]

    return result_holder[0]


def _save_results(
    session_dir: str,
    data: dict,
    params: TranscribeParams,
) -> None:
    """Write transcript.txt, transcript.json, and transcribe_params.json atomically."""
    txt_content = None
    for fmt in data.get("additional_formats", []):
        if fmt.get("requested_format") == "txt" or fmt.get("file_extension") == "txt":
            txt_content = fmt.get("content", "")
            break
    if txt_content is None:
        txt_content = data.get("text", "")

    _atomic_write(
        os.path.join(session_dir, "transcript.txt"),
        txt_content,
    )
    _atomic_write(
        os.path.join(session_dir, "transcript.json"),
        json.dumps(data, indent=2),
    )
    _atomic_write(
        os.path.join(session_dir, "transcribe_params.json"),
        json.dumps(
            {
                "num_speakers": params.num_speakers,
                "keyterms": params.keyterms,
                "language_code": params.language_code,
            },
            indent=2,
        ),
    )
    logger.debug("transcribe: saved results to %s", session_dir)


def _atomic_write(path: str, content: str) -> None:
    """Write content to path via temp file + rename for crash safety."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, path)


def _wait_for_subprocess(
    proc: subprocess.Popen,
    stop_event: threading.Event,
    output_path: str,
) -> bool:
    """Poll subprocess until it exits or stop_event fires. Returns True if aborted."""
    while proc.poll() is None:
        if stop_event.is_set():
            logger.info("transcribe: abort requested, terminating subprocess")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)
            except Exception:
                logger.exception("transcribe: failed to stop subprocess")
            try:  # noqa: SIM105
                os.unlink(output_path)
            except OSError:
                pass
            return True
        time.sleep(_FFMPEG_POLL_S)
    return False


def _drain_stderr(proc: subprocess.Popen, tail: list[str], label: str) -> None:
    """Forward subprocess stderr to debug log and retain a tail buffer."""
    if not proc.stderr:
        return
    try:
        for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 200:
                del tail[: len(tail) - 200]
            logger.debug("[%s] ffmpeg: %s", label, line)
    except Exception:
        logger.debug("transcribe: stderr drain exited", exc_info=True)


def _progress_loop(
    on_progress: Callable[[str, float], None],
    stop: threading.Event,
    start_t: float,
    phase: list[str],
) -> None:
    """Tick progress callback until stop is set."""
    while not stop.wait(_PROGRESS_TICK_S):
        on_progress(phase[0], time.monotonic() - start_t)


def _result(
    success: bool,
    error_reason: str | None,
    start_t: float,
    audio_duration_s: float | None,
) -> TranscribeResult:
    return TranscribeResult(
        success=success,
        error_reason=error_reason,
        elapsed_s=time.monotonic() - start_t,
        audio_duration_s=audio_duration_s,
    )
