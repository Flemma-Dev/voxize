"""voxize-tui against a real backend in mock mode, end to end.

Headless: Textual's run_test needs no terminal. The clipboard is a fake —
a real one would write to the desktop clipboard.
"""

import asyncio
import contextlib
import os
import subprocess
import sys
import time

from helpers import MOCK_ENV
from tui_fakes import FakeClipboard

from voxize.mock import SHORT_TRANSCRIPT
from voxize.protocol import State
from voxize.tui import card as c
from voxize.tui.app import ConfirmCancel, VoxizeTui
from voxize.tui.backend import AsyncBackendClient


def spawn(env):
    return subprocess.Popen(
        [sys.executable, "-m", "voxize.backend"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **MOCK_ENV, **env},
    )


async def wait_for(pilot, pred, what, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await pilot.pause(0.02)


def at(app, state):
    return lambda: app.core.state.state == state


def run_session(env, drive):
    """Run one dictation; returns (app, copier, hung_up_by_app)."""
    proc = spawn(env)
    copier = FakeClipboard()
    app = VoxizeTui(
        lambda on_event, on_lost: AsyncBackendClient(proc, on_event, on_lost),
        cwd=None,
        copier=copier,
        install_signals=False,
    )

    async def go():
        async with app.run_test(size=(88, 24)) as pilot:
            await drive(app, pilot)
            await pilot.pause()

    try:
        asyncio.run(go())
        hung_up = proc.stdin.closed  # the app closed it, not this harness
    finally:
        with contextlib.suppress(OSError):
            proc.stdin.close()
        proc.wait(timeout=10)  # raises if the backend was left running
        proc.stdout.close()
        proc.stderr.close()
    return app, copier, hung_up


def test_ok_enter_stops_then_enter_copies_and_exits():
    async def drive(app, pilot):
        await wait_for(pilot, at(app, State.RECORDING), "RECORDING")
        # The mock batch returns the live preview as it stood at stop, so
        # stopping before the preview has played would be "no speech".
        await wait_for(pilot, lambda: app.card.text == SHORT_TRANSCRIPT, "live preview")
        await pilot.press("enter")
        await wait_for(pilot, at(app, State.READY), "READY")
        assert app.card.text == SHORT_TRANSCRIPT and not app.card.dimmed
        await pilot.press("enter")

    app, copier, hung_up = run_session({"VOXIZE_MOCK_SHORT": "1"}, drive)
    assert app.return_value == SHORT_TRANSCRIPT
    assert copier.copies == [SHORT_TRANSCRIPT] * 3  # batch, cleaned, Enter
    assert hung_up


def test_long_dictation_shows_an_estimate_then_delivers():
    seen = []

    async def drive(app, pilot):
        def ready():
            if app.core.state.state in (State.TRANSCRIBING, State.CLEANING):
                seen.append(app.card.progress)
            return app.core.state.state == State.READY

        await wait_for(pilot, ready, "READY", timeout=30)
        await pilot.press("q")

    app, copier, _ = run_session({"VOXIZE_STOP": "3000"}, drive)
    assert app.return_value and app.return_value == copier.copies[-1]
    assert any(p is not None and 0 < p < 1 for p in seen)


def test_no_speech_exits_with_nothing():
    async def drive(app, pilot):
        await wait_for(pilot, at(app, State.READY), "READY")
        assert app.card.text == c.NO_SPEECH
        await pilot.press("enter")

    app, copier, _ = run_session(
        {
            "VOXIZE_MOCK_SHORT": "1",
            "VOXIZE_STOP": "1600",
            "VOXIZE_MOCK_OUTCOME": "no-speech",
        },
        drive,
    )
    assert app.return_value is None and copier.copies == []


def test_batch_failure_delivers_the_live_preview():
    async def drive(app, pilot):
        await wait_for(pilot, at(app, State.READY), "READY")
        assert app.card.notice == c.NOTICE_BATCH_FAILED
        await pilot.press("q")

    app, copier, _ = run_session(
        {
            "VOXIZE_MOCK_SHORT": "1",
            "VOXIZE_STOP": "1600",
            "VOXIZE_MOCK_OUTCOME": "batch-fail",
        },
        drive,
    )
    assert app.return_value == SHORT_TRANSCRIPT
    assert copier.copies == []  # no result event: q exits without copying


def test_cleanup_failure_delivers_the_batch_transcript():
    async def drive(app, pilot):
        await wait_for(pilot, at(app, State.READY), "READY")
        assert app.card.notice == c.NOTICE_CLEANUP_FAILED
        await pilot.press("escape")

    app, copier, _ = run_session(
        {
            "VOXIZE_MOCK_SHORT": "1",
            "VOXIZE_STOP": "1600",
            "VOXIZE_MOCK_OUTCOME": "cleanup-fail",
        },
        drive,
    )
    assert app.return_value == SHORT_TRANSCRIPT
    assert copier.copies == [SHORT_TRANSCRIPT]  # batch only


def test_lock_error_shows_the_error_card():
    async def drive(app, pilot):
        await wait_for(pilot, at(app, State.ERROR), "ERROR")
        assert app.card.text == "Another Voxize instance is recording"
        await pilot.press("q")

    app, _, _ = run_session({"VOXIZE_MOCK_OUTCOME": "error"}, drive)
    assert app.return_value is None


def test_preview_lost_is_shown_then_the_batch_delivers():
    async def drive(app, pilot):
        await wait_for(
            pilot, lambda: app.card.notice == c.NOTICE_PREVIEW_LOST, "preview notice"
        )
        await wait_for(pilot, at(app, State.READY), "READY")
        assert app.card.notice == ""
        await pilot.press("enter")

    app, _, _ = run_session(
        {"VOXIZE_MOCK_SHORT": "1", "VOXIZE_STOP": "1600", "VOXIZE_ERROR": "300"}, drive
    )
    assert app.return_value == SHORT_TRANSCRIPT


def test_cancel_with_discard_exits_and_the_backend_goes_too():
    async def drive(app, pilot):
        await wait_for(pilot, at(app, State.RECORDING), "RECORDING")
        await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmCancel), "dialog")
        await pilot.click("#discard")
        await wait_for(pilot, at(app, State.CANCELLED), "CANCELLED")

    app, copier, hung_up = run_session({"VOXIZE_MOCK_SHORT": "1"}, drive)
    assert app.return_value is None and copier.copies == []
    assert hung_up


def test_a_killed_backend_shows_an_error():
    procs = []

    async def drive(app, pilot):
        await wait_for(pilot, at(app, State.RECORDING), "RECORDING")
        app.core.transport._proc.kill()
        procs.append(app.core.transport._proc)
        await wait_for(pilot, at(app, State.ERROR), "ERROR")
        assert app.card.text.startswith("Backend exited unexpectedly")
        await pilot.press("q")

    app, _, _ = run_session({"VOXIZE_MOCK_SHORT": "1"}, drive)
    assert app.return_value is None
