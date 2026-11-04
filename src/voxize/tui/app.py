"""Voxize terminal client — a protocol client of voxize.backend, on Textual.

The card, keys and clipboard live here. Everything else (audio, the three
transcription phases, progress estimation, costs) runs in the backend
process spawned by voxize.tui.__main__ and arrives as protocol events
(voxize.protocol). The card's rules and strings are in voxize.tui.card.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.timer import Timer

from voxize.frontend.client import HELLO_TIMEOUT_S, ClientCore, Transport
from voxize.frontend.session_log import SessionLog
from voxize.protocol import State
from voxize.tui.card import Card
from voxize.tui.deliver import Clipboard
from voxize.tui.widgets import CardView

logger = logging.getLogger(__name__)

# Builds the transport once the app's event loop runs: (on_event, on_lost).
TransportFactory = Callable[[Callable[[dict], None], Callable[[str], None]], Transport]


class CardScreen(Screen):
    def __init__(self, card: Card) -> None:
        super().__init__()
        self._card = card

    def compose(self) -> ComposeResult:
        yield CardView(self._card, id="card")


class VoxizeTui(App[str | None]):
    """``run()`` returns the transcript to print on exit, or None."""

    CSS_PATH = "tui.tcss"
    ENABLE_COMMAND_PALETTE = False

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

    # ── Clipboard ──

    async def _copy(self, text: str) -> None:
        await self.copier.copy(text)
        if self.copier.unavailable and not self.card.clipboard_notice:
            self.card.set_clipboard_unavailable()
            self._render()
