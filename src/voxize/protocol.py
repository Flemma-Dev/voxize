"""Voxize stdio protocol — JSON Lines between a frontend and the backend.

One JSON object per line, UTF-8, newline-terminated, with a ``type`` field.
Backend → frontend messages are *events*, frontend → backend messages are
*commands*. Receivers ignore unknown fields, and log and drop unknown types.

The backend owns domain facts and the estimates derived from them; the
frontend owns how they are drawn, worded and interacted with. Nothing here
names a window, a terminal, focus, or a widget.

Stdlib only. With ``voxize.config`` this is what a frontend shares with the
backend side of the codebase.
"""

from __future__ import annotations

import json
from enum import Enum, auto

PROTOCOL_VERSION = 1


class State(Enum):
    INITIALIZING = auto()
    WARMING = auto()
    RECORDING = auto()
    TRANSCRIBING = auto()
    CLEANING = auto()
    READY = auto()
    CANCELLED = auto()
    ERROR = auto()


# Event type → its exact field set (besides "type").
EVENTS: dict[str, frozenset[str]] = {
    "hello": frozenset({"protocol"}),
    "context": frozenset({"sources"}),  # [{"path": str, "content": str}]
    "session": frozenset({"dir"}),
    "state": frozenset({"state", "error"}),  # State name; error str | None
    "level": frozenset({"dbfs"}),
    "text": frozenset({"stage", "op", "text"}),  # live|batch|cleaned, append|replace
    "preview_lost": frozenset({"reason"}),
    "progress": frozenset({"fraction"}),  # float | None (no estimate)
    "stalled": frozenset({"stalled"}),
    "result": frozenset({"stage", "text"}),  # batch|cleaned — text to deliver
    "no_speech": frozenset(),
    "failure": frozenset({"phase", "message", "fallback"}),  # fallback live|batch|None
    "costs": frozenset({"live", "batch", "cleanup", "total"}),  # USD | None
}

# Command type → its exact field set (besides "type").
COMMANDS: dict[str, frozenset[str]] = {
    "start": frozenset({"cwd", "glossaries"}),
    "stop": frozenset(),
    "cancel": frozenset(),
    "close": frozenset({"trash_if_empty"}),
}


class ProtocolError(ValueError):
    """A message that does not fit the protocol."""


def _build(table: dict[str, frozenset[str]], type_: str, fields: dict) -> dict:
    expected = table.get(type_)
    if expected is None:
        raise ProtocolError(f"unknown message type {type_!r}")
    if set(fields) != expected:
        raise ProtocolError(
            f"{type_}: expected fields {sorted(expected)}, got {sorted(fields)}"
        )
    return {"type": type_, **fields}


def event(type_: str, **fields) -> dict:
    """Build a backend → frontend event, checking its field set."""
    return _build(EVENTS, type_, fields)


def command(type_: str, **fields) -> dict:
    """Build a frontend → backend command, checking its field set."""
    return _build(COMMANDS, type_, fields)


def encode(msg: dict) -> str:
    """One compact JSON line. NaN/Infinity are not JSON and are refused."""
    return (
        json.dumps(msg, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    )


def decode(line: str | bytes) -> dict:
    """Parse one line. Raises ProtocolError (a ValueError) if it is not a message."""
    try:
        obj = json.loads(line)
    except ValueError as e:  # JSONDecodeError, or UnicodeDecodeError for bad bytes
        raise ProtocolError(f"not JSON: {e}") from e
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        raise ProtocolError("not a message: expected an object with a string 'type'")
    return obj
