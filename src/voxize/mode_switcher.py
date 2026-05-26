"""Shared mode switcher — pill-shaped toggle for Dictate / Meeting.

Used by both the dictation overlay and the meeting welcome screen.
The CSS must be loaded at PRIORITY_USER to beat libadwaita's headerbar
button rules (which stack four :not() pseudo-classes for specificity).
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # noqa: E402

_CSS = """
.mode-switcher {
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 12px;
    padding: 3px;
}

.mode-switcher button {
    background: transparent;
    border: none;
    border-radius: 9px;
    padding: 4px 14px;
    min-height: 28px;
    font-weight: bold;
    box-shadow: none;
    transition: background 200ms ease;
}

.mode-switcher button:hover {
    background: rgba(255, 255, 255, 0.06);
}

.mode-switcher button:active {
    background: rgba(255, 255, 255, 0.12);
}

.mode-switcher button:checked {
    background: color-mix(in srgb, currentColor 15%, transparent);
}
"""


def load_css(display: Gdk.Display | None = None) -> None:
    """Load mode-switcher CSS at PRIORITY_USER (call once per app)."""
    if display is None:
        display = Gdk.Display.get_default()
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_USER,
    )


def build_titlebar(
    active: str,
    on_switch: Callable[[str], None],
    header: Gtk.HeaderBar,
) -> Gtk.Box:
    """Stack a mode-switcher HeaderBar above an existing HeaderBar.

    The top bar owns the window buttons (close/minimize) and is
    draggable. The bottom bar has its title buttons hidden.
    Returns a vertical Box suitable for ``window.set_titlebar()``.
    """
    top = Gtk.HeaderBar()
    top.set_title_widget(build(active, on_switch))

    header.set_show_title_buttons(False)

    titlebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    titlebar.append(top)
    titlebar.append(header)
    return titlebar


def build(active: str, on_switch: Callable[[str], None]) -> Gtk.Box:
    """Build a pill-shaped [Dictate | Meeting] toggle.

    ``active`` is ``"dictate"`` or ``"meeting"``.
    ``on_switch(mode)`` fires when the user clicks the *other* toggle.
    """
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
    box.add_css_class("mode-switcher")

    dictate_btn = Gtk.ToggleButton()
    dictate_btn.set_child(_content("audio-input-microphone-symbolic", "Dictate"))

    meeting_btn = Gtk.ToggleButton()
    meeting_btn.set_child(_content("system-users-symbolic", "Meeting"))

    meeting_btn.set_group(dictate_btn)

    if active == "meeting":
        meeting_btn.set_active(True)
    else:
        dictate_btn.set_active(True)

    def _on_toggled(btn, mode):
        if btn.get_active():
            on_switch(mode)

    dictate_btn.connect("toggled", _on_toggled, "dictate")
    meeting_btn.connect("toggled", _on_toggled, "meeting")

    box.append(dictate_btn)
    box.append(meeting_btn)

    return box


def _content(icon_name: str, label: str) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    box.append(Gtk.Image.new_from_icon_name(icon_name))
    box.append(Gtk.Label(label=label))
    return box
