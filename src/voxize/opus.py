"""Streaming Opus encoder — runs ffmpeg during recording.

Spawns ``ffmpeg -c:a libopus`` reading raw PCM from stdin and writing
an ``.opus`` file.  The sounddevice callback feeds it the same chunks
that go to the WAV writer, so encoding happens in real time with no
post-stop delay.

If ffmpeg is missing, crashes, or its stdin pipe breaks, the encoder
silently marks itself failed.  The caller checks ``.ok`` and falls
back to the WAV file for batch upload — this is purely an optimisation,
never a hard requirement.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading

from voxize._audio_const import CHANNELS, SAMPLE_RATE

logger = logging.getLogger(__name__)

OPUS_BITRATE_KBPS = 48


class OpusEncoder:
    """Streams PCM to ffmpeg, producing an Opus file in real time."""

    def __init__(self, session_dir: str) -> None:
        self._path = os.path.join(session_dir, "audio.ogg")
        self._proc: subprocess.Popen | None = None
        self._ok = True
        self._stderr_thread: threading.Thread | None = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def ok(self) -> bool:
        """True while the encoder is running or finished cleanly."""
        return self._ok

    def start(self) -> None:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-y",
            "-f",
            "s16le",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            str(CHANNELS),
            "-i",
            "pipe:0",
            "-c:a",
            "libopus",
            "-b:a",
            f"{OPUS_BITRATE_KBPS}k",
            self._path,
        ]
        logger.debug("opus start: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning("opus start: ffmpeg not found, Opus encoding disabled")
            self._ok = False
            return

        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            daemon=True,
            name="voxize-opus-err",
        )
        self._stderr_thread.start()

    def write(self, pcm: bytes) -> None:
        """Feed a PCM chunk to ffmpeg's stdin.  Non-blocking on failure."""
        if not self._ok or not self._proc or not self._proc.stdin:
            return
        try:
            self._proc.stdin.write(pcm)
        except (BrokenPipeError, OSError):
            logger.warning("opus write: pipe broken, disabling")
            self._ok = False

    def stop(self) -> None:
        """Close stdin and wait for ffmpeg to flush.  ~10-50ms typical."""
        proc = self._proc
        self._proc = None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5.0)
            if proc.returncode != 0:
                logger.warning("opus stop: ffmpeg exited %d", proc.returncode)
                self._ok = False
            else:
                try:
                    size = os.path.getsize(self._path)
                except OSError:
                    size = 0
                logger.debug("opus stop: ok size=%d", size)
        except subprocess.TimeoutExpired:
            logger.warning("opus stop: ffmpeg did not exit, killing")
            proc.kill()
            proc.wait(timeout=1.0)
            self._ok = False
        except Exception:
            logger.exception("opus stop: unexpected error")
            self._ok = False
        if self._stderr_thread:
            self._stderr_thread.join(timeout=1.0)
            self._stderr_thread = None

    def _drain_stderr(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    logger.debug("opus ffmpeg: %s", line)
        except Exception:
            pass
