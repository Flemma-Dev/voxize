"""The TUI card's presentation model. No Textual, no I/O.

Backend events and state changes go in; what the card shows comes out as
plain attributes that voxize.tui.widgets draws. The rules and strings are
the GTK window's (voxize/gui/ui.py), ported rather than imported: they are
presentation, and each frontend owns its own.
"""

from __future__ import annotations

from voxize.protocol import State

PLACEHOLDER = "Waiting for audio"
NO_SPEECH = "No speech detected."
NO_PREVIEW = "No live preview."
NOTICE_PREVIEW_LOST = "Live preview disconnected — still recording."
NOTICE_BATCH_FAILED = "Transcription failed — showing the live preview."
NOTICE_BATCH_FAILED_EMPTY = (
    "Transcription failed — the recording is saved in the session folder."
)
NOTICE_CLEANUP_FAILED = "Cleanup failed — showing the uncleaned transcript."
NOTICE_NO_CLIPBOARD = "Clipboard unavailable — the transcript is printed on exit."

# Zone boundaries as fractions of the [-60, 0] dBFS range, unchanged from
# gui/ui.py: on RMS dBFS they land at -40.2 / -12 / -3.
_ZONE_GOOD = 0.33
_ZONE_HOT = 0.80
_ZONE_CLIP = 0.95

AUDIO = (State.WARMING, State.RECORDING)
WORKING = (State.TRANSCRIBING, State.CLEANING)
# From RECORDING on, cancelling throws away recorded audio, so it asks first.
CONFIRM_CANCEL = (State.RECORDING, State.TRANSCRIBING, State.CLEANING)
IN_FLIGHT = (State.INITIALIZING, State.WARMING, *CONFIRM_CANCEL)


def dbfs_to_fraction(dbfs: float) -> float:
    """Map dBFS [-60, 0] to fraction [0, 1] for the level meter."""
    return max(0.0, min(1.0, (dbfs + 60.0) / 60.0))


def level_zone(fraction: float) -> str:
    """The mic-gain zone of a level fraction: quiet, good, hot or clip."""
    if fraction > _ZONE_CLIP:
        return "clip"
    if fraction > _ZONE_HOT:
        return "hot"
    if fraction > _ZONE_GOOD:
        return "good"
    return "quiet"


def format_timer(seconds: int) -> str:
    """``mm:ss`` up to 59:59, then ``h:mm:ss``."""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class Card:
    def __init__(self) -> None:
        self.state = State.INITIALIZING
        self.tone = "idle"
        self.status = "Starting…"
        self.timer_seconds = 0
        self.timer_visible = False
        self.timer_dim = False
        self.text = ""
        self.dimmed = False
        # True when the text is transcript worth copying — not the
        # placeholder, "No speech detected." or an error message.
        self.copyable = False
        self.notice = ""
        self.clipboard_notice = ""
        self.strip: str | None = None
        self.level = 0.0
        self.zone = "quiet"
        self.progress: float | None = 0.0  # None: no estimate, indeterminate
        self.stalled = False
        self.sources: list[dict] = []
        self.cost: float | None = None
        self.cost_parts: list[tuple[str, float]] = []
        self._had_first_text = False
        self._preview_error: str | None = None
        self._failure: tuple[str, str, str | None] | None = None

    # ── Derived ──

    @property
    def dot(self) -> str:
        return "○" if self.tone == "idle" else "●"

    @property
    def vocab_visible(self) -> bool:
        return bool(self.sources) and self.state != State.ERROR

    @property
    def costs_visible(self) -> bool:
        return self.state == State.READY and self.cost is not None

    @property
    def deliverable(self) -> str | None:
        """What Copy would take now, and what is printed on exit."""
        if self.state == State.READY and self.copyable and self.text:
            return self.text
        return None

    @property
    def keys(self) -> list[tuple[str, str]]:
        """(key, action) hints, capitalised as the GUI's buttons are."""
        s = self.state
        if s in AUDIO:
            keys = [("Enter", "Stop"), ("Esc", "Cancel")]
        elif s == State.INITIALIZING or s in WORKING:
            keys = [("Esc", "Cancel")]
        elif s == State.READY and self.deliverable:
            keys = [("Enter", "Copy & Exit"), ("C", "Copy"), ("Q", "Exit")]
        elif s in (State.READY, State.ERROR):
            keys = [("Enter", "Exit"), ("Q", "Exit")]
        else:
            keys = []
        if self.costs_visible:
            keys.append(("$", "Costs"))
        if self.vocab_visible:
            keys.append(("V", "Vocabulary"))
        return keys

    # ── State ──

    def on_state(self, new: State, error: str = "") -> None:
        old, self.state = self.state, new
        if new not in WORKING:
            self.stalled = False
        if old in AUDIO and new not in AUDIO:
            self.level = 0.0
            self.zone = "quiet"
            # The preview-lost notice is about capture; it ends with it.
            self._preview_error = None
            self.notice = ""

        if new == State.WARMING:
            self.tone, self.status = "idle", "Warming up…"
            self._preview_error = None
            self._failure = None
            self.notice = ""
            self.timer_seconds = 0
            self.timer_visible, self.timer_dim = True, True
            self._show_placeholder()
            self.strip = "level"
        elif new == State.RECORDING:
            self.tone, self.status = "recording", "Listening"
            if old != State.WARMING:  # mock mode skips WARMING
                self._failure = None
                self.timer_seconds = 0
            if not self._had_first_text:
                self._show_placeholder()
            if self._preview_error is not None:
                # Failed while warming: say so now the session is live.
                self.dimmed = True
                self.notice = NOTICE_PREVIEW_LOST
            self.timer_visible, self.timer_dim = True, False
            self.strip = "level"
        elif new == State.TRANSCRIBING:
            self.tone, self.status = "working", "Working…"
            self.timer_visible, self.timer_dim = True, True
            self._had_first_text = False
            self.dimmed = True
            self.progress = 0.0
            self.strip = "progress"
        elif new == State.CLEANING:
            # Same look as TRANSCRIBING, label included: a stall that began
            # in the batch pass is still a stall.
            self.tone = "working"
            self.timer_visible, self.timer_dim = True, True
        elif new == State.READY:
            self.tone, self.status = "ready", "Ready"
            self.strip = None
            self.timer_visible, self.timer_dim = True, True
            if self._failure is not None:
                self._apply_failure()
            else:
                self.notice = ""
                if self.copyable:
                    self.dimmed = False
        elif new == State.CANCELLED:
            self.strip = None
        elif new == State.ERROR:
            self.tone, self.status = "error", "Error"
            self.strip = None
            self.timer_visible = False
            self.notice = ""
            self._set_text(error, copyable=False, dimmed=False)

    def tick(self) -> None:
        """One second of recording (the app ticks only while RECORDING)."""
        if self.state == State.RECORDING:
            self.timer_seconds += 1

    # ── Events ──

    def on_text(self, stage: str, op: str, text: str) -> None:
        """Transcript text. Text about to be superseded is dimmed: the live
        preview once the batch pass runs, the batch transcript in cleanup."""
        self._had_first_text = True
        if op == "replace":
            self.text = ""
        self.dimmed = (self.state == State.TRANSCRIBING and stage == "live") or (
            self.state == State.CLEANING and stage == "batch"
        )
        self.copyable = True
        self.text += text

    def on_no_speech(self) -> None:
        self._had_first_text = True
        self._set_text(NO_SPEECH, copyable=False, dimmed=True)

    def on_preview_lost(self, reason: str) -> None:
        self._preview_error = reason
        if self.state in AUDIO:
            self.dimmed = True
            self.notice = NOTICE_PREVIEW_LOST

    def on_failure(self, phase: str, message: str, fallback: str | None) -> None:
        """Arm the READY notice. The fallback text itself arrived as text."""
        self._failure = (phase, message, fallback)

    def on_level(self, dbfs: float) -> None:
        if self.state in AUDIO:
            self.level = dbfs_to_fraction(dbfs)
            self.zone = level_zone(self.level)

    def on_progress(self, fraction: float | None) -> None:
        if self.state in WORKING:
            self.progress = fraction

    def on_stalled(self, stalled: bool) -> None:
        if self.state in WORKING and stalled != self.stalled:
            self.stalled = stalled
            self.status = "Still working…" if stalled else "Working…"

    def on_context(self, sources: list[dict]) -> None:
        self.sources = list(sources)

    def on_costs(
        self, live: float | None, batch: float | None, cleanup: float | None
    ) -> None:
        self.cost_parts = [
            (label, cost)
            for label, cost in (("Live", live), ("Batch", batch), ("Cleanup", cleanup))
            if cost is not None
        ]
        known = [cost for _, cost in self.cost_parts]
        self.cost = sum(known) if known else None

    def set_clipboard_unavailable(self) -> None:
        self.clipboard_notice = NOTICE_NO_CLIPBOARD

    # ── Internals ──

    def _show_placeholder(self) -> None:
        self._had_first_text = False
        self._set_text(PLACEHOLDER, copyable=False, dimmed=True)

    def _set_text(self, text: str, *, copyable: bool, dimmed: bool) -> None:
        self.text = text
        self.copyable = copyable
        self.dimmed = dimmed

    def _apply_failure(self) -> None:
        """On READY: explain why the fallback text is what's on screen."""
        phase, _message, fallback = self._failure
        if phase == "batch":
            if fallback is not None:
                self.dimmed = False
                self.notice = NOTICE_BATCH_FAILED
            else:
                self._set_text(NO_PREVIEW, copyable=False, dimmed=True)
                self.notice = NOTICE_BATCH_FAILED_EMPTY
        else:
            # The full batch transcript is on screen — the same text the
            # clipboard already holds, never a half-streamed cleanup.
            self.dimmed = False
            self.notice = NOTICE_CLEANUP_FAILED
