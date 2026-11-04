"""Voxize backend — one dictation session over stdio (JSON Lines).

    python -m voxize.backend

No GTK. Spawned by a frontend (voxize.gui, later voxize.tui), which sends
commands on stdin and reads events from stdout; see voxize.protocol.
stdout carries protocol lines only, and everything else goes to stderr.

Mock mode (env vars):
    VOXIZE_MOCK=1                  Use mock providers (no mic, no API)
    VOXIZE_ERROR=<ms>              Simulate WebSocket error after <ms>
    VOXIZE_STOP=<ms>               Auto-stop <ms> after entering RECORDING
    VOXIZE_MOCK_OUTCOME=<name>     ok | no-speech | batch-fail | cleanup-fail | error
    VOXIZE_MOCK_TTFB=<ms>          Mock batch wait before first token (stall: 8000)
    VOXIZE_MOCK_SHORT=1            Short dictation, no prediction — the bar pulses
    VOXIZE_MOCK_FAST=1             Shrink fixed mock delays (tests)
    VOXIZE_TRACE=1                 Print startup phase timings to stderr
"""

import os
import threading

from voxize.backend.stdio import claim_stdout

# Before anything else can write to stdout.
_PROTO_FD = claim_stdout()

from voxize import _trace  # noqa: E402 — stdout must be claimed first

_trace.set_tag("backend")
_trace.trace("__main__ start (after interpreter + site)")

# Kick off the ~1.9s PortAudio Pa_Initialize() immediately, in a background
# thread, while the rest of startup proceeds.
if not os.environ.get("VOXIZE_MOCK"):

    def _preload_sounddevice():
        import sounddevice  # noqa: F401 — triggers Pa_Initialize()

        _trace.trace("preload: sounddevice ready (Pa_Initialize done)")

    threading.Thread(
        target=_preload_sounddevice, daemon=True, name="voxize-preload"
    ).start()
    _trace.trace("preload: sounddevice import started in background")

from voxize import config  # noqa: E402
from voxize.backend.server import Server  # noqa: E402
from voxize.mock import MockOptions  # noqa: E402

_trace.trace("imported voxize.backend.server")


def main() -> None:
    config.load()
    _trace.trace("config.load() complete")
    mock = MockOptions.from_env()
    server = Server(in_fd=0, out_fd=_PROTO_FD, mock=mock)
    server.send_hello()
    _trace.trace("hello sent")
    errors: list[str] = []
    if mock is None:
        from voxize.checks import check_all

        errors = check_all()
        _trace.trace(f"checks done (errors={len(errors)})")
    server.run(errors)


if __name__ == "__main__":
    main()
