"""Microphone lock — ensures only one Voxize instance records at a time.

Uses fcntl.flock() on $XDG_RUNTIME_DIR/voxize-mic.lock by default. Advisory
lock is released automatically by the OS if the process crashes, so there
is no stale lock problem. The meeting recorder uses a distinct lock name
(voxize-meeting.lock) so a meeting can run alongside dictation.
"""

import fcntl
import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_LOCK_NAME = "voxize-mic.lock"


class MicLockError(Exception):
    """Raised when the mic lock cannot be acquired."""


class MicLock:
    """Advisory file lock for microphone exclusion."""

    def __init__(self, lock_name: str = _DEFAULT_LOCK_NAME) -> None:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime_dir:
            raise MicLockError("$XDG_RUNTIME_DIR is not set")
        self._path = os.path.join(runtime_dir, lock_name)
        self._fd = None

    def acquire(self) -> None:
        """Acquire the mic lock. Raises MicLockError if another instance holds it."""
        logger.debug("acquire: path=%s", self._path)
        self._fd = open(self._path, "w")  # noqa: SIM115
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fd.close()
            self._fd = None
            raise MicLockError("Another Voxize instance is recording") from exc
        self._fd.write(str(os.getpid()))
        self._fd.flush()
        logger.debug("acquire: success pid=%d", os.getpid())

    def release(self) -> None:
        """Release the mic lock."""
        logger.debug("release: path=%s", self._path)
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
            except OSError:
                pass
            try:  # noqa: SIM105
                os.unlink(self._path)
            except OSError:
                pass
            self._fd = None
