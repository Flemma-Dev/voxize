"""Audio capture via sounddevice and WAV file writing.

AudioCapture opens a sounddevice RawInputStream at 24kHz/16-bit/mono with
40ms block size (960 samples). Each callback writes PCM data to a WAV file
and forwards the raw bytes to a caller-supplied callback (for WebSocket).

WavWriter uses the placeholder-header technique: writes a 44-byte RIFF/WAV
header at open time with data size set to 0xFFFFFFFF, appends + flushes PCM
on each write, and fixes the two size fields on finalize. On crash, the file
has an incorrect header but all PCM data is intact and recoverable.

PipeAudioCapture is AudioCapture's interface fed from a file descriptor
instead of the microphone: raw PCM in the same format, written by a process
that records elsewhere (``python -m voxize.backend --audio-fd FD``). It never
imports sounddevice.

LevelMeter tracks per-chunk RMS for the UI level bar.  Audio is never
modified — per-chunk gain manipulation was proven to degrade the
Realtime API's transcription quality.
"""

import array
import logging
import math
import os
import struct
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from voxize._audio_const import BLOCK_SIZE, CHANNELS, DTYPE, SAMPLE_RATE
from voxize.opus import OpusEncoder

if TYPE_CHECKING:
    import sounddevice as sd

logger = logging.getLogger(__name__)

# Bytes in one capture block (16-bit samples).
BLOCK_BYTES = BLOCK_SIZE * CHANNELS * 2
# How long PipeAudioCapture.stop() waits for the writer to close its end.
PIPE_DRAIN_SECONDS = 15.0


def rms_dbfs(samples: array.array) -> float:
    """Compute RMS level in dBFS from an int16 sample array."""
    n = len(samples)
    if n == 0:
        return -96.0
    sum_sq = sum(s * s for s in samples)
    rms = math.sqrt(sum_sq / n)
    if rms < 1:
        return -96.0
    return 20.0 * math.log10(rms / 32768.0)


class LevelMeter:
    """Passive audio level tracker — observes without modifying audio.

    Exposes ``level_dbfs`` for the UI meter bar.
    """

    def __init__(self) -> None:
        self.level_dbfs: float = -96.0

    def update(self, pcm: bytes) -> None:
        """Compute level from a chunk of int16 PCM."""
        samples = array.array("h", pcm)
        self.level_dbfs = rms_dbfs(samples)


class WavWriter:
    """Streams PCM data to a WAV file with crash-safe placeholder header.

    Defaults match the live-dictation pipeline (24 kHz mono int16). Pass
    explicit values for the meeting recorder (48 kHz stereo) or any other
    capture shape — the 44-byte header layout is fixed for PCM int16 so
    only the rate/channel fields vary.
    """

    def __init__(
        self,
        path: str,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
    ) -> None:
        self._path = path
        self._sample_rate = sample_rate
        self._channels = channels
        self._fd = None
        self._data_bytes = 0
        self._lock = threading.Lock()

    def open(self) -> None:
        with self._lock:
            logger.debug(
                "wav open: path=%s rate=%d ch=%d",
                self._path,
                self._sample_rate,
                self._channels,
            )
            self._fd = open(self._path, "wb")  # noqa: SIM115
            byte_rate = self._sample_rate * self._channels * 2
            block_align = self._channels * 2
            self._fd.write(b"RIFF")
            self._fd.write(struct.pack("<I", 0xFFFFFFFF))  # RIFF size placeholder
            self._fd.write(b"WAVE")
            # fmt sub-chunk (16 bytes)
            self._fd.write(b"fmt ")
            self._fd.write(struct.pack("<I", 16))
            self._fd.write(struct.pack("<H", 1))  # PCM format
            self._fd.write(struct.pack("<H", self._channels))
            self._fd.write(struct.pack("<I", self._sample_rate))
            self._fd.write(struct.pack("<I", byte_rate))
            self._fd.write(struct.pack("<H", block_align))
            self._fd.write(struct.pack("<H", 16))  # bits per sample
            self._fd.write(b"data")
            self._fd.write(struct.pack("<I", 0xFFFFFFFF))  # data size placeholder
            self._fd.flush()
            self._data_bytes = 0

    def write(self, pcm: bytes) -> None:
        with self._lock:
            if self._fd:
                self._fd.write(pcm)
                self._fd.flush()
                self._data_bytes += len(pcm)

    def rewrite_header(self) -> None:
        """Update the size fields without closing the file.

        Periodically called by the meeting recorder so that, on power loss
        or hard kill, the resulting WAV is playable up to the most recent
        rewrite — not just a placeholder header pointing past EOF.
        """
        with self._lock:
            if not self._fd or self._data_bytes == 0:
                return
            pos = self._fd.tell()
            try:
                self._fd.seek(4)
                self._fd.write(struct.pack("<I", 36 + self._data_bytes))
                self._fd.seek(40)
                self._fd.write(struct.pack("<I", self._data_bytes))
                self._fd.flush()
            finally:
                self._fd.seek(pos)

    @property
    def data_bytes(self) -> int:
        """Bytes of PCM written so far (excludes the 44-byte header)."""
        return self._data_bytes

    def finalize(self) -> None:
        """Fix WAV header sizes and close the file."""
        with self._lock:
            logger.debug("wav finalize: data_bytes=%d", self._data_bytes)
            if self._fd:
                try:
                    self._fd.seek(4)
                    self._fd.write(struct.pack("<I", 36 + self._data_bytes))
                    self._fd.seek(40)
                    self._fd.write(struct.pack("<I", self._data_bytes))
                    self._fd.flush()
                finally:
                    self._fd.close()
                    self._fd = None


def _noop_on_chunk(_pcm: bytes) -> None:
    """Default per-chunk callback — discards the chunk."""


class AudioCapture:
    """Captures microphone audio, writes WAV, and forwards PCM chunks."""

    def __init__(
        self,
        session_dir: str,
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> None:
        self._session_dir = session_dir
        self._wav = WavWriter(os.path.join(session_dir, "audio.wav"))
        self._opus = OpusEncoder(session_dir)
        # Callback is swappable: during fast startup we open the stream
        # before the live-preview WS is set up, so chunks are discarded
        # until set_on_chunk() wires in RealtimeTranscription.send_audio.
        self._on_chunk = on_chunk or _noop_on_chunk
        self._meter = LevelMeter()
        self._stream: sd.RawInputStream | None = None

    @property
    def meter(self) -> LevelMeter:
        """Access the level meter for UI polling."""
        return self._meter

    def set_on_chunk(self, on_chunk: Callable[[bytes], None]) -> None:
        """Swap the per-chunk forwarding callback.

        Safe from any thread: attribute assignment is atomic under the
        GIL. The sounddevice callback will pick up the new function on
        its next invocation.
        """
        self._on_chunk = on_chunk

    def _callback(self, indata, frames, time_info, status) -> None:
        pcm = bytes(indata)
        self._meter.update(pcm)
        self._wav.write(pcm)
        self._opus.write(pcm)
        self._on_chunk(pcm)

    @property
    def opus_path(self) -> str | None:
        """Path to the Opus file if encoding succeeded, else None."""
        if self._opus.ok and os.path.exists(self._opus.path):
            return self._opus.path
        return None

    def start(self) -> None:
        # Imported here, so PipeAudioCapture never loads PortAudio; the
        # backend preloads it in the background for microphone sessions.
        import sounddevice as sd

        self._wav.open()
        self._opus.start()
        logger.debug(
            "audio start: rate=%d ch=%d dtype=%s blocksize=%d",
            SAMPLE_RATE,
            CHANNELS,
            DTYPE,
            BLOCK_SIZE,
        )
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=BLOCK_SIZE,
            callback=self._callback,
        )
        self._stream.start()
        logger.debug("audio start: stream active")

    def finalize_wav(self) -> None:
        """Finalize the WAV header without stopping the stream.

        Safe to call from a signal handler — only touches the file descriptor.
        """
        logger.debug("finalize_wav: finalizing WAV header (stream still active)")
        self._wav.finalize()

    def stop(self) -> None:
        logger.debug("audio stop")
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._opus.stop()
        self._wav.finalize()
        logger.debug("audio stop: complete opus_ok=%s", self._opus.ok)


class PipeAudioCapture:
    """AudioCapture's interface, reading PCM from a file descriptor.

    The writer sends raw PCM in the capture format (``SAMPLE_RATE`` Hz,
    ``CHANNELS`` channel, 16-bit little-endian) and closes its end when the
    recording is over, before it sends ``stop``; ``stop()`` waits for that
    end of file, so the WAV batch transcription reads holds every frame.
    """

    def __init__(
        self,
        session_dir: str,
        fd: int,
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> None:
        self._session_dir = session_dir
        self._fd = fd
        self._wav = WavWriter(os.path.join(session_dir, "audio.wav"))
        self._opus = OpusEncoder(session_dir)
        self._on_chunk = on_chunk or _noop_on_chunk
        self._meter = LevelMeter()
        self._reader: threading.Thread | None = None
        self._received = 0

    @property
    def meter(self) -> LevelMeter:
        """Access the level meter for UI polling."""
        return self._meter

    def set_on_chunk(self, on_chunk: Callable[[bytes], None]) -> None:
        """Swap the per-chunk forwarding callback (see AudioCapture)."""
        self._on_chunk = on_chunk

    @property
    def opus_path(self) -> str | None:
        """Path to the Opus file if encoding succeeded, else None."""
        if self._opus.ok and os.path.exists(self._opus.path):
            return self._opus.path
        return None

    def start(self) -> None:
        self._wav.open()
        self._opus.start()
        self._reader = threading.Thread(
            target=self._read, daemon=True, name="voxize-audio-pipe"
        )
        self._reader.start()
        logger.debug("pipe audio start: fd=%d block_bytes=%d", self._fd, BLOCK_BYTES)

    def _deliver(self, pcm: bytes) -> None:
        self._meter.update(pcm)
        self._wav.write(pcm)
        self._opus.write(pcm)
        self._on_chunk(pcm)

    def _read(self) -> None:
        pending = b""
        try:
            while data := os.read(self._fd, 65536):
                self._received += len(data)
                pending += data
                whole = len(pending) - len(pending) % BLOCK_BYTES
                for offset in range(0, whole, BLOCK_BYTES):
                    self._deliver(pending[offset : offset + BLOCK_BYTES])
                pending = pending[whole:]
            # A short last block; an odd trailing byte would split a sample.
            pending = pending[: len(pending) - len(pending) % 2]
            if pending:
                self._deliver(pending)
        except Exception:
            logger.exception("pipe audio: reading failed")
        finally:
            os.close(self._fd)
            logger.debug("pipe audio: end of stream after %d bytes", self._received)

    def finalize_wav(self) -> None:
        """Finalize the WAV header without waiting; safe from a signal handler."""
        self._wav.finalize()

    def stop(self) -> None:
        logger.debug("pipe audio stop")
        if self._reader is not None:
            self._reader.join(PIPE_DRAIN_SECONDS)
            if self._reader.is_alive():
                logger.warning(
                    "pipe audio: no end of stream after %.0fs; finalizing",
                    PIPE_DRAIN_SECONDS,
                )
        self._opus.stop()
        self._wav.finalize()
        logger.debug(
            "pipe audio stop: complete bytes=%d opus_ok=%s",
            self._received,
            self._opus.ok,
        )
