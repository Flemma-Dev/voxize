"""Audio capture via sounddevice and WAV file writing.

AudioCapture opens a sounddevice RawInputStream at 24kHz/16-bit/mono with
40ms block size (960 samples). Each callback writes PCM data to a WAV file
and forwards the raw bytes to a caller-supplied callback (for WebSocket).

WavWriter uses the placeholder-header technique: writes a 44-byte RIFF/WAV
header at open time with data size set to 0xFFFFFFFF, appends + flushes PCM
on each write, and fixes the two size fields on finalize. On crash, the file
has an incorrect header but all PCM data is intact and recoverable.

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

import sounddevice as sd

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24_000
CHANNELS = 1
DTYPE = "int16"
BLOCK_SIZE = 960  # 40ms at 24kHz — 1920 bytes per block
WAV_HEADER_SIZE = 44


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
    """Streams PCM data to a WAV file with crash-safe placeholder header."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._fd = None
        self._data_bytes = 0
        self._lock = threading.Lock()

    def open(self) -> None:
        with self._lock:
            logger.debug("wav open: path=%s", self._path)
            self._fd = open(self._path, "wb")  # noqa: SIM115
            # 44-byte RIFF/WAV header with placeholder sizes
            self._fd.write(b"RIFF")
            self._fd.write(
                struct.pack("<I", 0xFFFFFFFF)
            )  # RIFF chunk size (placeholder)
            self._fd.write(b"WAVE")
            # fmt sub-chunk (16 bytes)
            self._fd.write(b"fmt ")
            self._fd.write(struct.pack("<I", 16))  # sub-chunk size
            self._fd.write(struct.pack("<H", 1))  # PCM format
            self._fd.write(struct.pack("<H", CHANNELS))
            self._fd.write(struct.pack("<I", SAMPLE_RATE))
            self._fd.write(struct.pack("<I", SAMPLE_RATE * CHANNELS * 2))  # byte rate
            self._fd.write(struct.pack("<H", CHANNELS * 2))  # block align
            self._fd.write(struct.pack("<H", 16))  # bits per sample
            # data sub-chunk
            self._fd.write(b"data")
            self._fd.write(struct.pack("<I", 0xFFFFFFFF))  # data size (placeholder)
            self._fd.flush()
            self._data_bytes = 0

    def write(self, pcm: bytes) -> None:
        with self._lock:
            if self._fd:
                self._fd.write(pcm)
                self._fd.flush()
                self._data_bytes += len(pcm)

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


class AudioCapture:
    """Captures microphone audio, writes WAV, and forwards PCM chunks."""

    def __init__(
        self,
        session_dir: str,
        on_chunk: Callable[[bytes], None],
    ) -> None:
        self._wav = WavWriter(os.path.join(session_dir, "audio.wav"))
        self._on_chunk = on_chunk
        self._meter = LevelMeter()
        self._stream: sd.RawInputStream | None = None

    @property
    def meter(self) -> LevelMeter:
        """Access the level meter for UI polling."""
        return self._meter

    def _callback(self, indata, frames, time_info, status) -> None:
        pcm = bytes(indata)
        self._meter.update(pcm)
        self._wav.write(pcm)
        self._on_chunk(pcm)

    def start(self) -> None:
        self._wav.open()
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
        self._wav.finalize()
        logger.debug("audio stop: complete")
