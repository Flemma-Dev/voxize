"""Voxize Meeting entry point.

Dispatches to one of three apps based on arguments:

    python -m voxize.meeting                  # welcome screen
    python -m voxize.meeting --record         # start recording
    python -m voxize.meeting --process DIR    # post-processing workbench
"""

import argparse
import logging

from voxize import config

logger = logging.getLogger(__name__)


def main() -> None:
    config.load()

    parser = argparse.ArgumentParser(prog="voxize.meeting")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--record",
        action="store_true",
        help="start recording immediately",
    )
    group.add_argument(
        "--process",
        metavar="DIR",
        help="open the processing workbench for a session directory",
    )
    args = parser.parse_args()

    if args.record:
        from voxize.meeting.app import MeetingApp

        logger.info("voxize.meeting: starting recorder")
        app = MeetingApp()
        app.run([])
    elif args.process:
        from voxize.meeting.process_app import ProcessApp

        logger.info("voxize.meeting: starting process app for %s", args.process)
        app = ProcessApp(session_dir=args.process)
        app.run([])
    else:
        from voxize.meeting.welcome_app import WelcomeApp

        logger.info("voxize.meeting: starting welcome screen")
        app = WelcomeApp()
        app.run([])

    logger.info("voxize.meeting: exited")


if __name__ == "__main__":
    main()
