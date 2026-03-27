"""State machine for Voxize session lifecycle."""

from enum import Enum, auto


class State(Enum):
    INITIALIZING = auto()
    RECORDING = auto()
    CLEANING = auto()
    READY = auto()
    CANCELLED = auto()
    ERROR = auto()


_ALLOWED: dict[State, frozenset[State]] = {
    State.INITIALIZING: frozenset({State.RECORDING, State.ERROR}),
    State.RECORDING: frozenset({State.CLEANING, State.CANCELLED, State.ERROR}),
    State.CLEANING: frozenset({State.READY, State.CANCELLED, State.ERROR}),
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
        if new_state not in _ALLOWED[old]:
            raise InvalidTransition(f"{old.name} → {new_state.name} is not allowed")
        if new_state == State.ERROR:
            self.error_message = error
        self._state = new_state
        for cb in self._listeners:
            cb(self, old, new_state)
