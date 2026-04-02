"""Detect transcription prompts from the focused window's context.

Best-effort chain:
1. Get focused window metadata via D-Bus (GNOME Shell extension).
2. Resolve the actual working directory:
   - Ghostty + tmux → nvim: read /proc/{nvim_pid}/cwd
   - Ghostty + tmux → other: tmux pane_current_path
   - Other: /proc/{pid}/cwd
3. Read WHISPER.txt from the resolved directory if it exists.
4. Load app-specific glossary from XDG config based on wm_class.
5. Load context-specific glossary via per-app title extractors (e.g., Slack channel).

Every step is wrapped in try/except — this feature must never block startup
or raise. Returns empty list if anything fails.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_WHISPER_FILE = "WHISPER.txt"


@dataclass
class PromptSource:
    """A single vocabulary guidance source."""

    path: str  # absolute file path
    content: str  # stripped, non-empty content


# ── Per-app title extractors ──


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


# ── Public API ──


def detect_prompt() -> list[PromptSource]:
    """Detect transcription prompts from the focused window's context.

    Returns a list of PromptSource in specificity order (highest first):
    1. WHISPER.txt from the focused window's working directory
    2. Extractor context (e.g., Slack channel glossary)
    3. App-level glossary (e.g., Slack.txt)
    """
    window = _get_focused_window()
    sources: list[PromptSource] = []

    # 1. WHISPER.txt from CWD
    if window:
        cwd = _detect_cwd(window["pid"])
        if cwd:
            whisper = _read_whisper(cwd)
            if whisper:
                sources.append(whisper)

    # 2. Extractor context + 3. App glossary
    if window and window.get("wm_class"):
        try:
            wm_class = _sanitize_filename(window["wm_class"])
            title = window.get("title", "")
            prompts_base = _prompts_dir()

            context_source = _load_context_glossary(wm_class, title, prompts_base)
            app_source = _load_app_glossary(wm_class, prompts_base)

            if context_source:
                sources.append(context_source)
            if app_source:
                sources.append(app_source)
        except Exception:
            logger.debug("prompt: glossary loading failed", exc_info=True)

    logger.debug(
        "prompt: detect_prompt returning %d source(s): %s",
        len(sources),
        [s.path for s in sources],
    )
    return sources


# ── XDG glossary files ──


def _prompts_dir() -> str:
    """Return $XDG_CONFIG_HOME/voxize/prompts/, creating it if needed."""
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    path = os.path.join(base, "voxize", "prompts")
    os.makedirs(path, exist_ok=True)
    return path


def _ensure_and_load(path: str) -> PromptSource | None:
    """Create file if missing, read and strip, return PromptSource or None."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w") as f:
                pass
            logger.debug("prompt: created empty glossary %s", path)
        with open(path) as f:
            content = f.read().strip()
        if not content:
            logger.debug("prompt: glossary %s is empty", path)
            return None
        logger.debug("prompt: loaded glossary %s (%d chars)", path, len(content))
        return PromptSource(path=path, content=content)
    except Exception:
        logger.debug("prompt: failed to load glossary %s", path, exc_info=True)
    return None


def _load_app_glossary(wm_class: str, prompts_base: str) -> PromptSource | None:
    """Load the app-level glossary file for a given wm_class."""
    path = os.path.join(prompts_base, f"{wm_class}.txt")
    return _ensure_and_load(path)


def _sanitize_filename(name: str) -> str:
    """Sanitize an extractor-produced name for use as a filename."""
    name = name.replace("/", "-").replace("\0", "")
    name = name.lstrip(".").rstrip(".").strip()
    return name or "unknown"


def _load_context_glossary(
    wm_class: str, title: str, prompts_base: str
) -> PromptSource | None:
    """Run the title extractor for wm_class, load the context glossary file."""
    extractor = _EXTRACTORS.get(wm_class)
    if not extractor:
        logger.debug("prompt: no extractor for wm_class=%s", wm_class)
        return None
    context = extractor(title)
    if not context:
        logger.debug("prompt: extractor for %s returned no context", wm_class)
        return None
    context = _sanitize_filename(context)
    path = os.path.join(prompts_base, wm_class, f"{context}.txt")
    return _ensure_and_load(path)


# ── Focused window detection ──


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


# ── CWD resolution ──


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


# ── WHISPER.txt ──


def _read_whisper(cwd: str) -> PromptSource | None:
    """Read WHISPER.txt from the given directory, return PromptSource or None."""
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
        return PromptSource(path=path, content=content)

    except Exception:
        logger.debug("prompt: failed to read %s", path, exc_info=True)
    return None
