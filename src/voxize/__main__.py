"""Voxize entry point.

Mock mode (env vars):
    VOXIZE_MOCK=1                  Use mock providers (no mic, no API)
    VOXIZE_ERROR=<ms>              Simulate WebSocket error after <ms>
    VOXIZE_STOP=<ms>               Auto-stop recording after <ms>
    VOXIZE_TRACE=1                 Print startup phase timings to stderr

Example:
    VOXIZE_MOCK=1 VOXIZE_ERROR=3000 uv run python -m voxize
"""

import logging
import os

from voxize._trace import trace as _trace

logger = logging.getLogger(__name__)

_trace("__main__ start (after interpreter + site)")

if not os.environ.get("VOXIZE_MOCK"):
    from voxize.checks import exit_on_failure

    _trace("imported voxize.checks")
    exit_on_failure()
    _trace("exit_on_failure complete (libsecret lookup done)")

from voxize import config  # noqa: E402

_trace("imported voxize.config")

from voxize.app import VoxizeApp  # noqa: E402 — must follow exit_on_failure()

_trace("imported voxize.app (gi, sounddevice — no openai yet)")

# Load user config exactly once, before any module reads it. First launch
# creates ~/.config/voxize/voxize.toml populated with commented defaults.
config.load()
_trace("config.load() complete")

# openai_patches.install() is deferred into the bootstrap thread so the
# openai SDK import (~300ms on warm cache, much more on cold) does not
# block window presentation or mic-open.


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
