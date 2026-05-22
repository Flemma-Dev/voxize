"""Post-recording compression: WAV → Opus via ffmpeg.

After the meeting recorder finalizes the stereo WAV, this module runs
``ffmpeg -c:a libopus -b:a 48k`` to produce a much smaller ``.opus``
file in the same session directory. On successful encode + duration
verification, the original WAV is trashed via ``Gio.File.trash()`` —
recoverable from the system trash, never permanently deleted.

Why Opus 48 kbps stereo: ~30x smaller than the 48 kHz/16-bit/stereo
WAV at roughly equivalent perceived quality for voice, accepted by
WhisperX and every diarization frontend via ffmpeg. See the journal
entry that introduced this module for the comparison table.

Failure-mode contract — all are keep-the-WAV:
- ffmpeg crash / non-zero exit
- ffprobe could not parse the output's duration
- duration mismatch (|opus - wav| > 0.5 s)
- user-requested abort (Esc / window close during compress)

Every failure writes ``compress_error.txt`` next to the WAV with the
reason, so the user can spot it via the session folder button.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio  # noqa: E402

from voxize.meeting.capture import OUTPUT_CHANNELS, SAMPLE_RATE  # noqa: E402

logger = logging.getLogger(__name__)

OPUS_BITRATE_KBPS = 96
DURATION_TOLERANCE_S = 0.5
_PROGRESS_TICK_S = 0.25
_STDERR_TAIL_LINES = 8


@dataclass
class CompressResult:
    success: bool
    output_path: str | None
    error_reason: str | None
    elapsed_s: float
    expected_duration_s: float
    actual_duration_s: float | None


def compress_meeting_wav(
    session_dir: str,
    wav_data_bytes: int,
    stop_event: threading.Event,
    on_progress: Callable[[float], None] | None = None,
) -> CompressResult:
    """Encode recording.wav → recording.opus, verify, trash WAV on success.

    ``wav_data_bytes`` is the PCM byte count from ``WavWriter.data_bytes``
    (the writer already knows it; passing it in avoids a redundant
    ffprobe of the WAV).

    ``stop_event`` is polled while ffmpeg runs; when set, ffmpeg is
    terminated and the partial .opus is removed.

    ``on_progress`` is called every ~250 ms with elapsed seconds (float)
    so the UI can render a live "Compressing… HH:MM:SS" label without
    its own timer source.
    """
    wav_path = os.path.join(session_dir, "recording.wav")
    opus_path = os.path.join(session_dir, "recording.opus")
    if not os.path.exists(wav_path):
        return _result(False, None, "wav file not found", 0.0, 0.0, None)
    if wav_data_bytes == 0:
        return _result(False, None, "no audio data to compress", 0.0, 0.0, None)

    expected_duration_s = wav_data_bytes / (SAMPLE_RATE * OUTPUT_CHANNELS * 2)
    logger.info(
        "compress: wav=%s opus=%s expected_duration=%.2fs bitrate=%dk",
        wav_path,
        opus_path,
        expected_duration_s,
        OPUS_BITRATE_KBPS,
    )

    start_t = time.monotonic()
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-y",
        "-i",
        wav_path,
        "-c:a",
        "libopus",
        "-b:a",
        f"{OPUS_BITRATE_KBPS}k",
        "-ac",
        str(OUTPUT_CHANNELS),
        "-mapping_family",
        "255",
        opus_path,
    ]
    logger.debug("compress: ffmpeg cmd=%s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return _fail(
            session_dir,
            "ffmpeg not found — install ffmpeg",
            time.monotonic() - start_t,
            expected_duration_s,
            None,
        )

    stderr_tail: list[str] = []
    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(proc, stderr_tail),
        daemon=True,
        name="meeting-ffmpeg-err",
    )
    stderr_thread.start()

    aborted = _wait_for_ffmpeg(proc, stop_event, on_progress, opus_path)
    stderr_thread.join(timeout=1.0)
    elapsed = time.monotonic() - start_t

    if aborted:
        return _result(False, None, "aborted", elapsed, expected_duration_s, None)

    if proc.returncode != 0:
        tail = "\n".join(stderr_tail[-_STDERR_TAIL_LINES:]) or "(no stderr captured)"
        reason = f"ffmpeg exited with code {proc.returncode}"
        logger.error("compress: %s\n%s", reason, tail)
        return _fail(
            session_dir,
            f"{reason}\n{tail}",
            elapsed,
            expected_duration_s,
            None,
        )

    actual_duration = _probe_duration(opus_path)
    if actual_duration is None:
        return _fail(
            session_dir,
            "ffprobe could not parse opus duration",
            elapsed,
            expected_duration_s,
            None,
        )

    delta = abs(actual_duration - expected_duration_s)
    if delta > DURATION_TOLERANCE_S:
        reason = (
            f"duration mismatch wav={expected_duration_s:.2f}s "
            f"opus={actual_duration:.2f}s delta={delta:.2f}s"
        )
        logger.error("compress: %s", reason)
        return _fail(
            session_dir,
            reason,
            elapsed,
            expected_duration_s,
            actual_duration,
        )

    wav_size = os.path.getsize(wav_path)
    opus_size = os.path.getsize(opus_path)
    logger.info(
        "compress: success wav=%.2fs opus=%.2fs delta=%.2fs "
        "wav_size=%d opus_size=%d ratio=%.1fx elapsed=%.1fs",
        expected_duration_s,
        actual_duration,
        delta,
        wav_size,
        opus_size,
        wav_size / opus_size if opus_size > 0 else 0.0,
        elapsed,
    )
    _trash_file(wav_path)
    return _result(
        True,
        opus_path,
        None,
        elapsed,
        expected_duration_s,
        actual_duration,
    )


# ── Internals ──


def _wait_for_ffmpeg(
    proc: subprocess.Popen,
    stop_event: threading.Event,
    on_progress: Callable[[float], None] | None,
    opus_path: str,
) -> bool:
    """Poll ffmpeg until it exits or stop_event fires. Returns True if aborted."""
    start_t = time.monotonic()
    while proc.poll() is None:
        if stop_event.is_set():
            logger.info("compress: abort requested, terminating ffmpeg")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    logger.warning("compress: ffmpeg did not exit on SIGTERM, killing")
                    proc.kill()
                    proc.wait(timeout=1.0)
            except Exception:
                logger.exception("compress: failed to stop ffmpeg")
            try:  # noqa: SIM105
                os.unlink(opus_path)
            except OSError:
                pass
            return True
        if on_progress is not None:
            on_progress(time.monotonic() - start_t)
        time.sleep(_PROGRESS_TICK_S)
    return False


def _drain_stderr(proc: subprocess.Popen, tail: list[str]) -> None:
    """Forward ffmpeg stderr lines into the debug log + retain a tail buffer."""
    if not proc.stderr:
        return
    try:
        for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            tail.append(line)
            # Bound the tail so a long run doesn't accumulate megabytes.
            if len(tail) > 200:
                del tail[: len(tail) - 200]
            logger.debug("ffmpeg: %s", line)
    except Exception:
        logger.debug("compress: stderr drain exited", exc_info=True)


def _probe_duration(path: str) -> float | None:
    """Use ffprobe to read the audio file's duration in seconds."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        logger.debug("_probe_duration: ffprobe failed", exc_info=True)
        return None
    text = result.stdout.strip()
    try:
        return float(text)
    except ValueError:
        logger.debug("_probe_duration: could not parse %r", text)
        return None


def _trash_file(path: str) -> None:
    """Move a file to the system trash via Gio (recoverable, never rm -f)."""
    try:
        Gio.File.new_for_path(path).trash(None)
        logger.debug("_trash_file: trashed %s", path)
    except Exception:
        logger.exception("_trash_file: failed to trash %s", path)


def _write_compress_error(session_dir: str, message: str) -> None:
    """Drop compress_error.txt alongside the WAV explaining what happened."""
    try:
        with open(os.path.join(session_dir, "compress_error.txt"), "w") as f:
            f.write(message + "\n")
    except OSError:
        logger.debug("_write_compress_error: could not write file", exc_info=True)


def _result(
    success: bool,
    output_path: str | None,
    error_reason: str | None,
    elapsed_s: float,
    expected_duration_s: float,
    actual_duration_s: float | None,
) -> CompressResult:
    return CompressResult(
        success=success,
        output_path=output_path,
        error_reason=error_reason,
        elapsed_s=elapsed_s,
        expected_duration_s=expected_duration_s,
        actual_duration_s=actual_duration_s,
    )


def _fail(
    session_dir: str,
    reason: str,
    elapsed_s: float,
    expected_duration_s: float,
    actual_duration_s: float | None,
) -> CompressResult:
    """Failure path: write compress_error.txt + return a failing result."""
    _write_compress_error(session_dir, reason)
    return _result(
        False,
        None,
        reason.splitlines()[0],
        elapsed_s,
        expected_duration_s,
        actual_duration_s,
    )
