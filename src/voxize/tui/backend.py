"""The terminal client's end of the stdio pipe: BackendPipe on asyncio.

The backend's stdout and stderr are watched with ``loop.add_reader`` on
the app's event loop. Backend stderr goes to tui.log only (logger
``voxize.tui.backend``) and is never echoed: Textual draws on stderr.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable

from voxize.frontend.pipe import BackendPipe


class AsyncBackendClient(BackendPipe):
    def __init__(
        self,
        proc: subprocess.Popen,
        on_event: Callable[[dict], None],
        on_lost: Callable[[str], None],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        super().__init__(
            proc,
            on_event,
            on_lost,
            stderr_logger="voxize.tui.backend",
            echo_stderr=False,
        )
        self._loop = loop or asyncio.get_running_loop()
        self._watch(proc.stdout.fileno(), self.read_stdout)
        self._watch(proc.stderr.fileno(), self.read_stderr)

    def _watch(self, fd: int, read: Callable[[], bool]) -> None:
        def ready() -> None:
            if not read():
                self._loop.remove_reader(fd)

        self._loop.add_reader(fd, ready)
