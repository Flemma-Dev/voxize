#!/usr/bin/env python3
"""Derive progress-bar constants from local session data.

Reads debug.log and live_transcript.txt from every dictation session in
~/.local/state/voxize/ and fits the duration models used by the unified
pipeline progress bar in src/voxize/ui.py.

Usage:
    python contrib/calibrate-progress-bar.py

The output is a block of Python constants you can paste into ui.py to
replace the existing values.  Re-run after extended usage to recalibrate.
"""

from __future__ import annotations

import os
import re
import statistics
import sys

STATE_DIR = os.path.expanduser("~/.local/state/voxize")


def main() -> None:
    sessions: list[dict] = []

    for name in sorted(os.listdir(STATE_DIR)):
        if "-meeting" in name:
            continue
        path = os.path.join(STATE_DIR, name)
        if not os.path.isdir(path):
            continue

        debug = os.path.join(path, "debug.log")
        live_f = os.path.join(path, "live_transcript.txt")
        if not os.path.exists(debug) or not os.path.exists(live_f):
            continue

        live_text = open(live_f).read()
        live_len = len(live_text)
        if live_len < 50:
            continue

        batch_txt = os.path.join(path, "transcription.txt")
        clean_txt = os.path.join(path, "cleaned.txt")
        if not os.path.exists(batch_txt) or not os.path.exists(clean_txt):
            continue

        batch_len = len(open(batch_txt).read())
        clean_len = len(open(clean_txt).read())
        if not batch_len or not clean_len:
            continue

        batch_ttfb = batch_stream = cleanup_ttfb = cleanup_stream = None
        cleanup_model = ""
        with open(debug) as f:
            for line in f:
                m = re.search(
                    r"voxize\.batch phase_timing: ttft_ms=(\d+) streaming_ms=(\d+)",
                    line,
                )
                if m:
                    batch_ttfb = int(m.group(1))
                    batch_stream = int(m.group(2))
                m = re.search(
                    r"voxize\.cleanup phase_timing: ttft_ms=(\d+) streaming_ms=(\d+)",
                    line,
                )
                if m:
                    cleanup_ttfb = int(m.group(1))
                    cleanup_stream = int(m.group(2))

        cleanup_log = os.path.join(path, "cleanup_events.jsonl")
        if os.path.exists(cleanup_log):
            try:
                import json
                with open(cleanup_log) as f:
                    first = json.loads(f.readline())
                cleanup_model = first.get("model", "")
            except Exception:
                pass

        if None in (batch_ttfb, batch_stream, cleanup_ttfb, cleanup_stream):
            continue

        sessions.append(
            {
                "live_len": live_len,
                "batch_len": batch_len,
                "clean_len": clean_len,
                "batch_ttfb": batch_ttfb,
                "batch_stream": batch_stream,
                "cleanup_ttfb": cleanup_ttfb,
                "cleanup_stream": cleanup_stream,
                "cleanup_model": cleanup_model,
            }
        )

    if len(sessions) < 20:
        print(f"Only {len(sessions)} complete sessions found — need at least 20.", file=sys.stderr)
        sys.exit(1)

    print(f"Sessions analysed: {len(sessions)}\n")

    # ── Character ratio models ──
    bl_ratios = [s["batch_len"] / s["live_len"] for s in sessions]
    batch_live_ratio = statistics.median(bl_ratios)
    print(f"_BATCH_LIVE_RATIO    = {batch_live_ratio:.4f}  (median batch/live chars)")

    # Filter to the most recent cleanup model — different models trim
    # different amounts (Luna removes ~8.5% more than Nano did).
    # Use the last session's model rather than the most frequent, since
    # we want the model that will be used going forward.
    models = [s["cleanup_model"] for s in sessions if s["cleanup_model"]]
    current_model = models[-1] if models else ""
    model_sessions = [s for s in sessions if s["cleanup_model"] == current_model]
    if len(model_sessions) < 20:
        model_sessions = sessions
        current_model = "all"

    cb_ratios = [s["clean_len"] / s["batch_len"] for s in model_sessions]
    cleanup_batch_ratio = statistics.median(cb_ratios)
    print(
        f"_CLEANUP_BATCH_RATIO = {cleanup_batch_ratio:.4f}"
        f"  (median cleanup/batch chars, model={current_model}, n={len(model_sessions)})"
    )
    print()

    # ── Linear regression helper ──
    def linreg(x, y):
        n = len(x)
        x_mean = sum(x) / n
        y_mean = sum(y) / n
        ss_xy = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
        ss_xx = sum((xi - x_mean) ** 2 for xi in x)
        slope = ss_xy / ss_xx if ss_xx else 0
        intercept = y_mean - slope * x_mean
        ss_tot = sum((yi - y_mean) ** 2 for yi in y)
        ss_res = sum((yi - (intercept + slope * xi)) ** 2 for xi, yi in zip(x, y))
        r2 = 1 - ss_res / ss_tot if ss_tot else 0
        return intercept, slope, r2

    # ── Duration models — use only sessions with the current cleanup model,
    # since model changes affect latency characteristics. ──
    tsessions = model_sessions

    # ── Batch TTFB model: intercept + slope × live_len ──
    x = [s["live_len"] for s in tsessions]
    y = [s["batch_ttfb"] for s in tsessions]
    bt_intercept, bt_slope, bt_r2 = linreg(x, y)

    print(f"_BATCH_TTFB_BASE_MS  = {bt_intercept:.1f}")
    print(f"_BATCH_TTFB_SLOPE_MS = {bt_slope:.4f}  (R²={bt_r2:.2f})")
    print()

    # ── Batch streaming: ms per char ──
    bs_rates = [s["batch_stream"] / s["live_len"] for s in tsessions if s["live_len"] > 0]
    bs_median = statistics.median(bs_rates)
    print(f"_BATCH_STREAM_MS_PER_CHAR = {bs_median:.4f}  (median ms/char of live_len)")
    print()

    # ── Cleanup TTFB model ──
    y = [s["cleanup_ttfb"] for s in tsessions]
    ct_intercept, ct_slope, ct_r2 = linreg(x, y)
    ct_median = statistics.median(y)

    if ct_r2 < 0.1:
        print(f"_CLEANUP_TTFB_MS = {ct_median:.1f}  (flat, R²={ct_r2:.2f})")
    else:
        print(f"_CLEANUP_TTFB_BASE_MS  = {ct_intercept:.1f}")
        print(f"_CLEANUP_TTFB_SLOPE_MS = {ct_slope:.4f}  (R²={ct_r2:.2f})")
    print()

    # ── Cleanup streaming: ms per char ──
    cs_rates = [s["cleanup_stream"] / s["live_len"] for s in tsessions if s["live_len"] > 0]
    cs_median = statistics.median(cs_rates)
    print(f"_CLEANUP_STREAM_MS_PER_CHAR = {cs_median:.4f}  (median ms/char of live_len)")
    print()

    # ── Upload estimate ──
    print(f"_UPLOAD_MS = 500.0  (fixed base; upload is byte-tracked, this sets the weight)")
    print()

    # ── Summary table ──
    print("─" * 60)
    print("Segment weight examples (% of bar for a given live_len):\n")
    print(f"{'live_len':>8} | {'upload':>7} | {'b_ttfb':>7} | {'b_strm':>7} | {'c_ttfb':>7} | {'c_strm':>7}")
    print("-" * 60)

    for ll in [100, 300, 500, 1000, 2000, 5000]:
        d_upload = 500.0 + 0.3 * ll
        d_bt = bt_intercept + bt_slope * ll
        d_bs = bs_median * ll
        d_ct = ct_median
        d_cs = cs_median * ll
        total = d_upload + d_bt + d_bs + d_ct + d_cs
        print(
            f"{ll:>8} | {d_upload/total*100:>6.1f}% | {d_bt/total*100:>6.1f}% "
            f"| {d_bs/total*100:>6.1f}% | {d_ct/total*100:>6.1f}% | {d_cs/total*100:>6.1f}%"
        )


if __name__ == "__main__":
    main()
