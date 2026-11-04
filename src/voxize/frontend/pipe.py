"""A frontend's end of the stdio pipe to voxize.backend, minus the event loop.

Commands go out on the child's stdin; events come back on its stdout. The
child's stderr is logged and kept as a short tail, so a dead backend can be
explained by its last words. Pipe failures never raise into the frontend: a
dead backend shows up as one ``on_lost`` call. Exceptions from the handlers
are logged and swallowed so a buggy handler cannot take down the read watch.

No event loop here. A subclass watches the two read fds on its own loop and
calls ``read_stdout()`` / ``read_stderr()`` when they are readable; each
returns False once its fd is done and should no longer be watched
(voxize.gui.backend_client: GLib; voxize.tui.backend: asyncio).
"""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import subprocess
import sys
from collections.abc import Callable

from voxize import protocol

logger = logging.getLogger(__name__)


class BackendPipe:
    def __init__(
        self,
        proc: subprocess.Popen,
        on_event: Callable[[dict], None],
        on_lost: Callable[[str], None],
        *,
        stderr_logger: str,
        echo_stderr: bool,
    ) -> None:
        self._proc = proc
        self._on_event = on_event
        self._on_lost = on_lost
        self._stderr_log = logging.getLogger(stderr_logger)
        self._echo_stderr = echo_stderr
        self._stdin_open = True
        self._finished = False
        self._lost_reported = False
        self._eof = False
        self._out_buf = b""
        self._err_buf = b""
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        # stderr is drained synchronously on stdout EOF, so it must not block.
        os.set_blocking(proc.stderr.fileno(), False)

    @property
    def eof(self) -> bool:
        return self._eof

    def send(self, type_: str, **fields) -> None:
        """Send one command. Never raises on pipe failures.

        A malformed command (wrong field set) is a bug and raises
        ``protocol.ProtocolError``.
        """
        # backslashreplace: a cwd read from /proc may hold non-UTF-8 bytes
        # (surrogateescape'd in the str). They go out as a JSON \udcXX escape
        # and come back as the same str, so os.fsencode recovers the bytes.
        data = protocol.encode(protocol.command(type_, **fields)).encode(
            "utf-8", errors="backslashreplace"
        )
        if not self._stdin_open:
            logger.debug("send: %s dropped, stdin closed", type_)
            return
        try:
            self._proc.stdin.write(data)
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

    def read_stdout(self) -> bool:
        """Read what stdout holds and dispatch whole lines. False at EOF."""
        try:
            chunk = os.read(self._proc.stdout.fileno(), 65536)
        except OSError:
            chunk = b""
        if not chunk:
            self._eof = True
            if self._out_buf.strip():
                logger.debug(
                    "stdout: dropping trailing fragment %r", self._out_buf[:200]
                )
            self._out_buf = b""
            logger.debug("stdout: EOF finished=%s", self._finished)
            if not self._finished and not self._lost_reported:
                self._lost_reported = True
                # The final stderr lines usually explain the death; they may
                # still be unread when stdout EOF is dispatched first.
                self._drain_stderr()
                try:
                    self._on_lost(self.last_stderr_line())
                except Exception:
                    logger.exception("on_lost handler failed")
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
            try:
                self._on_event(msg)
            except Exception:
                logger.exception("on_event handler failed for %r", msg.get("type"))
        return True

    def read_stderr(self) -> bool:
        """Read what stderr holds and log whole lines. False at EOF."""
        try:
            chunk = os.read(self._proc.stderr.fileno(), 65536)
        except BlockingIOError:
            return True
        except OSError:
            chunk = b""
        if not chunk:
            self._flush_stderr_partial()
            return False
        self._feed_stderr(chunk)
        return True

    def _drain_stderr(self) -> None:
        """Read whatever stderr holds right now, then flush a partial line."""
        fd = self._proc.stderr.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            self._feed_stderr(chunk)
        self._flush_stderr_partial()

    def _feed_stderr(self, chunk: bytes) -> None:
        self._err_buf += chunk
        while b"\n" in self._err_buf:
            raw, self._err_buf = self._err_buf.split(b"\n", 1)
            self._log_stderr_line(raw)

    def _flush_stderr_partial(self) -> None:
        raw, self._err_buf = self._err_buf, b""
        self._log_stderr_line(raw)

    def _log_stderr_line(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line:
            return
        self._stderr_tail.append(line)
        self._stderr_log.debug("%s", line)
        if self._echo_stderr:
            # a broken stderr must not take the watch down
            with contextlib.suppress(OSError, ValueError):
                print(line, file=sys.stderr, flush=True)
