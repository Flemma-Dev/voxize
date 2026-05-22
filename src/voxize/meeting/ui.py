"""Meeting recorder status window — small always-on-top GTK overlay.

Single state: RECORDING (or STOPPING during the brief teardown). No
state machine, no autoclose, no transcription — just elapsed time, two
level meters (mic / system), live file size, and Stop. Reuses the
existing style.css / ``.vu-meter`` palette so it matches the dictation
overlay.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

logger = logging.getLogger(__name__)

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

_VU_ZONE_CLASSES = ("vu-low", "vu-good", "vu-hot", "vu-clip")
_METER_TICK_MS = 40
_TIMER_TICK_MS = 1000
_SIZE_TICK_MS = 1000


def _dbfs_to_fraction(dbfs: float) -> float:
    """Map dBFS [-60, 0] to fraction [0, 1] for the level bar."""
    return max(0.0, min(1.0, (dbfs + 60.0) / 60.0))


def _format_size(n_bytes: int) -> str:
    """Compact human-readable file size — MB once we cross 1 MB, else KB."""
    if n_bytes < 1024 * 1024:
        return f"{n_bytes / 1024:.0f} KB"
    return f"{n_bytes / (1024 * 1024):.1f} MB"


def _format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class MeetingWindow:
    """Builds and manages the meeting recorder UI widgets."""

    def __init__(
        self,
        window: Gtk.ApplicationWindow,
        on_stop: Callable[[], None],
    ) -> None:
        self._window = window
        self._on_stop = on_stop
        self._mic_meter = None
        self._sys_meter = None
        self._data_bytes_getter: Callable[[], int] | None = None
        self._session_dir: str | None = None
        self._elapsed_s = 0
        self._timer_source: int | None = None
        self._meter_source: int | None = None
        self._size_source: int | None = None
        self._destroyed = False
        self._stopping = False
        self._build()

    # ── Construction ──

    def _build(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(False)
        header.set_title_widget(Gtk.Box())

        # Status area: dot + label + elapsed timer
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._dot = Gtk.Label(label="●")  # ●
        self._dot.add_css_class("status-dot")
        self._dot.add_css_class("recording")
        status.append(self._dot)

        self._status_label = Gtk.Label(label="Recording")
        self._status_label.add_css_class("status-label")
        status.append(self._status_label)

        self._timer_label = Gtk.Label(label="00:00:00")
        self._timer_label.add_css_class("timer-label")
        self._timer_label.set_margin_start(8)
        status.append(self._timer_label)
        header.pack_start(status)

        # Action buttons (pack_end is right-to-left)
        self._stop_btn = Gtk.Button(label="Stop")
        self._stop_btn.add_css_class("destructive-action")
        self._stop_btn.connect("clicked", self._on_stop_clicked)
        header.pack_end(self._stop_btn)

        self._folder_btn = Gtk.Button.new_from_icon_name("folder-symbolic")
        self._folder_btn.set_tooltip_text("Open session folder")
        self._folder_btn.add_css_class("flat")
        self._folder_btn.add_css_class("dim-label")
        self._folder_btn.set_focusable(False)
        self._folder_btn.connect("clicked", self._on_open_folder)
        header.pack_end(self._folder_btn)

        self._window.set_titlebar(header)

        # Content: two meter rows + file size
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(14)
        content.set_margin_bottom(14)
        content.set_margin_start(14)
        content.set_margin_end(14)

        self._mic_bar = Gtk.ProgressBar()
        self._mic_bar.add_css_class("osd")
        self._mic_bar.add_css_class("vu-meter")
        content.append(self._build_meter_row("Mic", self._mic_bar))

        self._sys_bar = Gtk.ProgressBar()
        self._sys_bar.add_css_class("osd")
        self._sys_bar.add_css_class("vu-meter")
        content.append(self._build_meter_row("System", self._sys_bar))

        info_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        info_row.set_margin_top(4)
        self._size_label = Gtk.Label(label="0 KB")
        self._size_label.add_css_class("timer-label")
        self._size_label.set_xalign(1.0)
        self._size_label.set_hexpand(True)
        info_row.append(self._size_label)
        content.append(info_row)

        # Error bar — hidden until check_errors() shows it
        error_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        error_box.add_css_class("error-bar")
        error_icon = Gtk.Label(label="⚠")  # ⚠
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
        content.append(self._error_bar)

        self._window.set_child(content)
        self._window.set_default_widget(self._stop_btn)

    def _build_meter_row(self, label_text: str, bar: Gtk.ProgressBar) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        label = Gtk.Label(label=label_text)
        label.add_css_class("timer-label")
        label.set_xalign(0)
        label.set_width_chars(7)
        row.append(label)
        bar.set_hexpand(True)
        bar.set_valign(Gtk.Align.CENTER)
        row.append(bar)
        return row

    # ── Lifecycle ──

    def attach(
        self,
        mic_meter,
        sys_meter,
        data_bytes_getter: Callable[[], int],
        session_dir: str,
    ) -> None:
        """Wire the UI to the live capture (meters + file-size source)."""
        self._mic_meter = mic_meter
        self._sys_meter = sys_meter
        self._data_bytes_getter = data_bytes_getter
        self._session_dir = session_dir
        self._start_timers()

    def show_error(self, message: str) -> None:
        """Show a non-fatal error banner; capture continues if it can."""
        if self._destroyed:
            return
        logger.debug("show_error: %s", message)
        self._error_label.set_text(message)
        self._error_bar.set_visible(True)

    def mark_stopping(self) -> None:
        """Switch to a 'stopping' visual while teardown completes."""
        if self._destroyed:
            return
        logger.debug("mark_stopping")
        self._stopping = True
        self._status_label.set_text("Stopping…")
        self._dot.remove_css_class("recording")
        self._dot.add_css_class("cleaning")
        self._stop_btn.set_sensitive(False)

    def mark_compressing(self) -> None:
        """Switch to a 'compressing' visual; timer label re-roles to compress elapsed.

        Stops the per-second meeting timer and meters (no audio is flowing
        anymore — their last value would freeze anyway). The compress-elapsed
        value is pushed in from the background thread via
        :meth:`update_compress_elapsed`.
        """
        if self._destroyed:
            return
        logger.debug("mark_compressing")
        self._status_label.set_text("Compressing…")
        self._dot.remove_css_class("recording")
        self._dot.remove_css_class("cleaning")
        self._dot.add_css_class("transcribing")  # amber, reused
        self._stop_btn.set_sensitive(False)
        # Repurpose the timer label for compress elapsed; freeze the meeting
        # timer so we don't race the compress updates.
        if self._timer_source is not None:
            GLib.source_remove(self._timer_source)
            self._timer_source = None
        self._timer_label.set_text("00:00:00")

    def update_compress_elapsed(self, seconds: float) -> bool:
        """Idle callback fired from the compress thread with elapsed seconds."""
        if self._destroyed:
            return False
        self._timer_label.set_text(_format_duration(int(seconds)))
        return False  # one-shot idle

    def mark_done(self, success: bool, file_size_bytes: int) -> None:
        """Switch to a 'done' visual; window stays open until the user closes it.

        Mirrors the dictation app's READY state: green dot + 'Done' on
        success, amber dot + 'Saved (compress failed)' on a compression
        error (the WAV is preserved in that case, hence still 'Saved').
        The Stop button is re-labelled 'Close' and re-enabled, but the
        click handler stays the same — the app side checks its
        `_session_done` flag and routes a Close click straight to
        `_finalize_app`.
        """
        if self._destroyed:
            return
        logger.debug(
            "mark_done success=%s size=%d elapsed_s=%d",
            success,
            file_size_bytes,
            self._elapsed_s,
        )
        for cls in ("recording", "cleaning", "transcribing", "ready"):
            self._dot.remove_css_class(cls)
        if success:
            self._status_label.set_text("Done")
            self._dot.add_css_class("ready")
        else:
            # WAV is preserved on failure, so "Saved" is honest — just not
            # the compressed result the user expected.
            self._status_label.set_text("Saved (compress failed)")
            self._dot.add_css_class("cleaning")
        # Restore the timer label to the meeting elapsed (which we still
        # have in self._elapsed_s — only its tick source was stopped).
        self._timer_label.set_text(_format_duration(self._elapsed_s))
        # File size readout reflects the deliverable on disk (opus on
        # success, WAV on failure).
        self._size_label.set_text(_format_size(file_size_bytes))
        # Stop button → Close; drop the destructive styling so it doesn't
        # look like the user is about to throw work away.
        self._stop_btn.set_label("Close")
        self._stop_btn.remove_css_class("destructive-action")
        self._stop_btn.set_sensitive(True)
        # Release the "stopping" interlock — `_on_stop_clicked` and
        # `handle_escape` both early-return while it's set, which would
        # silently swallow clicks on the now-relabelled Close button.
        # By the time we get here the stop+compress sequence is done.
        self._stopping = False
        # Freeze meter bars at their last value — there's no fresh audio.
        if self._meter_source is not None:
            GLib.source_remove(self._meter_source)
            self._meter_source = None

    def destroy(self) -> None:
        logger.debug("destroy")
        self._destroyed = True
        self._stop_timers()

    # ── Timers ──

    def _start_timers(self) -> None:
        self._stop_timers()
        self._timer_source = GLib.timeout_add(_TIMER_TICK_MS, self._tick_timer)
        self._meter_source = GLib.timeout_add(_METER_TICK_MS, self._tick_meter)
        self._size_source = GLib.timeout_add(_SIZE_TICK_MS, self._tick_size)

    def _stop_timers(self) -> None:
        for attr in ("_timer_source", "_meter_source", "_size_source"):
            src = getattr(self, attr)
            if src is not None:
                GLib.source_remove(src)
                setattr(self, attr, None)

    def _tick_timer(self) -> bool:
        if self._destroyed:
            return False
        self._elapsed_s += 1
        self._timer_label.set_text(_format_duration(self._elapsed_s))
        return True

    def _tick_meter(self) -> bool:
        if self._destroyed:
            return False
        self._update_bar(self._mic_bar, self._mic_meter)
        self._update_bar(self._sys_bar, self._sys_meter)
        return True

    def _update_bar(self, bar: Gtk.ProgressBar, meter) -> None:
        if meter is None:
            return
        frac = _dbfs_to_fraction(meter.level_dbfs)
        bar.set_fraction(frac)
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
                bar.remove_css_class(cls)
        bar.add_css_class(zone)

    def _tick_size(self) -> bool:
        if self._destroyed:
            return False
        getter = self._data_bytes_getter
        if getter is None:
            return True
        try:
            n = getter()
        except Exception:
            logger.debug("_tick_size: getter raised", exc_info=True)
            return True
        self._size_label.set_text(_format_size(n))
        return True

    # ── Button handlers ──

    def _on_stop_clicked(self, _btn: Gtk.Button) -> None:
        if self._stopping:
            return
        logger.debug("_on_stop_clicked")
        self._on_stop()

    def _on_open_folder(self, _btn: Gtk.Button) -> None:
        if self._session_dir:
            Gio.AppInfo.launch_default_for_uri(
                GLib.filename_to_uri(self._session_dir, None), None
            )

    # ── Keyboard ──

    def handle_escape(self) -> bool:
        """Escape: ask the app to stop. Returns True if handled."""
        if self._stopping:
            return True
        logger.debug("handle_escape")
        self._on_stop()
        return True


__all__ = ["MeetingWindow"]
