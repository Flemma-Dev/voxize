"""Opt-in startup phase tracing. Enable with VOXIZE_TRACE=1.

Prints ``[voxize-trace] t+<total>ms (+<delta>ms) <label>`` to stderr (or to
a sink, see ``set_sink``).

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
from collections.abc import Callable

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
_SINK: Callable[[str], None] | None = None


def set_tag(tag: str) -> None:
    """Prefix this process's trace lines, e.g. ``[gui]`` or ``[backend]``."""
    global _TAG
    _TAG = f"[{tag}] "


def export_origin() -> None:
    """Make child processes measure from our t=0 (via VOXIZE_TRACE_T0)."""
    os.environ["VOXIZE_TRACE_T0"] = repr(time.time() - (time.monotonic() - _T0))


def set_sink(sink: Callable[[str], None] | None) -> None:
    """Send trace lines to ``sink`` instead of stderr (None: back to stderr).

    The terminal client sets one once Textual owns the terminal: stderr is
    then the screen, and a trace line would draw over it.
    """
    global _SINK
    _SINK = sink


def trace(label: str) -> None:
    global _PREV
    if not _ENABLED:
        return
    now = time.monotonic()
    total_ms = (now - _T0) * 1000
    delta_ms = (now - _PREV) * 1000
    _PREV = now
    line = f"[voxize-trace] {_TAG}t+{total_ms:8.1f}ms  (+{delta_ms:7.1f}ms)  {label}"
    if _SINK is not None:
        _SINK(line)
    else:
        print(line, file=sys.stderr, flush=True)
