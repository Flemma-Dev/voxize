"""Voxize overlay window — header bar, text area, state-driven updates."""

from __future__ import annotations

import cairo
import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

# gi.require_version() above is executable code; all subsequent imports trigger E402.
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from voxize.state import State, StateMachine  # noqa: E402


def _draw_fade(
    area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int, _data=None
) -> None:
    """Draw a gradient from window background (top) to transparent (bottom)."""
    r, g, b, a = 30 / 255, 30 / 255, 30 / 255, 0.85
    try:
        # PyGObject returns (found, Gdk.RGBA) — not an output parameter
        found, color = area.get_style_context().lookup_color("vox_bg")
        if found:
            r, g, b, a = color.red, color.green, color.blue, color.alpha
    except Exception:
        pass
    pat = cairo.LinearGradient(0, 0, 0, h)
    pat.add_color_stop_rgba(0, r, g, b, a)
    pat.add_color_stop_rgba(1, r, g, b, 0.0)
    cr.set_source(pat)
    cr.rectangle(0, 0, w, h)
    cr.fill()


class OverlayWindow:
    """Builds and manages the Voxize overlay UI widgets."""

    def __init__(self, window: Gtk.ApplicationWindow, machine: StateMachine) -> None:
        self._window = window
        self._machine = machine
        self._timer_seconds = 0
        self._timer_source: int | None = None
        self._pulse_source: int | None = None
        self._pulse_dim = False
        self._scroll_pending = False
        self._text_pulse_source: int | None = None
        self._text_pulse_dim = False
        self._awaiting_cleanup = False
        self._destroyed = False
        self._ws_ready = False
        self._speech_active = False
        self._had_first_text = False
        self._build()
        machine.on_change(self._on_state_change)

    # ── Construction ──

    def _build(self) -> None:
        # HeaderBar as titlebar — gives us drag-to-move for free
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(False)
        header.set_title_widget(Gtk.Box())  # suppress default title

        # Status area (start)
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self._dot = Gtk.Label(label="\u25cf")  # ●
        self._dot.add_css_class("status-dot")
        status.append(self._dot)

        self._status_label = Gtk.Label(label="Initializing")
        self._status_label.add_css_class("status-label")
        status.append(self._status_label)

        self._timer_label = Gtk.Label()
        self._timer_label.add_css_class("timer-label")
        self._timer_label.set_margin_start(8)
        self._timer_label.set_visible(False)
        status.append(self._timer_label)

        header.pack_start(status)

        # Action buttons (end) — pack_end adds right-to-left
        self._action_btn = Gtk.Button(label="Stop")
        self._action_btn.connect("clicked", self._on_action)
        self._action_btn.set_visible(False)
        header.pack_end(self._action_btn)

        self._cancel_btn = Gtk.Button(label="Cancel")
        self._cancel_btn.add_css_class("destructive-action")
        self._cancel_btn.connect("clicked", self._on_cancel)
        self._cancel_btn.set_visible(False)
        header.pack_end(self._cancel_btn)

        self._window.set_titlebar(header)

        # Dim text area when window loses focus
        self._window.connect("notify::is-active", self._on_active_changed)

        # Content area
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._window.set_child(main)

        # Spinner — shown while waiting for first text in a new phase
        self._spinner = Gtk.Spinner()
        self._spinner.set_halign(Gtk.Align.CENTER)
        self._spinner.set_margin_top(12)
        self._spinner.set_margin_bottom(12)
        self._spinner.set_visible(True)
        self._spinner.set_spinning(True)
        main.append(self._spinner)

        # Text area with fade overlay
        overlay = Gtk.Overlay()

        self._text_view = Gtk.TextView()
        self._text_view.set_editable(False)
        self._text_view.set_monospace(True)
        self._text_view.set_cursor_visible(False)
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._text_view.add_css_class("text-area")
        self._text_view.set_top_margin(10)
        self._text_view.set_bottom_margin(10)
        self._text_view.set_left_margin(12)
        self._text_view.set_right_margin(12)

        buf = self._text_view.get_buffer()
        self._end_mark = buf.create_mark("end", buf.get_end_iter(), False)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_child(self._text_view)
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        self._scroll.set_propagate_natural_height(True)
        self._scroll.set_max_content_height(250)  # overridden in setup_max_height

        overlay.set_child(self._scroll)

        self._fade = Gtk.DrawingArea()
        self._fade.set_content_height(32)
        self._fade.set_valign(Gtk.Align.START)
        self._fade.set_halign(Gtk.Align.FILL)
        self._fade.set_hexpand(True)
        self._fade.set_can_target(False)  # click-through
        self._fade.set_draw_func(_draw_fade)
        self._fade.set_visible(False)
        overlay.add_overlay(self._fade)

        # Show/hide fade reactively when scroll geometry changes
        self._scroll.get_vadjustment().connect("changed", self._on_vadj_changed)

        self._overlay = overlay
        self._overlay.set_visible(False)  # hidden until text arrives
        main.append(overlay)

        # Error bar — shown at bottom when a non-fatal error occurs
        error_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        error_box.add_css_class("error-bar")

        error_icon = Gtk.Label(label="\u26a0")  # ⚠
        error_icon.add_css_class("error-icon")
        error_box.append(error_icon)

        self._error_label = Gtk.Label()
        self._error_label.add_css_class("error-message")
        self._error_label.set_wrap(True)
        self._error_label.set_hexpand(True)
        self._error_label.set_xalign(0)
        error_box.append(self._error_label)

        self._error_bar = error_box
        self._error_bar.set_visible(False)
        main.append(error_box)

        self._degraded = False

        # Context bar — persistent prompt indicator at the bottom
        from gi.repository import Pango

        self._context_label = Gtk.Label()
        self._context_label.add_css_class("context-bar")
        self._context_label.set_wrap(True)
        self._context_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._context_label.set_lines(3)
        self._context_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._context_label.set_xalign(0)
        self._context_label.set_visible(False)
        main.append(self._context_label)

    def setup_max_height(self) -> None:
        """Set max text area height based on screen geometry (1/4 screen)."""
        display = Gdk.Display.get_default()
        if not display:
            return
        monitors = display.get_monitors()
        if monitors.get_n_items() == 0:
            return
        geom = monitors.get_item(0).get_geometry()
        max_h = geom.height // 4 - 50  # reserve ~50px for header
        self._scroll.set_max_content_height(max(100, max_h))

    # ── Context statusbar ──

    def show_prompt_context(self, prompt: str, cwd: str) -> None:
        """Show a persistent context bar with the active WHISPER.txt prompt."""
        if self._destroyed:
            return
        import os

        file_uri = GLib.filename_to_uri(os.path.join(cwd, "WHISPER.txt"), None)
        # Link color must be set in Pango markup — GtkLabel ignores CSS for
        # link colors (it uses the accent color internally).
        # NOTE: this matches --vox-fg-dim in style.css; update both if the palette changes.
        link_color = "#ffffff80"
        self._context_label.set_markup(
            f'<a href="{GLib.markup_escape_text(file_uri)}">'
            f'<span foreground="{link_color}"><b>WHISPER.txt</b></span></a>'
            f"  {GLib.markup_escape_text(prompt)}"
        )
        self._context_label.set_visible(True)

    def show_session_costs(
        self, transcription_cost: float | None, cleanup_cost: float | None
    ) -> None:
        """Show session costs in the context bar (plain text)."""
        if self._destroyed:
            return
        t_str = (
            f"${transcription_cost:.4f}" if transcription_cost is not None else "\u2014"
        )
        c_str = f"${cleanup_cost:.4f}" if cleanup_cost is not None else "\u2014"
        if transcription_cost is not None or cleanup_cost is not None:
            total = (transcription_cost or 0) + (cleanup_cost or 0)
            total_str = f"${total:.4f}"
        else:
            total_str = "\u2014"
        self._context_label.set_text(
            f"Total {total_str} \u2022 Transcription {t_str} \u00b7 Cleanup {c_str}"
        )
        self._context_label.set_visible(True)

    # ── Text operations ──

    def on_ws_ready(self) -> None:
        """WebSocket session configured — show 'Listening...' status."""
        if self._destroyed or self._machine.state != State.RECORDING:
            return
        self._ws_ready = True
        if not self._had_first_text:
            self._status_label.set_text("Listening\u2026")

    def on_speech(self, active: bool) -> None:
        """VAD speech_started/speech_stopped — tie dot pulse to speech."""
        if self._destroyed or self._machine.state != State.RECORDING:
            return
        self._speech_active = active
        if active:
            # Bright dot during speech (remove dim, restart pulse)
            self._dot.remove_css_class("dim")
        else:
            # Dim dot when speech stops — pulse resumes from dim
            self._dot.add_css_class("dim")

    def append_text(self, text: str) -> None:
        if self._destroyed:
            return
        if not self._had_first_text:
            self._had_first_text = True
            if self._machine.state == State.RECORDING:
                self._status_label.set_text("Recording")
        if self._spinner.get_spinning():
            self._spinner.set_spinning(False)
            self._spinner.set_visible(False)
        if self._awaiting_cleanup:
            # First cleanup token — swap out the pulsing transcript
            self._awaiting_cleanup = False
            self._stop_text_pulse()
            self.clear_text()
        self._overlay.set_visible(True)
        buf = self._text_view.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        # Defer scroll to after the layout pass so the ScrolledWindow has
        # finished reallocating height for any new line wraps.
        if not self._scroll_pending:
            self._scroll_pending = True
            GLib.idle_add(self._scroll_to_end)

    def clear_text(self) -> None:
        self._text_view.get_buffer().set_text("")

    def show_transcript_for_cleanup(self, transcript: str) -> None:
        """Show the final transcript and arm the cleanup swap.

        Called by app._start_cleanup after the transcription drain completes.
        Updates the status label, sets the text buffer to the full transcript,
        starts pulsing, and arms _awaiting_cleanup so the first cleanup token
        clears and replaces it.
        """
        if self._destroyed:
            return
        self._status_label.set_text("Cleaning up\u2026")
        if self._spinner.get_spinning():
            self._spinner.set_spinning(False)
            self._spinner.set_visible(False)
        self._overlay.set_visible(True)
        self._text_view.get_buffer().set_text(transcript)
        if not self._scroll_pending:
            self._scroll_pending = True
            GLib.idle_add(self._scroll_to_end)
        self._awaiting_cleanup = True
        self._start_text_pulse()

    def show_error_banner(self, message: str) -> None:
        """Show error banner at the bottom without clearing transcript.

        Stops the recording timer and pulse. Changes the action button to
        Close so the user can end the session. Audio capture continues in
        the background — the WAV file is the safety net.
        """
        if self._destroyed:
            return
        self._error_label.set_text(message)
        self._error_bar.set_visible(True)
        self._stop_pulse()
        self._stop_timer()
        self._degraded = True
        self._cancel_btn.set_visible(False)
        self._action_btn.set_label("Close")
        self._action_btn.add_css_class("flat")
        self._action_btn.set_visible(True)
        self._window.set_default_widget(self._action_btn)

    def _scroll_to_end(self) -> bool:
        self._scroll_pending = False
        vadj = self._scroll.get_vadjustment()
        vadj.set_value(vadj.get_upper() - vadj.get_page_size())
        return False  # one-shot

    def _on_vadj_changed(self, vadj) -> None:
        """Show fade gradient only when content overflows the viewport."""
        should_show = vadj.get_upper() > vadj.get_page_size() + 1
        if self._fade.get_visible() != should_show:
            self._fade.set_visible(should_show)

    # ── State change handler ──

    def _on_state_change(self, machine: StateMachine, old: State, new: State) -> None:
        # Update dot color class
        for cls in ("recording", "cleaning", "ready", "error", "cancelled"):
            self._dot.remove_css_class(cls)
        self._dot.add_css_class(new.name.lower())
        self._stop_pulse()
        self._stop_text_pulse()

        if new == State.RECORDING:
            self._status_label.set_text("Listening\u2026")
            self._ws_ready = False
            self._speech_active = False
            self._had_first_text = False
            self._timer_seconds = 0
            self._timer_label.set_text("00:00")
            self._timer_label.set_visible(True)
            self._cancel_btn.set_visible(True)
            self._action_btn.set_label("Stop")
            self._action_btn.remove_css_class("flat")
            self._action_btn.set_visible(True)
            self._window.set_default_widget(self._action_btn)
            self._overlay.set_visible(False)
            self._spinner.set_visible(True)
            self._spinner.set_spinning(True)
            self._start_timer()
            self._start_pulse()
            self.clear_text()

        elif new == State.CLEANING:
            self._context_label.set_visible(False)
            self._status_label.set_text("Finishing\u2026")
            self._timer_label.set_visible(False)
            self._stop_timer()
            self._cancel_btn.set_visible(True)
            self._action_btn.set_visible(False)
            self._window.set_default_widget(None)
            self._start_text_pulse()
            # Keep spinner visible — deltas continue streaming during drain
            # and append_text will dismiss the spinner on the first arrival.
            # If no deltas arrive, show_transcript_for_cleanup handles it.
            #
            # Don't set _awaiting_cleanup here — _start_cleanup will show the
            # final transcript and arm the swap after the drain completes.

        elif new == State.READY:
            self._status_label.set_text("Ready")
            self._timer_label.set_visible(False)
            self._cancel_btn.set_visible(False)
            self._action_btn.set_label("Close")
            self._action_btn.add_css_class("flat")
            self._action_btn.set_visible(True)
            self._window.set_default_widget(self._action_btn)

        elif new == State.CANCELLED:
            self._stop_timer()
            # Defer window.close() — calling it synchronously from within
            # machine.transition() nests window destruction inside the
            # callback chain, which can prevent self.quit() from taking
            # effect (the main loop never sees the quit flag).
            GLib.idle_add(self._window.close)

        elif new == State.ERROR:
            self._status_label.set_text("Error")
            self._timer_label.set_visible(False)
            self._stop_timer()
            self._cancel_btn.set_visible(False)
            self._action_btn.set_label("Close")
            self._action_btn.add_css_class("flat")
            self._action_btn.set_visible(True)
            self._window.set_default_widget(self._action_btn)
            self.show_error_banner(machine.error_message)

    def _on_active_changed(self, window, _pspec) -> None:
        if window.is_active():
            self._text_view.remove_css_class("backdrop")
        else:
            self._text_view.add_css_class("backdrop")

    # ── Button handlers ──

    def _on_cancel(self, _btn: Gtk.Button) -> None:
        if self._machine.state in (State.RECORDING, State.CLEANING):
            self._machine.transition(State.CANCELLED)

    def _on_action(self, _btn: Gtk.Button) -> None:
        s = self._machine.state
        if s == State.RECORDING:
            if self._degraded:
                self._machine.transition(State.CANCELLED)
            else:
                self._machine.transition(State.CLEANING)
        elif s in (State.READY, State.ERROR):
            self._window.close()

    # ── Timer ──

    def _start_timer(self) -> None:
        self._stop_timer()
        self._timer_source = GLib.timeout_add(1000, self._tick_timer)

    def _stop_timer(self) -> None:
        if self._timer_source is not None:
            GLib.source_remove(self._timer_source)
            self._timer_source = None

    def _tick_timer(self) -> bool:
        self._timer_seconds += 1
        m, s = divmod(self._timer_seconds, 60)
        self._timer_label.set_text(f"{m:02d}:{s:02d}")
        return True

    # ── Pulse animation ──

    def _start_pulse(self) -> None:
        self._stop_pulse()  # idempotent — safe to call when no pulse is running
        self._pulse_dim = False
        self._pulse_source = GLib.timeout_add(600, self._tick_pulse)

    def _stop_pulse(self) -> None:
        if self._pulse_source is not None:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = None
        self._dot.remove_css_class("dim")

    def _tick_pulse(self) -> bool:
        if self._speech_active:
            # During active speech, keep dot bright
            self._dot.remove_css_class("dim")
            self._pulse_dim = False
        else:
            # Between speech / waiting — gentle pulse
            self._pulse_dim = not self._pulse_dim
            if self._pulse_dim:
                self._dot.add_css_class("dim")
            else:
                self._dot.remove_css_class("dim")
        return True

    # ── Text pulse (processing indicator) ──

    def _start_text_pulse(self) -> None:
        self._stop_text_pulse()  # idempotent — safe to call when no pulse is running
        self._text_pulse_dim = True
        self._text_view.add_css_class("processing")  # start dim immediately
        self._text_pulse_source = GLib.timeout_add(1200, self._tick_text_pulse)

    def _stop_text_pulse(self) -> None:
        if self._text_pulse_source is not None:
            GLib.source_remove(self._text_pulse_source)
            self._text_pulse_source = None
        self._text_view.remove_css_class("processing")

    def _tick_text_pulse(self) -> bool:
        self._text_pulse_dim = not self._text_pulse_dim
        if self._text_pulse_dim:
            self._text_view.add_css_class("processing")
        else:
            self._text_view.remove_css_class("processing")
        return True

    # ── Cleanup ──

    def destroy(self) -> None:
        self._destroyed = True
        self._stop_timer()
        self._stop_pulse()
        self._stop_text_pulse()
