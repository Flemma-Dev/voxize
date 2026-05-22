"""Dual-stream capture: mic + system monitor → stereo WAV.

Two ``pw-cat --record`` subprocesses each emit raw int16 mono PCM at
48 kHz to their stdout. Reader threads pull fixed-size blocks into
bounded queues and update per-stream LevelMeters. A writer thread
pulls one block from each queue, interleaves them (L=mic, R=system),
and appends to the stereo WAV via the shared crash-safe
:class:`WavWriter`.

Why pw-cat (and not parec, or sounddevice):
- Voxize is a PipeWire app — ``ducking.py`` already shells out to
  ``pw-dump`` and ``wpctl`` — so the audio toolchain stays consistent
  by sourcing capture from the same family.
- For the system side, we pass ``--properties stream.capture.sink=true``
  so PipeWire links the record stream to the target sink's monitor
  port. (pw-cat has no ``--capture-sink`` CLI flag — that's a parec-ism
  — but the underlying pw-stream property is what makes capture-from-
  output-device work natively in either tool.)
- sounddevice's PortAudio→ALSA backend doesn't reliably enumerate
  PipeWire/PulseAudio source *names*, so the default sink's monitor
  isn't addressable through it.
- Symmetry: identical control flow for both streams (one with the
  monitor property, one without) keeps the writer loop trivial.

Default-device discovery uses ``wpctl inspect @DEFAULT_AUDIO_SINK@``
and parses ``node.name`` — the same wpctl binary already required by
the ducking module.

Drift / sync:
- PipeWire's graph clock is shared across both sources, so the two
  pw-cat processes deliver blocks in lockstep at steady state.
- If a stream stalls, the writer pads silence on that channel for as
  long as it takes the slow side to recover (queue is drop-oldest if
  it fills, so an arbitrarily-long stall does not cause unbounded
  memory growth — it only sacrifices that channel's audio).
"""

from __future__ import annotations

import array
import contextlib
import logging
import os
import queue
import subprocess
import threading
import time

from voxize.audio import LevelMeter, WavWriter

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
PER_STREAM_CHANNELS = 1
OUTPUT_CHANNELS = 2  # L=mic, R=system
BLOCK_FRAMES = 1920  # 40 ms at 48 kHz
BLOCK_BYTES = BLOCK_FRAMES * 2  # int16 mono = 2 bytes/frame
QUEUE_DEPTH = 200  # ~8 s of buffer per stream before drop-oldest kicks in
HEADER_REWRITE_INTERVAL_S = 5.0
LATENCY_MS = 40


class CaptureError(Exception):
    """Raised when capture cannot start (missing tools, unreachable sources, …)."""


def _read_exact(stream, n: int) -> bytes:
    """Read exactly ``n`` bytes from ``stream`` or return what we got at EOF.

    ``BufferedReader.read(n)`` may return fewer than n bytes when the OS
    pipe partially fills, so we loop until either n bytes are buffered
    or the stream closes.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
    return bytes(buf)


def _resolve_default_sink() -> str:
    """Resolve the default audio sink's ``node.name`` via wpctl.

    wpctl is part of WirePlumber, which is the session manager every
    PipeWire desktop install ships. ``@DEFAULT_AUDIO_SINK@`` is wpctl's
    canonical alias for "whatever is currently the default output."
    """
    try:
        result = subprocess.run(
            ["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=True,
        )
    except FileNotFoundError as e:
        raise CaptureError(
            "wpctl not found — install wireplumber (or run inside the nix dev shell)"
        ) from e
    except subprocess.SubprocessError as e:
        raise CaptureError(f"wpctl inspect failed: {e}") from e

    for raw in result.stdout.splitlines():
        line = raw.strip().lstrip("*").strip()
        if not line.startswith("node.name"):
            continue
        # Lines look like:  node.name = "alsa_output.pci-0000…analog-stereo"
        # Tolerate either ``=`` or ``:`` and an optional double-quote wrapping.
        _, sep, value = line.partition("=")
        if not sep:
            _, sep, value = line.partition(":")
        value = value.strip().strip('"').strip("'")
        if value:
            logger.debug("default sink node.name=%s", value)
            return value
    raise CaptureError(
        "wpctl did not report a node.name for the default sink — "
        "check `wpctl inspect @DEFAULT_AUDIO_SINK@`"
    )


class _StreamReader:
    """pw-cat → bounded queue + LevelMeter, one per source."""

    def __init__(
        self,
        name: str,
        target: str | None = None,
        capture_sink: bool = False,
    ) -> None:
        self.name = name
        self.target = target
        self.capture_sink = capture_sink
        self.meter = LevelMeter()
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=QUEUE_DEPTH)
        self.error: str | None = None
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        cmd = [
            "pw-cat",
            "--record",
            "--rate",
            str(SAMPLE_RATE),
            "--channels",
            str(PER_STREAM_CHANNELS),
            "--format",
            "s16",
            "--latency",
            f"{LATENCY_MS}ms",
            "--raw",
        ]
        if self.target:
            cmd.extend(["--target", self.target])
        if self.capture_sink:
            # pw-cat has no --capture-sink CLI flag; the equivalent is
            # the pw-stream property ``stream.capture.sink=true``, which
            # makes the record stream link to the target sink's monitor
            # port instead of expecting a regular source.
            cmd.extend(["--properties", "stream.capture.sink=true"])
        cmd.append("-")  # write raw audio to stdout
        logger.debug("[%s] pw-cat: %s", self.name, " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise CaptureError(
                "pw-cat not found — install pipewire (or run inside the nix dev shell)"
            ) from e

        self._thread = threading.Thread(
            target=self._read_loop,
            name=f"meeting-{self.name}",
            daemon=True,
        )
        self._thread.start()
        # Drain stderr into the debug log so pw-cat's diagnostics aren't
        # silently swallowed (this is how the original `--capture-sink`
        # bug stayed hidden — pw-cat was printing "unknown option" to
        # stderr but we'd routed it to /dev/null).
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name=f"meeting-{self.name}-err",
            daemon=True,
        )
        self._stderr_thread.start()

    def _stderr_loop(self) -> None:
        """Forward pw-cat stderr lines to the debug log."""
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    logger.warning("[%s] pw-cat: %s", self.name, line)
        except Exception:
            logger.debug("[%s] stderr loop exited", self.name, exc_info=True)

    def _read_loop(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        logger.debug("[%s] read loop entry", self.name)
        try:
            while not self._stop_event.is_set():
                block = _read_exact(proc.stdout, BLOCK_BYTES)
                if len(block) < BLOCK_BYTES:
                    if not self._stop_event.is_set():
                        logger.warning(
                            "[%s] short read (got %d/%d bytes), stream ended",
                            self.name,
                            len(block),
                            BLOCK_BYTES,
                        )
                        self.error = "Capture stream ended unexpectedly"
                    break
                self.meter.update(block)
                try:
                    self.queue.put(block, block=False)
                except queue.Full:
                    # Writer is falling behind — drop the oldest block so the
                    # newest audio still lands. Bounded queue + drop-oldest is
                    # the per-channel backpressure policy for the whole
                    # capture; without it a stalled writer would OOM.
                    with contextlib.suppress(queue.Empty):
                        self.queue.get_nowait()
                    with contextlib.suppress(queue.Full):
                        self.queue.put(block, block=False)
                    logger.warning("[%s] queue full, dropped oldest block", self.name)
        except Exception:
            logger.exception("[%s] read loop crashed", self.name)
            self.error = "Capture read loop crashed"
        finally:
            logger.debug("[%s] read loop exiting", self.name)

    def stop(self) -> None:
        self._stop_event.set()
        proc = self._proc
        self._proc = None
        if proc:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "[%s] pw-cat did not exit on SIGTERM, killing", self.name
                    )
                    proc.kill()
                    proc.wait(timeout=1.0)
            except Exception:
                logger.exception("[%s] failed to stop pw-cat", self.name)
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._stderr_thread:
            self._stderr_thread.join(timeout=1.0)
            self._stderr_thread = None


class DualStreamCapture:
    """Captures mic + default-sink monitor into a stereo (L=mic, R=system) WAV."""

    def __init__(self, session_dir: str) -> None:
        self.wav_path = os.path.join(session_dir, "recording.wav")
        self._wav = WavWriter(
            self.wav_path,
            sample_rate=SAMPLE_RATE,
            channels=OUTPUT_CHANNELS,
        )
        # Mic reader has no --target (pw-cat picks the default source) and
        # no --capture-sink. The sys reader is constructed in start() once
        # the default sink's node.name has been resolved.
        self._mic = _StreamReader("mic")
        self._sys: _StreamReader | None = None
        self._writer_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._default_sink: str | None = None

    @property
    def mic_meter(self) -> LevelMeter:
        return self._mic.meter

    @property
    def sys_meter(self) -> LevelMeter:
        if self._sys is None:
            return LevelMeter()
        return self._sys.meter

    @property
    def data_bytes(self) -> int:
        return self._wav.data_bytes

    @property
    def default_sink(self) -> str | None:
        """The resolved default-sink node.name (post-start), for diagnostics."""
        return self._default_sink

    def start(self) -> None:
        """Resolve devices, open WAV, spawn both pw-cat processes + writer."""
        self._default_sink = _resolve_default_sink()
        self._sys = _StreamReader(
            "sys",
            target=self._default_sink,
            capture_sink=True,
        )
        self._wav.open()
        try:
            self._mic.start()
            self._sys.start()
        except CaptureError:
            self._wav.finalize()
            raise
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="meeting-writer",
            daemon=True,
        )
        self._writer_thread.start()
        logger.debug("DualStreamCapture started: wav=%s", self.wav_path)

    def _writer_loop(self) -> None:
        """Pull one block from each queue, interleave L/R, append to WAV."""
        last_rewrite = time.monotonic()
        zero_block = b"\x00" * BLOCK_BYTES
        while not self._stop_event.is_set():
            mic_block = self._get_block(self._mic, timeout=0.5)
            sys_block = self._get_block(self._sys, timeout=0.05) if self._sys else None
            if mic_block is None and sys_block is None:
                continue
            if mic_block is None:
                mic_block = zero_block
            if sys_block is None:
                sys_block = zero_block

            # Interleave int16 mono → int16 stereo via array.array slice
            # assignment. CPython implements typed-array slice copies at
            # C level, so the per-block cost is dominated by .frombytes /
            # .tobytes rather than the interleave itself — fast enough at
            # 48 kHz / 25 Hz block rate without pulling in numpy.
            n = min(len(mic_block), len(sys_block))
            mic_arr = array.array("h")
            mic_arr.frombytes(mic_block[:n])
            sys_arr = array.array("h")
            sys_arr.frombytes(sys_block[:n])
            samples = len(mic_arr)
            stereo = array.array("h", bytes(samples * 4))
            stereo[0::2] = mic_arr
            stereo[1::2] = sys_arr
            self._wav.write(stereo.tobytes())

            now = time.monotonic()
            if now - last_rewrite > HEADER_REWRITE_INTERVAL_S:
                self._wav.rewrite_header()
                last_rewrite = now

    @staticmethod
    def _get_block(reader: _StreamReader, timeout: float) -> bytes | None:
        try:
            return reader.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        """Terminate both pw-cat processes, drain writer, finalize WAV."""
        logger.debug("DualStreamCapture.stop entry")
        self._stop_event.set()
        self._mic.stop()
        if self._sys:
            self._sys.stop()
        if self._writer_thread:
            self._writer_thread.join(timeout=3.0)
            self._writer_thread = None
        self._wav.finalize()
        logger.debug("DualStreamCapture.stop complete: data_bytes=%d", self.data_bytes)

    def finalize_wav(self) -> None:
        """Rewrite the WAV header without stopping — safe from signal handlers."""
        logger.debug("finalize_wav: rewriting header (streams still active)")
        self._wav.rewrite_header()

    def check_errors(self) -> str | None:
        """Return the first non-None error from either stream, or None."""
        if self._mic.error:
            return f"Microphone: {self._mic.error}"
        if self._sys and self._sys.error:
            return f"System audio: {self._sys.error}"
        return None
