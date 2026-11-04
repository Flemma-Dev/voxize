"""Helpers for tests that drive GLib main-loop code or a backend subprocess."""

from __future__ import annotations

import time

from gi.repository import GLib

# Mock mode with the fixed delays shrunk, so a whole dictation takes seconds.
MOCK_ENV = {
    "VOXIZE_MOCK": "1",
    "VOXIZE_MOCK_FAST": "1",
    "VOXIZE_MOCK_TTFB": "50",
}


def run_until(pred, timeout_s: float = 15.0, what: str = "") -> None:
    """Run the default GLib main loop until ``pred()`` is true; fail on timeout."""
    loop = GLib.MainLoop()
    deadline = time.monotonic() + timeout_s

    def poll() -> bool:
        if pred() or time.monotonic() > deadline:
            loop.quit()
            return False
        return True

    GLib.timeout_add(10, poll)
    loop.run()
    assert pred(), f"timed out after {timeout_s}s waiting for {what or pred}"


class Recorder:
    """An ``emit``/``on_event`` callback that keeps every message."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, msg: dict) -> None:
        self.events.append(msg)

    def of(self, type_: str) -> list[dict]:
        return [e for e in self.events if e["type"] == type_]

    def states(self) -> list[str]:
        return [e["state"] for e in self.of("state")]
