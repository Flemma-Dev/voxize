"""Mock providers for UI development.

These emit predefined text on timers, simulating the real OpenAI Realtime
transcription deltas, gpt-transcribe batch streaming (with upload progress)
and GPT-5.6 Luna cleanup streaming, plus a level meter for the UI to poll.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass

from gi.repository import GLib

logger = logging.getLogger(__name__)

# Short enough (< 50 chars) that the UI has no prediction and pulses.
SHORT_TRANSCRIPT = "Fix the typo in the README."

_TRANSCRIPT = (
    "So I've been thinking about this project and how it should work. "
    "Basically when you press the hotkey it opens a small overlay window "
    "and starts recording from the microphone. The audio gets streamed "
    "in real time to OpenAI's transcription API and you see the words "
    "appearing on screen as you speak. When you're done you hit stop "
    "and it sends the transcript through GPT for cleanup. "
    "The cleaned text goes to your clipboard and you can paste it "
    "wherever you want. It's really just about reducing friction "
    "between thinking and having clean text ready to use. "
    "And the nice thing is that the overlay stays out of the way. "
    "It's this small translucent window that floats on top of whatever "
    "you're doing. You don't have to switch context or open a separate app. "
    "You just talk and the words appear. And then when cleanup runs "
    "it fixes all the ums and ahs and punctuation so you get clean "
    "professional text without any effort. I think that's the key insight "
    "here is that the friction between having a thought and having it "
    "written down cleanly should be as close to zero as possible."
)

_CLEANED = (
    "I've been thinking about this project and how it should work. "
    "When you press the hotkey, it opens a small overlay window "
    "and starts recording from the microphone. The audio gets streamed "
    "in real time to OpenAI's transcription API, and you see the words "
    "appearing on screen as you speak. When you're done, you hit Stop, "
    "and it sends the transcript through GPT for cleanup. "
    "The cleaned text goes to your clipboard, and you can paste it "
    "wherever you want. It's about reducing friction "
    "between thinking and having clean text ready to use. "
    "The nice thing is that the overlay stays out of the way. "
    "It's a small translucent window that floats on top of whatever "
    "you're doing. You don't have to switch context or open a separate app. "
    "You just talk, and the words appear. When cleanup runs, "
    "it fixes all the filler words and punctuation so you get clean, "
    "professional text without any effort. The key insight "
    "is that the friction between having a thought and having it "
    "written down cleanly should be as close to zero as possible."
)


@dataclass(frozen=True)
class MockOptions:
    """Mock-mode knobs, from the VOXIZE_MOCK* environment variables.

    ``fast`` (VOXIZE_MOCK_FAST=1) shrinks the fixed delays — warming,
    preview start, cleanup thinking time and cleanup token rate — so tests
    can run a whole dictation in a few seconds.
    """

    outcome: str = "ok"
    short: bool = False
    error_ms: int | None = None
    stop_ms: int | None = None
    ttfb_ms: int = 1200
    fast: bool = False

    @property
    def warming_ms(self) -> int:
        return 50 if self.fast else 1500

    @property
    def preview_delay_ms(self) -> int:
        return 50 if self.fast else 1500

    @property
    def cleanup_delay_ms(self) -> int:
        return 50 if self.fast else 2500

    @property
    def cleanup_tick_ms(self) -> int:
        return 5 if self.fast else 60

    @classmethod
    def from_env(cls, env=os.environ) -> MockOptions | None:
        if not env.get("VOXIZE_MOCK"):
            return None

        def _int(name: str) -> int | None:
            value = env.get(name)
            return int(value) if value else None

        return cls(
            outcome=env.get("VOXIZE_MOCK_OUTCOME", "ok"),
            short=bool(env.get("VOXIZE_MOCK_SHORT")),
            error_ms=_int("VOXIZE_ERROR"),
            stop_ms=_int("VOXIZE_STOP"),
            ttfb_ms=int(env.get("VOXIZE_MOCK_TTFB", "1200")),
            fast=bool(env.get("VOXIZE_MOCK_FAST")),
        )


class MockLevelMeter:
    """Speech-like RMS level for the UI meter, in dBFS.

    A slow 8 s swell under a faster syllable ripple, so a few seconds of
    recording sweeps every zone: quiet, good, hot and (briefly) clip.
    """

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    @property
    def level_dbfs(self) -> float:
        t = time.monotonic() - self._t0
        swell = 0.5 + 0.5 * math.sin(2 * math.pi * t / 8 - math.pi / 2)
        syllable = 0.75 + 0.25 * math.sin(2 * math.pi * t * 3)
        return -58.0 + 57.0 * swell * syllable


class MockTranscription:
    """Emits transcript text word-by-word at ~4 words/sec."""

    def __init__(self, text: str = _TRANSCRIPT) -> None:
        self._text = text
        self._source: int | None = None
        self._words: list[str] = []
        self._pos = 0
        self._on_delta = None

    def start(self, on_delta, delay_ms: int = 1500) -> None:
        logger.debug(
            "MockTranscription.start: delay_ms=%d words=%d",
            delay_ms,
            len(self._text.split()),
        )
        self._on_delta = on_delta
        self._words = self._text.split()
        self._pos = 0
        # Simulate connection/thinking delay before first word
        self._source = GLib.timeout_add(delay_ms, self._begin)

    def stop(self) -> str:
        """Stop and return the transcript emitted so far."""
        logger.debug("MockTranscription.stop: pos=%d/%d", self._pos, len(self._words))
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None
        transcript = " ".join(self._words[: self._pos])
        logger.debug("MockTranscription.stop: transcript_len=%d", len(transcript))
        return transcript

    def cancel(self) -> None:
        logger.debug("MockTranscription.cancel: pos=%d/%d", self._pos, len(self._words))
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None

    def _begin(self) -> bool:
        logger.debug("MockTranscription._begin: starting word emission")
        self._source = GLib.timeout_add(220, self._tick)
        return False  # one-shot

    def _tick(self) -> bool:
        if self._pos >= len(self._words):
            logger.debug(
                "MockTranscription._tick: all %d words emitted", len(self._words)
            )
            self._source = (
                None  # GLib auto-removes; clear to prevent stale source_remove
            )
            return False  # all words emitted, stay "recording"
        sep = " " if self._pos > 0 else ""
        self._on_delta(sep + self._words[self._pos])
        self._pos += 1
        return True


class MockBatch:
    """Simulates batch transcription: upload progress, TTFB, then streaming.

    Returns the live transcript as the authoritative text. ``ttfb_ms`` sets
    the wait before the first token (raise it to see the stall label);
    ``fail`` errors out halfway through the stream.
    """

    def __init__(self, ttfb_ms: int = 1200, fail: bool = False) -> None:
        self._ttfb_ms = ttfb_ms
        self._fail = fail
        self._source: int | None = None
        self._words: list[str] = []
        self._pos = 0
        self._upload = 0.0
        self._on_delta = None
        self._on_complete = None
        self._on_error = None
        self._on_upload_progress = None

    @property
    def usage(self):
        return None

    def start(
        self, text, on_delta, on_complete, on_error, on_upload_progress=None
    ) -> None:
        logger.debug(
            "MockBatch.start: text_len=%d ttfb_ms=%d fail=%s",
            len(text),
            self._ttfb_ms,
            self._fail,
        )
        self._words = text.split()
        self._pos = 0
        self._upload = 0.0
        self._on_delta = on_delta
        self._on_complete = on_complete
        self._on_error = on_error
        self._on_upload_progress = on_upload_progress
        self._source = GLib.timeout_add(20, self._tick_upload)

    def cancel(self) -> None:
        logger.debug("MockBatch.cancel: pos=%d/%d", self._pos, len(self._words))
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None

    def _tick_upload(self) -> bool:
        self._upload = min(1.0, self._upload + 0.2)
        if self._on_upload_progress:
            self._on_upload_progress(self._upload)
        if self._upload < 1.0:
            return True
        self._source = GLib.timeout_add(self._ttfb_ms, self._begin)
        return False

    def _begin(self) -> bool:
        if not self._words:
            self._source = None
            self._on_complete("")
            return False
        self._source = GLib.timeout_add(40, self._tick)
        return False

    def _tick(self) -> bool:
        if self._fail and self._pos >= len(self._words) // 2:
            self._source = None
            self._on_error(
                "Batch transcription failed: simulated (VOXIZE_MOCK_OUTCOME)"
            )
            return False
        if self._pos >= len(self._words):
            self._source = None
            self._on_complete(" ".join(self._words))
            return False
        sep = " " if self._pos > 0 else ""
        self._on_delta(sep + self._words[self._pos])
        self._pos += 1
        return True


class MockCleanup:
    """Emits cleaned text word-by-word at ~16 words/sec (simulates fast API streaming)."""

    def __init__(
        self, text: str = _CLEANED, fail: bool = False, tick_ms: int = 60
    ) -> None:
        self._text = text
        self._fail = fail
        self._tick_ms = tick_ms
        self._source: int | None = None
        self._words: list[str] = []
        self._pos = 0
        self._on_delta = None
        self._on_complete = None
        self._on_error = None

    def start(
        self,
        transcript: str,
        on_delta,
        on_complete,
        on_error=None,
        delay_ms: int = 2500,
    ) -> None:
        logger.debug(
            "MockCleanup.start: transcript_len=%d delay_ms=%d words=%d",
            len(transcript),
            delay_ms,
            len(self._text.split()),
        )
        self._on_delta = on_delta
        self._on_complete = on_complete
        self._on_error = on_error
        self._words = self._text.split()
        self._pos = 0
        # Simulate API thinking time before first token
        self._source = GLib.timeout_add(delay_ms, self._begin)

    @property
    def usage(self):
        return None

    def cancel(self) -> None:
        logger.debug("MockCleanup.cancel: pos=%d/%d", self._pos, len(self._words))
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None

    def _begin(self) -> bool:
        logger.debug("MockCleanup._begin: starting token emission")
        self._source = GLib.timeout_add(self._tick_ms, self._tick)
        return False  # one-shot

    def _tick(self) -> bool:
        if self._fail and self._on_error and self._pos >= len(self._words) // 3:
            self._source = None
            self._on_error("Cleanup failed: simulated (VOXIZE_MOCK_OUTCOME)")
            return False
        if self._pos >= len(self._words):
            logger.debug("MockCleanup._tick: all %d words emitted", len(self._words))
            self._source = (
                None  # GLib auto-removes; clear to prevent stale source_remove
            )
            if self._on_complete:
                cleaned = " ".join(self._words)
                logger.debug(
                    "MockCleanup._tick: complete, cleaned_len=%d", len(cleaned)
                )
                self._on_complete(cleaned)
            return False
        sep = " " if self._pos > 0 else ""
        self._on_delta(sep + self._words[self._pos])
        self._pos += 1
        return True


def default_transcript(short: bool = False) -> str:
    """The text a mock session "said" — what the batch pass transcribes."""
    return SHORT_TRANSCRIPT if short else _TRANSCRIPT
