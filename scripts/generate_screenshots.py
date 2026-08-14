#!/usr/bin/env python3
"""Generate reproducible screenshots for the README and design review.

Usage:
    uv run python scripts/generate_screenshots.py           # README shots
    uv run python scripts/generate_screenshots.py --all     # every state

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
from voxize.meeting.sessions import list_meeting_sessions  # noqa: E402
from voxize.prompt import PromptSource  # noqa: E402
from voxize.state import State, StateMachine  # noqa: E402
from voxize.ui import OverlayWindow  # noqa: E402

ASSETS = _ROOT / "assets"
ALL_STATES = "--all" in sys.argv

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
    ASSETS.mkdir(exist_ok=True)
    path = ASSETS / filename
    texture.save_to_png(str(path))
    print(f"  saved {path.name} ({w * _SCALE}x{h * _SCALE})")
    return True


class _Meter:
    def __init__(self, level_dbfs=-25.0):
        self.level_dbfs = level_dbfs


def _mk(app, level=-25.0):
    """Build a fresh window + OverlayWindow pair."""
    win = Gtk.ApplicationWindow(application=app)
    # Must match app.py: a resizable window keeps whatever size it was
    # mapped at and ignores the transcript's grown size request.
    win.set_resizable(False)
    win.set_default_size(516, -1)
    machine = StateMachine()
    ui = OverlayWindow(
        win, machine, autoclose_seconds=0, on_switch_to_meeting=lambda: None
    )
    ui.set_level_meter(_Meter(level))
    app.ui = ui
    return win, machine, ui


def _freeze_timer(ui, seconds):
    ui._stop_timer()
    ui._timer_seconds = seconds
    m, s = divmod(seconds, 60)
    ui._timer_label.set_text(f"{m:02d}:{s:02d}")


# ── Screenshot app ──


class _App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="dev.flemma.VoxizeScreenshot", flags=0)
        self._queue = []
        self._current = None
        self._extra = None

    def do_activate(self):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        display = Gdk.Display.get_default()
        css = Gtk.CssProvider()
        css.load_from_string((_ROOT / "src" / "voxize" / "style.css").read_text())
        Gtk.StyleContext.add_provider_for_display(
            display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self._queue = [
            ("dictation-recording.png", self._recording),
            ("dictation-ready.png", self._ready),
            ("meeting-welcome.png", self._meeting_welcome),
        ]
        if ALL_STATES:
            self._queue = [
                ("state-00-initializing.png", self._initializing),
                ("state-01-warming.png", self._warming),
                ("dictation-recording.png", self._recording),
                ("state-02b-silence.png", self._silence),
                ("state-02c-level-low.png", self._level_low),
                ("state-02d-level-hot.png", self._level_hot),
                ("state-02e-level-clip.png", self._level_clip),
                ("state-03-working-first.png", self._working_first),
                ("state-03b-working-second.png", self._working_second),
                ("dictation-ready.png", self._ready),
                ("state-04b-copied.png", self._copied),
                ("state-05-guidance.png", self._guidance),
                ("state-06-overflow-error.png", self._overflow_error),
                ("state-07-meeting-recording.png", self._meeting_recording),
                ("meeting-welcome.png", self._meeting_welcome),
            ]
        self._next()

    def _next(self):
        if self._current is not None:
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
        win, _machine, ui = _mk(self, level=-70.0)
        _freeze_timer(ui, 0)
        return win

    def _warming(self):
        win, machine, ui = _mk(self, level=-70.0)
        machine.transition(State.WARMING)
        _freeze_timer(ui, 0)
        return win

    def _recording(self):
        win, machine, ui = _mk(self, level=-19.0)
        machine.transition(State.WARMING)
        machine.transition(State.RECORDING)
        ui.append_text(_LIVE_TEXT)
        ui.on_speech(True)
        ui._tick_meter()
        _freeze_timer(ui, 8)
        return win

    def _silence(self):
        win, machine, ui = _mk(self, level=-52.0)
        machine.transition(State.WARMING)
        machine.transition(State.RECORDING)
        ui.append_text(
            "…and update the integration tests plus the API "
            "documentation while you're in there"
        )
        ui.on_speech(False)
        ui._tick_meter()
        _freeze_timer(ui, 21)
        return win

    def _level_hot(self):
        # -8 dBFS -> fraction 0.867, the "hot" band.
        return self._level_at(-8.0, "You're getting a bit loud for the mic now")

    def _level_clip(self):
        # -1 dBFS -> fraction 0.983, past the clip threshold.
        return self._level_at(-1.0, "THIS IS FAR TOO LOUD FOR THE MICROPHONE")

    def _level_low(self):
        # -46 dBFS -> fraction 0.233, under-driven.
        return self._level_at(-46.0, "barely audible, mic gain is too low")

    def _level_at(self, dbfs, text):
        win, machine, ui = _mk(self, level=dbfs)
        machine.transition(State.WARMING)
        machine.transition(State.RECORDING)
        ui.append_text(text)
        ui.on_speech(True)
        ui._tick_meter()
        _freeze_timer(ui, 12)
        return win

    def _working_first(self):
        win, machine, ui = _mk(self)
        machine.transition(State.WARMING)
        machine.transition(State.RECORDING)
        ui.append_text(_LIVE_TEXT)
        _freeze_timer(ui, 14)
        machine.transition(State.TRANSCRIBING)
        ui.start_pipeline(len(_LIVE_TEXT) * 3)
        ui._rail_set(0.31)
        return win

    def _working_second(self):
        win, machine, ui = _mk(self)
        machine.transition(State.WARMING)
        machine.transition(State.RECORDING)
        ui.append_text(_LIVE_TEXT)
        _freeze_timer(ui, 14)
        machine.transition(State.TRANSCRIBING)
        ui.start_pipeline(len(_LIVE_TEXT) * 3)
        ui.append_text(_BATCH_TEXT)
        machine.transition(State.CLEANING)
        ui.show_transcript_for_cleanup(_BATCH_TEXT)
        ui.append_text(_BATCH_TEXT)
        ui.on_batch_complete(len(_BATCH_TEXT))
        ui._rail_set(0.78)
        return win

    def _ready(self):
        win, machine, ui = _mk(self)
        machine.transition(State.WARMING)
        machine.transition(State.RECORDING)
        ui.append_text("placeholder")
        _freeze_timer(ui, 14)
        machine.transition(State.TRANSCRIBING)
        ui.append_text("swap")
        machine.transition(State.CLEANING)
        ui.show_transcript_for_cleanup("placeholder")
        ui.append_text(_CLEANED_TEXT)
        machine.transition(State.READY)
        ui.show_session_costs(0.0007, 0.0031, 0.0005)
        _freeze_timer(ui, 14)
        return win

    def _copied(self):
        win = self._ready()
        GLib.timeout_add(250, lambda: (self.ui.copy_to_clipboard(), False)[1])
        return win

    def _guidance(self):
        win, machine, ui = _mk(self, level=-19.0)
        machine.transition(State.WARMING)
        machine.transition(State.RECORDING)
        ui.append_text(_LIVE_TEXT)
        ui.on_speech(True)
        ui._tick_meter()
        _freeze_timer(ui, 8)
        ui.show_prompt_context(
            [
                PromptSource(
                    path="/home/stan/proj/WHISPER.txt",
                    content="Supabase, Luup, PyGObject, libadwaita, Wayland, "
                    "PipeWire, wpctl",
                ),
                PromptSource(
                    path="/home/stan/proj/slack/#eng-platform",
                    content="sev-2, Kubo, retro, on-call rota, blameless",
                ),
            ]
        )
        # _shoot() pops this once the toplevel is mapped.
        self._extra = (ui._guidance_popover, "state-05b-guidance-popover.png")
        return win

    def _overflow_error(self):
        win, machine, ui = _mk(self, level=-19.0)
        machine.transition(State.WARMING)
        machine.transition(State.RECORDING)
        ui.append_text(_LONG_TEXT)
        ui.on_speech(True)
        ui._tick_meter()
        _freeze_timer(ui, 47)
        ui._error_label.set_text("Live preview disconnected — still recording")
        ui._error_revealer.set_reveal_child(True)
        return win

    def _meeting_recording(self):
        """Meeting recorder — shares style.css but has no .card ancestor.

        Here to catch the class of regression where a rule scoped to the
        dictation card (or to `window`) silently strips this window's
        palette or chrome.
        """
        from voxize.meeting.ui import MeetingWindow

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
        that shares style.css without a .card ancestor."""
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
    ASSETS.mkdir(exist_ok=True)
    _seed_meetings()
    print("Generating screenshots...")
    _App().run([])
    shutil.rmtree(_SEED_STATE, ignore_errors=True)
    print("Done!")


if __name__ == "__main__":
    main()
