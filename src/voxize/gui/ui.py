"""Voxize dictation overlay ("3a") — stock libadwaita card, status row, strip.

Layout, top to bottom: a draggable toolbar strip (mode menu, guidance,
folder, close), then one .card holding the transcript, an inline notice,
the status/action row and a 2px strip along the card's bottom edge — a
LevelBar while capturing, a ProgressBar while working. Everything is a
stock widget with stock classes; dictation.css holds the few exceptions.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable

import gi

logger = logging.getLogger(__name__)

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

# gi.require_version() above is executable code; all subsequent imports trigger E402.
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from voxize.gui.client_state import ClientState  # noqa: E402
from voxize.protocol import State  # noqa: E402

# No prediction (L < 50): pulse instead of a fraction.
_PULSE_MS = 100

# Zone boundaries as fractions of the [-60, 0] dBFS range — see _level_zone.
_ZONE_GOOD = 0.33
_ZONE_HOT = 0.80
_ZONE_CLIP = 0.95
_LEVEL_ZONE_CLASSES = ("zone-quiet", "zone-hot", "zone-clip")

# Provisional line height until the real one is measured (15px system font).
_FALLBACK_LINE_PX = 20
_MAX_LINES_LIVE = 6
_MAX_LINES_READY = 10

_DOT_CLASSES = ("idle", "recording", "working", "ready", "error")
_WORKING = (State.TRANSCRIBING, State.CLEANING)
# States in which switching to Meeting abandons the dictation.
# Cancelling here would throw away recorded audio, so it asks first.
_CONFIRM_CANCEL = (State.RECORDING, State.TRANSCRIBING, State.CLEANING)
_IN_FLIGHT = (
    State.INITIALIZING,
    State.WARMING,
    State.RECORDING,
    State.TRANSCRIBING,
    State.CLEANING,
)

_PLACEHOLDER = "Waiting for audio"
_NO_SPEECH = "No speech detected."
_NO_PREVIEW = "No live preview."
_NOTICE_PREVIEW_LOST = "Live preview disconnected — still recording."
_NOTICE_BATCH_FAILED = "Transcription failed — showing the live preview."
_NOTICE_BATCH_FAILED_EMPTY = (
    "Transcription failed — the recording is saved in the session folder."
)
_NOTICE_CLEANUP_FAILED = "Cleanup failed — showing the uncleaned transcript."


def _dbfs_to_fraction(dbfs: float) -> float:
    """Map dBFS [-60, 0] to fraction [0, 1] for the level meter."""
    return max(0.0, min(1.0, (dbfs + 60.0) / 60.0))


def _level_zone(fraction: float) -> str | None:
    """Classify a level fraction into a mic-gain zone.

    Thresholds are carried over unchanged from the pre-BREATH meter — on
    RMS dBFS they land at -40.2 / -12 / -3, which is a sane band for speech.
    Keeping the exact numbers matters more than tuning them: the meter is
    only useful as a gain check if what "good" means stays put.

    Returns None for the good zone, which is the stock accent colour.
    """
    if fraction > _ZONE_CLIP:
        return "zone-clip"
    if fraction > _ZONE_HOT:
        return "zone-hot"
    if fraction > _ZONE_GOOD:
        return None
    return "zone-quiet"


def describe_widget(widget: Gtk.Widget | None) -> str:
    """Short, log-friendly name for a widget: type, label and CSS classes."""
    if widget is None:
        return "None"
    label = widget.get_label() if hasattr(widget, "get_label") else None
    classes = ".".join(widget.get_css_classes())
    return (
        f"{type(widget).__name__}"
        + (f"({label!r})" if label else "")
        + (f".{classes}" if classes else "")
        + ("" if widget.get_mapped() else "[unmapped]")
    )


def _format_timer(seconds: int) -> str:
    """``mm:ss`` up to 59:59, then ``h:mm:ss``."""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class OverlayWindow:
    """Builds and manages the Voxize dictation overlay widgets."""

    def __init__(
        self,
        window: Adw.ApplicationWindow,
        machine: ClientState,
        autoclose_seconds: int = 0,
        on_switch_to_meeting: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        self._window = window
        self._machine = machine
        self._on_switch_to_meeting = on_switch_to_meeting
        self._on_stop_cb = on_stop or (lambda: None)
        self._on_cancel_cb = on_cancel or (lambda: None)
        self._timer_seconds = 0
        self._timer_source: int | None = None
        self._scroll_pending = False
        self._destroyed = False
        self._session_dir: str | None = None
        self._had_first_text = False
        # True when the buffer holds transcript text worth copying — not the
        # placeholder, "No speech detected." or an error message.
        self._copyable = False
        self._preview_error: str | None = None
        self._failure: tuple[str, str, str] | None = None
        self._pulse_source: int | None = None
        self._stalled = False
        self._autoclose_total = autoclose_seconds
        self._autoclose_remaining = 0
        self._autoclose_source: int | None = None
        self._resize_pending = False
        self._text_height = 0
        self._height_floor = 0
        self._line_height = 0
        self._max_lines = _MAX_LINES_LIVE
        self._prompt_sources: list[dict] = []
        self._cancel_dialog: Adw.AlertDialog | None = None
        self._mode_btn: Gtk.MenuButton | None = None
        self._mode_caption_shown: bool | None = None
        self._mode_caption_wanted: bool | None = None
        self._build()
        machine.on_change(self._on_state_change)

    # ── Construction ──

    def _build(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(outer)
        self._window.set_content(self._toast_overlay)

        self._window.connect("notify::is-active", self._on_active_changed)
        # Diagnostics for keyboard routing (Enter → default widget). Debug
        # level, so they only reach the session's debug.log.
        self._window.connect(
            "notify::focus-widget",
            lambda w, _p: logger.debug("focus: %s", describe_widget(w.get_focus())),
        )
        self._window.connect(
            "activate-default",
            lambda w: logger.debug(
                "activate-default: focus=%s default=%s",
                describe_widget(w.get_focus()),
                describe_widget(w.get_default_widget()),
            ),
        )

        self._build_strip(outer)

        # ── Card ──

        self._card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._card.add_css_class("card")
        self._card.set_margin_start(8)
        self._card.set_margin_end(8)
        self._card.set_margin_bottom(8)
        # Clips the bottom strip to the card's rounded corners. Focus rings
        # are drawn inset (outline-offset: -2px) so this does not cut them.
        self._card.set_overflow(Gtk.Overflow.HIDDEN)
        outer.append(self._card)

        # ── Transcript ──

        self._text_view = Gtk.TextView()
        self._text_view.set_editable(False)
        self._text_view.set_cursor_visible(False)
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._text_view.add_css_class("transcript")
        self._text_view.set_top_margin(14)
        self._text_view.set_bottom_margin(0)
        self._text_view.set_left_margin(16)
        self._text_view.set_right_margin(16)

        buf = self._text_view.get_buffer()
        self._end_mark = buf.create_mark("end", buf.get_end_iter(), False)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_child(self._text_view)
        # EXTERNAL until the text overflows the line cap: a present stock
        # scrollbar imposes its own ~58px minimum height, which would hold a
        # one-line transcript open at two lines. See _resize_text_area.
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        self._scroll.set_min_content_height(_FALLBACK_LINE_PX)
        self._scroll.set_max_content_height(_MAX_LINES_LIVE * _FALLBACK_LINE_PX)
        # Relayout (including the first real allocation) changes how the
        # transcript wraps, so re-measure whenever the scroller reports
        # new geometry. Converges: an unchanged height triggers no work.
        self._scroll.get_vadjustment().connect(
            "changed", lambda _a: self._queue_text_resize()
        )
        self._card.append(self._scroll)

        # ── Inline notice (Gtk.Revealer) ──

        self._notice_revealer = Gtk.Revealer()
        self._notice_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._notice_revealer.set_reveal_child(False)

        notice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        notice.set_margin_top(12)
        notice.set_margin_start(16)
        notice.set_margin_end(16)
        notice_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        notice_icon.add_css_class("warning")
        notice_icon.set_valign(Gtk.Align.START)
        notice.append(notice_icon)
        self._notice_label = Gtk.Label()
        self._notice_label.set_wrap(True)
        self._notice_label.set_xalign(0)
        # Without hexpand a wrapping label settles at its natural width and
        # wraps short sentences at half the card.
        self._notice_label.set_hexpand(True)
        notice.append(self._notice_label)
        self._notice_revealer.set_child(notice)
        self._card.append(self._notice_revealer)

        # ── Status / action row ──

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_top(12)
        row.set_margin_end(10)
        row.set_margin_bottom(10)
        row.set_margin_start(16)

        self._dot = Gtk.Box()
        self._dot.add_css_class("status-dot")
        self._dot.add_css_class("idle")
        self._dot.set_valign(Gtk.Align.CENTER)
        row.append(self._dot)

        self._status_label = Gtk.Label(label="Starting…")
        self._status_label.add_css_class("heading")
        row.append(self._status_label)

        self._timer_label = Gtk.Label()
        self._timer_label.add_css_class("numeric")
        self._timer_label.set_visible(False)
        row.append(self._timer_label)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)

        self._cancel_btn = Gtk.Button(label="Cancel")
        self._cancel_btn.add_css_class("flat")
        self._cancel_btn.set_valign(Gtk.Align.CENTER)
        self._cancel_btn.connect("clicked", self._on_cancel)
        row.append(self._cancel_btn)

        self._stop_btn = Gtk.Button.new_from_icon_name("media-playback-stop-symbolic")
        self._stop_btn.add_css_class("circular")
        self._stop_btn.add_css_class("destructive-action")
        self._stop_btn.set_tooltip_text("Stop")
        self._stop_btn.set_valign(Gtk.Align.CENTER)
        self._stop_btn.connect("clicked", self._on_stop)
        self._stop_btn.set_visible(False)
        row.append(self._stop_btn)

        self._close_btn = Gtk.Button(label="Close")
        self._close_btn.add_css_class("flat")
        self._close_btn.set_valign(Gtk.Align.CENTER)
        self._close_btn.connect("clicked", lambda _b: self._window.close())
        self._close_btn.set_visible(False)
        row.append(self._close_btn)

        self._copy_btn = Gtk.Button(label="Copy")
        self._copy_btn.add_css_class("suggested-action")
        self._copy_btn.set_valign(Gtk.Align.CENTER)
        self._copy_btn.connect("clicked", lambda _b: self.copy_to_clipboard())
        self._copy_btn.set_visible(False)
        row.append(self._copy_btn)

        self._card.append(row)

        # ── Bottom strip: level meter / pipeline progress ──

        self._strip = Gtk.Stack()
        self._strip.set_transition_type(Gtk.StackTransitionType.NONE)

        self._levelbar = Gtk.LevelBar()
        self._levelbar.add_css_class("strip")
        self._levelbar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        self._levelbar.set_min_value(0.0)
        self._levelbar.set_max_value(1.0)
        # The stock offsets would paint the fill warning-yellow below 25%
        # and success-green at 100% — i.e. clipping would read as "good".
        # Zones are a class on the bar instead (see dictation.css).
        for name in (
            Gtk.LEVEL_BAR_OFFSET_LOW,
            Gtk.LEVEL_BAR_OFFSET_HIGH,
            Gtk.LEVEL_BAR_OFFSET_FULL,
        ):
            self._levelbar.remove_offset_value(name)
        self._strip.add_named(self._levelbar, "level")

        self._progress = Gtk.ProgressBar()
        self._progress.add_css_class("osd")
        self._strip.add_named(self._progress, "progress")

        self._strip.set_visible_child_name("level")
        self._card.append(self._strip)

        self._prime_initial_size()

    def _build_strip(self, outer: Gtk.Box) -> None:
        """Top strip: mode menu on the left, dimmed icon buttons on the right.

        `.toolbar` makes every button in it flat, as a headerbar would. The
        strip doubles as the drag handle; buttons inside still get clicks.
        """
        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        strip.add_css_class("toolbar")

        if self._on_switch_to_meeting is not None:
            action = Gio.SimpleAction.new_stateful(
                "mode",
                GLib.VariantType.new("s"),
                GLib.Variant.new_string("dictation"),
            )
            action.connect("change-state", self._on_mode_change_state)
            self._window.add_action(action)
            self._mode_btn = Gtk.MenuButton(label="Dictation")
            self._unfocusable(self._mode_btn)
            self._mode_btn.connect("notify::active", self._on_mode_menu_toggled)
            strip.append(self._mode_btn)
            self._update_mode_menu(State.INITIALIZING)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        strip.append(spacer)

        self._guidance_btn = Gtk.MenuButton(icon_name="view-list-symbolic")
        self._guidance_btn.set_tooltip_text("Vocabulary guidance")
        self._guidance_btn.add_css_class("dimmed")
        self._unfocusable(self._guidance_btn)
        self._guidance_btn.set_visible(False)
        # Undim while open: the open state lives on the inner button, which
        # CSS on the outer node cannot see.
        self._guidance_btn.connect("notify::active", self._on_guidance_toggled)
        self._guidance_popover = Gtk.Popover()
        self._guidance_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._guidance_popover.set_child(self._guidance_box)
        self._guidance_btn.set_popover(self._guidance_popover)
        strip.append(self._guidance_btn)

        self._folder_btn = Gtk.Button.new_from_icon_name("folder-symbolic")
        self._folder_btn.set_tooltip_text("Open session folder (Ctrl+O)")
        self._folder_btn.add_css_class("dimmed")
        self._folder_btn.set_focusable(False)
        self._folder_btn.connect("clicked", self._on_open_folder)
        strip.append(self._folder_btn)

        close_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_btn.set_tooltip_text("Close")
        close_btn.add_css_class("dimmed")
        close_btn.set_focusable(False)
        close_btn.connect("clicked", lambda _b: self._window.close())
        strip.append(close_btn)

        handle = Gtk.WindowHandle()
        handle.set_child(strip)
        outer.append(handle)

    @staticmethod
    def _unfocusable(menu_button: Gtk.MenuButton) -> None:
        """Keep keyboard focus off the strip, as the old icon buttons did.

        A MenuButton forwards focus to its inner button, so both are set.
        """
        menu_button.set_focusable(False)
        inner = menu_button.get_first_child()
        if inner is not None:
            inner.set_focusable(False)

    def _prime_initial_size(self) -> None:
        """Lay INITIALIZING out at WARMING's height so the mapped size is honest.

        ``app.py`` calls ``present()`` while the machine is still
        INITIALIZING, and Mutter centres whatever size it sees then — on
        Wayland that is the only chance to be centred, because the window
        cannot reposition itself afterwards. WARMING adds the timer (same
        row) and Stop, which is 34px tall like Cancel, so the two match.
        """
        self._show_waiting_placeholder()
        self._cancel_btn.set_visible(True)
        self._measure_line_height()
        if self._line_height:
            self._scroll.set_min_content_height(self._line_height)

    def setup_max_height(self) -> None:
        """Line caps are fixed (6 live / 10 ready), not screen-derived."""
        self._queue_text_resize()

    # ── Mode menu ──

    def _update_mode_menu(self, state: State) -> None:
        """Ask for the mode menu's caption to match ``state``.

        The rebuild replaces the popover, so it never happens while the menu
        is open — choosing "Meeting" ends the dictation from inside the
        menu's own click handler — and is applied once the menu closes.
        """
        if self._mode_btn is None:
            return
        self._mode_caption_wanted = state in _IN_FLIGHT
        if not self._mode_btn.get_active():
            self._rebuild_mode_menu()

    def _on_mode_menu_toggled(self, btn: Gtk.MenuButton, _pspec) -> None:
        if not btn.get_active():
            GLib.idle_add(self._rebuild_mode_menu)

    def _rebuild_mode_menu(self) -> bool:
        if self._destroyed or self._mode_btn is None:
            return False
        with_caption = self._mode_caption_wanted
        if with_caption == self._mode_caption_shown:
            return False
        self._mode_caption_shown = with_caption

        menu = Gio.Menu()
        modes = Gio.Menu()
        dictation = Gio.MenuItem.new("Dictation", None)
        dictation.set_action_and_target_value(
            "win.mode", GLib.Variant.new_string("dictation")
        )
        modes.append_item(dictation)
        meeting = Gio.MenuItem.new("Meeting", None)
        meeting.set_action_and_target_value(
            "win.mode", GLib.Variant.new_string("meeting")
        )
        # Display only — the key itself is handled by app.py:_on_key, as it
        # was before the menu existed.
        meeting.set_attribute_value("accel", GLib.Variant.new_string("<Control>m"))
        modes.append_item(meeting)
        menu.append_section(None, modes)

        caption = None
        if with_caption:
            section = Gio.Menu()
            item = Gio.MenuItem.new(None, None)
            item.set_attribute_value("custom", GLib.Variant.new_string("caption"))
            section.append_item(item)
            menu.append_section(None, section)
            caption = Gtk.Label(label="Switching ends this dictation.")
            caption.add_css_class("dimmed")
            caption.add_css_class("caption")
            caption.set_xalign(0)

        popover = Gtk.PopoverMenu.new_from_model(menu)
        if caption is not None:
            popover.add_child(caption, "caption")
        self._mode_btn.set_popover(popover)
        return False

    def _on_mode_change_state(self, _action, value: GLib.Variant) -> None:
        # The state is never changed: this window is always Dictation.
        if value.get_string() == "meeting":
            self._on_mode_switch("meeting")

    def overlay_open(self) -> bool:
        """True while a popover or the cancel dialog is showing.

        Escape then belongs to that surface (close it / Keep), not to the
        window's cancel handling.
        """
        if self._cancel_dialog is not None:
            return True
        buttons = [self._guidance_btn]
        if self._mode_btn is not None:
            buttons.append(self._mode_btn)
        return any(b.get_active() for b in buttons)

    # ── Cancel confirmation ──

    def request_cancel(self) -> None:
        """Cancel the dictation, asking first once there is audio to lose.

        INITIALIZING and WARMING cancel at once — nothing has been said yet.
        From RECORDING on, an Adw.AlertDialog asks, with Keep as both the
        default and the close response: Enter or Escape never discard.
        """
        state = self._machine.state
        logger.debug("request_cancel: state=%s", state.name)
        if state not in _IN_FLIGHT:
            return
        if state not in _CONFIRM_CANCEL:
            self._on_cancel_cb()
            return
        if self._cancel_dialog is not None:
            return
        dialog = Adw.AlertDialog(
            heading="Discard this dictation?",
            body="The audio stays in the session folder.",
        )
        # Keep first: it is the focused button, as well as the default.
        dialog.add_response("keep", "Keep")
        dialog.add_response("discard", "Discard")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("keep")
        dialog.set_close_response("keep")
        dialog.connect("response", self._on_cancel_response)
        dialog.connect("closed", self._on_cancel_dialog_closed)
        self._cancel_dialog = dialog
        # Libadwaita hosts the dialog inside the window when it fits and
        # otherwise opens it as a modal window attached to ours — at the
        # usual ~150px height it is the latter, so nothing resizes. When it
        # is hosted inside, Enter must not reach the window's default (Stop).
        self._window.set_default_widget(None)
        dialog.present(self._window)

    def _on_cancel_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        logger.debug(
            "cancel dialog: response=%s state=%s",
            response,
            self._machine.state.name,
        )
        # The pipeline may have finished while the question was up.
        if response == "discard" and self._machine.state in _CONFIRM_CANCEL:
            self._on_cancel_cb()

    def _on_cancel_dialog_closed(self, dialog: Adw.AlertDialog) -> None:
        # A late "closed" from a force-closed dialog must not clear a newer one.
        if self._cancel_dialog is dialog:
            self._cancel_dialog = None
        if self._destroyed:
            return
        if self._machine.state == State.RECORDING:
            self._window.set_default_widget(self._stop_btn)

    def _close_cancel_dialog(self) -> None:
        """The question no longer applies (READY, or the window is going)."""
        dialog = self._cancel_dialog
        if dialog is not None:
            # Clear first: "closed" is not reliably emitted by force_close()
            # for a dialog shown as its own window (probed), and a stale
            # reference would keep overlay_open() true — Escape at READY
            # would then never close the window.
            self._cancel_dialog = None
            dialog.force_close()

    # ── Guidance popover ──

    def show_prompt_context(self, sources: list[dict]) -> None:
        """Populate the guidance popover and show the icon."""
        if self._destroyed:
            return
        logger.debug("show_prompt_context: sources=%d", len(sources))
        self._prompt_sources = sources
        if not sources:
            self._guidance_btn.set_visible(False)
            return
        box = self._guidance_box
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

        title = Gtk.Label(label="Vocabulary in use")
        title.add_css_class("heading")
        title.set_xalign(0)
        box.append(title)

        for source in sources:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            # A link label rather than a Gtk.LinkButton: the button's stock
            # padding indents the filename away from the title and preview.
            # The default activate-link handler opens it in the default app.
            uri = GLib.filename_to_uri(source["path"], None)
            name = os.path.basename(source["path"])
            link = Gtk.Label()
            link.set_markup(
                f'<a href="{GLib.markup_escape_text(uri)}">'
                f"{GLib.markup_escape_text(name)}</a>"
            )
            link.set_tooltip_text(source["path"])
            link.set_xalign(0)
            link.set_halign(Gtk.Align.START)
            row.append(link)

            preview = Gtk.Label()
            preview.add_css_class("dimmed")
            preview.add_css_class("caption")
            preview.set_text(
                source["content"][:80] + ("…" if len(source["content"]) > 80 else "")
            )
            preview.set_xalign(0)
            preview.set_wrap(True)
            # A long unbroken glossary token must wrap, not widen the popover.
            preview.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            preview.set_max_width_chars(40)
            row.append(preview)

            box.append(row)

        self._guidance_btn.set_visible(self._machine.state != State.ERROR)

    def _on_guidance_toggled(self, btn: Gtk.MenuButton, _pspec) -> None:
        if btn.get_active():
            btn.remove_css_class("dimmed")
        else:
            btn.add_css_class("dimmed")

    # ── Cost display (tooltip on dot) ──

    def show_session_costs(
        self,
        live_cost: float | None,
        batch_cost: float | None,
        cleanup_cost: float | None,
    ) -> None:
        """Show session costs as tooltip on the status dot."""
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
            sep = " · "
            self._dot.set_tooltip_text(f"Total ${total:.4f}\n{sep.join(parts)}")
        else:
            self._dot.set_tooltip_text(None)

    # ── Text operations ──

    def on_text(self, stage: str, op: str, text: str) -> None:
        """Transcript text from the backend: live, batch or cleaned.

        The first text of a stage replaces what is on screen. Text that is
        about to be superseded is dimmed: the live preview once the batch
        pass is running, the batch transcript while cleanup runs.
        """
        if self._destroyed:
            return
        if not self._had_first_text:
            logger.debug("on_text: first text arrived, len=%d", len(text))
            self._had_first_text = True
        if op == "replace":
            logger.debug("on_text: replace stage=%s len=%d", stage, len(text))
            self.clear_text()
        state = self._machine.state
        superseded = (state == State.TRANSCRIBING and stage == "live") or (
            state == State.CLEANING and stage == "batch"
        )
        self._set_dimmed(superseded)
        self._copyable = True
        buf = self._text_view.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        self._queue_text_resize()
        self._queue_scroll_to_end()

    def clear_text(self) -> None:
        self._text_view.get_buffer().set_text("")
        self._queue_text_resize()

    def _set_text(self, text: str, *, copyable: bool, dimmed: bool) -> None:
        """Replace the whole buffer in one go (failure and terminal states)."""
        self._text_view.get_buffer().set_text(text)
        self._copyable = copyable
        self._set_dimmed(dimmed)
        self._queue_text_resize()
        self._queue_scroll_to_end()

    def _set_dimmed(self, dimmed: bool) -> None:
        """Outgoing and placeholder text carry the stock .dimmed class."""
        if dimmed:
            self._text_view.add_css_class("dimmed")
        else:
            self._text_view.remove_css_class("dimmed")

    def _queue_scroll_to_end(self) -> None:
        if not self._scroll_pending:
            self._scroll_pending = True
            GLib.idle_add(self._scroll_to_end)

    def _scroll_to_end(self) -> bool:
        self._scroll_pending = False
        vadj = self._scroll.get_vadjustment()
        vadj.set_value(vadj.get_upper() - vadj.get_page_size())
        return False

    def show_no_speech(self) -> None:
        """Batch returned nothing: say so, and leave nothing to copy."""
        if self._destroyed:
            return
        self._had_first_text = True
        self._set_text(_NO_SPEECH, copyable=False, dimmed=True)

    # ── Inline notice ──

    def show_live_preview_error(self, message: str) -> None:
        """WS failed while capturing — recording carries on, preview stops."""
        if self._destroyed:
            return
        logger.debug("show_live_preview_error: message=%s", message)
        self._preview_error = message
        if self._machine.state in (State.WARMING, State.RECORDING):
            self._set_dimmed(True)
            self._reveal_notice(_NOTICE_PREVIEW_LOST, message)

    def show_failure(self, phase: str, message: str, fallback: str | None) -> None:
        """Arm the READY notice. The fallback text itself arrived as text."""
        logger.debug(
            "show_failure: phase=%s fallback=%s message=%s", phase, fallback, message
        )
        self._failure = (phase, message, fallback)

    def _reveal_notice(self, text: str, detail: str) -> None:
        self._notice_label.set_text(text)
        # The raw error is still one hover away; it used to be the banner.
        self._notice_label.set_tooltip_text(detail or None)
        self._notice_revealer.set_reveal_child(True)

    def _hide_notice(self) -> None:
        self._notice_revealer.set_reveal_child(False)

    def _apply_failure(self) -> None:
        """On READY: explain why the fallback text is what's on screen."""
        phase, message, fallback = self._failure
        if phase == "batch":
            if fallback is not None:
                self._set_dimmed(False)
                self._reveal_notice(_NOTICE_BATCH_FAILED, message)
            else:
                self._set_text(_NO_PREVIEW, copyable=False, dimmed=True)
                self._reveal_notice(_NOTICE_BATCH_FAILED_EMPTY, message)
        else:
            # The full batch transcript is on screen — the same text the
            # clipboard already holds, never a half-streamed cleanup.
            self._set_dimmed(False)
            self._reveal_notice(_NOTICE_CLEANUP_FAILED, message)

    # ── Text area sizing ──

    def _queue_text_resize(self) -> None:
        """Re-measure the transcript after the next layout pass."""
        if self._resize_pending:
            return
        self._resize_pending = True
        GLib.idle_add(self._resize_text_area)

    def _measure_line_height(self) -> None:
        """Measure one line of the transcript font, once.

        Measured rather than assumed: the system font differs between
        machines, and a hardcoded constant would make the line caps a lie.
        """
        if self._line_height:
            return
        layout = self._text_view.create_pango_layout("Ag")
        base = layout.get_pixel_extents()[1].height
        if base > 0:
            self._line_height = base
            logger.debug("line height: %dpx", base)

    def _resize_text_area(self) -> bool:
        """Grow the card to fit the transcript, up to the line cap.

        ``propagate_natural_height`` cannot help here: a wrapping TextView
        has no natural height until it knows its width, so the scroller
        would settle at roughly two lines regardless of content.
        """
        self._resize_pending = False
        if self._destroyed:
            return False
        if self._text_view.get_width() <= 1:
            # Not allocated yet; measuring now would read an unwrapped
            # layout and settle the card at one or two lines.
            self._queue_text_resize()
            return False
        self._measure_line_height()
        line = self._line_height or _FALLBACK_LINE_PX
        content = self._text_view.measure(
            Gtk.Orientation.VERTICAL, self._text_view.get_width()
        )[1]
        cap = self._max_lines * line + self._text_view.get_top_margin()
        # Round the content up: truncating leaves the viewport a pixel
        # short of its own text and GTK answers with a scrollbar for
        # content that visibly fits.
        height = int(max(line, min(math.ceil(content), cap)))
        # Only offer a scrollbar once there is something to scroll to.
        self._scroll.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC if content > cap else Gtk.PolicyType.EXTERNAL,
        )
        # Ratchet: the text area may grow but never shrink. Each handoff
        # (live -> batch -> cleanup) restarts the transcript from empty, so
        # without this the card collapses to one line and re-grows three
        # times per session, tearing the window on every pass.
        height = max(height, self._height_floor)
        self._height_floor = height
        if height != self._text_height:
            self._text_height = height
            # GtkScrolledWindow rejects a min above the current max, so
            # drop the min before moving the max. Setting them in the
            # wrong order silently leaves the min at its old value.
            self._scroll.set_min_content_height(-1)
            self._scroll.set_max_content_height(height)
            self._scroll.set_min_content_height(height)
        return False

    def _show_waiting_placeholder(self) -> None:
        """One dimmed line standing in for the words to come."""
        self._had_first_text = False
        self._set_text(_PLACEHOLDER, copyable=False, dimmed=True)

    # ── State change handler ──

    def _set_dot(self, cls: str) -> None:
        for c in _DOT_CLASSES:
            if c != cls:
                self._dot.remove_css_class(c)
        self._dot.add_css_class(cls)

    def _show_buttons(self, *visible: Gtk.Button) -> None:
        for btn in (self._cancel_btn, self._stop_btn, self._close_btn, self._copy_btn):
            btn.set_visible(btn in visible)

    def _on_state_change(self, machine: ClientState, old: State, new: State) -> None:
        logger.debug("_on_state_change: %s -> %s", old.name, new.name)

        # READY (or ERROR) while the question is up: it no longer applies.
        # Not on CANCELLED — that arrives from inside the dialog's own
        # "response" handler, and the dialog is already closing itself.
        if new not in _CONFIRM_CANCEL and new != State.CANCELLED:
            self._close_cancel_dialog()

        self._stop_autoclose_countdown()
        self._timer_label.remove_css_class("dimmed")
        self._close_btn.remove_css_class("suggested-action")
        self._close_btn.add_css_class("flat")
        self._update_mode_menu(new)
        if new not in _WORKING:
            self._stalled = False

        audio_phase = {State.WARMING, State.RECORDING}
        if old in audio_phase and new not in audio_phase:
            self._stop_meter()
            self._stop_timer()
            # The preview-lost notice is about capture; it ends with it.
            self._preview_error = None
            self._hide_notice()

        if new == State.WARMING:
            self._set_dot("idle")
            self._status_label.set_text("Warming up…")
            self._preview_error = None
            self._failure = None
            self._hide_notice()
            self._timer_seconds = 0
            self._timer_label.set_text(_format_timer(0))
            self._timer_label.set_visible(True)
            self._timer_label.add_css_class("dimmed")
            self._show_buttons(self._cancel_btn, self._stop_btn)
            self._window.set_default_widget(self._stop_btn)
            self._show_waiting_placeholder()
            self._show_level()
            self._max_lines = _MAX_LINES_LIVE
            self._queue_text_resize()

        elif new == State.RECORDING:
            self._set_dot("recording")
            self._status_label.set_text("Listening")
            if old != State.WARMING:
                # Mock mode skips WARMING.
                self._failure = None
                self._timer_seconds = 0
                self._timer_label.set_text(_format_timer(0))
            if not self._had_first_text:
                self._show_waiting_placeholder()
            if self._preview_error is not None:
                # Failed while warming: say so now the session is live.
                self._set_dimmed(True)
                self._reveal_notice(_NOTICE_PREVIEW_LOST, self._preview_error)
            self._timer_label.set_visible(True)
            self._show_buttons(self._cancel_btn, self._stop_btn)
            self._window.set_default_widget(self._stop_btn)
            self._show_level()
            self._start_timer()

        elif new == State.TRANSCRIBING:
            self._set_dot("working")
            self._status_label.set_text("Working…")
            self._stalled = False
            self._timer_label.set_visible(True)
            self._timer_label.add_css_class("dimmed")
            self._show_buttons(self._cancel_btn)
            self._window.set_default_widget(None)
            self._had_first_text = False
            self._set_dimmed(True)
            self._progress.set_fraction(0.0)
            self._strip.set_visible_child_name("progress")
            self._strip.set_visible(True)

        elif new == State.CLEANING:
            # Same visual state as TRANSCRIBING, label included — a stall
            # that began in the batch pass is still a stall.
            self._set_dot("working")
            self._timer_label.set_visible(True)
            self._timer_label.add_css_class("dimmed")
            self._show_buttons(self._cancel_btn)
            self._window.set_default_widget(None)

        elif new == State.READY:
            self._set_dot("ready")
            self._status_label.set_text("Ready")
            self._reset_progress()
            self._strip.set_visible(False)
            self._timer_label.set_visible(True)
            self._timer_label.add_css_class("dimmed")
            if self._failure is not None:
                self._apply_failure()
            else:
                self._hide_notice()
                if self._copyable:
                    self._set_dimmed(False)
            self._copy_btn.set_sensitive(self._copyable)
            self._show_buttons(self._close_btn, self._copy_btn)
            self._window.set_default_widget(
                self._copy_btn if self._copyable else self._close_btn
            )
            logger.debug(
                "ready: default=%s focus=%s active=%s",
                describe_widget(self._window.get_default_widget()),
                describe_widget(self._window.get_focus()),
                self._window.is_active(),
            )
            # At ready the text is a finished artefact being proof-read
            # before paste; forcing a scroll through a 9-line result is
            # hostile, so the cap rises.
            self._max_lines = _MAX_LINES_READY
            self._queue_text_resize()
            if self._autoclose_total > 0 and self._window.is_active():
                self._start_autoclose_countdown()

        elif new == State.CANCELLED:
            self._reset_progress()
            self._stop_timer()
            GLib.idle_add(self._window.close)

        elif new == State.ERROR:
            self._set_dot("error")
            self._status_label.set_text("Error")
            self._reset_progress()
            self._strip.set_visible(False)
            self._timer_label.set_visible(False)
            self._stop_timer()
            self._stop_meter()
            self._hide_notice()
            self._guidance_btn.set_visible(False)
            # Whether an empty session folder is trashed on Ctrl+M is the
            # backend's call (its own had_content), not this window's.
            self._set_text(machine.error_message, copyable=False, dimmed=False)
            self._close_btn.remove_css_class("flat")
            self._close_btn.add_css_class("suggested-action")
            self._show_buttons(self._close_btn)
            self._window.set_default_widget(self._close_btn)

    def _on_active_changed(self, window, _pspec) -> None:
        active = window.is_active()
        logger.debug("_on_active_changed: active=%s", active)
        if active:
            from voxize import clipboard

            clipboard.flush()
        # Focus-gated (journal 2026-04-21): counts while focused, stops on
        # blur, restarts from the full duration on refocus. Nothing else —
        # pointer, Copy — touches it.
        if self._machine.state == State.READY and self._autoclose_total > 0:
            if active:
                self._start_autoclose_countdown()
            else:
                self._stop_autoclose_countdown()

    # ── Level meter ──

    def set_level(self, dbfs: float) -> None:
        """Raw RMS from the backend, 25 times a second while capturing."""
        if self._destroyed or self._machine.state not in (
            State.WARMING,
            State.RECORDING,
        ):
            return
        frac = _dbfs_to_fraction(dbfs)
        self._levelbar.set_value(frac)
        self._set_level_zone(_level_zone(frac))

    def _show_level(self) -> None:
        self._strip.set_visible_child_name("level")
        self._strip.set_visible(True)

    def _set_level_zone(self, zone: str | None) -> None:
        """Swap the mic-gain zone class on the level bar."""
        for cls in _LEVEL_ZONE_CLASSES:
            if cls != zone:
                self._levelbar.remove_css_class(cls)
        if zone is not None and not self._levelbar.has_css_class(zone):
            self._levelbar.add_css_class(zone)

    def _stop_meter(self) -> None:
        self._levelbar.set_value(0.0)
        self._set_level_zone(None)

    # ── Progress (the backend's estimate) ──

    def _reset_progress(self) -> None:
        if self._destroyed:
            return
        self._stop_pulse()
        self._progress.set_fraction(0)

    def set_progress(self, fraction: float | None) -> None:
        """The backend's estimate; ``None`` means none — pulse instead."""
        if self._destroyed or self._machine.state not in _WORKING:
            return
        if fraction is None:
            if self._pulse_source is None:
                self._start_pulse()
            return
        self._stop_pulse()
        self._progress.set_fraction(fraction)

    def set_stalled(self, stalled: bool) -> None:
        if self._destroyed or self._machine.state not in _WORKING:
            return
        if stalled != self._stalled:
            self._stalled = stalled
            self._status_label.set_text("Still working…" if stalled else "Working…")

    # ── Pulse (no prediction) ──

    def _start_pulse(self) -> None:
        self._stop_pulse()
        self._progress.pulse()
        self._pulse_source = GLib.timeout_add(_PULSE_MS, self._tick_pulse)

    def _stop_pulse(self) -> None:
        if self._pulse_source is not None:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = None

    def _tick_pulse(self) -> bool:
        self._progress.pulse()
        return True

    # ── Session directory ──

    def window_active(self) -> bool:
        return self._window.is_active()

    def close(self) -> None:
        """Close the window, as its own close button does."""
        self._window.close()

    def set_session_dir(self, path: str) -> None:
        """Store the session directory path for the folder button."""
        self._session_dir = path

    # ── Button handlers ──

    def _on_mode_switch(self, mode: str) -> None:
        if mode == "meeting" and self._on_switch_to_meeting is not None:
            self._on_switch_to_meeting()

    def _on_open_folder(self, _btn: Gtk.Button | None) -> None:
        if self._session_dir:
            Gio.AppInfo.launch_default_for_uri(
                GLib.filename_to_uri(self._session_dir, None), None
            )

    def has_text_selection(self) -> bool:
        """True when the user has selected part of the transcript.

        Ctrl+C is captured before the TextView sees it (the TextView holds
        focus and would otherwise swallow the accelerator and copy nothing).
        A deliberate selection is the one case where the TextView's own
        copy is the right answer, so the key is handed back to it.
        """
        return self._text_view.get_buffer().get_has_selection()

    def copy_to_clipboard(self) -> bool:
        """Copy the transcript to the clipboard. Returns True if text was copied.

        Placeholder, "No speech detected." and error text are not
        transcript, so there is nothing to copy while they are showing.
        """
        if not self._copyable:
            logger.debug("copy_to_clipboard: nothing copyable on screen")
            return False
        buf = self._text_view.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, False)
        if not text:
            logger.debug("copy_to_clipboard: no text to copy")
            return False
        logger.debug("copy_to_clipboard: text_len=%d", len(text))
        display = Gdk.Display.get_default()
        if not display:
            logger.debug("copy_to_clipboard: no display available")
            return False
        display.get_clipboard().set(text)
        toast = Adw.Toast(title="Copied")
        toast.set_timeout(2)
        self._toast_overlay.add_toast(toast)
        return True

    def _on_cancel(self, _btn: Gtk.Button) -> None:
        self.request_cancel()

    def _on_stop(self, _btn: Gtk.Button) -> None:
        s = self._machine.state
        logger.debug("_on_stop: state=%s", s.name)
        if s in (State.WARMING, State.RECORDING):
            self._on_stop_cb()

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
        self._timer_label.set_text(_format_timer(self._timer_seconds))
        return True

    # ── Auto-close countdown ──

    def _start_autoclose_countdown(self) -> None:
        """Start the countdown from the full duration (no-op if running)."""
        if self._autoclose_source is not None:
            return
        self._autoclose_remaining = self._autoclose_total
        self._update_autoclose_label()
        self._autoclose_source = GLib.timeout_add_seconds(1, self._tick_autoclose)

    def _stop_autoclose_countdown(self) -> None:
        if self._autoclose_source is not None:
            GLib.source_remove(self._autoclose_source)
            self._autoclose_source = None
        self._close_btn.set_label("Close")

    def _update_autoclose_label(self) -> None:
        self._close_btn.set_label(f"Close · {self._autoclose_remaining}s")

    def _tick_autoclose(self) -> bool:
        self._autoclose_remaining -= 1
        if self._autoclose_remaining <= 0:
            self._autoclose_source = None
            self._window.close()
            return False
        self._update_autoclose_label()
        return True

    # ── Cleanup ──

    def destroy(self) -> None:
        logger.debug("destroy: tearing down UI")
        self._destroyed = True
        self._close_cancel_dialog()
        self._stop_timer()
        self._stop_meter()
        self._stop_pulse()
        self._stop_autoclose_countdown()
