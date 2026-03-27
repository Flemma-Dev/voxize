"""XDG state directory management for session storage."""

import logging
import os
from datetime import UTC, datetime

from gi.repository import Gio

logger = logging.getLogger(__name__)

_MAX_SESSIONS = 8


def state_dir() -> str:
    """Return the base voxize state directory, creating it if needed."""
    base = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    path = os.path.join(base, "voxize")
    os.makedirs(path, exist_ok=True)
    return path


def create_session_dir() -> str:
    """Create and return a new session directory named by ISO timestamp."""
    now = datetime.now(UTC).astimezone()
    name = now.strftime("%Y-%m-%dT%H-%M-%S")
    path = os.path.join(state_dir(), name)
    os.makedirs(path, exist_ok=True)
    logger.debug("create_session_dir: path=%s", path)
    return path


def prune_sessions(keep: int = _MAX_SESSIONS) -> None:
    """Trash session directories beyond the most recent *keep*.

    Uses ``Gio.File.trash()`` so sessions are recoverable from the system
    trash rather than permanently deleted.

    Called at termination time (not startup) so that a faulty launch that
    crashes repeatedly cannot wipe out the session history.

    Session dirs are named ``YYYY-MM-DDTHH-MM-SS`` so lexicographic sort
    equals chronological order.  Non-directory entries are ignored.
    """
    base = state_dir()
    try:
        dirs = sorted(
            (d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))),
            reverse=True,
        )
    except OSError:
        return

    logger.debug(
        "prune_sessions: found=%d keep=%d prune=%d",
        len(dirs),
        keep,
        max(0, len(dirs) - keep),
    )

    for name in dirs[keep:]:
        path = os.path.join(base, name)
        try:
            Gio.File.new_for_path(path).trash(None)
        except Exception:
            logger.debug("Failed to trash session %s", path)
