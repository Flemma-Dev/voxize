"""Voxize overlay window — header bar, text area, state-driven updates."""

from __future__ import annotations

import logging

import gi

logger = logging.getLogger(__name__)

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

# gi.require_version() above is executable code; all subsequent imports trigger E402.
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from voxize.prompt import PromptSource  # noqa: E402
from voxize.state import State, StateMachine  # noqa: E402

_VU_ZONE_CLASSES = ("vu-low", "vu-good", "vu-hot", "vu-clip")


def _dbfs_to_fraction(dbfs: float) -> float:
    """Map dBFS [-60, 0] to fraction [0, 1] for the level bar."""
    return max(0.0, min(1.0, (dbfs + 60.0) / 60.0))


class OverlayWindow:
    """Builds and manages the Voxize overlay UI widgets."""

    def __init__(
        self,
        window: Gtk.ApplicationWindow,
        machine: StateMachine,
        autoclose_seconds: int = 0,
    ) -> None:
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
        self._awaiting_batch = False
        self._destroyed = False
        self._session_dir: str | None = None
        self._speech_active = False
        self._had_first_text = False
        self._meter_source: int | None = None
        self._level_meter = None  # set via set_level_meter()
        self._autoclose_total = autoclose_seconds
        self._autoclose_remaining = 0
        self._autoclose_source: int | None = None
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
        self._action_btn = Gtk.Button()
        self._action_btn_label = Gtk.Label(label="Stop", use_markup=True)
        self._action_btn.set_child(self._action_btn_label)
        self._action_btn.connect("clicked", self._on_action)
        self._action_btn.set_visible(False)
        header.pack_end(self._action_btn)

        self._copy_btn = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
        self._copy_btn.set_tooltip_text("Copy to clipboard")
        self._copy_btn.add_css_class("flat")
        self._copy_btn.set_focusable(False)
        self._copy_btn.connect("clicked", self._on_copy)
        self._copy_btn.set_visible(False)
        header.pack_end(self._copy_btn)

        self._cancel_btn = Gtk.Button(label="Cancel")
        self._cancel_btn.add_css_class("destructive-action")
        self._cancel_btn.connect("clicked", self._on_cancel)
        self._cancel_btn.set_visible(False)
        header.pack_end(self._cancel_btn)

        self._folder_btn = Gtk.Button.new_from_icon_name("folder-symbolic")
        self._folder_btn.set_tooltip_text("Open session folder")
        self._folder_btn.add_css_class("flat")
        self._folder_btn.add_css_class("dim-label")
        self._folder_btn.set_focusable(False)
        self._folder_btn.connect("clicked", self._on_open_folder)
        header.pack_end(self._folder_btn)

        self._window.set_titlebar(header)

        # Dim text area when window loses focus
        self._window.connect("notify::is-active", self._on_active_changed)

        # Content area
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._window.set_child(main)

        # Level meter — thin OSD progress bar, visible only during RECORDING
        self._meter_bar = Gtk.ProgressBar()
        self._meter_bar.add_css_class("osd")
        self._meter_bar.add_css_class("vu-meter")
        self._meter_bar.set_visible(False)
        main.append(self._meter_bar)

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

        self._fade = Gtk.Box()
        self._fade.add_css_class("fade-overlay")
        self._fade.set_size_request(-1, 32)
        self._fade.set_valign(Gtk.Align.START)
        self._fade.set_halign(Gtk.Align.FILL)
        self._fade.set_hexpand(True)
        self._fade.set_can_target(False)  # click-through
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
            logger.debug("setup_max_height: no display available")
            return
        monitors = display.get_monitors()
        if monitors.get_n_items() == 0:
            logger.debug("setup_max_height: no monitors found")
            return
        geom = monitors.get_item(0).get_geometry()
        max_h = geom.height // 4 - 50  # reserve ~50px for header
        logger.debug(
            "setup_max_height: screen=%dx%d max_content_height=%d",
            geom.width,
            geom.height,
            max(100, max_h),
        )
        self._scroll.set_max_content_height(max(100, max_h))

    # ── Context statusbar ──

    def show_prompt_context(self, prompts: list[PromptSource]) -> None:
        """Show a persistent context bar with active vocabulary guidance sources."""
        if self._destroyed:
            return
        logger.debug("show_prompt_context: sources=%d", len(prompts))
        import os

        # Link color must be set in Pango markup — GtkLabel ignores CSS for
        # link colors (it uses the accent color internally).
        # NOTE: this matches --vox-fg-dim in style.css; update both if the palette changes.
        link_color = "#ffffff80"
        parts = []
        for source in prompts:
            file_uri = GLib.filename_to_uri(source.path, None)
            name = os.path.basename(source.path)
            parts.append(
                f'<a href="{GLib.markup_escape_text(file_uri)}">'
                f'<span foreground="{link_color}"><b>'
                f"{GLib.markup_escape_text(name)}</b></span></a>"
                f"  {GLib.markup_escape_text(source.content)}"
            )
        self._context_label.set_markup(" | ".join(parts))
        self._context_label.set_visible(True)

    def show_session_costs(
        self,
        live_cost: float | None,
        batch_cost: float | None,
        cleanup_cost: float | None,
    ) -> None:
        """Show session costs in the context bar (plain text)."""
        if self._destroyed:
            return
        logger.debug(
            "show_session_costs: live=$%s batch=$%s cleanup=$%s",
            f"{live_cost:.4f}" if live_cost is not None else "n/a",
            f"{batch_cost:.4f}" if batch_cost is not None else "n/a",
            f"{cleanup_cost:.4f}" if cleanup_cost is not None else "n/a",
        )
        parts = []
        total = 0.0
        for label, cost in [
            ("Live", live_cost),
            ("Batch", batch_cost),
            ("Cleanup", cleanup_cost),
        ]:
            if cost is not None:
                parts.append(f"{label} ${cost:.4f}")
                total += cost
        if parts:
            sep = " \u00b7 "
            self._context_label.set_text(f"Total ${total:.4f} \u2022 {sep.join(parts)}")
        else:
            self._context_label.set_text("Total \u2014")
        self._context_label.set_visible(True)

    # ── Text operations ──

    def on_speech(self, active: bool) -> None:
        """VAD speech_started/speech_stopped — tie dot pulse to speech."""
        if self._destroyed or self._machine.state != State.RECORDING:
            return
        logger.debug("on_speech: active=%s", active)
        self._speech_active = active
        if active:
            self._dot.remove_css_class("dim")
        else:
            self._dot.add_css_class("dim")

    def append_text(self, text: str) -> None:
        if self._destroyed:
            return
        if not self._had_first_text:
            logger.debug("append_text: first text arrived, len=%d", len(text))
            self._had_first_text = True
            if self._machine.state == State.RECORDING:
                self._status_label.set_text("Listening\u2026")
        if self._spinner.get_spinning():
            self._spinner.set_spinning(False)
            self._spinner.set_visible(False)
        if self._awaiting_batch and self._machine.state == State.TRANSCRIBING:
            # First batch delta — swap out the pulsing live preview
            logger.debug("append_text: first batch delta, swapping live preview")
            self._awaiting_batch = False
            self._stop_text_pulse()
            self.clear_text()
        if self._awaiting_cleanup and self._machine.state == State.CLEANING:
            # First cleanup token — swap out the pulsing transcript
            logger.debug("append_text: first cleanup token, swapping transcript")
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
        """Show the batch transcript and arm the cleanup swap.

        Called by app._begin_cleanup before cleanup streaming starts.
        Sets the text buffer to the batch transcript, starts pulsing,
        and arms _awaiting_cleanup so the first cleanup token clears
        and replaces it.
        """
        if self._destroyed:
            return
        logger.debug("show_transcript_for_cleanup: transcript_len=%d", len(transcript))
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
        logger.debug("show_error_banner: message=%s", message)
        self._error_label.set_text(message)
        self._error_bar.set_visible(True)
        self._stop_pulse()
        self._stop_timer()
        self._degraded = True
        self._cancel_btn.set_visible(False)
        self._action_btn_label.set_label("Close")
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
            logger.debug("_on_vadj_changed: fade_visible=%s", should_show)
            self._fade.set_visible(should_show)

    # ── State change handler ──

    def _on_state_change(self, machine: StateMachine, old: State, new: State) -> None:
        logger.debug("_on_state_change: %s -> %s", old.name, new.name)
        # Update dot color class
        for cls in (
            "initializing",
            "recording",
            "transcribing",
            "cleaning",
            "ready",
            "error",
            "cancelled",
        ):
            self._dot.remove_css_class(cls)
        self._dot.add_css_class(new.name.lower())
        self._stop_pulse()
        self._stop_text_pulse()
        self._stop_meter()
        self._stop_autoclose_countdown()
        self._awaiting_batch = False
        self._awaiting_cleanup = False

        if new == State.RECORDING:
            self._status_label.set_text("Recording")
            self._speech_active = False
            self._had_first_text = False
            self._timer_seconds = 0
            self._timer_label.set_text("00:00")
            self._timer_label.set_visible(True)
            self._copy_btn.set_visible(False)
            self._cancel_btn.set_visible(True)
            self._action_btn_label.set_label("Stop")
            self._action_btn.remove_css_class("flat")
            self._action_btn.set_visible(True)
            self._window.set_default_widget(self._action_btn)
            self._overlay.set_visible(False)
            self._spinner.set_visible(True)
            self._spinner.set_spinning(True)
            self._start_timer()
            self._start_pulse()
            self._start_meter()
            self.clear_text()

        elif new == State.TRANSCRIBING:
            self._status_label.set_text("Transcribing\u2026")
            self._timer_label.set_visible(False)
            self._stop_timer()
            self._copy_btn.set_visible(False)
            self._cancel_btn.set_visible(True)
            self._action_btn.set_visible(False)
            self._window.set_default_widget(None)
            # Keep the live preview pulsing — first batch delta will swap it out
            self._awaiting_batch = True
            self._had_first_text = False
            self._start_text_pulse()

        elif new == State.CLEANING:
            self._status_label.set_text("Cleaning up\u2026")
            self._timer_label.set_visible(False)
            self._copy_btn.set_visible(False)
            self._cancel_btn.set_visible(True)
            self._action_btn.set_visible(False)
            self._window.set_default_widget(None)
            # show_transcript_for_cleanup arms the swap and starts text pulse

        elif new == State.READY:
            self._status_label.set_text("Ready")
            self._timer_label.set_visible(False)
            self._copy_btn.set_visible(True)
            self._cancel_btn.set_visible(False)
            self._action_btn_label.set_label("Close")
            self._action_btn.add_css_class("flat")
            self._action_btn.set_visible(True)
            self._window.set_default_widget(self._action_btn)
            if self._autoclose_total > 0 and self._window.is_active():
                self._start_autoclose_countdown()

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
            self._action_btn_label.set_label("Close")
            self._action_btn.add_css_class("flat")
            self._action_btn.set_visible(True)
            self._window.set_default_widget(self._action_btn)
            self.show_error_banner(machine.error_message)

    def _on_active_changed(self, window, _pspec) -> None:
        active = window.is_active()
        logger.debug("_on_active_changed: active=%s", active)
        if active:
            self._text_view.remove_css_class("backdrop")
        else:
            self._text_view.add_css_class("backdrop")
        if self._machine.state == State.READY and self._autoclose_total > 0:
            if active:
                self._start_autoclose_countdown()
            else:
                self._stop_autoclose_countdown()

    # ── Level meter ──

    def set_level_meter(self, meter) -> None:
        """Store a reference to the LevelMeter for polling."""
        self._level_meter = meter

    def _start_meter(self) -> None:
        self._stop_meter()
        if self._level_meter is None:
            return
        self._meter_bar.set_visible(True)
        self._meter_source = GLib.timeout_add(40, self._tick_meter)

    def _stop_meter(self) -> None:
        if self._meter_source is not None:
            GLib.source_remove(self._meter_source)
            self._meter_source = None
        self._meter_bar.set_visible(False)
        self._meter_bar.set_fraction(0)

    def _tick_meter(self) -> bool:
        meter = self._level_meter
        if meter is None:
            return True
        frac = _dbfs_to_fraction(meter.level_dbfs)
        self._meter_bar.set_fraction(frac)
        # Swap CSS class for color zone
        if frac > 0.95:
            zone = "vu-clip"
        elif frac > 0.80:
            zone = "vu-hot"
        elif frac > 0.33:
            zone = "vu-good"
        else:
            zone = "vu-low"
        for cls in _VU_ZONE_CLASSES:
            if cls != zone:
                self._meter_bar.remove_css_class(cls)
        self._meter_bar.add_css_class(zone)
        return True

    # ── Session directory ──

    def set_session_dir(self, path: str) -> None:
        """Store the session directory path for the folder button."""
        self._session_dir = path

    # ── Button handlers ──

    def _on_open_folder(self, _btn: Gtk.Button) -> None:
        if self._session_dir:
            Gio.AppInfo.launch_default_for_uri(
                GLib.filename_to_uri(self._session_dir, None), None
            )

    def _on_copy(self, _btn: Gtk.Button) -> None:
        buf = self._text_view.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, False)
        if text:
            logger.debug("_on_copy: text_len=%d", len(text))
            display = Gdk.Display.get_default()
            if display:
                display.get_clipboard().set(text)
            else:
                logger.debug("_on_copy: no display available")
        else:
            logger.debug("_on_copy: no text to copy")

    def _on_cancel(self, _btn: Gtk.Button) -> None:
        logger.debug("_on_cancel: state=%s", self._machine.state.name)
        if self._machine.state in (State.RECORDING, State.TRANSCRIBING, State.CLEANING):
            self._machine.transition(State.CANCELLED)

    def _on_action(self, _btn: Gtk.Button) -> None:
        s = self._machine.state
        logger.debug("_on_action: state=%s degraded=%s", s.name, self._degraded)
        if s == State.RECORDING:
            if self._degraded:
                self._machine.transition(State.CANCELLED)
            else:
                self._machine.transition(State.TRANSCRIBING)
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

    # ── Auto-close countdown ──

    def _start_autoclose_countdown(self) -> None:
        """Start (or restart) the countdown to auto-close the window.

        Always restarts from the full duration — regaining focus after a
        blur gives the user a fresh window to interact with the overlay.
        The remaining seconds are appended to the Close button label.
        """
        self._stop_autoclose_countdown()
        self._autoclose_remaining = self._autoclose_total
        self._update_autoclose_label()
        self._autoclose_source = GLib.timeout_add_seconds(1, self._tick_autoclose)

    def _stop_autoclose_countdown(self) -> None:
        if self._autoclose_source is None:
            return
        GLib.source_remove(self._autoclose_source)
        self._autoclose_source = None
        self._action_btn_label.set_label("Close")

    def _update_autoclose_label(self) -> None:
        self._action_btn_label.set_label(
            f'Close <span weight="light" fgalpha="50%">'
            f"({self._autoclose_remaining}s)</span>"
        )

    def _tick_autoclose(self) -> bool:
        self._autoclose_remaining -= 1
        if self._autoclose_remaining <= 0:
            self._autoclose_source = None
            self._window.close()
            return False
        self._update_autoclose_label()
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
            self._dot.remove_css_class("dim")
            self._pulse_dim = False
        else:
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
        logger.debug("destroy: tearing down UI")
        self._destroyed = True
        self._stop_timer()
        self._stop_pulse()
        self._stop_text_pulse()
        self._stop_meter()
        self._stop_autoclose_countdown()
