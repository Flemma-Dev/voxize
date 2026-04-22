"""State machine for Voxize session lifecycle."""

import logging
from enum import Enum, auto

logger = logging.getLogger(__name__)


class State(Enum):
    INITIALIZING = auto()
    WARMING = auto()
    RECORDING = auto()
    TRANSCRIBING = auto()
    CLEANING = auto()
    READY = auto()
    CANCELLED = auto()
    ERROR = auto()


_ALLOWED: dict[State, frozenset[State]] = {
    State.INITIALIZING: frozenset(
        {State.WARMING, State.RECORDING, State.CANCELLED, State.ERROR}
    ),
    State.WARMING: frozenset(
        {State.RECORDING, State.TRANSCRIBING, State.CANCELLED, State.ERROR}
    ),
    State.RECORDING: frozenset({State.TRANSCRIBING, State.CANCELLED}),
    State.TRANSCRIBING: frozenset({State.CLEANING, State.READY, State.CANCELLED}),
    State.CLEANING: frozenset({State.READY, State.CANCELLED}),
    State.READY: frozenset(),
    State.CANCELLED: frozenset(),
    State.ERROR: frozenset(),
}


class InvalidTransition(Exception):
    """Raised when a state transition is not allowed."""


class StateMachine:
    """Manages Voxize session state and notifies listeners on transitions.

    Pure logic — no UI or I/O dependencies. Listeners are called synchronously.
    """

    def __init__(self) -> None:
        self._state = State.INITIALIZING
        self._listeners: list = []
        self.error_message: str = ""

    @property
    def state(self) -> State:
        return self._state

    def on_change(self, callback) -> None:
        """Register callback(machine, old_state, new_state)."""
        self._listeners.append(callback)

    def transition(self, new_state: State, *, error: str = "") -> None:
        old = self._state
        allowed = new_state in _ALLOWED[old]
        logger.debug(
            "transition: %s -> %s allowed=%s", old.name, new_state.name, allowed
        )
        if not allowed:
            raise InvalidTransition(f"{old.name} → {new_state.name} is not allowed")
        if new_state == State.ERROR:
            self.error_message = error
            logger.info("transition: %s -> ERROR error=%s", old.name, error)
        else:
            logger.info("transition: %s -> %s", old.name, new_state.name)
        self._state = new_state
        for cb in self._listeners:
            cb(self, old, new_state)
