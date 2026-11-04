"""AsyncBackendClient: BackendPipe on asyncio readers, against real processes."""

import asyncio
import os
import subprocess
import sys
import time

from helpers import MOCK_ENV, Recorder

from voxize.tui.backend import AsyncBackendClient


def spawn_backend(**env):
    return subprocess.Popen(
        [sys.executable, "-m", "voxize.backend"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **MOCK_ENV, "VOXIZE_MOCK_SHORT": "1", **env},
    )


async def until(pred, what, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


def test_round_trip_with_a_real_backend():
    async def go():
        proc = spawn_backend()
        rec, lost = Recorder(), []
        client = AsyncBackendClient(proc, rec, lost.append)
        client.send("start", cwd=None, glossaries=[])
        await until(lambda: "RECORDING" in rec.states(), "RECORDING")
        client.send("stop")
        await until(lambda: "READY" in rec.states(), "READY")
        client.finish()
        client.send("close", trash_if_empty=False)
        client.close_stdin()
        await until(lambda: client.eof, "stdout EOF")
        assert proc.wait(timeout=10) == 0
        assert rec.events[0] == {"type": "hello", "protocol": 1}
        assert lost == []

    asyncio.run(go())


def test_lost_report_carries_the_final_stderr_line():
    code = (
        "import os, sys\n"
        "sys.stderr.write('first line\\nfinal words')\n"
        "sys.stderr.flush()\n"
        "os.close(1)\n"
        "import time; time.sleep(0.3)\n"
    )

    async def go():
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        lost = []
        AsyncBackendClient(proc, Recorder(), lost.append)
        await until(lambda: lost, "on_lost")
        proc.wait(timeout=10)
        return lost

    assert asyncio.run(go()) == ["final words"]


def test_backend_stderr_is_logged_not_echoed(capfd, caplog):
    code = "import sys; sys.stderr.write('hello from the backend\\n')"

    async def go():
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        client = AsyncBackendClient(proc, Recorder(), lambda _: None)
        client.finish()
        await until(lambda: client.eof, "stdout EOF")
        await until(lambda: client.last_stderr_line(), "stderr line")
        proc.wait(timeout=10)

    with caplog.at_level("DEBUG", logger="voxize.tui.backend"):
        asyncio.run(go())
    assert "hello from the backend" in caplog.text
    assert "hello from the backend" not in capfd.readouterr().err
