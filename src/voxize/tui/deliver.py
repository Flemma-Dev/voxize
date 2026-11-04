"""Getting the transcript out of the terminal client: clipboard and stdout.

Clipboard: the first tool that fits this session and works — wl-copy on
Wayland, xclip or xsel on X11 — else OSC 52, which asks the terminal (and
tmux, with set-clipboard on) but gets no answer back.

Stdout: on exit, after Textual has restored the screen, the transcript is
written so ``voxize-tui | tmux load-buffer -`` works (Textual draws on
stderr, so stdout is free).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from collections.abc import Awaitable, Callable, Mapping
from typing import IO

logger = logging.getLogger(__name__)

TOOL_TIMEOUT_S = 2.0


def clipboard_tools(
    environ: Mapping[str, str] = os.environ,
    which: Callable[[str], str | None] = shutil.which,
) -> list[list[str]]:
    """Clipboard commands to try, best first, for this session's display."""
    tools: list[list[str]] = []
    if environ.get("WAYLAND_DISPLAY") and (wl_copy := which("wl-copy")):
        tools.append([wl_copy])
    if environ.get("DISPLAY"):
        if xclip := which("xclip"):
            tools.append([xclip, "-selection", "clipboard"])
        if xsel := which("xsel"):
            tools.append([xsel, "--clipboard", "--input"])
    return tools


async def run_tool(cmd: list[str], text: str, stderr: IO | int) -> bool:
    """Feed ``text`` to one clipboard tool. True when it exited 0.

    stdout is /dev/null: these tools fork a child that keeps serving the
    selection, and a child holding our stdout would stop a
    ``| tmux load-buffer -`` reader from ever seeing EOF. stderr goes to
    tui.log: not the terminal, which is the screen, and not a pipe, which
    the forked child would hold open. Only the parent process is awaited.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr,
        )
    except OSError as e:
        logger.debug("run_tool: %s did not start: %s", cmd[0], e)
        return False
    try:
        await asyncio.wait_for(
            proc.communicate(text.encode("utf-8")), timeout=TOOL_TIMEOUT_S
        )
    except (TimeoutError, OSError) as e:
        logger.debug("run_tool: %s did not finish: %r", cmd[0], e)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return False
    return proc.returncode == 0


class Clipboard:
    """Copies via the first clipboard tool that works, else OSC 52.

    Copies are serialised: the cleaned result can never be overtaken by the
    batch copy it replaces.
    """

    def __init__(
        self,
        *,
        osc52: Callable[[str], None],
        stderr_target: Callable[[], IO | int],
        tools: Callable[[], list[list[str]]] = clipboard_tools,
        run: Callable[[list[str], str, IO | int], Awaitable[bool]] = run_tool,
    ) -> None:
        self._osc52 = osc52
        self._stderr_target = stderr_target
        self._tools = tools
        self._run = run
        self._lock = asyncio.Lock()
        # True once a copy fell through to OSC 52, which cannot confirm it.
        self.unavailable = False

    async def copy(self, text: str) -> str:
        """Copy ``text``; returns what carried it (a tool name, or "osc52")."""
        async with self._lock:
            for cmd in self._tools():
                name = os.path.basename(cmd[0])
                if await self._run(cmd, text, self._stderr_target()):
                    logger.debug("copy: %s, text_len=%d", name, len(text))
                    return name
                logger.debug("copy: %s failed", name)
            self._osc52(text)
            self.unavailable = True
            logger.debug("copy: osc52, text_len=%d", len(text))
            return "osc52"

    async def settle(self) -> None:
        """Wait for any copy in progress (exit would cancel it)."""
        async with self._lock:
            pass


def final_output(text: str | None, isatty: bool) -> tuple[str, int]:
    """What to write to stdout on exit, and the exit code.

    No trailing newline when stdout is not a terminal: a pasted "\\n" would
    submit the prompt in Claude Code. One when it is, so the shell prompt
    starts on its own line. Exit code 0 when there was text, else 1.
    """
    if not text:
        return "", 1
    return (text + "\n" if isatty else text), 0
