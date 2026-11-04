"""The GUI's end of the stdio pipe to voxize.backend: BackendPipe on GLib.

Events are dispatched on the GLib main loop. The child's stderr is logged
(it ends up in gui.log) and echoed to our own stderr so VOXIZE_TRACE stays
visible. Everything else (framing, the stderr tail, the lost report) is
voxize.frontend.pipe.BackendPipe.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from gi.repository import GLib

from voxize.frontend.pipe import BackendPipe


class BackendClient(BackendPipe):
    def __init__(
        self,
        proc: subprocess.Popen,
        on_event: Callable[[dict], None],
        on_lost: Callable[[str], None],
    ) -> None:
        super().__init__(
            proc,
            on_event,
            on_lost,
            stderr_logger="voxize.gui.backend",
            echo_stderr=True,
        )
        cond = GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR
        GLib.io_add_watch(
            proc.stdout.fileno(), GLib.PRIORITY_DEFAULT, cond, self._on_stdout
        )
        GLib.io_add_watch(
            proc.stderr.fileno(), GLib.PRIORITY_DEFAULT, cond, self._on_stderr
        )

    def _on_stdout(self, _fd: int, _cond) -> bool:
        return self.read_stdout()

    def _on_stderr(self, _fd: int, _cond) -> bool:
        return self.read_stderr()
