"""The client rules every frontend must get right, whatever its toolkit.

A frontend owns drawing and interaction; this owns the few decisions about
the backend connection itself, so the GTK window and the terminal client
make them the same way:

- ``hello`` must arrive, with our protocol version, or the session is ERROR;
- a frontend-decided failure sticks: later backend events are dropped so
  nothing replaces the error;
- READY, CANCELLED and ERROR are final (voxize.frontend.state);
- stdout EOF after a terminal state, or after close, is expected; before
  that the backend was lost;
- ``close`` is sent once.

The transport is anything with ``send(type_, **fields)``, ``finish()`` and
``close_stdin()`` (voxize.frontend.pipe.BackendPipe and its subclasses). It
may be attached after construction. The hello deadline (HELLO_TIMEOUT_S) is
scheduled by the frontend, which calls ``check_hello()`` when it fires.
"""

from __future__ import annotations

import logging
from typing import Protocol

from voxize import protocol
from voxize.frontend.state import TERMINAL, ClientState
from voxize.protocol import State

logger = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 5


class Transport(Protocol):
    def send(self, type_: str, **fields) -> None: ...

    def finish(self) -> None: ...

    def close_stdin(self) -> None: ...


class ClientCore:
    def __init__(self, transport: Transport | None = None, *, client_noun: str) -> None:
        self.transport = transport
        self.state = ClientState()
        self.state.on_change(self._on_state_change)
        self._noun = client_noun
        self.hello = False
        # Set once the frontend has decided the backend is unusable.
        self.failed = False
        # Set once the frontend is leaving (close sent, or stdin hung up).
        self.closed = False

    def send(self, type_: str, **fields) -> None:
        if self.transport is not None:
            self.transport.send(type_, **fields)

    def handle(self, msg: dict) -> dict | None:
        """Apply one backend event.

        Returns the event for the frontend to render, or None when it was
        consumed here (``hello``, ``state``) or must be dropped (after a
        frontend-decided failure). State changes reach the frontend through
        ``self.state.on_change``.
        """
        kind = msg["type"]
        if self.failed:
            logger.debug("handle: %s ignored, backend already failed", kind)
            return None
        if kind == "hello":
            self.hello = True
            if msg.get("protocol") != protocol.PROTOCOL_VERSION:
                self.fail(
                    f"Backend speaks protocol {msg.get('protocol')}, "
                    f"{self._noun} expects {protocol.PROTOCOL_VERSION}"
                )
            return None
        if kind == "state":
            self.state.apply(State[msg["state"]], msg.get("error") or "")
            return None
        return msg

    def fail(self, message: str) -> None:
        """The frontend's one self-decided transition: the backend can't be used."""
        logger.warning("fail: %s", message)
        self.failed = True
        if self.state.state not in TERMINAL:
            self.state.apply(State.ERROR, error=message)
        self.close(trash_if_empty=False)

    def backend_lost(self, last_stderr_line: str) -> None:
        detail = f": {last_stderr_line}" if last_stderr_line else ""
        self.fail(f"Backend exited unexpectedly{detail}")

    def check_hello(self) -> None:
        """The frontend's hello deadline fired."""
        if not self.hello and not self.closed:
            self.fail("Backend did not start")

    def close(self, *, trash_if_empty: bool) -> None:
        """The frontend is leaving: send ``close`` once and hang up.

        The backend cancels anything in flight, tears down and exits. It
        trashes the session folder when ``trash_if_empty`` is set and no
        transcript was ever produced.
        """
        if self.closed:
            return
        self.closed = True
        if self.transport is None:
            return
        self.transport.finish()
        self.transport.send("close", trash_if_empty=trash_if_empty)
        self.transport.close_stdin()

    def hang_up(self) -> None:
        """Close the backend's stdin without ``close`` (signals).

        The backend treats stdin EOF as cancel + close: it finalizes the WAV,
        releases the mic lock and restores ducked volumes itself.
        """
        self.closed = True
        if self.transport is not None:
            self.transport.finish()
            self.transport.close_stdin()

    def _on_state_change(self, _state, _old: State, new: State) -> None:
        if new in TERMINAL and self.transport is not None:
            # The backend stays up until close; EOF from here is not "lost".
            self.transport.finish()
