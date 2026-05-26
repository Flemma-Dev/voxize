"""Post-meeting processing window — transcription controls + results."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime

import gi

logger = logging.getLogger(__name__)

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from voxize.clipboard import copy as clipboard_copy  # noqa: E402
from voxize.meeting.sessions import MeetingSession, save_title  # noqa: E402
from voxize.meeting.transcribe import TranscribeParams, TranscribeResult  # noqa: E402

_TITLE_DEBOUNCE_MS = 300

_TIMESTAMP_LINE_RE = re.compile(
    r"^(\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}\s+)\[(.+?)\](\s*)$",
    re.MULTILINE,
)


class TagEntry:
    """Inline tag input — type terms, press Enter to add as pills."""

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self._tags: list[str] = []
        self._pill_widgets: list[Gtk.Widget] = []
        self._selected_idx: int | None = None
        self._on_change = on_change

        self._frame = Gtk.Frame()
        self._frame.add_css_class("tag-entry-frame")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_focusable(True)

        self._tag_view = self._make_tag_view()
        box.append(self._tag_view)

        self._entry = Gtk.Text()
        self._entry.set_placeholder_text("Add term…")
        self._entry.set_margin_start(8)
        self._entry.set_margin_end(8)
        self._entry.set_margin_top(6)
        self._entry.set_margin_bottom(6)
        self._entry.set_size_request(-1, 28)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        box.add_controller(key_ctrl)

        self._entry.connect("notify::has-focus", lambda *_: self._update_focus())
        box.connect("notify::has-focus", lambda *_: self._update_focus())

        box.append(self._entry)
        self._box = box
        self._frame.set_child(box)

    @property
    def widget(self) -> Gtk.Widget:
        return self._frame

    def get_tags(self) -> list[str]:
        return list(self._tags)

    def set_tags(self, tags: list[str]) -> None:
        self._clear_all()
        for tag in tags:
            if tag not in self._tags:
                self._add_tag(tag)

    def set_sensitive(self, sensitive: bool) -> None:
        self._frame.set_sensitive(sensitive)

    def grab_focus(self) -> None:
        self._entry.grab_focus()

    # ── Key handling ──

    def _on_key_pressed(self, _ctrl, keyval, _code, _state) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            text = self._entry.get_text().strip()
            if text:
                self._commit_text()
            elif self._selected_idx is not None:
                tag = self._tags[self._selected_idx]
                self._remove_tag_at(self._selected_idx)
                self._deselect()
                self._entry.set_text(tag)
                self._entry.set_position(-1)
            return True

        if keyval == Gdk.KEY_BackSpace:
            if not self._entry.get_text() and self._tags:
                if self._selected_idx is not None:
                    idx = self._selected_idx
                    self._remove_tag_at(idx)
                    if self._tags:
                        self._select_tag(min(idx, len(self._tags) - 1))
                    else:
                        self._deselect()
                else:
                    self._select_tag(len(self._tags) - 1)
                return True
            if self._selected_idx is not None:
                self._deselect()
            return False

        if keyval == Gdk.KEY_Delete and self._selected_idx is not None:
            idx = self._selected_idx
            self._remove_tag_at(idx)
            if self._tags:
                self._select_tag(min(idx, len(self._tags) - 1))
            else:
                self._deselect()
            return True

        if keyval in (Gdk.KEY_Left, Gdk.KEY_Up) and not self._entry.get_text():
            if self._selected_idx is not None and self._selected_idx > 0:
                self._select_tag(self._selected_idx - 1)
            elif self._selected_idx is None and self._tags:
                self._select_tag(len(self._tags) - 1)
            return True

        if keyval in (Gdk.KEY_Right, Gdk.KEY_Down) and self._selected_idx is not None:
            if self._selected_idx < len(self._tags) - 1:
                self._select_tag(self._selected_idx + 1)
            else:
                self._deselect()
            return True

        if self._selected_idx is not None:
            self._deselect()
        return False

    # ── Tag manipulation ──

    def _commit_text(self) -> None:
        text = self._entry.get_text().strip()
        if not text or text in self._tags:
            return
        self._add_tag(text)
        self._entry.set_text("")
        self._deselect()
        if self._on_change:
            self._on_change()

    @staticmethod
    def _make_tag_view() -> Gtk.TextView:
        tv = Gtk.TextView()
        tv.set_editable(False)
        tv.set_cursor_visible(False)
        tv.set_wrap_mode(Gtk.WrapMode.WORD)
        tv.set_top_margin(8)
        tv.set_bottom_margin(6)
        tv.set_left_margin(6)
        tv.set_right_margin(6)
        tv.set_visible(False)
        tv.add_css_class("tag-view")
        return tv

    def _add_tag(self, text: str) -> None:
        buf = self._tag_view.get_buffer()
        if self._tags:
            end = buf.get_end_iter()
            buf.insert(end, " ")
        self._tags.append(text)
        end = buf.get_end_iter()
        anchor = buf.create_child_anchor(end)
        pill = self._make_pill(text)
        self._tag_view.add_child_at_anchor(pill, anchor)
        self._pill_widgets.append(pill)
        self._tag_view.set_visible(True)
        self._entry.set_margin_top(8)

    def _remove_tag_at(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._tags):
            return
        self._tags.pop(idx)
        self._pill_widgets.pop(idx)
        self._rebuild_tags()
        if self._on_change:
            self._on_change()

    def _clear_all(self) -> None:
        self._tags.clear()
        self._pill_widgets.clear()
        self._selected_idx = None
        self._replace_tag_view()
        self._tag_view.set_visible(False)
        self._entry.set_margin_top(6)

    def _rebuild_tags(self) -> None:
        saved = list(self._tags)
        self._tags.clear()
        self._pill_widgets.clear()
        self._replace_tag_view()
        for tag in saved:
            self._add_tag(tag)
        has_tags = bool(self._tags)
        self._tag_view.set_visible(has_tags)
        self._entry.set_margin_top(8 if has_tags else 6)

    def _replace_tag_view(self) -> None:
        """Swap the tag_view for a fresh one — avoids segfault from clearing child anchors."""
        old = self._tag_view
        self._tag_view = self._make_tag_view()
        self._box.insert_child_after(self._tag_view, None)
        old.set_visible(False)
        GLib.idle_add(self._box.remove, old)

    def _make_pill(self, text: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        box.add_css_class("tag-pill")
        box.set_cursor_from_name("default")

        label = Gtk.Label(label=text)
        box.append(label)

        close_btn = Gtk.Button(icon_name="window-close-symbolic")
        close_btn.add_css_class("flat")
        close_btn.add_css_class("circular")
        close_btn.add_css_class("tag-pill-close")
        close_btn.set_cursor_from_name("default")
        close_btn.connect("clicked", lambda _b, t=text: self._on_pill_close(t))
        box.append(close_btn)

        gesture = Gtk.GestureClick()
        gesture.set_button(1)
        gesture.connect(
            "released",
            lambda _g, n, _x, _y, t=text: self._on_pill_click(n, t),
        )
        box.add_controller(gesture)

        return box

    def _on_pill_close(self, text: str) -> None:
        def do_remove():
            if text in self._tags:
                self._remove_tag_at(self._tags.index(text))
                self._deselect()
            return False

        GLib.idle_add(do_remove)

    def _on_pill_click(self, n_press: int, text: str) -> None:
        if n_press != 2:
            return

        def do_edit():
            if text in self._tags:
                self._remove_tag_at(self._tags.index(text))
                self._deselect()
                self._entry.set_text(text)
                self._entry.set_position(-1)
                self._entry.grab_focus()
            return False

        GLib.idle_add(do_edit)

    # ── Selection ──

    def _select_tag(self, idx: int) -> None:
        self._deselect()
        if idx < 0 or idx >= len(self._tags):
            return
        self._selected_idx = idx
        self._pill_widgets[idx].add_css_class("tag-pill-selected")
        self._box.grab_focus()

    def _deselect(self) -> None:
        if self._selected_idx is not None and self._selected_idx < len(
            self._pill_widgets
        ):
            self._pill_widgets[self._selected_idx].remove_css_class("tag-pill-selected")
        self._selected_idx = None
        self._entry.grab_focus()

    def _update_focus(self) -> None:
        has = self._entry.has_focus() or self._box.has_focus()
        if has:
            self._frame.add_css_class("focused")
        else:
            self._frame.remove_css_class("focused")


class ProcessWindow:
    """Builds and manages the post-processing workbench UI."""

    def __init__(
        self,
        window: Gtk.ApplicationWindow,
        session: MeetingSession,
        params: TranscribeParams | None,
        on_transcribe: Callable[[TranscribeParams], None],
        on_back: Callable[[], None] | None = None,
    ) -> None:
        self._window = window
        self._session = session
        self._on_transcribe = on_transcribe
        self._on_back = on_back
        self._destroyed = False
        self._title_save_source: int | None = None

        self._params = params or TranscribeParams()
        self._build()

        if session.has_transcript:
            self._show_done_state()

    # ── Widget construction ──

    def _build(self) -> None:
        hb = Gtk.HeaderBar()
        self._window.set_titlebar(hb)

        if self._on_back:
            back_btn = Gtk.Button(icon_name="go-previous-symbolic")
            back_btn.add_css_class("flat")
            back_btn.connect("clicked", lambda _b: self._on_back())
            hb.pack_start(back_btn)

        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._dot = Gtk.Label(label="●")
        self._dot.add_css_class("status-dot")
        self._dot.set_visible(False)
        title_box.append(self._dot)
        title_label = Gtk.Label(label="Voxize · Meeting")
        title_label.add_css_class("status-label")
        title_box.append(title_label)
        hb.set_title_widget(title_box)

        folder_btn = Gtk.Button(icon_name="folder-symbolic")
        folder_btn.add_css_class("flat")
        folder_btn.add_css_class("dim-label")
        folder_btn.connect("clicked", self._on_open_folder)
        hb.pack_end(folder_btn)

        if self._session.has_opus:
            play_btn = Gtk.Button(icon_name="media-playback-start-symbolic")
            play_btn.add_css_class("flat")
            play_btn.add_css_class("dim-label")
            play_btn.set_tooltip_text("Play recording")
            play_btn.connect("clicked", self._on_play)
            hb.pack_end(play_btn)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        content.set_margin_top(14)
        content.set_margin_bottom(14)
        content.set_margin_start(14)
        content.set_margin_end(14)

        # ── Session info ──
        info_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        date_label = Gtk.Label(
            label=_format_session_date(self._session),
        )
        date_label.add_css_class("status-label")
        date_label.set_xalign(0)
        info_row.append(date_label)

        details = []
        if self._session.duration_s is not None:
            details.append(_format_duration(int(self._session.duration_s)))
        if self._session.file_size_bytes > 0:
            details.append(_format_size(self._session.file_size_bytes))
        if details:
            detail_label = Gtk.Label(label=" · ".join(details))
            detail_label.add_css_class("timer-label")
            info_row.append(detail_label)

        content.append(info_row)

        # ── Title entry ──
        title_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        self._title_entry = Gtk.Entry()
        self._title_entry.set_placeholder_text("Meeting title…")
        self._title_entry.set_hexpand(True)
        if self._session.title:
            self._title_entry.set_text(self._session.title)
        self._title_entry.get_buffer().connect(
            "notify::text", lambda *_: self._schedule_title_save()
        )
        title_row.append(self._title_entry)

        self._generate_title_btn = Gtk.Button(
            icon_name="error-correct-symbolic",
        )
        self._generate_title_btn.add_css_class("flat")
        self._generate_title_btn.set_tooltip_text("Generate title from transcript")
        self._generate_title_btn.set_sensitive(self._session.has_transcript)
        self._generate_title_btn.connect("clicked", self._on_generate_title)
        title_row.append(self._generate_title_btn)
        content.append(title_row)

        # ── Speakers row (compact) ──
        speakers_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        speakers_label = Gtk.Label(label="Speakers")
        speakers_label.add_css_class("timer-label")
        speakers_label.set_xalign(0)
        speakers_label.set_hexpand(True)
        speakers_row.append(speakers_label)

        self._speakers_spin = Gtk.SpinButton()
        self._speakers_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._params.num_speakers,
                lower=1,
                upper=32,
                step_increment=1,
            )
        )
        self._speakers_spin.set_size_request(80, -1)
        self._speakers_spin.connect("value-changed", lambda _s: self._save_params())
        speakers_row.append(self._speakers_spin)
        content.append(speakers_row)

        # ── Key terms ──
        terms_label = Gtk.Label(label="Key terms")
        terms_label.add_css_class("timer-label")
        terms_label.set_xalign(0)
        content.append(terms_label)

        self._tag_entry = TagEntry(on_change=self._save_params)
        if self._params.keyterms:
            self._tag_entry.set_tags(self._params.keyterms)
        content.append(self._tag_entry.widget)

        hint = Gtk.Label(label="Press Enter to add (optional, +20% cost)")
        hint.add_css_class("timer-label")
        hint.set_xalign(0)
        hint.set_opacity(0.6)
        content.append(hint)

        # ── Action buttons ──
        action_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )

        self._transcribe_btn = _icon_button("document-send-symbolic", "Transcribe")
        self._transcribe_btn.add_css_class("suggested-action")
        self._transcribe_btn.set_hexpand(True)
        self._transcribe_btn.connect("clicked", self._on_transcribe_clicked)
        action_row.append(self._transcribe_btn)

        self._copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        self._copy_btn.set_sensitive(False)
        self._copy_btn.set_tooltip_text("Copy transcript")
        self._copy_btn.connect("clicked", self._on_copy_clicked)
        action_row.append(self._copy_btn)

        content.append(action_row)

        # ── Status label (visible during transcription only) ──
        self._status_label = Gtk.Label()
        self._status_label.add_css_class("timer-label")
        self._status_label.set_xalign(0)
        self._status_label.set_visible(False)
        content.append(self._status_label)

        # ── Transcript preview ──
        self._preview_frame = Gtk.Frame()
        self._preview_frame.set_visible(False)

        self._preview_view = Gtk.TextView()
        self._preview_view.set_editable(False)
        self._preview_view.set_cursor_visible(False)
        self._preview_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._preview_view.set_top_margin(8)
        self._preview_view.set_bottom_margin(8)
        self._preview_view.set_left_margin(10)
        self._preview_view.set_right_margin(10)

        preview_scroll = Gtk.ScrolledWindow()
        preview_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        preview_scroll.set_child(self._preview_view)
        preview_scroll.set_min_content_height(120)
        preview_scroll.set_max_content_height(300)
        preview_scroll.set_propagate_natural_height(True)

        self._preview_frame.set_child(preview_scroll)
        content.append(self._preview_frame)

        # ── Speaker rename (collapsible fieldset) ──
        self._rename_frame = Gtk.Frame()
        self._rename_frame.add_css_class("rename-section")
        self._rename_frame.set_visible(False)

        rename_label = Gtk.Label(label="Rename speakers")
        rename_label.add_css_class("timer-label")
        self._rename_expander = Gtk.Expander()
        self._rename_expander.set_label_widget(rename_label)
        self._rename_expander.connect("notify::expanded", self._on_rename_expanded)

        self._rename_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._rename_box.set_margin_top(12)
        self._rename_box.set_margin_bottom(4)
        self._rename_entries: dict[str, Gtk.Entry] = {}
        self._rename_apply_btn: Gtk.Button | None = None
        self._rename_expander.set_child(self._rename_box)
        self._rename_frame.set_child(self._rename_expander)
        content.append(self._rename_frame)

        # ── Error bar ──
        self._error_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        self._error_bar.add_css_class("error-bar")
        self._error_bar.set_visible(False)

        err_icon = Gtk.Label(label="⚠")
        err_icon.add_css_class("error-icon")
        self._error_bar.append(err_icon)

        self._error_label = Gtk.Label()
        self._error_label.add_css_class("error-message")
        self._error_label.set_wrap(True)
        self._error_label.set_hexpand(True)
        self._error_label.set_xalign(0)
        self._error_bar.append(self._error_label)
        content.append(self._error_bar)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(True)
        scroll.set_child(content)
        self._window.set_child(scroll)
        self._transcribe_btn.grab_focus()

    # ── State transitions ──

    def _show_done_state(self) -> None:
        self._dot.set_visible(True)
        self._dot.add_css_class("ready")
        self._copy_btn.set_sensitive(True)
        self._set_transcribe_label("Re-transcribe")
        self._load_preview()
        self._rename_frame.set_visible(True)

    def mark_transcribing(self) -> None:
        if self._destroyed:
            return
        self._dot.set_visible(True)
        for cls in ("ready", "cleaning"):
            self._dot.remove_css_class(cls)
        self._dot.add_css_class("transcribing")
        self._status_label.set_text("Downmixing…")
        self._status_label.set_visible(True)
        self._transcribe_btn.set_sensitive(False)
        self._copy_btn.set_sensitive(False)
        self._generate_title_btn.set_sensitive(False)
        self._speakers_spin.set_sensitive(False)
        self._tag_entry.set_sensitive(False)
        self._error_bar.set_visible(False)
        self._preview_frame.set_visible(False)
        self._rename_expander.set_expanded(False)
        self._rename_frame.set_visible(False)

    def update_transcribe_elapsed(self, phase: str, seconds: float) -> bool:
        if self._destroyed:
            return False
        elapsed = _format_duration(int(seconds))
        if phase == "downmix":
            self._status_label.set_text(f"Downmixing… {elapsed}")
        else:
            text = f"Transcribing… {elapsed}"
            if self._session.duration_s:
                est = int(self._session.duration_s * 0.1) + 15
                text += f"  (est. ~{_format_duration(est)})"
            self._status_label.set_text(text)
        return False

    def mark_transcribe_done(self, result: TranscribeResult) -> None:
        if self._destroyed:
            return
        for cls in ("transcribing", "cleaning"):
            self._dot.remove_css_class(cls)
        self._dot.add_css_class("ready")
        self._copy_btn.set_sensitive(True)
        self._generate_title_btn.set_sensitive(True)
        self._transcribe_btn.set_sensitive(True)
        self._set_transcribe_label("Re-transcribe")
        self._speakers_spin.set_sensitive(True)
        self._tag_entry.set_sensitive(True)

        elapsed = _format_duration(int(result.elapsed_s))
        duration = ""
        if result.audio_duration_s:
            duration = f" · {_format_duration(int(result.audio_duration_s))} audio"
        self._status_label.set_text(f"Transcribed in {elapsed}{duration}")

        self._load_preview()
        self._rename_frame.set_visible(True)

        if not self._title_entry.get_text().strip():
            self._on_generate_title(None)

    def mark_transcribe_idle(self) -> None:
        if self._destroyed:
            return
        self._dot.set_visible(False)
        for cls in ("ready", "transcribing", "cleaning"):
            self._dot.remove_css_class(cls)
        self._status_label.set_visible(False)
        self._transcribe_btn.set_sensitive(True)
        self._set_transcribe_label("Transcribe")
        self._speakers_spin.set_sensitive(True)
        self._tag_entry.set_sensitive(True)

    def show_error(self, message: str) -> None:
        if self._destroyed:
            return
        self._error_label.set_text(message)
        self._error_bar.set_visible(True)

    def destroy(self) -> None:
        self._destroyed = True
        if self._title_save_source is not None:
            GLib.source_remove(self._title_save_source)
            self._title_save_source = None
            save_title(self._session.path, self._title_entry.get_text())

    def _set_transcribe_label(self, text: str) -> None:
        icon = (
            "view-refresh-symbolic"
            if text == "Re-transcribe"
            else "document-send-symbolic"
        )
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        box.append(Gtk.Image.new_from_icon_name(icon))
        box.append(Gtk.Label(label=text))
        self._transcribe_btn.set_child(box)

    # ── Transcript preview ──

    def _load_preview(self) -> None:
        transcript_path = os.path.join(self._session.path, "transcript.txt")
        try:
            with open(transcript_path) as f:
                text = f.read()
        except OSError:
            return
        if not text.strip():
            return
        buf = self._preview_view.get_buffer()
        buf.set_text(text)
        self._preview_frame.set_visible(True)

    # ── Speaker rename ──

    def _on_rename_expanded(self, expander: Gtk.Expander, _pspec) -> None:
        if expander.get_expanded():
            self._populate_rename_rows()

    def _populate_rename_rows(self) -> None:
        self._rename_entries.clear()
        child = self._rename_box.get_first_child()
        while child is not None:
            sibling = child.get_next_sibling()
            self._rename_box.remove(child)
            child = sibling

        speakers = self._parse_speakers()
        if not speakers:
            empty = Gtk.Label(label="No speakers found in transcript")
            empty.add_css_class("timer-label")
            empty.set_xalign(0)
            self._rename_box.append(empty)
            self._rename_apply_btn = None
            return

        for name in speakers:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            label = Gtk.Label(label=name)
            label.add_css_class("rename-original")
            label.set_xalign(0)
            label.set_size_request(100, -1)
            row.append(label)

            arrow = Gtk.Label(label="→")
            arrow.add_css_class("timer-label")
            row.append(arrow)

            entry = Gtk.Entry()
            entry.set_text(name)
            entry.set_hexpand(True)
            entry.get_buffer().connect(
                "notify::text", lambda *_: self._check_rename_dirty()
            )
            row.append(entry)

            self._rename_entries[name] = entry
            self._rename_box.append(row)

        self._rename_apply_btn = Gtk.Button(label="Apply")
        self._rename_apply_btn.set_margin_top(6)
        self._rename_apply_btn.set_sensitive(False)
        self._rename_apply_btn.connect("clicked", self._on_apply_rename)
        self._rename_box.append(self._rename_apply_btn)

    def _parse_speakers(self) -> list[str]:
        transcript_path = os.path.join(self._session.path, "transcript.txt")
        try:
            with open(transcript_path) as f:
                text = f.read()
        except OSError:
            return []
        seen: set[str] = set()
        ordered: list[str] = []
        for m in _TIMESTAMP_LINE_RE.finditer(text):
            name = m.group(2)
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        return ordered

    def _check_rename_dirty(self) -> None:
        if self._rename_apply_btn is None:
            return
        has_change = False
        for original, entry in self._rename_entries.items():
            text = entry.get_text().strip()
            if not text:
                self._rename_apply_btn.set_sensitive(False)
                return
            if text != original:
                has_change = True
        self._rename_apply_btn.set_sensitive(has_change)

    def _on_apply_rename(self, _btn: Gtk.Button) -> None:
        transcript_path = os.path.join(self._session.path, "transcript.txt")
        try:
            with open(transcript_path) as f:
                text = f.read()
        except OSError:
            self.show_error("Could not read transcript.txt")
            return

        rename_map: dict[str, str] = {}
        for original, entry in self._rename_entries.items():
            new_name = entry.get_text().strip()
            if new_name and new_name != original:
                rename_map[original] = new_name
        if not rename_map:
            return

        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        backup_path = os.path.join(self._session.path, f"transcript.{stamp}.txt")
        try:
            with open(backup_path, "w") as f:
                f.write(text)
        except OSError:
            self.show_error("Could not create transcript backup")
            return

        def _replace(m):
            prefix, speaker, trailing = m.group(1), m.group(2), m.group(3)
            return f"{prefix}[{rename_map.get(speaker, speaker)}]{trailing}"

        new_text = _TIMESTAMP_LINE_RE.sub(_replace, text)

        tmp = transcript_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(new_text)
            os.replace(tmp, transcript_path)
        except OSError:
            self.show_error("Could not write transcript.txt")
            return

        self._load_preview()
        self._populate_rename_rows()

    def _schedule_title_save(self) -> None:
        if self._title_save_source is not None:
            GLib.source_remove(self._title_save_source)
        self._title_save_source = GLib.timeout_add(
            _TITLE_DEBOUNCE_MS, self._flush_title
        )

    def _flush_title(self) -> bool:
        self._title_save_source = None
        save_title(self._session.path, self._title_entry.get_text())
        return GLib.SOURCE_REMOVE

    def _save_params(self) -> None:
        data = json.dumps(
            {
                "num_speakers": int(self._speakers_spin.get_value()),
                "keyterms": self._tag_entry.get_tags(),
                "language_code": "eng",
            },
            indent=2,
        )
        path = os.path.join(self._session.path, "transcribe_params.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, path)

    # ── Button handlers ──

    def _on_transcribe_clicked(self, _btn: Gtk.Button) -> None:
        num_speakers = int(self._speakers_spin.get_value())
        keyterms = self._tag_entry.get_tags()

        params = TranscribeParams(
            num_speakers=num_speakers,
            keyterms=keyterms,
        )

        dialog = Adw.AlertDialog(
            heading="Transcribe recording?",
            body="The recording will be uploaded to ElevenLabs for transcription.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("transcribe", "Transcribe")
        dialog.set_response_appearance("transcribe", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("transcribe")
        dialog.set_close_response("cancel")

        def on_response(_dialog, result):
            try:
                response = _dialog.choose_finish(result)
            except GLib.Error:
                return
            if response == "transcribe":
                self._error_bar.set_visible(False)
                self._on_transcribe(params)

        dialog.choose(self._window, None, on_response)

    def _on_generate_title(self, _btn: Gtk.Button) -> None:
        self._generate_title_btn.set_sensitive(False)
        self._title_entry.set_text("Generating…")
        self._title_entry.set_sensitive(False)

        import threading

        from voxize.meeting.titling import generate_title

        date_str = _format_session_date(self._session)

        def _run():
            try:
                title = generate_title(self._session.path, date_str)
                GLib.idle_add(self._on_title_generated, title)
            except Exception as e:
                logger.debug("generate_title failed", exc_info=True)
                GLib.idle_add(self._on_title_generated, None, str(e))

        threading.Thread(target=_run, daemon=True, name="meeting-title").start()

    def _on_title_generated(self, title: str | None, error: str | None = None) -> bool:
        if self._destroyed:
            return False
        self._title_entry.set_sensitive(True)
        self._generate_title_btn.set_sensitive(True)
        if title:
            self._title_entry.set_text(title)
            self._title_entry.set_position(-1)
        else:
            self._title_entry.set_text("")
            if error:
                self.show_error(f"Title generation failed: {error}")
        return False

    def _on_copy_clicked(self, _btn: Gtk.Button) -> None:
        transcript_path = os.path.join(self._session.path, "transcript.txt")
        try:
            with open(transcript_path) as f:
                text = f.read()
        except OSError:
            self.show_error("Could not read transcript.txt")
            return
        clipboard_copy(text)
        self._copy_btn.set_tooltip_text("Copied!")
        GLib.timeout_add(
            2000, lambda: self._copy_btn.set_tooltip_text("Copy transcript") or False
        )

    def _on_play(self, _btn: Gtk.Button) -> None:
        opus_path = os.path.join(self._session.path, "recording.opus")
        try:
            uri = GLib.filename_to_uri(opus_path, None)
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception:
            logger.debug("play recording failed", exc_info=True)

    def _on_open_folder(self, _btn: Gtk.Button) -> None:
        try:
            uri = GLib.filename_to_uri(self._session.path, None)
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception:
            logger.debug("open folder failed", exc_info=True)


# ── Helpers ──


def _icon_button(icon_name: str, label: str) -> Gtk.Button:
    """Create a button with an icon + text label."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    box.set_halign(Gtk.Align.CENTER)
    box.append(Gtk.Image.new_from_icon_name(icon_name))
    box.append(Gtk.Label(label=label))
    btn = Gtk.Button()
    btn.set_child(box)
    return btn


def _format_session_date(session: MeetingSession) -> str:
    return session.created.strftime("%-d %b %Y %H:%M")


def _format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _format_size(n_bytes: int) -> str:
    if n_bytes < 1024 * 1024:
        return f"{n_bytes / 1024:.0f} KB"
    return f"{n_bytes / (1024 * 1024):.1f} MB"
