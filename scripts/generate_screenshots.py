#!/usr/bin/env python3
"""Generate reproducible screenshots for the README.

Usage:
    uv run python scripts/generate_screenshots.py

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

from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

from voxize import mode_switcher  # noqa: E402
from voxize.meeting.sessions import list_meeting_sessions  # noqa: E402
from voxize.state import State, StateMachine  # noqa: E402
from voxize.ui import OverlayWindow  # noqa: E402

ASSETS = _ROOT / "assets"

_RECORDING_TEXT = (
    "So I need you to refactor the authentication middleware "
    "to use JWT tokens instead of session cookies."
)

_CLEANED_TEXT = (
    "Refactor the authentication middleware to use JWT tokens "
    "instead of session cookies. The current implementation stores "
    "session tokens in a way that doesn't meet the new compliance "
    "requirements. Update the integration tests and the API "
    "documentation."
)


# ── Seed data ──


def _seed_meetings():
    base = os.path.join(_SEED_STATE, "voxize")
    os.makedirs(base, exist_ok=True)

    s1 = os.path.join(base, "2026-06-01T14-30-00-meeting")
    os.makedirs(s1)
    Path(s1, "recording.opus").write_bytes(os.urandom(48_000))
    Path(s1, "transcript.txt").write_text(
        "00:00:00,000 --> 00:00:15,000 [Speaker 1]\n"
        "Welcome everyone. Let's review the sprint goals.\n\n"
        "00:00:15,000 --> 00:00:30,000 [Speaker 2]\n"
        "Sure. The main priority is shipping the auth migration.\n"
    )
    Path(s1, "title.txt").write_text("Weekly AI standup\n")

    s2 = os.path.join(base, "2026-05-28T09-15-00-meeting")
    os.makedirs(s2)
    Path(s2, "recording.opus").write_bytes(os.urandom(120_000))
    Path(s2, "title.txt").write_text("Architecture review\n")

    s3 = os.path.join(base, "2026-06-04T16-00-00-meeting")
    os.makedirs(s3)
    Path(s3, "recording.opus").write_bytes(os.urandom(85_000))


# ── Capture utility ──


def _capture(window, filename):
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
    native = window.get_native()
    if native is None:
        print(f"  SKIP {filename}: window not realized")
        return False
    renderer = native.get_renderer()
    if renderer is None:
        print(f"  SKIP {filename}: no renderer")
        return False
    scaled = Gsk.TransformNode.new(
        node, Gsk.Transform.new().scale(_SCALE, _SCALE)
    )
    viewport = Graphene.Rect()
    viewport.init(0, 0, w * _SCALE, h * _SCALE)
    texture = renderer.render_texture(scaled, viewport)
    ASSETS.mkdir(exist_ok=True)
    path = ASSETS / filename
    texture.save_to_png(str(path))
    print(f"  saved {path} ({w * _SCALE}x{h * _SCALE})")
    return True


# ── Mock level meter ──


class _Meter:
    def __init__(self, level_dbfs=-25.0):
        self.level_dbfs = level_dbfs


# ── Screenshot app ──


class _App(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="dev.flemma.VoxizeScreenshot",
            flags=0,
        )

    def do_activate(self):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        display = Gdk.Display.get_default()

        css = Gtk.CssProvider()
        css.load_from_string((_ROOT / "src" / "voxize" / "style.css").read_text())
        Gtk.StyleContext.add_provider_for_display(
            display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        mode_switcher.load_css(display)

        self._display = display
        self._build_recording_window()
        GLib.timeout_add(2000, self._capture_recording)

    # ── Dictation: recording ──

    def _build_recording_window(self):
        self._win = Gtk.ApplicationWindow(application=self)
        self._win.set_default_size(520, -1)

        self._machine = StateMachine()
        self._ui = OverlayWindow(
            self._win,
            self._machine,
            autoclose_seconds=0,
            on_switch_to_meeting=lambda: None,
        )
        self._ui.set_level_meter(_Meter(-25.0))
        self._ui._scroll.set_min_content_height(80)

        self._machine.transition(State.RECORDING)
        self._ui._stop_timer()
        self._ui._timer_seconds = 8
        self._ui._timer_label.set_text("00:08")
        self._ui.append_text(_RECORDING_TEXT)

        self._win.present()

    def _capture_recording(self):
        _capture(self._win, "dictation-recording.png")
        self._win.close()
        self._build_ready_window()
        GLib.timeout_add(1000, self._capture_ready)
        return False

    # ── Dictation: ready ──

    def _build_ready_window(self):
        self._ready_win = Gtk.ApplicationWindow(application=self)
        self._ready_win.set_default_size(520, -1)

        machine = StateMachine()
        ui = OverlayWindow(
            self._ready_win,
            machine,
            autoclose_seconds=0,
            on_switch_to_meeting=lambda: None,
        )

        ui._scroll.set_min_content_height(120)

        machine.transition(State.RECORDING)
        ui.append_text("placeholder")
        machine.transition(State.TRANSCRIBING)
        ui.append_text("swap")
        machine.transition(State.CLEANING)
        ui.show_transcript_for_cleanup("placeholder")
        ui.append_text(_CLEANED_TEXT)
        machine.transition(State.READY)
        ui.show_session_costs(0.0007, 0.0031, 0.0005)

        self._ready_win.present()

    def _capture_ready(self):
        _capture(self._ready_win, "dictation-ready.png")
        self._ready_win.close()
        self._meeting_window()
        return False

    # ── Meeting welcome ──

    def _meeting_window(self):
        from gi.repository import Pango

        win = Gtk.ApplicationWindow(application=self)
        win.set_title("Voxize · Meeting")
        win.set_default_size(400, 350)

        hb = Gtk.HeaderBar()
        record_btn = Gtk.Button(label="Record")
        record_btn.add_css_class("suggested-action")
        hb.pack_end(record_btn)
        win.set_titlebar(
            mode_switcher.build_titlebar("meeting", lambda _m: None, hb)
        )

        sessions = list_meeting_sessions()
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)

        for session in sessions:
            row_box = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=10
            )
            row_box.set_margin_top(8)
            row_box.set_margin_bottom(8)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            info.set_hexpand(True)

            date_label = Gtk.Label(
                label=session.created.strftime("%-d %b %Y  %H:%M")
            )
            date_label.add_css_class("status-label")
            date_label.set_xalign(0)
            info.append(date_label)

            if session.title:
                title = Gtk.Label(label=session.title)
                title.set_xalign(0)
                title.set_ellipsize(Pango.EllipsizeMode.END)
                info.append(title)

            details = []
            if session.file_size_bytes > 0:
                mb = session.file_size_bytes / (1024 * 1024)
                if mb >= 1:
                    details.append(f"{mb:.1f} MB")
                else:
                    details.append(f"{session.file_size_bytes / 1024:.0f} KB")
            if details:
                detail = Gtk.Label(label="  ·  ".join(details))
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

        self._meeting_win = win
        win.present()

        GLib.timeout_add(1000, self._capture_meeting)

    def _capture_meeting(self):
        _capture(self._meeting_win, "meeting-welcome.png")
        self._meeting_win.close()
        self.quit()
        return False


def main():
    _seed_meetings()
    ASSETS.mkdir(exist_ok=True)
    print("Generating screenshots...")
    _App().run([])
    shutil.rmtree(_SEED_STATE, ignore_errors=True)
    print("Done!")


if __name__ == "__main__":
    main()
