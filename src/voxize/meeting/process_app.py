"""Post-meeting processing application — transcription workbench.

Operates on a completed session directory. Lets the user configure
transcription parameters (speaker count, key terms), run ElevenLabs
Scribe v2 batch transcription, and copy the result. Detects previous
runs from file presence so returning to a session shows what's done.

Entry:
  python -m voxize.meeting --process <session-dir>
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from voxize.meeting.process_ui import ProcessWindow  # noqa: E402
from voxize.meeting.sessions import (  # noqa: E402
    inspect_session,
    load_transcribe_params,
)
from voxize.meeting.transcribe import (  # noqa: E402
    TranscribeParams,
    TranscribeResult,
    transcribe_meeting,
)

logger = logging.getLogger(__name__)


class ProcessApp(Gtk.Application):
    def __init__(self, session_dir: str) -> None:
        super().__init__(
            application_id="dev.flemma.VoxizeMeetingProcess",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self._session_dir = session_dir
        self._ui: ProcessWindow | None = None
        self._window: Gtk.ApplicationWindow | None = None
        self._transcribe_abort = threading.Event()
        self._transcribe_running = False
        self._log_handler: logging.FileHandler | None = None

    # ── Activation ──

    def do_activate(self) -> None:
        log_path = os.path.join(self._session_dir, "debug.log")
        fh = logging.FileHandler(log_path)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d %(threadName)-14s %(name)s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        logging.getLogger("voxize").addHandler(fh)
        logging.getLogger("voxize").setLevel(logging.DEBUG)
        self._log_handler = fh
        logger.info("ProcessApp init: session_dir=%s", self._session_dir)

        css = Gtk.CssProvider()
        css_path = Path(__file__).parent.parent / "style.css"
        css.load_from_string(css_path.read_text())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        win = Gtk.ApplicationWindow(application=self)
        win.set_title("Voxize Meeting")
        win.set_resizable(False)
        win.set_default_size(440, -1)
        win.connect("close-request", self._on_close_request)

        ctrl = Gtk.EventControllerKey()
        ctrl.connect("key-pressed", self._on_key)
        win.add_controller(ctrl)

        session = inspect_session(self._session_dir)
        params = load_transcribe_params(self._session_dir)

        self._window = win
        self._ui = ProcessWindow(
            window=win,
            session=session,
            params=params,
            on_transcribe=self._start_transcribe,
            on_back=self._go_back,
        )
        win.present()

        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal, sig)

    # ── Transcription lifecycle ──

    def _start_transcribe(self, params: TranscribeParams) -> None:
        if self._transcribe_running:
            return
        self._transcribe_abort.clear()
        self._transcribe_running = True
        if self._ui:
            self._ui.mark_transcribing()
        threading.Thread(
            target=self._transcribe_thread,
            args=(params,),
            daemon=True,
            name="meeting-transcribe",
        ).start()

    def _transcribe_thread(self, params: TranscribeParams) -> None:
        opus_path = os.path.join(self._session_dir, "recording.opus")
        result = transcribe_meeting(
            opus_path=opus_path,
            session_dir=self._session_dir,
            params=params,
            stop_event=self._transcribe_abort,
            on_progress=self._on_progress,
        )
        logger.info(
            "transcribe: success=%s reason=%s elapsed=%.1fs",
            result.success,
            result.error_reason,
            result.elapsed_s,
        )
        GLib.idle_add(self._on_transcribe_done, result)

    def _on_progress(self, phase: str, elapsed_s: float) -> None:
        if self._ui:
            GLib.idle_add(self._ui.update_transcribe_elapsed, phase, elapsed_s)

    def _on_transcribe_done(self, result: TranscribeResult) -> bool:
        self._transcribe_running = False
        if not self._ui:
            return False
        if result.success:
            self._ui.mark_transcribe_done(result)
        elif result.error_reason == "aborted":
            self._ui.mark_transcribe_idle()
        else:
            self._ui.show_error(f"Transcription failed: {result.error_reason}")
            self._ui.mark_transcribe_idle()
        return False

    # ── Navigation ──

    def _go_back(self) -> None:
        if self._transcribe_running:
            return
        try:
            subprocess.Popen([sys.executable, "-m", "voxize.meeting"])
            logger.info("spawned welcome screen, quitting process app")
        except Exception:
            logger.exception("failed to spawn welcome screen")
        self.quit()

    # ── Close / signal handling ──

    def _on_close_request(self, _win) -> bool:
        if self._transcribe_running:
            self._transcribe_abort.set()
            return True
        return False

    def _on_key(self, _ctrl, keyval, _code, _mod) -> bool:
        if keyval == Gdk.KEY_Escape:
            if self._transcribe_running:
                self._transcribe_abort.set()
            else:
                self.quit()
            return True
        return False

    def _on_signal(self, sig: int) -> bool:
        logger.info("signal %s received", sig)
        if self._transcribe_running:
            self._transcribe_abort.set()
        else:
            self.quit()
        return GLib.SOURCE_REMOVE

    def do_shutdown(self) -> None:
        if self._log_handler:
            logging.getLogger("voxize").removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None
        Gtk.Application.do_shutdown(self)
