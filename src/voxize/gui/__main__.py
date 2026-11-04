"""Voxize GTK frontend entry point.

The backend is spawned before anything else — before GTK is imported — so
its sounddevice preload overlaps this process's GTK/libadwaita import.
VOXIZE_MOCK* and VOXIZE_TRACE reach it by inheritance.
"""

import os
import subprocess
import sys

from voxize import _trace

_trace.set_tag("gui")
_trace.trace("__main__ start (after interpreter + site)")
if os.environ.get("VOXIZE_TRACE"):
    _trace.export_origin()  # the backend's t=0 becomes ours

BACKEND = subprocess.Popen(
    [sys.executable, "-m", "voxize.backend"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
_trace.trace("backend spawned")

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk  # noqa: F401 — availability check
except (ImportError, ValueError) as e:
    print(
        f"Voxize: GTK4/libadwaita not available: {e}\n\n"
        "Ensure you are running inside the nix dev shell (nix develop).",
        file=sys.stderr,
    )
    BACKEND.kill()
    sys.exit(1)

from voxize import config  # noqa: E402
from voxize.gui.app import VoxizeApp  # noqa: E402

_trace.trace("imported voxize.gui.app")

# Load user config exactly once, before any module reads it.
config.load()
_trace.trace("config.load() complete")


def main() -> None:
    app = VoxizeApp(BACKEND)
    _trace.trace("VoxizeApp() constructed; entering app.run()")
    app.run([])
    _trace.trace("app.run() returned (process exit)")


if __name__ == "__main__":
    main()
