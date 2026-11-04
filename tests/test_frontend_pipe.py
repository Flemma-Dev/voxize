"""BackendPipe driven by hand over real OS pipes — no event loop."""

import contextlib
import os

import pytest

from voxize.frontend.pipe import BackendPipe


class FakeProc:
    """The three pipes of a Popen(stdin=PIPE, stdout=PIPE, stderr=PIPE)."""

    def __init__(self):
        out_r, self.out_w = os.pipe()
        err_r, self.err_w = os.pipe()
        self.in_r, in_w = os.pipe()
        self.stdout = os.fdopen(out_r, "rb", buffering=0)
        self.stderr = os.fdopen(err_r, "rb", buffering=0)
        self.stdin = os.fdopen(in_w, "wb")

    def close(self):
        for fd in (self.out_w, self.err_w, self.in_r):
            with contextlib.suppress(OSError):
                os.close(fd)
        for f in (self.stdout, self.stderr, self.stdin):
            with contextlib.suppress(OSError, ValueError):
                f.close()


@pytest.fixture
def proc():
    p = FakeProc()
    yield p
    p.close()


def make(proc, echo=False):
    events, lost = [], []
    pipe = BackendPipe(
        proc,
        on_event=events.append,
        on_lost=lost.append,
        stderr_logger="voxize.test.backend",
        echo_stderr=echo,
    )
    return pipe, events, lost


def test_lines_split_across_reads_are_reassembled(proc):
    pipe, events, _ = make(proc)
    os.write(proc.out_w, b'{"type":"hello","protocol":1}\n{"type":"no_')
    assert pipe.read_stdout() is True
    os.write(proc.out_w, b'speech"}\n')
    assert pipe.read_stdout() is True
    assert [e["type"] for e in events] == ["hello", "no_speech"]


def test_a_bad_line_is_dropped_and_later_lines_flow(proc):
    pipe, events, _ = make(proc)
    os.write(proc.out_w, b'not json\n[1]\n{"type":"no_speech"}\n')
    pipe.read_stdout()
    assert events == [{"type": "no_speech"}]


def test_a_failing_handler_does_not_stop_later_lines(proc):
    seen = []

    def flaky(msg):
        seen.append(msg["type"])
        if len(seen) == 1:
            raise RuntimeError("handler bug")

    pipe = BackendPipe(
        proc, flaky, lambda _: None, stderr_logger="voxize.test", echo_stderr=False
    )
    os.write(proc.out_w, b'{"type":"hello","protocol":1}\n{"type":"no_speech"}\n')
    assert pipe.read_stdout() is True
    assert seen == ["hello", "no_speech"]


def test_eof_before_finish_reports_lost_once_with_the_final_stderr_line(proc):
    pipe, _, lost = make(proc)
    os.write(proc.err_w, b"first line\nfinal words")
    os.close(proc.out_w)
    assert pipe.read_stdout() is False
    assert pipe.eof
    assert lost == ["final words"]
    assert pipe.last_stderr_line() == "final words"


def test_eof_after_finish_is_not_lost(proc):
    pipe, _, lost = make(proc)
    pipe.finish()
    os.close(proc.out_w)
    assert pipe.read_stdout() is False
    assert lost == []


@pytest.mark.parametrize("echo", [True, False])
def test_stderr_is_logged_and_echoed_only_when_asked(proc, capfd, caplog, echo):
    pipe, _, _ = make(proc, echo=echo)
    os.write(proc.err_w, b"backend says hi\n")
    with caplog.at_level("DEBUG", logger="voxize.test.backend"):
        assert pipe.read_stderr() is True
    assert "backend says hi" in caplog.text
    assert ("backend says hi" in capfd.readouterr().err) is echo


def test_send_writes_one_line_and_is_dropped_after_close_stdin(proc):
    pipe, _, _ = make(proc)
    pipe.send("stop")
    assert os.read(proc.in_r, 1024) == b'{"type":"stop"}\n'
    pipe.close_stdin()
    pipe.send("stop")  # dropped, no error
