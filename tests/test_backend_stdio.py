import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time

from helpers import MOCK_ENV


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


def test_sigterm_exits():
    b = Backend()
    b.send({"type": "start", "cwd": None, "glossaries": []})
    b.read_until(lambda b: "RECORDING" in b.states())
    b.proc.send_signal(signal.SIGTERM)
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
