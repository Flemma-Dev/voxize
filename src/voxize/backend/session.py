"""Voxize dictation session — the backend's orchestrator.

Moved from the old app.py Gtk.Application with its behaviour intact: the
same fast-startup bootstrap, warming detector, warmup pings and cache
priming, batch-before-WS-teardown overlap, cleanup, cost accounting and
mock mode. Two things changed. Output: every place app.py called a
window method now emits a protocol event. Input: four commands
(start, stop, cancel, close) instead of widget callbacks.

Runs on a plain GLib.MainLoop — no GTK. Threads are as before: the main
loop (this class), the sounddevice callback, the ``voxize-ws`` asyncio
thread, and short-lived daemon threads (bootstrap, stop, warmup,
teardown). ``emit`` is only ever called on the main loop.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from gi.repository import Gio, GLib

from voxize import protocol
from voxize._trace import trace as _trace
from voxize.backend.presenter import Pipeline, Transcript
from voxize.batch import BatchTranscription
from voxize.checks import get_api_key
from voxize.cleanup import Cleanup
from voxize.ducking import VolumeDucker
from voxize.lock import MicLock, MicLockError
from voxize.mock import (
    SHORT_TRANSCRIPT,
    MockBatch,
    MockCleanup,
    MockLevelMeter,
    MockOptions,
    MockTranscription,
    default_transcript,
)
from voxize.prompt import PromptSource, load_sources, write_prompt_snapshot
from voxize.recover import write_recover_script
from voxize.state import State, StateMachine
from voxize.storage import create_session_dir, prune_sessions
from voxize.transcribe import RealtimeTranscription

if TYPE_CHECKING:
    from openai import OpenAI

logger = logging.getLogger(__name__)

Emit = Callable[[dict], None]

# Connection pool keepalive for the shared OpenAI client. Client-side
# upper bound; the server may idle-close sooner, which is why we also
# fire periodic warmup pings while RECORDING.
_KEEPALIVE_SECONDS = 900  # 15 minutes
_WARMUP_INTERVAL_SECONDS = 45  # safely under typical LB idle timeouts (~60-120s)

# WARMING → RECORDING thresholds. The mic stream is open but BT headsets
# switch A2DP → HFP on capture-open and may deliver silent zero-chunks
# for 200-1500ms before real audio flows. Once the level meter crosses
# the threshold for _WARMING_REQUIRED_LOUD_CHUNKS consecutive 40ms
# blocks (≈80ms of audio), we call it "really recording" and flip the
# UI. _WARMING_TIMEOUT_MS is the hard fallback — a very quiet room may
# never cross the threshold, and we still want to show RECORDING.
_WARMING_RMS_THRESHOLD_DBFS = -70.0
_WARMING_REQUIRED_LOUD_CHUNKS = 2
_WARMING_TIMEOUT_MS = 5000
_WARMING_POLL_MS = 40  # same cadence as the level-meter UI tick

_LEVEL_MS = 40  # level events: the cadence the window used to poll the meter at
_PIPELINE_TICK_MS = 50  # progress events: TTFB-wait interpolation, ≤ 20 Hz
_STALL_POLL_MS = 250  # the cadence the window's stall watch ran at

_CAPTURE = (State.WARMING, State.RECORDING)
_WORKING = (State.TRANSCRIBING, State.CLEANING)
_IN_FLIGHT = (
    State.INITIALIZING,
    State.WARMING,
    State.RECORDING,
    State.TRANSCRIBING,
    State.CLEANING,
)


class Session:
    def __init__(
        self,
        emit: Emit,
        *,
        mock: MockOptions | None = None,
        on_exit: Callable[[], None] = lambda: None,
    ) -> None:
        self._emit = emit
        self._mock = mock
        self._on_exit = on_exit
        self._started = False
        self._closed = False

        # Registered first: a state event goes out before anything the
        # transition itself triggers.
        self._machine = StateMachine()
        self._machine.on_change(self._emit_state)
        self._machine.on_change(self._on_state_change)

        # View logic moved out of the window (see presenter.py).
        self._pipeline = Pipeline()
        self._transcript = Transcript()
        self._meter = None  # LevelMeter or MockLevelMeter
        self._level_timer_id: int | None = None
        self._pipeline_timer_id: int | None = None
        self._stall_timer_id: int | None = None
        self._last_fraction: float | None = 0.0  # what the frontend last saw
        self._last_stalled = False

        # Real providers
        self._lock = None
        self._session_dir: str | None = None
        self._api_key: str | None = None
        self._prompts: list[PromptSource] = []
        self._audio = None
        self._transcription = None
        self._batch = None
        self._cleanup = None

        # Shared OpenAI client for batch + cleanup. Single httpx connection
        # pool so the TLS/HTTP1.1 keep-alive socket established during one
        # phase can be reused by the next. Built lazily in the bootstrap
        # thread so the openai SDK import does not block mic-open.
        self._client: OpenAI | None = None
        self._warmup_timer_id: int | None = None

        # Bootstrap coordination — see _bootstrap_providers.
        self._bootstrap_done = threading.Event()
        self._bootstrap_cancelled = False

        # WARMING → RECORDING detector
        self._warming_timer_id: int | None = None
        self._warming_start_time = 0.0
        self._warming_consecutive_loud = 0

        # Mock transcription (only in mock mode)
        self._mock_transcription: MockTranscription | None = None
        self._mock_preview_lost = False

        # Batch transcript (passed from TRANSCRIBING to CLEANING)
        self._batch_transcript: str = ""

        # Session cost tracking (three phases + the billable warmup pings)
        self._live_usage: dict[str, int] | None = None
        self._batch_usage: dict[str, int] | None = None
        self._cleanup_usage: dict[str, int] | None = None
        self._warmup_usage: dict[str, int] | None = None
        # Only the first ping carries the cleanup prompt; see _warmup_ping.
        self._warmup_primed = False

        # Per-app volume ducking while recording. Disabled in mock mode so
        # tests don't silence the user's browser tabs.
        self._ducker = VolumeDucker(apps=[] if mock else None)

        self._log_handler = None
        self._trash_session = False

    # ── Public surface (commands) ──

    @property
    def state(self) -> State:
        return self._machine.state

    @property
    def closed(self) -> bool:
        return self._closed

    def fail_startup(self, errors: list[str]) -> None:
        """Startup checks failed: the frontend shows them in the ERROR card."""
        logger.info("fail_startup: %s", errors)
        self._machine.transition(State.ERROR, error="\n".join(errors))

    def start(self, cwd: str | None, glossaries: list[str]) -> None:
        """Begin the session with the vocabulary the frontend pointed at."""
        if self._started or self._machine.state != State.INITIALIZING:
            logger.debug(
                "start: ignored, started=%s state=%s",
                self._started,
                self._machine.state.name,
            )
            return
        self._started = True
        self._prompts = load_sources(cwd, glossaries)
        self._emit(
            protocol.event(
                "context",
                sources=[{"path": s.path, "content": s.content} for s in self._prompts],
            )
        )
        _trace(f"start: context sent (sources={len(self._prompts)})")
        if self._mock:
            self._initialize_mock()
        else:
            self._initialize()

    def stop(self) -> None:
        state = self._machine.state
        logger.debug("stop: state=%s", state.name)
        if state in _CAPTURE:
            self._machine.transition(State.TRANSCRIBING)
        else:
            logger.debug("stop: ignored in %s", state.name)

    def cancel(self) -> None:
        state = self._machine.state
        logger.debug("cancel: state=%s", state.name)
        if state in _IN_FLIGHT:
            self._machine.transition(State.CANCELLED)
        else:
            logger.debug("cancel: ignored in %s", state.name)

    def close(self, trash_if_empty: bool) -> None:
        """End the session and the process — what closing the window used to do.

        Cancels an in-flight dictation first. With ``trash_if_empty`` the
        session folder goes to the trash when no transcript was ever
        produced (the Ctrl+M switch to the meeting recorder).
        """
        if self._closed:
            logger.debug("close: already closed")
            return
        self._closed = True
        logger.debug(
            "close: trash_if_empty=%s state=%s had_content=%s",
            trash_if_empty,
            self._machine.state.name,
            self._transcript.had_content,
        )
        if self._machine.state in _IN_FLIGHT:
            self._machine.transition(State.CANCELLED)
        if trash_if_empty and not self._transcript.had_content:
            self._trash_session = True
        self._stop_level_timer()
        self._stop_pipeline_timers()
        # Tell any in-flight bootstrap to stop.
        self._bootstrap_cancelled = True
        # Restore ducked volumes synchronously — closing mid-recording would
        # otherwise leave apps silent forever.
        try:
            self._ducker.restore_sync()
        except Exception:
            logger.exception("Close: failed to restore ducked volumes")
        self._stop_warmup()
        self._teardown_async()
        if self._mock_transcription:
            self._mock_transcription.cancel()
        if self._batch:
            self._batch.cancel()
        if self._cleanup:
            self._cleanup.cancel()
        if self._client:
            try:
                self._client.close()
            except Exception:
                logger.debug("Close: failed to close OpenAI client", exc_info=True)
            self._client = None
        # Prune old sessions (best-effort, at termination time)
        try:
            prune_sessions()
        except Exception:
            logger.debug("close: prune_sessions failed", exc_info=True)
        if self._log_handler:
            for name in ("voxize", "openai", "httpx", "httpcore"):
                logging.getLogger(name).removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None
        if self._trash_session and self._session_dir:
            try:
                Gio.File.new_for_path(self._session_dir).trash(None)
                logger.info("trashed empty session: %s", self._session_dir)
            except Exception:
                logger.debug("failed to trash session", exc_info=True)
        self._on_exit()

    def on_signal(self, sig: int) -> None:
        """SIGTERM/SIGINT — finalize WAV, release lock, restore volumes, exit."""
        logger.debug("on_signal: sig=%s", sig)
        logger.info("Signal %s received, shutting down", sig)
        self._closed = True
        self._bootstrap_cancelled = True
        if self._audio:
            try:
                self._audio.finalize_wav()
            except Exception:
                logger.exception("Signal handler: failed to finalize WAV")
        if self._lock:
            try:
                self._lock.release()
                self._lock = None
            except Exception:
                pass
        # Restore ducked volumes synchronously — daemon threads die with
        # the process, so fire-and-forget is not safe here.
        try:
            self._ducker.restore_sync()
        except Exception:
            logger.exception("Signal handler: failed to restore ducked volumes")
        self._on_exit()

    # ── Output helpers ──

    def _emit_state(self, machine: StateMachine, _old: State, new: State) -> None:
        self._emit(
            protocol.event(
                "state",
                state=new.name,
                error=machine.error_message if new == State.ERROR else None,
            )
        )

    def _emit_text(self, stage: str, text: str) -> None:
        op = self._transcript.add(stage, text)
        self._emit(protocol.event("text", stage=stage, op=op, text=text))

    def _replace_text(self, stage: str, text: str) -> None:
        self._transcript.replace(stage, text)
        self._emit(protocol.event("text", stage=stage, op="replace", text=text))

    # ── Level and pipeline timers ──

    def _start_level_timer(self) -> None:
        if self._level_timer_id is None:
            self._level_timer_id = GLib.timeout_add(_LEVEL_MS, self._tick_level)

    def _stop_level_timer(self) -> None:
        if self._level_timer_id is not None:
            GLib.source_remove(self._level_timer_id)
            self._level_timer_id = None

    def _tick_level(self) -> bool:
        """Raw RMS, every tick, never gated on VAD."""
        meter = self._meter
        if meter is not None:
            self._emit(protocol.event("level", dbfs=round(meter.level_dbfs, 1)))
        return True

    def _start_pipeline_timers(self) -> None:
        self._pipeline.begin()
        self._last_fraction = 0.0
        self._last_stalled = False
        self._pipeline_timer_id = GLib.timeout_add(
            _PIPELINE_TICK_MS, self._tick_pipeline
        )
        self._stall_timer_id = GLib.timeout_add(_STALL_POLL_MS, self._tick_stall)

    def _stop_pipeline_timers(self) -> None:
        for attr in ("_pipeline_timer_id", "_stall_timer_id"):
            source = getattr(self, attr)
            if source is not None:
                GLib.source_remove(source)
                setattr(self, attr, None)
        self._pipeline.stop()

    def _tick_pipeline(self) -> bool:
        self._pipeline.tick()
        self._flush_progress()
        return True

    def _flush_progress(self) -> None:
        """Send the progress fraction if it changed enough to see."""
        f = self._pipeline.fraction
        last = self._last_fraction
        changed = (f is None) != (last is None) or (
            f is not None and (abs(f - last) >= 0.001 or (f == 1.0 and last != 1.0))
        )
        if changed:
            self._last_fraction = f
            self._emit(
                protocol.event("progress", fraction=None if f is None else round(f, 4))
            )

    def _tick_stall(self) -> bool:
        stalled = self._pipeline.check_stall()
        if stalled != self._last_stalled:
            self._last_stalled = stalled
            self._emit(protocol.event("stalled", stalled=stalled))
        return True

    # ── Real initialization (fast startup) ──

    def _initialize(self) -> None:
        """Acquire mic lock, set up session, then bootstrap audio + WS.

        Only fast, non-blocking work runs on the main loop: mic lock,
        session dir, file logging. The heavy ``sounddevice`` import and the
        openai SDK import are deferred to the background
        "voxize-bootstrap" thread.
        """
        _trace("_initialize entry")
        logger.debug("_initialize: acquiring lock")
        try:
            self._lock = MicLock()
            self._lock.acquire()
        except MicLockError as e:
            self._machine.transition(State.ERROR, error=str(e))
            return
        except Exception as e:
            self._machine.transition(State.ERROR, error=f"Lock failed: {e}")
            return
        logger.debug("_initialize: lock acquired")
        _trace("mic lock acquired")

        try:
            self._session_dir = create_session_dir()
            logger.debug("_initialize: session_dir=%s", self._session_dir)
            self._api_key = get_api_key("openai")
            logger.debug("_initialize: api_key retrieved")
        except Exception as e:
            self._release_lock()
            self._machine.transition(State.ERROR, error=str(e))
            return
        _trace("session_dir created, api_key retrieved")

        self._emit(protocol.event("session", dir=self._session_dir))
        write_recover_script(self._session_dir, self._prompts)
        write_prompt_snapshot(self._session_dir, self._prompts)

        # Set up session-level file logging
        fh = logging.FileHandler(os.path.join(self._session_dir, "debug.log"))
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d %(threadName)-14s %(name)s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        logging.getLogger("voxize").addHandler(fh)
        logging.getLogger("voxize").setLevel(logging.DEBUG)
        for name in ("openai", "httpx", "httpcore"):
            lg = logging.getLogger(name)
            lg.addHandler(fh)
            lg.setLevel(logging.DEBUG)
        logging.getLogger("httpcore.http11").setLevel(logging.INFO)
        logging.getLogger("httpcore.http2").setLevel(logging.INFO)
        self._log_handler = fh
        logger.debug("_initialize: file logging active")
        logger.debug("_initialize: prompts=%d", len(self._prompts))

        # Audio import + capture + openai all happen in the bootstrap
        # thread. The sounddevice import (~2s via numpy) must never
        # block the main loop.
        self._bootstrap_done.clear()
        self._bootstrap_cancelled = False
        threading.Thread(
            target=self._bootstrap_providers,
            daemon=True,
            name="voxize-bootstrap",
        ).start()
        _trace("bootstrap thread spawned")

    # ── WARMING → RECORDING detector ──

    def _start_warming_detector(self) -> None:
        """Watch the level meter and flip to RECORDING once audio is flowing."""
        self._stop_warming_detector()
        self._warming_start_time = time.monotonic()
        self._warming_consecutive_loud = 0
        self._warming_timer_id = GLib.timeout_add(_WARMING_POLL_MS, self._check_warming)
        logger.debug(
            "warming: detector armed threshold=%.1f dBFS timeout=%dms",
            _WARMING_RMS_THRESHOLD_DBFS,
            _WARMING_TIMEOUT_MS,
        )

    def _stop_warming_detector(self) -> None:
        if self._warming_timer_id is not None:
            GLib.source_remove(self._warming_timer_id)
            self._warming_timer_id = None

    def _mock_warming_to_recording(self) -> bool:
        """Main-loop idle: skip the audio-detection wait in mock mode."""
        if self._machine and self._machine.state == State.WARMING:
            self._machine.transition(State.RECORDING)
        return False

    def _check_warming(self) -> bool:
        """GLib timer: poll the level meter, transition when audio flows."""
        if not self._machine or self._machine.state != State.WARMING:
            self._warming_timer_id = None
            return False
        elapsed_ms = (time.monotonic() - self._warming_start_time) * 1000
        audio = self._audio
        level = audio.meter.level_dbfs if audio else -96.0
        if level > _WARMING_RMS_THRESHOLD_DBFS:
            self._warming_consecutive_loud += 1
            if self._warming_consecutive_loud >= _WARMING_REQUIRED_LOUD_CHUNKS:
                logger.debug(
                    "warming: audio detected level=%.1f dBFS after %dms",
                    level,
                    int(elapsed_ms),
                )
                _trace(f"warming: audio detected at {int(elapsed_ms)}ms")
                self._warming_timer_id = None
                self._machine.transition(State.RECORDING)
                return False
        else:
            self._warming_consecutive_loud = 0
        if elapsed_ms >= _WARMING_TIMEOUT_MS:
            logger.debug(
                "warming: timeout after %dms, transitioning anyway (last level=%.1f dBFS)",
                int(elapsed_ms),
                level,
            )
            _trace(f"warming: timeout at {int(elapsed_ms)}ms")
            self._warming_timer_id = None
            self._machine.transition(State.RECORDING)
            return False
        return True

    def _bootstrap_providers(self) -> None:
        """Background thread: import audio + openai, start mic, build client, start WS.

        The sounddevice import (~2s via numpy on cold cache) and the
        openai SDK import (~300ms) both run here so neither blocks the
        main loop. Audio capture starts first; the frontend is
        already showing INITIALIZING by this point.

        Respects ``self._bootstrap_cancelled`` at checkpoints so
        Escape-during-startup doesn't leave a half-wired client behind.
        """
        _trace("bootstrap: entry")
        if self._bootstrap_cancelled:
            _trace("bootstrap: cancelled before audio import")
            self._bootstrap_done.set()
            return

        # ── Phase 0: audio (sounddevice + numpy, ~2s cold) ──

        from voxize.audio import AudioCapture

        _trace("bootstrap: sounddevice + numpy imported")

        if self._bootstrap_cancelled:
            _trace("bootstrap: cancelled after audio import")
            self._bootstrap_done.set()
            return

        audio = AudioCapture(self._session_dir)
        try:
            audio.start()
        except Exception as e:
            logger.exception("bootstrap: failed to start audio")

            def _audio_error(err=str(e)):
                self._release_lock()
                if self._machine and self._machine.state == State.INITIALIZING:
                    self._machine.transition(State.ERROR, error=f"Microphone: {err}")
                return False

            GLib.idle_add(_audio_error)
            self._bootstrap_done.set()
            return
        self._audio = audio
        _trace("bootstrap: audio.start() returned (mic capturing)")

        def _enter_warming():
            if self._bootstrap_cancelled:
                return False
            if not self._machine or self._machine.state != State.INITIALIZING:
                return False
            self._meter = audio.meter
            self._machine.transition(State.WARMING)
            _trace("WARMING state entered")
            return False

        GLib.idle_add(_enter_warming)

        # ── Phase 1: openai client ──

        if self._bootstrap_cancelled:
            _trace("bootstrap: cancelled before client build")
            self._bootstrap_done.set()
            return

        client: OpenAI | None = None
        try:
            import httpx
            from openai import OpenAI as _OpenAI

            from voxize import openai_patches

            _trace("bootstrap: openai + httpx imported")
            openai_patches.install()
            _trace("bootstrap: openai_patches installed")

            http_client = httpx.Client(
                limits=httpx.Limits(keepalive_expiry=_KEEPALIVE_SECONDS),
            )
            client = _OpenAI(api_key=self._api_key, http_client=http_client)
            logger.debug(
                "bootstrap: openai client ready keepalive_expiry=%ds",
                _KEEPALIVE_SECONDS,
            )
            _trace("bootstrap: OpenAI client built")
        except Exception:
            logger.exception("bootstrap: failed to build OpenAI client")
            self._bootstrap_done.set()
            return

        if self._bootstrap_cancelled:
            _trace("bootstrap: cancelled after client build")
            try:
                client.close()
            except Exception:
                logger.debug("bootstrap: client close failed", exc_info=True)
            self._bootstrap_done.set()
            return

        # Publish the client so the batch/cleanup/stop paths can see it.
        self._client = client

        # ── Phase 2: live-preview WS ──

        transcription: RealtimeTranscription | None = None
        try:
            transcription = RealtimeTranscription(
                self._api_key,
                self._session_dir,
            )
            if audio is not None and not self._bootstrap_cancelled:
                audio.set_on_chunk(transcription.send_audio)
                logger.debug("bootstrap: audio callback swapped to WS")
            if self._bootstrap_cancelled:
                _trace("bootstrap: cancelled before WS start")
                self._bootstrap_done.set()
                return
            self._transcription = transcription
            transcription.start(
                on_delta=self._on_live_delta,
                on_error=self._on_ws_error,
                on_ready=self._on_ws_ready,
                on_speech=self._on_speech,
            )
            _trace("bootstrap: transcription.start() invoked")
        except Exception:
            logger.exception("bootstrap: failed to start transcription WS")

        # Schedule warmup on the main loop if we're still RECORDING.
        GLib.idle_add(self._kick_warmup_after_bootstrap)
        self._bootstrap_done.set()
        _trace("bootstrap: complete")

    def _kick_warmup_after_bootstrap(self) -> bool:
        """Main loop: start warmup pings once bootstrap has produced a client."""
        if self._machine and self._machine.state == State.RECORDING:
            self._start_warmup()
        return False

    def _on_ws_ready(self) -> None:
        """WS session configured — just bookkeeping, not a state gate."""
        logger.debug("_on_ws_ready: WS session configured")

    def _on_speech(self, active: bool) -> None:
        """VAD speech_started/speech_stopped — logged only."""
        if self._machine.state == State.RECORDING:
            logger.debug("on_speech: active=%s", active)

    def _on_live_delta(self, text: str) -> None:
        """Gate live-preview deltas — drop if we've left RECORDING.

        The WS receive loop can deliver deltas via GLib.idle_add for up to
        ~55ms after the TRANSCRIBING transition (until transcription.stop()
        cancels it); a stale one must not reach the transcript.
        """
        if self._machine.state == State.RECORDING:
            self._emit_text("live", text)

    # ── Mock initialization ──

    def _initialize_mock(self) -> None:
        """Mock mode: skip mic/lock/WebSocket, use mock transcription."""
        mock = self._mock
        logger.debug("_initialize_mock: starting mock mode outcome=%s", mock.outcome)
        if mock.outcome == "error":
            self._machine.transition(
                State.ERROR, error="Another Voxize instance is recording"
            )
            return
        self._meter = MockLevelMeter()
        self._machine.transition(State.WARMING)
        if mock.error_ms is not None:
            logger.debug("_initialize_mock: scheduling error after %sms", mock.error_ms)
            GLib.timeout_add(mock.error_ms, self._mock_error)
        # Auto-stop is scheduled on entering RECORDING (see _on_state_change)

    # ── WebSocket callbacks ──

    def _on_ws_error(self, message: str) -> None:
        """WebSocket error — non-fatal during WARMING/RECORDING, fatal before.

        Audio capture continues (the WAV is the safety net); the frontend is
        told the live preview is gone. The user can still stop and get the
        batch transcription.
        """
        current = self._machine.state
        logger.debug("_on_ws_error: state=%s msg=%s", current, message)
        if current in _CAPTURE:
            if self._transcription:
                self._transcription.cancel()
                self._transcription = None
            self._emit(protocol.event("preview_lost", reason=message))
        elif current == State.INITIALIZING:
            self._teardown_async()
            self._machine.transition(State.ERROR, error=message)

    # ── State orchestration ──

    def _on_state_change(self, machine: StateMachine, old: State, new: State) -> None:
        logger.debug("_on_state_change: %s -> %s", old.name, new.name)

        # Per-app volume ducking — orthogonal to the phase logic below.
        if new == State.WARMING:
            self._ducker.duck()
        elif old in _CAPTURE and new not in _CAPTURE:
            self._ducker.restore()

        if new == State.WARMING:
            self._live_usage = None
            self._batch_usage = None
            self._cleanup_usage = None
            self._warmup_usage = None
            self._warmup_primed = False
            self._batch_transcript = ""
            self._start_level_timer()
            if self._mock:
                # Hold WARMING briefly so it can be seen, then RECORDING.
                GLib.timeout_add(self._mock.warming_ms, self._mock_warming_to_recording)
            else:
                self._start_warming_detector()

        if old == State.WARMING and new != State.RECORDING:
            self._stop_warming_detector()

        if new == State.RECORDING:
            if self._mock:
                if not self._mock_preview_lost:
                    self._mock_transcription = MockTranscription(
                        **({"text": SHORT_TRANSCRIPT} if self._mock.short else {})
                    )
                    self._mock_transcription.start(
                        on_delta=self._on_live_delta,
                        delay_ms=self._mock.preview_delay_ms,
                    )
                if self._mock.stop_ms is not None:
                    logger.debug("mock: auto-stop in %dms", self._mock.stop_ms)
                    GLib.timeout_add(self._mock.stop_ms, self._mock_stop)
            else:
                # Warm the shared client's HTTP pool so batch's first call
                # lands on an established TLS socket.
                self._start_warmup()

        if old == State.RECORDING:
            self._stop_warmup()

        if old in _CAPTURE and new not in _CAPTURE:
            self._stop_level_timer()

        if new == State.TRANSCRIBING:
            self._start_pipeline_timers()
            GLib.idle_add(self._begin_transcribing)
        elif new == State.CLEANING:
            GLib.idle_add(self._begin_cleanup)
        elif new == State.CANCELLED:
            # Tell the bootstrap thread to abandon its work.
            self._bootstrap_cancelled = True
            if self._mock_transcription:
                self._mock_transcription.cancel()
                self._mock_transcription = None
            if self._batch:
                self._batch.cancel()
                self._batch = None
            if self._cleanup:
                self._cleanup.cancel()
                self._cleanup = None
            # Stop audio synchronously — it's fast (stream.stop + close + WAV
            # finalize) and MUST complete before Python shutdown.
            audio = self._audio
            self._audio = None
            if audio:
                try:
                    audio.stop()
                except Exception:
                    logger.exception("Cancel: failed to stop audio")
            self._teardown_async()
        elif new == State.ERROR:
            self._bootstrap_cancelled = True
            self._teardown_async()

        if old in _WORKING and new not in _WORKING:
            self._stop_pipeline_timers()

    # ── Phase 1 → Phase 2 ──

    def _start_mock_batch(self, live_transcript: str) -> None:
        """Mock mode: run the batch phase through the real callbacks."""
        self._set_live_len(len(live_transcript))
        mock = self._mock
        if mock.outcome == "no-speech":
            text = ""
        elif self._mock_preview_lost:
            # The real batch transcribes the audio, not the preview.
            text = default_transcript(mock.short)
        else:
            text = live_transcript
        self._batch = MockBatch(ttfb_ms=mock.ttfb_ms, fail=mock.outcome == "batch-fail")
        self._batch.start(
            text,
            on_delta=self._on_batch_delta,
            on_complete=self._on_batch_done,
            on_error=self._on_batch_error,
            on_upload_progress=self._on_upload_progress,
        )

    def _set_live_len(self, live_len: int) -> bool:
        """Main loop: arm the pipeline estimate once the live length is known."""
        logger.debug("_set_live_len: live_len=%d", live_len)
        # Arrives via idle after the WS teardown; a fast empty batch can
        # already have reached READY.
        if self._machine.state in _WORKING:
            self._pipeline.arm(live_len)
        return False

    def _on_upload_progress(self, fraction: float) -> None:
        self._pipeline.upload(fraction)

    def _start_batch(self, session_dir: str, opus_path: str | None = None) -> bool:
        """Main loop: start batch transcription of the audio file."""
        logger.debug("_start_batch: session_dir=%s opus=%s", session_dir, opus_path)
        if self._machine.state != State.TRANSCRIBING:
            return False
        # Prefer the Opus file (much smaller upload), fall back to WAV.
        audio_path = opus_path if opus_path and os.path.exists(opus_path) else None
        if not audio_path:
            audio_path = os.path.join(session_dir, "audio.wav")
        if not os.path.exists(audio_path):
            logger.error("_start_batch: no audio file found")
            self._fail_batch("Audio file not found")
            return False
        self._batch = BatchTranscription(
            self._client, prompts=self._prompts, session_dir=session_dir
        )
        self._batch.start(
            wav_path=audio_path,
            on_delta=self._on_batch_delta,
            on_complete=self._on_batch_done,
            on_error=self._on_batch_error,
            on_upload_progress=self._on_upload_progress,
        )
        return False  # one-shot idle

    def _on_batch_delta(self, text: str) -> None:
        if self._machine.state != State.TRANSCRIBING:
            # Cancelled (or failed) while a delta was already queued.
            logger.debug("_on_batch_delta: dropped in %s", self._machine.state.name)
            return
        if self._transcript.stage != "batch":
            self._pipeline.batch_first_delta()
        self._emit_text("batch", text)
        self._pipeline.delta(text, "batch")

    def _on_batch_done(self, transcript: str) -> None:
        """Batch transcription complete — save, deliver, start cleanup."""
        logger.debug("_on_batch_done: transcript_len=%d", len(transcript))
        self._batch_usage = self._batch.usage if self._batch else None
        self._batch = None
        self._pipeline.batch_done(len(transcript))
        if self._machine.state != State.TRANSCRIBING:
            return
        # Save batch transcript (the authoritative result)
        if transcript and self._session_dir:
            try:
                Path(self._session_dir, "transcription.txt").write_text(transcript)
            except Exception:
                logger.exception("Failed to save transcription.txt")
        # First delivery — batch transcript (the frontend copies it)
        if transcript:
            self._emit(protocol.event("result", stage="batch", text=transcript))
        if not transcript.strip():
            self._transcript.mark_no_speech()
            self._emit(protocol.event("no_speech"))
            self._machine.transition(State.READY)
            self._show_session_costs()
            return
        self._batch_transcript = transcript
        self._machine.transition(State.CLEANING)

    def _on_batch_error(self, message: str) -> None:
        """Batch failed — non-fatal. WAV + recover.sh still usable."""
        logger.error("_on_batch_error: %s", message)
        self._batch_usage = self._batch.usage if self._batch else None
        self._batch = None
        if self._machine.state == State.TRANSCRIBING:
            self._fail_batch(message)

    def _fail_batch(self, message: str) -> None:
        """READY with the live preview standing in, if it said anything."""
        fallback = self._transcript.batch_fallback()
        if fallback is not None:
            self._replace_text("live", fallback)
        self._emit(
            protocol.event(
                "failure",
                phase="batch",
                message=message,
                fallback="live" if fallback is not None else None,
            )
        )
        self._machine.transition(State.READY)
        self._show_session_costs()

    # ── Phase 3: Cleanup ──

    def _begin_cleanup(self) -> bool:
        """Start cleanup of the batch transcript."""
        logger.debug("_begin_cleanup: mock=%s", self._mock is not None)
        if self._machine.state != State.CLEANING:
            return False
        transcript = self._batch_transcript
        if not transcript.strip():
            self._machine.transition(State.READY)
            self._show_session_costs()
            return False
        # The full batch transcript stands while cleanup thinks; the first
        # cleaned token replaces it.
        self._replace_text("batch", transcript)
        if self._mock:
            self._cleanup = MockCleanup(
                **({"text": SHORT_TRANSCRIPT} if self._mock.short else {}),
                fail=self._mock.outcome == "cleanup-fail",
                tick_ms=self._mock.cleanup_tick_ms,
            )
            self._cleanup.start(
                transcript=transcript,
                on_delta=self._on_cleanup_delta,
                on_complete=self._on_cleanup_done,
                on_error=self._on_cleanup_error,
                delay_ms=self._mock.cleanup_delay_ms,
            )
        else:
            self._cleanup = Cleanup(
                self._client, prompts=self._prompts, session_dir=self._session_dir
            )
            self._cleanup.start(
                transcript=transcript,
                on_delta=self._on_cleanup_delta,
                on_complete=self._on_cleanup_done,
                on_error=self._on_cleanup_error,
            )
        return False  # one-shot idle

    def _on_cleanup_delta(self, text: str) -> None:
        if self._machine.state != State.CLEANING:
            logger.debug("_on_cleanup_delta: dropped in %s", self._machine.state.name)
            return
        if self._transcript.stage != "cleaned":
            self._pipeline.cleanup_first_delta()
        self._emit_text("cleaned", text)
        self._pipeline.delta(text, "cleaned")

    def _on_cleanup_done(self, cleaned: str) -> None:
        logger.debug("_on_cleanup_done: cleaned_len=%d", len(cleaned))
        self._cleanup_usage = self._cleanup.usage if self._cleanup else None
        self._cleanup = None
        self._pipeline.finish()
        self._flush_progress()
        if self._machine.state == State.CLEANING:
            if cleaned and self._session_dir:
                try:
                    Path(self._session_dir, "cleaned.txt").write_text(cleaned)
                except Exception:
                    logger.exception("Failed to save cleaned.txt")
            if cleaned:
                # Second delivery — overwrites the batch transcript
                self._emit(protocol.event("result", stage="cleaned", text=cleaned))
            self._machine.transition(State.READY)
            self._show_session_costs()

    def _on_cleanup_error(self, message: str) -> None:
        """Cleanup failed — non-fatal. The batch transcript was already delivered."""
        logger.error("_on_cleanup_error: %s", message)
        self._cleanup_usage = self._cleanup.usage if self._cleanup else None
        self._cleanup = None
        if self._machine.state == State.CLEANING:
            # The full batch transcript stands — never a half-streamed cleanup.
            self._replace_text("batch", self._batch_transcript)
            self._emit(
                protocol.event(
                    "failure", phase="cleanup", message=message, fallback="batch"
                )
            )
            self._machine.transition(State.READY)
            self._show_session_costs()

    def _mock_error(self) -> bool:
        """Fire a simulated WebSocket error."""
        logger.debug(
            "_mock_error: state=%s", self._machine.state if self._machine else None
        )
        if self._machine and self._machine.state in (State.WARMING, State.RECORDING):
            # The real error path cancels the WS; the mock equivalent is to
            # stop (or never start) the mock live preview.
            self._mock_preview_lost = True
            if self._mock_transcription:
                self._mock_transcription.cancel()
            self._on_ws_error("Simulated: WebSocket connection lost")
        return False

    def _mock_stop(self) -> bool:
        """Fire a simulated stop."""
        logger.debug(
            "_mock_stop: state=%s", self._machine.state if self._machine else None
        )
        if self._machine and self._machine.state == State.RECORDING:
            self._machine.transition(State.TRANSCRIBING)
        return False

    # ── Connection warmup ──

    def _start_warmup(self) -> None:
        """Fire an initial warmup ping and schedule recurring pings.

        Idempotent: `_on_state_change(RECORDING)` and
        `_kick_warmup_after_bootstrap` can both call this depending on
        which of "mic warmed up" vs "bootstrap finished" lands first.
        """
        if not self._client or self._warmup_timer_id is not None:
            return
        self._schedule_warmup_ping()
        self._warmup_timer_id = GLib.timeout_add_seconds(
            _WARMUP_INTERVAL_SECONDS, self._warmup_tick
        )
        logger.debug(
            "_start_warmup: initial ping fired, timer_id=%s interval=%ds",
            self._warmup_timer_id,
            _WARMUP_INTERVAL_SECONDS,
        )

    def _stop_warmup(self) -> None:
        if self._warmup_timer_id is not None:
            GLib.source_remove(self._warmup_timer_id)
            logger.debug("_stop_warmup: timer_id=%s removed", self._warmup_timer_id)
            self._warmup_timer_id = None

    def _warmup_tick(self) -> bool:
        """GLib timer callback — fires every _WARMUP_INTERVAL_SECONDS."""
        if not self._machine or self._machine.state != State.RECORDING:
            self._warmup_timer_id = None
            return False  # stop timer
        self._schedule_warmup_ping()
        return True  # keep timer

    def _schedule_warmup_ping(self) -> None:
        """Fire a warmup ping in a daemon thread (non-blocking)."""
        threading.Thread(
            target=self._warmup_ping,
            daemon=True,
            name="voxize-warmup",
        ).start()

    def _warmup_ping(self) -> None:
        """Keep the pooled TLS socket warm; prime the cleanup cache once.

        The socket half is why this exists at all — without it the first
        post-stop call pays a TCP+TLS handshake.

        The first ping of a session does a second job: it carries the exact
        developer message the real cleanup call will send, so OpenAI has the
        ~1400-token prefix cached before the user stops talking. Measured on
        the production ``Cleanup`` class: a cleanup call with no preceding
        ping reports ``cached=0``; after a ping it reports ``cached=1385`` of
        1459 input tokens, billed at $0.02/M instead of $0.20/M.

        Be skeptical of the latency story. Observed TTFT deltas were 250 ms
        and 863 ms in favour of priming, but a later run with every arm warm
        produced 1117/1324/734 ms — i.e. run-to-run noise is the same size as
        the effect. The token accounting is deterministic; the speedup is not.

        Also note the cache survives across sessions (a cleanup call was
        observed hitting a prefix written minutes earlier by an unrelated
        run). Back-to-back dictations therefore hit without any help from
        here, and for those the priming ping is pure added cost. Its value is
        confined to the first dictation after a quiet spell — and recordings
        under 45 s never fire a ping at all, which is 46.7% of sessions.

        Only the *first* ping needs the payload. Later ones would rewrite a
        prefix that is already cached, and at ~$0.00007 each that is roughly
        the price of the cleanup call they exist to make cheaper — a 300 s
        recording fires six. So subsequent pings send a minimal request:
        enough to keep the socket alive, too small (<1024 tokens) to touch
        the cache either way. A recording long enough to outlive the cache
        TTL loses the priming, which is the 1.2% of sessions over 300 s.

        This was tried in April with gpt-5.4-nano and reverted: nano returned
        0 cached tokens on every probe (0/376 across a month of real
        sessions, see the 2026-04-21 and 2026-07-31 journal entries).

        The prefix must match to hit, which is why the priming ping mirrors
        ``Cleanup._run``'s request shape exactly — same model, same reasoning
        effort, system prompt as ``input[0]`` with ``role: developer`` rather
        than via ``instructions=``. The April attempt used ``instructions=``
        against a developer-role cleanup call; those render differently, so it
        may have been unable to hit even on a model that caches.

        ``max_output_tokens`` must clear the reasoning tokens or the response
        comes back ``status=incomplete`` (16 was too low — low-effort
        reasoning alone spends ~11).
        """
        client = self._client
        if not client:
            return
        # Lazy-import so the warmup path isn't what pulls voxize.cleanup
        # into memory; it is already resident via the bootstrap thread
        # by the time we get here.
        from voxize import cleanup as cleanup_mod

        prime = not self._warmup_primed
        if prime:
            # Same constructor Cleanup._run uses, so the prefix we write is
            # byte-for-byte the prefix it will look up.
            payload = [
                cleanup_mod.build_cache_prefix_input(self._prompts),
                {"role": "user", "content": "."},
            ]
        else:
            payload = [{"role": "user", "content": "."}]

        t0 = time.monotonic()
        logger.debug("warmup: start prime=%s", prime)
        try:
            response = client.responses.create(
                model=cleanup_mod.MODEL,
                reasoning={"effort": "low"},
                input=payload,
                max_output_tokens=64,
                stream=False,
                store=False,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            cached = self._accumulate_warmup_usage(response)
            if prime:
                self._warmup_primed = True
            logger.debug(
                "warmup: complete duration_ms=%d status=%s prime=%s cached_tokens=%d",
                duration_ms,
                getattr(response, "status", "unknown"),
                prime,
                cached,
            )
        except Exception as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            # Non-fatal by design: a failed ping still completed the TCP+TLS
            # handshake, so the socket is warm even when the call errors. Do
            # not set _warmup_primed — let the next ping retry the priming.
            logger.warning(
                "warmup: failed duration_ms=%d prime=%s error=%s",
                duration_ms,
                prime,
                e,
            )

    def _accumulate_warmup_usage(self, response) -> int:
        """Add one warmup response's tokens to the session total.

        Pings accumulate — a long recording fires one every 45 s — so this
        sums rather than replaces. Returns that ping's cached-token count for
        the log line, which is how we tell whether priming is working.
        """
        usage = getattr(response, "usage", None)
        if not usage:
            return 0
        details = getattr(usage, "input_tokens_details", None)
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        totals = self._warmup_usage or {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "pings": 0,
        }
        totals["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
        totals["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        totals["cached_tokens"] += cached
        totals["pings"] += 1
        self._warmup_usage = totals
        return cached

    # ── Phase 1 → Phase 2 transition ──

    def _begin_transcribing(self) -> bool:
        """Stop recording, cancel WS, start batch transcription."""
        logger.debug("_begin_transcribing: mock=%s", self._mock is not None)
        if self._machine.state != State.TRANSCRIBING:
            # Cancelled between the transition and this idle: the CANCELLED
            # teardown already owns audio, transcription and the lock.
            logger.debug("_begin_transcribing: skipped in %s", self._machine.state.name)
            return False

        # Short-circuit any bootstrap still importing openai or opening
        # the WS — we no longer need the live preview, batch does the
        # authoritative pass.
        self._bootstrap_cancelled = True

        # Handle mock mode
        mock_transcript = ""
        if self._mock_transcription:
            mock_transcript = self._mock_transcription.stop()
            self._mock_transcription = None

        if self._mock:
            self._release_lock()
            self._start_mock_batch(mock_transcript)
            return False

        # Release mic lock immediately — recording is done
        self._release_lock()

        # Grab references and null them to prevent races
        audio = self._audio
        self._audio = None
        transcription = self._transcription
        self._transcription = None
        session_dir = self._session_dir

        threading.Thread(
            target=self._stop_and_batch,
            args=(audio, transcription, session_dir),
            daemon=True,
            name="voxize-stop",
        ).start()

        return False  # one-shot idle

    def _stop_and_batch(self, audio, transcription, session_dir) -> None:
        """Background thread: stop audio, kick off batch, tear down WS in parallel.

        Once ``audio.stop()`` has finalized the WAV, the batch POST has
        everything it needs. Firing it before ``transcription.stop()``
        overlaps the WS close/usage-wait (~100ms) with batch TTFT.
        """
        # Step 1: stop audio — must complete before batch reads the WAV.
        logger.debug("_stop_and_batch: stopping audio")
        try:
            if audio:
                audio.stop()
        except Exception:
            logger.exception("Failed to stop audio")
        logger.debug("_stop_and_batch: audio stopped")

        # Step 1b: wait for the bootstrap thread to settle so self._client
        # is populated before we dispatch batch. On a fast stop (user hit
        # Escape immediately after the window showed), openai may still
        # be importing; we block up to 10s for it. Batch can't proceed
        # without a client. Transcription stop-up below uses the local
        # ``transcription`` snapshot so a late bootstrap finishing after
        # cancellation does not leak a dangling WS.
        if not self._bootstrap_done.wait(timeout=10.0):
            logger.warning("_stop_and_batch: bootstrap did not finish within 10s")

        # Log WAV file info (cheap; do before the handoff).
        wav_path = os.path.join(session_dir, "audio.wav") if session_dir else None
        wav_size = 0
        if wav_path and os.path.exists(wav_path):
            from voxize._audio_const import CHANNELS, SAMPLE_RATE, WAV_HEADER_SIZE

            wav_size = os.path.getsize(wav_path)
            logger.debug(
                "_stop_and_batch: wav_size=%d (%.1fs audio)",
                wav_size,
                (wav_size - WAV_HEADER_SIZE) / (SAMPLE_RATE * CHANNELS * 2),
            )

        opus_path = audio.opus_path if audio else None
        if opus_path:
            opus_size = os.path.getsize(opus_path)
            logger.debug(
                "_stop_and_batch: opus_path=%s opus_size=%d ratio=%.1fx",
                opus_path,
                opus_size,
                wav_size / opus_size if wav_size and opus_size else 0,
            )

        # Step 2: kick off batch now. It runs concurrently with the
        # transcription teardown that follows.
        logger.debug("_stop_and_batch: dispatching batch start")
        GLib.idle_add(self._start_batch, session_dir, opus_path)

        # Step 3: cancel WS (live_transcript.stop() can wait up to 2s for
        # usage events — that's what we're parallelising away).
        logger.debug("_stop_and_batch: cancelling transcription")
        live_usage = None
        live_transcript = ""
        # Bootstrap may have populated self._transcription after our
        # parent ``_begin_transcribing`` captured a None reference —
        # grab the latest so we don't leave a dangling WS open.
        if transcription is None:
            transcription = self._transcription
            self._transcription = None
        try:
            if transcription:
                transcription.stop()
                live_usage = transcription.usage
                live_transcript = transcription.transcript
        except Exception:
            logger.exception("Failed to stop transcription")
        logger.debug(
            "_stop_and_batch: transcription cancelled, live_transcript_len=%d",
            len(live_transcript),
        )

        # Arm batch progress bar before the first delta can arrive.
        GLib.idle_add(self._set_live_len, len(live_transcript))

        # Save live transcript for debugging (not the authoritative result)
        if live_transcript and session_dir:
            try:
                Path(session_dir, "live_transcript.txt").write_text(live_transcript)
            except Exception:
                logger.exception("Failed to save live_transcript.txt")

        # Step 4: hand live-preview usage back to the main loop.
        GLib.idle_add(self._set_live_usage, live_usage)

    def _set_live_usage(self, live_usage) -> bool:
        """Main loop: store live-preview usage once WS teardown settles."""
        logger.debug("_set_live_usage: live_usage=%s", live_usage)
        self._live_usage = live_usage
        return False

    def _teardown_async(self) -> None:
        """Move blocking teardown to a background thread."""
        logger.debug(
            "_teardown_async: audio=%s transcription=%s lock=%s",
            self._audio is not None,
            self._transcription is not None,
            self._lock is not None,
        )
        # Grab references and null them on the main loop to prevent races
        audio = self._audio
        self._audio = None
        transcription = self._transcription
        self._transcription = None
        lock = self._lock
        self._lock = None

        if not audio and not transcription and not lock:
            logger.debug("_teardown_async: nothing to tear down")
            return  # nothing to tear down

        threading.Thread(
            target=self._teardown_blocking,
            args=(audio, transcription, lock),
            daemon=True,
            name="voxize-teardown",
        ).start()

    @staticmethod
    def _teardown_blocking(audio, transcription, lock) -> None:
        """Background thread: stop providers, release lock."""
        logger.debug(
            "_teardown_blocking: audio=%s transcription=%s lock=%s",
            audio is not None,
            transcription is not None,
            lock is not None,
        )
        try:
            if audio:
                logger.debug("_teardown_blocking: stopping audio")
                audio.stop()
        except Exception:
            logger.exception("Teardown: failed to stop audio")
        try:
            if transcription:
                logger.debug("_teardown_blocking: cancelling transcription")
                transcription.cancel()
        except Exception:
            logger.exception("Teardown: failed to cancel transcription")
        try:
            if lock:
                logger.debug("_teardown_blocking: releasing lock")
                lock.release()
        except Exception:
            logger.exception("Teardown: failed to release lock")
        logger.debug("_teardown_blocking: complete")

    def _release_lock(self) -> None:
        if self._lock:
            logger.debug("_release_lock: releasing mic lock")
            self._lock.release()
            self._lock = None

    # ── Session costs ──

    # Pricing per million tokens
    _LIVE_INPUT_PRICE = 1.25  # gpt-4o-mini-transcribe
    _LIVE_OUTPUT_PRICE = 5.00
    _CLEANUP_INPUT_PRICE = 0.20  # gpt-5.6-luna
    _CLEANUP_CACHED_INPUT_PRICE = 0.02  # 10% of standard (OpenAI pricing page)
    _CLEANUP_OUTPUT_PRICE = 1.20
    # Luna also bills cache *writes* at 1.25x uncached input ($0.25/M), which
    # gpt-5.4-nano did not. `usage` cannot tell a cache-writing miss from a
    # plain miss, so we bill all uncached input at the standard rate and
    # under-report by at most 25% of the input line on cold calls.

    # Batch is priced per minute of audio, not per token — gpt-transcribe
    # reports {"seconds": N} instead of token counts. Metering is per second
    # with no whole-minute rounding and no minimum (measured: a 127.4s WAV
    # billed 128s).
    _BATCH_PRICE_PER_MIN = 0.0045  # gpt-transcribe

    def _batch_cost(self, usage: dict[str, int] | None) -> float | None:
        """Cost for a batch transcription call, from billed audio duration."""
        if not usage:
            return None
        seconds = usage.get("seconds")
        if seconds is None:
            return None
        return seconds / 60 * self._BATCH_PRICE_PER_MIN

    def _cleanup_cost(self, usage: dict[str, int] | None) -> float | None:
        """Cost for a cleanup call, splitting cached vs non-cached input tokens."""
        if not usage:
            return None
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        cached = usage.get("cached_tokens", 0) or 0
        uncached = max(inp - cached, 0)
        return (
            uncached * self._CLEANUP_INPUT_PRICE
            + cached * self._CLEANUP_CACHED_INPUT_PRICE
            + out * self._CLEANUP_OUTPUT_PRICE
        ) / 1_000_000

    def _show_session_costs(self) -> None:
        """Compute dollar costs from provider usage and display in UI."""
        logger.debug(
            "_show_session_costs: live_usage=%s batch_usage=%s cleanup_usage=%s "
            "warmup_usage=%s",
            self._live_usage,
            self._batch_usage,
            self._cleanup_usage,
            self._warmup_usage,
        )

        def _cost(usage, in_price, out_price):
            if not usage:
                return None
            inp = usage["input_tokens"]
            out = usage["output_tokens"]
            return (inp * in_price + out * out_price) / 1_000_000

        l_cost = _cost(
            self._live_usage, self._LIVE_INPUT_PRICE, self._LIVE_OUTPUT_PRICE
        )
        b_cost = self._batch_cost(self._batch_usage)
        c_cost = self._cleanup_cost(self._cleanup_usage)
        # Pings hit the cleanup model at identical pricing (including the
        # cached-input split).  Fold into the cleanup bucket for display.
        w_cost = self._cleanup_cost(self._warmup_usage)
        if c_cost is not None and w_cost is not None:
            c_cost += w_cost
        elif w_cost is not None:
            c_cost = w_cost
        logger.debug(
            "_show_session_costs: live=%s batch=%s cleanup=%s (incl. pings=%s)",
            f"{l_cost:.4f}" if l_cost is not None else "n/a",
            f"{b_cost:.4f}" if b_cost is not None else "n/a",
            f"{c_cost:.4f}" if c_cost is not None else "n/a",
            f"{w_cost:.4f}" if w_cost is not None else "n/a",
        )

        parts = (l_cost, b_cost, c_cost)
        if any(c is not None for c in parts):
            self._emit(
                protocol.event(
                    "costs",
                    live=l_cost,
                    batch=b_cost,
                    cleanup=c_cost,
                    total=sum(c for c in parts if c is not None),
                )
            )
