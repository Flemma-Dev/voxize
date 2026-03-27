"""Detect transcription prompt from the focused window's working directory.

Best-effort chain:
1. Get focused window PID via D-Bus (GNOME Shell extension).
2. Resolve the actual working directory:
   - Ghostty + tmux → nvim: read /proc/{nvim_pid}/cwd
   - Ghostty + tmux → other: tmux pane_current_path
   - Other: /proc/{pid}/cwd
3. Read WHISPER.txt from the resolved directory if it exists.

Every step is wrapped in try/except — this feature must never block startup
or raise. Returns None if anything fails.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

_WHISPER_FILE = "WHISPER.txt"


def detect_prompt() -> tuple[str | None, str | None]:
    """Detect transcription prompt from the focused window's project directory.

    Returns:
        (cwd, prompt) — both may be None if detection fails at any step.
    """
    cwd = _detect_cwd()
    if not cwd:
        return None, None

    prompt = _read_whisper(cwd)
    return cwd, prompt


def _detect_cwd() -> str | None:
    """Resolve the focused window's actual working directory."""
    pid = _get_focused_window_pid()
    if not pid:
        return None

    cmdline = _read_cmdline(pid)
    if not cmdline:
        # Fall back to /proc/{pid}/cwd
        return _read_proc_cwd(pid)

    logger.debug("prompt: focused pid=%d cmdline=%s", pid, cmdline[:120])

    # Ghostty + tmux — delve into the tmux pane
    if "/ghostty" in cmdline and "/tmux" in cmdline:
        logger.debug("prompt: Ghostty+tmux detected")
        return _resolve_tmux_cwd()

    # Default: /proc/{pid}/cwd
    return _read_proc_cwd(pid)


def _get_focused_window_pid() -> int | None:
    """Get the PID of the focused window via GNOME Shell D-Bus extension."""
    try:
        from gi.repository import Gio

        proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.gnome.Shell",
            "/org/gnome/Shell/Extensions/Windows",
            "org.gnome.Shell.Extensions.Windows",
            None,
        )
        result = proxy.call_sync(
            "List",
            None,
            Gio.DBusCallFlags.NONE,
            1000,  # 1s timeout
            None,
        )

        # Result is a GVariant tuple: (json_string,)
        json_str = result.get_child_value(0).get_string()
        windows = json.loads(json_str)

        for win in windows:
            if win.get("focus"):
                pid = win.get("pid")
                if pid:
                    logger.debug("prompt: focused window pid=%d", pid)
                    return int(pid)

        logger.debug("prompt: no focused window found in D-Bus response")
    except Exception:
        logger.debug("prompt: D-Bus focused window lookup failed", exc_info=True)
    return None


def _read_cmdline(pid: int) -> str | None:
    """Read /proc/{pid}/cmdline, returning space-joined args or None."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except Exception:
        return None


def _read_proc_cwd(pid: int) -> str | None:
    """Read /proc/{pid}/cwd symlink."""
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        if os.path.isdir(cwd):
            logger.debug("prompt: /proc/%d/cwd=%s", pid, cwd)
            return cwd
    except Exception:
        pass
    return None


def _resolve_tmux_cwd() -> str | None:
    """Resolve CWD from the active tmux pane, diving into nvim if running."""
    try:
        pane_cmd = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_current_command}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if pane_cmd.returncode != 0:
            logger.debug("prompt: tmux display-message failed: %s", pane_cmd.stderr)
            return None

        command = pane_cmd.stdout.strip()
        logger.debug("prompt: tmux pane command=%s", command)

        if command == "nvim":
            return _resolve_nvim_cwd()

        # Not nvim — use tmux pane's current path
        pane_path = subprocess.run(
            ["tmux", "display-message", "-p", "-F", "#{pane_current_path}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if pane_path.returncode == 0:
            path = pane_path.stdout.strip()
            if path and os.path.isdir(path):
                logger.debug("prompt: tmux pane path=%s", path)
                return path

    except Exception:
        logger.debug("prompt: tmux resolution failed", exc_info=True)
    return None


def _resolve_nvim_cwd() -> str | None:
    """Find nvim's PID in the active tmux pane and read its /proc/cwd."""
    try:
        # Get the pane's TTY
        tty_result = subprocess.run(
            ["tmux", "display-message", "-p", "-F", "#{pane_tty}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if tty_result.returncode != 0:
            return None

        tty = tty_result.stdout.strip()
        # Strip /dev/ prefix for pgrep -t
        tty_short = tty.removeprefix("/dev/")
        logger.debug("prompt: pane tty=%s", tty_short)

        # Find nvim PID on this TTY
        pgrep_result = subprocess.run(
            ["pgrep", "-t", tty_short, "nvim"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if pgrep_result.returncode != 0 or not pgrep_result.stdout.strip():
            logger.debug("prompt: no nvim PID found on tty %s", tty_short)
            return None

        # pgrep may return multiple PIDs — take the first
        nvim_pid = int(pgrep_result.stdout.strip().splitlines()[0])
        logger.debug("prompt: nvim pid=%d", nvim_pid)
        return _read_proc_cwd(nvim_pid)

    except Exception:
        logger.debug("prompt: nvim resolution failed", exc_info=True)
    return None


def _read_whisper(cwd: str) -> str | None:
    """Read WHISPER.txt from the given directory, return content or None."""
    path = os.path.join(cwd, _WHISPER_FILE)
    try:
        if not os.path.isfile(path):
            logger.debug("prompt: no %s in %s", _WHISPER_FILE, cwd)
            return None

        with open(path) as f:
            content = f.read().strip()
        if not content:
            logger.debug("prompt: %s is empty", path)
            return None

        # Collapse newlines to spaces (matches old record.sh behavior)
        content = " ".join(content.split())
        logger.debug("prompt: loaded %s (%d chars)", path, len(content))
        return content

    except Exception:
        logger.debug("prompt: failed to read %s", path, exc_info=True)
    return None
