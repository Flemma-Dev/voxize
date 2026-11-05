"""The terminal card's widgets. They draw a voxize.tui.card.Card, nothing else."""

from __future__ import annotations

from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.renderables.bar import Bar
from textual.widget import Widget
from textual.widgets import ProgressBar, Rule, Static

from voxize.protocol import State
from voxize.tui.card import Card, format_timer

_TONES = ("idle", "recording", "working", "ready", "error")
_ZONES = ("quiet", "good", "hot", "clip")


class LevelBar(Widget):
    """Mic level as a thin line: the line the stock ProgressBar draws.

    The filled part takes the colour of the zone-* class, the rest is the
    levelbar--track colour — the progress bar's track, so the two strips
    look the same, as in the GUI.
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {"levelbar--track"}

    fraction: reactive[float] = reactive(0.0)

    def render(self) -> Bar:
        track = self.get_component_rich_style("levelbar--track")
        return Bar(
            highlight_range=(0, self.size.width * self.fraction),
            highlight_style=Style(color=self.rich_style.color),
            background_style=Style(color=track.color),
        )


class KeyHints(Widget):
    """``Enter Stop · Esc Cancel``: each key in keyhints--key, its action muted."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"keyhints--key"}

    keys: reactive[tuple[tuple[str, str], ...]] = reactive(())

    def render(self) -> Text:
        key_style = self.get_component_rich_style("keyhints--key", partial=True)
        hints = Text()
        for i, (key, action) in enumerate(self.keys):
            if i:
                hints.append(" · ")
            hints.append(key, style=key_style)
            hints.append(f" {action}")
        return hints


class CardView(Vertical):
    """Header, transcript, notice, status row, level/progress strip, key hints.

    As in the GUI: the mode on top, the status at the bottom with the strip.
    """

    def __init__(self, card: Card, **kwargs) -> None:
        super().__init__(**kwargs)
        self._card = card

    def compose(self) -> ComposeResult:
        # markup=False everywhere: transcripts and errors are plain text, and
        # a "[" in them must not be parsed as markup.
        with Horizontal(id="header"):
            yield Static("Dictation", id="title", markup=False)
            yield Static(id="vocab", markup=False)
        yield Rule(id="rule")
        with VerticalScroll(id="scroll"):
            yield Static(id="transcript", markup=False)
        yield Static(id="notice", markup=False)
        with Horizontal(id="statusbar"):
            yield Static(id="dot", markup=False)
            yield Static(id="status", markup=False)
            yield Static(id="timer", markup=False)
            # Here, not in the header: as the GUI's tooltip on the status dot,
            # and nothing in the header moves when it appears at READY.
            yield Static(id="cost", markup=False)
        # One row, kept with or without a strip in it, so the status row and
        # the keys never move between states.
        with Container(id="strip"):
            yield LevelBar(id="level")
            yield ProgressBar(
                id="progress", total=1.0, show_eta=False, show_percentage=False
            )
        yield KeyHints(id="keys")

    def on_mount(self) -> None:
        self.show(self._card)

    def show(self, card: Card) -> None:
        dot = self.query_one("#dot", Static)
        dot.update(card.dot)
        for tone in _TONES:
            dot.set_class(tone == card.tone, f"tone-{tone}")
        self.query_one("#status", Static).update(card.status)

        vocab = self.query_one("#vocab", Static)
        vocab.display = card.vocab_visible
        vocab.update(f"≡ {len(card.sources)}")

        cost = self.query_one("#cost", Static)
        cost.display = card.state == State.READY and card.cost is not None
        cost.update(f"${card.cost:.4f}" if card.cost is not None else "")

        timer = self.query_one("#timer", Static)
        timer.display = card.timer_visible
        timer.update(format_timer(card.timer_seconds))
        timer.set_class(card.timer_dim, "-dim")

        transcript = self.query_one("#transcript", Static)
        if str(transcript.content) != card.text:
            transcript.update(card.text)
            scroll = self.query_one("#scroll", VerticalScroll)
            self.call_after_refresh(scroll.scroll_end, animate=False)
        transcript.set_class(card.dimmed, "-dimmed")

        notices = [n for n in (card.notice, card.clipboard_notice) if n]
        notice = self.query_one("#notice", Static)
        notice.display = bool(notices)
        notice.update("\n".join(notices))

        level = self.query_one(LevelBar)
        level.display = card.strip == "level"
        level.fraction = card.level
        for zone in _ZONES:
            level.set_class(zone == card.zone, f"zone-{zone}")

        progress = self.query_one(ProgressBar)
        progress.display = card.strip == "progress"
        if card.progress is None:
            progress.update(total=None)  # indeterminate: no estimate yet
        else:
            progress.update(total=1.0, progress=card.progress)

        self.query_one(KeyHints).keys = tuple(card.keys)
