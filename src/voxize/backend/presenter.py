"""Pipeline progress and transcript stages — the backend's view logic.

Moved out of ui.py so every frontend draws the same bar and shows the same
text without re-deriving them. Pure: no GLib, no I/O. Time comes from an
injected clock (integer microseconds, monotonic) so tests can drive it.

The session calls the event methods as the pipeline moves, ``tick()`` every
50 ms and ``check_stall()`` every 250 ms while working, and sends
``fraction``/``stalled`` to the frontend when they change.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Fitted by contrib/calibrate-progress-bar.py — paste new values here.
_BATCH_LIVE_RATIO = 0.9843
_CLEANUP_BATCH_RATIO = 0.9167
_MIN_LIVE_LEN = 50

_UPLOAD_MS = 50.0
_BATCH_TTFB_BASE_MS = 1196.7
_BATCH_TTFB_SLOPE_MS = 1.3181
_BATCH_STREAM_MS_PER_CHAR = 1.1337
_CLEANUP_TTFB_MS = 1264.0
_CLEANUP_STREAM_MS_PER_CHAR = 1.4499

# "Still working…" once progress has been flat this long.
_STALL_US = 3_000_000


def monotonic_us() -> int:
    return time.monotonic_ns() // 1000


class Pipeline:
    """Progress estimate for upload → batch → cleanup, and stall detection.

    ``fraction`` is what a frontend draws. Before ``arm()`` it is 0.0, as the
    bar always started. Once armed without a prediction (live text under 50
    chars) it is ``None``, meaning "draw an indeterminate bar". Otherwise it
    is the rail position in [0, 1], which never moves backwards.
    """

    def __init__(self, clock: Callable[[], int] = monotonic_us) -> None:
        self._clock = clock
        self._watching = False
        self._stalled = False
        self._last_progress_us = 0
        self._clear()

    def _clear(self) -> None:
        self._armed = False
        self._active = False
        self._predicted = 0.0
        self._batch_predicted = 0.0
        self._chars = 0
        self._seg_upload = 0.0
        self._seg_batch_wait = 0.0
        self._seg_batch_stream = 0.0
        self._seg_cleanup_wait = 0.0
        self._seg_cleanup_stream = 0.0
        self._batch_ttfb_ms = 0.0
        self._cleanup_ttfb_ms = 0.0
        self._d_upload = 0.0
        self._d_batch_ttfb = 0.0
        self._d_batch_stream = 0.0
        self._d_cleanup_ttfb = 0.0
        self._d_cleanup_stream = 0.0
        self._pl_start_us = 0
        self._pl_upload_done_us = 0
        self._pl_batch_first_us = 0
        self._pl_batch_done_us = 0
        self._pl_cleanup_first_us = 0
        # (from_fraction, to_fraction, expected_ms, start_us) of a TTFB gap
        self._wait: tuple[float, float, float, int] | None = None
        # Upload can finish before arm() runs (it waits on the WS teardown);
        # remember when, so the arm can catch the bar up.
        self._upload_done_early_us = 0
        self._rail = 0.0

    # ── Read side ──

    @property
    def fraction(self) -> float | None:
        if self._armed and not self._active:
            return None
        return self._rail

    @property
    def stalled(self) -> bool:
        return self._stalled

    # ── Lifecycle ──

    def begin(self) -> None:
        """Entering TRANSCRIBING: a fresh rail, and the stall watch starts."""
        self._clear()
        self._stalled = False
        self._watching = True
        self._mark_progress()

    def stop(self) -> None:
        """Leaving the working states (READY, CANCELLED, ERROR)."""
        self._watching = False
        self._stalled = False
        self._wait = None

    # ── Pipeline events ──

    def arm(self, live_len: int) -> None:
        """Predict the whole post-recording pipeline from the live-preview length."""
        self._mark_progress()
        self._armed = True
        self._active = live_len >= _MIN_LIVE_LEN
        self._predicted = live_len * _BATCH_LIVE_RATIO if self._active else 0.0
        self._batch_predicted = 0.0
        self._chars = 0
        if not self._active:
            # No prediction to map events onto — the frontend pulses.
            logger.debug("pipeline_progress: start live_len=%d pulse", live_len)
            return
        d_upload = _UPLOAD_MS + 0.05 * live_len
        d_batch_ttfb = _BATCH_TTFB_BASE_MS + _BATCH_TTFB_SLOPE_MS * live_len
        d_batch_stream = _BATCH_STREAM_MS_PER_CHAR * live_len
        d_cleanup_ttfb = _CLEANUP_TTFB_MS
        d_cleanup_stream = _CLEANUP_STREAM_MS_PER_CHAR * live_len
        total = (
            d_upload + d_batch_ttfb + d_batch_stream + d_cleanup_ttfb + d_cleanup_stream
        )
        self._seg_upload = d_upload / total
        self._seg_batch_wait = self._seg_upload + d_batch_ttfb / total
        self._seg_batch_stream = self._seg_batch_wait + d_batch_stream / total
        self._seg_cleanup_wait = self._seg_batch_stream + d_cleanup_ttfb / total
        self._seg_cleanup_stream = min(
            self._seg_cleanup_wait + d_cleanup_stream / total, 0.95
        )
        self._batch_ttfb_ms = d_batch_ttfb
        self._cleanup_ttfb_ms = d_cleanup_ttfb
        self._d_upload = d_upload
        self._d_batch_ttfb = d_batch_ttfb
        self._d_batch_stream = d_batch_stream
        self._d_cleanup_ttfb = d_cleanup_ttfb
        self._d_cleanup_stream = d_cleanup_stream
        self._pl_start_us = self._clock()
        self._pl_upload_done_us = 0
        self._pl_batch_first_us = 0
        self._pl_batch_done_us = 0
        self._pl_cleanup_first_us = 0
        logger.debug(
            "pipeline_progress: start live_len=%d "
            "predicted_ms=[upload=%.0f batch_ttfb=%.0f batch_stream=%.0f "
            "cleanup_ttfb=%.0f cleanup_stream=%.0f total=%.0f] "
            "segs=[%.3f %.3f %.3f %.3f %.3f] "
            "constants=[bl_ratio=%.4f cb_ratio=%.4f bt_base=%.1f bt_slope=%.4f "
            "bs_rate=%.4f ct_flat=%.1f cs_rate=%.4f]",
            live_len,
            d_upload,
            d_batch_ttfb,
            d_batch_stream,
            d_cleanup_ttfb,
            d_cleanup_stream,
            total,
            self._seg_upload,
            self._seg_batch_wait,
            self._seg_batch_stream,
            self._seg_cleanup_wait,
            self._seg_cleanup_stream,
            _BATCH_LIVE_RATIO,
            _CLEANUP_BATCH_RATIO,
            _BATCH_TTFB_BASE_MS,
            _BATCH_TTFB_SLOPE_MS,
            _BATCH_STREAM_MS_PER_CHAR,
            _CLEANUP_TTFB_MS,
            _CLEANUP_STREAM_MS_PER_CHAR,
        )
        self._rail = 0.0
        if self._upload_done_early_us:
            # Upload finished before the arm: fill its segment and run the
            # TTFB wait from when it really ended, so the bar catches up.
            self._pl_upload_done_us = self._upload_done_early_us
            self._rail_set(self._seg_upload)
            self._start_wait(
                self._seg_upload,
                self._seg_batch_wait,
                self._batch_ttfb_ms,
                start_us=self._upload_done_early_us,
            )

    def upload(self, fraction: float) -> None:
        """Upload byte progress [0, 1] → mapped to [0, seg_upload] of the pipeline."""
        if not self._active:
            self._mark_progress()
            if fraction >= 1.0 and not self._upload_done_early_us:
                self._upload_done_early_us = self._clock()
            return
        self._rail_set(min(fraction, 1.0) * self._seg_upload)
        if fraction >= 1.0:
            now = self._clock()
            self._pl_upload_done_us = now
            actual_ms = (now - self._pl_start_us) / 1000
            logger.debug(
                "pipeline_progress: upload_done actual_ms=%.0f predicted_ms=%.0f",
                actual_ms,
                self._d_upload,
            )
            self._start_wait(
                self._seg_upload, self._seg_batch_wait, self._batch_ttfb_ms
            )

    def batch_first_delta(self) -> None:
        """First batch delta — end the TTFB wait, start char tracking."""
        if not self._active:
            return
        self._wait = None
        self._chars = 0
        self._rail_set(self._seg_batch_wait)
        now = self._clock()
        self._pl_batch_first_us = now
        ref = self._pl_upload_done_us or self._pl_start_us
        logger.debug(
            "pipeline_progress: batch_first_delta "
            "actual_ttfb_ms=%.0f predicted_ttfb_ms=%.0f",
            (now - ref) / 1000,
            self._d_batch_ttfb,
        )

    def delta(self, text: str, stage: str) -> None:
        """A streaming delta of ``stage`` ("batch" or "cleaned")."""
        if not self._active:
            self._mark_progress()
            return
        self._chars += len(text)
        if stage == "batch" and self._predicted > 0:
            t = min(self._chars / self._predicted, 1.0)
            seg_start = self._seg_batch_wait
            seg_end = self._seg_batch_stream
        elif stage == "cleaned" and self._batch_predicted > 0:
            t = min(self._chars / self._batch_predicted, 1.0)
            seg_start = self._seg_cleanup_wait
            seg_end = self._seg_cleanup_stream
        else:
            # No prediction for this pass (batch text < 50 chars): the bar
            # holds, but tokens arriving is still not a stall.
            self._mark_progress()
            return
        frac = seg_start + t * (seg_end - seg_start)
        self._rail_set(min(frac, seg_end))

    def batch_done(self, batch_len: int) -> None:
        """Batch done — snap to segment boundary, arm cleanup, start wait."""
        if not self._active:
            self._mark_progress()
            return
        self._wait = None
        self._rail_set(self._seg_batch_stream)
        now = self._clock()
        self._pl_batch_done_us = now
        ref = self._pl_batch_first_us or self._pl_start_us
        logger.debug(
            "pipeline_progress: batch_done "
            "actual_stream_ms=%.0f predicted_stream_ms=%.0f "
            "chars=%d predicted_chars=%.0f batch_len=%d",
            (now - ref) / 1000,
            self._d_batch_stream,
            self._chars,
            self._predicted,
            batch_len,
        )
        self._batch_predicted = (
            batch_len * _CLEANUP_BATCH_RATIO if batch_len >= _MIN_LIVE_LEN else 0.0
        )
        self._chars = 0
        if self._batch_predicted > 0:
            self._start_wait(
                self._seg_batch_stream, self._seg_cleanup_wait, self._cleanup_ttfb_ms
            )

    def cleanup_first_delta(self) -> None:
        """First cleanup delta — end the TTFB wait, start char tracking."""
        if not self._active:
            return
        self._wait = None
        self._chars = 0
        self._rail_set(self._seg_cleanup_wait)
        now = self._clock()
        self._pl_cleanup_first_us = now
        ref = self._pl_batch_done_us or self._pl_start_us
        logger.debug(
            "pipeline_progress: cleanup_first_delta "
            "actual_ttfb_ms=%.0f predicted_ttfb_ms=%.0f",
            (now - ref) / 1000,
            self._d_cleanup_ttfb,
        )

    def finish(self) -> None:
        """Snap to 100%."""
        if not self._active:
            return
        self._wait = None
        self._rail_set(1.0)
        now = self._clock()
        ref = self._pl_cleanup_first_us or self._pl_start_us
        logger.debug(
            "pipeline_progress: done "
            "cleanup_stream_ms=%.0f predicted_cleanup_stream_ms=%.0f "
            "cleanup_chars=%d predicted_cleanup_chars=%.0f "
            "total_ms=%.0f predicted_total_ms=%.0f",
            (now - ref) / 1000,
            self._d_cleanup_stream,
            self._chars,
            self._batch_predicted,
            (now - self._pl_start_us) / 1000,
            self._d_upload
            + self._d_batch_ttfb
            + self._d_batch_stream
            + self._d_cleanup_ttfb
            + self._d_cleanup_stream,
        )

    # ── Timers (driven by the session) ──

    def tick(self) -> None:
        """Advance a TTFB wait. Called every 50 ms while working."""
        if self._wait is None:
            return
        from_frac, to_frac, expected_ms, start_us = self._wait
        elapsed_ms = (self._clock() - start_us) / 1000
        t = min(elapsed_ms / expected_ms, 1.0)
        self._rail_set(from_frac + t * (to_frac - from_frac))
        if t >= 1.0:
            self._wait = None

    def check_stall(self) -> bool:
        """Called every 250 ms while working. True once flat for 3 s."""
        if not self._watching:
            return False
        stalled = self._clock() - self._last_progress_us >= _STALL_US
        if stalled != self._stalled:
            self._stalled = stalled
            logger.debug("pipeline_progress: stalled=%s", stalled)
        return stalled

    # ── Internals ──

    def _rail_set(self, fraction: float) -> None:
        """Move the rail, monotonically.

        Predictions over- and under-shoot, so a raw fraction can move
        backwards between the model and a real event. The bar is a
        promise about elapsed progress; it must never retreat.
        """
        f = max(self._rail, min(fraction, 1.0))
        if f > self._rail:
            self._mark_progress()
        self._rail = f

    def _start_wait(
        self,
        from_frac: float,
        to_frac: float,
        expected_ms: float,
        start_us: int | None = None,
    ) -> None:
        self._wait = (
            from_frac,
            to_frac,
            expected_ms,
            self._clock() if start_us is None else start_us,
        )

    def _mark_progress(self) -> None:
        """Something moved: the rail, or (no estimate) a real event."""
        self._last_progress_us = self._clock()
