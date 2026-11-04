"""A frontend's copy of the backend's session state.

Reads like voxize.state.StateMachine — ``state``, ``error_message``,
``on_change(cb)`` with ``cb(machine, old, new)``. Transitions come from
``state`` events: a frontend never decides one, except ERROR when the
backend is unusable (voxize.frontend.client). READY, CANCELLED and ERROR are
terminal here as in the backend: later transitions are refused.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from voxize.protocol import State

logger = logging.getLogger(__name__)

TERMINAL = frozenset({State.READY, State.CANCELLED, State.ERROR})


class ClientState:
    def __init__(self) -> None:
        self._state = State.INITIALIZING
        self._listeners: list[Callable] = []
        self.error_message = ""

    @property
    def state(self) -> State:
        return self._state

    def on_change(self, callback: Callable) -> None:
        """Register callback(machine, old_state, new_state)."""
        self._listeners.append(callback)

    def apply(self, new: State, error: str = "") -> None:
        old = self._state
        if new == old:
            return
        if old in TERMINAL:
            # Terminal means terminal, as in the backend's machine: a late
            # event must not pull the frontend off READY or an error card.
            logger.debug(
                "state: %s -> %s refused, already terminal", old.name, new.name
            )
            return
        if new == State.ERROR:
            self.error_message = error
        self._state = new
        logger.debug("state: %s -> %s", old.name, new.name)
        for cb in list(self._listeners):
            try:
                cb(self, old, new)
            except Exception:
                logger.exception("state listener failed")
