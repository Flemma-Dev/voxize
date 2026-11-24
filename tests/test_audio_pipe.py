"""PipeAudioCapture and --audio-fd: the backend's audio from a pipe instead of the microphone."""

import os
import struct
import subprocess
import sys
import threading
import wave

import pytest

from voxize._audio_const import SAMPLE_RATE
from voxize.audio import BLOCK_BYTES, PipeAudioCapture


def _tone(blocks: int, extra: bytes = b"") -> bytes:
    samples = [(index * 37) % 2000 - 1000 for index in range(blocks * BLOCK_BYTES // 2)]
    return struct.pack(f"<{len(samples)}h", *samples) + extra


def _capture(tmp_path, pcm: bytes, chunks: list[bytes]):
    read_fd, write_fd = os.pipe()
    capture = PipeAudioCapture(str(tmp_path), read_fd, on_chunk=chunks.append)
    capture.start()

    def write():
        with os.fdopen(write_fd, "wb") as stream:
            # In pieces that do not line up with blocks, as a network delivers them.
            for offset in range(0, len(pcm), 1000):
                stream.write(pcm[offset : offset + 1000])

    writer = threading.Thread(target=write)
    writer.start()
    writer.join(10)
    capture.stop()
    return capture


def test_every_frame_reaches_the_wav_and_the_chunks(tmp_path):
    pcm = _tone(5)
    chunks: list[bytes] = []
    capture = _capture(tmp_path, pcm, chunks)
    assert b"".join(chunks) == pcm
    assert {len(chunk) for chunk in chunks} == {BLOCK_BYTES}
    with wave.open(str(tmp_path / "audio.wav")) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
            SAMPLE_RATE,
            1,
            2,
        )
        assert wav.readframes(wav.getnframes()) == pcm
    assert capture.meter is not None


def test_a_short_last_block_is_kept_and_a_split_sample_dropped(tmp_path):
    pcm = _tone(2, extra=b"\x01\x02\x03")  # one more sample and half of another
    chunks: list[bytes] = []
    _capture(tmp_path, pcm, chunks)
    assert [len(chunk) for chunk in chunks] == [BLOCK_BYTES, BLOCK_BYTES, 2]
    assert b"".join(chunks) == pcm[:-1]


def test_stop_waits_for_the_writer_to_finish(tmp_path):
    read_fd, write_fd = os.pipe()
    chunks: list[bytes] = []
    capture = PipeAudioCapture(str(tmp_path), read_fd, on_chunk=chunks.append)
    capture.start()
    pcm = _tone(3)

    def late_writer():
        with os.fdopen(write_fd, "wb") as stream:
            stream.write(pcm[:BLOCK_BYTES])
            stream.flush()
            threading.Event().wait(0.3)
            stream.write(pcm[BLOCK_BYTES:])

    writer = threading.Thread(target=late_writer)
    writer.start()
    capture.stop()  # before the writer is done: waits for its end of file
    writer.join(10)
    assert b"".join(chunks) == pcm


def test_piped_audio_never_loads_sounddevice():
    code = (
        "import sys, voxize.audio, voxize.backend.session; "
        "print('sounddevice' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


@pytest.mark.parametrize("value", ["three", ""])
def test_a_bad_audio_fd_is_refused(value):
    completed = subprocess.run(
        [sys.executable, "-m", "voxize.backend", "--audio-fd", value],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "VOXIZE_MOCK": "1"},
    )
    assert completed.returncode == 2
    assert "--audio-fd" in completed.stderr
