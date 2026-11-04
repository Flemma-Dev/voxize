"""VoxizeTui driven headless (Textual run_test) with a fake backend and clipboard."""

import asyncio

import pytest
from textual.widgets import ProgressBar, Static
from tui_fakes import FakeClipboard, FakeTransport

from voxize.protocol import PROTOCOL_VERSION, State
from voxize.tui import card as c
from voxize.tui.app import VoxizeTui
from voxize.tui.widgets import LevelBar


def ev(type_, **fields):
    return {"type": type_, **fields}


def st(name, error=None):
    return ev("state", state=name, error=error)


HELLO = ev("hello", protocol=PROTOCOL_VERSION)
SOURCES = [{"path": "/work/WHISPER.txt", "content": "Supabase, Luup."}]
RECORDING = (HELLO, st("WARMING"), st("RECORDING"))


def ready_with(text, batch=None):
    """Scenario to READY; `batch` (default: same as `text`) is the pre-cleanup text."""
    batch = text if batch is None else batch
    return (
        *RECORDING,
        ev("text", stage="live", op="replace", text="draft"),
        st("TRANSCRIBING"),
        ev("text", stage="batch", op="replace", text=batch),
        ev("result", stage="batch", text=batch),
        st("CLEANING"),
        ev("text", stage="batch", op="replace", text=batch),
        ev("text", stage="cleaned", op="replace", text=text),
        ev("result", stage="cleaned", text=text),
        st("READY"),
    )


def make(copier=None, **kw):
    transport = FakeTransport()
    copier = copier or FakeClipboard()
    app = VoxizeTui(
        lambda on_event, on_lost: transport,
        cwd="/work",
        copier=copier,
        install_signals=False,
        **kw,
    )
    return app, transport, copier


def run(app, scenario, size=(88, 24)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await scenario(pilot)

    asyncio.run(go())


async def feed(app, pilot, *events):
    for event in events:
        app.on_backend_event(event)
    await pilot.pause()


def text(app, wid):
    return str(app.screen.query_one(f"#{wid}", Static).content)


def shown(app, wid):
    return app.screen.query_one(f"#{wid}").display


def test_mount_sends_start_with_the_cwd():
    app, transport, _ = make()

    async def scenario(pilot):
        assert transport.sent[0] == ("start", {"cwd": "/work", "glossaries": []})

    run(app, scenario)


def test_warming_then_recording():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, HELLO, st("WARMING"))
        assert (text(app, "dot"), text(app, "status")) == ("○", "Warming up…")
        assert text(app, "transcript") == c.PLACEHOLDER
        assert app.screen.query_one("#transcript").has_class("-dimmed")
        assert shown(app, "level") and not shown(app, "progress")
        assert text(app, "keys") == "enter stop · esc cancel"
        await feed(
            app,
            pilot,
            st("RECORDING"),
            ev("text", stage="live", op="replace", text="hello"),
        )
        assert text(app, "status") == "Listening"
        assert app.screen.query_one("#dot").has_class("tone-recording")
        assert text(app, "transcript") == "hello"
        assert not app.screen.query_one("#transcript").has_class("-dimmed")
        assert text(app, "timer") == "00:00"

    run(app, scenario)


def test_level_bar_follows_the_zone():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING, ev("level", dbfs=-8.0))
        bar = app.screen.query_one(LevelBar)
        assert bar.has_class("zone-hot") and not bar.has_class("zone-good")
        assert bar.fraction == pytest.approx(c.dbfs_to_fraction(-8.0))

    run(app, scenario)


def test_working_dims_and_shows_progress():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(
            app,
            pilot,
            *RECORDING,
            ev("text", stage="live", op="replace", text="draft"),
            st("TRANSCRIBING"),
        )
        assert text(app, "status") == "Working…"
        assert app.screen.query_one("#transcript").has_class("-dimmed")
        assert shown(app, "progress") and not shown(app, "level")
        await feed(app, pilot, ev("progress", fraction=None))
        assert app.screen.query_one(ProgressBar).total is None
        await feed(
            app, pilot, ev("progress", fraction=0.4), ev("stalled", stalled=True)
        )
        bar = app.screen.query_one(ProgressBar)
        assert (bar.total, bar.progress) == (1.0, 0.4)
        assert text(app, "status") == "Still working…"

    run(app, scenario)


def test_ready_shows_text_costs_and_keys():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, ev("context", sources=SOURCES), *ready_with("Fix it."))
        await feed(
            app,
            pilot,
            ev("costs", live=0.0001, batch=0.0002, cleanup=0.0001, total=0.0004),
        )
        assert text(app, "status") == "Ready"
        assert text(app, "transcript") == "Fix it."
        assert text(app, "cost") == "$0.0004" and shown(app, "cost")
        assert text(app, "vocab") == "≡ 1"
        assert (
            text(app, "keys") == "enter copy & exit · ^c copy · q exit · v vocabulary"
        )
        assert not shown(app, "level") and not shown(app, "progress")

    run(app, scenario)


def test_cleanup_failure_notice():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(
            app,
            pilot,
            *RECORDING,
            st("TRANSCRIBING"),
            st("CLEANING"),
            ev("text", stage="batch", op="replace", text="Fix it."),
            ev("failure", phase="cleanup", message="simulated", fallback="batch"),
            st("READY"),
        )
        assert text(app, "notice") == c.NOTICE_CLEANUP_FAILED and shown(app, "notice")
        assert text(app, "transcript") == "Fix it."

    run(app, scenario)


def test_error_card():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(
            app, pilot, HELLO, st("ERROR", error="Another Voxize instance is recording")
        )
        assert text(app, "status") == "Error"
        assert text(app, "transcript") == "Another Voxize instance is recording"
        assert not shown(app, "timer") and not shown(app, "level")
        assert text(app, "keys") == "enter exit · q exit"

    run(app, scenario)


def test_hello_timeout_is_an_error():
    app, transport, _ = make(hello_timeout_s=0.05)

    async def scenario(pilot):
        await pilot.pause(0.3)
        assert app.core.state.state == State.ERROR
        assert text(app, "transcript") == "Backend did not start"
        assert ("close", {"trash_if_empty": False}) in transport.sent

    run(app, scenario)


def test_protocol_mismatch_is_an_error():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, ev("hello", protocol=99), st("WARMING"))
        assert app.core.state.state == State.ERROR
        assert text(app, "transcript") == (
            f"Backend speaks protocol 99, this terminal client expects {PROTOCOL_VERSION}"
        )

    run(app, scenario)


def test_a_lost_backend_is_an_error_with_its_last_words():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        app.core.backend_lost("Traceback: boom")
        await pilot.pause()
        assert text(app, "transcript") == "Backend exited unexpectedly: Traceback: boom"

    run(app, scenario)


def test_results_are_copied_in_order():
    app, _, copier = make()

    async def scenario(pilot):
        await feed(app, pilot, *ready_with("Fix it.", batch="fix it"))
        await pilot.pause()
        assert copier.copies == ["fix it", "Fix it."]
        assert text(app, "transcript") == "Fix it."
        assert not shown(app, "notice")

    run(app, scenario)


def test_osc52_only_shows_the_clipboard_notice():
    app, _, _ = make(copier=FakeClipboard(osc52_only=True))

    async def scenario(pilot):
        await feed(app, pilot, *ready_with("Fix it."))
        await pilot.pause()
        assert text(app, "notice") == c.NOTICE_NO_CLIPBOARD

    run(app, scenario)


def test_session_event_starts_tui_log(tmp_path):
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, HELLO, ev("session", dir=str(tmp_path)))
        assert app.session_dir == str(tmp_path)
        assert (tmp_path / "tui.log").exists()

    run(app, scenario)


def test_the_timer_ticks_while_recording():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        await pilot.pause(1.2)
        assert text(app, "timer") == "00:01"

    run(app, scenario)


@pytest.mark.parametrize("size", [(60, 12), (88, 24), (200, 60)])
def test_card_fills_small_terminals_and_is_capped_and_centred(size):
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        region = app.screen.query_one("#card").region
        width, height = size
        assert (region.width, region.height) == (min(width, 88), min(height, 24))
        assert region.x == (width - region.width) // 2
        assert region.y == (height - region.height) // 2
        assert app.screen.query_one("#scroll").region.height >= 1
        assert text(app, "status") == "Listening"

    run(app, scenario, size)
