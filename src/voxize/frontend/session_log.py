"""A frontend's log file in the session folder (gui.log, tui.log).

Log lines from logger ``voxize`` wait in a bounded buffer until the backend
names the session folder (the ``session`` event), then go to
``<session dir>/<filename>``. A session that never names a folder (mock
mode, or an error before the session starts) keeps only the newest records.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
from typing import IO

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(threadName)-14s %(name)s %(message)s"
BUFFER_RECORDS = 10_000

logger = logging.getLogger(__name__)


class _LogBuffer(logging.handlers.MemoryHandler):
    """Holds log lines until the log file exists, keeping only the newest.

    A stock MemoryHandler never clears its buffer while it has no target,
    so a session that never names a folder would grow it without bound.
    """

    def shouldFlush(self, record: logging.LogRecord) -> bool:
        if self.target is None:
            if len(self.buffer) > self.capacity:
                del self.buffer[0]
            return False
        return super().shouldFlush(record)


class SessionLog:
    def __init__(self, filename: str, capacity: int = BUFFER_RECORDS) -> None:
        self._filename = filename
        self.buffer = _LogBuffer(capacity=capacity, flushLevel=logging.CRITICAL + 1)
        self.buffer.setLevel(logging.DEBUG)
        self._handler: logging.FileHandler | None = None
        root = logging.getLogger("voxize")
        root.addHandler(self.buffer)
        root.setLevel(logging.DEBUG)

    def attach(self, session_dir: str) -> None:
        """Start writing ``<session_dir>/<filename>``, buffered lines first."""
        if self._handler is not None:
            return
        try:
            fh = logging.FileHandler(os.path.join(session_dir, self._filename))
        except OSError:
            logger.debug("attach: %s failed", self._filename, exc_info=True)
            return
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))
        root = logging.getLogger("voxize")
        self.buffer.setTarget(fh)
        self.buffer.flush()
        root.removeHandler(self.buffer)
        root.addHandler(fh)
        self._handler = fh

    def detach(self) -> None:
        root = logging.getLogger("voxize")
        root.removeHandler(self.buffer)
        if self._handler is not None:
            root.removeHandler(self._handler)
            self._handler.close()
            self._handler = None

    def stderr_target(self) -> IO | int:
        """Where a child process's stderr goes: the open log file.

        ``subprocess.DEVNULL`` before ``attach`` — unreachable in practice for
        the clipboard tools, since ``result`` always follows ``session``.
        """
        if self._handler is not None and self._handler.stream is not None:
            return self._handler.stream
        return subprocess.DEVNULL
