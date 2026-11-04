import pytest
from helpers import Recorder, run_until

from voxize.backend.session import Session
from voxize.mock import SHORT_TRANSCRIPT, MockOptions


@pytest.fixture
def make():
    sessions = []

    def _make(**kw):
        rec = Recorder()
        exits: list[bool] = []
        opts = MockOptions(fast=True, ttfb_ms=50, **kw)
        s = Session(rec, mock=opts, on_exit=lambda: exits.append(True))
        sessions.append(s)
        return s, rec, exits

    yield _make
    for s in sessions:
        s.close(trash_if_empty=False)  # stops every timer before the next test


def ready(rec):
    return "READY" in rec.states()


def test_short_dictation_runs_every_phase(make):
    s, rec, _ = make(short=True, stop_ms=1600)
    s.start(None, [])
    run_until(lambda: ready(rec), what="READY")
    assert rec.events[0] == {"type": "context", "sources": []}
    assert rec.states() == ["WARMING", "RECORDING", "TRANSCRIBING", "CLEANING", "READY"]
    live = [e for e in rec.of("text") if e["stage"] == "live"]
    assert live[0]["op"] == "replace"
    assert all(e["op"] == "append" for e in live[1:])
    assert "".join(e["text"] for e in live) == SHORT_TRANSCRIPT
    assert [(r["stage"], r["text"]) for r in rec.of("result")] == [
        ("batch", SHORT_TRANSCRIPT),
        ("cleaned", SHORT_TRANSCRIPT),
    ]
    # Under 50 chars of live text there is no estimate.
    assert [p["fraction"] for p in rec.of("progress")] == [None]
    assert rec.of("level")
    assert rec.of("costs") == []  # mock providers report no usage


def test_cleanup_handoff_order(make):
    s, rec, _ = make(short=True, stop_ms=1600)
    s.start(None, [])
    run_until(lambda: ready(rec), what="READY")
    seq = [
        (e["type"], e.get("state") or e.get("stage"), e.get("op"))
        for e in rec.events
        if e["type"] in ("state", "text")
    ]
    i = seq.index(("state", "CLEANING", None))
    # The full batch transcript is re-sent for the cleanup wait, then the
    # first cleaned token replaces it.
    assert seq[i + 1] == ("text", "batch", "replace")
    assert seq[i + 2] == ("text", "cleaned", "replace")
    first_batch = seq.index(("text", "batch", "replace"))
    assert seq[first_batch - 1] == ("state", "TRANSCRIBING", None)


def test_long_dictation_progress_is_monotonic_and_completes(make):
    s, rec, _ = make(stop_ms=3000)
    s.start(None, [])
    run_until(lambda: ready(rec), timeout_s=30, what="READY")
    fractions = [p["fraction"] for p in rec.of("progress")]
    assert None not in fractions
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1.0
    assert any(0.0 < f < 1.0 for f in fractions)


def test_no_speech(make):
    s, rec, _ = make(short=True, stop_ms=1600, outcome="no-speech")
    s.start(None, [])
    run_until(lambda: ready(rec), what="READY")
    assert rec.states() == ["WARMING", "RECORDING", "TRANSCRIBING", "READY"]
    assert rec.of("no_speech") == [{"type": "no_speech"}]
    assert rec.of("result") == []


def test_batch_failure_falls_back_to_live_preview(make):
    s, rec, _ = make(short=True, stop_ms=1600, outcome="batch-fail")
    s.start(None, [])
    run_until(lambda: ready(rec), what="READY")
    [failure] = rec.of("failure")
    assert failure["phase"] == "batch"
    assert failure["fallback"] == "live"
    assert "simulated" in failure["message"]
    i = rec.events.index(failure)
    assert rec.events[i - 1] == {
        "type": "text",
        "stage": "live",
        "op": "replace",
        "text": SHORT_TRANSCRIPT,
    }
    assert (
        rec.events[i + 1]["type"] == "state" and rec.events[i + 1]["state"] == "READY"
    )
    assert "CLEANING" not in rec.states()
    assert rec.of("result") == []


def test_batch_failure_without_live_text(make):
    s, rec, _ = make(short=True, stop_ms=500, error_ms=10, outcome="batch-fail")
    s.start(None, [])
    run_until(lambda: ready(rec), what="READY")
    assert rec.of("preview_lost")
    assert not [e for e in rec.of("text") if e["stage"] == "live"]
    [failure] = rec.of("failure")
    assert failure["fallback"] is None


def test_cleanup_failure_falls_back_to_batch(make):
    s, rec, _ = make(short=True, stop_ms=1600, outcome="cleanup-fail")
    s.start(None, [])
    run_until(lambda: ready(rec), what="READY")
    [failure] = rec.of("failure")
    assert failure["phase"] == "cleanup"
    assert failure["fallback"] == "batch"
    i = rec.events.index(failure)
    assert rec.events[i - 1] == {
        "type": "text",
        "stage": "batch",
        "op": "replace",
        "text": SHORT_TRANSCRIPT,
    }
    assert [r["stage"] for r in rec.of("result")] == ["batch"]


def test_preview_lost_during_recording_is_not_fatal(make):
    s, rec, _ = make(short=True, stop_ms=1600, error_ms=300)
    s.start(None, [])
    run_until(lambda: ready(rec), what="READY")
    assert rec.of("preview_lost") == [
        {"type": "preview_lost", "reason": "Simulated: WebSocket connection lost"}
    ]


def test_lock_error_outcome(make):
    s, rec, _ = make(outcome="error")
    s.start(None, [])
    assert rec.of("state") == [
        {
            "type": "state",
            "state": "ERROR",
            "error": "Another Voxize instance is recording",
        }
    ]


def test_startup_failure_shows_in_error_card(make):
    s, rec, _ = make()
    s.fail_startup(["OpenAI API key not found in keyring", "Something else"])
    s.start(None, [])  # ignored: the session is already over
    assert rec.events == [
        {
            "type": "state",
            "state": "ERROR",
            "error": "OpenAI API key not found in keyring\nSomething else",
        }
    ]


def test_stop_command(make):
    s, rec, _ = make(short=True)
    s.stop()  # before start: ignored
    assert rec.events == []
    s.start(None, [])
    run_until(lambda: "RECORDING" in rec.states(), what="RECORDING")
    s.stop()
    run_until(lambda: ready(rec), what="READY")


def test_commands_after_ready_are_ignored(make):
    s, rec, _ = make(short=True, stop_ms=1600)
    s.start(None, [])
    run_until(lambda: ready(rec), what="READY")
    n = len(rec.events)
    s.stop()
    s.cancel()
    s.start(None, [])
    assert len(rec.events) == n


def test_cancel_while_recording(make):
    s, rec, exits = make(short=True)
    s.start(None, [])
    run_until(lambda: "RECORDING" in rec.states(), what="RECORDING")
    s.cancel()
    assert rec.states()[-1] == "CANCELLED"
    assert exits == []
    s.close(trash_if_empty=False)
    assert exits == [True]


def test_close_cancels_in_flight_and_exits_once(make):
    s, rec, exits = make(short=True)
    s.start(None, [])
    run_until(lambda: "RECORDING" in rec.states(), what="RECORDING")
    s.close(trash_if_empty=True)
    assert rec.states()[-1] == "CANCELLED"
    assert s.closed
    s.close(trash_if_empty=True)
    assert exits == [True]


def test_context_lists_loaded_sources(make, tmp_path):
    (tmp_path / "WHISPER.txt").write_text("Glossary: Supabase")
    g = tmp_path / "Slack.txt"
    g.write_text("sev-2")
    s, rec, _ = make(short=True)
    s.start(str(tmp_path), [str(g)])
    assert rec.events[0] == {
        "type": "context",
        "sources": [
            {"path": str(tmp_path / "WHISPER.txt"), "content": "Glossary: Supabase"},
            {"path": str(g), "content": "sev-2"},
        ],
    }
