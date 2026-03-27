"""Voxize GTK4 application — wires state machine, UI, and providers.

Environment variables:
    VOXIZE_MOCK=1          Use mock transcription (no mic, no API)
    VOXIZE_ERROR=<ms>      Simulate WebSocket error after <ms>
    VOXIZE_STOP=<ms>       Auto-stop recording after <ms>
    VOXIZE_AUTOCLOSE=<s>   Auto-close after READY state (default 30, 0 to disable)
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

from gi.repository import Gdk, GLib, Gtk

from voxize.mock import MockCleanup, MockTranscription
from voxize.state import State, StateMachine
from voxize.ui import OverlayWindow

logger = logging.getLogger(__name__)

_MOCK = bool(os.environ.get("VOXIZE_MOCK"))
_AUTOCLOSE = int(os.environ.get("VOXIZE_AUTOCLOSE", "30"))


class VoxizeApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="dev.voxize.overlay")
        self._machine: StateMachine | None = None
        self._ui: OverlayWindow | None = None

        # Real providers
        self._lock = None
        self._session_dir: str | None = None
        self._api_key: str | None = None
        self._audio = None
        self._transcription = None
        self._cleanup = None

        # Mock transcription (only in mock mode)
        self._mock_transcription: MockTranscription | None = None

        # Auto-close timer
        self._autoclose_source: int | None = None

        # Session-level file log handler
        self._log_handler = None

    def do_activate(self) -> None:
        # Load CSS theme
        css = Gtk.CssProvider()
        css.load_from_string((Path(__file__).parent / "style.css").read_text())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Window
        win = Gtk.ApplicationWindow(application=self)
        win.set_resizable(False)
        win.set_default_size(420, -1)
        win.connect("close-request", self._on_close_request)

        # Escape key
        ctrl = Gtk.EventControllerKey()
        ctrl.connect("key-pressed", self._on_key, win)
        win.add_controller(ctrl)

        # State machine + UI
        self._machine = StateMachine()
        self._machine.on_change(self._on_state_change)
        self._ui = OverlayWindow(win, self._machine)

        win.present()
        self._ui.setup_max_height()

        # Signal handlers — finalize WAV header before exit so the file is
        # a valid WAV (not just raw PCM with a placeholder header).
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal, sig)

        if _MOCK:
            GLib.idle_add(self._initialize_mock)
        else:
            GLib.idle_add(self._initialize)

    # ── Real initialization ──

    def _initialize(self) -> bool:
        """Acquire mic lock, open audio, connect WebSocket."""
        from voxize.audio import AudioCapture
        from voxize.checks import get_api_key
        from voxize.lock import MicLock, MicLockError
        from voxize.storage import create_session_dir
        from voxize.transcribe import RealtimeTranscription

        logger.debug("_initialize: acquiring lock")
        try:
            self._lock = MicLock()
            self._lock.acquire()
        except MicLockError as e:
            self._machine.transition(State.ERROR, error=str(e))
            return False
        except Exception as e:
            self._machine.transition(State.ERROR, error=f"Lock failed: {e}")
            return False
        logger.debug("_initialize: lock acquired")

        try:
            self._session_dir = create_session_dir()
            logger.debug("_initialize: session_dir=%s", self._session_dir)
            self._api_key = get_api_key("openai")
            logger.debug("_initialize: api_key retrieved")
        except Exception as e:
            self._release_lock()
            self._machine.transition(State.ERROR, error=str(e))
            return False

        # Set up session-level file logging
        fh = logging.FileHandler(os.path.join(self._session_dir, "debug.log"))
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d %(threadName)-14s %(name)s %(message)s",
            datefmt="%H:%M:%S",
        ))
        logging.getLogger("voxize").addHandler(fh)
        logging.getLogger("voxize").setLevel(logging.DEBUG)
        self._log_handler = fh
        logger.debug("_initialize: file logging active")

        # Create providers
        logger.debug("_initialize: creating providers")
        self._transcription = RealtimeTranscription(self._api_key, self._session_dir)
        self._audio = AudioCapture(self._session_dir, self._transcription.send_audio)

        # Start WebSocket (background thread) — connects while we record
        logger.debug("_initialize: starting transcription WS")
        self._transcription.start(
            on_delta=self._ui.append_text,
            on_error=self._on_ws_error,
            on_ready=self._ui.on_ws_ready,
            on_speech=self._ui.on_speech,
        )

        # Start audio capture — transition to RECORDING immediately (fast startup).
        # Audio chunks queue in the asyncio queue until WS is connected.
        # If WS fails later, _on_ws_error handles RECORDING state (degraded mode).
        logger.debug("_initialize: starting audio capture")
        try:
            self._audio.start()
        except Exception as e:
            self._transcription.cancel()
            self._transcription = None
            self._release_lock()
            self._machine.transition(State.ERROR, error=f"Microphone: {e}")
            return False

        logger.debug("_initialize: transitioning to RECORDING")
        self._machine.transition(State.RECORDING)
        return False  # one-shot idle

    # ── Mock initialization ──

    def _initialize_mock(self) -> bool:
        """Mock mode: skip mic/lock/WebSocket, use mock transcription."""
        self._machine.transition(State.RECORDING)

        # Schedule simulated error if requested
        error_ms = os.environ.get("VOXIZE_ERROR")
        if error_ms:
            GLib.timeout_add(int(error_ms), self._mock_error)

        # Schedule auto-stop if requested
        stop_ms = os.environ.get("VOXIZE_STOP")
        if stop_ms:
            GLib.timeout_add(int(stop_ms), self._mock_stop)

        return False

    def _mock_error(self) -> bool:
        """Fire a simulated WebSocket error."""
        if self._machine and self._machine.state == State.RECORDING:
            self._on_ws_error("Simulated: WebSocket connection lost")
        return False

    def _mock_stop(self) -> bool:
        """Fire a simulated stop."""
        if self._machine and self._machine.state == State.RECORDING:
            self._machine.transition(State.CLEANING)
        return False

    # ── WebSocket callbacks ──

    def _on_ws_error(self, message: str) -> None:
        """WebSocket error — behaviour depends on current state.

        During RECORDING: non-destructive. Audio capture continues (WAV is
        the safety net). Error banner is shown. Transcript text stays visible
        so the user can recover it. Close button replaces Stop.

        During INITIALIZING: nothing to preserve, transition to ERROR.
        """
        current = self._machine.state if self._machine else None
        logger.debug("_on_ws_error: state=%s msg=%s", current, message)
        if self._machine and self._machine.state == State.RECORDING:
            # Degraded mode — keep audio, show banner
            if self._transcription:
                self._transcription.cancel()
                self._transcription = None
            self._ui.show_error_banner(message)
        elif self._machine and self._machine.state == State.INITIALIZING:
            self._teardown_async()
            self._machine.transition(State.ERROR, error=message)

    # ── State orchestration ──

    def _on_state_change(self, machine: StateMachine, old: State, new: State) -> None:
        logger.debug("_on_state_change: %s -> %s", old.name, new.name)
        # Cancel any pending auto-close when leaving READY
        self._cancel_autoclose()

        if new == State.READY and _AUTOCLOSE > 0:
            self._autoclose_source = GLib.timeout_add_seconds(
                _AUTOCLOSE, self._on_autoclose
            )

        if new == State.RECORDING:
            if _MOCK:
                self._mock_transcription = MockTranscription()
                self._mock_transcription.start(on_delta=self._ui.append_text)

        elif new == State.CLEANING:
            # Defer to next idle so the UI repaints with CLEANING state first
            GLib.idle_add(self._begin_cleanup)

        elif new == State.CANCELLED:
            # Stop mock providers on GTK thread (instant, uses GLib timers)
            if self._mock_transcription:
                self._mock_transcription.cancel()
                self._mock_transcription = None
            if self._cleanup:
                self._cleanup.cancel()
                self._cleanup = None
            # Stop audio synchronously — it's fast (stream.stop + close + WAV
            # finalize) and MUST complete before Python shutdown, otherwise
            # sounddevice's atexit Pa_Terminate() blocks on the still-active
            # PortAudio stream while the daemon thread that was supposed to
            # close it has been killed.
            audio = self._audio
            self._audio = None
            if audio:
                try:
                    audio.stop()
                except Exception:
                    logger.exception("Cancel: failed to stop audio")
            # Defer only the slow parts (transcription cancel with 15s join,
            # lock release) to a background thread.
            self._teardown_async()

        elif new == State.ERROR:
            # Recording teardown may already have happened (e.g., from _on_ws_error)
            self._teardown_async()

    def _begin_cleanup(self) -> bool:
        """Stop recording, save raw transcript, copy to clipboard, run cleanup.

        Mock providers are stopped here (instant, uses GLib timers).
        Real providers are stopped in a background thread to avoid freezing
        the GTK main loop (transcription.stop blocks up to ~5s).
        """
        logger.debug("_begin_cleanup: mock=%s", _MOCK)
        # Stop mock transcription on GTK thread (uses GLib timers)
        mock_transcript = ""
        if self._mock_transcription:
            mock_transcript = self._mock_transcription.stop()
            self._mock_transcription = None

        if _MOCK:
            self._release_lock()
            self._start_cleanup(mock_transcript)
        else:
            # Grab references and null them to prevent races with teardown
            audio = self._audio
            self._audio = None
            transcription = self._transcription
            self._transcription = None
            lock = self._lock
            self._lock = None
            session_dir = self._session_dir

            threading.Thread(
                target=self._stop_providers,
                args=(audio, transcription, lock, session_dir),
                daemon=True,
                name="voxize-stop",
            ).start()

        return False  # one-shot idle

    def _stop_providers(self, audio, transcription, lock, session_dir) -> None:
        """Background thread: stop real providers, save transcript, copy clipboard."""
        from gi.repository import GLib
        from voxize import clipboard

        logger.debug("_stop_providers: stopping audio")
        try:
            if audio:
                audio.stop()
        except Exception:
            logger.exception("Failed to stop audio")
        logger.debug("_stop_providers: audio stopped")

        transcript = ""
        logger.debug("_stop_providers: stopping transcription")
        try:
            if transcription:
                transcript = transcription.stop()
        except Exception:
            logger.exception("Failed to stop transcription")
        logger.debug("_stop_providers: transcription stopped, transcript_len=%d",
                      len(transcript))

        logger.debug("_stop_providers: releasing lock")
        try:
            if lock:
                lock.release()
        except Exception:
            logger.exception("Failed to release lock")

        # Save raw transcript and copy to clipboard (safety net)
        if transcript and session_dir:
            try:
                Path(session_dir, "transcription.txt").write_text(transcript)
            except Exception:
                logger.exception("Failed to save transcription.txt")
        if transcript:
            clipboard.copy(transcript)

        # Post back to GTK thread to start cleanup streaming
        GLib.idle_add(self._start_cleanup, transcript)

    def _start_cleanup(self, transcript: str) -> bool:
        """GTK thread: start cleanup provider (or handle empty transcript)."""
        logger.debug("_start_cleanup: transcript_len=%d empty=%s",
                      len(transcript), not transcript.strip())
        # Guard against stale callback (e.g., user cancelled during drain)
        if not self._machine or self._machine.state != State.CLEANING:
            return False

        # Handle empty transcript
        if not transcript.strip():
            self._ui.clear_text()
            self._ui.append_text("No speech detected.")
            if self._machine:
                self._machine.transition(State.READY)
            return False

        # Show the final transcript and arm the cleanup swap — the user sees
        # their raw text pulsing while cleanup streams in to replace it.
        self._ui.show_transcript_for_cleanup(transcript)

        # Start cleanup
        if _MOCK:
            self._cleanup = MockCleanup()
            self._cleanup.start(
                transcript=transcript,
                on_delta=self._ui.append_text,
                on_complete=self._on_cleanup_done,
            )
        else:
            from voxize.cleanup import Cleanup

            self._cleanup = Cleanup(self._api_key)
            self._cleanup.start(
                transcript=transcript,
                on_delta=self._ui.append_text,
                on_complete=self._on_cleanup_done,
                on_error=self._on_cleanup_error,
            )
        return False  # one-shot idle

    def _teardown_async(self) -> None:
        """Move blocking teardown to a background thread."""
        logger.debug("_teardown_async: audio=%s transcription=%s lock=%s",
                      self._audio is not None, self._transcription is not None,
                      self._lock is not None)
        # Grab references and null them on GTK thread to prevent races
        audio = self._audio
        self._audio = None
        transcription = self._transcription
        self._transcription = None
        lock = self._lock
        self._lock = None

        if not audio and not transcription and not lock:
            logger.debug("_teardown_async: nothing to tear down")
            return  # nothing to tear down

        threading.Thread(
            target=self._teardown_blocking,
            args=(audio, transcription, lock),
            daemon=True,
            name="voxize-teardown",
        ).start()

    @staticmethod
    def _teardown_blocking(audio, transcription, lock) -> None:
        """Background thread: stop providers, release lock."""
        logger.debug("_teardown_blocking: audio=%s transcription=%s lock=%s",
                      audio is not None, transcription is not None, lock is not None)
        try:
            if audio:
                logger.debug("_teardown_blocking: stopping audio")
                audio.stop()
        except Exception:
            logger.exception("Teardown: failed to stop audio")
        try:
            if transcription:
                logger.debug("_teardown_blocking: cancelling transcription")
                transcription.cancel()
        except Exception:
            logger.exception("Teardown: failed to cancel transcription")
        try:
            if lock:
                logger.debug("_teardown_blocking: releasing lock")
                lock.release()
        except Exception:
            logger.exception("Teardown: failed to release lock")
        logger.debug("_teardown_blocking: complete")

    def _release_lock(self) -> None:
        if self._lock:
            self._lock.release()
            self._lock = None

    def _on_cleanup_done(self, cleaned: str) -> None:
        from voxize import clipboard

        self._cleanup = None
        if self._machine and self._machine.state == State.CLEANING:
            # Save cleaned text and copy to clipboard (overwrites raw transcript)
            if cleaned and self._session_dir:
                try:
                    Path(self._session_dir, "cleaned.txt").write_text(cleaned)
                except Exception:
                    logger.exception("Failed to save cleaned.txt")
            if cleaned:
                clipboard.copy(cleaned)
            self._machine.transition(State.READY)

    def _on_cleanup_error(self, message: str) -> None:
        """Cleanup failed — non-fatal. Raw transcript is already in clipboard."""
        if self._machine and self._machine.state == State.CLEANING:
            self._cleanup = None
            self._machine.transition(State.ERROR, error=message)

    # ── Window events ──

    def _on_key(self, _ctrl, keyval, _code, _mod, win) -> bool:
        if keyval != Gdk.KEY_Escape:
            return False
        s = self._machine.state if self._machine else None
        if s in (State.RECORDING, State.CLEANING):
            self._machine.transition(State.CANCELLED)
        elif s == State.INITIALIZING:
            self._teardown_async()
            win.close()
        elif s in (State.READY, State.ERROR, None):
            win.close()
        return True

    def _on_autoclose(self) -> bool:
        """Auto-close the window after the READY timeout."""
        logger.debug("_on_autoclose: firing")
        self._autoclose_source = None
        if self._machine and self._machine.state == State.READY:
            win = self.get_active_window()
            if win:
                win.close()
        return GLib.SOURCE_REMOVE

    def _cancel_autoclose(self) -> None:
        if self._autoclose_source is not None:
            GLib.source_remove(self._autoclose_source)
            self._autoclose_source = None

    def _on_signal(self, sig: int) -> bool:
        """Handle SIGTERM/SIGINT — finalize WAV, release lock, quit."""
        logger.debug("_on_signal: sig=%s", sig)
        logger.info("Signal %s received, shutting down", sig)
        if self._audio:
            try:
                self._audio.finalize_wav()
            except Exception:
                logger.exception("Signal handler: failed to finalize WAV")
        if self._lock:
            try:
                self._lock.release()
                self._lock = None
            except Exception:
                pass
        self.quit()
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, _win) -> bool:
        logger.debug("_on_close_request: entry")
        self._cancel_autoclose()
        self._teardown_async()
        if self._mock_transcription:
            self._mock_transcription.cancel()
        if self._cleanup:
            self._cleanup.cancel()
        if self._ui:
            self._ui.destroy()
        # Prune old sessions (best-effort, at termination time)
        try:
            from voxize.storage import prune_sessions

            prune_sessions()
        except Exception:
            pass
        # Remove session file log handler
        if self._log_handler:
            logging.getLogger("voxize").removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None
        # Redirect stdio to /dev/null so any parent process (e.g., GNOME Shell
        # extension) sees EOF immediately, while daemon threads can still write
        # to stderr without EBADF.
        try:
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                try:
                    os.dup2(devnull, fd)
                except OSError:
                    pass
            os.close(devnull)
        except OSError:
            pass
        # Explicitly quit the application so the main loop exits even if
        # GLib sources (signal handlers, stale idle callbacks) are pending.
        self.quit()
        return False  # allow close
