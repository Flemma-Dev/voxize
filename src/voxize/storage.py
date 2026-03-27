"""XDG state directory management for session storage."""

import os
from datetime import datetime, timezone


def state_dir() -> str:
    """Return the base voxize state directory, creating it if needed."""
    base = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    path = os.path.join(base, "voxize")
    os.makedirs(path, exist_ok=True)
    return path


def create_session_dir() -> str:
    """Create and return a new session directory named by ISO timestamp."""
    now = datetime.now(timezone.utc).astimezone()
    name = now.strftime("%Y-%m-%dT%H-%M-%S")
    path = os.path.join(state_dir(), name)
    os.makedirs(path, exist_ok=True)
    return path
