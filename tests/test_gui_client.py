import os
import subprocess
import sys

import pytest
from helpers import MOCK_ENV, Recorder, run_until

from voxize.gui.backend_client import BackendClient
from voxize.gui.client_state import ClientState
from voxize.protocol import State


def spawn_backend(**env):
    return subprocess.Popen(
        [sys.executable, "-m", "voxize.backend"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **MOCK_ENV, "VOXIZE_MOCK_SHORT": "1", **env},
    )


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


def test_round_trip_with_a_real_backend():
    proc = spawn_backend()
    rec = Recorder()
    lost: list[str] = []
    client = BackendClient(proc, on_event=rec, on_lost=lost.append)
    client.send("start", cwd=None, glossaries=[])
    run_until(lambda: "RECORDING" in rec.states(), what="RECORDING")
    client.send("stop")
    run_until(lambda: "READY" in rec.states(), what="READY")
    client.finish()
    client.send("close", trash_if_empty=False)
    client.close_stdin()
    run_until(lambda: client.eof, what="stdout EOF")
    assert proc.wait(timeout=10) == 0
    assert rec.events[0] == {"type": "hello", "protocol": 1}
    assert lost == []


def test_a_dying_backend_is_reported():
    proc = spawn_backend()
    rec = Recorder()
    lost: list[str] = []
    client = BackendClient(proc, on_event=rec, on_lost=lost.append)
    client.send("start", cwd=None, glossaries=[])
    run_until(lambda: "RECORDING" in rec.states(), what="RECORDING")
    proc.kill()
    run_until(lambda: lost, what="on_lost")
    assert len(lost) == 1
    assert isinstance(lost[0], str)


def test_lost_report_carries_the_final_stderr_line():
    # Writes stderr (the last line unterminated) then closes stdout, so the
    # stdout EOF can be dispatched before stderr is read.
    code = (
        "import os, sys\n"
        "sys.stderr.write('first line\\nfinal words')\n"
        "sys.stderr.flush()\n"
        "os.close(1)\n"
        "import time; time.sleep(0.3)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    lost: list[str] = []
    BackendClient(proc, on_event=Recorder(), on_lost=lost.append)
    run_until(lambda: lost, what="on_lost")
    assert lost == ["final words"]
    proc.wait(timeout=10)


def test_a_failing_handler_does_not_stop_later_events_or_the_lost_report():
    proc = spawn_backend()
    rec = Recorder()
    calls = []

    def flaky(msg):
        calls.append(msg)
        if len(calls) == 1:
            raise RuntimeError("handler bug")
        rec(msg)

    lost: list[str] = []

    def bad_lost(reason):
        lost.append(reason)
        raise RuntimeError("lost handler bug")

    client = BackendClient(proc, on_event=flaky, on_lost=bad_lost)
    client.send("start", cwd=None, glossaries=[])
    run_until(lambda: "RECORDING" in rec.states(), what="RECORDING after a raise")
    assert calls[0]["type"] == "hello"
    proc.kill()
    run_until(lambda: lost, what="on_lost after a raise")
    assert len(lost) == 1


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


def test_send_after_the_backend_exited_is_harmless():
    proc = spawn_backend()
    lost: list[str] = []
    client = BackendClient(proc, on_event=Recorder(), on_lost=lost.append)
    proc.kill()
    proc.wait(timeout=10)
    client.send("stop")  # must not raise
    client.send("stop")


def test_a_non_utf8_cwd_reaches_the_backend(tmp_path):
    # /proc hands back raw bytes; Python surrogateescapes them into the str.
    cwd = tmp_path / os.fsdecode(b"caf\xe9")
    cwd.mkdir()
    (cwd / "WHISPER.txt").write_text("Glossary: Luup")
    proc = spawn_backend()
    rec = Recorder()
    lost: list[str] = []
    client = BackendClient(proc, on_event=rec, on_lost=lost.append)
    client.send("start", cwd=str(cwd), glossaries=[])
    run_until(lambda: rec.of("context"), what="context")
    [source] = rec.of("context")[0]["sources"]
    assert os.fsencode(source["path"]) == os.fsencode(cwd / "WHISPER.txt")
    run_until(lambda: "RECORDING" in rec.states(), what="RECORDING")
    client.finish()
    client.send("close", trash_if_empty=False)
    client.close_stdin()
    run_until(lambda: client.eof, what="stdout EOF")
    assert proc.wait(timeout=10) == 0
    assert lost == []
