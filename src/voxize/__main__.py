"""``python -m voxize`` — the GTK dictation window.

The window is a client of a separate backend process; see voxize.gui and
voxize.backend. Mock-mode env vars are documented in voxize.backend.__main__.
"""

from voxize.gui.__main__ import main

if __name__ == "__main__":
    main()
