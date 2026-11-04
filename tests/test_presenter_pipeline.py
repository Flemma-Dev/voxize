import pytest

from voxize.backend.presenter import Pipeline

# The model's constants, restated: the tests pin today's behaviour.
BATCH_LIVE_RATIO = 0.9843
CLEANUP_BATCH_RATIO = 0.9167


def segments(live_len: int) -> dict[str, float]:
    d_upload = 50.0 + 0.05 * live_len
    d_bt = 1196.7 + 1.3181 * live_len
    d_bs = 1.1337 * live_len
    d_ct = 1264.0
    d_cs = 1.4499 * live_len
    total = d_upload + d_bt + d_bs + d_ct + d_cs
    upload = d_upload / total
    batch_wait = upload + d_bt / total
    batch_stream = batch_wait + d_bs / total
    cleanup_wait = batch_stream + d_ct / total
    cleanup_stream = min(cleanup_wait + d_cs / total, 0.95)
    return {
        "upload": upload,
        "batch_wait": batch_wait,
        "batch_stream": batch_stream,
        "cleanup_wait": cleanup_wait,
        "cleanup_stream": cleanup_stream,
        "batch_ttfb_ms": d_bt,
        "cleanup_ttfb_ms": d_ct,
    }


class Clock:
    def __init__(self) -> None:
        self.us = 1_000_000

    def __call__(self) -> int:
        return self.us

    def advance_ms(self, ms: float) -> None:
        self.us += int(ms * 1000)


def armed(live_len: int = 1000):
    clock = Clock()
    p = Pipeline(clock)
    p.begin()
    p.arm(live_len)
    return p, clock


def test_fraction_is_zero_before_arm():
    p = Pipeline(Clock())
    p.begin()
    assert p.fraction == 0.0


def test_short_live_text_has_no_estimate():
    p, _ = armed(49)
    assert p.fraction is None
    p.upload(1.0)
    p.delta("x" * 100, "batch")
    p.finish()
    assert p.fraction is None


def test_upload_fills_its_segment():
    p, _ = armed()
    p.upload(0.5)
    assert p.fraction == pytest.approx(segments(1000)["upload"] * 0.5)
    p.upload(1.0)
    assert p.fraction == pytest.approx(segments(1000)["upload"])


def test_ttfb_wait_interpolates_and_stops_at_its_target():
    p, clock = armed()
    seg = segments(1000)
    p.upload(1.0)
    clock.advance_ms(seg["batch_ttfb_ms"] / 2)
    p.tick()
    midpoint = (seg["upload"] + seg["batch_wait"]) / 2
    assert p.fraction == pytest.approx(midpoint, rel=1e-3)
    clock.advance_ms(seg["batch_ttfb_ms"] * 5)
    p.tick()
    p.tick()
    assert p.fraction == pytest.approx(seg["batch_wait"])


def test_upload_done_before_arm_catches_the_bar_up():
    clock = Clock()
    p = Pipeline(clock)
    p.begin()
    p.upload(1.0)  # not armed yet: remembered
    clock.advance_ms(1000)
    p.arm(1000)
    seg = segments(1000)
    assert p.fraction == pytest.approx(seg["upload"])
    p.tick()  # the wait runs from when the upload really ended
    t = 1000 / seg["batch_ttfb_ms"]
    expected = seg["upload"] + t * (seg["batch_wait"] - seg["upload"])
    assert p.fraction == pytest.approx(expected, rel=1e-3)


def test_batch_stream_maps_chars_onto_its_segment():
    p, _ = armed()
    seg = segments(1000)
    p.upload(1.0)
    p.batch_first_delta()
    assert p.fraction == pytest.approx(seg["batch_wait"])
    predicted = 1000 * BATCH_LIVE_RATIO
    p.delta("x" * int(predicted / 2), "batch")
    t = int(predicted / 2) / predicted
    expected = seg["batch_wait"] + t * (seg["batch_stream"] - seg["batch_wait"])
    assert p.fraction == pytest.approx(expected)


def test_the_bar_never_moves_backwards():
    p, _ = armed()
    p.upload(1.0)
    p.batch_first_delta()
    p.delta("x" * 2000, "batch")  # overshoots the prediction
    high = p.fraction
    p.upload(0.1)  # a stale, lower reading
    p.batch_done(10)
    assert p.fraction == high


def test_cleanup_segment_and_finish():
    p, _ = armed()
    seg = segments(1000)
    p.upload(1.0)
    p.batch_first_delta()
    p.delta("x" * 984, "batch")
    p.batch_done(900)
    assert p.fraction == pytest.approx(seg["batch_stream"])
    p.cleanup_first_delta()
    assert p.fraction == pytest.approx(seg["cleanup_wait"])
    p.delta("x" * int(900 * CLEANUP_BATCH_RATIO), "cleaned")
    # int() truncates the char count, so t is a hair under 1.
    assert p.fraction == pytest.approx(seg["cleanup_stream"], rel=1e-3)
    assert p.fraction <= 0.95
    p.finish()
    assert p.fraction == 1.0


def test_short_batch_has_no_cleanup_prediction():
    p, _ = armed()
    p.upload(1.0)
    p.batch_first_delta()
    p.batch_done(49)
    before = p.fraction
    p.cleanup_first_delta()
    p.delta("x" * 500, "cleaned")
    # Only the cleanup-first snap moves it; cleaned chars map onto nothing.
    assert p.fraction == pytest.approx(max(before, segments(1000)["cleanup_wait"]))


def test_stall_after_three_seconds_without_progress():
    p, clock = armed()
    clock.advance_ms(2999)
    assert p.check_stall() is False
    clock.advance_ms(1)
    assert p.check_stall() is True
    assert p.stalled is True
    p.upload(0.5)  # the bar moved
    assert p.check_stall() is False


def test_events_without_an_estimate_still_count_as_progress():
    p, clock = armed(10)
    clock.advance_ms(2900)
    p.delta("word", "batch")
    clock.advance_ms(2900)
    assert p.check_stall() is False


def test_stop_ends_the_stall_watch():
    p, clock = armed()
    clock.advance_ms(5000)
    assert p.check_stall() is True
    p.stop()
    assert p.stalled is False
    assert p.check_stall() is False


def test_begin_resets_everything():
    p, _ = armed()
    p.upload(1.0)
    p.begin()
    assert p.fraction == 0.0
