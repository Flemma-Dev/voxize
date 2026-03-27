"""Voxize GTK4 application — wires state machine, UI, and providers.

Mock mode (env vars, see __main__.py):
    VOXIZE_MOCK=1          Use mock transcription (no mic, no API)
    VOXIZE_ERROR=<ms>      Simulate WebSocket error after <ms>
    VOXIZE_STOP=<ms>       Auto-stop recording after <ms>
"""

from __future__ import annotations

import logging
import os
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

        try:
            self._lock = MicLock()
            self._lock.acquire()
        except MicLockError as e:
            self._machine.transition(State.ERROR, error=str(e))
            return False
        except Exception as e:
            self._machine.transition(State.ERROR, error=f"Lock failed: {e}")
            return False

        try:
            self._session_dir = create_session_dir()
            self._api_key = get_api_key("openai")
        except Exception as e:
            self._release_lock()
            self._machine.transition(State.ERROR, error=str(e))
            return False

        # Create providers
        self._transcription = RealtimeTranscription(self._api_key, self._session_dir)
        self._audio = AudioCapture(self._session_dir, self._transcription.send_audio)

        # Start WebSocket (background thread) — connects while we record
        self._transcription.start(
            on_delta=self._ui.append_text,
            on_error=self._on_ws_error,
        )

        # Start audio capture — transition to RECORDING immediately (fast startup).
        # Audio chunks queue in the asyncio queue until WS is connected.
        # If WS fails later, _on_ws_error handles RECORDING state (degraded mode).
        try:
            self._audio.start()
        except Exception as e:
            self._transcription.cancel()
            self._transcription = None
            self._release_lock()
            self._machine.transition(State.ERROR, error=f"Microphone: {e}")
            return False

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
            # Move blocking teardown to background thread, close window when done
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

        try:
            if audio:
                audio.stop()
        except Exception:
            logger.exception("Failed to stop audio")

        transcript = ""
        try:
            if transcription:
                transcript = transcription.stop()
        except Exception:
            logger.exception("Failed to stop transcription")

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
        # Grab references and null them on GTK thread to prevent races
        audio = self._audio
        self._audio = None
        transcription = self._transcription
        self._transcription = None
        lock = self._lock
        self._lock = None

        if not audio and not transcription and not lock:
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
        try:
            if audio:
                audio.stop()
        except Exception:
            logger.exception("Teardown: failed to stop audio")
        try:
            if transcription:
                transcription.cancel()
        except Exception:
            logger.exception("Teardown: failed to cancel transcription")
        try:
            if lock:
                lock.release()
        except Exception:
            logger.exception("Teardown: failed to release lock")

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

    def _on_close_request(self, _win) -> bool:
        self._teardown_async()
        if self._mock_transcription:
            self._mock_transcription.cancel()
        if self._cleanup:
            self._cleanup.cancel()
        if self._ui:
            self._ui.destroy()
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
        return False  # allow close
