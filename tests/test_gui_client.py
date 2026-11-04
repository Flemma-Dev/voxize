import os
import subprocess
import sys

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


def test_send_after_the_backend_exited_is_harmless():
    proc = spawn_backend()
    lost: list[str] = []
    client = BackendClient(proc, on_event=Recorder(), on_lost=lost.append)
    proc.kill()
    proc.wait(timeout=10)
    client.send("stop")  # must not raise
    client.send("stop")
