"""Meeting session discovery and inspection.

Scans the Voxize state directory for meeting session directories
(those with a ``-meeting`` suffix) and reports what files exist in
each. Used by the welcome screen to list sessions and by the process
app to inspect a single session's state.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime

from voxize.meeting.transcribe import TranscribeParams
from voxize.storage import state_dir

logger = logging.getLogger(__name__)

_NAME_FORMAT = "%Y-%m-%dT%H-%M-%S"
_TIMESTAMP_LEN = 19
_BUCKET_SUFFIX = "-meeting"


@dataclass(frozen=True)
class MeetingSession:
    path: str
    name: str
    created: datetime
    has_opus: bool
    has_transcript: bool
    duration_s: float | None
    file_size_bytes: int
    title: str = ""


def list_meeting_sessions() -> list[MeetingSession]:
    """Discover all meeting sessions, newest first."""
    base = state_dir()
    if not os.path.isdir(base):
        return []
    sessions: list[MeetingSession] = []
    for name in os.listdir(base):
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        if not _is_meeting_dir(name):
            continue
        sessions.append(inspect_session(path))
    sessions.sort(key=lambda s: s.created, reverse=True)
    return sessions


def inspect_session(session_dir: str) -> MeetingSession:
    """Build a MeetingSession for a single directory."""
    name = os.path.basename(session_dir)
    created = _parse_timestamp(name)
    opus_path = os.path.join(session_dir, "recording.opus")
    has_opus = os.path.isfile(opus_path)
    has_transcript = os.path.isfile(os.path.join(session_dir, "transcript.txt"))

    duration_s = None
    file_size_bytes = 0
    if has_opus:
        with contextlib.suppress(OSError):
            file_size_bytes = os.path.getsize(opus_path)
        duration_s = _probe_duration(opus_path)

    title = load_title(session_dir)

    return MeetingSession(
        path=session_dir,
        name=name,
        created=created,
        has_opus=has_opus,
        has_transcript=has_transcript,
        duration_s=duration_s,
        file_size_bytes=file_size_bytes,
        title=title,
    )


def load_title(session_dir: str) -> str:
    path = os.path.join(session_dir, "title.txt")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def save_title(session_dir: str, title: str) -> None:
    path = os.path.join(session_dir, "title.txt")
    text = title.strip()
    if not text:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text + "\n")
    os.replace(tmp, path)


def load_transcribe_params(session_dir: str) -> TranscribeParams | None:
    """Load transcribe_params.json if it exists, else None."""
    path = os.path.join(session_dir, "transcribe_params.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return TranscribeParams(
            num_speakers=data.get("num_speakers", 2),
            keyterms=data.get("keyterms", []),
            language_code=data.get("language_code", "eng"),
        )
    except Exception:
        logger.debug("load_transcribe_params: failed to parse %s", path, exc_info=True)
        return None


# ── Internals ──


def _is_meeting_dir(name: str) -> bool:
    if len(name) < _TIMESTAMP_LEN + len(_BUCKET_SUFFIX):
        return False
    if not name.endswith(_BUCKET_SUFFIX):
        return False
    return _parse_timestamp(name) is not None


def _parse_timestamp(name: str) -> datetime | None:
    try:
        return datetime.strptime(name[:_TIMESTAMP_LEN], _NAME_FORMAT)
    except (ValueError, IndexError):
        return None


def _probe_duration(path: str) -> float | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        logger.debug("_probe_duration: ffprobe failed", exc_info=True)
        return None
    text = result.stdout.strip()
    try:
        return float(text)
    except ValueError:
        logger.debug("_probe_duration: could not parse %r", text)
        return None
