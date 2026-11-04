"""The real ``voxize-tui`` entry point (``python -m voxize.tui``) under a pty.

Every other end-to-end test runs with stderr piped, where the terminal-stdout
claim is a no-op. Here stderr is a tty and stdout is a pipe: the headline use
(``voxize-tui | tmux load-buffer -``) — the transcript must arrive exactly,
with no trailing newline (a newline submits the prompt in Claude Code).
"""

import contextlib
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time

from helpers import MOCK_ENV

from voxize.mock import SHORT_TRANSCRIPT


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def test_entry_point_pipes_exact_transcript_under_a_pty():
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    # No display: the copy can only fall back to OSC 52, which lands on the pty.
    child_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("WAYLAND_DISPLAY", "DISPLAY", "COLUMNS", "LINES")
    }
    child_env.update(MOCK_ENV)
    child_env.update(VOXIZE_MOCK_SHORT="1", VOXIZE_STOP="1600", TERM="xterm-256color")
    proc = subprocess.Popen(
        [sys.executable, "-m", "voxize.tui"],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=slave,
        env=child_env,
        start_new_session=True,
    )
    os.close(slave)
    pgid = proc.pid  # start_new_session: the child leads its own group
    stdout = b""
    try:
        deadline = time.monotonic() + 30
        next_q = 0.0
        # Not communicate(): the pty must keep being drained and nudged.
        while proc.poll() is None:
            assert time.monotonic() < deadline, "voxize-tui did not exit in 30s"
            if select.select([master], [], [], 0.05)[0]:
                with contextlib.suppress(OSError):
                    os.read(master, 65536)
            if time.monotonic() >= next_q:
                # Ignored while a dictation is in flight; exits at READY.
                os.write(master, b"q")
                next_q = time.monotonic() + 0.2
        stdout = proc.stdout.read()
        assert proc.returncode == 0
        # No backend may outlive the client.
        end = time.monotonic() + 5
        while _group_alive(pgid) and time.monotonic() < end:
            time.sleep(0.05)
        assert not _group_alive(pgid), "a process from voxize-tui is still running"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        proc.wait()
        proc.stdout.close()
        os.close(master)
    assert stdout == SHORT_TRANSCRIPT.encode()
