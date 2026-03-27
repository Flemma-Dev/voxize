"""Mock providers for Phase 1 UI development.

These emit predefined text on timers, simulating the real OpenAI Realtime
transcription deltas and GPT-5.4 Mini cleanup streaming.
"""

from gi.repository import GLib

_TRANSCRIPT = (
    "So I've been thinking about this project and how it should work. "
    "Basically when you press the hotkey it opens a small overlay window "
    "and starts recording from the microphone. The audio gets streamed "
    "in real time to OpenAI's transcription API and you see the words "
    "appearing on screen as you speak. When you're done you hit stop "
    "and it sends the transcript through Claude for cleanup. "
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
    "and it sends the transcript through Claude for cleanup. "
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
        self._on_delta = on_delta
        self._words = _TRANSCRIPT.split()
        self._pos = 0
        # Simulate connection/thinking delay before first word
        self._source = GLib.timeout_add(delay_ms, self._begin)

    def stop(self) -> str:
        """Stop and return the transcript emitted so far."""
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None
        return " ".join(self._words[: self._pos])

    def cancel(self) -> None:
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None

    def _begin(self) -> bool:
        self._source = GLib.timeout_add(220, self._tick)
        return False  # one-shot

    def _tick(self) -> bool:
        if self._pos >= len(self._words):
            self._source = None  # GLib auto-removes; clear to prevent stale source_remove
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

    def start(self, transcript: str, on_delta, on_complete, delay_ms: int = 2500) -> None:
        self._on_delta = on_delta
        self._on_complete = on_complete
        self._words = _CLEANED.split()
        self._pos = 0
        # Simulate API thinking time before first token
        self._source = GLib.timeout_add(delay_ms, self._begin)

    def cancel(self) -> None:
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None

    def _begin(self) -> bool:
        self._source = GLib.timeout_add(60, self._tick)
        return False  # one-shot

    def _tick(self) -> bool:
        if self._pos >= len(self._words):
            self._source = None  # GLib auto-removes; clear to prevent stale source_remove
            if self._on_complete:
                self._on_complete(" ".join(self._words))
            return False
        sep = " " if self._pos > 0 else ""
        self._on_delta(sep + self._words[self._pos])
        self._pos += 1
        return True
