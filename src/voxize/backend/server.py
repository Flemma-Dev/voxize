"""Stdio server: commands in on stdin, events out on a private fd.

One Session per process. Commands are dispatched on the GLib main loop as
lines arrive; events are written as Session emits them (always on the main
loop, so there is one writer). Every message in and out is also appended to
``protocol.jsonl`` in the session folder.

End of life:
- ``close`` → Session.close → _exit.
- stdin EOF without ``close`` (the frontend died) → treated as
  ``close {trash_if_empty: false}``, so an orphaned backend never holds the
  mic or leaves apps ducked.
- SIGTERM/SIGINT → Session.on_signal → _exit.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import time

from gi.repository import GLib

from voxize import protocol
from voxize.backend.session import Session
from voxize.mock import MockOptions

logger = logging.getLogger(__name__)


class ProtocolLog:
    """``protocol.jsonl``: every message in and out, timestamped.

    Messages before the session folder exists are buffered and written once
    the ``session`` event names it. Best-effort: never raises.
    """

    def __init__(self) -> None:
        self._pending: list[str] | None = []
        self._f = None

    def attach(self, session_dir: str) -> None:
        try:
            self._f = open(  # noqa: SIM115 — lives for the whole session
                os.path.join(session_dir, "protocol.jsonl"),
                "a",
                encoding="utf-8",
                buffering=1,
            )
            for record in self._pending or []:
                self._f.write(record)
        except OSError:
            logger.debug("protocol log: attach failed", exc_info=True)
            self._f = None
        self._pending = None

    def write(self, direction: str, msg: dict) -> None:
        record = (
            json.dumps(
                {"t": round(time.time(), 3), "dir": direction, "msg": msg},
                ensure_ascii=False,
            )
            + "\n"
        )
        if self._f is not None:
            with contextlib.suppress(OSError):
                self._f.write(record)
        elif self._pending is not None:
            self._pending.append(record)

    def close(self) -> None:
        if self._f is not None:
            with contextlib.suppress(OSError):
                self._f.close()
            self._f = None
        self._pending = None


class Server:
    def __init__(self, in_fd: int, out_fd: int, *, mock: MockOptions | None) -> None:
        self._in_fd = in_fd
        self._out = os.fdopen(out_fd, "w", encoding="utf-8", buffering=1)
        self._out_dead = False
        self._closing = False  # close/EOF/signal seen: write errors are expected
        self._buf = b""
        self._loop = GLib.MainLoop()
        self._log = ProtocolLog()
        self.session = Session(self.send, mock=mock, on_exit=self._exit)

    def send(self, msg: dict) -> None:
        if msg["type"] == "session":
            self._log.attach(msg["dir"])
        self._log.write("out", msg)
        if self._out_dead:
            return
        try:
            self._out.write(protocol.encode(msg))
        except (BrokenPipeError, OSError, ValueError) as e:
            self._out_dead = True
            if not self._closing:
                logger.warning("send: frontend gone: %s", e)

    def send_hello(self) -> None:
        self.send(protocol.event("hello", protocol=protocol.PROTOCOL_VERSION))

    def run(self, startup_errors: list[str]) -> None:
        if startup_errors:
            self.session.fail_startup(startup_errors)
        GLib.io_add_watch(
            self._in_fd,
            GLib.PRIORITY_DEFAULT,
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            self._on_stdin,
        )
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal, sig)
        self._loop.run()

    def _on_stdin(self, fd: int, _cond) -> bool:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            logger.debug("stdin: EOF closed=%s", self.session.closed)
            if not self.session.closed:
                self._closing = True
                self.session.close(trash_if_empty=False)
            return False
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                self._dispatch(line)
        return True

    def _dispatch(self, line: bytes) -> None:
        try:
            msg = protocol.decode(line)
        except ValueError as e:
            logger.warning("stdin: dropping bad line %r: %s", line[:200], e)
            return
        self._log.write("in", msg)
        kind = msg["type"]
        logger.debug("stdin: command %s", kind)
        if kind == "start":
            self.session.start(msg.get("cwd"), list(msg.get("glossaries") or []))
        elif kind == "stop":
            self.session.stop()
        elif kind == "cancel":
            self.session.cancel()
        elif kind == "close":
            self._closing = True
            self.session.close(trash_if_empty=bool(msg.get("trash_if_empty")))
        else:
            logger.debug("stdin: unknown command type=%s", kind)

    def _on_signal(self, sig: int) -> bool:
        self._closing = True
        self.session.on_signal(sig)
        return GLib.SOURCE_REMOVE

    def _exit(self) -> None:
        """Session finished: release the frontend and stop the loop.

        Closing the protocol fd and pointing 0/1/2 at /dev/null makes the
        frontend see EOF on both pipes at once, while daemon teardown
        threads can still write to stderr without EBADF — the same trick
        the old window close used.
        """
        self._closing = True
        self._out_dead = True
        self._log.close()
        with contextlib.suppress(OSError):
            self._out.close()
        try:
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                try:  # noqa: SIM105 — one fd failing must not stop the others
                    os.dup2(devnull, fd)
                except OSError:
                    pass
            os.close(devnull)
        except OSError:
            pass
        self._loop.quit()
