"""Clipboard integration via Gdk.Clipboard (Wayland-native)."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gdk", "4.0")

# gi.require_version() above is executable code; the subsequent import triggers E402.
from gi.repository import Gdk, GLib  # noqa: E402

logger = logging.getLogger(__name__)


def copy(text: str) -> None:
    """Copy text to the clipboard via Gdk.

    Posts to the GTK main thread via GLib.idle_add — safe to call from any thread.
    Failures are logged but never raised — clipboard is best-effort.
    """
    logger.debug("copy: text_len=%d", len(text))

    def _set_clipboard():
        try:
            display = Gdk.Display.get_default()
            if display:
                display.get_clipboard().set(text)
                logger.debug("copy: success")
            else:
                logger.debug("copy: no display")
        except Exception:
            logger.exception("clipboard set failed")
        return False  # one-shot idle

    GLib.idle_add(_set_clipboard)
