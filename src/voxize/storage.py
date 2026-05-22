"""XDG state directory management for session storage."""

import logging
import os
from datetime import UTC, datetime, timedelta

from gi.repository import Gio

from voxize import config

logger = logging.getLogger(__name__)

_NAME_FORMAT = "%Y-%m-%dT%H-%M-%S"
_TIMESTAMP_LEN = 19

BUCKET_DEFAULT = "default"


def state_dir() -> str:
    """Return the base voxize state directory, creating it if needed."""
    base = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    path = os.path.join(base, "voxize")
    os.makedirs(path, exist_ok=True)
    return path


def create_session_dir(suffix: str = "") -> str:
    """Create and return a new session directory named by ISO timestamp.

    ``suffix`` is appended verbatim to the timestamp (e.g. ``-meeting``)
    so that distinct flavors of session — short dictation vs. multi-hour
    meeting capture — share the same parent directory while remaining
    distinguishable for bucketed retention. See ``_bucket_for_name`` for
    the suffix-to-bucket mapping.
    """
    now = datetime.now(UTC).astimezone()
    name = now.strftime(_NAME_FORMAT) + suffix
    path = os.path.join(state_dir(), name)
    os.makedirs(path, exist_ok=True)
    logger.debug("create_session_dir: path=%s", path)
    return path


def _parse_start_time(name: str) -> datetime | None:
    """Parse a session directory name back to a naive local datetime.

    The timestamp prefix is always 19 chars (``YYYY-MM-DDTHH-MM-SS``);
    anything after that is a suffix (e.g. ``-meeting``) that determines
    the bucket. Names shorter than 19 chars or with a malformed prefix
    return ``None`` and are treated as strays — see ``_bucket_for_name``.
    """
    try:
        return datetime.strptime(name[:_TIMESTAMP_LEN], _NAME_FORMAT)
    except ValueError:
        return None


def _bucket_for_name(name: str) -> str | None:
    """Detect which bucket a session directory belongs to.

    Rules:
      - 19-char timestamp + no suffix → ``BUCKET_DEFAULT``
      - 19-char timestamp + ``-<tag>`` → bucket ``<tag>``
      - anything else (malformed name, missing dash, empty tag) → ``None``

    A ``None`` result means "stray" — the directory is left alone by all
    bucket-scoped pruning. Strays should be rare in normal use; we log
    them in ``prune_sessions`` so the user notices hand-edited names.
    """
    if _parse_start_time(name) is None:
        return None
    remainder = name[_TIMESTAMP_LEN:]
    if remainder == "":
        return BUCKET_DEFAULT
    if remainder.startswith("-") and len(remainder) > 1:
        return remainder[1:]
    return None


def prune_sessions(bucket: str = BUCKET_DEFAULT) -> None:
    """Trash sessions in ``bucket`` that exceed the bucket's retention limits.

    Uses ``Gio.File.trash()`` so sessions are recoverable from the system
    trash rather than permanently deleted.

    Each app prunes only its own bucket: dictation calls with no arg
    (default), the meeting recorder passes ``"meeting"``. Strays
    (directories whose names don't match the bucket-naming scheme) are
    never pruned and are logged once per call so the user can find them.

    Called at termination time (not startup) so that a faulty launch
    that crashes repeatedly cannot wipe out the session history.

    Strict/OR semantics: a session is pruned if it's beyond the newest
    ``max_sessions`` entries OR older than ``max_age_days``. Either limit
    can be disabled by setting it to ``0``; setting both to ``0`` disables
    pruning entirely for this bucket.

    Retention values come from ``config.CONFIG.storage.for_bucket(bucket)``
    so per-bucket overrides in ``[storage.<bucket>]`` take effect; missing
    keys inherit from the top-level ``[storage]`` defaults.
    """
    cfg = config.CONFIG.storage.for_bucket(bucket)
    max_sessions = cfg.max_sessions
    max_age_days = cfg.max_age_days

    if max_sessions == 0 and max_age_days == 0:
        logger.debug("prune_sessions[%s]: both limits disabled, skipping", bucket)
        return

    base = state_dir()
    try:
        all_dirs = sorted(
            (d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))),
            reverse=True,
        )
    except OSError:
        return

    in_bucket: list[str] = []
    strays: list[str] = []
    for name in all_dirs:
        detected = _bucket_for_name(name)
        if detected is None:
            strays.append(name)
        elif detected == bucket:
            in_bucket.append(name)

    if strays:
        logger.info(
            "prune_sessions[%s]: %d stray directory name(s) ignored: %s",
            bucket,
            len(strays),
            strays,
        )

    cutoff = datetime.now() - timedelta(days=max_age_days) if max_age_days > 0 else None

    to_prune: list[str] = []
    for index, name in enumerate(in_bucket):
        over_count = max_sessions > 0 and index >= max_sessions
        over_age = False
        if cutoff is not None:
            started = _parse_start_time(name)
            if started is not None and started < cutoff:
                over_age = True
        if over_count or over_age:
            to_prune.append(name)

    logger.debug(
        "prune_sessions[%s]: found=%d max_sessions=%d max_age_days=%d prune=%d",
        bucket,
        len(in_bucket),
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
