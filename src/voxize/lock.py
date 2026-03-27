"""Microphone lock — ensures only one Voxize instance records at a time.

Uses fcntl.flock() on $XDG_RUNTIME_DIR/voxize-mic.lock. Advisory lock is
released automatically by the OS if the process crashes, so there is no
stale lock problem.
"""

import fcntl
import os

_LOCK_NAME = "voxize-mic.lock"


class MicLockError(Exception):
    """Raised when the mic lock cannot be acquired."""


class MicLock:
    """Advisory file lock for microphone exclusion."""

    def __init__(self) -> None:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime_dir:
            raise MicLockError("$XDG_RUNTIME_DIR is not set")
        self._path = os.path.join(runtime_dir, _LOCK_NAME)
        self._fd = None

    def acquire(self) -> None:
        """Acquire the mic lock. Raises MicLockError if another instance holds it."""
        self._fd = open(self._path, "w")
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fd.close()
            self._fd = None
            raise MicLockError("Another Voxize instance is recording")
        self._fd.write(str(os.getpid()))
        self._fd.flush()

    def release(self) -> None:
        """Release the mic lock."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
            except OSError:
                pass
            try:
                os.unlink(self._path)
            except OSError:
                pass
            self._fd = None
