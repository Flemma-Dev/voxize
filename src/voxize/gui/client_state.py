"""The GUI's copy of the backend's session state.

Reads like voxize.state.StateMachine — ``state``, ``error_message``,
``on_change(cb)`` with ``cb(machine, old, new)`` — so ui.py reads it the way
it read the real machine. Transitions come from ``state`` events: the GUI
never decides one, except ERROR when the backend is lost.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from voxize.protocol import State

logger = logging.getLogger(__name__)


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
        if new == State.ERROR:
            self.error_message = error
        self._state = new
        logger.debug("state: %s -> %s", old.name, new.name)
        for cb in list(self._listeners):
            cb(self, old, new)
