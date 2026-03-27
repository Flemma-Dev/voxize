"""Clipboard integration via wl-copy (Wayland)."""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


def copy(text: str) -> None:
    """Copy text to the Wayland clipboard via wl-copy.

    Text is piped via stdin to avoid ARG_MAX limits on large transcripts.
    Failures are logged but never raised — clipboard is best-effort.
    """
    try:
        subprocess.run(
            ["wl-copy"],
            input=text,
            text=True,
            check=True,
            timeout=5,
        )
    except Exception:
        logger.exception("wl-copy failed")
