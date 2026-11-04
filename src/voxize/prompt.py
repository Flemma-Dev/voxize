"""Vocabulary guidance: reading the sources a frontend points at, and the prompt.

The backend gets a working directory and glossary file paths from the
frontend (``start {cwd, glossaries}``). ``load_sources`` reads them, and
``build_transcription_prompt`` turns them into the one string that batch
sends, ``recover.sh`` embeds and ``prompt.txt`` archives.

Where those paths come from — the focused window, tmux, a per-app
glossary — is a frontend skill: see ``voxize.gui.context``.

Every read is best-effort and never raises into startup.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_WHISPER_FILE = "WHISPER.txt"

# The transcription API rejects the whole request when `prompt` exceeds the
# model's (undocumented) length limit. Guidance files are one-liners in
# practice, so this only makes a pathological one degrade into a truncated
# prompt rather than into no transcript at all.
_MAX_PROMPT_CHARS = 4000

_SNAPSHOT_FILE = "prompt.txt"


@dataclass
class PromptSource:
    """A single vocabulary guidance source."""

    path: str  # absolute file path
    content: str  # stripped, non-empty content


def build_transcription_prompt(prompts: list[PromptSource] | None) -> str | None:
    """Return the exact string sent as the transcription API's ``prompt``.

    Single source of truth. ``batch.py`` sends it, ``recover.py`` bakes it
    into recover.sh, and ``write_prompt_snapshot`` archives it — if those
    built it independently they would drift, and the snapshot would quietly
    stop describing what was actually sent.

    Content is joined verbatim, unparsed. Guidance files are *prompts*, not
    keyword lists: usually a ``Glossary: X, Y, Z`` line, sometimes prose,
    and prose measurably helps the hard cases.
    """
    if not prompts:
        return None
    combined = "\n".join(s.content for s in prompts).strip()
    if not combined:
        return None
    if len(combined) > _MAX_PROMPT_CHARS:
        logger.warning(
            "build_transcription_prompt: truncating %d chars to %d",
            len(combined),
            _MAX_PROMPT_CHARS,
        )
        combined = combined[:_MAX_PROMPT_CHARS]
    return combined


def write_prompt_snapshot(session_dir: str, prompts: list[PromptSource] | None) -> None:
    """Archive the prompt this session used, into ``prompt.txt``.

    Guidance files live outside the session and change over time — a project's
    WHISPER.txt gets edited, a Slack-channel glossary is per-window. Without a
    snapshot there is no way to tell after the fact whether a transcription
    error happened because a term was missing from the glossary or in spite of
    it being there.

    The file is the verbatim payload, so it is directly comparable against
    what `recover.sh` embeds. Source paths go to the log rather than a header,
    which would make the file no longer byte-equal to what was sent.

    Best-effort, like the rest of this module: never raise into startup.
    """
    prompt = build_transcription_prompt(prompts)
    if not prompt:
        logger.debug("write_prompt_snapshot: no prompts, nothing to archive")
        return
    try:
        path = os.path.join(session_dir, _SNAPSHOT_FILE)
        with open(path, "w") as f:
            f.write(prompt)
        logger.debug(
            "write_prompt_snapshot: chars=%d sources=%s",
            len(prompt),
            [s.path for s in (prompts or [])],
        )
    except Exception:
        logger.debug("write_prompt_snapshot: failed", exc_info=True)


def load_sources(cwd: str | None, glossaries: list[str]) -> list[PromptSource]:
    """Read the vocabulary a frontend pointed us at, in specificity order.

    ``cwd`` contributes its WHISPER.txt (newlines collapsed); each glossary
    path is read as-is, in the order given. Missing or empty files are
    skipped.
    """
    sources: list[PromptSource] = []
    if cwd:
        whisper = _read_whisper(cwd)
        if whisper:
            sources.append(whisper)
    for path in glossaries:
        source = _read_glossary(path)
        if source:
            sources.append(source)
    logger.debug(
        "prompt: load_sources cwd=%s returning %d source(s): %s",
        cwd,
        len(sources),
        [s.path for s in sources],
    )
    return sources


def _read_glossary(path: str) -> PromptSource | None:
    try:
        with open(path) as f:
            content = f.read().strip()
    except OSError:
        logger.debug("prompt: failed to load glossary %s", path, exc_info=True)
        return None
    if not content:
        logger.debug("prompt: glossary %s is empty", path)
        return None
    logger.debug("prompt: loaded glossary %s (%d chars)", path, len(content))
    return PromptSource(path=path, content=content)


def detect_prompt() -> list[PromptSource]:
    """Compatibility shim for app.py until the GUI is a protocol client.

    Deleted in the task that rewires the GUI; nothing else may call it.
    """
    from voxize.gui.context import detect_context

    return load_sources(*detect_context())


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
