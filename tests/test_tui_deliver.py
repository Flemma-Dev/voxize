"""Clipboard chain and stdout delivery for the terminal client.

Real subprocesses, but fake tools: no test touches the desktop clipboard.
"""

import asyncio
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

import pytest

from voxize.tui import deliver
from voxize.tui.deliver import Clipboard, clipboard_tools, final_output, run_tool


def tool(tmp_path, body: str) -> str:
    path = tmp_path / "fake-copy"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


def test_run_tool_feeds_stdin_and_hands_the_tool_devnull_stdout(tmp_path):
    got, where = tmp_path / "got.txt", tmp_path / "stdout.txt"
    cmd = tool(tmp_path, f"cat > {got}\nreadlink /proc/$$/fd/1 > {where}")
    assert asyncio.run(run_tool([cmd], "héllo — ok", subprocess.DEVNULL))
    assert got.read_text() == "héllo — ok"
    assert where.read_text().strip() == "/dev/null"


def test_run_tool_does_not_wait_for_a_forked_child(tmp_path):
    # Like wl-copy: a child stays behind to serve the selection.
    cmd = tool(tmp_path, "cat > /dev/null\n(sleep 3) &")
    with open(tmp_path / "tui.log", "ab") as log:
        t0 = time.monotonic()
        assert asyncio.run(run_tool([cmd], "x", log))
    assert time.monotonic() - t0 < 1.5


def test_run_tool_stderr_goes_to_the_given_file(tmp_path):
    cmd = tool(tmp_path, "cat > /dev/null\necho oops >&2")
    with open(tmp_path / "tui.log", "ab") as log:
        assert asyncio.run(run_tool([cmd], "x", log))
    assert "oops" in (tmp_path / "tui.log").read_text()


def test_run_tool_failures_are_false(tmp_path, monkeypatch):
    assert not asyncio.run(
        run_tool([tool(tmp_path, "exit 3")], "x", subprocess.DEVNULL)
    )
    assert not asyncio.run(run_tool(["/nonexistent/tool"], "x", subprocess.DEVNULL))
    monkeypatch.setattr(deliver, "TOOL_TIMEOUT_S", 0.2)
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\ncat > /dev/null\nsleep 5\n")
    slow.chmod(0o755)
    t0 = time.monotonic()
    assert not asyncio.run(run_tool([str(slow)], "x", subprocess.DEVNULL))
    assert time.monotonic() - t0 < 2


WHICH = {"wl-copy": "/b/wl-copy", "xclip": "/b/xclip", "xsel": "/b/xsel"}.get


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        (
            {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
            [
                ["/b/wl-copy"],
                ["/b/xclip", "-selection", "clipboard"],
                ["/b/xsel", "--clipboard", "--input"],
            ],
        ),
        (
            {"DISPLAY": ":0"},
            [
                ["/b/xclip", "-selection", "clipboard"],
                ["/b/xsel", "--clipboard", "--input"],
            ],
        ),
        ({"WAYLAND_DISPLAY": "wayland-0"}, [["/b/wl-copy"]]),
        ({}, []),
    ],
)
def test_clipboard_tools_follow_the_display(environ, expected):
    assert clipboard_tools(environ, WHICH) == expected


def test_clipboard_tools_skip_missing_binaries():
    assert (
        clipboard_tools({"WAYLAND_DISPLAY": "w", "DISPLAY": ":0"}, lambda _: None) == []
    )


def make_clipboard(tools, results):
    calls, osc = [], []

    async def run(cmd, text, stderr):
        calls.append(cmd[0])
        return results.get(cmd[0], False)

    clip = Clipboard(
        osc52=osc.append,
        stderr_target=lambda: subprocess.DEVNULL,
        tools=lambda: tools,
        run=run,
    )
    return clip, calls, osc


def test_clipboard_falls_through_to_the_next_tool():
    clip, calls, osc = make_clipboard([["/b/wl-copy"], ["/b/xsel"]], {"/b/xsel": True})
    assert asyncio.run(clip.copy("t")) == "xsel"
    assert calls == ["/b/wl-copy", "/b/xsel"] and osc == []
    assert not clip.unavailable


def test_clipboard_falls_back_to_osc52_and_says_so():
    clip, _, osc = make_clipboard([["/b/wl-copy"]], {})
    assert asyncio.run(clip.copy("t")) == "osc52"
    assert osc == ["t"] and clip.unavailable


def test_copies_are_serialised_so_cleaned_lands_last():
    async def go():
        order, gate = [], asyncio.Event()

        async def run(cmd, text, stderr):
            if text == "batch":
                await gate.wait()
            order.append(text)
            return True

        clip = Clipboard(
            osc52=lambda _: None,
            stderr_target=lambda: subprocess.DEVNULL,
            tools=lambda: [["/b/wl-copy"]],
            run=run,
        )
        first = asyncio.create_task(clip.copy("batch"))
        await asyncio.sleep(0)
        second = asyncio.create_task(clip.copy("cleaned"))
        await asyncio.sleep(0.01)
        assert order == []
        gate.set()
        await asyncio.gather(first, second)
        await clip.settle()
        return order

    assert asyncio.run(go()) == ["batch", "cleaned"]


@pytest.mark.parametrize(
    ("text", "isatty", "expected"),
    [
        (None, True, ("", 1)),
        ("", False, ("", 1)),
        ("Fix it.", False, ("Fix it.", 0)),
        ("Fix it.", True, ("Fix it.\n", 0)),
    ],
)
def test_final_output(text, isatty, expected):
    assert final_output(text, isatty) == expected


def read_until(fd: int, marker: bytes, timeout: float = 10.0) -> bytes:
    got, deadline = b"", time.monotonic() + timeout
    while marker not in got and time.monotonic() < deadline:
        if select.select([fd], [], [], 0.2)[0]:
            try:
                got += os.read(fd, 4096)
            except OSError:
                break
    return got


CLAIM_CHILD = """
import os, shutil
from voxize.tui.deliver import claim_terminal_stdout, write_output
fd = claim_terminal_stdout()
cols, lines = shutil.get_terminal_size()
os.write(1, f"SIZE={cols}x{lines};FD={fd is not None};".encode())
if fd is not None:
    write_output(fd, "Fix it.")
"""


def test_claim_terminal_stdout_keeps_the_terminal_size_when_piped():
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", CLAIM_CHILD],
            stdout=subprocess.PIPE,
            stderr=slave,
            # Passing env= gives the child a fresh environment without
            # COLUMNS/LINES (pytest's own process has COLUMNS=80 at the C
            # level); Python's shutil honours those variables over the tty.
            env={k: v for k, v in os.environ.items() if k not in ("COLUMNS", "LINES")},
        )
        piped, _ = proc.communicate(timeout=20)
        seen = read_until(master, b"FD=True;")
        assert proc.returncode == 0
    finally:
        os.close(master)
        os.close(slave)
    assert b"SIZE=100x30;FD=True;" in seen
    assert piped == b"Fix it."  # exactly, no newline added


def test_claim_terminal_stdout_is_a_no_op_when_stdout_is_a_tty():
    master, slave = pty.openpty()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", CLAIM_CHILD],
            stdout=slave,
            stderr=slave,
            timeout=20,
        )
        seen = read_until(master, b"FD=")
    finally:
        os.close(master)
        os.close(slave)
    assert proc.returncode == 0
    assert b"FD=False;" in seen


def test_claim_terminal_stdout_is_a_no_op_when_stderr_is_not_a_tty():
    proc = subprocess.run(
        [sys.executable, "-c", CLAIM_CHILD],
        capture_output=True,
        timeout=20,
    )
    assert proc.returncode == 0
    assert proc.stdout.endswith(b"FD=False;")  # still on the pipe, untouched
