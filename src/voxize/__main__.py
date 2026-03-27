"""Voxize entry point."""

from voxize.checks import exit_on_failure

exit_on_failure()

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk


class VoxizeApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="dev.voxize.overlay")

    def do_activate(self) -> None:
        window = Gtk.ApplicationWindow(application=self)
        window.set_decorated(False)
        window.set_resizable(False)
        window.set_default_size(420, -1)

        # Label
        label = Gtk.Label(label="Voxize")
        label.set_halign(Gtk.Align.CENTER)
        label.set_valign(Gtk.Align.CENTER)
        label.set_margin_top(24)
        label.set_margin_bottom(24)
        label.set_margin_start(24)
        label.set_margin_end(24)
        window.set_child(label)

        # CSS — dark translucent, rounded corners, no default GTK chrome
        css = Gtk.CssProvider()
        css.load_from_string(
            "window {"
            "  background-color: rgba(30, 30, 30, 0.85);"
            "  border: 1px solid rgba(255, 255, 255, 0.08);"
            "  border-radius: 12px;"
            "  color: rgba(255, 255, 255, 0.88);"
            "  font-family: monospace;"
            "  font-size: 13px;"
            "}"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Escape key to close
        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self._on_key_pressed, window)
        window.add_controller(controller)

        window.present()

    def _on_key_pressed(
        self,
        controller: Gtk.EventControllerKey,
        keyval: int,
        keycode: int,
        state: Gdk.ModifierType,
        window: Gtk.ApplicationWindow,
    ) -> bool:
        if keyval == Gdk.KEY_Escape:
            window.close()
            return True
        return False


def main() -> None:
    app = VoxizeApp()
    app.run([])


main()
