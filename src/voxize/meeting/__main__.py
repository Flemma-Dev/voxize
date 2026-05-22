"""Voxize Meeting entry point.

Captures mic + system audio output into a stereo WAV (L=mic, R=system).
No transcription, no API calls — the WAV file is the deliverable.

Run with:
    uv run python -m voxize.meeting
"""

import logging

from voxize import config
from voxize.meeting.app import MeetingApp

logger = logging.getLogger(__name__)


def main() -> None:
    # Load user config so storage retention ([storage] in voxize.toml) is
    # honored on close. The meeting recorder doesn't read [ducking] or
    # [ui], but it shares storage with the dictation app so the retention
    # knobs apply to ``-meeting`` sessions too.
    config.load()
    logger.info("voxize.meeting: starting")
    app = MeetingApp()
    app.run([])
    logger.info("voxize.meeting: exited")


if __name__ == "__main__":
    main()
