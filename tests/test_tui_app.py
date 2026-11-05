"""VoxizeTui driven headless (Textual run_test) with a fake backend and clipboard."""

import asyncio
import signal

import pytest
from textual.color import Color
from textual.containers import Vertical
from textual.widgets import ProgressBar, Static
from textual.widgets._progress_bar import Bar
from tui_fakes import FakeClipboard, FakeTransport

from voxize.protocol import PROTOCOL_VERSION, State
from voxize.tui import card as c
from voxize.tui.app import CardScreen, ConfirmCancel, Costs, Vocabulary, VoxizeTui
from voxize.tui.widgets import LevelBar, ThinScrollBarRender


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


def keys(app):
    return app.screen.query_one("#keys").render().plain


def top(app, wid):
    return app.screen.query_one(f"#{wid}").region.y


# Textual's thin bar: full cells, and half cells where fill and track meet.
THIN_LINE = set("━╸╺")


def drawn_line(widget):
    """A one-line widget as drawn: its glyphs, and each cell's colour."""
    line = widget.render_line(0)
    colours = []
    for segment in line:
        colours += [segment.style.color.triplet.hex] * len(segment.text)
    return line.text, colours


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
        assert keys(app) == "Enter Stop · Esc Cancel"
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


def test_level_bar_is_a_thin_line_in_the_zone_colour_on_a_grey_track():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING, ev("level", dbfs=-30.0))  # half, good
        bar = app.screen.query_one(LevelBar)
        width = bar.size.width
        assert width % 2 == 0  # half is a whole number of cells
        glyphs, colours = drawn_line(bar)
        assert len(glyphs) == width and set(glyphs) <= THIN_LINE
        fill, track = colours[: width // 2], colours[width // 2 :]
        assert set(fill) == {bar.styles.color.hex.lower()}
        assert len(set(track)) == 1 and track[0] != fill[0]

    run(app, scenario)


def test_a_quiet_level_stays_visible_on_the_track():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING, ev("level", dbfs=-30.0))
        bar = app.screen.query_one(LevelBar)
        track = drawn_line(bar)[1][-1]
        await feed(app, pilot, ev("level", dbfs=-50.0))
        assert bar.has_class("zone-quiet")
        assert drawn_line(bar)[1][0] != track

    run(app, scenario)


def test_the_progress_bar_draws_the_same_line_as_the_level_meter():
    # As in the GUI: one look for both strips. Textual's stock bar would
    # pulse red without an estimate and turn green when complete.
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING, ev("level", dbfs=-30.0))  # half, good
        level = app.screen.query_one(LevelBar)
        width = level.size.width
        _, colours = drawn_line(level)
        fill, track = colours[0], colours[-1]

        await feed(app, pilot, st("TRANSCRIBING"), ev("progress", fraction=0.5))
        bar = app.screen.query_one(ProgressBar).query_one(Bar)
        assert bar.size.width == width
        glyphs, colours = drawn_line(bar)
        assert len(glyphs) == width and set(glyphs) <= THIN_LINE
        assert colours == [fill] * (width // 2) + [track] * (width - width // 2)

        await feed(app, pilot, ev("progress", fraction=None))
        assert set(drawn_line(bar)[1]) == {fill, track}

        await feed(app, pilot, ev("progress", fraction=1.0))
        assert set(drawn_line(bar)[1]) == {fill}

    run(app, scenario)


def test_every_level_zone_has_its_own_colour():
    # In textual-dark, $accent and $warning are the same amber: a good level
    # must not look like a hot one.
    app, _, _ = make()
    colours = {}

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        bar = app.screen.query_one(LevelBar)
        for zone, dbfs in (
            ("quiet", -50.0),
            ("good", -25.0),
            ("hot", -8.0),
            ("clip", -1.0),
        ):
            await feed(app, pilot, ev("level", dbfs=dbfs))
            assert bar.has_class(f"zone-{zone}")
            colours[zone] = bar.styles.color

    run(app, scenario)
    assert len(set(colours.values())) == 4, colours


@pytest.mark.parametrize(
    ("position", "glyphs", "parts"),
    [
        # 10 rows showing 10 of 40: a 2.5-row thumb, drawn in half rows.
        (0, "┃┃╹┃┃┃┃┃┃┃", "TTTvvvvvvv"),
        (30, "┃┃┃┃┃┃┃╻┃┃", "^^^^^^^TTT"),
    ],
)
def test_thin_scrollbar_draws_the_strips_line_upright(position, glyphs, parts):
    track, thumb = Color.parse("#3b3b3b"), Color.parse("#a0a0a0")
    rows = ThinScrollBarRender.render_bar(
        size=10,
        virtual_size=40,
        window_size=10,
        position=position,
        back_color=track.rich_color,
        bar_color=thumb.rich_color,
    ).segments
    assert "".join(row.text for row in rows) == glyphs
    # T: thumb, grabbed to drag; ^ / v: track, clicked to page up / down.
    meaning = {"grab": "T", "scroll_up": "^", "scroll_down": "v"}
    assert "".join(meaning[row.style.meta["@mouse.down"]] for row in rows) == parts
    colour = {"T": thumb.rich_color, "^": track.rich_color, "v": track.rich_color}
    assert [row.style.color for row in rows] == [colour[p] for p in parts]


def test_the_transcript_scrollbar_is_a_thin_line_on_the_strips_track():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING, ev("level", dbfs=-30.0))  # good zone
        track = drawn_line(app.screen.query_one(LevelBar))[1][-1]
        long = " ".join(["Refactor the middleware to use JWT tokens."] * 60)
        await feed(app, pilot, ev("text", stage="live", op="replace", text=long))
        await pilot.pause()
        bar = app.screen.query_one("#scroll").vertical_scrollbar
        assert bar.display and bar.size.width == 1
        cells = [bar.render_line(y) for y in range(bar.size.height)]
        assert {cell.text for cell in cells} <= {"┃", "╹", "╻"}
        colours = {seg.style.color.triplet.hex for cell in cells for seg in cell}
        assert track in colours and len(colours) == 2  # track and thumb

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
            keys(app) == "Enter Copy & Exit · C Copy · Q Exit · $ Costs · V Vocabulary"
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
        assert keys(app) == "Enter Exit · Q Exit"

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


def test_the_header_names_the_mode_in_every_state():
    app, _, _ = make()

    async def scenario(pilot):
        assert text(app, "title") == "Dictation"
        for state in ("WARMING", "RECORDING", "TRANSCRIBING", "CLEANING", "READY"):
            await feed(app, pilot, *((HELLO,) if state == "WARMING" else ()), st(state))
            assert text(app, "title") == "Dictation", state

    run(app, scenario)


def test_status_sits_below_the_transcript_above_the_strip_and_the_keys():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(
            app,
            pilot,
            ev("context", sources=SOURCES),
            *RECORDING,
            ev("preview_lost", reason="ws closed"),
        )
        card = app.screen.query_one("#card").region
        assert top(app, "title") == top(app, "vocab") == card.y
        assert top(app, "dot") == top(app, "status") == top(app, "timer")
        assert top(app, "scroll") < top(app, "notice") < top(app, "status")
        assert top(app, "status") + 1 == top(app, "level")
        assert top(app, "level") + 1 == top(app, "keys") == card.bottom - 1

    run(app, scenario)


def test_the_status_row_and_keys_hold_still_from_start_to_finish():
    # No strip at INITIALIZING or READY, but its row stays: a whole terminal
    # line moving is far more than the GUI's 2px strip coming and going.
    app, _, _ = make()
    rows = []

    async def scenario(pilot):
        rows.append((top(app, "status"), top(app, "keys")))
        for events in (RECORDING, (st("TRANSCRIBING"),), ready_with("Fix it.")[-5:]):
            await feed(app, pilot, *events)
            rows.append((top(app, "status"), top(app, "keys")))

    run(app, scenario)
    assert len(set(rows)) == 1, rows


def test_the_header_holds_still_at_ready_and_the_cost_joins_the_status_row():
    # As in the GUI, whose cost is a tooltip on the status dot: nothing in
    # the header moves between states.
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, ev("context", sources=SOURCES), *RECORDING)
        header = app.screen.query_one("#header")
        before = [(w.id, w.region) for w in header.query(Static) if w.display]
        await feed(
            app,
            pilot,
            *ready_with("Fix it.")[3:],
            ev("costs", live=0.0001, batch=0.0002, cleanup=0.0001, total=0.0004),
        )
        after = [(w.id, w.region) for w in header.query(Static) if w.display]
        assert after == before
        assert top(app, "cost") == top(app, "status")
        timer = app.screen.query_one("#timer").region
        assert app.screen.query_one("#cost").region.x > timer.right
        assert text(app, "cost") == "$0.0004"

    run(app, scenario)


def test_key_hints_set_the_key_apart_from_its_action():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        hints = app.screen.query_one("#keys")
        drawn = hints.render()
        assert drawn.plain == "Enter Stop · Esc Cancel"
        styled = [(drawn.plain[s.start : s.end], s.style) for s in drawn.spans]
        assert [part for part, style in styled if style.bold] == ["Enter", "Esc"]
        # Brighter than the muted actions and dots around them.
        assert all(style.color != hints.rich_style.color for _, style in styled)

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


def test_enter_while_recording_sends_stop():
    app, transport, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        await pilot.press("enter")
        assert transport.types()[-1] == "stop"

    run(app, scenario)


@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
def test_escape_before_recording_cancels_at_once(key):
    app, transport, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, HELLO, st("WARMING"))
        await pilot.press(key)
        assert transport.types()[-1] == "cancel"
        assert isinstance(app.screen, CardScreen)

    run(app, scenario)


@pytest.mark.parametrize("key", ["escape", "ctrl+q", "ctrl+c"])
def test_cancel_from_recording_asks_and_keep_is_the_default(key):
    app, transport, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        await pilot.press(key)
        assert isinstance(app.screen, ConfirmCancel)
        await pilot.press("enter")  # Keep has focus
        assert isinstance(app.screen, CardScreen)
        await pilot.press(key)
        await pilot.press("escape")  # Escape in the dialog means Keep
        assert isinstance(app.screen, CardScreen)
        assert "cancel" not in transport.types()

    run(app, scenario)


@pytest.mark.parametrize("key", ["ctrl+q", "ctrl+c"])
def test_ctrl_q_or_c_inside_the_dialog_does_nothing(key):
    app, transport, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        await pilot.press("escape", key)
        assert isinstance(app.screen, ConfirmCancel)
        assert "cancel" not in transport.types()

    run(app, scenario)


def test_discard_cancels_and_cancelled_exits_with_nothing():
    app, transport, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        await pilot.press("escape")
        await pilot.click("#discard")
        await pilot.pause()
        assert transport.types()[-1] == "cancel"
        await feed(app, pilot, st("CANCELLED"))
        await pilot.pause()

    run(app, scenario)
    assert app.return_value is None
    assert transport.types()[-1] == "close"


def test_ready_force_closes_the_dialog_without_cancelling():
    app, transport, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING, st("TRANSCRIBING"))
        await pilot.press("escape")
        assert isinstance(app.screen, ConfirmCancel)
        await feed(app, pilot, st("TRANSCRIBING"), st("CLEANING"))
        assert isinstance(app.screen, ConfirmCancel)  # still a question
        await feed(app, pilot, st("READY"))
        await pilot.pause()
        assert isinstance(app.screen, CardScreen)
        assert "cancel" not in transport.types()

    run(app, scenario)


def test_enter_at_ready_copies_and_exits_with_the_transcript():
    app, transport, copier = make()

    async def scenario(pilot):
        await feed(app, pilot, *ready_with("Fix it."))
        await pilot.press("enter")
        await pilot.pause()

    run(app, scenario)
    assert app.return_value == "Fix it."
    assert copier.copies == ["Fix it.", "Fix it.", "Fix it."]
    assert transport.sent[-1] == ("close", {"trash_if_empty": False})
    assert transport.stdin_closed


@pytest.mark.parametrize("key", ["q", "Q"])
def test_q_is_ignored_in_flight_and_exits_at_ready(key):
    app, transport, copier = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        await pilot.press(key)
        assert app.is_running and "close" not in transport.types()
        await feed(app, pilot, *ready_with("Fix it.")[3:])
        await pilot.press(key)
        await pilot.pause()

    run(app, scenario)
    assert app.return_value == "Fix it."
    assert len(copier.copies) == 2  # the two results; q does not copy


@pytest.mark.parametrize("key", ["c", "C"])
def test_c_at_ready_copies_and_stays(key):
    app, _, copier = make()

    async def scenario(pilot):
        await feed(app, pilot, *ready_with("Fix it."))
        await pilot.press(key)
        await pilot.pause()
        assert copier.copies[-1] == "Fix it." and len(copier.copies) == 3
        assert app.is_running

    run(app, scenario)


def test_c_in_flight_does_nothing():
    app, _, copier = make()

    async def scenario(pilot):
        await feed(
            app, pilot, *RECORDING, ev("text", stage="live", op="replace", text="hi")
        )
        await pilot.press("c")
        await pilot.pause()
        assert copier.copies == [] and app.is_running

    run(app, scenario)


@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
def test_escape_or_ctrl_c_at_ready_exits_with_the_transcript_without_copying(key):
    # Ctrl+C is the terminal's interrupt key: it acts as Escape, not Copy.
    app, transport, copier = make()

    async def scenario(pilot):
        await feed(app, pilot, *ready_with("Fix it."))
        await pilot.press(key)
        await pilot.pause()

    run(app, scenario)
    assert app.return_value == "Fix it."
    assert len(copier.copies) == 2  # the two results only
    assert transport.sent[-1] == ("close", {"trash_if_empty": False})


def test_enter_at_ready_with_nothing_to_deliver_exits_with_nothing():
    app, _, copier = make()

    async def scenario(pilot):
        await feed(
            app, pilot, *RECORDING, st("TRANSCRIBING"), ev("no_speech"), st("READY")
        )
        assert text(app, "transcript") == c.NO_SPEECH
        await pilot.press("enter")
        await pilot.pause()

    run(app, scenario)
    assert app.return_value is None
    assert copier.copies == []


def test_enter_at_error_exits():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, HELLO, st("ERROR", error="boom"))
        await pilot.press("enter")
        await pilot.pause()

    run(app, scenario)
    assert app.return_value is None


@pytest.mark.parametrize("key", ["v", "V"])
def test_vocabulary_opens_and_closes(key):
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, ev("context", sources=SOURCES), *RECORDING)
        await pilot.press(key)
        assert isinstance(app.screen, Vocabulary)
        paths = [str(s.content) for s in app.screen.query(".vocab-path")]
        assert paths == ["/work/WHISPER.txt"]
        await pilot.press(key)
        assert isinstance(app.screen, CardScreen)
        await pilot.press(key, "escape")
        assert isinstance(app.screen, CardScreen)

    run(app, scenario)


def test_v_without_sources_does_nothing():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        await pilot.press("v")
        assert isinstance(app.screen, CardScreen)

    run(app, scenario)


def test_dollar_opens_the_cost_breakdown_at_ready_and_escape_closes_it():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *ready_with("Fix it."))
        await feed(
            app,
            pilot,
            ev("costs", live=0.0001, batch=0.0002, cleanup=None, total=0.0003),
        )
        await pilot.press("$")
        assert isinstance(app.screen, Costs)
        assert str(app.screen.query_one("#costs-title").content) == "Session cost"
        labels = [str(s.content) for s in app.screen.query(".cost-label")]
        values = [str(s.content) for s in app.screen.query(".cost-value")]
        assert labels == ["Live", "Batch", "Total"]
        assert values == ["$0.0001", "$0.0002", "$0.0003"]
        await pilot.press("escape")
        assert isinstance(app.screen, CardScreen)
        await pilot.press("$")
        assert isinstance(app.screen, Costs)
        await pilot.press("$")
        assert isinstance(app.screen, CardScreen)
        await pilot.press("$")
        await pilot.press("q")
        assert isinstance(app.screen, CardScreen)

    run(app, scenario)


def test_dollar_shows_all_three_parts():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(
            app,
            pilot,
            *ready_with("Fix it."),
            ev("costs", live=0.0001, batch=0.0002, cleanup=0.0001, total=0.0004),
        )
        await pilot.press("$")
        labels = [str(s.content) for s in app.screen.query(".cost-label")]
        values = [str(s.content) for s in app.screen.query(".cost-value")]
        assert labels == ["Live", "Batch", "Cleanup", "Total"]
        assert values == ["$0.0001", "$0.0002", "$0.0001", "$0.0004"]

    run(app, scenario)


def test_dollar_before_ready_or_without_costs_does_nothing():
    app, _, _ = make()

    async def scenario(pilot):
        await feed(
            app,
            pilot,
            *RECORDING,
            ev("costs", live=0.0001, batch=None, cleanup=None, total=0.0001),
        )
        await pilot.press("$")
        assert isinstance(app.screen, CardScreen)

    run(app, scenario)

    app, _, _ = make()

    async def scenario_none(pilot):
        await feed(
            app,
            pilot,
            *ready_with("Fix it."),
            ev("costs", live=None, batch=None, cleanup=None, total=None),
        )
        await pilot.press("$")
        assert isinstance(app.screen, CardScreen)

    run(app, scenario_none)


def test_a_signal_hangs_up_without_close_and_prints_nothing():
    app, transport, _ = make()

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        app._on_signal(signal.SIGTERM)
        await pilot.pause()

    run(app, scenario)
    assert app.return_value is None
    assert transport.stdin_closed and "close" not in transport.types()


class GatedClipboard(FakeClipboard):
    """A copy that stays in flight until the test releases it."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def copy(self, text):
        self.started.set()
        await self.release.wait()
        return await super().copy(text)


def test_a_signal_during_the_exit_copy_prints_nothing():
    copier = GatedClipboard()
    app, transport, _ = make(copier)

    async def scenario(pilot):
        await feed(app, pilot, *ready_with("Fix it."))
        # The results' own copies are gated too; let them drain first.
        copier.release.set()
        await pilot.pause()
        copier.release.clear()
        copier.started.clear()
        # pilot.press would wait for the gated copy; run the key's action aside.
        enter = asyncio.create_task(app.action_primary())
        await asyncio.wait_for(copier.started.wait(), 5)
        app._on_signal(signal.SIGTERM)
        copier.release.set()
        await enter
        await pilot.pause()

    run(app, scenario)
    assert app.return_value is None
    assert transport.stdin_closed and "close" not in transport.types()


class RaisingClipboard(FakeClipboard):
    async def copy(self, text):
        raise RuntimeError("boom")


def test_a_failing_copy_is_logged_and_does_not_crash(caplog):
    app, _, _ = make(RaisingClipboard())

    async def scenario(pilot):
        with caplog.at_level("ERROR", logger="voxize.tui.app"):
            await feed(app, pilot, *ready_with("Fix it."))
            await pilot.pause()
        assert app.is_running
        assert text(app, "transcript")

    run(app, scenario)
    assert any(
        r.name == "voxize.tui.app" and "copy failed" in r.getMessage()
        for r in caplog.records
    )


def test_a_background_tints_the_card_and_its_dialogs():
    app, _, _ = make(background="#100b06")

    async def scenario(pilot):
        await feed(app, pilot, *RECORDING)
        variables = app.get_css_variables()
        assert variables["background"].lower() == "#100b06"
        # textual-dark's own step from background to surface (#121212 -> #1e1e1e).
        assert variables["surface"].lower() == "#1c1712"
        assert app.screen.styles.background == Color.parse("#100b06")
        await pilot.press("escape")
        assert isinstance(app.screen, ConfirmCancel)
        dialog = app.screen.query_one(Vertical)
        assert dialog.styles.background == Color.parse("#1c1712")

    run(app, scenario)


def test_without_a_background_the_theme_is_textual_dark():
    app, _, _ = make()

    async def scenario(pilot):
        assert app.theme == "textual-dark"
        assert app.get_css_variables()["surface"].lower() == "#1e1e1e"

    run(app, scenario)


def test_an_unparseable_background_is_logged_and_ignored(caplog):
    app, _, _ = make(background="amberish")

    async def scenario(pilot):
        assert app.theme == "textual-dark"

    with caplog.at_level("WARNING", logger="voxize.tui.app"):
        run(app, scenario)
    assert "VOXIZE_TUI_BACKGROUND='amberish'" in caplog.text
