import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time

from helpers import MOCK_ENV

from voxize import protocol


class Backend:
    """A backend subprocess with line-buffered event reading."""

    def __init__(self, extra_env=None, python_args=()):
        env = {**os.environ, **MOCK_ENV, "VOXIZE_MOCK_SHORT": "1", **(extra_env or {})}
        # stderr goes to a file, not a pipe: -X importtime writes tens of KB,
        # and an unread pipe would block the backend once it fills.
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115 — read at the end
        self.proc = subprocess.Popen(
            [sys.executable, *python_args, "-m", "voxize.backend"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env=env,
        )
        self.events: list[dict] = []
        self.eof = False
        self._buf = b""
        self._fd = self.proc.stdout.fileno()
        self._sel = selectors.DefaultSelector()
        self._sel.register(self._fd, selectors.EVENT_READ)

    def send(self, msg):
        line = msg if isinstance(msg, str) else json.dumps(msg)
        self.proc.stdin.write((line + "\n").encode())
        self.proc.stdin.flush()

    def read_until(self, pred, timeout=15.0):
        deadline = time.monotonic() + timeout
        while not pred(self):
            left = deadline - time.monotonic()
            assert left > 0, f"timeout; events={[e['type'] for e in self.events]}"
            if self.eof:
                raise AssertionError(f"EOF; stderr={self.stderr_text()[-2000:]}")
            if not self._sel.select(left):
                continue
            chunk = os.read(self._fd, 65536)
            if not chunk:
                self.eof = True
                continue
            self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                self.events.append(json.loads(line))  # every stdout line is JSON
        return self.events

    def states(self):
        return [e["state"] for e in self.events if e["type"] == "state"]

    def stderr_text(self) -> str:
        self._stderr.seek(0)
        return self._stderr.read().decode("utf-8", errors="replace")


def test_hello_is_the_first_line():
    b = Backend()
    b.read_until(lambda b: b.events)
    assert b.events[0] == {"type": "hello", "protocol": 1}
    b.proc.kill()


def test_full_session_over_stdio():
    b = Backend()
    b.send({"type": "start", "cwd": None, "glossaries": []})
    b.read_until(lambda b: any(e["type"] == "text" for e in b.events))
    b.send({"type": "stop"})
    b.read_until(lambda b: "READY" in b.states())
    b.send({"type": "close", "trash_if_empty": False})
    b.read_until(lambda b: b.eof)
    assert b.proc.wait(timeout=10) == 0
    assert b.states() == ["WARMING", "RECORDING", "TRANSCRIBING", "CLEANING", "READY"]
    assert [e["stage"] for e in b.events if e["type"] == "result"] == [
        "batch",
        "cleaned",
    ]


def test_stale_unknown_and_garbage_commands_are_ignored():
    b = Backend({"VOXIZE_STOP": "1600"})
    b.send({"type": "start", "cwd": None, "glossaries": []})
    b.read_until(lambda b: "READY" in b.states())
    n = len(b.states())
    b.send({"type": "stop"})
    b.send({"type": "cancel"})
    b.send({"type": "bogus"})
    b.send("not json at all")
    b.send({"type": "close", "trash_if_empty": False})
    b.read_until(lambda b: b.eof)
    assert b.proc.wait(timeout=10) == 0
    assert len(b.states()) == n


def test_stdin_eof_cancels_and_exits():
    b = Backend()
    b.send({"type": "start", "cwd": None, "glossaries": []})
    b.read_until(lambda b: "RECORDING" in b.states())
    b.proc.stdin.close()
    b.read_until(lambda b: b.eof)
    assert b.proc.wait(timeout=10) == 0
    assert b.states()[-1] == "CANCELLED"


def test_malformed_start_then_eof_still_exits():
    b = Backend()
    b.read_until(lambda b: b.events)
    b.send({"type": "start", "cwd": 5, "glossaries": 5})
    b.proc.stdin.close()
    b.read_until(lambda b: b.eof)
    assert b.proc.wait(timeout=10) == 0
    assert "WARMING" not in b.states()  # the start was ignored
    assert "malformed start" in b.stderr_text()


def test_lines_after_close_are_not_dispatched():
    b = Backend()
    b.read_until(lambda b: b.events)
    # One write, so close and the start behind it arrive in the same chunk.
    b.proc.stdin.write(
        b'{"type":"close","trash_if_empty":false}\n'
        b'{"type":"start","cwd":null,"glossaries":[]}\n'
    )
    b.proc.stdin.flush()
    b.read_until(lambda b: b.eof)
    assert b.proc.wait(timeout=10) == 0
    assert "WARMING" not in b.states()  # the start behind close never ran


def test_sigterm_exits():
    b = Backend()
    b.send({"type": "start", "cwd": None, "glossaries": []})
    b.read_until(lambda b: "RECORDING" in b.states())
    b.proc.send_signal(signal.SIGTERM)
    b.read_until(lambda b: b.eof)
    assert b.proc.wait(timeout=10) == 0


def test_backend_never_imports_gtk_or_frontends():
    b = Backend({"VOXIZE_STOP": "1600"}, python_args=("-X", "importtime"))
    b.send({"type": "start", "cwd": None, "glossaries": []})
    b.read_until(lambda b: "READY" in b.states())
    b.send({"type": "close", "trash_if_empty": False})
    b.read_until(lambda b: b.eof)
    b.proc.wait(timeout=10)
    imports = b.stderr_text()
    assert "gi.repository.GLib" in imports  # the probe sees gi imports at all
    for banned in ("gi.repository.Gtk", "gi.repository.Gdk", "gi.repository.Adw"):
        assert banned not in imports
    assert "voxize.gui" not in imports
    assert "voxize.tui" not in imports


def test_claim_stdout_diverts_stray_writes():
    code = (
        "import os; from voxize.backend.stdio import claim_stdout; "
        "fd = claim_stdout(); print('stray', flush=True); "
        'os.write(fd, b\'{"type":"hello","protocol":1}\\n\')'
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, check=True)
    assert out.stdout == b'{"type":"hello","protocol":1}\n'
    assert b"stray" in out.stderr


def _in_process_server(monkeypatch):
    """A Server on pipes, with _exit stubbed (the real one redirects 0/1/2)."""
    from voxize.backend.server import Server

    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    server = Server(in_r, out_w, mock=None)
    exits = []
    monkeypatch.setattr(server, "_exit", lambda: exits.append(1))
    return server, in_r, in_w, exits, (out_r,)


def test_handler_exception_keeps_the_stdin_watch(monkeypatch):
    server, in_r, in_w, _exits, _ = _in_process_server(monkeypatch)

    def boom(*_a):
        raise RuntimeError("handler bug")

    monkeypatch.setattr(server.session, "start", boom)
    os.write(in_w, b'{"type":"start","cwd":null,"glossaries":[]}\n')
    assert server._on_stdin(in_r, None) is True  # watch survives


def test_close_raising_still_exits_once(monkeypatch):
    server, _in_r, _in_w, exits, _ = _in_process_server(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("close bug")

    monkeypatch.setattr(server.session, "close", boom)
    server._close(trash_if_empty=False)
    assert exits == [1]


def test_unencodable_event_does_not_silence_later_ones(monkeypatch):
    server, _in_r, _in_w, _exits, (out_r,) = _in_process_server(monkeypatch)
    server.send({"type": "level", "dbfs": float("nan")})
    server.send({"type": "hello", "protocol": 1})
    assert b'"hello"' in os.read(out_r, 65536)


def test_lone_surrogate_is_delivered_escaped_and_later_events_flow(monkeypatch):
    server, _in_r, _in_w, _exits, (out_r,) = _in_process_server(monkeypatch)
    server.send({"type": "text", "stage": "live", "op": "append", "text": "a\udc80b"})
    server.send({"type": "hello", "protocol": 1})
    first, second = os.read(out_r, 65536).splitlines()
    assert protocol.decode(first)["text"] == "a\udc80b"  # round-trips as \udc80
    assert protocol.decode(second)["type"] == "hello"


def test_protocol_jsonl_lands_in_the_session_dir(monkeypatch, tmp_path):
    # Mock sessions create no folder, so announce one by hand.
    server, _in_r, _in_w, _exits, _ = _in_process_server(monkeypatch)
    server.send_hello()  # before `session`: buffered, then flushed
    server.send(protocol.event("session", dir=str(tmp_path)))
    server._dispatch(b'{"type":"stop"}')  # an inbound message
    server.send(protocol.event("no_speech"))
    server._log.close()
    lines = (tmp_path / "protocol.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert [(r["dir"], r["msg"]["type"]) for r in records] == [
        ("out", "hello"),
        ("out", "session"),
        ("in", "stop"),
        ("out", "no_speech"),
    ]
    assert all(isinstance(r["t"], float) for r in records)
