"""Voxize GTK frontend — a protocol client of voxize.backend.

The window, keys, clipboard and context detection live here. Everything
else — audio, the three transcription phases, progress estimation, costs —
runs in the backend process spawned by gui/__main__.py and arrives as
protocol events (voxize.protocol).

Environment variables:
    VOXIZE_AUTOCLOSE=<s>   Overrides [ui] autoclose_seconds from voxize.toml
                           (0 disables the auto-close timer)
    VOXIZE_MOCK*, VOXIZE_TRACE — see voxize.backend.__main__
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

# gi.require_version() above is executable code; all subsequent imports trigger E402.
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from voxize import clipboard, config  # noqa: E402
from voxize._trace import trace as _trace  # noqa: E402
from voxize.frontend.client import HELLO_TIMEOUT_S, ClientCore  # noqa: E402
from voxize.frontend.session_log import SessionLog  # noqa: E402
from voxize.gui.backend_client import BackendClient  # noqa: E402
from voxize.gui.context import detect_context  # noqa: E402
from voxize.gui.ui import OverlayWindow, describe_widget  # noqa: E402
from voxize.protocol import State  # noqa: E402

logger = logging.getLogger(__name__)

_IN_FLIGHT = (
    State.INITIALIZING,
    State.WARMING,
    State.RECORDING,
    State.TRANSCRIBING,
    State.CLEANING,
)


def _autoclose_seconds() -> int:
    """Resolve autoclose: VOXIZE_AUTOCLOSE overrides ``[ui] autoclose_seconds``."""
    env = os.environ.get("VOXIZE_AUTOCLOSE")
    if env is not None:
        try:
            return int(env)
        except ValueError:
            logger.debug("VOXIZE_AUTOCLOSE=%r is not an int, ignoring", env)
    return config.CONFIG.ui.autoclose_seconds


class VoxizeApp(Gtk.Application):
    def __init__(self, backend: subprocess.Popen) -> None:
        super().__init__(
            application_id="dev.flemma.Voxize",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self._backend = backend
        # Client rules (hello, version, sticky failure, terminal states,
        # close-once) live in ClientCore; its transport arrives in
        # do_activate.
        self._core = ClientCore(client_noun="this window")
        self._ui: OverlayWindow | None = None
        self._trash_if_empty = False
        # GUI log lines wait in memory until the backend names the session
        # folder, then go to gui.log there.
        self._log = SessionLog("gui.log")

    def do_activate(self) -> None:
        _trace("do_activate entry")

        Adw.init()
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        # Context BEFORE presenting the window — once ours takes focus, the
        # focused-window query would return our own PID. Mock mode skips it,
        # as it always has.
        if os.environ.get("VOXIZE_MOCK"):
            cwd, glossaries = None, []
        else:
            cwd, glossaries = detect_context()
        _trace(f"detect_context done (glossaries={len(glossaries)})")

        self._core.transport = BackendClient(
            self._backend, on_event=self._on_event, on_lost=self._core.backend_lost
        )
        self._core.send("start", cwd=cwd, glossaries=glossaries)
        _trace("start sent")

        # Load CSS theme — dictation.css only. style.css belongs to the
        # meeting recorder; both define a .status-dot that must not mix.
        display = Gdk.Display.get_default()
        css = Gtk.CssProvider()
        css.load_from_string((Path(__file__).parent / "dictation.css").read_text())
        Gtk.StyleContext.add_provider_for_display(
            display,
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        _trace("CSS loaded")

        # Window — stock libadwaita window and shadow. Non-resizable, or the
        # card cannot grow past the size it was mapped at. The size request
        # also replaces Adw's default 360x200, whose 200px height floor
        # would pad the short warming layout.
        win = Adw.ApplicationWindow(application=self)
        win.set_resizable(False)
        win.set_default_size(460, -1)
        win.set_size_request(460, -1)
        win.connect("close-request", self._on_close_request)

        # Escape key. CAPTURE, not the default BUBBLE: the read-only TextView
        # takes focus and handles Ctrl+C itself, so in the bubble phase it
        # swallowed the accelerator and copied its (empty) selection instead
        # of the transcript. Unhandled keys still fall through to the widgets.
        ctrl = Gtk.EventControllerKey()
        ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ctrl.connect("key-pressed", self._on_key, win)
        win.add_controller(ctrl)

        self._ui = OverlayWindow(
            win,
            self._core.state,
            autoclose_seconds=_autoclose_seconds(),
            on_switch_to_meeting=self._switch_to_meeting,
            on_stop=lambda: self._core.send("stop"),
            on_cancel=lambda: self._core.send("cancel"),
        )
        _trace("OverlayWindow constructed")

        win.present()
        _trace("win.present() called")
        self._ui.setup_max_height()

        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal, sig)
        GLib.timeout_add_seconds(HELLO_TIMEOUT_S, self._check_hello)
        _trace("do_activate returning")

    # ── Backend events ──

    def _on_event(self, msg: dict) -> None:
        msg = self._core.handle(msg)
        if msg is None:
            return  # hello/state, or dropped after a GUI-side failure
        kind = msg["type"]
        ui = self._ui
        if kind == "context":
            ui.show_prompt_context(msg["sources"])
        elif kind == "session":
            ui.set_session_dir(msg["dir"])
            self._log.attach(msg["dir"])
        elif kind == "level":
            ui.set_level(msg["dbfs"])
        elif kind == "text":
            ui.on_text(msg["stage"], msg["op"], msg["text"])
        elif kind == "preview_lost":
            ui.show_live_preview_error(msg["reason"])
        elif kind == "progress":
            ui.set_progress(msg["fraction"])
        elif kind == "stalled":
            ui.set_stalled(msg["stalled"])
        elif kind == "result":
            # Batch first, then cleaned overwrites it.
            clipboard.copy(msg["text"], window_active=ui.window_active())
        elif kind == "no_speech":
            ui.show_no_speech()
        elif kind == "failure":
            ui.show_failure(msg["phase"], msg["message"], msg["fallback"])
        elif kind == "costs":
            ui.show_session_costs(msg["live"], msg["batch"], msg["cleanup"])
        else:
            logger.debug("_on_event: unknown event type=%s", kind)

    def _check_hello(self) -> bool:
        self._core.check_hello()
        return False  # one-shot

    # ── Mode switching ──

    def _switch_to_meeting(self) -> None:
        """Spawn the meeting recorder and close this dictation.

        The backend cancels an in-flight dictation on close, and trashes the
        session folder if it never produced any transcript.
        """
        logger.info("_switch_to_meeting: state=%s", self._core.state.state.name)
        try:
            subprocess.Popen([sys.executable, "-m", "voxize.meeting"])
        except Exception:
            logger.exception("failed to spawn meeting welcome")
        self._trash_if_empty = True
        if self._ui is not None:
            self._ui.close()

    # ── Window events ──

    def _on_key(self, _ctrl, keyval, _code, mod, win) -> bool:
        if mod & Gdk.ModifierType.CONTROL_MASK and self._ui is not None:
            if keyval == Gdk.KEY_c:
                # A live selection means the user asked for that text, not
                # the whole transcript — let the TextView serve it.
                if self._ui.has_text_selection():
                    return False
                self._ui.copy_to_clipboard()
                return True
            if keyval == Gdk.KEY_o:
                self._ui._on_open_folder(None)
                return True
            if keyval == Gdk.KEY_m and self._ui._on_switch_to_meeting is not None:
                self._switch_to_meeting()
                return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            # Diagnostics only: where Enter lands decides whether the
            # window's default widget (Stop / Copy / Close) is activated.
            logger.debug(
                "_on_key: %s state=%s focus=%s default=%s",
                Gdk.keyval_name(keyval),
                self._core.state.state.name,
                describe_widget(win.get_focus()),
                describe_widget(win.get_default_widget()),
            )
        if keyval != Gdk.KEY_Escape:
            return False
        # This controller runs in CAPTURE on the window, ahead of any open
        # popover or in-window dialog. Escape there means "close it" (for
        # the cancel dialog: Keep), not "cancel".
        if self._ui is not None and self._ui.overlay_open():
            return False
        s = self._core.state.state
        logger.debug("_on_key: Escape pressed, state=%s", s)
        if s in _IN_FLIGHT:
            # Asks first once audio has been recorded; Escape never discards.
            self._ui.request_cancel()
        elif s in (State.READY, State.ERROR):
            win.close()
        return True

    def _on_signal(self, sig: int) -> bool:
        """SIGTERM/SIGINT — hand the backend EOF and quit.

        The backend treats stdin EOF as cancel + close: it finalizes the
        WAV, releases the mic lock and restores ducked volumes itself.
        """
        logger.info("Signal %s received, shutting down", sig)
        self._core.hang_up()
        self._log.detach()
        self.quit()
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, _win) -> bool:
        logger.debug("_on_close_request: trash_if_empty=%s", self._trash_if_empty)
        clipboard.salvage()
        self._core.close(trash_if_empty=self._trash_if_empty)
        if self._ui is not None:
            self._ui.destroy()
        self._log.detach()
        # Quit even if GLib sources (signal handlers, idle callbacks) are pending.
        self.quit()
        return False  # allow close
