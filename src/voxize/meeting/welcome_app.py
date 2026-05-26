"""Meeting welcome screen — session list + Record button.

Default landing when ``python -m voxize.meeting`` is invoked with no
arguments. Lists all meeting sessions with green dots for those that
have been transcribed. Clicking a row opens the process workbench;
the Record button starts a new recording.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, Gtk  # noqa: E402

from voxize.meeting.sessions import MeetingSession, list_meeting_sessions  # noqa: E402

logger = logging.getLogger(__name__)


class WelcomeApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id="dev.flemma.VoxizeMeetingWelcome",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )

    def do_activate(self) -> None:
        css = Gtk.CssProvider()
        css_path = Path(__file__).parent.parent / "style.css"
        css.load_from_string(css_path.read_text())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        win = Gtk.ApplicationWindow(application=self)
        win.set_title("Voxize · Meeting")
        win.set_default_size(400, 500)

        ctrl = Gtk.EventControllerKey()
        ctrl.connect("key-pressed", self._on_key)
        win.add_controller(ctrl)

        hb = Gtk.HeaderBar()

        record_btn = Gtk.Button(label="Record")
        record_btn.add_css_class("suggested-action")
        record_btn.connect("clicked", self._on_record)
        hb.pack_end(record_btn)

        win.set_titlebar(hb)

        sessions = list_meeting_sessions()

        if not sessions:
            empty = Gtk.Label(label="No meeting recordings yet")
            empty.add_css_class("timer-label")
            empty.set_vexpand(True)
            empty.set_valign(Gtk.Align.CENTER)
            win.set_child(empty)
        else:
            listbox = Gtk.ListBox()
            listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
            listbox.connect("row-activated", self._on_row_activated)

            for session in sessions:
                row = self._build_row(session)
                listbox.append(row)

            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_child(listbox)
            scroll.set_vexpand(True)
            win.set_child(scroll)

        self._window = win
        win.present()

    def _build_row(self, session: MeetingSession) -> Gtk.ListBoxRow:
        row_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        row_box.set_margin_top(8)
        row_box.set_margin_bottom(8)
        row_box.set_margin_start(12)
        row_box.set_margin_end(12)

        info_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        info_box.set_hexpand(True)

        date_label = Gtk.Label(
            label=session.created.strftime("%-d %b %Y  %H:%M"),
        )
        date_label.add_css_class("status-label")
        date_label.set_xalign(0)
        info_box.append(date_label)

        details = []
        if session.duration_s is not None:
            details.append(_format_duration(int(session.duration_s)))
        if session.file_size_bytes > 0:
            details.append(_format_size(session.file_size_bytes))
        if details:
            detail_label = Gtk.Label(label="  ·  ".join(details))
            detail_label.add_css_class("timer-label")
            detail_label.set_xalign(0)
            info_box.append(detail_label)

        row_box.append(info_box)

        if session.has_transcript:
            dot = Gtk.Label(label="●")
            dot.add_css_class("status-dot")
            dot.add_css_class("ready")
            dot.set_valign(Gtk.Align.CENTER)
            row_box.append(dot)

        row = Gtk.ListBoxRow()
        row.set_child(row_box)
        row._session = session
        if not session.has_opus:
            row.set_sensitive(False)
        return row

    # ── Handlers ──

    def _on_record(self, _btn: Gtk.Button) -> None:
        self._spawn("--record")

    def _on_row_activated(self, _listbox, row) -> None:
        session = row._session
        self._spawn("--process", session.path)

    def _on_key(self, _ctrl, keyval, _code, _mod) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.quit()
            return True
        return False

    def _spawn(self, *args: str) -> None:
        cmd = [sys.executable, "-m", "voxize.meeting", *args]
        try:
            subprocess.Popen(cmd)
            logger.info("spawned: %s", " ".join(cmd))
        except Exception:
            logger.exception("failed to spawn: %s", " ".join(cmd))
        self.quit()


# ── Helpers ──


def _format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def _format_size(n_bytes: int) -> str:
    if n_bytes < 1024 * 1024:
        return f"{n_bytes / 1024:.0f} KB"
    return f"{n_bytes / (1024 * 1024):.1f} MB"
