"""Voxize GTK4 application — wires state machine, UI, and providers."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk

from voxize.mock import MockCleanup, MockTranscription
from voxize.state import State, StateMachine
from voxize.ui import OverlayWindow


class VoxizeApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="dev.voxize.overlay")
        self._machine: StateMachine | None = None
        self._ui: OverlayWindow | None = None
        self._transcription: MockTranscription | None = None
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

        # Start recording immediately
        self._machine.transition(State.RECORDING)

    # ── State orchestration ──

    def _on_state_change(self, machine: StateMachine, old: State, new: State) -> None:
        if new == State.RECORDING:
            self._transcription = MockTranscription()
            self._transcription.start(on_delta=self._ui.append_text)

        elif new == State.CLEANING:
            transcript = self._transcription.stop() if self._transcription else ""
            self._transcription = None
            self._cleanup = MockCleanup()
            self._cleanup.start(
                transcript=transcript,
                on_delta=self._ui.append_text,
                on_complete=self._on_cleanup_done,
            )

        elif new == State.CANCELLED:
            if self._transcription:
                self._transcription.cancel()
                self._transcription = None
            if self._cleanup:
                self._cleanup.cancel()
                self._cleanup = None

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
        elif s in (State.READY, State.ERROR, None):
            win.close()
        return True

    def _on_close_request(self, _win) -> bool:
        if self._transcription:
            self._transcription.cancel()
        if self._cleanup:
            self._cleanup.cancel()
        if self._ui:
            self._ui.destroy()
        return False  # allow close
