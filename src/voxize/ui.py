"""Voxize BREATH overlay — card with glow, status line, level dots, rail."""

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

from voxize.prompt import PromptSource  # noqa: E402
from voxize.state import State, StateMachine  # noqa: E402

_PHASE_CLASSES = ("phase-batch", "phase-cleanup", "phase-ready")
_CARD_STATE_CLASSES = (
    "warming-state",
    "recording-state",
    "working-state",
    "ready-state",
    "phase-cleanup",
    "speech",
    "silence",
)

_DOT_FILLED = "●"  # ●
_DOT_HOLLOW = "○"  # ○ — WARMING: outlined shapes read as "not armed"

_BATCH_LIVE_RATIO = 0.9843
_CLEANUP_BATCH_RATIO = 0.9167
_MIN_LIVE_LEN = 50

_UPLOAD_MS = 50.0
_BATCH_TTFB_BASE_MS = 1196.7
_BATCH_TTFB_SLOPE_MS = 1.3181
_BATCH_STREAM_MS_PER_CHAR = 1.1337
_CLEANUP_TTFB_MS = 1264.0
_CLEANUP_STREAM_MS_PER_CHAR = 1.4499
_WAIT_TICK_MS = 50

_LEVEL_DOT_COUNT = 7
# Zone boundaries as fractions of the [-60, 0] dBFS range — see _level_zone.
_ZONE_GOOD = 0.33
_ZONE_HOT = 0.80
_ZONE_CLIP = 0.95
_LEVEL_ZONE_CLASSES = ("zone-good", "zone-hot", "zone-clip")
# 19px serif at the design's 1.6 leading. The real per-line height is
# measured at runtime (fonts differ); this is only the target.
_TARGET_LINE_PX = 19 * 1.6
_MAX_LINES_LIVE = 6
_MAX_LINES_READY = 10


def _dbfs_to_fraction(dbfs: float) -> float:
    """Map dBFS [-60, 0] to fraction [0, 1] for the level dots."""
    return max(0.0, min(1.0, (dbfs + 60.0) / 60.0))


def _level_zone(fraction: float) -> str | None:
    """Classify a level fraction into a mic-gain zone.

    Thresholds are carried over unchanged from the pre-BREATH meter — on
    RMS dBFS they land at -40.2 / -12 / -3, which is a sane band for speech.
    Keeping the exact numbers matters more than tuning them: the meter is
    only useful as a gain check if what "green" means stays put.

    Returns None for the low zone, which is the stylesheet's default.
    """
    if fraction > _ZONE_CLIP:
        return "zone-clip"
    if fraction > _ZONE_HOT:
        return "zone-hot"
    if fraction > _ZONE_GOOD:
        return "zone-good"
    return None


class OverlayWindow:
    """Builds and manages the Voxize BREATH overlay UI widgets."""

    def __init__(
        self,
        window: Gtk.ApplicationWindow,
        machine: StateMachine,
        autoclose_seconds: int = 0,
        on_switch_to_meeting: Callable[[], None] | None = None,
    ) -> None:
        self._window = window
        self._machine = machine
        self._on_switch_to_meeting = on_switch_to_meeting
        self._timer_seconds = 0
        self._timer_source: int | None = None
        self._scroll_pending = False
        self._awaiting_cleanup = False
        self._awaiting_batch = False
        self._destroyed = False
        self._session_dir: str | None = None
        self._speech_active = False
        self._had_first_text = False
        self._meter_source: int | None = None
        self._level_meter = None
        self._pipeline_active = False
        self._pipeline_predicted: float = 0.0
        self._pipeline_chars: int = 0
        self._pipeline_batch_predicted: float = 0.0
        self._seg_upload: float = 0.0
        self._seg_batch_wait: float = 0.0
        self._seg_batch_stream: float = 0.0
        self._seg_cleanup_wait: float = 0.0
        self._seg_cleanup_stream: float = 0.0
        self._batch_ttfb_ms: float = 0.0
        self._cleanup_ttfb_ms: float = 0.0
        self._d_upload: float = 0.0
        self._d_batch_ttfb: float = 0.0
        self._d_batch_stream: float = 0.0
        self._d_cleanup_ttfb: float = 0.0
        self._d_cleanup_stream: float = 0.0
        self._pl_start_us: int = 0
        self._pl_upload_done_us: int = 0
        self._pl_batch_first_us: int = 0
        self._pl_batch_done_us: int = 0
        self._pl_cleanup_first_us: int = 0
        self._wait_source: int | None = None
        self._wait_start_us: int = 0
        self._wait_from: float = 0.0
        self._wait_to: float = 0.0
        self._wait_ms: int = 0
        self._autoclose_total = autoclose_seconds
        self._autoclose_remaining = 0
        self._autoclose_source: int | None = None
        self._autoclose_paused = False
        self._rail_fraction: float = 0.0
        self._resize_pending = False
        self._text_height = 0
        self._height_floor = 0
        self._line_height = 0
        self._max_lines = _MAX_LINES_LIVE
        self._prompt_sources: list[PromptSource] = []
        self._build()
        machine.on_change(self._on_state_change)

    # ── Construction ──

    def _build(self) -> None:
        # Marks this window as the transparent dictation overlay. style.css is
        # shared with the meeting recorder, whose windows are ordinary opaque
        # ones — scoping the "kill every decoration" rules to this class keeps
        # them from stripping the meeting windows' background too.
        self._window.add_css_class("overlay-window")

        # Invisible HeaderBar — gives us drag-to-move for free on Wayland
        titlebar = Gtk.HeaderBar()
        titlebar.set_show_title_buttons(False)
        titlebar.set_title_widget(Gtk.Box())
        titlebar.set_visible(False)
        self._window.set_titlebar(titlebar)

        # ── Outer shell: RGBA window with glow bleed ──

        # Card: the visible surface. The 28px margin is the bleed zone —
        # GTK4 paints box-shadow outside the widget allocation, so the
        # halo lands in that transparent margin (verified, not assumed).
        self._card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._card.add_css_class("card")
        self._card.set_hexpand(True)
        self._card.set_margin_top(28)
        self._card.set_margin_bottom(28)
        self._card.set_margin_start(28)
        self._card.set_margin_end(28)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._card)
        self._window.set_child(self._toast_overlay)

        self._window.connect("notify::is-active", self._on_active_changed)

        # Pause auto-close on pointer entry
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_pointer_enter)
        self._window.add_controller(motion)

        # ── Status line ──

        status_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        status_line.set_margin_top(17)
        status_line.set_margin_bottom(0)
        status_line.set_margin_start(20)
        status_line.set_margin_end(20)

        self._dot = Gtk.Label(label="●")
        self._dot.add_css_class("status-dot")
        status_line.append(self._dot)

        self._status_label = Gtk.Label(label="Initializing")
        self._status_label.add_css_class("status-label")
        status_line.append(self._status_label)

        self._timer_label = Gtk.Label()
        self._timer_label.add_css_class("timer-label")
        self._timer_label.set_margin_start(8)
        self._timer_label.set_visible(False)
        status_line.append(self._timer_label)

        if self._on_switch_to_meeting is not None:
            sep = Gtk.Label(label="·")
            sep.add_css_class("timer-label")
            sep.set_margin_start(4)
            status_line.append(sep)
            mode_btn = Gtk.Button(label="Meeting")
            mode_btn.add_css_class("flat")
            mode_btn.add_css_class("mode-word")
            mode_btn.set_focusable(False)
            mode_btn.connect("clicked", lambda _: self._on_mode_switch("meeting"))
            status_line.append(mode_btn)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        status_line.append(spacer)

        self._guidance_btn = Gtk.Button.new_from_icon_name("view-list-symbolic")
        self._guidance_btn.set_tooltip_text("Vocabulary guidance")
        self._guidance_btn.add_css_class("flat")
        self._guidance_btn.add_css_class("status-icon")
        self._guidance_btn.set_focusable(False)
        self._guidance_btn.set_visible(False)
        self._guidance_btn.connect("clicked", self._on_guidance_clicked)
        status_line.append(self._guidance_btn)

        self._folder_btn = Gtk.Button.new_from_icon_name("folder-symbolic")
        self._folder_btn.set_tooltip_text("Open session folder (Ctrl+O)")
        self._folder_btn.add_css_class("flat")
        self._folder_btn.add_css_class("status-icon")
        self._folder_btn.set_focusable(False)
        self._folder_btn.connect("clicked", self._on_open_folder)
        status_line.append(self._folder_btn)

        close_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_btn.add_css_class("flat")
        close_btn.add_css_class("status-icon")
        close_btn.set_focusable(False)
        close_btn.connect("clicked", lambda _: self._window.close())
        status_line.append(close_btn)

        # The status line doubles as the drag handle. The HeaderBar set above
        # is invisible and zero-height, so it offers no grab area of its own —
        # before the redesign it *was* this row, which is where drag-to-move
        # used to come from. Buttons inside still get their clicks.
        status_handle = Gtk.WindowHandle()
        status_handle.set_child(status_line)
        self._card.append(status_handle)

        # ── Text area ──

        self._text_view = Gtk.TextView()
        self._text_view.set_editable(False)
        self._text_view.set_cursor_visible(False)
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._text_view.add_css_class("text-area")
        self._text_view.set_top_margin(14)
        self._text_view.set_bottom_margin(16)
        self._text_view.set_left_margin(22)
        self._text_view.set_right_margin(22)

        buf = self._text_view.get_buffer()
        self._end_mark = buf.create_mark("end", buf.get_end_iter(), False)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_child(self._text_view)
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_min_content_height(int(_TARGET_LINE_PX))
        self._scroll.set_max_content_height(int(_MAX_LINES_LIVE * _TARGET_LINE_PX))
        # Relayout (including the first real allocation) changes how the
        # transcript wraps, so re-measure whenever the scroller reports
        # new geometry. Converges: an unchanged height triggers no work.
        self._scroll.get_vadjustment().connect(
            "changed", lambda _a: self._queue_text_resize()
        )
        # Non-overlay scrolling keeps the 3px thumb permanently visible —
        # per the design, position-in-the-text is the only overflow
        # affordance, so it must never fade out.
        self._scroll.set_overlay_scrolling(False)

        self._overlay = self._scroll
        self._overlay.set_visible(False)
        self._card.append(self._scroll)

        # ── Error banner (Gtk.Revealer) ──

        self._error_revealer = Gtk.Revealer()
        self._error_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._error_revealer.set_reveal_child(False)

        error_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        error_box.add_css_class("error-banner")

        error_dot = Gtk.Label(label="●")
        error_dot.add_css_class("error-dot")
        error_box.append(error_dot)

        self._error_label = Gtk.Label()
        self._error_label.add_css_class("error-message")
        self._error_label.set_wrap(True)
        self._error_label.set_hexpand(True)
        self._error_label.set_xalign(0)
        error_box.append(self._error_label)

        self._error_revealer.set_child(error_box)
        self._card.append(self._error_revealer)

        self._degraded = False

        # ── Pipeline rail ──

        rail_overlay = Gtk.Overlay()
        rail_overlay.add_css_class("rail")
        # The rail exists only in the states that have one (working, ready).
        # Leaving it in the tree while empty would hold open its 2px plus the
        # 12px gap it carries for the pill, which the design does not have
        # under the transcript while recording.
        rail_overlay.set_visible(False)
        self._rail_overlay = rail_overlay

        self._upload_bar = Gtk.ProgressBar()
        self._upload_bar.add_css_class("osd")
        self._upload_bar.add_css_class("rail-journey")
        self._upload_bar.set_visible(False)
        rail_overlay.set_child(self._upload_bar)

        self._rail_behind = Gtk.ProgressBar()
        self._rail_behind.add_css_class("osd")
        self._rail_behind.add_css_class("rail-behind")
        self._rail_behind.set_visible(False)
        rail_overlay.add_overlay(self._rail_behind)

        self._card.append(rail_overlay)

        # ── Action pill ──

        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pill.add_css_class("action-pill")

        self._level_dots_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=0
        )
        self._level_dots_box.add_css_class("level-dots")
        self._level_dots_box.set_valign(Gtk.Align.CENTER)
        self._level_dots: list[Gtk.Box] = []
        for _ in range(_LEVEL_DOT_COUNT):
            dot = Gtk.Box()
            dot.add_css_class("level-dot")
            self._level_dots_box.append(dot)
            self._level_dots.append(dot)
        self._level_dots_box.set_visible(False)
        pill.append(self._level_dots_box)

        pill_spacer = Gtk.Box()
        pill_spacer.set_hexpand(True)
        pill.append(pill_spacer)

        self._cancel_btn = Gtk.Button(label="Cancel")
        self._cancel_btn.add_css_class("flat")
        self._cancel_btn.add_css_class("cancel-btn")
        self._cancel_btn.connect("clicked", self._on_cancel)
        self._cancel_btn.set_visible(False)
        pill.append(self._cancel_btn)

        self._action_btn = Gtk.Button()
        self._action_btn_label = Gtk.Label(label="Stop", use_markup=True)
        self._action_btn.set_child(self._action_btn_label)
        self._action_btn.add_css_class("primary-action")
        self._action_btn.connect("clicked", self._on_action)
        self._action_btn.set_visible(False)
        pill.append(self._action_btn)

        self._copy_btn = Gtk.Button(label="Copy")
        self._copy_btn.add_css_class("primary-action")
        self._copy_btn.connect("clicked", self._on_copy)
        self._copy_btn.set_visible(False)
        pill.append(self._copy_btn)

        self._card.append(pill)
        self._pill = pill

        # ── Guidance popover ──

        self._guidance_popover = Gtk.Popover()
        self._guidance_popover.set_parent(self._guidance_btn)
        self._guidance_popover.add_css_class("guidance-popover")
        self._guidance_popover_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=11
        )
        self._guidance_popover.set_child(self._guidance_popover_box)

        self._prime_initial_size()

    def _prime_initial_size(self) -> None:
        """Lay INITIALIZING out like WARMING so the mapped size is honest.

        ``app.py`` calls ``present()`` while the machine is still
        INITIALIZING, and the compositor centres whatever size it sees then —
        on Wayland that is the only chance to be centred, because the window
        cannot reposition itself afterwards. Left as built, this state hid
        the transcript, the level dots and every button, so the window mapped
        as a 131px stub and then jumped to 230px the moment the mic came up,
        landing the card ~49px below centre before the user had said a word.

        Matching the warming layout is deliberate rather than calling
        ``set_default_size`` with a constant: the height depends on the
        measured serif line height, so a hardcoded number would silently rot
        the day the font changes.

        Stop stays hidden — it would do nothing until there is audio — and
        the pill takes its height from Cancel, which is the same height, so
        hiding it costs no pixels. Cancel is live here (see ``_on_cancel``).
        """
        self._card.add_css_class("warming-state")
        self._set_glow("glow-warming")
        self._dot.set_text(_DOT_HOLLOW)
        self._show_waiting_placeholder()
        self._level_dots_box.set_visible(True)
        self._cancel_btn.set_visible(True)

    _GLOW_CLASSES = (
        "glow-silence",
        "glow-speech",
        "glow-ready",
        "glow-warming",
        "glow-working",
        "glow-working-cleanup",
    )

    def _rail_set(self, fraction: float) -> None:
        """Drive the journey bar, monotonically.

        Predictions over- and under-shoot, so a raw fraction can move
        backwards between the model and a real event. The rail is a
        promise about elapsed progress; it must never retreat.
        """
        f = max(self._rail_fraction, min(fraction, 1.0))
        self._rail_fraction = f
        self._upload_bar.set_fraction(f)

    def _set_glow(self, cls: str) -> None:
        """Set the halo state class on the card, removing all others.

        The halo is a box-shadow on .card bleeding into its 28px margin;
        each class picks a hue and whether it breathes or holds.
        """
        for c in self._GLOW_CLASSES:
            if c != cls:
                self._card.remove_css_class(c)
        self._card.add_css_class(cls)

    def setup_max_height(self) -> None:
        """Line caps are fixed (6 live / 10 ready), not screen-derived."""
        self._queue_text_resize()

    # ── Guidance popover ──

    def show_prompt_context(self, prompts: list[PromptSource]) -> None:
        """Populate the guidance popover and show the icon."""
        if self._destroyed:
            return
        logger.debug("show_prompt_context: sources=%d", len(prompts))
        self._prompt_sources = prompts
        if not prompts:
            self._guidance_btn.set_visible(False)
            return
        # Rebuild popover content
        box = self._guidance_popover_box
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

        title = Gtk.Label(label="VOCABULARY IN USE")
        title.add_css_class("guidance-title")
        title.set_xalign(0)
        box.append(title)

        for source in prompts:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            name = os.path.basename(source.path)
            name_btn = Gtk.Button()
            name_btn.add_css_class("guidance-file")
            # START, not fill: the underline is a border on the label, so a
            # stretched label would rule the full popover width.
            name_btn.set_halign(Gtk.Align.START)
            name_label = Gtk.Label()
            name_label.add_css_class("guidance-filename")
            name_label.set_text(name)
            name_label.set_xalign(0)
            name_btn.set_child(name_label)
            file_path = source.path
            name_btn.connect(
                "clicked",
                lambda _, p=file_path: Gio.AppInfo.launch_default_for_uri(
                    GLib.filename_to_uri(p, None), None
                ),
            )
            row.append(name_btn)

            preview = Gtk.Label()
            preview.add_css_class("guidance-preview")
            preview.set_text(
                source.content[:80] + ("…" if len(source.content) > 80 else "")
            )
            preview.set_xalign(0)
            preview.set_wrap(True)
            preview.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            preview.set_max_width_chars(40)
            row.append(preview)

            box.append(row)

        self._guidance_btn.set_visible(True)

    def _on_guidance_clicked(self, _btn: Gtk.Button) -> None:
        self._guidance_popover.popup()

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

    def on_speech(self, active: bool) -> None:
        """VAD speech_started/speech_stopped — drive halo and dot."""
        if self._destroyed or self._machine.state != State.RECORDING:
            return
        logger.debug("on_speech: active=%s", active)
        self._speech_active = active
        # The dot's colour comes from the card's .speech / .silence class
        # (bright ember vs deep ember). The design gives the whole reactive
        # animation budget to the halo, so the dot itself never blinks.
        if active:
            self._card.remove_css_class("silence")
            self._card.add_css_class("speech")
            self._set_glow("glow-speech")
        else:
            self._card.remove_css_class("speech")
            self._card.add_css_class("silence")
            self._set_glow("glow-silence")

    def append_text(self, text: str) -> None:
        if self._destroyed:
            return
        if not self._had_first_text:
            logger.debug("append_text: first text arrived, len=%d", len(text))
            self._had_first_text = True
            # Drop the "waiting for audio" placeholder before the real words
            self._text_view.remove_css_class("waiting")
            self.clear_text()
        if self._awaiting_batch and self._machine.state == State.TRANSCRIBING:
            logger.debug("append_text: first batch delta, swapping live preview")
            self._awaiting_batch = False
            self._stop_text_pulse()
            self._text_view.remove_css_class("live-text")
            self.clear_text()
            self._snap_to_batch_stream()
        if self._awaiting_cleanup and self._machine.state == State.CLEANING:
            logger.debug("append_text: first cleanup token, swapping transcript")
            self._awaiting_cleanup = False
            self._stop_text_pulse()
            self.clear_text()
            self._snap_to_cleanup_stream()
            self._card.add_css_class("phase-cleanup")
            self._set_glow("glow-working-cleanup")
        self._overlay.set_visible(True)
        buf = self._text_view.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        self._queue_text_resize()
        if not self._scroll_pending:
            self._scroll_pending = True
            GLib.idle_add(self._scroll_to_end)

    def clear_text(self) -> None:
        self._text_view.get_buffer().set_text("")
        self._queue_text_resize()

    # ── Text area sizing ──

    def _queue_text_resize(self) -> None:
        """Re-measure the transcript after the next layout pass."""
        if self._resize_pending:
            return
        self._resize_pending = True
        GLib.idle_add(self._resize_text_area)

    def _apply_line_spacing(self) -> bool:
        """Pad lines out to the 1.6 leading the design asks for.

        Returns True if the spacing was just applied, meaning any height
        measured before this call is stale.

        GTK4 CSS has no ``line-height``, so the leading has to come from
        the TextView's own spacing properties. The base height is measured
        rather than assumed because the serif may be the packaged Source
        Serif 4 or any fallback, and those differ by several pixels — a
        hardcoded constant would silently make the six-line cap a lie.
        """
        if self._line_height:
            return False
        layout = self._text_view.create_pango_layout("Ag")
        base = layout.get_pixel_extents()[1].height
        if base <= 0:
            return False
        extra = max(0, round(_TARGET_LINE_PX) - base)
        self._text_view.set_pixels_inside_wrap(extra)
        self._text_view.set_pixels_below_lines(extra)
        self._line_height = base + extra
        logger.debug(
            "line spacing: base=%dpx extra=%dpx line_height=%dpx",
            base,
            extra,
            self._line_height,
        )
        return True

    def _resize_text_area(self) -> bool:
        """Grow the card to fit the transcript, up to the line cap.

        ``propagate_natural_height`` cannot help here: a wrapping TextView
        has no natural height until it knows its width, so the scroller
        would settle at roughly two lines regardless of content. Measuring
        the laid-out end iter gives the real wrapped height.
        """
        self._resize_pending = False
        if self._destroyed:
            return False
        if self._text_view.get_width() <= 1:
            # Not allocated yet; measuring now would read an unwrapped
            # layout and settle the card at one or two lines.
            self._queue_text_resize()
            return False
        if self._apply_line_spacing():
            # Spacing changed the layout under us — measure on the next pass.
            self._queue_text_resize()
            return False
        line = self._line_height or _TARGET_LINE_PX
        # Ask the widget itself rather than deriving from the end iter:
        # the iter rect stops at the last glyph and omits the trailing
        # line spacing, which left the viewport a few pixels short and
        # produced a scrollbar for text that fits.
        content = self._text_view.measure(
            Gtk.Orientation.VERTICAL, self._text_view.get_width()
        )[1]
        cap = self._max_lines * line + self._text_view.get_top_margin()
        # Round the content up: truncating leaves the viewport a pixel
        # short of its own text and GTK answers with a scrollbar for
        # content that visibly fits.
        height = int(max(line, min(math.ceil(content), cap)))
        # Ratchet: the text area may grow but never shrink. Each handoff
        # (live -> batch -> cleanup) restarts the transcript from empty, so
        # without this the card collapses to one line and re-grows three
        # times per session, tearing the window on every pass. Holding the
        # high-water mark costs at most some empty space when a later pass
        # is genuinely shorter, which is far cheaper than the thrash.
        # The cap only ever rises (6 lines live -> 10 at ready), so the
        # floor can never push the card past the current line limit.
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
        """One dim italic line standing in for the words to come."""
        self._had_first_text = False
        self._text_view.get_buffer().set_text("waiting for audio")
        self._queue_text_resize()
        self._text_view.add_css_class("waiting")
        self._overlay.set_visible(True)

    def show_transcript_for_cleanup(self, transcript: str) -> None:
        """Show the batch transcript and arm the cleanup swap."""
        if self._destroyed:
            return
        logger.debug("show_transcript_for_cleanup: transcript_len=%d", len(transcript))
        # Merged visual state: keep "Working…"
        self._overlay.set_visible(True)
        self._text_view.get_buffer().set_text(transcript)
        self._queue_text_resize()
        if not self._scroll_pending:
            self._scroll_pending = True
            GLib.idle_add(self._scroll_to_end)
        self._awaiting_cleanup = True
        self._start_text_pulse()

    def show_error_banner(self, message: str) -> None:
        """Show error banner via Gtk.Revealer without clearing transcript."""
        if self._destroyed:
            return
        logger.debug("show_error_banner: message=%s", message)
        self._error_label.set_text(message)
        self._error_revealer.set_reveal_child(True)
        self._stop_timer()
        self._degraded = True
        self._cancel_btn.set_visible(False)
        self._action_btn.set_visible(True)
        self._window.set_default_widget(self._action_btn)

    def _scroll_to_end(self) -> bool:
        self._scroll_pending = False
        vadj = self._scroll.get_vadjustment()
        vadj.set_value(vadj.get_upper() - vadj.get_page_size())
        return False

    # ── State change handler ──

    def _on_state_change(self, machine: StateMachine, old: State, new: State) -> None:
        logger.debug("_on_state_change: %s -> %s", old.name, new.name)

        # Update dot color class
        for cls in (
            "initializing",
            "warming",
            "recording",
            "transcribing",
            "cleaning",
            "ready",
            "error",
            "cancelled",
        ):
            self._dot.remove_css_class(cls)
        self._dot.add_css_class(new.name.lower())

        # Clear card state classes (halo classes are re-set per state below)
        for cls in _CARD_STATE_CLASSES:
            self._card.remove_css_class(cls)

        self._stop_text_pulse()
        self._stop_autoclose_countdown()
        self._awaiting_batch = False
        self._awaiting_cleanup = False
        self._error_revealer.set_reveal_child(False)
        self._timer_label.remove_css_class("timer-dim")
        # READY flattens the action button; every other state fills it.
        self._action_btn.remove_css_class("cancel-btn")
        self._action_btn.add_css_class("primary-action")

        audio_phase = {State.WARMING, State.RECORDING}
        if old in audio_phase and new not in audio_phase:
            self._stop_meter()
            self._stop_timer()
            self._text_view.remove_css_class("live-text")

        if new == State.WARMING:
            self._card.add_css_class("warming-state")
            self._set_glow("glow-warming")
            self._dot.set_text(_DOT_HOLLOW)
            self._status_label.set_text("Warming up…")
            self._speech_active = False
            self._had_first_text = False
            self._timer_seconds = 0
            self._timer_label.set_text("00:00")
            self._timer_label.set_visible(True)
            self._timer_label.add_css_class("timer-dim")
            self._copy_btn.set_visible(False)
            self._cancel_btn.set_visible(True)
            self._action_btn_label.set_label("Stop")
            self._action_btn.set_visible(True)
            self._window.set_default_widget(self._action_btn)
            # No spinner — one italic line where the words will go says
            # "not armed yet" without a sentence to read.
            self._show_waiting_placeholder()
            self._level_dots_box.set_visible(True)
            self._start_meter()
            self._max_lines = _MAX_LINES_LIVE
            self._queue_text_resize()

        elif new == State.RECORDING:
            self._card.add_css_class("recording-state")
            self._card.add_css_class("silence")
            self._set_glow("glow-silence")
            self._dot.set_text(_DOT_FILLED)
            self._status_label.set_text("Listening")
            if not self._had_first_text:
                self._show_waiting_placeholder()
            if old != State.WARMING:
                self._speech_active = False
                self._timer_seconds = 0
                self._timer_label.set_text("00:00")
                self._start_meter()
            self._timer_label.set_visible(True)
            self._timer_label.remove_css_class("timer-dim")
            self._copy_btn.set_visible(False)
            self._cancel_btn.set_visible(True)
            self._action_btn_label.set_label("Stop")
            self._action_btn.set_visible(True)
            self._window.set_default_widget(self._action_btn)
            self._level_dots_box.set_visible(True)
            self._text_view.add_css_class("live-text")
            self._start_timer()

        elif new == State.TRANSCRIBING:
            self._card.add_css_class("working-state")
            self._set_glow("glow-working")
            self._dot.set_text(_DOT_FILLED)
            self._status_label.set_text("Working…")
            self._timer_label.set_visible(True)
            self._timer_label.add_css_class("timer-dim")
            self._stop_timer()
            self._copy_btn.set_visible(False)
            self._cancel_btn.set_visible(True)
            self._action_btn.set_visible(False)
            self._level_dots_box.set_visible(False)
            self._window.set_default_widget(None)
            self._awaiting_batch = True
            self._had_first_text = False
            self._text_view.add_css_class("text-outgoing")
            self._start_text_pulse()

        elif new == State.CLEANING:
            self._card.add_css_class("working-state")
            # Keep "Working…" — merged visual state
            self._timer_label.set_visible(True)
            self._copy_btn.set_visible(False)
            self._cancel_btn.set_visible(True)
            self._action_btn.set_visible(False)
            self._level_dots_box.set_visible(False)
            self._window.set_default_widget(None)

        elif new == State.READY:
            self._card.add_css_class("ready-state")
            self._set_glow("glow-ready")
            self._dot.set_text(_DOT_FILLED)
            self.hide_upload_progress()
            self.show_ready_rail()
            self._status_label.set_text("Ready")
            self._timer_label.set_visible(True)
            self._timer_label.add_css_class("timer-dim")
            self._copy_btn.set_visible(True)
            self._cancel_btn.set_visible(False)
            self._action_btn_label.set_label("Close")
            # Copy is the only filled control at ready, so Close drops back
            # to flat text — three ranked exits, and Copy never doubles as
            # dismiss by looking like the same kind of button.
            self._action_btn.remove_css_class("primary-action")
            self._action_btn.add_css_class("cancel-btn")
            self._action_btn.set_visible(True)
            self._level_dots_box.set_visible(False)
            self._window.set_default_widget(self._copy_btn)
            # At ready the text is a finished artefact being proof-read
            # before paste; forcing a scroll through a 9-line result is
            # hostile, so the cap rises.
            self._max_lines = _MAX_LINES_READY
            self._queue_text_resize()
            if self._autoclose_total > 0 and self._window.is_active():
                self._start_autoclose_countdown()

        elif new == State.CANCELLED:
            self.hide_upload_progress()
            self._stop_timer()
            GLib.idle_add(self._window.close)

        elif new == State.ERROR:
            self.hide_upload_progress()
            self._status_label.set_text("Error")
            self._timer_label.set_visible(False)
            self._stop_timer()
            self._cancel_btn.set_visible(False)
            self._action_btn_label.set_label("Close")
            self._action_btn.set_visible(True)
            self._level_dots_box.set_visible(False)
            self._window.set_default_widget(self._action_btn)
            self.show_error_banner(machine.error_message)

    def _on_active_changed(self, window, _pspec) -> None:
        active = window.is_active()
        logger.debug("_on_active_changed: active=%s", active)
        if active:
            self._text_view.remove_css_class("backdrop")
            from voxize import clipboard

            clipboard.flush()
        else:
            self._text_view.add_css_class("backdrop")
        if self._machine.state == State.READY and self._autoclose_total > 0:
            if active and not self._autoclose_paused:
                self._start_autoclose_countdown()
            elif not active:
                self._pause_autoclose_countdown()

    def _on_pointer_enter(self, _ctrl, _x, _y) -> None:
        """Pointer entered the window — permanently pause auto-close."""
        if self._autoclose_source is not None:
            self._pause_autoclose_countdown()

    # ── Level dots (segmented meter) ──

    def set_level_meter(self, meter) -> None:
        """Store a reference to the LevelMeter for polling."""
        self._level_meter = meter

    def _start_meter(self) -> None:
        self._stop_meter()
        if self._level_meter is None:
            return
        self._level_dots_box.set_visible(True)
        self._meter_source = GLib.timeout_add(40, self._tick_meter)

    def _set_level_zone(self, zone: str | None) -> None:
        """Swap the mic-gain zone class on the dots container."""
        for cls in _LEVEL_ZONE_CLASSES:
            if cls != zone:
                self._level_dots_box.remove_css_class(cls)
        if zone is not None and not self._level_dots_box.has_css_class(zone):
            self._level_dots_box.add_css_class(zone)

    def _stop_meter(self) -> None:
        if self._meter_source is not None:
            GLib.source_remove(self._meter_source)
            self._meter_source = None
        self._set_level_zone(None)
        for dot in self._level_dots:
            dot.remove_css_class("lit")
            dot.remove_css_class("leading")
            dot.remove_css_class("silence-shimmer")

    def _tick_meter(self) -> bool:
        meter = self._level_meter
        if meter is None:
            return True
        frac = _dbfs_to_fraction(meter.level_dbfs)
        self._set_level_zone(None if not self._speech_active else _level_zone(frac))

        if not self._speech_active:
            # Silence: middle-three shimmer, no extent. No zone either — a
            # gain reading taken while nobody is talking is noise-floor, and
            # colouring it would report the room, not the mic.
            for i, dot in enumerate(self._level_dots):
                dot.remove_css_class("lit")
                dot.remove_css_class("leading")
                if 2 <= i <= 4:
                    if not dot.has_css_class("silence-shimmer"):
                        dot.add_css_class("silence-shimmer")
                else:
                    dot.remove_css_class("silence-shimmer")
            return True

        # Speech: segmented fill
        leading_idx = -1
        for i, dot in enumerate(self._level_dots):
            dot.remove_css_class("silence-shimmer")
            threshold = (i + 1) / _LEVEL_DOT_COUNT
            if frac >= threshold:
                if not dot.has_css_class("lit"):
                    dot.add_css_class("lit")
                leading_idx = i
            else:
                dot.remove_css_class("lit")
            dot.remove_css_class("leading")

        if leading_idx >= 0:
            self._level_dots[leading_idx].add_css_class("leading")

        return True

    # ── Pipeline progress (upload → batch → cleanup) ──

    def start_pipeline(self, live_len: int) -> None:
        """Arm the unified progress bar for the full post-recording pipeline."""
        if self._destroyed:
            return
        self._pipeline_active = live_len >= _MIN_LIVE_LEN
        self._pipeline_predicted = (
            live_len * _BATCH_LIVE_RATIO if self._pipeline_active else 0.0
        )
        self._pipeline_batch_predicted = 0.0
        self._pipeline_chars = 0
        if not self._pipeline_active:
            return
        d_upload = _UPLOAD_MS + 0.05 * live_len
        d_batch_ttfb = _BATCH_TTFB_BASE_MS + _BATCH_TTFB_SLOPE_MS * live_len
        d_batch_stream = _BATCH_STREAM_MS_PER_CHAR * live_len
        d_cleanup_ttfb = _CLEANUP_TTFB_MS
        d_cleanup_stream = _CLEANUP_STREAM_MS_PER_CHAR * live_len
        total = (
            d_upload + d_batch_ttfb + d_batch_stream + d_cleanup_ttfb + d_cleanup_stream
        )
        self._seg_upload = d_upload / total
        self._seg_batch_wait = self._seg_upload + d_batch_ttfb / total
        self._seg_batch_stream = self._seg_batch_wait + d_batch_stream / total
        self._seg_cleanup_wait = self._seg_batch_stream + d_cleanup_ttfb / total
        self._seg_cleanup_stream = min(
            self._seg_cleanup_wait + d_cleanup_stream / total, 0.95
        )
        self._batch_ttfb_ms = d_batch_ttfb
        self._cleanup_ttfb_ms = d_cleanup_ttfb
        self._d_upload = d_upload
        self._d_batch_ttfb = d_batch_ttfb
        self._d_batch_stream = d_batch_stream
        self._d_cleanup_ttfb = d_cleanup_ttfb
        self._d_cleanup_stream = d_cleanup_stream
        self._pl_start_us = GLib.get_monotonic_time()
        self._pl_upload_done_us = 0
        self._pl_batch_first_us = 0
        self._pl_batch_done_us = 0
        self._pl_cleanup_first_us = 0
        logger.debug(
            "pipeline_progress: start live_len=%d "
            "predicted_ms=[upload=%.0f batch_ttfb=%.0f batch_stream=%.0f "
            "cleanup_ttfb=%.0f cleanup_stream=%.0f total=%.0f] "
            "segs=[%.3f %.3f %.3f %.3f %.3f] "
            "constants=[bl_ratio=%.4f cb_ratio=%.4f bt_base=%.1f bt_slope=%.4f "
            "bs_rate=%.4f ct_flat=%.1f cs_rate=%.4f]",
            live_len,
            d_upload,
            d_batch_ttfb,
            d_batch_stream,
            d_cleanup_ttfb,
            d_cleanup_stream,
            total,
            self._seg_upload,
            self._seg_batch_wait,
            self._seg_batch_stream,
            self._seg_cleanup_wait,
            self._seg_cleanup_stream,
            _BATCH_LIVE_RATIO,
            _CLEANUP_BATCH_RATIO,
            _BATCH_TTFB_BASE_MS,
            _BATCH_TTFB_SLOPE_MS,
            _BATCH_STREAM_MS_PER_CHAR,
            _CLEANUP_TTFB_MS,
            _CLEANUP_STREAM_MS_PER_CHAR,
        )
        self._rail_fraction = 0.0
        self._upload_bar.set_fraction(0)
        self._upload_bar.set_visible(True)
        self._rail_overlay.set_visible(True)
        self._rail_behind.set_fraction(0)
        self._rail_behind.set_visible(False)

    def show_ready_rail(self) -> None:
        """Flat sage rail across the full width at READY.

        The design keeps a rail on the card at ready — a still, unlit sage
        tint rather than the glowing journey bar, since nothing on the card
        animates once the transcript is final.
        """
        if self._destroyed:
            return
        self._stop_wait()
        self._rail_fraction = 1.0
        for cls in _PHASE_CLASSES:
            self._upload_bar.remove_css_class(cls)
        self._upload_bar.add_css_class("phase-ready")
        self._upload_bar.set_fraction(1.0)
        self._upload_bar.set_visible(True)
        self._rail_behind.set_visible(False)
        self._rail_overlay.set_visible(True)

    def set_upload_progress(self, fraction: float) -> None:
        """Upload byte progress [0, 1] → mapped to [0, seg_upload] of pipeline."""
        if self._destroyed:
            return
        if self._pipeline_active:
            frac = min(fraction, 1.0) * self._seg_upload
            self._rail_set(frac)
            if fraction >= 1.0:
                now = GLib.get_monotonic_time()
                self._pl_upload_done_us = now
                actual_ms = (now - self._pl_start_us) / 1000
                logger.debug(
                    "pipeline_progress: upload_done "
                    "actual_ms=%.0f predicted_ms=%.0f",
                    actual_ms,
                    self._d_upload,
                )
                self._upload_bar.add_css_class("phase-batch")
                self._start_wait(
                    self._seg_upload, self._seg_batch_wait, self._batch_ttfb_ms
                )
        else:
            self._upload_bar.set_visible(True)
            self._rail_set(min(1.0, fraction))

    def on_batch_complete(self, batch_len: int) -> None:
        """Batch done — snap to segment boundary, arm cleanup, start wait."""
        if self._destroyed or not self._pipeline_active:
            return
        self._stop_wait()
        self._rail_set(self._seg_batch_stream)
        self._rail_behind.set_fraction(self._seg_batch_stream)
        self._rail_behind.set_visible(True)
        now = GLib.get_monotonic_time()
        self._pl_batch_done_us = now
        ref = self._pl_batch_first_us or self._pl_start_us
        actual_ms = (now - ref) / 1000
        logger.debug(
            "pipeline_progress: batch_done "
            "actual_stream_ms=%.0f predicted_stream_ms=%.0f "
            "chars=%d predicted_chars=%.0f batch_len=%d",
            actual_ms,
            self._d_batch_stream,
            self._pipeline_chars,
            self._pipeline_predicted,
            batch_len,
        )
        self._pipeline_batch_predicted = (
            batch_len * _CLEANUP_BATCH_RATIO if batch_len >= _MIN_LIVE_LEN else 0.0
        )
        self._pipeline_chars = 0
        self._upload_bar.remove_css_class("phase-batch")
        self._upload_bar.add_css_class("phase-cleanup")
        if self._pipeline_batch_predicted > 0:
            self._start_wait(
                self._seg_batch_stream, self._seg_cleanup_wait, self._cleanup_ttfb_ms
            )

    def on_pipeline_delta(self, text: str) -> None:
        """Update pipeline progress with a streaming delta (batch or cleanup)."""
        if self._destroyed or not self._pipeline_active:
            return
        self._pipeline_chars += len(text)
        state = self._machine.state
        if state == State.TRANSCRIBING and self._pipeline_predicted > 0:
            t = min(self._pipeline_chars / self._pipeline_predicted, 1.0)
            seg_start = self._seg_batch_wait
            seg_end = self._seg_batch_stream
        elif state == State.CLEANING and self._pipeline_batch_predicted > 0:
            t = min(self._pipeline_chars / self._pipeline_batch_predicted, 1.0)
            seg_start = self._seg_cleanup_wait
            seg_end = self._seg_cleanup_stream
        else:
            return
        frac = seg_start + t * (seg_end - seg_start)
        self._rail_set(min(frac, seg_end))

    def finish_pipeline(self) -> None:
        """Snap to 100%."""
        if self._destroyed or not self._pipeline_active:
            return
        self._stop_wait()
        self._rail_set(1.0)
        self._upload_bar.remove_css_class("phase-cleanup")
        self._upload_bar.add_css_class("phase-ready")
        now = GLib.get_monotonic_time()
        ref = self._pl_cleanup_first_us or self._pl_start_us
        actual_stream_ms = (now - ref) / 1000
        total_ms = (now - self._pl_start_us) / 1000
        logger.debug(
            "pipeline_progress: done "
            "cleanup_stream_ms=%.0f predicted_cleanup_stream_ms=%.0f "
            "cleanup_chars=%d predicted_cleanup_chars=%.0f "
            "total_ms=%.0f predicted_total_ms=%.0f",
            actual_stream_ms,
            self._d_cleanup_stream,
            self._pipeline_chars,
            self._pipeline_batch_predicted,
            total_ms,
            self._d_upload
            + self._d_batch_ttfb
            + self._d_batch_stream
            + self._d_cleanup_ttfb
            + self._d_cleanup_stream,
        )

    def hide_upload_progress(self) -> None:
        if self._destroyed:
            return
        self._stop_wait()
        self._pipeline_active = False
        self._rail_fraction = 0.0
        self._upload_bar.set_visible(False)
        self._upload_bar.set_fraction(0)
        self._rail_behind.set_visible(False)
        self._rail_behind.set_fraction(0)
        self._rail_overlay.set_visible(False)
        for cls in _PHASE_CLASSES:
            self._upload_bar.remove_css_class(cls)
            self._rail_behind.remove_css_class(cls)

    # ── Wait animation (TTFB gaps) ──

    def _snap_to_batch_stream(self) -> None:
        """First batch delta — end the TTFB wait, start char tracking."""
        if not self._pipeline_active:
            return
        self._stop_wait()
        self._pipeline_chars = 0
        self._rail_set(self._seg_batch_wait)
        now = GLib.get_monotonic_time()
        self._pl_batch_first_us = now
        ref = self._pl_upload_done_us or self._pl_start_us
        actual_ms = (now - ref) / 1000
        logger.debug(
            "pipeline_progress: batch_first_delta "
            "actual_ttfb_ms=%.0f predicted_ttfb_ms=%.0f",
            actual_ms,
            self._d_batch_ttfb,
        )

    def _snap_to_cleanup_stream(self) -> None:
        """First cleanup delta — end the TTFB wait, start char tracking."""
        if not self._pipeline_active:
            return
        self._stop_wait()
        self._pipeline_chars = 0
        self._rail_set(self._seg_cleanup_wait)
        now = GLib.get_monotonic_time()
        self._pl_cleanup_first_us = now
        ref = self._pl_batch_done_us or self._pl_start_us
        actual_ms = (now - ref) / 1000
        logger.debug(
            "pipeline_progress: cleanup_first_delta "
            "actual_ttfb_ms=%.0f predicted_ttfb_ms=%.0f",
            actual_ms,
            self._d_cleanup_ttfb,
        )

    def _start_wait(self, from_frac: float, to_frac: float, expected_ms: int) -> None:
        self._stop_wait()
        self._wait_from = from_frac
        self._wait_to = to_frac
        self._wait_ms = expected_ms
        self._wait_start_us = GLib.get_monotonic_time()
        self._wait_source = GLib.timeout_add(_WAIT_TICK_MS, self._tick_wait)

    def _stop_wait(self) -> None:
        if self._wait_source is not None:
            GLib.source_remove(self._wait_source)
            self._wait_source = None

    def _tick_wait(self) -> bool:
        elapsed_ms = (GLib.get_monotonic_time() - self._wait_start_us) / 1000
        t = min(elapsed_ms / self._wait_ms, 1.0)
        frac = self._wait_from + t * (self._wait_to - self._wait_from)
        self._rail_set(frac)
        if t >= 1.0:
            self._wait_source = None
            return False
        return True

    # ── Session directory ──

    def set_session_dir(self, path: str) -> None:
        """Store the session directory path for the folder button."""
        self._session_dir = path

    # ── Button handlers ──

    def _on_mode_switch(self, mode: str) -> None:
        if mode == "meeting" and self._on_switch_to_meeting is not None:
            self._on_switch_to_meeting()

    def _on_open_folder(self, _btn: Gtk.Button) -> None:
        if self._session_dir:
            Gio.AppInfo.launch_default_for_uri(
                GLib.filename_to_uri(self._session_dir, None), None
            )

    def _on_copy(self, _btn: Gtk.Button) -> None:
        self.copy_to_clipboard()

    def has_text_selection(self) -> bool:
        """True when the user has selected part of the transcript.

        Ctrl+C is captured before the TextView sees it (the TextView holds
        focus and would otherwise swallow the accelerator and copy nothing).
        A deliberate selection is the one case where the TextView's own
        copy is the right answer, so the key is handed back to it.
        """
        return self._text_view.get_buffer().get_has_selection()

    def copy_to_clipboard(self) -> bool:
        """Copy the current text buffer to the clipboard. Returns True if text was copied."""
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
        # The toast covers the pill you just used, never the words you are
        # still reading — so the pill dims under it instead of reflowing,
        # and the auto-close countdown holds while it is up.
        self._pause_autoclose_countdown()
        self._pill.add_css_class("toasting")
        toast = Adw.Toast(title="\u25cf  Copied")
        toast.set_timeout(2)
        toast.connect("dismissed", lambda _t: self._pill.remove_css_class("toasting"))
        self._toast_overlay.add_toast(toast)
        return True

    def _on_cancel(self, _btn: Gtk.Button) -> None:
        logger.debug("_on_cancel: state=%s", self._machine.state.name)
        if self._machine.state in (
            # INITIALIZING included because the button is now visible from the
            # moment the window maps; Escape already cancelled from here.
            State.INITIALIZING,
            State.WARMING,
            State.RECORDING,
            State.TRANSCRIBING,
            State.CLEANING,
        ):
            self._machine.transition(State.CANCELLED)

    def _on_action(self, _btn: Gtk.Button) -> None:
        s = self._machine.state
        logger.debug("_on_action: state=%s degraded=%s", s.name, self._degraded)
        if s in (State.WARMING, State.RECORDING):
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
        """Start the countdown once. Does not restart after interaction."""
        if self._autoclose_paused or self._autoclose_source is not None:
            return
        self._autoclose_remaining = self._autoclose_total
        self._update_autoclose_label()
        self._autoclose_source = GLib.timeout_add_seconds(1, self._tick_autoclose)

    def _pause_autoclose_countdown(self) -> None:
        """Permanently pause auto-close on any interaction."""
        self._autoclose_paused = True
        if self._autoclose_source is not None:
            GLib.source_remove(self._autoclose_source)
            self._autoclose_source = None
        self._action_btn_label.set_label("Close")

    def _stop_autoclose_countdown(self) -> None:
        if self._autoclose_source is not None:
            GLib.source_remove(self._autoclose_source)
            self._autoclose_source = None
        self._autoclose_paused = False
        self._action_btn_label.set_label("Close")

    def _update_autoclose_label(self) -> None:
        self._action_btn_label.set_label(f"Close · {self._autoclose_remaining}s")

    def _tick_autoclose(self) -> bool:
        self._autoclose_remaining -= 1
        if self._autoclose_remaining <= 0:
            self._autoclose_source = None
            self._window.close()
            return False
        self._update_autoclose_label()
        return True

    # ── Text pulse (processing indicator) ──

    def _start_text_pulse(self) -> None:
        """Drop the outgoing transcript to a flat 45% and hold it there.

        This deliberately does not pulse. A pulse duplicated the message
        the rail already carries; the real signal is "this text is
        provisional", and a steady dim states that without flicker.
        """
        self._text_view.add_css_class("text-outgoing")

    def _stop_text_pulse(self) -> None:
        self._text_view.remove_css_class("text-outgoing")

    # ── Cleanup ──

    def destroy(self) -> None:
        logger.debug("destroy: tearing down UI")
        self._destroyed = True
        self._stop_timer()
        self._stop_text_pulse()
        self._stop_meter()
        self._stop_wait()
        self._stop_autoclose_countdown()
