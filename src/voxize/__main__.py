"""Voxize entry point.

Mock mode (env vars):
    VOXIZE_MOCK=1                  Use mock providers (no mic, no API)
    VOXIZE_ERROR=<ms>              Simulate WebSocket error after <ms>
    VOXIZE_STOP=<ms>               Auto-stop recording after <ms>

Example:
    VOXIZE_MOCK=1 VOXIZE_ERROR=3000 uv run python -m voxize
"""

import os

if not os.environ.get("VOXIZE_MOCK"):
    from voxize.checks import exit_on_failure

    exit_on_failure()

from voxize.app import VoxizeApp


def main() -> None:
    app = VoxizeApp()
    app.run([])


main()
