"""VoxizeApp's event dispatch, driven without a display, a window or a backend.

The app is constructed but never run, so do_activate (and with it GTK init,
the window and the BackendClient) never happens. A fake transport records
what the app sends; a fake window records which methods the dispatch calls.
The client rules themselves are tested in tests/test_frontend_client.py;
these check the GUI is wired to them.
"""

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
    a = VoxizeApp(backend=None)
    a._core.transport = FakeClient()
    a._ui = FakeUi()
    yield a
    a._log.detach()


def state(name):
    return {"type": "state", "state": name, "error": None}


def test_protocol_mismatch_error_is_not_overwritten_by_queued_events(app):
    # start went out before hello, so the backend's first events are queued.
    app._on_event({"type": "hello", "protocol": 0})
    app._on_event({"type": "context", "sources": []})
    app._on_event(state("WARMING"))
    app._on_event(state("CANCELLED"))

    assert app._core.state.state == State.ERROR
    assert app._core.state.error_message == (
        f"Backend speaks protocol 0, this window expects {PROTOCOL_VERSION}"
    )
    assert app._ui.calls == []  # the queued context never reached the window
    assert app._core.transport.sent == ["close"]
    assert app._core.transport.finished and app._core.transport.stdin_closed


def test_hello_timeout_error_is_not_overwritten_by_a_late_backend(app):
    assert app._check_hello() is False  # one-shot timer
    app._on_event({"type": "hello", "protocol": PROTOCOL_VERSION})
    app._on_event(state("WARMING"))
    app._on_event(state("CANCELLED"))

    assert app._core.state.state == State.ERROR
    assert app._core.state.error_message == "Backend did not start"


def test_normal_flow_reaches_ready_and_stays_there(app):
    app._on_event({"type": "hello", "protocol": PROTOCOL_VERSION})
    for name in ("WARMING", "RECORDING", "TRANSCRIBING", "CLEANING", "READY"):
        app._on_event(state(name))
    assert app._core.state.state == State.READY
    assert app._core.transport.finished  # EOF from here on is not "backend lost"
    assert app._core.transport.sent == []  # READY -> close needs nothing more

    app._on_event(state("CANCELLED"))
    assert app._core.state.state == State.READY


def test_events_reach_the_window(app):
    app._on_event({"type": "hello", "protocol": PROTOCOL_VERSION})
    app._on_event({"type": "level", "dbfs": -20.0})
    app._on_event({"type": "text", "stage": "live", "op": "replace", "text": "hi"})
    assert app._ui.calls == ["set_level", "on_text"]


def test_close_request_sends_close_once(app):
    app._trash_if_empty = True
    app._on_close_request(None)
    app._on_close_request(None)
    assert app._core.transport.sent == ["close"]
