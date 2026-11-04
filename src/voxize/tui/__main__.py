"""Voxize terminal client entry point (``voxize-tui``, ``python -m voxize.tui``).

The backend is spawned before anything else — before Textual is imported
(~340 ms) — so its sounddevice preload overlaps that import. VOXIZE_MOCK*
and VOXIZE_TRACE reach it by inheritance. On exit, once the screen is
restored, the transcript goes to stdout (voxize.tui.deliver.final_output).
"""

import logging
import os
import subprocess
import sys

from voxize import _trace

_trace.set_tag("tui")
_trace.trace("__main__ start (after interpreter + site)")
if os.environ.get("VOXIZE_TRACE"):
    _trace.export_origin()  # the backend's t=0 becomes ours

BACKEND = subprocess.Popen(
    [sys.executable, "-m", "voxize.backend"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
_trace.trace("backend spawned")

from voxize.tui.app import (  # noqa: E402 — after the backend spawn
    BACKGROUND_ENV,
    VoxizeTui,
)
from voxize.tui.backend import AsyncBackendClient  # noqa: E402
from voxize.tui.deliver import (  # noqa: E402
    claim_terminal_stdout,
    final_output,
    write_output,
)

_trace.trace("imported voxize.tui.app")


def main() -> None:
    # Mock mode reads no WHISPER.txt, as in the GUI.
    cwd = None if os.environ.get("VOXIZE_MOCK") else os.getcwd()
    app = VoxizeTui(
        lambda on_event, on_lost: AsyncBackendClient(BACKEND, on_event, on_lost),
        cwd=cwd,
        # e.g. #100b06 — a tint for a tmux floating pane; unset keeps textual-dark.
        background=os.environ.get(BACKGROUND_ENV),
    )
    # Decided before claiming: final_output wants the original stdout's kind.
    isatty = sys.stdout.isatty()
    out_fd = claim_terminal_stdout()
    _trace.trace("VoxizeTui() constructed; entering app.run()")
    # From here Textual owns stderr (the screen): trace lines go to tui.log.
    _trace.set_sink(logging.getLogger("voxize.tui.trace").debug)
    try:
        text = app.run()
    finally:
        _trace.set_sink(None)
        app.session_log.detach()
    out, code = final_output(text, isatty)
    if out_fd is not None:
        write_output(out_fd, out)
    elif out:
        sys.stdout.write(out)
        sys.stdout.flush()
    sys.exit(code)


if __name__ == "__main__":
    main()
