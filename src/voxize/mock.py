"""Mock providers for UI development.

These emit predefined text on timers, simulating the real OpenAI Realtime
transcription deltas and GPT-5.4 Nano cleanup streaming.
"""

import logging

from gi.repository import GLib

logger = logging.getLogger(__name__)

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


class MockTranscription:
    """Emits transcript text word-by-word at ~4 words/sec."""

    def __init__(self) -> None:
        self._source: int | None = None
        self._words: list[str] = []
        self._pos = 0
        self._on_delta = None

    def start(self, on_delta, delay_ms: int = 1500) -> None:
        logger.debug(
            "MockTranscription.start: delay_ms=%d words=%d",
            delay_ms,
            len(_TRANSCRIPT.split()),
        )
        self._on_delta = on_delta
        self._words = _TRANSCRIPT.split()
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


class MockCleanup:
    """Emits cleaned text word-by-word at ~16 words/sec (simulates fast API streaming)."""

    def __init__(self) -> None:
        self._source: int | None = None
        self._words: list[str] = []
        self._pos = 0
        self._on_delta = None
        self._on_complete = None

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
            len(_CLEANED.split()),
        )
        self._on_delta = on_delta
        self._on_complete = on_complete
        self._words = _CLEANED.split()
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
        self._source = GLib.timeout_add(60, self._tick)
        return False  # one-shot

    def _tick(self) -> bool:
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
