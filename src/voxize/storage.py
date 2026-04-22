"""XDG state directory management for session storage."""

import logging
import os
from datetime import UTC, datetime, timedelta

from gi.repository import Gio

from voxize import config

logger = logging.getLogger(__name__)

_NAME_FORMAT = "%Y-%m-%dT%H-%M-%S"


def state_dir() -> str:
    """Return the base voxize state directory, creating it if needed."""
    base = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    path = os.path.join(base, "voxize")
    os.makedirs(path, exist_ok=True)
    return path


def create_session_dir() -> str:
    """Create and return a new session directory named by ISO timestamp."""
    now = datetime.now(UTC).astimezone()
    name = now.strftime(_NAME_FORMAT)
    path = os.path.join(state_dir(), name)
    os.makedirs(path, exist_ok=True)
    logger.debug("create_session_dir: path=%s", path)
    return path


def _parse_start_time(name: str) -> datetime | None:
    """Parse a session directory name back to a naive local datetime."""
    try:
        return datetime.strptime(name, _NAME_FORMAT)
    except ValueError:
        return None


def prune_sessions() -> None:
    """Trash session directories that exceed the configured retention limits.

    Uses ``Gio.File.trash()`` so sessions are recoverable from the system
    trash rather than permanently deleted.

    Called at termination time (not startup) so that a faulty launch that
    crashes repeatedly cannot wipe out the session history.

    Strict/OR semantics: a session is pruned if it's beyond the newest
    ``max_sessions`` entries OR older than ``max_age_days``. Either limit
    can be disabled by setting it to ``0``; setting both to ``0`` disables
    pruning entirely.

    Session dirs are named ``YYYY-MM-DDTHH-MM-SS`` so lexicographic sort
    equals chronological order. Non-directory entries are ignored.
    Directories whose names don't parse as timestamps are skipped from
    the age rule but still count toward the ``max_sessions`` ranking.
    """
    cfg = config.CONFIG.storage
    max_sessions = cfg.max_sessions
    max_age_days = cfg.max_age_days

    if max_sessions == 0 and max_age_days == 0:
        logger.debug("prune_sessions: both limits disabled, skipping")
        return

    base = state_dir()
    try:
        dirs = sorted(
            (d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))),
            reverse=True,
        )
    except OSError:
        return

    cutoff = datetime.now() - timedelta(days=max_age_days) if max_age_days > 0 else None

    to_prune: list[str] = []
    for index, name in enumerate(dirs):
        over_count = max_sessions > 0 and index >= max_sessions
        over_age = False
        if cutoff is not None:
            started = _parse_start_time(name)
            if started is not None and started < cutoff:
                over_age = True
        if over_count or over_age:
            to_prune.append(name)

    logger.debug(
        "prune_sessions: found=%d max_sessions=%d max_age_days=%d prune=%d",
        len(dirs),
        max_sessions,
        max_age_days,
        len(to_prune),
    )

    for name in to_prune:
        path = os.path.join(base, name)
        try:
            Gio.File.new_for_path(path).trash(None)
        except Exception:
            logger.debug("Failed to trash session %s", path)
