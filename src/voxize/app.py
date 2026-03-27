"""Voxize GTK4 application — wires state machine, UI, and providers.

Mock mode (env vars, see __main__.py):
    VOXIZE_MOCK=1          Use mock transcription (no mic, no API)
    VOXIZE_ERROR=<ms>      Simulate WebSocket error after <ms>
    VOXIZE_STOP=<ms>       Auto-stop recording after <ms>
"""

from __future__ import annotations

import os
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GLib, Gtk

from voxize.mock import MockCleanup, MockTranscription
from voxize.state import State, StateMachine
from voxize.ui import OverlayWindow

_MOCK = bool(os.environ.get("VOXIZE_MOCK"))


class VoxizeApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="dev.voxize.overlay")
        self._machine: StateMachine | None = None
        self._ui: OverlayWindow | None = None

        # Real providers (Phase 2) — None in mock mode
        self._lock = None
        self._session_dir: str | None = None
        self._audio = None
        self._transcription = None

        # Mock transcription (only in mock mode)
        self._mock_transcription: MockTranscription | None = None

        # Mock cleanup (until Phase 3)
        self._cleanup: MockCleanup | None = None

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
            api_key = get_api_key("openai")
        except Exception as e:
            self._release_lock()
            self._machine.transition(State.ERROR, error=str(e))
            return False

        # Create providers
        self._transcription = RealtimeTranscription(api_key)
        self._audio = AudioCapture(self._session_dir, self._transcription.send_audio)

        # Start WebSocket (background thread) — on_ready transitions to RECORDING
        self._transcription.start(
            on_delta=self._ui.append_text,
            on_ready=self._on_ws_ready,
            on_error=self._on_ws_error,
        )

        # Start audio capture immediately — chunks queue until WS is connected
        try:
            self._audio.start()
        except Exception as e:
            self._transcription.cancel()
            self._transcription = None
            self._release_lock()
            self._machine.transition(State.ERROR, error=f"Microphone: {e}")
            return False

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

    def _on_ws_ready(self) -> None:
        """WebSocket connected and session configured — begin recording."""
        if self._machine and self._machine.state == State.INITIALIZING:
            self._machine.transition(State.RECORDING)

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
            self._teardown_recording()
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
            self._teardown_recording()
            if self._mock_transcription:
                self._mock_transcription.cancel()
                self._mock_transcription = None
            if self._cleanup:
                self._cleanup.cancel()
                self._cleanup = None

        elif new == State.ERROR:
            # Recording teardown may already have happened (e.g., from _on_ws_error)
            self._teardown_recording()

    def _begin_cleanup(self) -> bool:
        """Stop recording and start mock cleanup (real cleanup is Phase 3)."""
        # Stop real providers
        if self._audio:
            self._audio.stop()
            self._audio = None

        transcript = ""
        if self._transcription:
            transcript = self._transcription.stop()
            self._transcription = None

        # Stop mock transcription
        if self._mock_transcription:
            transcript = self._mock_transcription.stop()
            self._mock_transcription = None

        self._release_lock()

        # Start mock cleanup (Phase 3 replaces this with Anthropic SDK)
        self._cleanup = MockCleanup()
        self._cleanup.start(
            transcript=transcript,
            on_delta=self._ui.append_text,
            on_complete=self._on_cleanup_done,
        )
        return False  # one-shot idle

    def _teardown_recording(self) -> None:
        """Stop audio + transcription if running, release lock."""
        if self._audio:
            self._audio.stop()
            self._audio = None
        if self._transcription:
            self._transcription.cancel()
            self._transcription = None
        self._release_lock()

    def _release_lock(self) -> None:
        if self._lock:
            self._lock.release()
            self._lock = None

    def _on_cleanup_done(self, cleaned: str) -> None:
        if self._machine and self._machine.state == State.CLEANING:
            self._machine.transition(State.READY)

    # ── Window events ──

    def _on_key(self, _ctrl, keyval, _code, _mod, win) -> bool:
        if keyval != Gdk.KEY_Escape:
            return False
        s = self._machine.state if self._machine else None
        if s in (State.RECORDING, State.CLEANING):
            self._machine.transition(State.CANCELLED)
        elif s == State.INITIALIZING:
            self._teardown_recording()
            win.close()
        elif s in (State.READY, State.ERROR, None):
            win.close()
        return True

    def _on_close_request(self, _win) -> bool:
        self._teardown_recording()
        if self._mock_transcription:
            self._mock_transcription.cancel()
        if self._cleanup:
            self._cleanup.cancel()
        if self._ui:
            self._ui.destroy()
        return False  # allow close
