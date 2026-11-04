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
import logging.handlers
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

from voxize import clipboard, config, protocol  # noqa: E402
from voxize._trace import trace as _trace  # noqa: E402
from voxize.gui.backend_client import BackendClient  # noqa: E402
from voxize.gui.client_state import ClientState  # noqa: E402
from voxize.gui.context import detect_context  # noqa: E402
from voxize.gui.ui import OverlayWindow, describe_widget  # noqa: E402
from voxize.protocol import State  # noqa: E402

logger = logging.getLogger(__name__)

_HELLO_TIMEOUT_S = 5
_TERMINAL = (State.READY, State.CANCELLED, State.ERROR)
_IN_FLIGHT = (
    State.INITIALIZING,
    State.WARMING,
    State.RECORDING,
    State.TRANSCRIBING,
    State.CLEANING,
)
_LOG_FORMAT = "%(asctime)s.%(msecs)03d %(threadName)-14s %(name)s %(message)s"


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
        self._client: BackendClient | None = None
        self._state = ClientState()
        self._ui: OverlayWindow | None = None
        self._hello = False
        self._closed = False
        self._trash_if_empty = False
        # GUI log lines wait in memory until the backend names the session
        # folder, then go to gui.log there (see _attach_log).
        self._log_buffer = logging.handlers.MemoryHandler(
            capacity=100_000, flushLevel=logging.CRITICAL + 1
        )
        self._log_buffer.setLevel(logging.DEBUG)
        logging.getLogger("voxize").addHandler(self._log_buffer)
        logging.getLogger("voxize").setLevel(logging.DEBUG)
        self._log_handler: logging.Handler | None = None

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

        self._client = BackendClient(
            self._backend, on_event=self._on_event, on_lost=self._on_backend_lost
        )
        self._client.send("start", cwd=cwd, glossaries=glossaries)
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

        self._state.on_change(self._on_state_change)
        self._ui = OverlayWindow(
            win,
            self._state,
            autoclose_seconds=_autoclose_seconds(),
            on_switch_to_meeting=self._switch_to_meeting,
            on_stop=lambda: self._send("stop"),
            on_cancel=lambda: self._send("cancel"),
        )
        _trace("OverlayWindow constructed")

        win.present()
        _trace("win.present() called")
        self._ui.setup_max_height()

        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal, sig)
        GLib.timeout_add_seconds(_HELLO_TIMEOUT_S, self._check_hello)
        _trace("do_activate returning")

    # ── Backend events ──

    def _send(self, type_: str, **fields) -> None:
        if self._client is not None:
            self._client.send(type_, **fields)

    def _on_event(self, msg: dict) -> None:
        kind = msg["type"]
        ui = self._ui
        if kind == "hello":
            self._hello = True
            if msg.get("protocol") != protocol.PROTOCOL_VERSION:
                self._fail(
                    f"Backend speaks protocol {msg.get('protocol')}, "
                    f"this window expects {protocol.PROTOCOL_VERSION}"
                )
        elif kind == "context":
            ui.show_prompt_context(msg["sources"])
        elif kind == "session":
            ui.set_session_dir(msg["dir"])
            self._attach_log(msg["dir"])
        elif kind == "state":
            self._state.apply(State[msg["state"]], msg.get("error") or "")
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

    def _on_state_change(self, _state, _old: State, new: State) -> None:
        if new in _TERMINAL and self._client is not None:
            # The backend stays up until close; EOF from here is not "lost".
            self._client.finish()

    def _fail(self, message: str) -> None:
        """The GUI's one self-decided transition: the backend can't be used."""
        logger.warning("_fail: %s", message)
        if self._state.state not in _TERMINAL:
            self._state.apply(State.ERROR, error=message)
        if self._client is not None:
            self._client.finish()
            self._client.send("close", trash_if_empty=False)
            self._client.close_stdin()

    def _on_backend_lost(self, last_stderr_line: str) -> None:
        detail = f": {last_stderr_line}" if last_stderr_line else ""
        self._fail(f"Backend exited unexpectedly{detail}")

    def _check_hello(self) -> bool:
        if not self._hello and not self._closed:
            self._fail("Backend did not start")
        return False

    def _attach_log(self, session_dir: str) -> None:
        try:
            fh = logging.FileHandler(os.path.join(session_dir, "gui.log"))
        except OSError:
            logger.debug("_attach_log: gui.log failed", exc_info=True)
            return
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt="%H:%M:%S"))
        root = logging.getLogger("voxize")
        self._log_buffer.setTarget(fh)
        self._log_buffer.flush()
        root.removeHandler(self._log_buffer)
        root.addHandler(fh)
        self._log_handler = fh

    def _detach_log(self) -> None:
        root = logging.getLogger("voxize")
        root.removeHandler(self._log_buffer)
        if self._log_handler is not None:
            root.removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None

    # ── Mode switching ──

    def _switch_to_meeting(self) -> None:
        """Spawn the meeting recorder and close this dictation.

        The backend cancels an in-flight dictation on close, and trashes the
        session folder if it never produced any transcript.
        """
        logger.info("_switch_to_meeting: state=%s", self._state.state.name)
        try:
            subprocess.Popen([sys.executable, "-m", "voxize.meeting"])
        except Exception:
            logger.exception("failed to spawn meeting welcome")
        self._trash_if_empty = True
        if self._ui is not None:
            self._ui._window.close()

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
                self._state.state.name,
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
        s = self._state.state
        logger.debug("_on_key: Escape pressed, state=%s", s)
        if s in (
            State.INITIALIZING,
            State.WARMING,
            State.RECORDING,
            State.TRANSCRIBING,
            State.CLEANING,
        ):
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
        self._closed = True
        if self._client is not None:
            self._client.finish()
            self._client.close_stdin()
        self._detach_log()
        self.quit()
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, _win) -> bool:
        logger.debug("_on_close_request: trash_if_empty=%s", self._trash_if_empty)
        clipboard.salvage()
        if self._client is not None and not self._closed:
            self._closed = True
            self._client.finish()
            self._client.send("close", trash_if_empty=self._trash_if_empty)
            self._client.close_stdin()
        if self._ui is not None:
            self._ui.destroy()
        self._detach_log()
        # Quit even if GLib sources (signal handlers, idle callbacks) are pending.
        self.quit()
        return False  # allow close
