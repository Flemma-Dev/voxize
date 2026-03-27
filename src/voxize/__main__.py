"""Voxize entry point."""

from voxize.checks import exit_on_failure

exit_on_failure()

from voxize.app import VoxizeApp


def main() -> None:
    app = VoxizeApp()
    app.run([])


main()
