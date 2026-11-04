"""Where the dictation is aimed — the GNOME frontend's context detection.

A frontend skill, not a backend concern. Before our window takes focus, the
GUI asks GNOME Shell which window had it, resolves that window's working
directory (diving into tmux and nvim), and finds the per-app and
per-context glossary files for it. The backend is handed a directory and
file paths and never learns where they came from. A TUI just passes its cwd.

Moved from prompt.py (its focused-window detection helpers) with the same
resolution chain:
1. Focused window metadata via D-Bus (GNOME Shell Windows extension).
2. Working directory: Ghostty + tmux → nvim /proc/{pid}/cwd, or the tmux
   pane's current path; otherwise /proc/{pid}/cwd.
3. Per-context glossary via a title extractor (Slack channel), then the
   per-app glossary, both under $XDG_CONFIG_HOME/voxize/prompts/.

Best-effort throughout: never raises, never blocks startup for long.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable

logger = logging.getLogger(__name__)


def detect_context() -> tuple[str | None, list[str]]:
    """Return ``(cwd, glossary_paths)`` for the window focused before ours."""
    window = _get_focused_window()
    if not window:
        logger.debug("context: no focused window")
        return None, []
    cwd = _detect_cwd(window["pid"])
    glossaries: list[str] = []
    if window.get("wm_class"):
        try:
            glossaries = glossary_paths(
                window["wm_class"], window.get("title", ""), _prompts_dir()
            )
        except Exception:
            logger.debug("context: glossary resolution failed", exc_info=True)
    logger.debug("context: cwd=%s glossaries=%s", cwd, glossaries)
    return cwd, glossaries


def glossary_paths(wm_class: str, title: str, prompts_base: str) -> list[str]:
    """Context glossary first, then the app glossary — the old specificity order.

    Missing files are created empty, so the user can find and fill them;
    the backend skips empty ones.
    """
    wm_class = _sanitize_filename(wm_class)
    paths: list[str] = []
    extractor = _EXTRACTORS.get(wm_class)
    if extractor:
        context = extractor(title)
        if context:
            paths.append(
                os.path.join(
                    prompts_base, wm_class, f"{_sanitize_filename(context)}.txt"
                )
            )
        else:
            logger.debug("prompt: extractor for %s returned no context", wm_class)
    else:
        logger.debug("prompt: no extractor for wm_class=%s", wm_class)
    paths.append(os.path.join(prompts_base, f"{wm_class}.txt"))
    for path in paths:
        _ensure_file(path)
    return paths


def _ensure_file(path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w"):
                pass
            logger.debug("prompt: created empty glossary %s", path)
    except Exception:
        logger.debug("prompt: failed to create glossary %s", path, exc_info=True)


def _extract_slack_context(title: str) -> str | None:
    """Extract Slack channel or DM name from the window title.

    Titles look like:
        channel-name (Channel) - Workspace - notifications - Slack
        Person Name (DM) - Workspace - notifications - Slack
    """
    idx = title.find(" (")
    if idx <= 0:
        logger.debug("prompt: Slack extractor: no ' (' in title=%s", title[:80])
        return None
    context = title[:idx]
    logger.debug("prompt: Slack extractor: context=%s", context)
    return context


_EXTRACTORS: dict[str, Callable[[str], str | None]] = {
    "Slack": _extract_slack_context,
}


def _prompts_dir() -> str:
    """Return $XDG_CONFIG_HOME/voxize/prompts/, creating it if needed."""
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    path = os.path.join(base, "voxize", "prompts")
    os.makedirs(path, exist_ok=True)
    return path


def _sanitize_filename(name: str) -> str:
    """Sanitize an extractor-produced name for use as a filename."""
    name = name.replace("/", "-").replace("\0", "")
    name = name.lstrip(".").rstrip(".").strip()
    return name or "unknown"


def _get_focused_window() -> dict | None:
    """Get metadata for the focused window via GNOME Shell D-Bus extension."""
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
                    logger.debug(
                        "prompt: focused window pid=%d wm_class=%s title=%s",
                        pid,
                        win.get("wm_class", ""),
                        (win.get("title", ""))[:80],
                    )
                    return {
                        "pid": int(pid),
                        "wm_class": win.get("wm_class", ""),
                        "title": win.get("title", ""),
                    }

        logger.debug("prompt: no focused window found in D-Bus response")
    except Exception:
        logger.debug("prompt: D-Bus focused window lookup failed", exc_info=True)
    return None


def _detect_cwd(pid: int) -> str | None:
    """Resolve the focused window's actual working directory."""
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
