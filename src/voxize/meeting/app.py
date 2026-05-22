"""Voxize Meeting Application — GTK wiring for the dual-stream recorder.

Lifecycle:
  do_activate → present window → idle_add(_initialize)
  _initialize: acquire lock → create session dir + debug.log →
               start DualStreamCapture → wire UI → start error poller
  _request_stop (Stop / Escape / window close):
               mark UI stopping → bg thread runs capture.stop() →
               idle_add(_finalize_app) → prune sessions → destroy window
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from voxize.lock import MicLock, MicLockError  # noqa: E402
from voxize.meeting.capture import CaptureError, DualStreamCapture  # noqa: E402
from voxize.meeting.compress import compress_meeting_wav  # noqa: E402
from voxize.meeting.ui import MeetingWindow  # noqa: E402
from voxize.storage import create_session_dir, prune_sessions  # noqa: E402

logger = logging.getLogger(__name__)

_LOCK_NAME = "voxize-meeting.lock"
_BUCKET = "meeting"
_SESSION_SUFFIX = f"-{_BUCKET}"
_ERROR_POLL_INTERVAL_MS = 500


class MeetingApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id="dev.flemma.VoxizeMeeting",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self._lock: MicLock | None = None
        self._session_dir: str | None = None
        self._capture: DualStreamCapture | None = None
        self._ui: MeetingWindow | None = None
        self._window: Gtk.ApplicationWindow | None = None
        self._log_handler: logging.FileHandler | None = None
        self._error_poll_source: int | None = None
        self._stopping = False
        self._compress_abort = threading.Event()
        self._compress_running = False
        # True once capture + compress finish (success or non-abort failure).
        # In this state the window stays open with a green/amber dot until
        # the user explicitly closes it — matches the dictation overlay's
        # READY behaviour so the user has visible confirmation things
        # worked, plus a still-clickable folder button to find the file.
        self._session_done = False

    # ── Activation ──

    def do_activate(self) -> None:
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
        win.set_default_size(380, -1)
        win.connect("close-request", self._on_close_request)

        ctrl = Gtk.EventControllerKey()
        ctrl.connect("key-pressed", self._on_key)
        win.add_controller(ctrl)

        self._window = win
        self._ui = MeetingWindow(win, on_stop=self._request_stop)
        win.present()

        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal, sig)

        # Defer heavy bring-up so the window paints first; failures land
        # in the visible error bar instead of an invisible crash.
        GLib.idle_add(self._initialize)

    # ── Initialization ──

    def _initialize(self) -> bool:
        try:
            self._lock = MicLock(lock_name=_LOCK_NAME)
            self._lock.acquire()
        except MicLockError as e:
            logger.error("lock acquire failed: %s", e)
            self._show_fatal(f"Another Voxize Meeting is already running ({e})")
            return False
        except Exception as e:
            logger.exception("lock acquire crashed")
            self._show_fatal(f"Could not acquire lock: {e}")
            return False

        try:
            self._session_dir = create_session_dir(suffix=_SESSION_SUFFIX)
        except Exception as e:
            logger.exception("session_dir creation failed")
            self._release_lock()
            self._show_fatal(f"Could not create session directory: {e}")
            return False

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
        logger.info("MeetingApp init: session_dir=%s", self._session_dir)

        try:
            self._capture = DualStreamCapture(self._session_dir)
            self._capture.start()
            logger.info("capture started: default_sink=%s", self._capture.default_sink)
        except CaptureError as e:
            logger.exception("capture start failed")
            self._release_lock()
            self._show_fatal(str(e))
            return False
        except Exception as e:
            logger.exception("capture start crashed")
            self._release_lock()
            self._show_fatal(f"Capture crashed: {e}")
            return False

        self._ui.attach(
            mic_meter=self._capture.mic_meter,
            sys_meter=self._capture.sys_meter,
            data_bytes_getter=self._safe_data_bytes,
            session_dir=self._session_dir,
        )

        self._error_poll_source = GLib.timeout_add(
            _ERROR_POLL_INTERVAL_MS, self._check_errors
        )
        return False  # one-shot idle

    def _safe_data_bytes(self) -> int:
        return self._capture.data_bytes if self._capture else 0

    def _check_errors(self) -> bool:
        if self._stopping or not self._capture:
            self._error_poll_source = None
            return False
        err = self._capture.check_errors()
        if err and self._ui:
            self._ui.show_error(err)
        return True

    # ── Stop / teardown ──

    def _request_stop(self) -> None:
        """Begin teardown. Handles Stop, Close, Escape, and signal entries."""
        if self._session_done:
            # User clicked "Close" (or hit Esc) on the done screen — go
            # straight to finalize. Defer to next idle so the button
            # release event finishes painting before the window destroys.
            logger.debug("_request_stop: session done, finalizing")
            GLib.idle_add(self._finalize_app)
            return
        if self._stopping:
            # Already tearing down. If we're past capture and currently
            # compressing, treat the second request as "abort compression
            # and close" — keeps Esc / window-close responsive.
            if self._compress_running:
                logger.info("_request_stop: abort requested during compress")
                self._compress_abort.set()
            return
        self._stopping = True
        logger.info("_request_stop")
        if self._ui:
            self._ui.mark_stopping()
        if self._error_poll_source is not None:
            GLib.source_remove(self._error_poll_source)
            self._error_poll_source = None
        threading.Thread(
            target=self._stop_thread,
            daemon=True,
            name="meeting-stop",
        ).start()

    def _stop_thread(self) -> None:
        """Background: stop capture, compress WAV → Opus, trash WAV, finalize."""
        logger.debug("_stop_thread entry")
        capture = self._capture
        self._capture = None
        wav_data_bytes = 0
        try:
            if capture:
                wav_data_bytes = capture.data_bytes
                capture.stop()
                # ``capture.data_bytes`` is finalized post-stop; re-read so
                # the duration calc uses the exact byte count written.
                wav_data_bytes = capture.data_bytes
        except Exception:
            logger.exception("capture.stop failed")

        result = None
        if self._session_dir and wav_data_bytes > 0:
            GLib.idle_add(self._ui.mark_compressing)
            self._compress_abort.clear()
            self._compress_running = True
            try:
                result = compress_meeting_wav(
                    self._session_dir,
                    wav_data_bytes,
                    self._compress_abort,
                    on_progress=self._on_compress_progress,
                )
                logger.info(
                    "compress result: success=%s reason=%s elapsed=%.1fs",
                    result.success,
                    result.error_reason,
                    result.elapsed_s,
                )
            except Exception:
                logger.exception("compress: unexpected crash, WAV preserved")
            finally:
                self._compress_running = False

        self._release_lock()

        # Abort path or "nothing to compress" (zero-byte recording) →
        # close window immediately, same as before. Everything else
        # transitions to the done screen and waits for the user.
        if result is None or (not result.success and result.error_reason == "aborted"):
            GLib.idle_add(self._finalize_app)
            return

        self._session_done = True
        size_bytes = self._deliverable_size(result)
        if result.success:
            GLib.idle_add(self._ui.mark_done, True, size_bytes)
        else:
            GLib.idle_add(
                self._ui.show_error,
                f"Compression failed: {result.error_reason} — WAV preserved",
            )
            GLib.idle_add(self._ui.mark_done, False, size_bytes)

    def _deliverable_size(self, result) -> int:
        """Bytes on disk of the file the user will pick up — opus or WAV."""
        if self._session_dir is None:
            return 0
        path = (
            result.output_path
            if result.success and result.output_path
            else os.path.join(self._session_dir, "recording.wav")
        )
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def _on_compress_progress(self, elapsed_s: float) -> None:
        """Compress thread → GTK thread: push the elapsed seconds to the UI."""
        if self._ui:
            GLib.idle_add(self._ui.update_compress_elapsed, elapsed_s)

    def _finalize_app(self) -> bool:
        """GTK thread: prune, close logs, destroy window, quit."""
        logger.info("_finalize_app")
        try:
            prune_sessions(_BUCKET)
        except Exception:
            logger.debug("prune_sessions failed", exc_info=True)
        if self._log_handler:
            logging.getLogger("voxize").removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None
        if self._ui:
            self._ui.destroy()
            self._ui = None
        if self._window:
            # destroy() bypasses close-request so we don't re-enter teardown.
            self._window.destroy()
            self._window = None
        self.quit()
        return False  # one-shot idle

    def _release_lock(self) -> None:
        if self._lock:
            try:
                self._lock.release()
            except Exception:
                logger.exception("lock release failed")
            self._lock = None

    # ── Signal / window event handlers ──

    def _on_signal(self, sig: int) -> bool:
        """SIGTERM/SIGINT: finalize WAV header inline, then schedule clean stop."""
        logger.info("signal %s received", sig)
        # Rewrite the WAV size fields synchronously so the file on disk
        # is playable up to the moment we got the signal, even if the
        # rest of teardown is interrupted (e.g. by SIGKILL after a grace
        # period). Cheap — touches only 8 bytes.
        if self._capture:
            try:
                self._capture.finalize_wav()
            except Exception:
                logger.exception("finalize_wav failed in signal handler")
        # Kick off the normal stop path; if we're already mid-stop, this
        # is a no-op.
        self._request_stop()
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, _win) -> bool:
        """Window-X / Alt-F4: route through the same paths as the Close button."""
        logger.debug(
            "_on_close_request stopping=%s compress_running=%s done=%s",
            self._stopping,
            self._compress_running,
            self._session_done,
        )
        if self._session_done:
            # User dismissed the done screen — run the finalize path so
            # logs are flushed, prune runs, and the app quits cleanly.
            GLib.idle_add(self._finalize_app)
            return True  # block immediate close; _finalize_app destroys
        if self._stopping:
            if self._compress_running:
                # User wants out mid-compress: signal abort, keep the window
                # open until the compress thread finalizes us via idle_add.
                self._compress_abort.set()
                return True
            return False  # mid-stop with no compress running — allow close
        self._request_stop()
        return True  # block close; _finalize_app destroys the window

    def _on_key(self, _ctrl, keyval, _code, _mod) -> bool:
        if keyval == Gdk.KEY_Escape:
            logger.debug("Escape pressed")
            self._request_stop()
            return True
        return False

    # ── Fatal-error rendering ──

    def _show_fatal(self, message: str) -> None:
        """Display a fatal error in the window; Stop becomes Close."""
        logger.error("fatal: %s", message)
        if self._ui:
            self._ui.show_error(message)
            self._ui.mark_stopping()
