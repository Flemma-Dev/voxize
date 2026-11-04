#!/usr/bin/env python3
"""Generate reproducible screenshots for the README and design review.

Usage:
    uv run python scripts/generate_screenshots.py           # README shots
    uv run python scripts/generate_screenshots.py --all     # every state
    uv run python scripts/generate_screenshots.py --all --out /tmp/shots

Requires a display (Wayland or X11). Use xvfb-run for headless.
Outputs PNGs to assets/ with stable filenames.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

_SEED_STATE = tempfile.mkdtemp(prefix="voxize-screenshots-")
os.environ["XDG_STATE_HOME"] = _SEED_STATE
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="voxize-config-")
_SCALE = 2

import gi  # noqa: E402 — XDG env vars must be set before GTK imports

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from voxize import mode_switcher  # noqa: E402
from voxize.frontend.state import ClientState  # noqa: E402
from voxize.gui.ui import OverlayWindow, _format_timer  # noqa: E402
from voxize.meeting.sessions import list_meeting_sessions  # noqa: E402
from voxize.protocol import State  # noqa: E402

ALL_STATES = "--all" in sys.argv
# --out DIR renders somewhere other than the README's assets/ folder.
if "--out" in sys.argv:
    _i = sys.argv.index("--out") + 1
    if _i >= len(sys.argv):
        sys.exit("--out needs a directory")
    ASSETS = Path(sys.argv[_i]).resolve()
else:
    ASSETS = _ROOT / "assets"

_LIVE_TEXT = (
    "So I need you to refactor the authentication middleware "
    "to use JWT tokens instead of session cookies."
)

_BATCH_TEXT = (
    "Refactor the authentication middleware to use JWT tokens instead "
    "of session cookies. The current implementation stores session"
)

_CLEANED_TEXT = (
    "Refactor the authentication middleware to use JWT tokens "
    "instead of session cookies. The current implementation stores "
    "session tokens in a way that doesn't meet the new compliance "
    "requirements. Update the integration tests and the API "
    "documentation."
)


def _seed_meetings():
    """Fake meeting sessions so the welcome list has rows to render."""
    base = os.path.join(_SEED_STATE, "voxize")
    os.makedirs(base, exist_ok=True)

    s1 = os.path.join(base, "2026-06-01T14-30-00-meeting")
    os.makedirs(s1, exist_ok=True)
    Path(s1, "recording.opus").write_bytes(b"\0" * 48_000)
    Path(s1, "transcript.txt").write_text(
        "00:00:00,000 --> 00:00:15,000 [Speaker 1]\n"
        "Welcome everyone. Let's review the sprint goals.\n"
    )
    Path(s1, "title.txt").write_text("Weekly AI standup\n")

    s2 = os.path.join(base, "2026-05-28T09-15-00-meeting")
    os.makedirs(s2, exist_ok=True)
    Path(s2, "recording.opus").write_bytes(b"\0" * 120_000)
    Path(s2, "title.txt").write_text("Architecture review\n")

    s3 = os.path.join(base, "2026-06-04T16-00-00-meeting")
    os.makedirs(s3, exist_ok=True)
    Path(s3, "recording.opus").write_bytes(b"\0" * 85_000)


_LONG_TEXT = (
    "The current session store doesn't meet the new compliance "
    "requirements, so the middleware needs to move to short-lived JWTs "
    "with a refresh endpoint behind the same rate limiter we use for "
    "login. Update the integration tests, the API documentation, and "
    "the onboarding guide, then flag anything still reading the cookie"
)


# ── Capture utility ──


def _capture(window, filename, native=None):
    """Snapshot a widget through the renderer of the surface it lives on.

    A popover is its own GtkNative with its own GdkSurface, so capturing one
    means rendering it against *its* renderer rather than the toplevel's —
    pass it as both `window` and `native`.
    """
    paintable = Gtk.WidgetPaintable(widget=window)
    w = window.get_width()
    h = window.get_height()
    if w <= 0 or h <= 0:
        print(f"  SKIP {filename}: window has no size ({w}x{h})")
        return False
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, w, h)
    node = snapshot.to_node()
    if node is None:
        print(f"  SKIP {filename}: no render node")
        return False
    native = native or window.get_native()
    if native is None:
        print(f"  SKIP {filename}: window not realized")
        return False
    renderer = native.get_renderer()
    if renderer is None:
        print(f"  SKIP {filename}: no renderer")
        return False
    scaled = Gsk.TransformNode.new(node, Gsk.Transform.new().scale(_SCALE, _SCALE))
    viewport = Graphene.Rect()
    viewport.init(0, 0, w * _SCALE, h * _SCALE)
    texture = renderer.render_texture(scaled, viewport)
    ASSETS.mkdir(parents=True, exist_ok=True)
    path = ASSETS / filename
    texture.save_to_png(str(path))
    print(f"  saved {path.name} ({w * _SCALE}x{h * _SCALE})")
    return True


def _mk(app, level=-25.0):
    """Build a fresh window + OverlayWindow pair, configured as the app does."""
    app.use_css("dictation")
    win = Adw.ApplicationWindow(application=app)
    # Must match gui/app.py: a resizable window keeps whatever size it was
    # mapped at and ignores the transcript's grown size request.
    win.set_resizable(False)
    win.set_default_size(460, -1)
    win.set_size_request(460, -1)
    state = ClientState()
    ui = OverlayWindow(
        win,
        state,
        autoclose_seconds=0,
        on_switch_to_meeting=lambda: None,
        on_stop=lambda: None,
        on_cancel=lambda: None,
    )
    app.ui = ui
    app.level = level
    return win, state, ui


def _freeze_timer(ui, seconds):
    ui._stop_timer()
    ui._timer_seconds = seconds
    ui._timer_label.set_text(_format_timer(seconds))


def _show_countdown(ui, seconds):
    """What a focused READY shows; the harness window may not be active."""
    ui._autoclose_remaining = seconds
    ui._update_autoclose_label()


def _to_recording(app, state, ui, text, seconds):
    state.apply(State.WARMING)
    state.apply(State.RECORDING)
    if text:
        ui.on_text("live", "replace", text)
    ui.set_level(app.level)
    _freeze_timer(ui, seconds)


def _to_ready(app, state, ui, seconds=14):
    """RECORDING → TRANSCRIBING → CLEANING with the real text handoffs."""
    _to_recording(app, state, ui, _LIVE_TEXT, seconds)
    state.apply(State.TRANSCRIBING)
    ui.set_progress(0.0)
    ui.on_text("batch", "replace", _BATCH_TEXT)
    state.apply(State.CLEANING)
    ui.on_text("batch", "replace", _BATCH_TEXT)


# ── Screenshot app ──


class _App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="dev.flemma.VoxizeScreenshot", flags=0)
        self._queue = []
        self._current = None
        self._extra = None
        self._css = {}
        self._active_css = None

    def use_css(self, name):
        """Install exactly one app stylesheet, as each real app does.

        The dictation window loads dictation.css and the meeting recorder
        loads style.css (+ the mode switcher's). Both define .status-dot,
        so having both on the display would show neither app as it ships.
        """
        if name == self._active_css:
            return
        display = Gdk.Display.get_default()
        for provider, _priority in self._css.get(self._active_css, []):
            Gtk.StyleContext.remove_provider_for_display(display, provider)
        if name not in self._css:
            css = Gtk.CssProvider()
            filename = "gui/dictation.css" if name == "dictation" else "style.css"
            css.load_from_string((_ROOT / "src" / "voxize" / filename).read_text())
            providers = [(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)]
            if name == "meeting":
                switcher = Gtk.CssProvider()
                switcher.load_from_string(mode_switcher._CSS)
                providers.append((switcher, Gtk.STYLE_PROVIDER_PRIORITY_USER))
            self._css[name] = providers
        for provider, priority in self._css[name]:
            Gtk.StyleContext.add_provider_for_display(display, provider, priority)
        self._active_css = name

    def do_activate(self):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        self._queue = [
            ("dictation-recording.png", self._recording),
            ("dictation-ready.png", self._ready),
            ("meeting-welcome.png", self._meeting_welcome),
        ]
        if ALL_STATES:
            self._queue = [
                ("state-01-initializing.png", self._initializing),
                ("state-02-warming.png", self._warming),
                ("dictation-recording.png", self._recording),
                ("state-04-recording-quiet.png", self._level_quiet),
                ("state-05-recording-loud.png", self._level_hot),
                ("state-05b-recording-clip.png", self._level_clip),
                ("state-06-recording-preview-error.png", self._preview_error),
                ("state-07-working.png", self._working),
                ("state-07b-working-cleanup.png", self._working_cleanup),
                ("state-08-working-stalled.png", self._working_stalled),
                ("state-09-working-no-prediction.png", self._working_pulse),
                ("dictation-ready.png", self._ready),
                ("state-11-ready-copied.png", self._copied),
                ("state-12-ready-no-speech.png", self._no_speech),
                ("state-13-ready-batch-failed.png", self._batch_failed),
                ("state-14-ready-cleanup-failed.png", self._cleanup_failed),
                ("state-15-error.png", self._error),
                ("state-16-mode-menu.png", self._mode_menu),
                ("state-17-guidance.png", self._guidance),
                ("state-18-long-text.png", self._long_text),
                ("state-19-meeting-recording.png", self._meeting_recording),
                ("meeting-welcome.png", self._meeting_welcome),
            ]
        self._next()

    def _next(self):
        if self._current is not None:
            # Stop the shot's meter/stall/pulse timers with its window.
            ui = getattr(self, "ui", None)
            if ui is not None:
                ui.destroy()
                self.ui = None
            self._current.close()
            self._current = None
        if not self._queue:
            self.quit()
            return
        filename, builder = self._queue.pop(0)
        self._current = builder()
        self._current.present()
        GLib.timeout_add(900, self._shoot, filename)

    def _shoot(self, filename):
        _capture(self._current, filename)
        # Popovers render into their own GdkSurface, so Gtk.WidgetPaintable
        # on the toplevel never sees them. Capture the popover separately
        # against its own renderer — and poll for it, because the surface is
        # not reliably mapped and sized by any fixed deadline under Xvfb.
        extra = self._extra
        self._extra = None
        if extra is None:
            self._next()
            return False
        # Pop it up only now, with the toplevel definitely mapped. Doing it
        # on a timer from the builder raced the parent surface and silently
        # produced an unmapped, unsizeable popover.
        extra[0].popup()
        self._await_popover(extra, tries=20)
        return False

    def _await_popover(self, extra, tries):
        popover, name = extra
        if popover.get_width() > 0:
            _capture(popover, name, native=popover)
            self._next()
            return False
        if tries <= 0:
            print(f"  SKIP {name}: popover never mapped")
            self._next()
            return False
        GLib.timeout_add(150, self._await_popover, extra, tries - 1)
        return False

    # ── States ──

    def _initializing(self):
        """What the compositor centres — the window is presented in this
        state, before the mic is up."""
        win, _state, _ui = _mk(self, level=-70.0)
        return win

    def _warming(self):
        win, state, ui = _mk(self, level=-70.0)
        state.apply(State.WARMING)
        ui.set_level(self.level)
        _freeze_timer(ui, 0)
        return win

    def _recording(self):
        win, state, ui = _mk(self, level=-19.0)
        _to_recording(self, state, ui, _LIVE_TEXT, 8)
        return win

    def _level_quiet(self):
        # -46 dBFS -> fraction 0.233, under-driven.
        return self._level_at(-46.0, 12)

    def _level_hot(self):
        # -8 dBFS -> fraction 0.867, the "hot" band.
        return self._level_at(-8.0, 21)

    def _level_clip(self):
        # -1 dBFS -> fraction 0.983, past the clip threshold.
        return self._level_at(-1.0, 22)

    def _level_at(self, dbfs, seconds):
        win, state, ui = _mk(self, level=dbfs)
        _to_recording(self, state, ui, _LIVE_TEXT, seconds)
        return win

    def _preview_error(self):
        win, state, ui = _mk(self, level=-25.0)
        _to_recording(self, state, ui, _LIVE_TEXT, 31)
        ui.show_live_preview_error("WebSocket rejected (HTTP 503)")
        return win

    def _working(self):
        win, state, ui = _mk(self)
        _to_recording(self, state, ui, _LIVE_TEXT, 14)
        state.apply(State.TRANSCRIBING)
        ui.set_progress(0.31)
        return win

    def _working_cleanup(self):
        win, state, ui = _mk(self)
        _to_ready(self, state, ui)
        ui.set_progress(0.78)
        return win

    def _working_stalled(self):
        win, state, ui = _mk(self)
        _to_recording(self, state, ui, _LIVE_TEXT, 14)
        state.apply(State.TRANSCRIBING)
        ui.set_progress(0.52)
        ui.set_stalled(True)
        return win

    def _working_pulse(self):
        win, state, ui = _mk(self)
        _to_recording(self, state, ui, "Fix the typo", 3)
        state.apply(State.TRANSCRIBING)
        ui.set_progress(None)
        return win

    def _ready(self):
        win, state, ui = _mk(self)
        _to_ready(self, state, ui)
        ui.on_text("cleaned", "replace", _CLEANED_TEXT)
        ui.set_progress(1.0)
        state.apply(State.READY)
        ui.show_session_costs(0.0007, 0.0031, 0.0005)
        _show_countdown(ui, 28)
        return win

    def _copied(self):
        win = self._ready()
        GLib.timeout_add(250, lambda: (self.ui.copy_to_clipboard(), False)[1])
        return win

    def _no_speech(self):
        win, state, ui = _mk(self)
        _to_recording(self, state, ui, "", 4)
        state.apply(State.TRANSCRIBING)
        ui.show_no_speech()
        state.apply(State.READY)
        _show_countdown(ui, 28)
        return win

    def _batch_failed(self):
        win, state, ui = _mk(self)
        _to_recording(self, state, ui, _LIVE_TEXT, 14)
        state.apply(State.TRANSCRIBING)
        ui.on_text("live", "replace", _LIVE_TEXT)
        ui.show_failure(
            "batch", "Batch transcription failed: Connection reset by peer", "live"
        )
        state.apply(State.READY)
        _show_countdown(ui, 28)
        return win

    def _cleanup_failed(self):
        win, state, ui = _mk(self)
        _to_ready(self, state, ui)
        ui.on_text("cleaned", "replace", "Refactor the auth")
        ui.on_text("batch", "replace", _BATCH_TEXT)
        ui.show_failure("cleanup", "Cleanup failed: Request timed out.", "batch")
        state.apply(State.READY)
        _show_countdown(ui, 28)
        return win

    def _error(self):
        win, state, _ui = _mk(self)
        state.apply(State.ERROR, error="Another Voxize instance is recording")
        return win

    def _mode_menu(self):
        win, state, ui = _mk(self, level=-19.0)
        _to_recording(self, state, ui, _LIVE_TEXT, 8)
        self._extra = (ui._mode_btn.get_popover(), "state-16b-mode-menu-popover.png")
        return win

    def _guidance(self):
        win, state, ui = _mk(self, level=-19.0)
        _to_recording(self, state, ui, _LIVE_TEXT, 8)
        ui.show_prompt_context(
            [
                {
                    "path": "/home/stan/proj/WHISPER.txt",
                    "content": "Supabase, Luup, PyGObject, libadwaita, Wayland, "
                    "PipeWire, wpctl",
                },
                {
                    "path": "/home/stan/proj/slack/#eng-platform",
                    "content": "sev-2, Kubo, retro, on-call rota, blameless",
                },
            ]
        )
        # _shoot() pops this once the toplevel is mapped.
        self._extra = (ui._guidance_popover, "state-17b-guidance-popover.png")
        return win

    def _long_text(self):
        win, state, ui = _mk(self)
        _to_ready(self, state, ui)
        ui.on_text(
            "cleaned", "replace", " ".join([_CLEANED_TEXT, _LONG_TEXT, _CLEANED_TEXT])
        )
        ui.set_progress(1.0)
        state.apply(State.READY)
        _show_countdown(ui, 28)
        return win

    def _meeting_recording(self):
        """Meeting recorder — loads style.css, not dictation.css.

        Here to catch the class of regression where a dictation rule leaks
        into this window, or a shared rule is deleted from under it.
        """
        from voxize.meeting.ui import MeetingWindow

        self.use_css("meeting")
        win = Gtk.ApplicationWindow(application=self)
        win.set_default_size(400, 240)
        mui = MeetingWindow(win, on_stop=lambda: None)
        mui._mic_bar.set_fraction(0.62)
        mui._sys_bar.set_fraction(0.28)
        mui._timer_label.set_text("00:04:12")
        mui._size_label.set_text("7.4 MB")
        mui._error_label.set_text("System audio source not found")
        mui._error_bar.set_visible(True)
        return win

    def _meeting_welcome(self):
        """The meeting session list — a README asset, and the other window
        that loads style.css."""
        self.use_css("meeting")
        win = Gtk.ApplicationWindow(application=self)
        win.set_title("Voxize · Meeting")
        win.set_default_size(400, 350)

        hb = Gtk.HeaderBar()
        record_btn = Gtk.Button(label="Record")
        record_btn.add_css_class("suggested-action")
        hb.pack_end(record_btn)
        win.set_titlebar(mode_switcher.build_titlebar("meeting", lambda _m: None, hb))

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)

        for session in list_meeting_sessions():
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row_box.set_margin_top(8)
            row_box.set_margin_bottom(8)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            info.set_hexpand(True)

            date_label = Gtk.Label(label=session.created.strftime("%-d %b %Y  %H:%M"))
            date_label.add_css_class("status-label")
            date_label.set_xalign(0)
            info.append(date_label)

            if session.title:
                title = Gtk.Label(label=session.title)
                title.set_xalign(0)
                title.set_ellipsize(Pango.EllipsizeMode.END)
                info.append(title)

            if session.file_size_bytes > 0:
                mb = session.file_size_bytes / (1024 * 1024)
                size = (
                    f"{mb:.1f} MB"
                    if mb >= 1
                    else f"{session.file_size_bytes / 1024:.0f} KB"
                )
                detail = Gtk.Label(label=size)
                detail.add_css_class("timer-label")
                detail.set_xalign(0)
                info.append(detail)

            row_box.append(info)

            if session.has_transcript:
                dot = Gtk.Label(label="●")
                dot.add_css_class("status-dot")
                dot.add_css_class("ready")
                dot.set_valign(Gtk.Align.CENTER)
                row_box.append(dot)

            row = Gtk.ListBoxRow()
            row.set_child(row_box)
            listbox.append(row)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(listbox)
        scroll.set_vexpand(True)
        win.set_child(scroll)
        return win


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    _seed_meetings()
    print("Generating screenshots...")
    _App().run([])
    shutil.rmtree(_SEED_STATE, ignore_errors=True)
    print("Done!")


if __name__ == "__main__":
    main()
