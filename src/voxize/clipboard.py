"""Clipboard integration via Gdk.Clipboard (Wayland-native).

On Wayland, only the focused window can write to the clipboard. When a copy
is attempted while the window is unfocused, the text is stashed and retried
the next time the window regains focus (via ``flush``).
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gdk", "4.0")

# gi.require_version() above is executable code; the subsequent import triggers E402.
from gi.repository import Gdk, GLib  # noqa: E402

logger = logging.getLogger(__name__)

_pending: str | None = None


def copy(text: str, *, window_active: bool = True) -> None:
    """Copy text to the clipboard via Gdk.

    Posts to the GTK main thread via GLib.idle_add — safe to call from any thread.
    Failures are logged but never raised — clipboard is best-effort.

    When *window_active* is False the write is deferred — call ``flush()`` when
    the window regains focus.
    """
    global _pending
    logger.debug("copy: text_len=%d window_active=%s", len(text), window_active)

    if not window_active:
        _pending = text
        logger.debug("copy: deferred (window not active)")
        return

    _pending = None

    def _set_clipboard():
        global _pending
        try:
            display = Gdk.Display.get_default()
            if display:
                display.get_clipboard().set(text)
                logger.debug("copy: success")
            else:
                logger.debug("copy: no display — stashing as pending")
                _pending = text
        except Exception:
            logger.exception("clipboard set failed — stashing as pending")
            _pending = text
        return False  # one-shot idle

    GLib.idle_add(_set_clipboard)


def flush() -> None:
    """Retry a previously deferred clipboard write.

    Call this from the window's focus-gained handler. No-op if there is
    nothing pending.
    """
    global _pending
    text = _pending
    if text is None:
        return
    _pending = None
    logger.debug("flush: retrying deferred copy, text_len=%d", len(text))

    def _set_clipboard():
        global _pending
        try:
            display = Gdk.Display.get_default()
            if display:
                display.get_clipboard().set(text)
                logger.debug("flush: success")
            else:
                logger.debug("flush: no display — re-stashing")
                _pending = text
        except Exception:
            logger.exception("flush: clipboard set failed — re-stashing")
            _pending = text
        return False

    GLib.idle_add(_set_clipboard)
