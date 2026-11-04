"""voxize.frontend: the state mirror, the session log and the client rules."""

import logging
import subprocess

import pytest

from voxize.frontend.client import ClientCore
from voxize.frontend.session_log import SessionLog
from voxize.frontend.state import ClientState
from voxize.protocol import PROTOCOL_VERSION, State


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.finished = False
        self.stdin_closed = False

    def send(self, type_, **fields):
        self.sent.append((type_, fields))

    def finish(self):
        self.finished = True

    def close_stdin(self):
        self.stdin_closed = True


def hello(version=PROTOCOL_VERSION):
    return {"type": "hello", "protocol": version}


def state(name, error=None):
    return {"type": "state", "state": name, "error": error}


@pytest.fixture
def core():
    return ClientCore(FakeTransport(), client_noun="this test client")


# ── ClientState (moved from tests/test_gui_client.py) ──


def test_client_state_notifies_like_the_state_machine():
    seen = []
    cs = ClientState()
    cs.on_change(lambda m, old, new: seen.append((m, old, new)))
    assert cs.state == State.INITIALIZING
    cs.apply(State.WARMING)
    cs.apply(State.WARMING)  # no-op: unchanged
    cs.apply(State.ERROR, error="boom")
    assert seen == [
        (cs, State.INITIALIZING, State.WARMING),
        (cs, State.WARMING, State.ERROR),
    ]
    assert cs.error_message == "boom"


@pytest.mark.parametrize("terminal", [State.READY, State.CANCELLED, State.ERROR])
def test_terminal_states_are_sticky(terminal):
    seen = []
    cs = ClientState()
    cs.apply(State.WARMING)
    cs.apply(terminal, error="first")
    cs.on_change(lambda m, old, new: seen.append(new))
    for later in (State.WARMING, State.RECORDING, State.CANCELLED, State.ERROR):
        cs.apply(later, error="later")
    assert cs.state == terminal
    assert seen == []
    if terminal == State.ERROR:
        assert cs.error_message == "first"


def test_a_failing_listener_does_not_stop_the_others():
    seen = []
    cs = ClientState()

    def bad(*_):
        raise RuntimeError("listener bug")

    cs.on_change(bad)
    cs.on_change(lambda m, old, new: seen.append(new))
    cs.apply(State.WARMING)
    assert seen == [State.WARMING]
    assert cs.state == State.WARMING


# ── ClientCore ──


def test_hello_and_state_are_consumed_other_events_pass_through(core):
    assert core.handle(hello()) is None
    assert core.hello
    assert core.handle(state("WARMING")) is None
    assert core.state.state == State.WARMING
    msg = {"type": "level", "dbfs": -20.0}
    assert core.handle(msg) is msg
    assert core.transport.sent == []


def test_protocol_mismatch_sticks_and_drops_queued_events(core):
    core.handle(hello(0))
    assert core.handle({"type": "context", "sources": []}) is None
    core.handle(state("WARMING"))
    core.handle(state("CANCELLED"))
    assert core.state.state == State.ERROR
    assert core.state.error_message == (
        f"Backend speaks protocol 0, this test client expects {PROTOCOL_VERSION}"
    )
    assert core.transport.sent == [("close", {"trash_if_empty": False})]
    assert core.transport.finished and core.transport.stdin_closed


def test_hello_timeout_sticks_against_a_late_backend(core):
    core.check_hello()
    core.handle(hello())
    core.handle(state("WARMING"))
    assert core.state.state == State.ERROR
    assert core.state.error_message == "Backend did not start"


def test_check_hello_after_hello_or_close_does_nothing(core):
    core.handle(hello())
    core.check_hello()
    assert core.state.state == State.INITIALIZING
    late = ClientCore(FakeTransport(), client_noun="x")
    late.close(trash_if_empty=False)
    late.check_hello()
    assert late.state.state == State.INITIALIZING


def test_normal_flow_reaches_ready_finishes_and_stays(core):
    core.handle(hello())
    for name in ("WARMING", "RECORDING", "TRANSCRIBING", "CLEANING", "READY"):
        core.handle(state(name))
    assert core.state.state == State.READY
    assert core.transport.finished  # EOF from here on is not "backend lost"
    assert core.transport.sent == []
    core.handle(state("CANCELLED"))
    assert core.state.state == State.READY


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("Traceback: boom", "Backend exited unexpectedly: Traceback: boom"),
        ("", "Backend exited unexpectedly"),
    ],
)
def test_backend_lost_is_an_error_with_the_last_stderr_line(core, line, message):
    core.handle(hello())
    core.handle(state("RECORDING"))
    core.backend_lost(line)
    assert core.state.state == State.ERROR
    assert core.state.error_message == message


def test_close_is_sent_once(core):
    core.close(trash_if_empty=True)
    core.close(trash_if_empty=False)
    assert core.transport.sent == [("close", {"trash_if_empty": True})]
    assert core.transport.finished and core.transport.stdin_closed
    assert core.closed


def test_hang_up_closes_stdin_without_close(core):
    core.hang_up()
    assert core.transport.sent == []
    assert core.transport.finished and core.transport.stdin_closed
    assert core.closed


def test_the_transport_may_arrive_later():
    core = ClientCore(client_noun="x")
    core.send("stop")  # no transport yet: dropped, no error
    core.close(trash_if_empty=False)
    core.transport = FakeTransport()
    core.send("stop")
    assert core.transport.sent == [("stop", {})]


# ── SessionLog ──


def test_session_log_keeps_only_the_newest_records_without_a_target():
    log = SessionLog("x.log", capacity=5)
    for i in range(50):
        logging.getLogger("voxize.test").debug("line %d", i)
    kept = [r.getMessage() for r in log.buffer.buffer]
    assert len(kept) <= 6
    assert kept[-1] == "line 49"


def test_session_log_writes_buffered_then_live_lines(tmp_path):
    log = SessionLog("tui.log")
    logging.getLogger("voxize.test").debug("before the session")
    log.attach(str(tmp_path))
    logging.getLogger("voxize.test").debug("after the session")
    log.detach()
    text = (tmp_path / "tui.log").read_text()
    assert text.index("before the session") < text.index("after the session")
    assert "voxize.test" in text


def test_session_log_stderr_target(tmp_path):
    log = SessionLog("tui.log")
    assert log.stderr_target() is subprocess.DEVNULL
    log.attach(str(tmp_path))
    target = log.stderr_target()
    assert target.fileno() > 2
    log.detach()


def test_session_log_detach_removes_its_handlers(tmp_path):
    root = logging.getLogger("voxize")
    before = list(root.handlers)
    log = SessionLog("tui.log")
    log.attach(str(tmp_path))
    log.detach()
    assert [h for h in root.handlers if h not in before] == []
