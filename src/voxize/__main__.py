"""Voxize entry point.

Mock mode (env vars):
    VOXIZE_MOCK=1                  Use mock providers (no mic, no API)
    VOXIZE_ERROR=<ms>              Simulate WebSocket error after <ms>
    VOXIZE_STOP=<ms>               Auto-stop recording after <ms>

Example:
    VOXIZE_MOCK=1 VOXIZE_ERROR=3000 uv run python -m voxize
"""

import logging
import os

logger = logging.getLogger(__name__)

if not os.environ.get("VOXIZE_MOCK"):
    from voxize.checks import exit_on_failure

    exit_on_failure()

from voxize import config, openai_patches  # noqa: E402
from voxize.app import VoxizeApp  # noqa: E402 — must follow exit_on_failure()

# Load user config exactly once, before any module reads it. First launch
# creates ~/.config/voxize/voxize.toml populated with commented defaults.
config.load()

# Patch openai SDK so its streaming responses drain cleanly and httpx can
# reuse the underlying TCP/TLS connection across batch → cleanup. Remove
# this call (and src/voxize/openai_patches.py) if upstream fixes the bug.
openai_patches.install()


def main() -> None:
    mock = bool(os.environ.get("VOXIZE_MOCK"))
    logger.info("main: starting voxize mock=%s", mock)
    if mock:
        logger.debug(
            "main: mock env VOXIZE_ERROR=%s VOXIZE_STOP=%s",
            os.environ.get("VOXIZE_ERROR"),
            os.environ.get("VOXIZE_STOP"),
        )
    app = VoxizeApp()
    app.run([])


if __name__ == "__main__":
    main()
