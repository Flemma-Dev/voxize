"""Voxize entry point.

Mock mode (env vars):
    VOXIZE_MOCK=1                  Use mock providers (no mic, no API)
    VOXIZE_ERROR=<ms>              Simulate WebSocket error after <ms>
    VOXIZE_STOP=<ms>               Auto-stop recording after <ms>
    VOXIZE_MOCK_OUTCOME=<name>     ok | no-speech | batch-fail | cleanup-fail | error
    VOXIZE_MOCK_TTFB=<ms>          Mock batch wait before first token (stall: 8000)
    VOXIZE_MOCK_SHORT=1            Short dictation, no prediction — the bar pulses
    VOXIZE_TRACE=1                 Print startup phase timings to stderr

Example:
    VOXIZE_MOCK=1 VOXIZE_ERROR=3000 uv run python -m voxize
"""

import logging
import os
import threading

from voxize._trace import trace as _trace

logger = logging.getLogger(__name__)

_trace("__main__ start (after interpreter + site)")

# Kick off the ~1.9s PortAudio Pa_Initialize() immediately — it runs
# in a background thread while checks, config, and GTK imports proceed
# on the main thread. By the time the bootstrap thread needs
# sounddevice, the import (and device enumeration) is already done.
if not os.environ.get("VOXIZE_MOCK"):

    def _preload_sounddevice():
        import sounddevice  # noqa: F401 — triggers Pa_Initialize()

        _trace("preload: sounddevice ready (Pa_Initialize done)")

    threading.Thread(
        target=_preload_sounddevice, daemon=True, name="voxize-preload"
    ).start()
    _trace("preload: sounddevice import started in background")

if not os.environ.get("VOXIZE_MOCK"):
    from voxize.checks import exit_on_failure

    _trace("imported voxize.checks")
    exit_on_failure()
    _trace("exit_on_failure complete (libsecret lookup done)")

from voxize import config  # noqa: E402

_trace("imported voxize.config")

from voxize.app import VoxizeApp  # noqa: E402 — must follow exit_on_failure()

_trace("imported voxize.app (gi — no sounddevice/openai yet)")

# Load user config exactly once, before any module reads it. First launch
# creates ~/.config/voxize/voxize.toml populated with commented defaults.
config.load()
_trace("config.load() complete")

# Pa_Initialize() (~1.9s for PortAudio device enumeration) started in
# a background thread above, overlapping with checks + GTK imports.
# The openai SDK (~300ms) is deferred to the bootstrap thread.


def main() -> None:
    _trace("main() entry")
    mock = bool(os.environ.get("VOXIZE_MOCK"))
    logger.info("main: starting voxize mock=%s", mock)
    if mock:
        logger.debug(
            "main: mock env VOXIZE_ERROR=%s VOXIZE_STOP=%s",
            os.environ.get("VOXIZE_ERROR"),
            os.environ.get("VOXIZE_STOP"),
        )
    app = VoxizeApp()
    _trace("VoxizeApp() constructed; entering app.run()")
    app.run([])
    _trace("app.run() returned (process exit)")


if __name__ == "__main__":
    main()
