"""Voxize terminal client — a protocol client of voxize.backend, on Textual.

The card, keys and clipboard live here. Everything else (audio, the three
transcription phases, progress estimation, costs) runs in the backend
process spawned by voxize.tui.__main__ and arrives as protocol events
(voxize.protocol). The card's rules and strings are in voxize.tui.card.

Differences from the GTK window (voxize/gui/app.py): Enter at READY copies
and exits, and there is no auto-close, meeting switch or folder key.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import Button, Static

from voxize.frontend.client import HELLO_TIMEOUT_S, ClientCore, Transport
from voxize.frontend.session_log import SessionLog
from voxize.protocol import State
from voxize.tui.card import CONFIRM_CANCEL, IN_FLIGHT, Card
from voxize.tui.deliver import Clipboard
from voxize.tui.widgets import CardView

logger = logging.getLogger(__name__)

# Builds the transport once the app's event loop runs: (on_event, on_lost).
TransportFactory = Callable[[Callable[[dict], None], Callable[[str], None]], Transport]
_DONE = (State.READY, State.ERROR)


class ConfirmCancel(ModalScreen[bool]):
    """Discard this dictation? Keep is focused, and Escape means Keep."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "keep", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Discard this dictation?", id="confirm-title", markup=False)
            yield Static("The audio stays in the session folder.", markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button("Keep", id="keep")
                yield Button("Discard", id="discard", variant="error")

    def on_mount(self) -> None:
        self.query_one("#keep", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "discard")

    def action_keep(self) -> None:
        self.dismiss(False)


class Vocabulary(ModalScreen[None]):
    """The vocabulary the backend loaded for this session (``context``)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", show=False),
        Binding("v", "close", show=False),
        Binding("q", "close", show=False),
    ]

    def __init__(self, sources: list[dict]) -> None:
        super().__init__()
        self._sources = sources

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("Vocabulary in use", id="vocab-title", markup=False)
            for source in self._sources:
                yield Static(source["path"], classes="vocab-path", markup=False)
                yield Static(
                    source["content"].strip(), classes="vocab-content", markup=False
                )

    def action_close(self) -> None:
        self.dismiss(None)


class CardScreen(Screen):
    # On the card, not the app: a modal on top shadows these, so Enter on a
    # modal's button presses the button.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "app.primary", show=False),
        Binding("escape", "app.cancel_or_exit", show=False),
        Binding("q", "app.exit_if_done", show=False),
        Binding("v", "app.vocabulary", show=False),
    ]

    def __init__(self, card: Card) -> None:
        super().__init__()
        self._card = card

    def compose(self) -> ComposeResult:
        yield CardView(self._card, id="card")


class VoxizeTui(App[str | None]):
    """``run()`` returns the transcript to print on exit, or None."""

    CSS_PATH = "tui.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        # Textual's own ctrl+q quits and its ctrl+c only nags. Neither may
        # skip the cancel confirm, and ctrl+c copies, as in the GUI.
        Binding("ctrl+q", "ctrl_q", show=False, priority=True),
        Binding("ctrl+c", "copy", show=False, priority=True, system=True),
    ]

    def __init__(
        self,
        make_transport: TransportFactory,
        *,
        cwd: str | None,
        copier: Clipboard | None = None,
        hello_timeout_s: float = HELLO_TIMEOUT_S,
        install_signals: bool = True,
    ) -> None:
        super().__init__()
        self.session_log = SessionLog("tui.log")
        self.core = ClientCore(client_noun="this terminal client")
        self.card = Card()
        # Not "clipboard": that is Textual's own read-only App property.
        self.copier = copier or Clipboard(
            osc52=self.copy_to_clipboard, stderr_target=self.session_log.stderr_target
        )
        self.session_dir: str | None = None
        self._make_transport = make_transport
        self._cwd = cwd
        self._hello_timeout_s = hello_timeout_s
        self._install_signals = install_signals
        self._card_screen: CardScreen | None = None
        self._timer: Timer | None = None
        self._confirm: ConfirmCancel | None = None
        self._leaving = False
        self._signalled = False
        self.core.state.on_change(self._on_state_change)

    def get_default_screen(self) -> Screen:
        self._card_screen = CardScreen(self.card)
        return self._card_screen

    def on_mount(self) -> None:
        self.core.transport = self._make_transport(
            self.on_backend_event, self.core.backend_lost
        )
        self.core.send("start", cwd=self._cwd, glossaries=[])
        self.set_timer(self._hello_timeout_s, self.core.check_hello)
        if self._install_signals:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                loop.add_signal_handler(sig, self._on_signal, sig)

    # ── Backend events ──

    def on_backend_event(self, msg: dict) -> None:
        msg = self.core.handle(msg)
        if msg is None:
            return  # hello/state are the core's; state changes render below
        kind = msg["type"]
        card = self.card
        if kind == "context":
            card.on_context(msg["sources"])
        elif kind == "session":
            self.session_dir = msg["dir"]
            self.session_log.attach(msg["dir"])
        elif kind == "level":
            card.on_level(msg["dbfs"])
        elif kind == "text":
            card.on_text(msg["stage"], msg["op"], msg["text"])
        elif kind == "preview_lost":
            logger.info("preview_lost: %s", msg["reason"])
            card.on_preview_lost(msg["reason"])
        elif kind == "progress":
            card.on_progress(msg["fraction"])
        elif kind == "stalled":
            card.on_stalled(msg["stalled"])
        elif kind == "result":
            # Batch first, then cleaned overwrites it; copies are serialised.
            self.run_worker(self._copy(msg["text"]), group="copy", exit_on_error=False)
        elif kind == "no_speech":
            card.on_no_speech()
        elif kind == "failure":
            logger.info(
                "failure: phase=%s fallback=%s message=%s",
                msg["phase"],
                msg["fallback"],
                msg["message"],
            )
            card.on_failure(msg["phase"], msg["message"], msg["fallback"])
        elif kind == "costs":
            card.on_costs(msg["live"], msg["batch"], msg["cleanup"])
        else:
            logger.debug("on_backend_event: unknown event type=%s", kind)
            return
        self._render()

    def _on_state_change(self, _state, _old: State, new: State) -> None:
        self.card.on_state(new, self.core.state.error_message)
        if new == State.RECORDING:
            self._start_timer()
        else:
            self._stop_timer()
        # Past cancelling (READY, ERROR, CANCELLED) the question no longer
        # applies; the backend would ignore a late cancel anyway.
        if new not in CONFIRM_CANCEL:
            self._close_confirm()
        if new == State.CANCELLED:
            self.run_worker(self._leave(), group="leave", exit_on_error=False)
        self._render()

    def _render(self) -> None:
        if self._card_screen is None:
            return
        try:
            view = self._card_screen.query_one(CardView)
        except NoMatches:
            return  # not composed yet: CardView draws the card when it mounts
        view.show(self.card)

    # ── Timer: ticks from entering RECORDING, as in the GUI ──

    def _start_timer(self) -> None:
        self._stop_timer()
        self._timer = self.set_interval(1.0, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self.card.tick()
        self._render()

    # ── Keys ──

    async def action_primary(self) -> None:
        """Enter: stop while capturing; copy and exit at READY; exit at ERROR."""
        s = self.core.state.state
        if s in (State.WARMING, State.RECORDING):
            self.core.send("stop")
        elif s == State.READY:
            text = self.card.deliverable
            if text:
                await self._copy(text)
            await self._leave()
        elif s == State.ERROR:
            await self._leave()

    async def action_cancel_or_exit(self) -> None:
        """Escape: cancel in flight (asking first from RECORDING on); exit when done."""
        s = self.core.state.state
        if s in CONFIRM_CANCEL:
            self._ask_cancel()
        elif s in IN_FLIGHT:
            self.core.send("cancel")
        elif s in _DONE:
            await self._leave()

    async def action_exit_if_done(self) -> None:
        """q: exit at READY or ERROR; ignored while a dictation is in flight."""
        if self.core.state.state in _DONE:
            await self._leave()

    async def action_ctrl_q(self) -> None:
        """Ctrl+Q behaves as Escape on the card; inside a modal it does nothing."""
        if isinstance(self.screen, ModalScreen):
            return
        await self.action_cancel_or_exit()

    async def action_copy(self) -> None:
        if self.core.state.state == State.READY and (text := self.card.deliverable):
            await self._copy(text)

    def action_vocabulary(self) -> None:
        if self.card.vocab_visible:
            self.push_screen(Vocabulary(self.card.sources))

    def _ask_cancel(self) -> None:
        if self._confirm is not None:
            return
        self._confirm = ConfirmCancel()

        def answered(discard: bool | None) -> None:
            self._confirm = None
            if discard:
                self.core.send("cancel")

        self.push_screen(self._confirm, answered)

    def _close_confirm(self) -> None:
        confirm = self._confirm
        if confirm is not None and self.screen is confirm:
            confirm.dismiss(False)

    # ── Clipboard and leaving ──

    async def _copy(self, text: str) -> None:
        try:
            await self.copier.copy(text)
        except Exception:
            # Workers run with exit_on_error=False: without this the failure
            # would vanish. The card stays usable; tui.log gets the trace.
            logger.exception("copy failed")
            return
        if self.copier.unavailable and not self.card.clipboard_notice:
            self.card.set_clipboard_unavailable()
            self._render()

    async def _leave(self) -> None:
        if self._leaving:
            return
        self._leaving = True
        # exit() would cancel a copy still running; let it finish first.
        await self.copier.settle()
        if self._signalled:
            return  # a signal during the copy already hung up and exited
        self.core.close(trash_if_empty=False)
        self.exit(self.card.deliverable)

    def _on_signal(self, sig: int) -> None:
        """SIGTERM/SIGHUP/SIGINT: hand the backend EOF and quit, printing nothing.

        The backend treats stdin EOF as cancel + close: it finalizes the WAV,
        releases the mic lock and restores ducked volumes itself.
        """
        logger.info("Signal %s received, shutting down", sig)
        # A copy at exit may still be in flight: its _leave must not then
        # close the backend or exit with the transcript.
        self._signalled = True
        self._leaving = True
        self.core.hang_up()
        self.exit(None)
