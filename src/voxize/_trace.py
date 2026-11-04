"""Opt-in startup phase tracing. Enable with VOXIZE_TRACE=1.

Prints ``[voxize-trace] t+<total>ms (+<delta>ms) <label>`` to stderr.

``t`` is measured from interpreter start by default. If the shell sets
``VOXIZE_TRACE_T0=$EPOCHREALTIME`` (bash built-in, seconds.microseconds)
before invoking, ``t=0`` aligns with that wall-clock moment — so the
first trace line reveals how long ``uv`` / Python interpreter / site
setup took before our code ran.
"""

from __future__ import annotations

import os
import sys
import time

_ENABLED = bool(os.environ.get("VOXIZE_TRACE"))

_t0_env = os.environ.get("VOXIZE_TRACE_T0")
if _t0_env:
    try:
        _T0_WALL = float(_t0_env)  # shell-supplied epoch seconds
        # Map wall-clock origin onto monotonic by subtracting the elapsed
        # wall-clock delta from the current monotonic reading.
        _T0 = time.monotonic() - (time.time() - _T0_WALL)
    except ValueError:
        _T0 = time.monotonic()
else:
    _T0 = time.monotonic()

_PREV = _T0
_TAG = ""


def set_tag(tag: str) -> None:
    """Prefix this process's trace lines, e.g. ``[gui]`` or ``[backend]``."""
    global _TAG
    _TAG = f"[{tag}] "


def export_origin() -> None:
    """Make child processes measure from our t=0 (via VOXIZE_TRACE_T0)."""
    os.environ["VOXIZE_TRACE_T0"] = repr(time.time() - (time.monotonic() - _T0))


def trace(label: str) -> None:
    global _PREV
    if not _ENABLED:
        return
    now = time.monotonic()
    total_ms = (now - _T0) * 1000
    delta_ms = (now - _PREV) * 1000
    _PREV = now
    print(
        f"[voxize-trace] {_TAG}t+{total_ms:8.1f}ms  (+{delta_ms:7.1f}ms)  {label}",
        file=sys.stderr,
        flush=True,
    )
