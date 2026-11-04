"""Keep stdout for protocol lines only.

A stray ``print`` or a library writing to stdout would corrupt the JSON
Lines stream the frontend parses. ``claim_stdout`` moves the real stdout to
a private fd for the protocol writer and points fd 1 at stderr, so anything
else that writes to "stdout" lands on stderr instead. Stdlib only: it runs
before any other import.
"""

import os


def claim_stdout() -> int:
    """Return a private fd for protocol output; fd 1 becomes stderr."""
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    return proto_fd
