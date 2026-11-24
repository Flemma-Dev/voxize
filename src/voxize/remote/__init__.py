"""Voxize Remote: dictate on an Android client into the tmux pane its Termux shows.

The server (``voxize-remote serve``) pairs clients, takes their audio over Tailscale,
runs one Voxize backend per dictation (``python -m voxize.backend --audio-fd FD``)
and pastes the result into the pane. ``voxize-remote setup-client`` installs and
pairs the client over adb. The client's source is in ``android/``.
"""
