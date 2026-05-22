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
        self._wav = WavWriter(os.path.join(session_dir, "audio.wav"))
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
