"""VoxizeApp's event dispatch, driven without a display, a window or a backend.

The app is constructed but never run, so do_activate (and with it GTK init,
the window and the BackendClient) never happens. A fake client records what
the app sends; a fake window records which methods the dispatch calls.
"""

import logging

import pytest

from voxize.gui.app import VoxizeApp
from voxize.protocol import PROTOCOL_VERSION, State


class FakeClient:
    def __init__(self):
        self.sent = []
        self.finished = False
        self.stdin_closed = False

    def send(self, type_, **fields):
        self.sent.append(type_)

    def finish(self):
        self.finished = True

    def close_stdin(self):
        self.stdin_closed = True


class FakeUi:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append(name)


@pytest.fixture
def app():
    root = logging.getLogger("voxize")
    level = root.level
    a = VoxizeApp(backend=None)
    a._client = FakeClient()
    a._ui = FakeUi()
    a._state.on_change(a._on_state_change)  # as do_activate wires it
    yield a
    a._detach_log()
    root.setLevel(level)


def state(name):
    return {"type": "state", "state": name, "error": None}


def test_protocol_mismatch_error_is_not_overwritten_by_queued_events(app):
    # start went out before hello, so the backend's first events are queued.
    app._on_event({"type": "hello", "protocol": 0})
    app._on_event({"type": "context", "sources": []})
    app._on_event(state("WARMING"))
    app._on_event(state("CANCELLED"))

    assert app._state.state == State.ERROR
    assert app._state.error_message == (
        f"Backend speaks protocol 0, this window expects {PROTOCOL_VERSION}"
    )
    assert app._ui.calls == []  # the queued context never reached the window
    assert app._client.sent == ["close"]
    assert app._client.finished and app._client.stdin_closed


def test_hello_timeout_error_is_not_overwritten_by_a_late_backend(app):
    assert app._check_hello() is False  # one-shot timer
    app._on_event({"type": "hello", "protocol": PROTOCOL_VERSION})
    app._on_event(state("WARMING"))
    app._on_event(state("CANCELLED"))

    assert app._state.state == State.ERROR
    assert app._state.error_message == "Backend did not start"


def test_normal_flow_reaches_ready_and_stays_there(app):
    app._on_event({"type": "hello", "protocol": PROTOCOL_VERSION})
    for name in ("WARMING", "RECORDING", "TRANSCRIBING", "CLEANING", "READY"):
        app._on_event(state(name))
    assert app._state.state == State.READY
    assert app._client.finished  # EOF from here on is not "backend lost"
    assert app._client.sent == []  # READY -> close needs nothing more

    app._on_event(state("CANCELLED"))
    assert app._state.state == State.READY


def test_log_buffer_keeps_only_the_newest_records_without_a_target(app):
    app._log_buffer.capacity = 5
    log = logging.getLogger("voxize.test")
    for i in range(50):
        log.debug("line %d", i)
    kept = [r.getMessage() for r in app._log_buffer.buffer]
    assert len(kept) <= 6
    assert kept[-1] == "line 49"
