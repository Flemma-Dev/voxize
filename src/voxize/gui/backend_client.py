"""The GUI's end of the stdio pipe to voxize.backend.

Commands go out on the child's stdin; events come back on its stdout and
are dispatched on the GLib main loop. The child's stderr is logged (it ends
up in gui.log) and echoed to our own stderr so VOXIZE_TRACE stays visible.
Never raises into the UI: a dead backend shows up as one ``on_lost`` call.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import subprocess
import sys
from collections.abc import Callable

from gi.repository import GLib

from voxize import protocol

logger = logging.getLogger(__name__)
_backend_log = logging.getLogger("voxize.gui.backend")


class BackendClient:
    def __init__(
        self,
        proc: subprocess.Popen,
        on_event: Callable[[dict], None],
        on_lost: Callable[[str], None],
    ) -> None:
        self._proc = proc
        self._on_event = on_event
        self._on_lost = on_lost
        self._stdin_open = True
        self._finished = False
        self._lost_reported = False
        self._eof = False
        self._out_buf = b""
        self._err_buf = b""
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        cond = GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR
        GLib.io_add_watch(
            proc.stdout.fileno(), GLib.PRIORITY_DEFAULT, cond, self._on_stdout
        )
        GLib.io_add_watch(
            proc.stderr.fileno(), GLib.PRIORITY_DEFAULT, cond, self._on_stderr
        )

    @property
    def eof(self) -> bool:
        return self._eof

    def send(self, type_: str, **fields) -> None:
        if not self._stdin_open:
            logger.debug("send: %s dropped, stdin closed", type_)
            return
        line = protocol.encode(protocol.command(type_, **fields))
        try:
            self._proc.stdin.write(line.encode("utf-8"))
            self._proc.stdin.flush()
            logger.debug("send: %s", type_)
        except (BrokenPipeError, OSError, ValueError) as e:
            logger.debug("send: %s failed: %s", type_, e)
            # Close now: a BufferedWriter holding unflushed bytes for a dead
            # pipe would otherwise raise again from its finalizer.
            self.close_stdin()

    def close_stdin(self) -> None:
        if not self._stdin_open:
            return
        self._stdin_open = False
        with contextlib.suppress(OSError):
            self._proc.stdin.close()

    def finish(self) -> None:
        """A terminal state was seen or close was sent: EOF is expected now."""
        self._finished = True

    def last_stderr_line(self) -> str:
        return self._stderr_tail[-1] if self._stderr_tail else ""

    def _on_stdout(self, fd: int, _cond) -> bool:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            self._eof = True
            logger.debug("stdout: EOF finished=%s", self._finished)
            if not self._finished and not self._lost_reported:
                self._lost_reported = True
                self._on_lost(self.last_stderr_line())
            return False
        self._out_buf += chunk
        while b"\n" in self._out_buf:
            line, self._out_buf = self._out_buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = protocol.decode(line)
            except ValueError as e:
                logger.warning("stdout: dropping bad line %r: %s", line[:200], e)
                continue
            self._on_event(msg)
        return True

    def _on_stderr(self, fd: int, _cond) -> bool:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            return False
        self._err_buf += chunk
        while b"\n" in self._err_buf:
            raw, self._err_buf = self._err_buf.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            _backend_log.debug("%s", line)
            print(line, file=sys.stderr, flush=True)
        return True
